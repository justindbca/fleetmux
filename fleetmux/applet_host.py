"""Applet processes of one fleet (docs/applets.md section 2, docs/applet-interface.md section 2).

The fleet whose roster declares ``applets:`` (a root service fleet or a named sub-fleet) is the host. Its own
daemon starts, watches, restarts and stops each applet on its tick, the way it supervises its channel actor
(docs/design.md section 53): a plain child process, no system service. The fleet window shows each applet as a
read-only tab in the daemon area (``applet_view``, applet-interface.md section 2.6).

- Start: on the daemon tick, for every declared ``autostart`` entry (and on ``fleetmux applet start``). The
  process is a session leader (its own process group); stdout and stderr go to ``$FLEETMUX_APPLET_RUN/applet.log``.
- Heartbeat: the applet writes ``hb_at`` into ``fx:applet:<name>``. Only a beat newer than this start counts.
  The first one moves ``starting`` to ``up``; none for ``stale_after_s`` = stalled (stopped, then a crash).
  Every wait here is awake time (fleetmux/awake.py): the time the machine slept is no silence of the applet.
  ``hb_at`` is Unix seconds (written by the applet) and is read through ``awake.from_wall``.
- Exit codes: 0 = exited (not restarted), 75 = restarted at once (not a crash), 78 = config_error (not
  restarted), anything else or a signal = crash, restarted at once. The ``crash_max``-th crash or stall within
  ``crash_window_s`` (awake time) = crashloop (no more restarts until ``fleetmux applet start``). A crash within
  ``WAKE_GRACE_BEATS`` heartbeats of awake time after the machine woke (``awake.since_wake``) is restarted but not
  counted (``applet.exit_after_wake``): the sleep, not the applet, broke it.
- Stop: SIGTERM to the applet, ``stop_grace_s``, SIGKILL; then the whole process group is swept so no child of
  the applet outlives it. Triggers: a removed roster row, ``fleetmux applet stop``, the fleet's explicit stop.
- Detach (a removed row, docs/applets.md section 2.1): field ``detaching`` = 1 in ``fx:applet:<name>`` (the lease
  gate refuses new requests with ``applet_detaching``), ``host.draining`` (``reason: detach``), then up to the
  entry's ``detach_wait_s`` for ``fx:applet:<name>:leases`` to empty; leases still open are revoked
  (``applet_detached``, through ``applet_lease.revoke_all``); then SIGTERM, ``stop_grace_s``, SIGKILL, and the
  applet's keys are cleared except ended lease records. Putting the row back before the revoke cancels it.
- Root host stop: ``host.draining`` (``reason: stop``) and the daemon loop runs on until the leases ended, at most
  the largest ``detach_wait_s`` (``stop_wait``). A named sub-fleet host does not wait again: its parent's drain
  (``drain_s``) already waited. ``fleetmux applet stop`` never waits for leases (it waits, bounded, for the
  process to end: ``wait_stopped``). No wait is a sleep.
- Move: the owner record ``host.json`` in the state directory (host, run, pid) is written after the start and
  removed after the exit; a live record of another run keeps the row ``declared`` (``applet.attach_wait``).
- A stale copy (a daemon that died without stopping its applets) is cleared on the next start from the pid file
  ``$FLEETMUX_APPLET_RUN/pid``, only when that pid still leads its own session.

Keys written here (applet-interface.md section 3.1): ``fx:applet:<name>`` fields ``state``, ``state_at``, ``started_at``,
``restarts``, ``crashes``, ``token_sha256``, ``interface``; in the parent's Redis ``fx:subfleet:<host>`` fields
``applets`` and ``applet_crashloop`` (see ``mirror``); one ``host.draining`` event on
``fx:applet:<name>:events``. Internal (not part of the interface): ``fx:applet:ctl`` (operator requests) and
``fx:applet:ctl:<id>`` (their answers).

Thresholds are parameters: ``SETTINGS`` defaults, overridden per run by ``config.json#applet_host``.
"""
import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

from fleetmux import awake, releases

KEY = 'fx:applet:'
WAKE_GRACE_BEATS = 2  # a crash this many heartbeat periods (awake) after a wake is not counted
CTL = 'fx:applet:ctl'          # hash {name: {"id", "action", "at"}}: fleetmux applet start|stop|restart -> daemon
CTL_REPLY = 'fx:applet:ctl:'   # + request id: the daemon's answer (JSON, kept an hour)
INTERFACES = (1,)              # interface versions this release speaks
EXIT_DONE, EXIT_RESTART, EXIT_CONFIG = 0, 75, 78
STATES = ('declared', 'starting', 'up', 'stalled', 'stopping', 'exited', 'crashloop', 'config_error')
HELD_STATES = ('exited', 'crashloop', 'config_error')  # not started again until `fleetmux applet start`
SETTINGS = {'crash_window_s': 3600, 'crash_max': 3, 'stale_copy_wait_s': 5, 'ctl_stale_s': 60,
            'kill_wait_s': 5}
