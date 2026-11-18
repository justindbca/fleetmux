"""`fleetmux relay <command>` (docs/relay.md, "Commands").

init <job> --from <job.yaml>       create the task records on the state remote (dependents `blocked`)
join <job> --repo <path>           this machine takes part in the job
status [<job>]                     machines and segments as this machine sees them
leave [--reason R]                 snapshot, hand every segment over, say goodbye
checkpoint <seg> --phase <name>    record a phase checkpoint of a segment held here
deliver <seg> [--commit SHA]       mark a segment held here delivered (after its gate passed)
take <seg>                         take a segment by hand (same checks and compare-and-swap as automatic claims)
park <seg> --reason R              stop a segment held here until a person looks at it
run --node <name>                  the long-running relay process (started by the root fleet's daemon)

Exit codes: 0 done, 1 refused, 2 usage, 69 the state layer is not available.
"""
import argparse
import hashlib
import json
import os
import signal
import sys
import time
from pathlib import Path

from . import coordinator, events, flow, jobfile, knobs
from . import records as R
from .backend import Unavailable, _NoExecutor, default_backend

APPLET_NAME = 'FLEETMUX_APPLET_NAME'            # set by the root fleet's daemon for the processes it starts

SECONDS_PER_MINUTE = 60                   # status: ages under a minute show in seconds
EXIT_REFUSED = 1
EXIT_UNAVAILABLE = 69
EXIT_HELD = 78                                  # applet host: held until a person fixes it (no restart)
COMMANDS = ('init', 'join', 'status', 'leave', 'checkpoint', 'deliver', 'take', 'park', 'run')


class Refused(Exception):
    """The command was understood and refused; the message says why."""


class _Clock:
    def mono(self):
        return time.monotonic()

    def wall(self):
        return time.time()


def parser():
    p = argparse.ArgumentParser(prog='fleetmux relay', description='Develop one job in segments on several '
                                'machines through one git remote.')
    sub = p.add_subparsers(dest='cmd', required=True, metavar='{' + ','.join(COMMANDS) + '}')
    s = sub.add_parser('init', help='create the task records of a job on the state remote')
    s.add_argument('job')
    s.add_argument('--from', dest='job_file', required=True, help='the job file: <repo>/.relay/<job>/job.yaml')
    s = sub.add_parser('join', help='this machine takes part in a job')
    s.add_argument('job')
    s.add_argument('--repo', required=True, help='this machine\'s clone of the project repo')
    s = sub.add_parser('status', help='machines and segments of a job')
    s.add_argument('job', nargs='?')
    s = sub.add_parser('leave', help='hand every segment over and say goodbye')
    s.add_argument('--reason', choices=('shutdown', 'sleep', 'operator'), default='operator')
    for name, text in (('checkpoint', 'record a phase checkpoint'), ('deliver', 'mark a segment delivered'),
                       ('take', 'take a segment by hand'), ('park', 'stop a segment until a person looks at it')):
        s = sub.add_parser(name, help=text)
        s.add_argument('seg')
        s.add_argument('--job')
        if name == 'checkpoint':
            s.add_argument('--phase', required=True)
        if name == 'deliver':
            s.add_argument('--commit')
        if name == 'park':
            s.add_argument('--reason', required=True)
    s = sub.add_parser('run', help='the long-running relay process (started by the root fleet\'s daemon)')
    s.add_argument('--node')
    return p


def default_state_dir():
    from fleetmux import paths
    return paths.state_dir() / 'relay'


