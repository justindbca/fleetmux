"""Named sub-fleets (docs/design.md §29): roster ``subfleets:`` entries, external up/down, service children.

A named sub-fleet is declared like a worker (roster ``subfleets:``, hot) and run by the §19 machinery
(``subfleet.spawn_child`` with ``named=<entry>``): the entry's ``supervisor_slot`` worker becomes the
child's supervisor, the child runs its own roster and worker runtime, and a service child has no task.

Parent Redis
  fx:subfleet:<name>      hash: state (down / starting / up / paused / draining), slot, redis_socket,
                          run_dir, up_at, drain_since, drain_reason, crashloop, usage_at
  fx:subfleet:opq         list of external op ids still open
  fx:subfleet:op:<id>     hash: op, name, reason, status (awaiting / approved), created, approved_at

External ops ``{"id", "op": "subfleet.up" | "subfleet.down", "name", "reason"}`` arrive as service
requests but never go through validate_request / REQQ / request_timeout. The program gate answers
what it can from state alone (unknown name, autostart down, already up / already down, limits);
everything else asks the parent supervisor, whose service decision JSON carries
``"subfleet": {"id", "decision": "approve" | "reject", "note"}``. Execution is the daemon's (``tick``).
The result is published under ``fx:result:<id>`` like any service request.

Internal API
  declare(fleet, entries)           roster reload: new / removed / re-added entries
  request(fleet, spec) -> id        inside the request event (transaction-safe: Redis + queue only)
  validate_decision(item)           shape of the decision's ``subfleet`` field (parse_service_decision)
  decide(fleet, item)               supervisor approve / reject (transaction-safe)
  tick(fleet)                       parent daemon tick, outside transactions (called by subfleet.tick)
  revive(fleet, d, rec, cfg)        autostart child daemon gone: relaunch unless crashlooping
  collected(fleet, rec, returned)   subfleet.collect finished a named child
"""
import json
import re
import shlex
import time
from pathlib import Path

from fleetmux import roster, subfleet

OPS = ('subfleet.up', 'subfleet.down')
HASH = 'fx:subfleet:'
OPQ = 'fx:subfleet:opq'
OP = 'fx:subfleet:op:'
DOWN_OUTCOMES = ('down', 'stopped')  # collect outcomes that are a planned down, not a failure
SLOT_WAIT = 600  # seconds an approved up waits for the supervisor slot to be idle / READY
CRASH_WINDOW, CRASH_MAX = 3600, 3  # autostart: this many daemon deaths within the window = crashloop
# A returned session's last Stop marks its channel stopping (claude_hook.STOPPING_TTL), then the rewake waits
# claude_hook.GRACE: past this, nothing will take the return handoff (§29).
RETURN_WAKE_S = 180
USAGE_EVERY = 30  # seconds between two context-usage reads of an up autostart child's supervisor


def _fleet():
    from fleetmux import fleet
    return fleet


def declared(f):
    return getattr(f, 'subfleet_decl', None) or {}


def state(f, name):
    return f.r.hgetall(HASH + name)


def live(f):
    """{name: child run dir} of the named children this fleet runs now (starting / running / stopping)."""
    out = {}
    for d in subfleet.active_children(f.run_dir):
        rec = subfleet.record(d)
        if rec.get('named'):
            out[rec['name']] = d
    return out


def address(f, name):
    h = state(f, name)
    return {'name': name, 'state': h.get('state') or 'down', 'redis_socket': h.get('redis_socket') or '',
            'run_dir': h.get('run_dir') or ''}