DETACHING = 'detaching'        # field of fx:applet:<name>: '1' while a detach runs (read by the lease gate)
OWNER = 'host.json'            # owner record in the state directory: {"host", "run", "pid", "release"}
DRAIN_REASONS = {'rotate': 'rotate'}  # parent drain cause -> host.draining reason; every other cause is 'down'
NAME_RE = r'applet-[a-z][a-z0-9]*(-[a-z0-9]+)*-[0-9]+'


def key(name):
    return KEY + name


def settings(cfg):
    extra = (cfg or {}).get('applet_host')
    return dict(SETTINGS, **(extra if isinstance(extra, dict) else {}))


def _home(cfg):
    from fleetmux import fleet
    return Path(cfg.get('fleet_home') or fleet.HOME)


def run_dir(cfg, name):
    return Path(cfg['run']) / 'applets' / name


def state_dir(cfg, spec):
    """Durable directory under the fleet home (deleted only by ``fleetmux applet clear-state --yes``), or inside the
    run for ``durable_state: false``. An attached service sub-fleet (``fleetmux start`` while a root runs) keys it by
    its own name, so its applets never share state with the root's or another sub-fleet's applet of the same name."""
    if spec.get('durable_state', True):
        named = (cfg or {}).get('named') or {}
        if named.get('attached'):
            return _home(cfg) / 'subfleets' / named['name'] / 'applets' / spec['name']
        return _home(cfg) / 'applets' / spec['name']
    return run_dir(cfg, spec['name']) / 'state'


def sdk_dir():
    """The directory that contains this release's ``fleetmux`` package."""
    return str(Path(__file__).resolve().parent.parent)


def python(cfg):
    """The release's interpreter: ``{python}`` and ``FLEETMUX_APPLET_PYTHON``."""
    return cfg.get('python') or sys.executable


def argv(cfg, spec):
    subs = {'{python}': python(cfg), '{state}': str(state_dir(cfg, spec)),
            '{run}': str(run_dir(cfg, spec['name']))}
    out = []
    for part in spec['command']:
        for k, v in subs.items():
            part = part.replace(k, v)
        out.append(part)
    return out


def environment(cfg, spec, token, base=None):
    """The applet's environment (applet-interface.md section 2.2)."""
    from fleetmux import paths
    env = {k: v for k, v in (os.environ if base is None else base).items() if not k.startswith('FLEETMUX_APPLET_')}
    env.update(spec.get('env') or {})
    name = spec['name']
    env.update({
        'FLEETMUX_APPLET_NAME': name,
        'FLEETMUX_APPLET_INTERFACE': str(spec.get('interface', 1)),
        'FLEETMUX_APPLET_CONFIG': str(Path(cfg['run']) / 'config.json'),
        'FLEETMUX_APPLET_REDIS': str(cfg.get('redis_socket') or ''),
        'FLEETMUX_APPLET_TOKEN': token,
        'FLEETMUX_APPLET_STATE': str(state_dir(cfg, spec)),
        'FLEETMUX_APPLET_RUN': str(run_dir(cfg, name)),
        'FLEETMUX_APPLET_SDK': sdk_dir(),
        'FLEETMUX_APPLET_PYTHON': python(cfg),
        'FLEETMUX_APPLET_WORKERS': ','.join((spec.get('lease') or {}).get('eligible') or []),
        'FLEETMUX_APPLET_HEARTBEAT_S': str(spec.get('heartbeat_s', 10)),
        'FLEETMUX_APPLET_STALE_AFTER_S': str(spec.get('stale_after_s', 60)),
        'FLEETMUX_APPLET_STOP_GRACE_S': str(spec.get('stop_grace_s', 30)),
        'FLEETMUX_HOME': str(_home(cfg)),
        'FLEETMUX_STATE': str(paths.state_dir()),
    })
    if spec.get('waits'):
        env['FLEETMUX_APPLET_WAITS'] = json.dumps(spec['waits'], sort_keys=True)
    params = spec.get('params')
    if params:
        env['FLEETMUX_APPLET_PARAMS_FILE'] = params['file']
        env['FLEETMUX_APPLET_PARAMS_BLOCK'] = params['block']
    env['PYTHONPATH'] = os.pathsep.join([sdk_dir()] + ([env['PYTHONPATH']] if env.get('PYTHONPATH') else []))
    return env


def spawn(cfg, spec, token):
    """Start one applet process (a new session: its own process group). Returns the Popen handle."""
    rd = run_dir(cfg, spec['name'])
    rd.mkdir(parents=True, exist_ok=True)
    state_dir(cfg, spec).mkdir(parents=True, exist_ok=True)
    with open(rd / 'applet.log', 'ab') as log:
        return subprocess.Popen(argv(cfg, spec), cwd=spec['cwd'], env=environment(cfg, spec, token),
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, close_fds=True,
                                start_new_session=True)


