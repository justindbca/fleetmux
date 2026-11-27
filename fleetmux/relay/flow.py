"""The relay flow on one machine: claim, take over, release dependents, leave, come back (04-design 2, 5-10).

`Flow.step()` runs one round. Every task record change is a transition of `records` pushed by compare-and-swap.
The flow talks to three replaceable parts, so the same logic runs on the git hub (hubstore.py) and on in-memory
fakes in unit tests:

store       the state remote. `tasks() -> {seg: (version, record)}` (OSError when unreadable),
            `nodes() -> {node: record}`, `cas_task(seg, expected, record, snap=None) -> True | False | None`
            (expected None = create only; snap = snapshot commit for `snap/<seg>`, pushed atomically;
            None = outcome unknown), `put_node(node, record)`, `put_salvage(seg, node, epoch, content) -> bool`,
            `salvages(seg) -> {name: content}` with name `salvage/<seg>/<node>-e<epoch>`.
workspace   this machine's worktrees. `prepare(seg, record, takeover) -> worktree` (start point, TAKEOVER.md on
            takeover, deploy branch), `snapshot(seg, record, kind) -> {commit, snap, skipped?} | None`
            (None: unchanged for kind auto, or failed; worker index / HEAD untouched),
            `progress(seg, record) -> dict | None`, `publish(seg, epoch, branch, sha) -> None | why` (push the
            delivered commit), `salvage(seg, epoch) -> content | None` (everything local),
            `salvage_note(seg, epoch, name, content)`,
            `queue_path(seg, epoch)` (the dev_task gate's queue file).
executor    launch.Executor: `start(job, seg, spec) -> handle` (with `epoch`), `stop(handle)`,
            `running(job) -> {seg: handle}`; `start_spec(seg, record, worktree, takeover) -> spec`.

Liveness comes from `judge(nodes, read_ok) -> {node: online | leaving | lost}`; this machine's own entry is filled
from its own state. A wake is a gap on the wall clock (the monotonic clock stops while a Mac sleeps); settle and
stagger use the monotonic clock, so they count only time awake.

Only the long-running process (`start_work=True`) starts and stops sub-fleets; it begins in the rejoin path and
derives what it holds from the table (owner == this machine) and the executor. One-shot commands only write records.
"""
import copy

from . import coordinator, deps, knobs, launch, placement
from . import records as R
from .errors import TransitionError

NOTE_SHA = 12                   # hex digits of a commit named in a notice

ONLINE, LEAVING, REJOINING = R.ONLINE, R.LEAVING, R.REJOINING
TAKEABLE_OWNER = ('lost', 'leaving')
HELD_HERE = (R.WORKING, R.PARKED, R.HANDED_OVER)


def salvage_ref(seg, node, epoch):
    return 'salvage/%s/%s-e%d' % (seg, node, epoch)