class Ctx:
    def __init__(self, backend, state_dir, out, err):
        self.b = backend
        self.state_dir = Path(state_dir)
        self.out = out
        self.err = err

    def say(self, text):
        self.out.write(text + '\n')

    # ------------------------------------------------------------------ membership (local, per machine)
    def member_path(self, job):
        return self.state_dir / 'jobs' / (job + '.json')

    def members(self):
        d = self.state_dir / 'jobs'
        return sorted(p.stem for p in d.glob('*.json')) if d.is_dir() else []

    def member(self, job=None):
        jobs = self.members()
        if job is None:
            if not jobs:
                raise Refused('this machine has joined no job: run `fleetmux relay join <job> --repo <path>`')
            if len(jobs) > 1:
                raise Refused('this machine has joined several jobs (' + ', '.join(jobs) + '): pass --job')
            job = jobs[0]
        path = self.member_path(job)
        if not path.is_file():
            raise Refused('this machine has not joined job ' + job + ': run `fleetmux relay join`')
        return json.loads(path.read_text())

    def view_path(self, job):
        """The running relay's view of one job (the state layer's ViewStore): node classes, last changes and
        notice keys."""
        return self.state_dir / ('view-' + job + '.json')

    def view(self, job):
        """(ViewStore, fresh) or (None, False). Fresh = a node change saved by `run` within lost_after_s (this
        machine's own record changes on every push, so a running relay keeps it fresh)."""
        path = self.view_path(job)
        if not path.is_file():
            return None, False
        store = events.ViewStore(path, _Clock())
        last = max([e.get('last_change_wall') or 0 for e in store.nodes.values()] or [0])
        return store, _Clock().wall() - last < knobs.get(self.b.params(), 'lost_after_s')

    def view_classes(self, job):
        store, fresh = self.view(job)
        if store is None or not fresh:
            return {}
        return {n: e['class'] for n, e in store.nodes.items() if e.get('class')}

    def flow(self, job, node, *, run=False, settle_on_start=True, notify=None):
        """A Flow for this machine. One-shot commands only write records; `run` (the only actor that starts and
        stops sub-fleets) begins in the rejoin path and derives what it holds from the table."""
        b = self.b
        score = getattr(b, 'score', None)
        judge = b.judge(job)
        if not run:                                     # a one-shot has no history: use the run's fresh view
            seen = self.view_classes(job)
            base = judge
            judge = lambda nodes, ok: dict(dict(base(nodes, ok)), **seen)    # noqa: E731
        f = flow.Flow(node, job, store=b.store(job), workspace=b.workspace(job), executor=b.make_executor(job),
                      start_spec=b.start_spec(job), judge=judge, params=b.params(), clock=_Clock(),
                      notify=notify or (lambda kind, text: self.say(kind + ': ' + text)), machine=b.machine,
                      score=score() if callable(score) else None, start_work=run, settle_on_start=settle_on_start,
                      peer_sync=b.peer_sync(job) if run and callable(getattr(b, 'peer_sync', None)) else None)
        if run:
            return f
        if not f._read():
            raise Refused('the state remote of job ' + job + ' is not readable now')
        f._observe(f.clock.mono())
        f.adopt_from_table()
        return f


# ------------------------------------------------------------------ commands
def cmd_init(ctx, a):
    job_file = Path(a.job_file).resolve()
    if job_file.name != 'job.yaml' or job_file.parent.name != a.job or job_file.parent.parent.name != '.relay':
        raise Refused('--from must be <repo>/.relay/' + a.job + '/job.yaml')
    repo = job_file.parent.parent.parent
    try:
        doc = jobfile.load_job(repo, a.job)
        jobfile.check_job_tree(repo, a.job)
    except jobfile.JobError as exc:
        raise Refused(str(exc)) from None
    store = ctx.b.store(a.job)
    if store.tasks():
        raise Refused('job ' + a.job + ' already has task records on the state remote')
    records = coordinator.job_records(doc, ctx.b.params(), base=ctx.b.head(repo))
    made = [seg for seg, rec in sorted(records.items()) if store.cas_task(seg, None, rec)]
    if len(made) != len(records):
        raise Refused('job ' + a.job + ': created ' + ', '.join(made) + ' only; another init ran at the same time')
    ctx.say('job %s: %d task records created (%s)' % (a.job, len(made), ', '.join(made)))
    return 0