# ------------------------------------------------------------------ process helpers
def _signal(pid, sig, group=False):
    try:
        (os.killpg if group else os.kill)(pid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _leads_session(pid):
    try:
        return os.getsid(pid) == pid
    except (ProcessLookupError, PermissionError, OSError):
        return False


class Adopted:
    """A ``Popen``-shaped handle (``pid``, ``poll``) for an applet started by an earlier daemon of this run. Its exit
    code is known only while it is still a child of this process; otherwise ``poll`` returns ``'unknown'``."""

    def __init__(self, pid):
        self.pid, self.code = pid, None

    def poll(self):
        if self.code is not None:
            return self.code
        try:
            got, status = os.waitpid(self.pid, os.WNOHANG)
            if got == self.pid:
                self.code = os.waitstatus_to_exitcode(status)
            return self.code
        except ChildProcessError:
            pass
        if not _leads_session(self.pid):
            self.code = 'unknown'
        return self.code


def adoptable(cfg, spec, h, now, note=None):
    """None when the running copy can be adopted after ``daemon --resume`` (applets.md 2.3), else the reason.

    A copy of another release than this daemon's is not adopted (design.md section 56, "Release drift"): its
    release is the owner record's ``release``, else the one its command line names. When either release cannot be
    told it is adopted as before and ``note`` (a callable) gets one line saying so."""
    try:
        rec = json.loads(owner_path(cfg, spec).read_text())
    except (OSError, ValueError):
        return 'no owner record'
    pid = rec.get('pid') if isinstance(rec, dict) else None
    if not isinstance(pid, int) or rec.get('run') != cfg['run']:
        return 'owner record of another run'
    if not _leads_session(pid):
        return 'process gone'
    mine, theirs = releases.own(), rec.get('release') or releases.of_pid(pid)
    if mine and theirs and not releases.same(mine, theirs):
        return 'release ' + str(theirs) + ', this daemon runs release ' + str(mine)
    if not (mine and theirs) and note:
        note('release unknown (applet ' + str(theirs or '?') + ', daemon ' + str(mine or '?') + '): adopted')
    try:
        hb, started = awake.from_wall(float(h.get('hb_at') or 0)), float(h.get('started_at') or 0)
    except ValueError:
        hb, started = 0.0, 0.0
    if hb < started or now - hb > float(spec['stale_after_s']):
        return 'stale heartbeat'
    return None


def sweep(pid):
    """SIGKILL whatever is left of the applet's process group (children it did not stop)."""
    if pid:
        _signal(pid, signal.SIGKILL, group=True)


def clear_stale(cfg, name, wait_s, sleep=time.sleep, clock=awake.mono):
    """Stop a copy left by an earlier daemon: the pid in the pid file, only while it still leads its session."""
    path = run_dir(cfg, name) / 'pid'
    try:
        pid = int(path.read_text().strip())
    except (OSError, ValueError):
        return None
    if pid <= 1 or pid == os.getpid() or not _leads_session(pid):
        return None
    _signal(pid, signal.SIGTERM, group=True)
    deadline = clock() + wait_s
    while _leads_session(pid) and clock() < deadline:
        sleep(.1)
    sweep(pid)
    return pid


def last_line(cfg, name, limit=4096):
    """The last non-empty line of the applet's log (the reason of a config error)."""
    try:
        with open(run_dir(cfg, name) / 'applet.log', 'rb') as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - limit))
            lines = [x.strip() for x in fh.read().decode('utf-8', 'replace').splitlines() if x.strip()]
    except OSError:
        return ''
    return lines[-1][:500] if lines else ''


# ------------------------------------------------------------------ the daemon side
class Runner:
    """What the daemon keeps per declared applet between ticks."""

    def __init__(self, spec):
        self.spec = spec
        self.proc = None
        self.started_at = None
        self.held = False        # do not start until an operator start (exited / crashloop / config_error / stop)
        self.stalled = False
        self.stop_deadline = None
        self.killed = False
        self.after = None        # after the running process is gone: 'stop' / 'restart' / 'remove' / None
        self.resume = False      # it ran when the previous daemon died: start it again even without autostart
        self.drain_deadline = None  # detach: revoke what is still open and SIGTERM at this time at the latest
        self.waiting = False     # another host's live owner record: applet.attach_wait was audited
        self.not_adopted = None  # why the copy of an earlier daemon was not adopted: restart it as one crash
        self.crashes = []
        self.restarts = 0

    @property
    def name(self):
        return self.spec['name']


def _log(f, event, **fields):
    try:
        f.log(event, **fields)
    except Exception:
        pass


def _set(f, name, **fields):
    if 'state' in fields:
        fields['state_at'] = repr(time.time())  # wall time of the last state write (shown in the applet tab)
    f.r.hset(key(name), mapping={k: (v if isinstance(v, str) else json.dumps(v)) for k, v in fields.items()})