# ------------------------------------------------------------------ declaration (roster reload)
def declare(f, entries):
    """Roster reload accepted ``entries`` (roster.subfleet_entries). A removed entry that is up drains
    and goes down; added back before that, the drain is cancelled. A changed entry applies at its next up."""
    before = declared(f)
    now = {e['name']: e for e in entries}
    f.subfleet_decl = now
    running = live(f)
    for name in now:
        if name not in before:
            f.log('subfleet.declared', name=name, slot=now[name]['supervisor_slot'], autostart=now[name]['autostart'],
                  mode=now[name]['mode'])
            h = state(f, name)
            if h.get('state') == 'draining' and h.get('drain_reason') == 'removed' and name in running \
                    and subfleet.record(running[name]).get('state') == 'running':
                f.r.hset(HASH + name, mapping={'state': 'up', 'drain_since': '', 'drain_reason': ''})
                f.log('subfleet.declared', name=name, result='removal cancelled')
        elif now[name] != before[name]:
            f.log('subfleet.declared', name=name, result='changed; applies at its next up')
    for name in before:
        if name not in now:
            f.log('subfleet.declared', name=name, result='removed' + ('; draining' if name in running else ''))
            if name in running:
                begin_drain(f, name, 'removed')


# ------------------------------------------------------------------ external ops (inside the request event)
def is_op(spec):
    return isinstance(spec, dict) and spec.get('op') in OPS


class InProgress(Exception):
    """The same id resent while its op is still open (awaiting / approved): not a new op, not refused."""
    def __init__(self, rid, status):
        super().__init__(rid + ' is in progress (' + status + ')')
        self.rid, self.status = rid, status


def request(f, spec):
    """Take one external up / down. ValueError: refused before it got an id (malformed / duplicate).
    InProgress: the same op under the same id is still open (a retry, e.g. after the parent resumed)."""
    fleet = _fleet()
    rid = spec.get('id')
    if not isinstance(rid, str) or not re.fullmatch(fleet.REQ_ID_RE, rid):
        raise ValueError('id must match ' + fleet.REQ_ID_RE)
    if not isinstance(spec.get('name'), str) or not spec['name']:
        raise ValueError('name is required')
    if spec.get('reason') is not None and not isinstance(spec['reason'], str):
        raise ValueError('reason must be text')
    extra = sorted(set(spec) - {'id', 'op', 'name', 'reason'})
    if extra:
        raise ValueError('unknown keys for ' + spec['op'] + ': ' + ', '.join(extra))
    open_op = f.r.hgetall(OP + rid)
    if open_op and open_op.get('op') == spec['op'].split('.', 1)[1] and open_op.get('name') == spec['name']:
        raise InProgress(rid, open_op.get('status') or 'awaiting')
    if (open_op or f.r.hgetall(fleet.REQ + rid) or f.r.get(fleet.RESULT + rid)
            or (f.v2 and f.r.get(f.v2.prefix + ':public:' + _digest(rid)))):
        raise ValueError('duplicate request id: ' + rid)
    op, name = spec['op'].split('.', 1)[1], spec['name']
    f.r.hset(OP + rid, mapping={'op': op, 'name': name, 'reason': spec.get('reason') or '', 'status': 'awaiting',
                                'created': time.time(), 'approved_at': 0})
    f.r.rpush(OPQ, rid)
    f.log('subfleet.request_in', id=rid, op=op, name=name, reason=spec.get('reason') or '')
    verdict = gate(f, op, name)
    if verdict is not None:
        finish(f, rid, *verdict)
        return rid
    ask(f, rid, op, name, spec.get('reason') or '')
    return rid


def ask(f, rid, op, name, reason):
    f.queue('supervisor', '[Request ' + rid + '] External request ' + rid + ' wants sub-fleet ' + name + ' '
            + op + ('. Reason: ' + reason if reason else '') + '.\nYou only approve or reject; '
            'the daemon does the rest. Reply with the service decision JSON from AGENTS.md: status RUNNING, '
            'dispatch [], skip [], plus "subfleet": {"id": "' + rid + '", "decision": "approve" or "reject", '
            '"note": "<one sentence>"}.')