def cmd_join(ctx, a):
    jobfile.check_name('job', a.job)
    node = ctx.b.node_name()
    path = ctx.member_path(a.job)
    path.parent.mkdir(parents=True, exist_ok=True)
    old = json.loads(path.read_text()) if path.is_file() else {}
    member = {'job': a.job, 'repo': str(Path(a.repo).resolve()), 'node': node}
    path.write_text(json.dumps(dict(member, returning=bool(old.get('returning')))))
    store = ctx.b.store(a.job)
    try:
        store.tasks()                                       # one read of the state remote (tasks and nodes)
        peers = store.nodes()
        known = node in peers
    except OSError as exc:
        raise Refused('the state remote of job ' + a.job + ' is not readable now: ' + str(exc)) from None
    path.write_text(json.dumps(dict(member, returning=known)))
    f = flow.Flow(node, a.job, store=store, workspace=None, executor=None, start_spec=None,
                  judge=ctx.b.judge(a.job), params=ctx.b.params(), clock=_Clock(), notify=None,
                  machine=ctx.b.machine, start_work=False)
    f.state = R.first_state(peers.get(node))               # a first join is online; a return settles first
    f._push_node()
    sync = getattr(ctx.b, 'peer_sync', None)
    if callable(sync):
        sync(a.job)(peers)                                  # R20: the peers' capabilities into the inventory
    ctx.say('joined job %s as %s (%s)' % (a.job, node, f.state))
    return 0