def _runners(f):
    runners = getattr(f, 'applet_runners', None)
    if runners is None:
        runners = f.applet_runners = {}
    return runners


def _clock(f):
    return getattr(f, 'applet_clock', None) or awake.now


def _adopt(f, spec, now):
    """A new Runner; after ``daemon --resume`` it takes over the state the previous daemon left in Redis and adopts
    a running copy that passes ``adoptable`` (applets.md 2.3)."""
    run = Runner(spec)
    seen = getattr(f, 'applet_seen', None)
    if seen is None:
        seen = f.applet_seen = set()
    first, _ = spec['name'] not in seen, seen.add(spec['name'])
    h = f.r.hgetall(key(spec['name']))
    if not first:  # declared again after a removal in this daemon's life: a new row
        _set(f, spec['name'], state='declared')
        _log(f, 'applet.declared', applet=spec['name'], pid='', code='')
        return run
    if not h.get('state'):
        _set(f, spec['name'], state='declared')
        _log(f, 'applet.declared', applet=spec['name'], pid='', code='')
        return run
    try:
        run.crashes = [float(t) for t in json.loads(h.get('crashes') or '[]')]
        run.restarts = int(h.get('restarts') or 0)
    except (TypeError, ValueError):
        pass
    run.held = h['state'] in HELD_STATES
    run.resume = h['state'] in ('starting', 'up', 'stalled')
    if run.resume:
        why = adoptable(f.cfg, spec, h, now, note=lambda text: _log(f, 'applet.release_unknown', applet=spec['name'],
                                                                     reason=text))
        if why is None:
            pid = json.loads(owner_path(f.cfg, spec).read_text())['pid']
            run.proc, run.started_at, run.resume = Adopted(pid), float(h['started_at']), False
            _log(f, 'applet.adopted', applet=spec['name'], pid=str(pid), code='', reason='daemon resumed')
        else:
            run.not_adopted = why
    if h['state'] == 'stopping':  # it was being stopped when the daemon died
        run.held = True
        _set(f, spec['name'], state='exited')
    return run


def tick(f, now=None):
    """``Fleet.tick`` segment: declare / start / watch / restart / stop the roster's applets."""
    specs = getattr(f, 'applet_specs', None) or []
    if not specs and not getattr(f, 'applet_runners', None):
        return  # no applets: no Redis access (the CLI refuses names the roster does not declare)
    runners = _runners(f)
    now = _clock(f)() if now is None else now
    s = settings(f.cfg)
    wanted = {spec['name']: spec for spec in specs}
    for name, spec in wanted.items():
        run = runners.get(name)
        if run is None:
            runners[name] = _adopt(f, spec, now)
        elif run.after == 'remove' and run.drain_deadline is not None:  # put back before the revoke: cancel
            run.after, run.drain_deadline, run.spec = None, None, spec
            f.r.hdel(key(name), DETACHING)
        elif run.after != 'remove':
            run.spec = spec  # an edited row takes effect at the next start
    for name, run in list(runners.items()):
        if name not in wanted and run.after != 'remove':
            if run.proc is None:
                del runners[name]
                revoke(f, name)
                detached(f, name, '', '')
                continue
            begin_detach(f, run, now)
    answer_ctl(f, runners, now, s)
    for name, run in list(runners.items()):
        step(f, run, now, s)


def step(f, run, now, s):
    if run.proc is None:
        if not run.held and run.after is None and (run.spec.get('autostart', True) or run.resume):
            run.resume, why, run.not_adopted = False, run.not_adopted, None
            if why is None:
                start(f, run, now, s, reason=None)
            elif crashed(f, run, now, s):  # the copy of an earlier daemon was not adopted: one crash
                start(f, run, now, s, reason='not adopted (' + why + ')')
        return
    code = run.proc.poll()
    if code is not None:
        exited(f, run, code, now, s)
        return
    if run.stop_deadline is not None:
        if now >= run.stop_deadline and not run.killed:
            run.killed = True
            sweep(run.proc.pid)
        return
    if run.drain_deadline is not None:  # detach: no stall check while its leases end
        if now >= run.drain_deadline or not open_leases(f.r, run.name):
            run.drain_deadline = None
            revoke(f, run.name)  # what is still open (the set may lag one tick): applet_detached
            begin_stop(f, run, now, 'remove')
        return
    h = f.r.hgetall(key(run.name))
    try:
        hb = awake.from_wall(float(h.get('hb_at') or 0))  # the applet writes Unix seconds
    except ValueError:
        hb = 0.0
    fresh = hb >= run.started_at
    if fresh and h.get('state') == 'starting':
        _set(f, run.name, state='up')
        _log(f, 'applet.up', applet=run.name, pid=h.get('pid') or str(run.proc.pid), code='')
    since = hb if fresh else run.started_at
    if now - since > float(run.spec['stale_after_s']):
        run.stalled = True
        _set(f, run.name, state='stalled')
        _log(f, 'applet.stalled', applet=run.name, reason='no heartbeat for ' + str(run.spec['stale_after_s'])
             + ' s', crashes=len(run.crashes), window_s=s['crash_window_s'])
        begin_stop(f, run, now, 'restart', state=None)