def requeue_awaiting(f):
    """Parent ``--resume`` (service): resume cancels every undelivered supervisor message, approvals included.
    Each op still awaiting its approval is asked again, so every accepted op reaches a final result; approved
    ops need nothing (tick runs them)."""
    for rid in f.r.lrange(OPQ, 0, -1):
        op = f.r.hgetall(OP + rid)
        if op.get('status') == 'awaiting':
            ask(f, rid, op['op'], op['name'], op.get('reason') or '')
            f.log('subfleet.request_requeued', id=rid, op=op['op'], name=op['name'])


def _digest(rid):
    from fleetmux import runtime_v2
    return runtime_v2.digest(rid)


def gate(f, op, name):
    """Program verdict (status, fields) without the supervisor, or None (ask the supervisor)."""
    entry = declared(f).get(name)
    if entry is None:
        return 'rejected', {'reason': 'program: no sub-fleet named ' + name + ' is declared'}
    if op == 'down' and entry['autostart']:
        return 'rejected', {'reason': 'program: ' + name + ' is autostart; it cannot be taken down from outside'}
    running = name in live(f)
    h = state(f, name)
    if op == 'up' and running and h.get('state') in ('up', 'paused'):
        return 'ok', address(f, name)
    if op == 'down' and not running:
        return 'ok', {'name': name, 'state': 'down'}
    if op == 'up' and not running:
        reason = limits_problem(f, entry)
        if reason:
            return 'rejected', {'reason': 'program: ' + reason}
    return None


def limits_problem(f, entry):
    try:
        workers = len(roster.load_entries(entry['roster'])[0])
    except (OSError, ValueError) as exc:
        return 'sub-fleet roster unreadable: ' + str(exc)
    return subfleet.check(f, entry['supervisor_slot'], entry['name'], workers, ready=False, named=entry)


def finish(f, rid, status, fields):
    op = f.r.hgetall(OP + rid)
    f.publish(rid, status, **fields)
    f.r.delete(OP + rid)
    f.r.lrem(OPQ, 0, rid)
    if status == 'rejected':
        f.log('subfleet.rejected', id=rid, name=op.get('name', ''), reason=fields.get('reason', ''))


def validate_decision(item):
    if (not isinstance(item, dict) or not isinstance(item.get('id'), str)
            or item.get('decision') not in ('approve', 'reject') or not isinstance(item.get('note', ''), str)):
        raise ValueError('subfleet must be {"id", "decision": "approve" | "reject", "note"}')


def decide(f, item):
    rid = item['id']
    op = f.r.hgetall(OP + rid)
    if op.get('status') != 'awaiting':
        f.log('subfleet.decision_unknown', id=rid)
        return
    if item['decision'] == 'reject':
        finish(f, rid, 'rejected', {'reason': 'supervisor: ' + (item.get('note') or 'rejected')})
        return
    f.r.hset(OP + rid, mapping={'status': 'approved', 'approved_at': time.time()})
    f.log('subfleet.approved', id=rid, op=op['op'], name=op['name'], note=item.get('note', ''))


# ------------------------------------------------------------------ parent tick (outside transactions)
def child_peek(cfg):
    """What the parent needs from a child's Redis, or None when it is unreachable."""
    fleet = _fleet()
    from fleetmux import runtime_v2
    if not subfleet.redis_up(cfg):
        return None
    try:
        r = fleet.connect(cfg)
    except Exception:
        return None
    try:
        bus = runtime_v2.bus_for(cfg, r)
        names = ['supervisor'] + list(cfg.get('workers') or [])
        sup = r.hgetall(fleet.key('agent', 'supervisor'))
        flying = [n for n in names if r.hget(bus.lease_key(n), 'current_delivery')]
        queued = r.llen(fleet.REQQ) + sum(r.llen(bus.pending_key(n)) for n in names)
        return {'ready': sup.get('runtime_ready') == '1' and sup.get('state') in ('idle', 'busy'),
                'paused': r.hget(fleet.SERVICE, 'paused') == '1', 'quiet': not flying and not queued,
                'sup_idle': sup.get('state') == 'idle' and 'supervisor' not in flying}
    except Exception:
        return None
    finally:
        r.close(); r.connection_pool.disconnect()