def _ago(seconds):
    if seconds is None:
        return '-'
    seconds = int(seconds)
    return '%ds' % seconds if seconds < SECONDS_PER_MINUTE else '%dm ago' % (seconds // SECONDS_PER_MINUTE)


def render_status(job, hub, records, nodes, judged, seen, me=None):
    lines = ['job %s   hub %s   %d nodes' % (job, hub, len(nodes)), '%-9s %-9s %-8s %s' % (
        'NODE', 'STATE', 'SEEN', 'TASKS')]
    for node in sorted(nodes):
        rec = nodes[node] or {}
        state = judged.get(node) or 'unknown'
        if node == me and rec.get('state'):            # this machine: its own record says what it is
            state = rec['state']
        elif state == 'online' and rec.get('state') in ('rejoining', 'leaving'):
            state = rec['state']
        tasks = ', '.join('%s (%s, e%s%s)' % (t['id'], t.get('state'), t.get('epoch'),
                                                ', phase ' + t['phase'] if t.get('phase') else '')
                          for t in rec.get('tasks') or []) or '-'
        lines.append('%-9s %-9s %-8s %s' % (node, state, _ago(seen.get(node)), tasks))
    lines += ['', '%-12s %-12s %-9s %-6s %-22s %s' % ('TASK', 'STATE', 'OWNER', 'EPOCH', 'CHECKPOINT', 'DEPENDS')]
    for seg in sorted(records, key=lambda s: (s in ('merge', 'coordinator'), s)):
        r = records[seg]
        cp = r.get('checkpoint') or {}
        cps = ('phase ' + str(cp.get('phase')) if cp.get('kind') == 'phase' else 'auto #' + str(cp.get('seq'))) \
            if cp else '-'
        note = ''
        last = (r.get('owners') or [{}])[-1]
        if str(last.get('why', '')).startswith('takeover') and len(r.get('owners') or []) > 1:
            prev = r['owners'][-2]['node']
            note = '   taken over from %s (%s)' % (prev, 'lost' if last['why'].endswith('lost') else 'goodbye')
        lines.append('%-12s %-12s %-9s %-6s %-22s %s%s' % (
            seg, r.get('state'), r.get('owner') or '-', r.get('epoch', 0), cps,
            ' '.join(r.get('depends_on') or []) or '-', note))
    return '\n'.join(lines)


def cmd_status(ctx, a):
    jobs = [a.job] if a.job else ctx.members()
    if not jobs:
        raise Refused('name a job, or join one first')
    for job in jobs:
        store = ctx.b.store(job)
        try:
            tasks = store.tasks()
            nodes = store.nodes()
        except OSError as exc:
            raise Refused('the state remote of job ' + job + ' is not readable now: ' + str(exc)) from None
        records = {s: r for s, (_, r) in tasks.items()}
        judged = dict(ctx.b.judge(job)(nodes, True))
        judged.update(ctx.view_classes(job))
        seen = dict(ctx.b.observations(job))
        view, _ = ctx.view(job)
        now = time.time()
        for node, e in (view.nodes.items() if view is not None else ()):
            if e.get('last_change_wall') is not None:
                seen.setdefault(node, max(now - e['last_change_wall'], 0))
        me = ctx.member(job)['node'] if job in ctx.members() else None
        ctx.say(render_status(job, ctx.b.hub(job), records, nodes, judged, seen, me=me))
    return 0


def cmd_leave(ctx, a):
    jobs = ctx.members()
    if not jobs:
        raise Refused('this machine has joined no job')
    for job in jobs:
        m = ctx.member(job)
        f = ctx.flow(job, m['node'])
        held = sorted(f.owned)
        f.leave(a.reason)
        ctx.say('job %s: left (%s); handed over: %s' % (job, a.reason, ', '.join(held) or 'nothing'))
    return 0


def _held(f, seg):
    if seg not in f.owned:
        rec = f.records().get(seg)
        raise Refused('%s is not held by this machine (%s)' % (
            seg, 'no such segment' if rec is None else '%s, owner %s' % (rec.get('state'), rec.get('owner'))))


def cmd_checkpoint(ctx, a):
    m = ctx.member(a.job)
    f = ctx.flow(m['job'], m['node'])
    _held(f, a.seg)
    if not f.checkpoint(a.seg, kind='phase', phase=a.phase):
        raise Refused('checkpoint of %s was refused: it changed owner; this machine now rejoins' % a.seg)
    ctx.say('%s: phase checkpoint %s recorded' % (a.seg, a.phase))
    return 0


def cmd_deliver(ctx, a):
    m = ctx.member(a.job)
    f = ctx.flow(m['job'], m['node'])
    _held(f, a.seg)
    commit = a.commit or ctx.b.head(m['repo'])
    if not f.deliver(a.seg, commit=commit):
        raise Refused('delivery of %s was refused (state %s)' % (a.seg, f.records()[a.seg].get('state')))
    ctx.say('%s: delivered at %s' % (a.seg, commit))
    return 0


def cmd_take(ctx, a):
    m = ctx.member(a.job)
    f = ctx.flow(m['job'], m['node'])
    rec = f.records().get(a.seg)
    why = None if rec is None else f._takeable(a.seg, rec, f.clock.mono())
    if why is None:
        reason = f.check_take(a.seg)
        if reason is None and rec is not None and rec.get('owner'):
            reason = 'held by %s (%s)' % (rec['owner'], rec.get('state'))
        raise Refused('cannot take %s: %s' % (a.seg, reason or rec.get('state')))
    reason = f.check_take(a.seg)
    if reason is not None:
        raise Refused('cannot take %s: %s' % (a.seg, reason))
    if not f.take(a.seg, why=why):
        raise Refused('another machine took %s first' % a.seg)
    ctx.say('%s: taken (%s, epoch %d)' % (a.seg, why, f.records()[a.seg]['epoch']))
    return 0


def cmd_park(ctx, a):
    m = ctx.member(a.job)
    f = ctx.flow(m['job'], m['node'])
    _held(f, a.seg)
    if not f.park(a.seg, reason=a.reason):
        raise Refused('park of %s was refused' % a.seg)
    ctx.say('%s: parked (%s)' % (a.seg, a.reason))
    return 0


NOTICE_KEY_DIGITS = 12                          # hex digits of a notice text's hash in its once-only key
EXIT_RESTART = 75                               # applet host: restart at once, not counted as a crash


def hub_backoff(params, failures):
    """Seconds before the next read after `failures` failed reads in a row: poll_s, doubling, capped at
    max(poll_s, beat_s). Only existing relay keys; dedicated keys need the config block (params.py)."""
    poll = knobs.get(params, 'poll_s')
    cap = max(poll, knobs.get(params, 'beat_s'))
    return min(cap, poll * 2 ** max(failures - 1, 0))


def hub_give_up_s(params):
    """Unreachable this long (monotonic): the peers judge this machine lost and take its segments, so the
    process exits 75 and its successor goes through the rejoin path."""
    return knobs.get(params, 'lost_after_s')


class GiveUp(Exception):
    """Every joined job's hub has been unreachable for hub_give_up_s."""


def params_write_back(hot, block, path=None):
    """params.write_back: hot values changed in Redis -> the private fleet.yaml's relay.hot (a seam for tests)."""
    from . import params
    return params.write_back(hot, block, path=path)


class HotParams:
    """The relay's hot values while `run` runs under the root fleet: seeded into the host Redis hash
    (params.REDIS_KEY) at start, read from there on every use, and written back every write_back_seconds and on
    a clean stop. A failed write-back is logged; the loop goes on."""

    def __init__(self, settings, store, *, err, path=None, clock=None):
        self.settings = settings
        self.store = store
        self.err = err
        self.path = path
        self.clock = clock or time.monotonic
        self.last = None

    def start(self):
        from . import params
        params.seed(self.store, self.settings.block)
        self.last = self.clock()

    def tick(self):
        if self.clock() - self.last >= self.settings.hot('write_back_seconds'):
            self.flush()

    def flush(self):
        self.last = self.clock()
        try:
            params_write_back(self.store, self.settings.block, path=self.path)
        except Exception as exc:                    # noqa: BLE001 -- ConfigError / OSError: keep running
            self.err.write('relay run: hot values not written back: %s\n'
                           % str(exc)[:knobs.cold(self.settings, 'reason_max')])


def text_limits(params=None):
    """(notify_max, reason_max) from the relay settings, else from the shipped relay block (the private one may be
    what is broken), else (None, None): no clipping."""
    try:
        if params is None:
            from fleetmux import fleet_config
            params = fleet_config.block('relay', fleet_config.SHIPPED)
        return knobs.cold(params, 'notify_max'), knobs.cold(params, 'reason_max')
    except Exception:                               # noqa: BLE001 -- clipping is cosmetic
        return None, None


def hold(app, err, exc, params=None):
    """Unavailable under the applet host (bad relay configuration, no `source: self` machine): a restart will not
    fix it, so tell the supervisor once what to fix; the caller exits 78 (held)."""
    text = 'relay held (exit %d) until this is fixed and the relay applet is restarted: %s' % (EXIT_HELD, exc)
    notify_max, reason_max = text_limits(params)
    err.write('relay run: %s\n' % text)
    if app is not None:
        try:
            app.notify(text[:notify_max])
        except Exception as notice_exc:             # noqa: BLE001 -- the exit code still says held
            err.write('relay run: notice not sent: %s\n' % str(notice_exc)[:reason_max])


def tick(beat, err, reason_max=None):
    """One tick of the applet loop heartbeat (relay_applet.LoopHeartbeat). A failed beat is logged, never a
    crash: the host sees the stale heartbeat if it lasts."""
    try:
        beat.tick()
    except Exception as exc:                        # noqa: BLE001
        err.write('relay run: heartbeat failed: %s\n' % str(exc)[:reason_max])


class Runner:
    """The `run` loop's passes: one Flow per joined job, a job joined later is picked up, each job is stepped
    when due (every poll_s; after hub_backoff while its hub cannot be read). Under the applet a pass beats first
    and never waits longer than the SDK's heartbeat_s."""

    def __init__(self, ctx, a, beat):
        self.ctx = ctx
        self.node = a.node
        self.beat = beat                # relay_applet.LoopHeartbeat under the applet host, else None
        self.app = beat.applet if beat is not None else None
        self.hot = None                 # HotParams under the applet host
        self.flows = {}
        self.views = {}                 # job -> ViewStore shared by the notices and the node view
        self.due = {}                   # job -> monotonic time of its next step
        self.down = {}                  # job -> {'since': mono, 'failures': n}

    def sync_members(self):
        ctx = self.ctx
        for job in ctx.members():
            if job in self.flows:
                continue
            m = ctx.member(job)
            self.views[job] = events.ViewStore(ctx.view_path(job), _Clock())
            note = job_notifier(job_events(ctx, job, self.app, self.views[job]), job) if self.app is not None else None
            self.flows[job] = ctx.flow(job, self.node or m['node'], run=True, notify=note,
                                       settle_on_start=bool(m.get('returning', True)))
            if not m.get('returning'):                      # from now on a restart is a return (settle first)
                ctx.member_path(job).write_text(json.dumps(dict(m, returning=True)))

    def round(self):
        """One pass; returns the seconds to wait. Raises GiveUp when every job's hub is gone for too long."""
        if self.beat is not None:
            tick(self.beat, self.ctx.err, knobs.cold(self.ctx.b.params(), 'reason_max'))
        if self.hot is not None:
            self.hot.tick()
        p = self.ctx.b.params()
        poll = knobs.get(p, 'poll_s')
        self.sync_members()
        now = time.monotonic()
        for job, f in sorted(self.flows.items()):
            if now < self.due.get(job, now):
                continue
            f.step()
            if getattr(f, 'hub_ok', True):
                self.down.pop(job, None)
                self.due[job] = now + poll
                self.save_view(job, f)
                continue
            d = self.down.setdefault(job, {'since': now, 'failures': 0})
            d['failures'] += 1
            self.due[job] = now + hub_backoff(p, d['failures'])
        if self.flows and len(self.down) == len(self.flows) and all(
                now - d['since'] >= hub_give_up_s(p) for d in self.down.values()):
            raise GiveUp('the hub of every joined job has been unreachable for %s s' % hub_give_up_s(p))
        wait = min([t - now for t in self.due.values()] or [poll])
        if self.app is not None:
            wait = min(wait, self.app.heartbeat_s)
        return max(wait, 0)

    def save_view(self, job, f):
        """What this pass saw, for one-shot commands and `status`: each node's record and class."""
        store = self.views[job]
        views = getattr(f.store, 'node_views', None) or {}
        for node, rec in (f.nodes or {}).items():
            v = views.get(node)
            sha = getattr(v, 'sha', None) or hashlib.sha1(
                json.dumps(rec, sort_keys=True, default=str).encode('utf-8')).hexdigest()
            store.put_node(node, rec, sha, f.judged.get(node))
        store.save()

    def leave(self, reason):
        for f in self.flows.values():
            f.leave(reason)


def cmd_run(ctx, a):
    """One Flow per joined job, a round every `poll_s`. Exit codes for the applet host (docs/applet-interface.md):
    0 after a stop (segments handed over, reason `shutdown`); 75 when every joined job's hub stayed unreachable
    for lost_after_s (restart; segments kept). With no job joined it stays up and polls for one. Started by the
    root fleet's daemon it runs under the applet SDK (heartbeat, stop handling); by hand it runs its own loop and
    only writes records, since starting sub-fleets needs the root fleet."""
    if not os.environ.get(APPLET_NAME) and isinstance(ctx.b.make_executor(None), _NoExecutor):
        raise Unavailable('`relay run` starts sub-fleets and must run inside the root fleet: set '
                          '`relay.enabled: true` and let the root fleet start it')
    beat = None
    if os.environ.get(APPLET_NAME):                 # started by the root fleet as the built-in relay applet
        from fleetmux import relay_applet
        beat = relay_applet.loop_heartbeat()        # SDK without its heartbeat thread; the loop ticks it
    runner = Runner(ctx, a, beat)
    app = runner.app
    use_hot = getattr(ctx.b, 'use_hot', None)
    if app is not None and callable(use_hot) and getattr(app, 'redis', None) is not None:
        from . import params
        store = params.RedisHot(app.redis)
        runner.hot = HotParams(use_hot(store), store, err=ctx.err)
        runner.hot.start()                          # seed fx:relay:params; hot values are read from there
    stop = []

    def on_signal(*_):
        stop.append(True)
        if app is not None:
            app.stopping.set()                      # the SDK's own handler is installed only by start()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, on_signal)
    if app is not None:
        def round_(_app):
            try:
                return runner.round()
            except Unavailable as exc:
                hold(app, ctx.err, exc, ctx.b.params())
                raise SystemExit(EXIT_HELD) from None
            except GiveUp as exc:
                ctx.err.write('relay run: %s; exiting %d (restart)\n' % (exc, EXIT_RESTART))
                raise SystemExit(EXIT_RESTART) from None
        try:
            app.run(round_)                         # exits the process (0 on stop, 1 on a crash)
        except SystemExit as exc:
            if exc.code in (0, None):               # a crash or a restart keeps the segments: the next process
                runner.leave('shutdown')            # settles and resumes them
                if runner.hot is not None:
                    runner.hot.flush()
            raise
    while not stop:
        try:
            wait = runner.round()
        except GiveUp as exc:
            ctx.err.write('relay run: %s; exiting %d (restart)\n' % (exc, EXIT_RESTART))
            return EXIT_RESTART
        end = time.monotonic() + wait
        while not stop and time.monotonic() < end:
            time.sleep(min(1.0, end - time.monotonic()))
    runner.leave('shutdown')
    return 0


def job_events(ctx, job, app, store=None):
    """The state layer's Events for one job: audit line + supervisor notice, keys kept in `<state dir>/view-<job>.json`
    (the same ViewStore the run loop writes its node view to)."""
    return events.Events(app.notify, app.audit, store or events.ViewStore(ctx.view_path(job), _Clock()),
                         settings=knobs.Limits(ctx.b.params()))


def job_notifier(ev, job):
    """Flow's notify(kind, text) on top of Events: one key per kind, subject (first word: segment or node) and
    text, so a notice is sent once, also across a restart. Flow sends transitions, not listings: no baseline."""
    ev.baseline()

    def notify(kind, text):
        subject = (text.split(' ', 1)[0] or '-').replace(':', '')
        digest = hashlib.sha1(text.encode('utf-8')).hexdigest()[:NOTICE_KEY_DIGITS]
        ev.emit(kind, '%s:%s:%s' % (kind, subject, digest), audit={'job': job, 'text': text[:ev.settings.reason_max]},
                notify=text)
    return notify


HANDLERS = {'init': cmd_init, 'join': cmd_join, 'status': cmd_status, 'leave': cmd_leave,
            'checkpoint': cmd_checkpoint, 'deliver': cmd_deliver, 'take': cmd_take, 'park': cmd_park,
            'run': cmd_run}


def main(argv=None, *, backend=None, backend_factory=default_backend, state_dir=None, out=None, err=None):
    out = out or sys.stdout
    err = err or sys.stderr
    a = parser().parse_args(argv)
    state_dir = state_dir or os.environ.get('FLEETMUX_RELAY_STATE') or default_state_dir()
    try:
        b = backend if backend is not None else backend_factory(state_dir)
        ctx = Ctx(b, state_dir, out, err)
        return HANDLERS[a.cmd](ctx, a)
    except Unavailable as exc:
        if a.cmd == 'run' and os.environ.get(APPLET_NAME):  # before the run loop existed (building the backend)
            app = None
            try:
                from fleetmux import relay_applet
                beat = relay_applet.loop_heartbeat()
                app = beat.applet if beat is not None else None
            except Exception as app_exc:                    # noqa: BLE001 -- still held
                err.write('relay run: applet SDK unavailable: %s\n' % str(app_exc)[:text_limits()[1]])
            hold(app, err, exc)
            return EXIT_HELD
        err.write('relay %s: %s\n' % (a.cmd, exc))
        return EXIT_UNAVAILABLE
    except Refused as exc:
        err.write('relay %s: %s\n' % (a.cmd, exc))
        return EXIT_REFUSED