def start(f, run, now, s, reason):
    """Spawn the applet (after clearing a stale copy). ``reason`` None = a plain start."""
    spec, name = run.spec, run.name
    run.stalled, run.stop_deadline, run.killed, run.after, run.drain_deadline = False, None, False, None, None
    if spec.get('interface', 1) not in INTERFACES:
        run.held = True
        _set(f, name, state='config_error', interface=str(spec.get('interface')))
        _log(f, 'applet.config_error', level='error', applet=name, crashes=len(run.crashes),
             window_s=s['crash_window_s'], reason='interface ' + str(spec.get('interface')) + ' is not supported '
             '(this release speaks ' + ', '.join(map(str, INTERFACES)) + ')')
        return False
    other = other_owner(f.cfg, spec)
    if other:  # move: the old host's process still runs (applets.md section 2.2)
        if not run.waiting:
            _log(f, 'applet.attach_wait', applet=name, pid=str(other.get('pid')), code='',
                 reason='still running on ' + str(other.get('host') or other.get('run')))
        run.waiting = True
        return False
    run.waiting = False
    clear_stale(f.cfg, name, s['stale_copy_wait_s'])
    token = secrets.token_hex(16)
    f.r.hdel(key(name), 'pid', 'hb_at', 'phase', DETACHING)  # nothing of an earlier process counts
    spawner = getattr(f, 'applet_spawn', None) or spawn
    try:
        run.proc = spawner(f.cfg, spec, token)
    except (OSError, ValueError) as exc:
        run.proc, run.held = None, True
        _set(f, name, state='config_error')
        _log(f, 'applet.config_error', level='error', applet=name, reason='cannot start: ' + str(exc)[:300],
             crashes=len(run.crashes), window_s=s['crash_window_s'])
        return False
    run.started_at, run.held = now, False
    owner = {'host': f.cfg.get('name') or '', 'run': f.cfg['run'], 'pid': run.proc.pid, 'release': releases.own()}
    for path, text in ((run_dir(f.cfg, name) / 'pid', str(run.proc.pid)),
                       (owner_path(f.cfg, spec), json.dumps(owner))):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        except OSError:
            pass
    _set(f, name, state='starting', started_at=repr(now), interface=str(spec.get('interface', 1)),
         token_sha256=hashlib.sha256(token.encode()).hexdigest(), restarts=str(run.restarts),
         crashes=json.dumps(run.crashes))
    if reason:
        _log(f, 'applet.restarted', applet=name, reason=reason, crashes=len(run.crashes),
             window_s=s['crash_window_s'], pid=str(run.proc.pid))
    return True


def open_leases(r, name):
    """Number of leases of ``name`` not ended yet (the lease side keeps the set; read only here)."""
    return r.scard(key(name) + ':leases')


def owner_path(cfg, spec):
    return state_dir(cfg, spec) / OWNER


def other_owner(cfg, spec):
    """The owner record of another host run whose process is still alive, else None."""
    try:
        rec = json.loads(owner_path(cfg, spec).read_text())
    except (OSError, ValueError):
        return None
    pid = rec.get('pid') if isinstance(rec, dict) else None
    if rec is None or not isinstance(pid, int) or rec.get('run') == cfg['run'] or not _leads_session(pid):
        return None
    return rec


def drop_owner(cfg, spec, pid):
    """Remove the owner record after the process ``pid`` has exited (only a record of that process)."""
    path = owner_path(cfg, spec)
    try:
        if json.loads(path.read_text()).get('pid') == pid:
            path.unlink()
    except (OSError, ValueError, AttributeError):
        pass


def begin_detach(f, run, now):
    """Removed row: refuse new leases, ``host.draining`` (``detach``), wait up to ``detach_wait_s``."""
    run.after, run.drain_deadline = 'remove', now + float(run.spec.get('detach_wait_s', 60))
    _set(f, run.name, **{DETACHING: '1'})
    draining_event(f.r, run.name, 'detach', run.drain_deadline, at=now)


def revoke(f, name):
    """Revoke the applet's leases that are still open (``applet_detached``; the lease side stops a running turn)."""
    from fleetmux import applet_lease
    applet_lease.revoke_all(f, name, 'applet_detached', 'the applet was detached from its host')


def detached(f, name, pid, code):
    """A detach is complete: clear the applet's keys (ended lease records stay) and audit it."""
    base = key(name)
    f.r.delete(base, base + ':events', base + ':inbox', base + ':params', base + ':leases', base + ':notices')
    _log(f, 'applet.detached', applet=name, pid=str(pid), code=str(code), reason='detach')