def begin_drain(f, name, why):
    """Stop taking work (the parent hash says draining) and go down once nothing is in flight."""
    if state(f, name).get('state') == 'draining':
        return
    f.r.hset(HASH + name, mapping={'state': 'draining', 'drain_since': time.time(), 'drain_reason': why})
    f.log('subfleet.rotate' if why == 'rotate' else 'subfleet.draining', name=name, reason=why)


def stop_child(f, d, rec, cfg, reason):
    rec.update(state='stopping', outcome='down', reason=reason,
               requests=subfleet.child_requests(cfg))  # for the slot's return note: its Redis goes with the child
    subfleet.save_record(d, rec)
    subfleet.post_stop(cfg)
    f.log('subfleet.stopping', task=rec['task_id'], outcome='down', reason=reason)


def over_threshold(f, slot):
    """(used, window) when ``slot``'s session is past its handoff ratio, else None (design §22)."""
    used, window = f.usage(slot)
    ratio = float(f.agent(slot).get('handoff_ratio') or f.cfg['handoff_ratio'])
    return (used, window) if window and used >= ratio * window else None


def rotation_gate(f, slot):
    """§29 N1: never delegate a session over its context threshold. True: delegate now; False: a
    rotation was started (the next generation's READY lets the delegation through)."""
    over = over_threshold(f, slot)
    if not over:
        return True
    info = f.agent(slot)
    gen = info.get('generation') or '0'
    if int(info.get('delivered') or 0) == 0 and info.get('delegated_gen') != gen:
        f.log('rotate.impossible', agent=slot, used=over[0], window=over[1],
              reason='fresh session already over ratio; delegating anyway')
        return True
    f.start_rotation(slot, *over)
    return False


def try_up(f, entry, goal=''):
    """('started', rec) | ('wait', why) | ('refused', why). Spawns only when the slot is idle / READY."""
    name, slot = entry['name'], entry['supervisor_slot']
    try:
        workers = len(roster.load_entries(entry['roster'])[0])
    except (OSError, ValueError) as exc:
        return 'refused', 'sub-fleet roster unreadable: ' + str(exc)
    reason = subfleet.check(f, slot, name, workers, ready=False, named=entry)
    if reason:
        return 'refused', reason
    busy = subfleet.check(f, slot, name, workers, ready=True, named=entry)
    if busy:
        return 'wait', busy
    if not rotation_gate(f, slot):
        return 'wait', slot + ' is rotating to a fresh session first'
    if subfleet.tmux_slot(f.cfg, slot) and subfleet.parent_view(f, slot, f.agent(slot).get('pane')) is None:
        # Declared while the slot already ran as a plain cmux pane (hot roster): this delegation takes the
        # §19 move-surface; the slot's next generation starts in the fleet's tmux (dual display from then on).
        f.log('subfleet.view_fallback', name=name, slot=slot,
              reason='slot not yet in fleet tmux; dual display from next generation')
    try:
        rec = subfleet.spawn_child(f, slot, name, goal or ('Named sub-fleet ' + name), workers, named=entry)
    except subfleet.Refused as exc:
        return 'refused', str(exc)
    cfg = _fleet().load(Path(rec['run']) / 'config.json')
    f.setf(slot, delegated_gen=f.agent(slot).get('generation') or '0')
    f.r.hset(HASH + name, mapping={'state': 'starting', 'slot': slot, 'run_dir': rec['run'],
                                   'redis_socket': cfg['redis_socket'], 'started_at': time.time(), 'up_at': '',
                                   'drain_since': '', 'drain_reason': '', 'crashloop': '', 'blocked': ''})
    return 'started', rec


def ready_limit(f):
    return float(f.cfg['profile_ready_timeout']) + subfleet.LAUNCH_GRACE