class Flow:
    def __init__(self, me, job, *, store, workspace, executor, start_spec, judge, params, clock, notify,
                 machine=None, score=None, start_work=True, settle_on_start=True, peer_sync=None):
        self.me = me
        self.job = job
        self.store = store
        self.ws = workspace
        self.executor = executor
        self.start_spec = start_spec
        self.judge = judge
        self.params = params
        self.clock = clock
        self.notify = notify or (lambda kind, text: None)
        self.machine = machine or (lambda: {})
        self.score = score              # capability score hook (the state layer's scoring); None = weights
        self.start_work = start_work
        self.peer_sync = peer_sync      # nodes -> None: copies the peers' measured blocks into the inventory (R20)
        self.peer_sync_error = None
        self.hub_ok = True
        self._pushed = None             # (content, monotonic time) of the last node push
        self.running_error = None       # why the root fleet's list of running sub-fleets was unreadable
        self.leaving_reason = None
        self.owned = {}                 # seg -> {'epoch', 'handle', 'worktree', 'queue', 'deploy', 'snap_due'}
        self.table = {}                 # seg -> (version, record), last good read
        self.nodes = {}
        self.judged = {}
        self.queue = placement.ClaimQueue()
        self.beat = 0
        self.last_step = None           # monotonic time of the last round
        self.last_wall = None           # wall time of the last round (wake detection)
        self._gone_since = {}
        self._seen_salvage = set()
        self._peer_view = {}
        self._unplaceable = {}
        if start_work:                  # the relay process: read the table before anything else
            self.state = REJOINING
            self.table_read = False
            self.rejoin_since = clock.mono()
            self.settle = settle_on_start
        else:
            self.state = ONLINE
            self.table_read = True
            self.rejoin_since = None
            self.settle = False

    # ------------------------------------------------------------------ helpers
    def _p(self, key):
        return knobs.get(self.params, key)

    def records(self):
        return {s: r for s, (_, r) in self.table.items()}

    def node_record(self):
        role = coordinator.NAME if coordinator.NAME in self.owned else None
        recs = self.records()
        tasks = [{'id': s, 'state': (recs.get(s) or {}).get('state') or R.WORKING,
                  'phase': ((recs.get(s) or {}).get('checkpoint') or {}).get('phase'), 'epoch': o['epoch']}
                 for s, o in sorted(self.owned.items()) if not coordinator.is_role(s)]
        return {'node': self.me, 'beat': self.beat, 'state': self.state, 'leaving_reason': self.leaving_reason,
                'tasks': tasks, 'slots': [], 'machine': self.machine(), 'fleet': {'machine': self.me},
                'idle': self.state == ONLINE and not tasks, 'role': role}

    def _push_node(self):
        """Push this machine's record when its content changed (state, tasks, idle, role, leaving reason) or a
        beat is due (beat_s since the last push). The measured machine block alone does not count as a change."""
        rec = self.node_record()
        content = {k: v for k, v in rec.items() if k not in ('beat', 'machine')}
        now = self.clock.mono()
        if (self._pushed is not None and content == self._pushed[0]
                and now - self._pushed[1] < self._p('beat_s')):
            return
        self.beat += 1
        rec['beat'] = self.beat
        self.store.put_node(self.me, rec)
        self._pushed = (content, now)

    def _read(self):
        try:
            self.table = self.store.tasks()
            self.nodes = self.store.nodes()
        except OSError:
            return False
        return True

    def _sync_peers(self):
        if self.peer_sync is None:
            return
        try:
            self.peer_sync(self.nodes)
            self.peer_sync_error = None
        except Exception as exc:                    # noqa: BLE001 -- the inventory never stops the relay
            self.peer_sync_error = str(exc)

    def _write(self, seg, version, new, snap=None):
        """One compare-and-swap. An unknown outcome is settled by reading the table again, never assumed."""
        res = self.store.cas_task(seg, version, new, snap=snap)
        read = self._read()
        if res is None:
            return bool(read) and self.records().get(seg) == new
        return bool(res)

    def _move(self, seg, transition, **kw):
        """Apply a records transition to the current record and push it; False when refused or not allowed."""
        if seg not in self.table:
            return False
        version, rec = self.table[seg]
        snap = kw.pop('_snap', None)
        try:
            new = transition(rec, **kw)
        except TransitionError:
            return False
        return self._write(seg, version, new, snap=snap)

    # ------------------------------------------------------------------ the round
    def step(self):
        now = self.clock.mono()
        wall = self.clock.wall()
        if self.last_wall is not None and wall - self.last_wall > self._p('wake_gap_s'):
            self.wake(now)
        self.last_step = now
        self.last_wall = wall
        if self.state == LEAVING:
            return
        ok = self._read()
        self.hub_ok = ok                # the run loop backs off while the hub cannot be read
        if ok:
            self._sync_peers()
        if self.state == REJOINING:
            self._rejoin_round(ok, now)
            return
        if not ok:
            return
        self._observe(now)
        self._fence_all()
        if self.state == LEAVING:                   # a one-shot `relay leave` handed our segments over
            self._push_node()
            return
        self._start_missing()
        self._delivery_watch()
        self._snapshot_round(now)
        self._advance()
        self._claim_round(now)
        self._salvage_watch()
        self._push_node()

    def _observe(self, now):
        judged = {n: c for n, c in dict(self.judge(self.nodes, True)).items() if c in ('online',) + TAKEABLE_OWNER}
        judged[self.me] = ONLINE if self.state == ONLINE else self.state
        for node, cls in judged.items():
            if cls in TAKEABLE_OWNER:
                self._gone_since.setdefault(node, now)
            else:
                self._gone_since.pop(node, None)
        self.judged = judged
        self._peer_notices()

    def _peer_notices(self):
        """The coordinator's holder tells its supervisor when a machine leaves, is lost, or is back and idle."""
        holder = coordinator.holder(self.records())
        for node, cls in sorted(self.judged.items()):
            if node == self.me:
                continue
            rec = self.nodes.get(node) or {}
            view = (cls, rec.get('state'), bool(rec.get('idle')))
            prev = self._peer_view.get(node)
            self._peer_view[node] = view
            if holder != self.me or prev is None or prev == view:
                continue
            if cls in TAKEABLE_OWNER:
                self.notify('relay.peer', '%s is %s' % (node, 'gone (goodbye)' if cls == 'leaving' else 'lost'))
            elif rec.get('state') == ONLINE and (prev[1] != ONLINE or prev[0] != ONLINE):
                self.notify('relay.peer', '%s is online%s' % (node, ', idle' if rec.get('idle') else ''))

    # ------------------------------------------------------------------ what this machine holds
    def _entry(self, seg, epoch, handle=None, worktree=None):
        e = {'epoch': epoch, 'handle': handle, 'worktree': worktree, 'queue': None, 'deploy': None,
             'snap_due': None}
        if not coordinator.is_role(seg) and self.ws is not None:
            e['queue'] = str(self.ws.queue_path(seg, epoch))
            e['deploy'] = launch.deploy_branch_of(self.job, seg)
        return e

    def derive_holdings(self):
        """What this machine holds, from the table (owner == this machine, not finished) and from the sub-fleets
        that run here now. A sub-fleet of a segment that is no longer ours is held too, so the fence stops it and
        salvages its work. False when the running sub-fleets cannot be listed: nothing is derived, so a live
        sub-fleet is never mistaken for a gone one and started twice."""
        try:
            running = self.executor.running(self.job) if self.start_work else {}
            self.running_error = None
        except Exception as exc:                    # noqa: BLE001 -- the root fleet's status is unreadable now
            self.running_error = str(exc)
            return False
        for seg, rec in sorted(self.records().items()):
            if seg in self.owned:
                continue
            run = running.get(seg)
            if rec.get('owner') == self.me and rec.get('state') in HELD_HERE:
                handle = run if run and run.get('epoch') == rec['epoch'] else None
                self.owned[seg] = self._entry(seg, rec['epoch'], handle=handle)
            elif run is not None and run.get('epoch') is not None:
                self.owned[seg] = self._entry(seg, run['epoch'], handle=run)
        return True

    def _fence_all(self):
        for seg in list(self.owned):
            rec = self.records().get(seg)
            mine = self.owned[seg]
            same = rec is not None and rec.get('owner') == self.me and rec.get('epoch') == mine['epoch']
            if same and rec.get('state') in (R.WORKING, R.PARKED):
                continue
            if same and rec.get('state') == R.DELIVERED:
                self.owned.pop(seg)         # delivered by me: done with it
                continue
            if same and rec.get('state') == R.HANDED_OVER:
                if self.state == REJOINING:     # back and nobody took it: carry on (04-design 8.2)
                    self._move(seg, R.resume, me=self.me)
                elif self.state == ONLINE:      # handed over by a one-shot `relay leave`: leave as well
                    self.state = LEAVING
                    self.leaving_reason = (self.nodes.get(self.me) or {}).get('leaving_reason') or 'operator'
                continue
            self._lose(seg, rec)

    def _lose(self, seg, rec):
        """The segment changed owner (or was delivered by its new owner): stop it here, push everything local to
        the salvage ref, never write the segment again. The local worktree stays."""
        mine = self.owned.pop(seg)
        if mine.get('handle') is not None and self.start_work:
            try:
                self.executor.stop(mine['handle'])
            except Exception as exc:                # noqa: BLE001 -- report and go on with the salvage
                self.notify('relay.lost', 'stopping %s failed: %s' % (seg, exc))
        name = salvage_ref(seg, self.me, mine['epoch'])
        if not coordinator.is_role(seg):
            content = self.ws.salvage(seg, mine['epoch'])
            if content is not None:
                self.store.put_salvage(seg, self.me, mine['epoch'], content)
        self.notify('relay.lost', '%s (epoch %d) is now %s by %s; local work pushed to %s' % (
            seg, mine['epoch'], (rec or {}).get('state'), (rec or {}).get('owner'), name))

    def _start_missing(self):
        """Start the work of a segment held here that has no running sub-fleet (taken by a one-shot command, or
        its sub-fleet was not found after a restart)."""
        if not self.start_work:
            return
        recs = self.records()
        for seg, rec in sorted(recs.items()):
            if rec.get('owner') == self.me and rec.get('state') == R.WORKING and seg not in self.owned:
                self.owned[seg] = self._entry(seg, rec['epoch'])
        for seg in sorted(self.owned):
            e = self.owned[seg]
            rec = recs.get(seg) or {}
            if coordinator.is_role(seg) or e.get('handle') is not None or rec.get('state') != R.WORKING:
                continue
            why = str(((rec.get('owners') or [{}])[-1]).get('why') or '')
            self._start(seg, rec, takeover=why.startswith('takeover'))

    # ------------------------------------------------------------------ delivery (the dev_task gate passed)
    def _delivery_watch(self):
        """The dev_task gate appends a queue row only after the tests and the fast-forward passed."""
        for seg in sorted(self.owned):
            e = self.owned[seg]
            if not e.get('queue') or not e.get('deploy'):
                continue
            row = launch.queued_delivery(e['queue'], e['deploy'])
            if row is None:
                continue
            why = self.ws.publish(seg, e['epoch'], e['deploy'], row['sha'])    # dependents start from it
            if why is not None:                     # not on the hub yet: do not release dependents; next round
                self.notify('relay.take', '%s: delivered commit %s not pushed to %s yet: %s' % (
                    seg, row['sha'][:NOTE_SHA], e['deploy'], why[:knobs.cold(self.params, 'reason_max')]))
                continue
            self.deliver(seg, commit=row['sha'], gate='pass', review=row.get('review'))

    # ------------------------------------------------------------------ automatic snapshots (R5)
    def _snapshot_round(self, now):
        for seg in sorted(self.owned):
            e = self.owned[seg]
            if coordinator.is_role(seg) or (self.records().get(seg) or {}).get('state') != R.WORKING:
                continue
            if e.get('snap_due') is None:
                e['snap_due'] = now + self._p('snapshot_s')
                continue
            if now >= e['snap_due']:
                e['snap_due'] = now + self._p('snapshot_s')
                self.checkpoint(seg, kind='auto')

    # ------------------------------------------------------------------ dependencies
    def _advance(self):
        for seg, new in deps.advance(self.records()).items():
            self._write(seg, self.table[seg][0], new)     # refused = another machine advanced it first

    # ------------------------------------------------------------------ claims and takeovers
    def _takeable(self, seg, rec, now):
        if R.claimable(rec):
            return R.WHY_CLAIM
        if rec.get('state') not in (R.WORKING, R.HANDED_OVER) or rec.get('owner') in (None, self.me):
            return None
        if coordinator.is_role(seg) and not coordinator.conf(self.params)['takeover']:
            return None
        cls = self.judged.get(rec['owner'])
        if rec.get('state') == R.HANDED_OVER and cls not in TAKEABLE_OWNER:
            cls = 'leaving' if (self.nodes.get(rec['owner']) or {}).get('state') == LEAVING else cls
        if cls not in TAKEABLE_OWNER:
            return None
        if cls == 'leaving' or rec.get('state') == R.HANDED_OVER:
            since = self._gone_since.get(rec['owner'], now)
            if now - since < self._p('offline_grace_s'):
                return None
            return R.WHY_GOODBYE
        return R.WHY_LOST

    def _claim_round(self, now):
        records = self.records()
        nodes = dict(self.nodes, **{self.me: self.node_record()})
        cands = placement.candidates(nodes, self.judged, records)
        order_key = lambda s: (0 if records[s].get('weight') == 'heavy' else 1, s)   # noqa: E731
        for seg in sorted(records, key=order_key):
            rec = records[seg]
            why = self._takeable(seg, rec, now)
            if why is None or seg in self.owned:
                self.queue.forget(seg)
                continue
            if coordinator.is_role(seg):
                order = coordinator.rank(rec, nodes, self.judged, self.params, score=self.score)
            else:
                order = placement.rank(rec, cands, self.params, score=self.score)
            if not order:
                self._report_unplaceable(seg, rec, cands, why)
                continue
            self._unplaceable.pop(seg, None)
            wait = placement.stagger_s(order, self.me, self.params)
            if wait is None:
                continue
            if self.queue.due(seg, self.table[seg][0], wait, now=now) and self.take(seg, why=why):
                records = self.records()
                nodes = dict(self.nodes, **{self.me: self.node_record()})
                cands = placement.candidates(nodes, self.judged, records)

    def _report_unplaceable(self, seg, rec, cands, why):
        """No machine can take it: a gone owner's segment goes back to the table as `reclaimed` (04-design 6.5);
        the coordinator's holder (or anyone, when there is none) tells its supervisor what is missing."""
        if why != R.WHY_CLAIM:
            self._move(seg, R.takeover, to=None, why=why)
        reason = placement.unplaceable_reason(rec, cands, self.params)
        holder = coordinator.holder(self.records())
        if self._unplaceable.get(seg) == reason or holder not in (None, self.me):
            return
        self._unplaceable[seg] = reason
        self.notify('relay.unplaceable', '%s cannot be placed: %s' % (seg, reason))

    def check_take(self, seg):
        """Why this machine may not take `seg` now (None when it may): the same filter as automatic claims."""
        rec = self.records().get(seg)
        if rec is None:
            return 'no such segment'
        if self.state != ONLINE:
            return 'this machine is ' + self.state
        if seg in self.owned:
            return 'already held here'
        if coordinator.is_role(seg):
            return None
        if rec.get('state') == R.BLOCKED:
            return 'blocked on ' + ', '.join(deps.waiting_on(self.records(), seg))
        cand = next((c for c in placement.candidates(dict(self.nodes, **{self.me: self.node_record()}),
                                                     dict(self.judged, **{self.me: ONLINE}), self.records())
                     if c['id'] == self.me), None)
        if cand is None:
            return 'this machine is not a candidate'
        return placement.refusal(cand, rec, self.params)

    def take(self, seg, *, why=R.WHY_CLAIM):
        """Compare-and-swap the record to this machine (epoch + 1); the relay process then starts its work."""
        if not self.table_read or seg not in self.table:
            return False
        rec = self.table[seg][1]
        if why == R.WHY_CLAIM:
            ok = self._move(seg, R.claim, me=self.me)
        else:
            ok = self._move(seg, R.takeover, to=self.me, why=why)
        if not ok:
            self.queue.forget(seg)
            return False
        new = self.records()[seg]
        self.owned[seg] = self._entry(seg, new['epoch'])
        self.queue.forget(seg)
        if coordinator.is_role(seg):
            self.notify('relay.take', 'this machine now holds the coordinator role (epoch %d)' % new['epoch'])
        elif self.start_work:
            self._start(seg, new, takeover=rec.get('owner') is not None and why != R.WHY_CLAIM)
        else:
            self.notify('relay.take', '%s (epoch %d) recorded here; the relay process starts its sub-fleet' % (
                seg, new['epoch']))
        self._push_node()
        return True

    def _start(self, seg, rec, *, takeover):
        """Prepare the worktree and start the sub-fleet. A failure (worktree, spec, the root fleet's refusal)
        parks the segment with the reason, so it is reported once and not retried every round."""
        e = self.owned[seg]
        try:
            wt = self.ws.prepare(seg, rec, takeover)
            e['worktree'] = wt
            spec = self.start_spec(seg, rec, wt, takeover)
            e['handle'] = self.executor.start(self.job, seg, spec)
        except Exception as exc:                    # noqa: BLE001 -- park and report, the relay goes on
            reason = 'sub-fleet start failed: %s' % (str(exc) or type(exc).__name__)
            self.park(seg, reason=reason[:knobs.cold(self.params, 'reason_max')])
            self.notify('relay.take', '%s (epoch %d) could not be started and is parked: %s' % (
                seg, rec['epoch'], reason))
            return
        self.notify('relay.take', '%s %s (epoch %d), sub-fleet started' % (
            'took over' if takeover else 'claimed', seg, rec['epoch']))

    # ------------------------------------------------------------------ salvage on the new owner's side
    def _salvage_watch(self):
        for seg in sorted(self.owned):
            if coordinator.is_role(seg):
                continue
            for name, content in sorted(self.store.salvages(seg).items()):
                if name in self._seen_salvage:
                    continue
                self._seen_salvage.add(name)
                self.ws.salvage_note(seg, self.owned[seg]['epoch'], name, content)
                self.notify('relay.salvage', '%s: the previous owner pushed its local work to %s; compare and '
                                             'merge what is useful (notes/SALVAGE.md)' % (seg, name))

    # ------------------------------------------------------------------ checkpoints, delivery, park
    def _update_owned(self, seg, transition, **kw):
        """Move an owned segment; a refusal means it changed owner: come back through the rejoin path."""
        if seg not in self.owned or not self.table_read:
            return False
        if self._move(seg, transition, **kw):
            return True
        rec = self.records().get(seg) or {}
        if rec.get('owner') != self.me or rec.get('epoch') != self.owned[seg]['epoch']:
            self.wake(self.clock.mono())
        return False

    def checkpoint(self, seg, *, kind='auto', phase=None):
        rec = (self.table.get(seg) or (None, {}))[1]
        snap = self.ws.snapshot(seg, rec, kind)
        if snap is None:
            return False
        if snap.get('skipped'):         # secret-looking files are not in the snapshot; a takeover lacks them
            self.notify('relay.snapshot', '%s: the snapshot left out %d file(s) whose names match relay.secret_globs, '
                        'so they stay only on this machine and a takeover will not have them: %s' % (
                            seg, len(snap['skipped']), ', '.join(sorted(snap['skipped']))))
        return self._update_owned(seg, R.checkpoint, commit=snap.get('commit'), snap=snap.get('snap'), kind=kind,
                                  phase=phase, progress=self.ws.progress(seg, rec), _snap=snap.get('snap'))

    def deliver(self, seg, *, commit, gate='pass', review=None):
        rec = (self.table.get(seg) or (None, {}))[1]
        if seg not in self.owned or rec.get('state') != R.WORKING:
            return False
        ok = self._update_owned(seg, R.deliver, branch=rec.get('branch'), commit=commit, gate=gate, review=review)
        if ok:
            self.owned.pop(seg, None)
            self._advance()                 # release dependents now; every other machine would do it too
            self._push_node()
        return ok

    def park(self, seg, *, reason):
        return self._update_owned(seg, R.park, reason=reason, by=self.me)

    def adopt_from_table(self):
        """A one-shot command's view of what this machine holds."""
        self.derive_holdings()

    # ------------------------------------------------------------------ leave (04-design section 5)
    def leave(self, reason):
        """Snapshot every segment, hand each over, then say goodbye. A failed step does not stop the next."""
        self._read()
        self.derive_holdings()
        for seg in sorted(self.owned):
            try:
                if not coordinator.is_role(seg):
                    self.checkpoint(seg, kind='auto')
            except Exception as exc:                # noqa: BLE001 -- the goodbye must still go out
                self.notify('relay.leave', 'snapshot of %s failed: %s' % (seg, exc))
            try:
                self._move(seg, R.hand_over)
            except Exception as exc:                # noqa: BLE001
                self.notify('relay.leave', 'hand-over of %s failed: %s' % (seg, exc))
        self.state = LEAVING
        self.leaving_reason = reason
        self._push_node()

    # ------------------------------------------------------------------ come back (04-design section 8)
    def wake(self, now):
        """A long gap between rounds, or a refused CAS: read the table before any task push; settle first."""
        self.state = REJOINING
        self.leaving_reason = None
        self.rejoin_since = now
        self.table_read = False
        self.settle = True
        self.store.put_node(self.me, dict(self.node_record(), tasks=[]))

    def _rejoin_round(self, ok, now):
        if not ok:
            if self.beat == 0:
                self._push_node()
            return                              # keep rejoining, write no task ref until the table is read
        if not self.table_read:
            if not self.derive_holdings():      # stay rejoining, read the root fleet's status again next round
                self._push_node()
                return
            self.table_read = True
            self._observe(now)
            self._fence_all()                   # keep what is still mine, salvage what was taken
            self._start_missing()
        if not self.settle or now - self.rejoin_since >= self._p('settle_s'):
            self.state = ONLINE
            self._observe(now)
            self._push_node()
            return
        self._salvage_watch()
        self._push_node()