def detaching(f):
    """{name: entry} of the applets being detached: the lease side still serves them until they have stopped."""
    return {n: r.spec for n, r in (getattr(f, 'applet_runners', None) or {}).items() if r.after == 'remove'}


def begin_stop(f, run, now, after, state='stopping'):
    """SIGTERM the applet; ``step`` sends SIGKILL to its group after ``stop_grace_s``."""
    run.after = after
    if run.proc is None or run.stop_deadline is not None:
        return
    if state:
        _set(f, run.name, state=state)
    run.stop_deadline = now + float(run.spec['stop_grace_s'])
    _signal(run.proc.pid, signal.SIGTERM)


def exited(f, run, code, now, s):
    """The process is gone: sweep its group, then apply the exit-code table."""
    name, pid = run.name, run.proc.pid
    sweep(pid)
    drop_owner(f.cfg, run.spec, pid)
    run.proc, run.stop_deadline, run.killed, run.drain_deadline = None, None, False, None
    after, run.after = run.after, None
    stalled, run.stalled = run.stalled, False
    if after == 'remove':
        _runners(f).pop(name, None)
        detached(f, name, pid, code)
        return
    if after == 'stop':
        run.held = True
        _set(f, name, state='exited')
        _log(f, 'applet.stopped', applet=name, pid=str(pid), code=str(code), reason='operator')
        return
    if after == 'restart' and not stalled:
        _log(f, 'applet.stopped', applet=name, pid=str(pid), code=str(code), reason='operator restart')
        start(f, run, now, s, reason='operator')
        return
    if stalled or code not in (EXIT_DONE, EXIT_RESTART, EXIT_CONFIG):
        _log(f, 'applet.exited', level='error', applet=name, pid=str(pid), code=str(code))
        woke = awake.since_wake()
        if not stalled and woke is not None and woke <= WAKE_GRACE_BEATS * float(run.spec.get('heartbeat_s', 10)):
            # The sleep broke it (a health check timed out, a child was stopped, a connection dropped): restarted,
            # not counted toward crash_max (design.md section 35).
            _log(f, 'applet.exit_after_wake', level='warn', applet=name, pid=str(pid), code=str(code),
                 since_wake_s=round(woke, 1))
            run.restarts += 1
            start(f, run, now, s, reason='exit after a wake (exit ' + str(code) + ')')
            return
        if crashed(f, run, now, s):
            start(f, run, now, s, reason='stalled' if stalled else 'crash (exit ' + str(code) + ')')
        return
    if code == EXIT_RESTART:
        _log(f, 'applet.exited', applet=name, pid=str(pid), code=str(code))
        run.restarts += 1
        start(f, run, now, s, reason='exit 75 (restart requested)')
        return
    run.held = True
    if code == EXIT_CONFIG:
        _set(f, name, state='config_error')
        _log(f, 'applet.config_error', level='error', applet=name, reason=last_line(f.cfg, name) or 'exit 78',
             crashes=len(run.crashes), window_s=s['crash_window_s'])
        return
    _set(f, name, state='exited')
    _log(f, 'applet.exited', applet=name, pid=str(pid), code=str(code))


def crashed(f, run, now, s):
    """Count one crash; True when the applet is to be restarted, False when this one made it a crashloop."""
    run.crashes = [t for t in run.crashes if now - t < s['crash_window_s']] + [now]
    if len(run.crashes) >= s['crash_max']:
        crashloop(f, run, s)
        return False
    run.restarts += 1
    return True


def crashloop(f, run, s):
    run.held = True
    name = run.name
    _set(f, name, state='crashloop', crashes=json.dumps(run.crashes), restarts=str(run.restarts))
    config = str(Path(f.cfg['run']) / 'config.json')
    text = ('applet ' + name + ' crashed ' + str(len(run.crashes)) + ' times in ' + ('%g' % s['crash_window_s'])
            + ' s and is not restarted (the fleet carries on). After fixing it: fleetmux applet start ' + name
            + ' --config ' + config)
    _log(f, 'applet.crashloop', level='error', applet=name, reason='crash_max reached', crashes=len(run.crashes),
         window_s=s['crash_window_s'], line=text)
    try:
        from fleetmux import tabs
        tabs.notice(f, text, name)
    except Exception:
        pass