def return_tick(f, now):
    """A returned slot's handoff (subfleet.collect) nobody took within RETURN_WAKE_S: no rewake fallback was
    left to wake the idle session, so the daemon's skeleton note goes to the next generation now (§29)."""
    for slot in f.workers:
        info = f.agent(slot)
        if not (info.get('subfleet_return') and info.get('state') == 'handoff' and f.v2) or f.v2.handoff_taken(slot):
            continue
        waited = now - float(json.loads(info['subfleet_return']).get('at') or now)
        if waited >= RETURN_WAKE_S:
            f.log('subfleet.return_untaken', slot=slot, waited=round(waited))
            f.finish_rotation(slot, timed_out=True)


def tick(f):
    fleet = _fleet()
    return_tick(f, time.time())
    decl = declared(f)
    if not decl and not f.r.llen(OPQ) and not live(f):
        return
    now = time.time()
    running = live(f)
    for name, d in running.items():
        rec = subfleet.record(d)
        if rec.get('state') != 'running':
            continue
        try:
            cfg = fleet.load(Path(d) / 'config.json')
        except (OSError, ValueError):
            continue  # subfleet.tick marks it failed
        h = state(f, name)
        peek = child_peek(cfg)
        if name not in decl and h.get('state') != 'draining':
            begin_drain(f, name, 'removed')
            h = state(f, name)
        if h.get('state') == 'draining':
            limit = float((decl.get(name) or {}).get('drain_s') or roster.DRAIN_S)
            quiet = bool(peek and peek['quiet'])
            if quiet or peek is None or now - float(h.get('drain_since') or now) >= limit:
                if not quiet and peek is not None:
                    f.log('subfleet.drain_timeout', name=name, drain_s=limit)
                    for rid, op in ops(f, name, 'down'):
                        finish(f, rid, 'timeout', {'reason': 'drain exceeded'})
                stop_child(f, d, rec, cfg, 'down (' + (h.get('drain_reason') or 'external') + ')')
            continue
        if peek is None:
            continue
        if h.get('state') == 'starting' and peek['ready']:
            since = time.strftime('%H:%M', time.localtime(now))
            f.r.hset(HASH + name, mapping={'state': 'paused' if peek['paused'] else 'up', 'up_at': now})
            f.log('subfleet.up', name=name, slot=rec['slot'], redis_socket=cfg['redis_socket'], run_dir=str(d),
                  line=name + ': up via ' + f.label(rec['slot']) + ' since ' + since)
        elif h.get('state') in ('up', 'paused'):
            want = 'paused' if peek['paused'] else 'up'
            if want != h['state']:
                f.r.hset(HASH + name, mapping={'state': want})
                f.log('subfleet.paused' if want == 'paused' else 'subfleet.up', name=name,
                      line=name + ': ' + want)
            entry = decl.get(name)
            if (entry and entry['autostart'] and want == 'up' and peek['sup_idle']
                    and now - float(h.get('usage_at') or 0) >= USAGE_EVERY):
                f.r.hset(HASH + name, mapping={'usage_at': now})
                if over_threshold(f, rec['slot']):
                    begin_drain(f, name, 'rotate')  # down -> rotation gate -> delegated again (autostart)
    for rid in f.r.lrange(OPQ, 0, -1):
        op = f.r.hgetall(OP + rid)
        if not op:
            f.r.lrem(OPQ, 0, rid)
        elif op.get('status') == 'approved':
            (op_up if op['op'] == 'up' else op_down)(f, rid, op, now)
    for name, entry in decl.items():
        if (entry['autostart'] and name not in live(f) and state(f, name).get('crashloop') != '1'
                and not ops(f, name, 'up', approved=True)):
            result = try_up(f, entry)
            if result[0] == 'started':
                f.log('subfleet.autostart', name=name, slot=entry['supervisor_slot'], run=result[1]['run'])
            elif result[1] != state(f, name).get('blocked'):  # each new reason once
                f.r.hset(HASH + name, mapping={'blocked': result[1]})
                f.log('subfleet.autostart_wait', name=name, reason=result[1])