def answer_ctl(f, runners, now, s):
    """``fleetmux applet start|stop|restart``: one answer per request at ``fx:applet:ctl:<id>``."""
    for name, raw in (f.r.hgetall(CTL) or {}).items():
        f.r.hdel(CTL, name)
        try:
            req = json.loads(raw)
            rid, action, at = str(req['id']), req['action'], float(req['at'])
        except (TypeError, ValueError, KeyError):
            continue
        run = runners.get(name)
        if now - at > s['ctl_stale_s']:
            answer = {'status': 'rejected', 'reason': 'stale request'}
        elif run is None or run.after == 'remove':
            answer = {'status': 'rejected', 'reason': 'the roster declares no applet ' + name}
        elif action == 'start':
            if run.proc is not None:
                answer = {'status': 'ok', 'applet': name, 'state': 'running'}
            else:
                run.crashes, run.held, run.after = [], False, None
                started = start(f, run, now, s, reason=None)
                answer = ({'status': 'ok', 'applet': name, 'state': 'starting', 'pid': run.proc.pid} if started
                          else {'status': 'rejected', 'reason': 'not started: ' + f.r.hget(key(name), 'state')})
        elif action == 'stop':
            if run.proc is None:
                run.held = True
                answer = {'status': 'ok', 'applet': name, 'state': f.r.hget(key(name), 'state') or 'declared'}
            else:
                begin_stop(f, run, now, 'stop')
                answer = {'status': 'ok', 'applet': name, 'state': 'stopping'}
        elif action == 'restart':
            if run.proc is None:
                run.held, run.after = False, None
                started = start(f, run, now, s, reason='operator')
                answer = ({'status': 'ok', 'applet': name, 'state': 'starting'} if started
                          else {'status': 'rejected', 'reason': 'not started: ' + f.r.hget(key(name), 'state')})
            else:
                begin_stop(f, run, now, 'restart')
                answer = {'status': 'ok', 'applet': name, 'state': 'stopping'}
        else:
            answer = {'status': 'rejected', 'reason': 'unknown action ' + str(action)}
        f.r.set(CTL_REPLY + rid, json.dumps(answer), ex=3600)


def request(r, name, action, wait=30, clock=None, sleep=time.sleep):
    """CLI side of start / stop / restart: leave the request for the daemon and wait for its answer. ``at`` is
    the host fleet's awake time (the daemon judges it stale on its own clock)."""
    rid = secrets.token_hex(8)
    r.hset(CTL, name, json.dumps({'id': rid, 'action': action, 'at': (clock or awake.of(r).now)()}))
    clock = clock or awake.mono
    deadline = clock() + wait
    while True:
        raw = r.get(CTL_REPLY + rid)
        if raw:
            return json.loads(raw)
        if clock() >= deadline:
            return {'status': 'pending', 'request_id': rid}
        sleep(.2)


def wait_stopped(r, name, wait, clock=awake.mono, sleep=time.sleep):
    """CLI side of ``fleetmux applet stop``: poll the applet's state until the daemon recorded the end of the
    process (a held state: ``exited``, ``crashloop``, ``config_error``) or ``wait`` seconds passed. Returns the
    state seen last."""
    deadline = clock() + wait
    while True:
        state = r.hget(key(name), 'state') or ''
        if state in HELD_STATES or clock() >= deadline:
            return state
        sleep(.2)


# ------------------------------------------------------------------ host going down
def draining_event(r, name, reason, deadline_at, since=None, at=None):
    """XADD one ``host.draining`` event (section 3.6); with ``since``, not when one was sent after it. ``at``,
    ``deadline_at`` and ``since`` are the host fleet's awake time (``at`` defaults to it now)."""
    stream = key(name) + ':events'
    for _, fields in (r.xrevrange(stream, count=20) if since is not None else ()):
        try:
            data = json.loads(fields.get('data') or '{}')
        except ValueError:
            continue
        if data.get('type') == 'host.draining' and float(data.get('at') or 0) >= since:
            return False
    data = {'type': 'host.draining', 'at': awake.of(r).now() if at is None else at, 'reason': reason,
            'deadline_at': deadline_at}
    r.xadd(stream, {'data': json.dumps(data)}, maxlen=1000, approximate=True)
    return True


def stop_wait(f):
    """Daemon loop condition after an explicit stop of a root host: True while it should keep running so that
    the applets' leases end first. The first call sends ``host.draining``; the whole wait is at most the largest
    ``detach_wait_s`` of the running applets. A named sub-fleet host (its parent's drain already waited) and a fleet without a running
    applet never wait."""
    if not getattr(f, 'explicit_stop', False) or (f.cfg or {}).get('parent_config'):
        return False
    live = [run for run in (getattr(f, 'applet_runners', None) or {}).values() if run.proc is not None]
    if not live:
        return False
    now = _clock(f)()
    if getattr(f, 'applet_stop_deadline', None) is None:
        if not any(open_leases(f.r, run.name) for run in live):
            return False
        f.applet_stop_deadline = now + max(float(run.spec.get('detach_wait_s', 60)) for run in live)
        for run in live:
            draining_event(f.r, run.name, 'stop', f.applet_stop_deadline, since=run.started_at or 0, at=now)
    return now < f.applet_stop_deadline and any(open_leases(f.r, run.name) for run in live)


def stop_all(f, reason='stop'):
    """Fleet stop: ``host.draining``, SIGTERM, ``stop_grace_s``, SIGKILL of every applet group. Returns True when
    every applet process is gone. Injectable: ``applet_sleep``, ``applet_clock``."""
    runners = _runners(f)
    live = [run for run in runners.values() if run.proc is not None]
    if not live:
        runners.clear()
        return True
    clock = _clock(f)
    sleep = getattr(f, 'applet_sleep', None) or time.sleep
    s = settings(f.cfg)
    now = clock()
    if (f.cfg or {}).get('parent_config'):
        reason = 'down'  # a named sub-fleet host goes down (the parent sent its own event when the drain began)
    for run in live:
        try:
            draining_event(f.r, run.name, reason, now + float(run.spec['stop_grace_s']), since=run.started_at or 0,
                           at=now)
        except Exception:
            pass
        run.stop_deadline = None
        begin_stop(f, run, now, 'stop')
    deadline = now + max(float(run.spec['stop_grace_s']) for run in live)
    while any(run.proc.poll() is None for run in live) and clock() < deadline:
        sleep(.1)
    end = clock() + s['kill_wait_s']
    for run in live:
        if run.proc.poll() is None:
            sweep(run.proc.pid)
    while any(run.proc.poll() is None for run in live) and clock() < end:
        sleep(.1)
    gone = True
    for run in live:
        code = run.proc.poll()
        sweep(run.proc.pid)
        gone = gone and code is not None
        _set(f, run.name, state='exited')
        if code is not None:
            drop_owner(f.cfg, run.spec, run.proc.pid)
        _log(f, 'applet.stopped', level='info' if code is not None else 'error', applet=run.name,
             pid=str(run.proc.pid), code=str(code), reason=reason)
    runners.clear()
    return gone


def announce_drain(f, name, why):
    """Parent side: the named sub-fleet ``name`` starts draining; tell each of its applets (``host.draining``)."""
    from fleetmux import fleet, named_subfleet, subfleet
    d = named_subfleet.live(f).get(name)
    if d is None:
        return
    entry = named_subfleet.declared(f).get(name) or {}
    try:
        cfg = fleet.load(Path(d) / 'config.json')
        if not subfleet.redis_up(cfg):
            return
        r = fleet.connect(cfg)
    except Exception:
        return
    try:
        at = awake.of(r).now()  # the sub-fleet's own awake clock: its started_at is in it
        deadline = at + float(entry.get('drain_s') or 600)
        for name_, h in states(r, full=True).items():
            if h.get('state') in ('starting', 'up', 'stalled'):
                draining_event(r, name_, DRAIN_REASONS.get(why, 'down'), deadline,
                               since=float(h.get('started_at') or 0), at=at)
    finally:
        r.close(); r.connection_pool.disconnect()


# ------------------------------------------------------------------ reading (status, parent mirror)
def names(r):
    out = []
    for k in r.scan_iter(match=KEY + 'applet-*', count=200):
        name = k[len(KEY):]
        if re.fullmatch(NAME_RE, name):
            out.append(name)
    return sorted(out)


def states(r, full=False):
    """{name: state} (or {name: hash} with ``full``) of every applet hash in this Redis."""
    out = {}
    for name in names(r):
        h = r.hgetall(key(name))
        out[name] = h if full else h.get('state') or ''
    return out


def rows(r):
    """The status tree's ``applets: [{name, state, phase, leases}]`` (``leases`` = open lease ids, read only)."""
    out = []
    for name, h in states(r, full=True).items():
        out.append({'name': name, 'state': h.get('state') or '', 'phase': h.get('phase') or '',
                    'leases': sorted(r.smembers(key(name) + ':leases'))})
    return out


def tree_rows(cfg):
    """``rows`` of the fleet described by ``cfg``; [] when its Redis is not reachable."""
    from fleetmux import fleet, subfleet
    try:
        if not subfleet.redis_up(cfg):
            return []
        r = fleet.connect(cfg)
    except Exception:
        return []
    try:
        return rows(r)
    except Exception:
        return []
    finally:
        r.close(); r.connection_pool.disconnect()


def mirror(f, name, peek):
    """Parent side: copy the child's applet states (``peek['applets']``, from ``named_subfleet.child_peek``) into
    the parent's ``fx:subfleet:<name>`` as ``applets`` (JSON map) and ``applet_crashloop`` (comma list)."""
    if not peek or 'applets' not in peek:
        return
    found = peek['applets'] or {}
    from fleetmux import named_subfleet
    value = json.dumps(found, sort_keys=True)
    old = f.r.hget(named_subfleet.HASH + name, 'applets')
    if old == value or (old is None and not found):
        return  # unchanged, or a host that never had applets: no write
    f.r.hset(named_subfleet.HASH + name, mapping={
        'applets': value, 'applet_crashloop': ','.join(n for n, st in sorted(found.items()) if st == 'crashloop')})