def ops(f, name, kind, approved=False):
    out = []
    for rid in f.r.lrange(OPQ, 0, -1):
        op = f.r.hgetall(OP + rid)
        if op.get('name') == name and op.get('op') == kind and (not approved or op.get('status') == 'approved'):
            out.append((rid, op))
    return out


def op_up(f, rid, op, now):
    name = op['name']
    entry = declared(f).get(name)
    if entry is None:
        return finish(f, rid, 'rejected', {'reason': 'program: ' + name + ' is no longer declared'})
    if name in live(f):
        h = state(f, name)
        if h.get('state') in ('up', 'paused'):
            finish(f, rid, 'ok', address(f, name))
        elif h.get('state') == 'starting' and now - float(h.get('started_at') or now) >= ready_limit(f):
            finish(f, rid, 'timeout', {'reason': 'child not READY'})
        return  # starting / draining: wait for READY or for the down to finish
    result = try_up(f, entry, op.get('reason') or '')
    if result[0] == 'refused':
        finish(f, rid, 'rejected', {'reason': 'program: ' + result[1]})
    elif result[0] == 'wait' and now - float(op.get('approved_at') or now) >= SLOT_WAIT:
        finish(f, rid, 'timeout', {'reason': 'slot busy'})


def op_down(f, rid, op, now):
    name = op['name']
    if name not in live(f):
        return finish(f, rid, 'ok', {'name': name, 'state': 'down'})
    begin_drain(f, name, 'external')  # finished by collected() once the child is back


# ------------------------------------------------------------------ crash / collect
def revive(f, d, rec, cfg):
    """An autostart child's daemon is gone (§29 2.1): restart its Redis + ``daemon --resume`` unless it
    died CRASH_MAX times within CRASH_WINDOW (then it is collected like before and waits for an operator)."""
    fleet = _fleet()
    name = rec['name']
    entry = declared(f).get(name)
    if not entry or not entry['autostart'] or state(f, name).get('state') == 'draining':
        return False
    now = time.time()
    crashes = [t for t in rec.get('crashes') or [] if now - t < CRASH_WINDOW] + [now]
    rec['crashes'] = crashes
    if len(crashes) >= CRASH_MAX:
        rec['outcome'] = 'crashloop'
        subfleet.save_record(d, rec)
        f.r.hset(HASH + name, mapping={'crashloop': 1})
        f.log('subfleet.crashloop', name=name, crashes=len(crashes), window_s=CRASH_WINDOW)
        return False
    try:
        if not subfleet.redis_up(cfg):
            fleet.start_redis(cfg, tries=300)  # reloads its AOF from the same directory
        command = [cfg['python'], str(Path(fleet.__file__).resolve()), 'daemon', str(Path(d) / 'config.json'),
                   '--resume']
        result = fleet.manager_for(cfg).spawn_window('daemon', shlex.join(command), metadata={'type': 'daemon'},
                                                     start_directory=str(d))
        if not getattr(result, 'success', True):
            raise RuntimeError(str(getattr(result, 'error', 'spawn failed')))
    except (Exception, SystemExit) as exc:
        f.log('subfleet.revive_failed', name=name, reason=str(exc))
        subfleet.save_record(d, rec)
        return False
    rec['launched'] = now
    subfleet.save_record(d, rec)
    f.log('subfleet.autostart', name=name, reason='daemon died; relaunched', crashes=len(crashes))
    return True


def collected(f, rec, returned):
    """subfleet.collect took a named child back: the parent hash says down, open downs answer ok."""
    name = rec['name']
    f.r.hset(HASH + name, mapping={'state': 'down', 'redis_socket': '', 'up_at': '', 'drain_since': '',
                                   'down_at': time.time(), 'outcome': rec.get('outcome') or ''})
    for rid, op in ops(f, name, 'down', approved=True):
        finish(f, rid, 'ok', {'name': name, 'state': 'down'})
    f.log('subfleet.down', name=name, outcome=rec.get('outcome'), returned=returned, line=name + ': down')

