"""Release drift: a root daemon keeps its run on the installed release (docs/design.md section 56, "Release drift").

``install_release.sh --activate`` moves ``~/fleetmux-current``; nothing else restarts a running fleet. Here the root
daemon of a run compares, every ``check_interval_s``, the release of every process of its run (the root daemon,
each live sub-fleet daemon, each applet process, the fleetmux server) with the installed one, and switches the run
to it when all of these hold:

* the drift has lasted ``grace_s`` since this daemon first saw it (an install still in progress is not switched to);
* the installed release passes its self-check: its directory is complete and its ``.venv`` runs ``fleet.py --help``
  (otherwise no switch, one notice);
* no ``blackout`` window (``HH:MM-HH:MM`` in ``timezone``, '' = the machine's) is open;
* no worker of the run (root or a live sub-fleet) is in a turn: the daemon waits up to ``idle_wait_s`` for all of them
  to be idle, then defers to the next check (never a hard switch);
* fewer than ``max_failures`` switches to that installed release have failed, and ``<fleet home>/start.sh`` exists.

The switch is a separate process (``python -m fleetmux.release_drift switch <plan>``, its own session), because it
stops the daemon that starts it; ``fleetmux upgrade`` (fleetmux/upgrade.py) is its manual entry and runs the same
``switch`` in its own process. One switch of a run at a time: it holds ``<run>/release-drift.lock``. Its steps
(``STEPS``): a ``fx:jobs`` snapshot and the state the checks compare with, taken before any daemon stops and also
kept under ``<fleet home>/release-drift/`` (``upgrade/`` for the manual entry); SIGTERM to the root daemon first
(while it lives it collects every sub-fleet daemon that exits), then the sub-fleet daemons level by level (a level
only once every parent let go of its lock); the applets; ``<run>/jobs.json`` and the job-runs index from the
snapshot; the run's Redis instances shut down with a save until none answers; the fleetmux server and GUI of the
run's runtime (never the fleet's own tmux). Then every config's ``python`` is set to the target's interpreter and the
same run continues with ``FLEETMUX_REL=<installed dir> start.sh --resume <run>`` (the manual entry without start.sh:
the target's own ``fleet.py start --resume <run>``; never ``fleetmux stop``, which records jobs as cancelled). The
checks (``CHECKS``: daemons, interpreter, jobs, supervisor, sub-fleets, applets, window) run until ``verify_s`` and
judge "not worse than before the switch": what did not hold before is reported, never failed. A start that fails or
a check that does not pass goes back the same way to the release the run had and counts one failure of that
release.

Notices: one before the switch (from the daemon) and one after it (from whichever daemon runs next), each to the
root supervisor and to the user through the run's human notice route (lifecycle_close.notify / notify_route).
Parameters: ``fleet.yaml#release_drift`` (``enabled`` is the master switch). A daemon whose own or installed
release is unknown, and every process whose release cannot be told, never counts as drift.
"""
import fcntl
import json
import math
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from fleetmux import fleet_config, releases

BLOCK = 'release_drift'
DEFAULTS = {'enabled': True, 'check_interval_s': 600, 'grace_s': 900, 'idle_wait_s': 600, 'blackout': [],
            'timezone': '', 'max_failures': 2, 'stop_wait_s': 240, 'verify_s': 300}
POSITIVE = ('check_interval_s', 'stop_wait_s', 'verify_s')
NON_NEGATIVE = ('grace_s', 'idle_wait_s')
WINDOW = re.compile(r'([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)\Z')
STATE = 'release-drift.json'      # <run>/: {failures: {release: n}, reported: {key: at}}
PLAN = 'release-drift-plan.json'  # <run>/: what the switch process does
OUTCOME = 'release-drift-outcome.json'  # <run>/: how the last switch went, until the next daemon reported it
LOG = 'release-drift.log'         # <run>/: the switch process's lines
START = 'start.sh'                # <fleet home>/start.sh: takes FLEETMUX_REL and passes --resume <run> on
EVENT = 'release_drift'
BUSY = ('busy', 'handoff')        # a slot in one of these states is in a turn
WAIT_RECHECK_S = 5                # while waiting for idle workers: one look every this many seconds
LOCK = 'release-drift.lock'       # <run>/: held (flock) by the one switch of the run
# The steps of the switch, in their fixed order (also the step names of `fleetmux upgrade`, its manual entry).
STEPS = ('preflight', 'backup', 'stop_root', 'stop_children', 'stop_applets', 'restore_files', 'redis_down',
         'terminal_down', 'start_new', 'verify')
CHECKS = ('daemons', 'interpreter', 'jobs', 'supervisor', 'subfleets', 'applets', 'window')
STOP_ORDER = 'root_first'         # tests inject 'children_first' to reproduce the order that loses jobs
OPEN_JOB = ('queued', 'starting', 'running', 'suspended')
SETTLED_JOB = ('queued', 'running')
JOB_SUCCESS = 'ok'                # Fleet.job_finished publishes a completed job sub-fleet as status ok
SUPERVISOR_KEY = 'fx:agent:supervisor'
WRAPPERS = ('caffeinate', 'systemd-inhibit')  # fleet.keep_awake_prefix
APPLET_GRACE_S = 20
REDIS_DOWN_S = 30
START_TIMEOUT_S = 600
ROOT_UP_S = 120
VERIFY_POLL_S = 5


# ------------------------------------------------------------------ parameters
def knobs():
    """(knobs, problem): fleet.yaml#release_drift over DEFAULTS; a bad value keeps its default and is named."""
    try:
        block = fleet_config.block(BLOCK) or {}
    except (OSError, ValueError, KeyError) as exc:
        return dict(DEFAULTS), 'fleet.yaml#' + BLOCK + ' unreadable: ' + str(exc)[:200]
    out, bad = dict(DEFAULTS), []
    for key, value in (block.items() if isinstance(block, dict) else ()):
        if key not in DEFAULTS:
            bad.append(key + ' (unknown)')
        elif key == 'enabled':
            out[key] = value if type(value) is bool else bad.append(key) or out[key]
        elif key in POSITIVE + NON_NEGATIVE:
            ok = type(value) in (int, float) and (value > 0 if key in POSITIVE else value >= 0)
            out[key] = value if ok else bad.append(key) or out[key]
        elif key == 'max_failures':
            out[key] = value if type(value) is int and value >= 1 else bad.append(key) or out[key]
        elif key == 'blackout':
            ok = isinstance(value, list) and all(isinstance(w, str) and WINDOW.match(w.strip()) for w in value)
            out[key] = [w.strip() for w in value] if ok else bad.append(key) or out[key]
        elif key == 'timezone':
            out[key] = value if isinstance(value, str) and _zone_ok(value) else bad.append(key) or out[key]
    return out, ('fleet.yaml#' + BLOCK + ': bad value kept at its default: ' + ', '.join(bad)) if bad else None


def _zone(name):
    if not name:
        return None
    from zoneinfo import ZoneInfo
    return ZoneInfo(name)


def _zone_ok(name):
    try:
        _zone(name)
        return True
    except Exception:  # noqa: BLE001  (ZoneInfoNotFoundError, ValueError, a missing tz database)
        return False


def in_blackout(now, windows, tz=''):
    """True when the wall time ``now`` (in ``tz``, '' = this machine's zone) falls in one of ``windows``
    (``HH:MM-HH:MM``; an end before the start runs across midnight)."""
    if not windows:
        return False
    zone = _zone(tz)
    t = datetime.fromtimestamp(now, zone) if zone else datetime.fromtimestamp(now)
    minute = t.hour * 60 + t.minute
    for w in windows:
        m = WINDOW.match(w.strip())
        if not m:
            continue
        a, b = int(m.group(1)) * 60 + int(m.group(2)), int(m.group(3)) * 60 + int(m.group(4))
        if (a <= minute < b) if a <= b else (minute >= a or minute < b):
            return True
    return False


def minutes(k):
    """The longest wait from an activation to the switch while workers are idle and no blackout is open."""
    return int(math.ceil((k['check_interval_s'] + k['grace_s']) / 60.0))


# ------------------------------------------------------------------ side effects (one object, stubbed in tests)
class Ops:
    def now(self):
        return time.time()

    def sleep(self, s):
        time.sleep(s)

    def knobs(self):
        return knobs()

    def installed_dir(self):
        path = Path(releases.LINK).expanduser()
        return path.resolve() if path.exists() else None

    def own(self):
        return releases.own()

    def own_dir(self):
        return releases.ROOT

    def ps(self):
        try:
            out = subprocess.run(['ps', '-axww', '-o', 'pid=,args='], capture_output=True, text=True, timeout=20,
                                 stdin=subprocess.DEVNULL).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        rows = []
        for line in out.splitlines():
            pid, _, args = line.strip().partition(' ')
            if pid.isdigit():
                rows.append((int(pid), args.strip()))
        return rows

    def cmdline(self, pid):
        return releases.cmdline(pid)

    def launcher(self, pid):
        return releases.launcher(pid)

    def switcher_alive(self, pid):
        """The switch process this daemon started: reaped here once it ended (only this pid: an applet's exit code
        belongs to its own Popen handle)."""
        try:
            got, _ = os.waitpid(pid, os.WNOHANG)
            if got == pid:
                return False
        except (ChildProcessError, OSError):
            pass
        return self.alive(pid)

    def alive(self, pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def daemon_running(self, run):
        try:
            return releases.daemon_running(run)
        except OSError:
            return False

    def kill(self, pid, sig):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass

    def _redis(self, sock):
        import redis
        return redis.Redis(unix_socket_path=str(sock), db=0, decode_responses=True, socket_connect_timeout=3,
                           socket_timeout=5)

    def redis_get(self, sock, key):
        try:
            client = self._redis(sock)
            try:
                value = client.get(key)
            finally:
                client.close()
        except Exception:  # noqa: BLE001  (redis.RedisError, OSError: down or unreadable)
            return None
        return '' if value is None else value  # '' = the key is absent (not the same as a Redis that is down)

    def redis_shutdown(self, sock):
        try:
            self._redis(sock).shutdown(save=True)
        except Exception:  # noqa: BLE001  (redis closes the socket while acknowledging shutdown; or it is down)
            pass

    def redis_answers(self, sock):
        try:
            client = self._redis(sock)
            try:
                return bool(client.ping())
            finally:
                client.close()
        except Exception:  # noqa: BLE001  (down: the socket is gone or refuses)
            return False

    def redis_hget(self, sock, key, field):
        try:
            client = self._redis(sock)
            try:
                return client.hget(key, field)
            finally:
                client.close()
        except Exception:  # noqa: BLE001
            return None

    def applets(self, run):
        """{name: pid} of the applet copies of ``run`` still running (pid file, still leading its session)."""
        from fleetmux import applet_host
        found, base = {}, Path(run) / 'applets'
        for d in sorted(base.iterdir()) if base.is_dir() else ():
            pid = _pid_file(d / 'pid')
            if pid and applet_host._leads_session(pid):
                found[d.name] = pid
        return found

    def stop_applet(self, run, name):
        """SIGTERM to the applet's process group, a grace, then the rest (applet_host.clear_stale)."""
        from fleetmux import applet_host
        cfg = dict(_json(Path(run) / 'config.json') or {}, run=str(run))
        return applet_host.clear_stale(cfg, name, APPLET_GRACE_S)

    def windows(self):
        """Owner pids of the on-screen windows (macOS CGWindowList), None where that cannot be read."""
        if sys.platform != 'darwin':
            return None
        script = ('ObjC.import("CoreGraphics");JSON.stringify(ObjC.deepUnwrap('
                  '$.CGWindowListCopyWindowInfo(1, 0)).map(w => w.kCGWindowOwnerPID))')
        try:
            out = subprocess.run(['osascript', '-l', 'JavaScript', '-e', script], capture_output=True, text=True,
                                 timeout=20, stdin=subprocess.DEVNULL)
            return set(json.loads(out.stdout.strip() or 'null') or [])
        except (OSError, subprocess.SubprocessError, ValueError):
            return None

    def descendants(self, pid):
        try:
            out = subprocess.run(['ps', '-axo', 'pid=,ppid='], capture_output=True, text=True, timeout=20,
                                 stdin=subprocess.DEVNULL).stdout
        except (OSError, subprocess.SubprocessError):
            return {pid}
        kids = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                kids.setdefault(int(parts[1]), []).append(int(parts[0]))
        found, todo = set(), [pid]
        while todo:
            p = todo.pop()
            if p not in found:
                found.add(p)
                todo += kids.get(p, [])
        return found

    def run_input(self, argv, text, env, timeout):
        try:
            done = subprocess.run(argv, input=text, capture_output=True, text=True, env=env, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            return -1, str(exc)
        return done.returncode, (done.stdout + done.stderr)[-2000:]

    def sub_busy(self, cfg):
        """The slots of a sub-fleet in a turn, read from its own Redis; unreadable counts as busy. Its supervisor
        counts: it is a worker pane of the parent (the parent's slot only says ``delegated``)."""
        try:
            client = self._redis(cfg['redis_socket'])
            try:
                out = []
                for k in client.scan_iter('fx:agent:*'):
                    name = k.split(':', 2)[2]
                    if name.startswith('advisor'):
                        continue
                    if client.hget(k, 'state') in BUSY:
                        out.append(name)
                return out
            finally:
                client.close()
        except Exception as exc:  # noqa: BLE001
            return ['(its Redis is unreadable: ' + type(exc).__name__ + ')']

    def run(self, argv, env=None, timeout=None):
        try:
            done = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=timeout,
                                  stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as exc:
            return -1, str(exc)
        return done.returncode, (done.stdout + done.stderr)[-2000:]

    def start(self, argv, env, log_path, timeout):
        """``start.sh --resume``: its output goes to the log file, never a pipe (what it leaves running, e.g. the
        fleetmux server or Redis, would hold a pipe open until the timeout). Returns the exit code (-1: not run)."""
        try:
            with open(log_path, 'ab') as log:
                return subprocess.run(argv, env=env, timeout=timeout, stdin=subprocess.DEVNULL, stdout=log,
                                      stderr=subprocess.STDOUT).returncode
        except (OSError, subprocess.SubprocessError) as exc:
            _say(Path(log_path).parent, 'start.error', error=str(exc)[:300])
            return -1

    def spawn(self, argv, env, log_path):
        with open(log_path, 'ab') as log:
            return subprocess.Popen(argv, env=env, cwd=str(releases.ROOT), stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT, close_fds=True, start_new_session=True).pid

    def notify_user(self, f, text):
        """The run's human notice route: a managed root -> lifecycle_close.notify, else notify_route.unmanaged_send
        (the run's channels, else counted in notify-undelivered.json)."""
        if f.cfg.get('parent_config'):
            return None
        from fleetmux import lifecycle_close, notify_route
        if lifecycle_close.managed(f):
            from fleetmux import lifecycle
            try:
                policy = lifecycle.get_mode(f.cfg['lifecycle'])
            except (ValueError, KeyError, TypeError):
                policy = {}
            out = lifecycle_close.notify(f, EVENT, '[fleet ' + lifecycle_close.fleet_name(f) + '] ' + text, policy)
            return ','.join(str(x.get('status')) for x in out) if isinstance(out, list) else 'sent'
        return notify_route.unmanaged_send(f, EVENT, text) or 'counted'


# ------------------------------------------------------------------ files
def _json(path):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return data


def _write(path, data):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.' + path.name + '.')
    with os.fdopen(fd, 'w') as fh:
        fh.write(json.dumps(data, ensure_ascii=False, indent=1))
    os.replace(tmp, path)


def load_state(run):
    data = _json(Path(run) / STATE)
    data = data if isinstance(data, dict) else {}
    for key in ('failures', 'reported'):
        if not isinstance(data.get(key), dict):
            data[key] = {}
    return data


def save_state(run, data):
    _write(Path(run) / STATE, data)


# ------------------------------------------------------------------ the run's processes
def sub_runs(run):
    """Every sub-fleet run directory below ``run`` (nested ones too), in name order."""
    sub, out = Path(run) / 'sub', []
    for d in sorted(sub.iterdir()) if sub.is_dir() else []:
        if (d / 'config.json').is_file():
            out.append(d)
            out += sub_runs(d)
    return out


def _sub_name(root, d):
    return '/'.join(p for p in Path(d).relative_to(root).parts if p != 'sub')


def _pid_file(path):
    try:
        pid = int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 1 else None


def _inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def server_pids(ops, runtime):
    """The ``fleetmux server`` / ``fleetmux gui`` processes of one runtime directory: [(pid, role)]."""
    out = []
    for pid, args in ops.ps():
        argv = args.split()
        if (len(argv) > 3 and Path(argv[0]).name == 'fleetmux' and argv[1] in ('server', 'gui')
                and any(a == '--runtime' and b == str(runtime) for a, b in zip(argv, argv[1:]))):
            out.append((pid, argv[1]))
    return out


def processes(f, ops):
    """[{kind, name, release, ...}]: the root daemon, each live sub-fleet daemon, each live applet process (root and
    sub-fleets) and the fleetmux server of the run. ``release`` None = cannot be told."""
    run = Path(f.cfg['run'])
    rows = [{'kind': 'root daemon', 'name': f.cfg.get('name') or run.name, 'release': ops.own()}]
    live = [run]
    for d in sub_runs(run):
        cfg = _json(d / 'config.json')
        if not isinstance(cfg, dict) or not ops.daemon_running(str(d)):
            continue
        live.append(d)
        rows.append({'kind': 'sub-fleet daemon', 'name': _sub_name(run, d), 'release': releases.running(cfg),
                     'run': str(d)})
    for d in live:
        for pid_path in sorted((d / 'applets').glob('*/pid')) if (d / 'applets').is_dir() else []:
            pid = _pid_file(pid_path)
            if pid and ops.alive(pid):
                rows.append({'kind': 'applet', 'name': pid_path.parent.name, 'release': releases.of_argv(
                    ops.cmdline(pid)), 'pid_file': str(pid_path)})
    try:  # the fleetmux backend only; a backend property that fails means no server row
        runtime = getattr(getattr(f, 'mgr', None), 'runtime_root', None)
    except Exception:  # noqa: BLE001
        runtime = None
    if runtime is not None:
        pid = (_json(Path(runtime) / 'server.json') or {}).get('pid')
        if isinstance(pid, int) and ops.alive(pid):
            rows.append({'kind': 'fleetmux server', 'name': '', 'release': releases.of_argv(ops.cmdline(pid)),
                         'runtime': str(runtime)})
    return rows


def label(row):
    return (row['kind'] + ' ' + row['name']).strip()


def busy_workers(f, ops):
    """The workers of the run in a turn: the root's (busy / handoff, or leased by an applet) and each live
    sub-fleet's (read from its Redis)."""
    out = []
    for name in list(getattr(f, 'workers', []) or []):
        info = f.agent(name) or {}
        leased = False
        if getattr(f, 'r', None) is not None:
            try:
                from fleetmux import applet_lease
                leased = bool(applet_lease.taken(f, name))
            except Exception:  # noqa: BLE001
                leased = False
        if info.get('state') in BUSY or leased:
            out.append(name)
    run = Path(f.cfg['run'])
    for d in sub_runs(run):
        cfg = _json(d / 'config.json')
        if isinstance(cfg, dict) and cfg.get('redis_socket') and ops.daemon_running(str(d)):
            out += [_sub_name(run, d) + '/' + n for n in ops.sub_busy(cfg)]
    return out


def selfcheck(rel_dir, backend, ops):
    """None when the release directory ``rel_dir`` is complete and its venv runs ``fleet.py --help``, else why."""
    rel = Path(rel_dir)
    need = ['fleetmux/fleet.py', '.venv/bin/python'] + (['bin/fleetmux'] if backend == 'fleetmux' else [])
    missing = [n for n in need if not (rel / n).exists()]
    if missing:
        return 'the release directory is incomplete: missing ' + ', '.join(missing)
    code, out = ops.run([str(rel / '.venv' / 'bin' / 'python'), str(rel / 'fleetmux' / 'fleet.py'), '--help'],
                        timeout=120)
    if code != 0:
        return ('.venv/bin/python fleetmux/fleet.py --help exited ' + str(code) + ': '
                + ' '.join(str(out).split())[-300:])
    return None


# ------------------------------------------------------------------ notices
def announce(f, ops, text):
    """One notice to the root supervisor and one to the user (the run's human notice route); never raises."""
    text = '[release drift] ' + text
    try:
        if f.running():
            tail = ('\nThis is a notice from the program, not a request: reply with the usual service decision JSON: '
                    'status RUNNING, dispatch [], skip [].' if getattr(f, 'service', False) else
                    '\nThis is a notice from the program; it needs no action from you.')
            f.queue('supervisor', text + tail)
    except Exception as exc:  # noqa: BLE001  (a notice never takes the daemon down)
        f.log('release_drift.notify_failed', level='error', leg='supervisor', error=str(exc)[:300])
    try:
        status = ops.notify_user(f, text)
    except Exception as exc:  # noqa: BLE001
        status = 'failed'
        f.log('release_drift.notify_failed', level='error', leg='user', error=str(exc)[:300])
    f.log('release_drift.notice', status=str(status), text=text[:1000])


def once(f, ops, key, text):
    """``announce`` the first time ``key`` comes up for this run (recorded in <run>/release-drift.json)."""
    run = f.cfg['run']
    st = load_state(run)
    if key in st['reported']:
        return False
    st['reported'][key] = ops.now()
    save_state(run, st)
    announce(f, ops, text)
    return True


def outcome_text(o):
    replaced = ', '.join(o.get('replaced') or []) or 'nothing'
    jobs = o.get('jobs') or {}
    jobs_line = ('jobs back: ' + str(jobs.get('back', 0)) + ' of ' + str(jobs.get('expected', 0))
                 + (' (missing: ' + ', '.join(jobs['missing']) + ')' if jobs.get('missing') else ''))
    na = ['%s %s' % (n, c.get('detail')) for n, c in (o.get('checks') or {}).items() if c.get('status') == 'n/a']
    if o.get('ok'):
        return ('switched from release ' + str(o['from']) + ' to release ' + str(o['to']) + ': replaced ' + replaced
                + '; every process of the run now runs release ' + str(o['to']) + '; ' + jobs_line
                + ('; not checked, as before the switch: ' + '; '.join(na) if na else '') + '.')
    text = ('switch from release ' + str(o['from']) + ' to release ' + str(o['to']) + ' failed: ' + str(o['reason'])
            + '. ')
    if o.get('rolled_back'):
        text += 'The run is back on release ' + str(o['from']) + ' and continues; ' + jobs_line + '. '
    else:
        text += ('Going back to release ' + str(o['from']) + ' failed too: ' + str(o.get('rollback_reason'))
                 + '. The run may be down: start it again by hand with its start.sh --resume. ')
    if stopped_nothing(o):
        text += 'Nothing was stopped, so this is not counted as a failed switch. '
    text += ('Failed switches to release ' + str(o['to']) + ': ' + str(o.get('failures')) + ' of '
             + str(o.get('max_failures')) + ' (at ' + str(o.get('max_failures'))
             + ' the daemon stops switching to it and only reports).')
    return text


def report_outcome(f, ops):
    """The daemon that runs after a switch reports how it went, once."""
    path = Path(f.cfg['run']) / OUTCOME
    data = _json(path)
    if not isinstance(data, dict) or data.get('reported'):
        return False
    data['reported'] = ops.now()
    _write(path, data)
    announce(f, ops, outcome_text(data))
    return True


# ------------------------------------------------------------------ the daemon side
def gate(f):
    g = getattr(f, 'release_drift_gate', None)
    if g is None:
        g = f.release_drift_gate = {'next_check': 0.0, 'wait_until': None, 'last_look': 0.0, 'first_seen': {},
                                    'switcher': None, 'problem': None}
    return g


def tick(f, ops=None):
    """``Fleet.tick`` part (root fleets only): compare, and switch when every condition holds."""
    if f.cfg.get('parent_config'):
        return None
    ops = ops or Ops()
    report_outcome(f, ops)
    k, problem = ops.knobs()
    g, now = gate(f), ops.now()
    if problem != g['problem']:  # a bad value is named once per change, never silently ignored
        g['problem'] = problem
        if problem:
            f.log('release_drift.config', level='warn', problem=problem)
    if not k['enabled']:
        return None
    if g['switcher'] is not None:
        if ops.switcher_alive(g['switcher']):
            return None  # the switch process runs: it stops this daemon
        g['switcher'] = None
    if g['wait_until'] is not None:
        if now - g['last_look'] < WAIT_RECHECK_S and now < g['wait_until']:
            return None
    elif now < g['next_check']:
        return None
    else:
        g['next_check'] = now + k['check_interval_s']
    g['last_look'] = now
    return check(f, ops, k, g, now, problem)


def check(f, ops, k, g, now, problem=None):
    inst_dir = ops.installed_dir()
    inst = releases.release_of(inst_dir) if inst_dir else None
    if not inst:
        g['wait_until'] = None
        return None
    rows = processes(f, ops)
    drift = [r for r in rows if r['release'] and not releases.same(r['release'], inst)]
    if not drift:
        g['first_seen'].pop(inst, None)
        g['wait_until'] = None
        return None
    first = g['first_seen'].setdefault(inst, now)
    if first == now:
        f.log('release_drift.seen', installed=inst, drift=[label(r) + ' ' + str(r['release']) for r in drift])
    if now - first < k['grace_s']:
        g['next_check'] = min(g['next_check'], first + k['grace_s'])
        return None
    run = Path(f.cfg['run'])
    failures = int(load_state(run)['failures'].get(inst) or 0)
    if failures >= k['max_failures']:
        once(f, ops, 'stopped:' + inst, 'automatic switching to release ' + inst + ' stopped after ' + str(failures)
             + ' failed switches; the run stays on release ' + str(ops.own()) + '. Restart it on release ' + inst
             + ' by hand once the cause is fixed.')
        g['wait_until'] = None
        return None
    why = selfcheck(inst_dir, f.cfg.get('backend'), ops)
    if why:
        once(f, ops, 'selfcheck:' + inst, 'release ' + inst + ' is installed but fails its self-check (' + why
             + '); the run stays on release ' + str(ops.own()) + ' and is not switched to it.')
        g['wait_until'] = None
        return None
    if problem and ('blackout' in problem or 'timezone' in problem):  # a window that cannot be read: no switch
        f.log('release_drift.config', level='warn', installed=inst, problem=problem, switch='held')
        g['wait_until'] = None
        return None
    if in_blackout(now, k['blackout'], k['timezone']):
        f.log('release_drift.blackout', installed=inst, windows=k['blackout'], timezone=k['timezone'])
        g['wait_until'] = None
        return None
    start = Path(f.cfg.get('fleet_home') or '') / START
    if not f.cfg.get('fleet_home') or not start.is_file():
        once(f, ops, 'nostart:' + inst, 'release ' + inst + ' is installed, the run is on release ' + str(ops.own())
             + ', and there is no ' + str(start) + ' to continue the run with: restart it by hand.')
        g['wait_until'] = None
        return None
    if switch_running(run):  # a manual `fleetmux upgrade` (or another switch) is moving this run
        f.log('release_drift.locked', installed=inst, lock=str(run / LOCK))
        g['wait_until'] = None
        return None
    busy = busy_workers(f, ops)
    if busy:
        if g['wait_until'] is None:
            g['wait_until'] = now + k['idle_wait_s']
            f.log('release_drift.waiting', installed=inst, busy=busy, until=g['wait_until'])
        elif now >= g['wait_until']:
            g['wait_until'] = None
            g['next_check'] = now + k['check_interval_s']
            f.log('release_drift.deferred', installed=inst, busy=busy, next_check=g['next_check'])
        return None
    g['wait_until'] = None
    return launch(f, ops, k, inst, inst_dir, rows, drift, start, now)


def launch(f, ops, k, inst, inst_dir, rows, drift, start, now):
    """The notice before the switch, the plan file and the detached switch process."""
    run = Path(f.cfg['run'])
    jobs = _jobs(ops.redis_get(f.cfg.get('redis_socket'), 'fx:jobs'))
    old = ops.own()
    plan = {'run': str(run), 'config': str(run / 'config.json'), 'fleet_home': f.cfg['fleet_home'],
            'start': str(start), 'name': f.cfg.get('name') or run.name,
            'old': {'release': old, 'dir': str(ops.own_dir())}, 'new': {'release': inst, 'dir': str(inst_dir)},
            'replace': [label(r) for r in drift],
            'subs': [r['run'] for r in rows if r['kind'] == 'sub-fleet daemon'],
            'applets': [r['pid_file'] for r in rows if r['kind'] == 'applet'],
            'server_runtime': next((r['runtime'] for r in rows if r['kind'] == 'fleetmux server'), None),
            'stop_wait_s': k['stop_wait_s'], 'verify_s': k['verify_s'], 'max_failures': k['max_failures'],
            'pythonpath': os.environ.get('PYTHONPATH') or '', 'at': now,
            'backup_dir': str(Path(f.cfg['fleet_home']) / 'release-drift' / time.strftime('%Y%m%dT%H%M%SZ',
                                                                                         time.gmtime(now)))}
    _write(run / PLAN, plan)
    announce(f, ops, 'switching this run from release ' + str(old) + ' to release ' + inst + ' now (the installed '
             'release; drift for ' + str(int(now - g_first(f, inst, now))) + ' s, every worker idle, no blackout '
             'window). Replacing: ' + ', '.join(label(r) + ' (' + str(r['release']) + ')' for r in drift) + '. Open '
             'jobs: ' + str(len(jobs)) + ', kept through a snapshot taken before any daemon stops. The same run '
             'continues on release ' + inst + ' (start.sh --resume); a second notice says how it went.')
    # The switch process imports this daemon's release through PYTHONPATH; start_env drops it again for start.sh.
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ops.own_dir())] + (
        [os.environ['PYTHONPATH']] if os.environ.get('PYTHONPATH') else [])))
    pid = ops.spawn([releases.python(), '-m', 'fleetmux.release_drift', 'switch', str(run / PLAN)], env,
                    str(run / LOG))
    gate(f)['switcher'] = pid
    f.log('release_drift.switching', old=old, new=inst, pid=pid, replace=plan['replace'])
    return pid


def g_first(f, inst, now):
    return gate(f)['first_seen'].get(inst, now)


def _jobs(raw):
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# ------------------------------------------------------------------ the switch process
class StepFailed(Exception):
    """A step of the switch failed: ``step`` (one of STEPS), ``why`` for the person, ``root_stopped`` False while the
    root daemon still runs the release it ran (nothing else was touched)."""

    def __init__(self, step, why, root_stopped=True):
        super().__init__(why)
        self.step, self.why, self.root_stopped = step, why, root_stopped


def _say(run, event, **fields):
    line = json.dumps(dict({'at': time.time(), 'event': event}, **fields), ensure_ascii=False)
    try:
        with open(Path(run) / LOG, 'a', encoding='utf-8') as fh:
            fh.write(line + '\n')
    except OSError:
        pass


def _quiet(*_):
    return None


def acquire(run):
    """The run's switch lock (``<run>/release-drift.lock``, flock), or None while another process holds it. One switch
    of a run at a time: the switch process, a manual ``fleetmux upgrade``; the daemon's tick never starts one while it
    is held."""
    try:
        fh = open(Path(run) / LOCK, 'a+')
    except OSError:
        return None
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def switch_running(run):
    """True while some process holds the run's switch lock."""
    path = Path(run) / LOCK
    if not path.exists():
        return False
    with open(path, 'a+') as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
    return False


def _daemon_pids(ops, run, root_only=True):
    """The daemons of ``run``: ``fleet.py daemon <run>/config.json`` (``root_only``, this run exactly), else every
    daemon below ``<run>/``; never the caffeinate / systemd-inhibit wrapper that carries the same command line."""
    needle = ' daemon ' + str(Path(run) / 'config.json') if root_only else ' daemon ' + str(run) + '/'
    out = []
    for pid, args in ops.ps():
        argv0 = Path(args.split()[0]).name if args.split() else ''
        if needle in args and argv0 not in WRAPPERS:
            out.append(pid)
    return out


def _up(ops, run):
    """A daemon of ``run`` still holds its lock or still has a process."""
    return ops.daemon_running(str(run)) or bool(_daemon_pids(ops, run))


def _wait(ops, cond, seconds):
    deadline = ops.now() + seconds
    while not cond():
        if ops.now() >= deadline:
            return False
        ops.sleep(1)
    return True


def _step(name, fn, say, root_stopped=True):
    """Run one step: its result line, or StepFailed with the step's name (an error of any kind included)."""
    try:
        detail = fn()
    except StepFailed:
        raise
    except Exception as exc:  # noqa: BLE001  (the step failed: named, with its reason, never a traceback)
        raise StepFailed(name, type(exc).__name__ + ': ' + str(exc)[:500], root_stopped)
    say(name, 'ok', detail or '')
    return detail


def _depth(run, d):
    return sum(1 for p in Path(d).relative_to(run).parts if p == 'sub')


def snapshot(plan, cfg, ops):
    """fx:jobs of the run's root Redis, written to <run>/jobs.json and the job-runs index; None when unreadable."""
    raw = ops.redis_get(cfg.get('redis_socket'), 'fx:jobs')
    if raw is None:
        return None
    jobs = _jobs(raw)
    write_jobs(plan, jobs)
    return jobs


def write_jobs(plan, jobs):
    run = Path(plan['run'])
    _write(run / 'jobs.json', jobs)
    index = Path(plan['fleet_home']) / 'job-runs' / (run.name + '.json')
    if jobs:
        index.parent.mkdir(parents=True, exist_ok=True)
        _write(index, {'run': str(run), 'config': str(run / 'config.json')})
    else:
        index.unlink(missing_ok=True)


def _attached(rec):
    named = rec.get('named')
    return bool(rec.get('attached') or (isinstance(named, dict) and named.get('attached')))


def _gui_pid(ops, runtime):
    return next((p for p, role in server_pids(ops, runtime) if role == 'gui'), None) if runtime else None


def observe(plan, cfg, ops, jobs):
    """What the checks compare with: the run as it is before any daemon stops."""
    run = Path(plan['run'])
    live = [str(d) for d in sub_runs(run) if ops.daemon_running(str(d))]
    named, up_after = [], []
    for d in live:
        rec = _json(Path(d) / 'subfleet.json') or {}
        if rec.get('named') and rec.get('state') == 'running':
            name = rec.get('name') or rec.get('task_id') or Path(d).name
            named.append({'name': name, 'run': d})
            if not rec.get('autostart') and not _attached(rec):
                up_after.append(name)  # section 29: nothing brings it back by itself
    applets = {}
    for d in [str(run)] + live:
        found = ops.applets(d)
        if found:
            applets[d] = found
    runtime = plan.get('server_runtime')
    gui = _gui_pid(ops, runtime)
    server = bool(runtime) and any(role == 'server' for _, role in server_pids(ops, runtime))
    shown = None
    if gui:
        on_screen = ops.windows()
        shown = None if on_screen is None else bool(ops.descendants(gui) & on_screen)
    return {'at': ops.now(), 'run': str(run), 'config': plan['config'], 'from': plan['old']['release'],
            'from_dir': plan['old'].get('dir') or '', 'to': plan['new']['release'], 'to_dir': plan['new']['dir'],
            'jobs': jobs, 'subs': live, 'subfleets': named, 'up_after': up_after, 'applets': applets,
            'supervisor': ops.redis_hget(cfg.get('redis_socket'), SUPERVISOR_KEY, 'state'), 'server': server,
            'window': {'runtime': runtime, 'pid': gui, 'shown': shown}}


# ---- stop
def _stop_root(plan, ops):
    run, wait_s = plan['run'], float(plan.get('stop_wait_s') or DEFAULTS['stop_wait_s'])
    roots = _daemon_pids(ops, run)
    if not roots and not ops.daemon_running(str(run)):
        return '(not running)'
    for pid in roots:
        ops.kill(pid, signal.SIGTERM)
    _say(run, 'stop.root', pids=roots)
    if not _wait(ops, lambda: not _up(ops, run), wait_s):  # never a sub-fleet while the root lives
        left = _daemon_pids(ops, run)
        raise StepFailed('stop_root', 'the root daemon did not exit within ' + str(int(wait_s)) + ' s of SIGTERM '
                         '(pids ' + ', '.join(map(str, left)) + '); nothing else was stopped', False)
    return '(pids ' + ', '.join(map(str, roots)) + ')'


def _stop_level(plan, ops, level, guard=True):
    """SIGTERM every daemon of one level (each matched by its own config), then wait until each let go of its lock.
    ``guard``: a daemon whose parent still runs is never signalled (the parent would collect it as cancelled)."""
    run, wait_s = Path(plan['run']), float(plan.get('stop_wait_s') or DEFAULTS['stop_wait_s'])
    for d in level:
        name = 'sub-fleet ' + _sub_name(run, d)
        parent = Path(d).parent.parent
        if guard and _up(ops, parent):
            raise StepFailed('stop_children', 'the parent of ' + name + ' still runs; a child is not stopped while '
                             'its parent can collect it')
        pids = _daemon_pids(ops, d)
        if not pids:
            raise StepFailed('stop_children', 'the daemon of ' + name + ' holds its lock but no process was found '
                             'for it')
        for pid in pids:
            ops.kill(pid, signal.SIGTERM)
    _say(run, 'stop.subs', runs=[str(d) for d in level])
    if not _wait(ops, lambda: not any(_up(ops, d) for d in level), wait_s):
        left = [_sub_name(run, d) for d in level if _up(ops, d)]
        raise StepFailed('stop_children', 'the daemon of sub-fleet ' + ', '.join(left) + ' still holds its lock '
                         + str(int(wait_s)) + ' s after SIGTERM; the deeper levels were not touched')


def _levels(plan, ops):
    run = Path(plan['run'])
    by = {}
    for d in sub_runs(run):
        by.setdefault(_depth(run, d), []).append(d)
    return [by[k] for k in sorted(by)]


def _stop_children(plan, ops):
    """Level by level: a level only once every daemon of the level above let go of its lock (a live parent
    collects a child whose daemon exited without result.json as cancelled)."""
    n, levels = 0, 0
    for level in _levels(plan, ops):
        level = [d for d in level if _up(ops, d)]
        if level:
            _stop_level(plan, ops, level)
            n, levels = n + len(level), levels + 1
    return '(%d daemons in %d levels)' % (n, levels) if n else '(none running)'


def stop_daemons(plan, ops, say=None):
    """The root daemon first, then the sub-fleets level by level. Raises StepFailed."""
    say = say or _quiet
    if STOP_ORDER == 'children_first':  # tests only: the order of the first restart script (the incident)
        for level in reversed(_levels(plan, ops)):
            level = [d for d in level if _up(ops, d)]
            if level:
                _stop_level(plan, ops, level, guard=False)
        _stop_root(plan, ops)
        return
    _step('stop_root', lambda: _stop_root(plan, ops), say, root_stopped=False)
    _step('stop_children', lambda: _stop_children(plan, ops), say)


def _stop_applets(plan, ops):
    """SIGTERM ends a daemon, not its applets: the next daemon would adopt a copy on the old interpreter."""
    run, stopped, left = Path(plan['run']), [], []
    for d in [run] + sub_runs(run):
        for name in ops.applets(str(d)):
            ops.stop_applet(str(d), name)
            stopped.append(name)
        left += sorted(ops.applets(str(d)))
    if left:
        raise StepFailed('stop_applets', 'applets still running after their stop: ' + ', '.join(left))
    return '(' + (', '.join(stopped) or 'none') + ')'


def _sockets(plan, cfg):
    socks = [cfg.get('redis_socket')] + [(_json(Path(d) / 'config.json') or {}).get('redis_socket')
                                         for d in sub_runs(plan['run'])]
    return [s for s in dict.fromkeys(socks) if s]


def _redis_down(plan, cfg, ops):
    socks = _sockets(plan, cfg)
    for sock in socks:
        if ops.redis_answers(sock):
            ops.redis_shutdown(sock)
    if not _wait(ops, lambda: not any(ops.redis_answers(s) for s in socks), REDIS_DOWN_S):
        left = [s for s in socks if ops.redis_answers(s)]
        raise StepFailed('redis_down', 'the Redis at ' + ', '.join(left) + ' still answers ' + str(REDIS_DOWN_S)
                         + ' s after SHUTDOWN SAVE')
    return '(%d saved and down)' % len(socks)


def _terminal_down(plan, ops):
    runtime = plan.get('server_runtime')
    if not runtime:
        return '(no fleetmux terminal runtime)'
    servers = server_pids(ops, runtime)
    for pid, _ in servers:
        ops.kill(pid, signal.SIGTERM)
    _say(plan['run'], 'stop.server', pids=[p for p, _ in servers])
    if not _wait(ops, lambda: not server_pids(ops, runtime), 10):
        raise StepFailed('terminal_down', 'processes of the terminal runtime ' + runtime + ' still alive after '
                         'SIGTERM: ' + ', '.join(str(p) for p, _ in server_pids(ops, runtime)))
    return '(' + runtime + ')'


def stop_run(plan, cfg, ops, jobs, say=None):
    """Root daemon first, then the sub-fleet daemons level by level, the applets, the job files from the snapshot,
    Redis (saved, and no longer answering) and the run's fleetmux server / GUI. Raises StepFailed."""
    say = say or _quiet
    stop_daemons(plan, ops, say)
    _step('stop_applets', lambda: _stop_applets(plan, ops), say)
    _step('restore_files', lambda: write_jobs(plan, jobs) or '(' + str(Path(plan['run']) / 'jobs.json')
          + ' from the snapshot)', say)
    _step('redis_down', lambda: _redis_down(plan, cfg, ops), say)
    _step('terminal_down', lambda: _terminal_down(plan, ops), say)


# ---- switch the interpreter, start
def python_of(rel_dir):
    """The interpreter of release directory ``rel_dir``: its .venv/bin/python, python3 or python3.x; None."""
    venv = Path(rel_dir) / '.venv' / 'bin'
    if not rel_dir or not venv.is_dir():
        return None
    for name in ['python', 'python3'] + sorted((p.name for p in venv.glob('python3.*')), reverse=True):
        if (venv / name).exists():
            return str(venv / name)
    return None


def _save_cfg(path, data):  # the format of fleet.save
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix='.' + path.name + '.')
    with os.fdopen(fd, 'w') as fh:
        fh.write(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    os.replace(tmp, path)


def write_python(plan, target):
    """Every config of the run (root and each sub-fleet, any depth) runs ``target``'s interpreter, written while no
    daemon runs: a release that does not switch it at its own resume (one from before that) starts on its own venv
    too, and actors, hook lines, history panes and applets are built from ``python``."""
    py = python_of(target['dir'])
    if not py:
        if plan.get('start'):  # start.sh finds the interpreter of FLEETMUX_REL itself
            return None, 0
        raise StepFailed('start_new', 'no interpreter in ' + str(Path(target['dir'] or '?') / '.venv' / 'bin'))
    changed = 0
    run = Path(plan['run'])
    for path in [run / 'config.json'] + [d / 'config.json' for d in sub_runs(run)]:
        data = _json(path)
        if isinstance(data, dict) and data.get('python') != py:
            data['python'] = py
            _save_cfg(path, data)
            changed += 1
    return py, changed


def start_env(plan, target, base=None):
    """The environment of the start: ``FLEETMUX_REL=<target dir>``, the target's ``bin`` first on PATH and its
    ``bin/fleetmux`` as FLEETMUX_BIN (an inherited one names the release before), the plan's ``env``, and the
    daemon's PYTHONPATH (the plan's ``pythonpath``) without any entry inside the old or the new release directory:
    the target's interpreter must import that release's own fleetmux, never the switch process's."""
    env = dict(os.environ if base is None else base)
    raw = plan['pythonpath'] if 'pythonpath' in plan else env.get('PYTHONPATH', '')
    roots = [r for r in (plan['old'].get('dir'), plan['new'].get('dir')) if r]
    parts = [x for x in str(raw or '').split(os.pathsep) if x and not any(_inside(x, r) for r in roots)]
    if parts:
        env['PYTHONPATH'] = os.pathsep.join(parts)
    else:
        env.pop('PYTHONPATH', None)
    if not plan.get('start'):  # the direct start of the manual entry: the fleet's own home and state
        env.update(plan.get('env') or {})
    rel = Path(target['dir'])
    env['FLEETMUX_REL'] = str(rel)
    env['FLEETMUX_BIN'] = str(rel / 'bin' / 'fleetmux')
    env['PATH'] = os.pathsep.join([str(rel / 'bin')] + [p for p in env.get('PATH', '').split(os.pathsep)
                                                         if p and p != str(rel / 'bin')])
    return env


def _start(plan, target, ops, before):
    py, changed = write_python(plan, target)
    env = start_env(plan, target)
    run = plan['run']
    rel = Path(target['dir'])
    if plan.get('start'):
        argv = [plan['start'], '--resume', run]
    else:  # no start.sh (the manual entry only): the target release's own start --resume of this run
        argv = [py, str(rel / 'fleetmux' / 'fleet.py'), 'start', '--resume', run]
    _say(run, 'start', release=target['release'], argv=argv, pythonpath=env.get('PYTHONPATH', ''))
    code = ops.start(argv, env, str(Path(run) / LOG), START_TIMEOUT_S)
    _say(run, 'started', release=target['release'], code=code)
    if code != 0:
        raise StepFailed('start_new', ' '.join(argv[:1] + argv[-3:] if plan.get('start') else argv[-3:])
                         + ' on release ' + str(target['release']) + ' exited ' + str(code) + ' (its output is in '
                         + str(Path(run) / LOG) + ')')
    asked = []
    if before.get('up_after'):
        if not _wait(ops, lambda: ops.daemon_running(run), ROOT_UP_S):
            raise StepFailed('start_new', 'the root daemon did not take its lock within %d s after the start'
                             % ROOT_UP_S)
        for name in before['up_after']:
            spec = {'id': 'upgrade-' + Path(plan.get('backup_dir') or run).name + '-' + name, 'op': 'subfleet.up',
                    'name': name, 'reason': 'running before the release switch'}
            code, text = ops.run_input([py, str(rel / 'fleetmux' / 'fleet.py'), 'request', '-', '--config',
                                        plan['config']], json.dumps(spec), env, 120)
            asked.append(name + ('' if code == 0 else ' (request exit %d: %s)' % (code, str(text).strip()[-200:])))
    return ('(' + str(target['release']) + ' started; python of %d configs set to %s' % (changed, py)
            + ('; subfleet.up asked for ' + ', '.join(asked) if asked else '') + ')')


# ---- check
def command_argv(argv):
    """A daemon's command line without a keep-awake wrapper: [python, fleet.py, ...]."""
    argv = list(argv or [])
    for i, a in enumerate(argv):
        if a.endswith('fleet.py') and i > 0:
            return argv[i - 1:]
    return argv


FRAMEWORK_EXE = '/Python.app/Contents/MacOS/'  # a macOS framework Python's real executable


def process_python(ops, pid, argv0, rel):
    """The interpreter Python process ``pid`` runs as, judged against release directory ``rel``: ``argv0``, or,
    when ``argv0`` is a macOS framework Python outside ``rel``, the venv python it was started as
    (``__PYVENV_LAUNCHER__``, ``Ops.launcher``). Any other ``argv0`` is taken as it is."""
    if not argv0 or in_release(argv0, rel) or FRAMEWORK_EXE not in argv0:
        return argv0
    return ops.launcher(pid) or argv0


def in_release(path, rel):
    """True when ``path`` lies inside release directory ``rel`` (compared as given and resolved)."""
    if not path or not rel:
        return False
    rel = Path(rel)
    for p in (Path(path), Path(os.path.realpath(path))):
        for r in (rel, rel.resolve()):
            if p == r or r in p.parents:
                return True
    return False


def _ended(d):
    """A sub-fleet whose work ended while the run was switched: nothing to bring back."""
    rec = _json(Path(d) / 'subfleet.json') or {}
    return (not Path(d).is_dir() or (Path(d) / 'result.json').exists()
            or rec.get('state') in ('returned', 'failed', 'cancelled'))


def _fleetmux(target):
    return str(Path(target['dir']) / 'bin' / 'fleetmux')


def _again(plan, target):
    return [_fleetmux(target) + ' upgrade --to ' + str(target['dir']) + ' --config ' + plan['config']
            + '   # run the same command again']


def _back(plan):
    old = plan['old'].get('dir')
    return [_fleetmux(plan['new']) + ' upgrade --to ' + old + ' --config ' + plan['config']
            + '   # or go back to the release that ran before'] if old else []


def _resume(plan, target):
    """The command that starts the run on release ``target`` by hand: the fleet's start.sh with FLEETMUX_REL, else
    the release's own ``start --resume <run>`` with the fleet's home and state (what ``_start`` runs)."""
    rel, run = str(target['dir']), plan['run']
    if plan.get('start'):
        return 'FLEETMUX_REL=%s %s --resume %s' % (shlex.quote(rel), shlex.quote(plan['start']), shlex.quote(run))
    env = dict(plan.get('env') or {}, FLEETMUX_REL=rel, FLEETMUX_BIN=str(Path(rel) / 'bin' / 'fleetmux'))
    py = python_of(rel) or str(Path(rel) / '.venv' / 'bin' / 'python')
    return ' '.join(['%s=%s' % (k, shlex.quote(str(v))) for k, v in env.items()]
                    + [shlex.quote(py), shlex.quote(str(Path(rel) / 'fleetmux' / 'fleet.py')), 'start', '--resume',
                       shlex.quote(run)])


def _down(plan, target):
    """The recovery when the way back failed too: the run may be down and a rerun would find no root daemon."""
    return [_resume(plan, target)]


def _check(status, detail='', recover=()):
    return {'status': status, 'detail': detail, 'recover': list(recover)}


def check_daemons(plan, cfg, target, ops, before):
    run, want, bad = Path(plan['run']), target['release'], []
    if not ops.daemon_running(str(run)):
        bad.append('root daemon is not up')
    else:
        found = releases.running(_json(run / 'config.json') or cfg)
        if found and not releases.same(found, want):
            bad.append('root daemon runs release ' + found)
    for d in before.get('subs') or []:
        name = 'sub-fleet daemon ' + _sub_name(run, d)
        if _ended(d):
            continue
        if not ops.daemon_running(str(d)):
            bad.append(name + ' is not up')
            continue
        found = releases.running(_json(Path(d) / 'config.json') or {})
        if found and not releases.same(found, want):
            bad.append(name + ' runs release ' + found)
    server = bool(plan.get('server_runtime')) and before.get('server', True)  # only one that ran before
    if server:
        servers = [p for p, role in server_pids(ops, plan['server_runtime']) if role == 'server']
        if not servers:
            bad.append('fleetmux server is not up')
        for pid in servers:
            found = releases.of_argv(ops.cmdline(pid))
            if found and not releases.same(found, want):
                bad.append('fleetmux server runs release ' + found)
    if bad:
        return _check('fail', '; '.join(bad), _again(plan, target) + _back(plan))
    return _check('ok', '(root, %d sub-fleets%s on %s)' % (len(before.get('subs') or []), ', server'
                                                           if server else '', want))


def check_interpreter(plan, cfg, target, ops, before):
    """The daemons, every config's ``python`` (actors, hook lines and history panes are started with it)
    and the applet processes (on macOS a framework venv's process is known by its launcher: ``process_python``). A
    CLI session that was already attached keeps its hook lines until its next generation."""
    run, rel, bad = Path(plan['run']), target['dir'], []
    for d in [run] + [d for d in sub_runs(run) if ops.daemon_running(str(d))]:
        key = 'root' if d == run else _sub_name(run, d)
        python = (_json(Path(d) / 'config.json') or {}).get('python')
        if not in_release(python, rel):
            bad.append(key + ' config python ' + str(python))
        for pid in _daemon_pids(ops, d):
            argv = command_argv(ops.cmdline(pid))
            exe = process_python(ops, pid, argv[0] if argv else '', rel)
            if len(argv) < 2 or not in_release(exe, rel) or not in_release(argv[1], rel):
                bad.append(key + ' daemon %d on %s' % (pid, ' '.join([exe] + argv[1:2]) if argv else '?'))
        for name, pid in sorted(ops.applets(str(d)).items()):
            argv = ops.cmdline(pid) or []
            exe = process_python(ops, pid, argv[0] if argv else '', rel)
            if not argv or not in_release(exe, rel):
                bad.append(key + ' applet ' + name + ' on ' + (exe or '?'))
    if bad:
        return _check('fail', 'not of ' + str(target['release']) + ': ' + '; '.join(bad), _again(plan, target))
    return _check('ok', '(' + str(python_of(rel)) + ')')


def check_jobs(plan, cfg, target, ops, before):
    from fleetmux import restart_state
    want = {j: r for j, r in (before.get('jobs') or {}).items() if isinstance(r, dict) and r.get('state') in OPEN_JOB}
    if not want:
        return _check('ok', '(no open jobs before)')
    raw = ops.redis_get(cfg.get('redis_socket'), 'fx:jobs')
    if raw is None:
        return _check('fail', 'fx:jobs of the run cannot be read', [_fleetmux(target) + ' status --config '
                                                                   + plan['config']])
    now = {j: (r or {}).get('state') for j, r in _jobs(raw).items()}
    results = Path(plan['run']) / 'job-results'
    bad, recover, completed = [], [], []
    for jid, row in sorted(want.items()):
        state, done = now.get(jid), (_json(results / (jid + '.json')) or {}).get('status')
        if state in SETTLED_JOB and done not in ('failed', 'cancelled'):
            continue
        if state is None and done == JOB_SUCCESS:  # it ended successfully meanwhile, not lost
            completed.append(jid)
            continue
        bad.append(jid + ' ' + (state or ('published ' + done if done else 'missing')))
        if state not in OPEN_JOB and plan.get('backup_dir'):
            path = Path(plan['backup_dir']) / ('resubmit-' + jid + '.json')
            restart_state.write_json(path, restart_state.submit_spec(row))
            recover.append('%s task --config %s - < %s' % (_fleetmux(target), plan['config'], path))
    if bad:
        if plan.get('backup_dir'):
            recover.append('# the jobs before the switch: ' + str(Path(plan['backup_dir']) / 'fx-jobs.json'))
        return _check('fail', 'not back as running / queued: ' + ', '.join(bad), recover)
    return _check('ok', '(%d open jobs back%s)' % (len(want) - len(completed), '; completed during the switch: '
                                                  + ', '.join(completed) if completed else ''))


def check_supervisor(plan, cfg, target, ops, before):
    """A root supervisor that was held before the switch is reported, never failed."""
    rec = ['%s status --config %s   # the supervisor slot and why it is held; a pause review lifts a hold whose '
           'cause is gone (section 24)' % (_fleetmux(target), plan['config'])]
    was = before.get('supervisor')
    now = ops.redis_hget(cfg.get('redis_socket'), SUPERVISOR_KEY, 'state')
    if was == 'held':
        return _check('n/a', '(held before the switch already' + ('' if now == 'held' else '; now ' + str(now))
                      + ')')
    if not was:
        return _check('n/a', '(no supervisor state could be read before the switch; now ' + str(now) + ')')
    if not now:
        return _check('fail', 'no supervisor state can be read after the switch (it was ' + was + ')', rec)
    if now == 'held':
        return _check('fail', 'the root supervisor is held (it was ' + was + ' before the switch)', rec)
    return _check('ok', '(' + now + ')')


def check_subfleets(plan, cfg, target, ops, before):
    want = before.get('subfleets') or []
    if not want:
        return _check('ok', '(none ran before)')
    run, found = Path(plan['run']), set()
    for d in sub_runs(run):
        rec = _json(d / 'subfleet.json') or {}
        if rec.get('state') in ('starting', 'running') and ops.daemon_running(str(d)):
            found.add(rec.get('name') or rec.get('task_id') or d.name)
    bad = [s['name'] for s in want if s['name'] not in found and not _ended(s['run'])]
    if bad:
        rec = ["echo '%s' | %s request - --config %s" % (json.dumps(
            {'id': 'upgrade-up-' + name, 'op': 'subfleet.up', 'name': name, 'reason': 'after the release switch'}),
            _fleetmux(target), plan['config']) for name in bad]
        return _check('fail', 'not running: ' + ', '.join(bad), rec)
    return _check('ok', '(' + ', '.join(s['name'] for s in want) + ')')


def check_applets(plan, cfg, target, ops, before):
    want = before.get('applets') or {}
    if not want:
        return _check('ok', '(none ran before)')
    bad, rec = [], []
    for run, names in sorted(want.items()):
        now = ops.applets(run)
        for name, old_pid in sorted(names.items()):
            if not now.get(name) or now[name] == old_pid:
                bad.append(name + (' (the copy from before)' if now.get(name) else ''))
                rec.append('%s applet restart %s --config %s' % (_fleetmux(target), name,
                                                                 Path(run) / 'config.json'))
    if bad:
        return _check('fail', 'not running on the new release: ' + ', '.join(bad), rec)
    return _check('ok', '(' + ', '.join(sorted(n for names in want.values() for n in names)) + ')')


def check_window(plan, cfg, target, ops, before):
    """No window before the switch (or one that was not on screen) is reported, never failed."""
    win = before.get('window') or {}
    if not win.get('pid'):
        return _check('n/a', '(no window before the switch)')
    if win.get('shown') is False:
        return _check('n/a', '(the window process had no on-screen window before the switch)')
    rec = [_fleetmux(target) + ' gui   # opens (or raises) the root fleet window']
    pid = _gui_pid(ops, win.get('runtime'))
    if not pid:
        return _check('fail', 'no `fleetmux gui --runtime` process for ' + str(win.get('runtime')), rec)
    on_screen = ops.windows()
    if on_screen is None:
        return _check('ok', '(window process %d; the window list cannot be read here)' % pid)
    if not (ops.descendants(pid) & on_screen):
        return _check('fail', 'process %d has no on-screen window' % pid, rec)
    return _check('ok', '(window process %d on screen)' % pid)


CHECK_FNS = {'daemons': check_daemons, 'interpreter': check_interpreter, 'jobs': check_jobs,
             'supervisor': check_supervisor, 'subfleets': check_subfleets, 'applets': check_applets,
             'window': check_window}


def verify(plan, cfg, target, ops, before):
    """The checks of CHECKS, polled until verify_s while one fails: {name: {status, detail, recover}}."""
    deadline = ops.now() + float(plan.get('verify_s') or DEFAULTS['verify_s'])
    while True:
        results = {}
        for name in CHECKS:
            try:
                results[name] = CHECK_FNS[name](plan, cfg, target, ops, before)
            except Exception as exc:  # noqa: BLE001  (a check that cannot run fails with its reason)
                results[name] = _check('fail', type(exc).__name__ + ': ' + str(exc)[:300])
        if all(r['status'] != 'fail' for r in results.values()) or ops.now() >= deadline:
            return results
        ops.sleep(VERIFY_POLL_S)


def attempt(plan, cfg, target, ops, jobs, before, say=None):
    """Stop the run and continue it on ``target`` ({release, dir}), then check.
    {ok, step, why, root_stopped, checks, recover}."""
    say = say or _quiet
    res = {'ok': False, 'step': None, 'why': None, 'root_stopped': True, 'checks': {}, 'recover': []}
    try:
        stop_run(plan, cfg, ops, jobs, say)
        _step('start_new', lambda: _start(plan, target, ops, before), say)
    except StepFailed as exc:
        say(exc.step, 'FAIL', exc.why)
        res.update(step=exc.step, why=exc.why, root_stopped=exc.root_stopped,
                   recover=_again(plan, target) + _back(plan))
        return res
    checks = verify(plan, cfg, target, ops, before)
    failed = [n for n in CHECKS if checks[n]['status'] == 'fail']
    for name in CHECKS:
        c = checks[name]
        say('verify ' + name, {'ok': 'ok', 'fail': 'FAIL', 'n/a': 'n/a'}[c['status']], c['detail'])
    res['checks'] = checks
    if failed:
        say('verify', 'FAIL', ', '.join(failed))
        recover = []
        for n in failed:
            recover += [r for r in checks[n]['recover'] if r not in recover]
        res.update(step='verify', why='check failed: ' + '; '.join(n + ': ' + checks[n]['detail'] for n in failed),
                   recover=recover)
        return res
    say('verify', 'ok', '')
    res['ok'] = True
    return res


def jobs_back(plan, cfg, expected, ops):
    now = _jobs(ops.redis_get(cfg.get('redis_socket'), 'fx:jobs'))
    results = Path(plan['run']) / 'job-results'
    missing = sorted(j for j in expected if j not in now and not (results / (j + '.json')).exists())
    return {'expected': len(expected), 'back': len(expected) - len(missing), 'missing': missing}


def _backup(plan, before):
    if not plan.get('backup_dir'):
        stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
        plan['backup_dir'] = str(Path(plan['fleet_home']) / 'release-drift' / stamp)
    d = Path(plan['backup_dir'])
    d.mkdir(parents=True, exist_ok=True)
    _write(d / 'fx-jobs.json', before['jobs'])
    _write(d / 'before.json', before)
    return d / 'before.json'


def switch(plan, ops=None, say=None, lock=None):
    """The switch: snapshot and the state before (written outside the run directory too), stop (root first, then
    each level), continue on the new release, check; on a failure after the root stopped the same way back to the old
    release. ``say(step, status, detail)`` gets one call per step and check; ``lock``: the run's switch lock when the
    caller already holds it (the manual entry), else this takes it. With ``plan['resume']`` (the manual entry, the
    switch process died between the stop and the start) it goes on from the start with the plan's own snapshot.
    Writes <run>/release-drift-outcome.json and returns it."""
    ops = ops or Ops()
    say = say or _quiet
    run = Path(plan['run'])
    held = lock or acquire(run)
    if held is None:
        reason = 'another release switch of this run is running (' + str(run / LOCK) + ' is held); nothing was changed'
        say('preflight', 'FAIL', reason)
        return dict(_outcome(plan), step='preflight', reason=reason, rolled_back=True)
    try:
        return _switch(plan, ops, say)
    finally:
        if lock is None:
            held.close()


def _outcome(plan):
    old, new = plan['old'], plan['new']
    return {'ok': False, 'from': old['release'], 'to': new['release'], 'replaced': plan.get('replace') or [],
            'jobs': {}, 'reason': None, 'step': '', 'rolled_back': None, 'rollback_step': None,
            'rollback_reason': None, 'checks': {},
            'recover': [], 'backup': '', 'manual': bool(plan.get('manual')),
            'max_failures': plan.get('max_failures', DEFAULTS['max_failures']), 'reported': False}


def _switch(plan, ops, say):
    run = Path(plan['run'])
    cfg = _json(plan['config']) or {}
    old, new = plan['old'], plan['new']
    outcome = _outcome(plan)
    (run / OUTCOME).unlink(missing_ok=True)  # from here on, an outcome file means this switch ended
    _say(run, 'switch.begin', old=old, new=new, resume=bool(plan.get('resume')))
    if plan.get('resume'):
        before = plan.get('before') or {}
        jobs = _json(Path(plan.get('backup_dir') or '') / 'fx-jobs.json') if plan.get('backup_dir') else None
        if not isinstance(jobs, dict):
            jobs = before.get('jobs') if isinstance(before.get('jobs'), dict) else None
        if jobs is None:
            outcome.update(step='backup', reason='the snapshot of the stopped switch cannot be read: nothing was '
                           'started', rolled_back=False)
            say('backup', 'FAIL', outcome['reason'])
            return finish(plan, outcome, ops)
        outcome['backup'] = str(Path(plan['backup_dir']) / 'before.json')
    else:
        roots = _daemon_pids(ops, run)
        jobs = snapshot(plan, cfg, ops) if len(roots) == 1 else None  # BEFORE any daemon is stopped
        if len(roots) != 1:
            outcome.update(step='preflight', reason='expected exactly one root daemon process, found ' + str(roots)
                           + '; nothing was stopped')
        elif jobs is None:
            outcome.update(step='backup', reason='fx:jobs of the run could not be read; nothing was stopped')
        if outcome['reason']:
            say(outcome['step'], 'FAIL', outcome['reason'])
            outcome['rolled_back'] = True  # nothing changed: the run goes on as it was
            return finish(plan, outcome, ops)
        before = observe(plan, cfg, ops, jobs)
        plan['before'] = before
        outcome['backup'] = str(_backup(plan, before))
        _write(run / PLAN, plan)  # what a rerun goes on from when this process dies after the stop
        say('backup', 'ok', '(%d jobs, %d sub-fleet daemons, backup %s)' % (len(jobs), len(before['subs']),
                                                                            outcome['backup']))
    res = attempt(plan, cfg, new, ops, jobs, before, say)
    outcome['checks'] = {n: {'status': c['status'], 'detail': c['detail']} for n, c in res['checks'].items()}
    if res['ok']:
        outcome.update(ok=True, jobs=jobs_back(plan, cfg, jobs, ops))
        return finish(plan, outcome, ops)
    outcome.update(reason=res['why'], step=res['step'], recover=res['recover'])
    _say(run, 'switch.failed', step=res['step'], reason=res['why'])
    if not res['root_stopped']:  # the root daemon still runs the old release and nothing else was touched
        outcome['rolled_back'] = True
        return finish(plan, outcome, ops)
    again = snapshot(plan, cfg, ops)  # the new release's fx:jobs, when its Redis came up
    keep = dict(jobs)
    keep.update(again or {})  # never fewer jobs than the snapshot before the switch
    results = run / 'job-results'
    keep = {j: v for j, v in keep.items() if not (results / (j + '.json')).exists()}  # done once: not again
    back_say = (lambda step, status, detail: say('rollback ' + step, status, detail))
    if not old.get('dir'):
        outcome.update(rolled_back=False, rollback_step='start_new',  # the old release could not be started
                       rollback_reason='the directory of release ' + str(old['release'])
                       + ' is not known: the run may be down; start it again with its start --resume',
                       recover=_down(plan, new))
    else:
        back = attempt(plan, cfg, old, ops, keep, before, back_say)
        # rollback_step: '' the way back worked; 'verify' the old release is up and only a check failed; another
        # step: the way back stopped there and the run may be down or mixed
        outcome.update(rolled_back=back['ok'], rollback_step='' if back['ok'] else (back['step'] or 'start_new'),
                       rollback_reason=None if back['ok'] else back['why'])
        if back['step'] == 'verify':  # the old release is up again: its failing checks say what to do
            outcome['recover'] = back['recover']
        elif not back['ok'] and back['root_stopped']:
            rerun = _again(plan, new) + _back(plan)  # both need a running root daemon
            outcome['recover'] = _down(plan, old) + [r for r in res['recover'] if r not in rerun]
    outcome['jobs'] = jobs_back(plan, cfg, jobs, ops)
    return finish(plan, outcome, ops)


def stopped_nothing(outcome):
    """A refusal before anything was stopped: the preflight or the backup failed and the run goes on as it was."""
    return outcome.get('step') in ('preflight', 'backup') and outcome.get('rolled_back') is True


def finish(plan, outcome, ops):
    """Write the outcome. A failed switch counts towards ``max_failures`` of its release; a refusal that stopped
    nothing does not (it says nothing about the release)."""
    run = Path(plan['run'])
    if not outcome['ok']:
        st = load_state(run)
        n = int(st['failures'].get(outcome['to']) or 0)
        if not stopped_nothing(outcome):
            n += 1
            st['failures'][outcome['to']] = n
            save_state(run, st)
        outcome['failures'] = n
    outcome['at'] = ops.now()
    _write(run / OUTCOME, outcome)
    _say(run, 'switch.end', ok=outcome['ok'], step=outcome.get('step'), reason=outcome['reason'],
         rolled_back=outcome['rolled_back'])
    return outcome


# ------------------------------------------------------------------ install_release.sh --activate
def live_fleets(home=None):
    """[(name, release, cfg)] of every root fleet of this machine whose daemon runs: ``<home>/latest.json`` and
    ``<home>/fleets/*/latest.json`` (home: the default fleet home)."""
    from fleetmux import paths
    home = Path(home) if home else paths.default_home()
    seen, out = set(), []
    for latest in [home / 'latest.json'] + sorted((home / 'fleets').glob('*/latest.json')):
        data = _json(latest)
        if not isinstance(data, dict):
            continue
        cfg = _json(data.get('config') or Path(str(data.get('run') or '')) / 'config.json') or data
        run = cfg.get('run') if isinstance(cfg, dict) else None
        if not run or run in seen or cfg.get('parent_config'):
            continue
        seen.add(run)
        try:
            if not releases.daemon_running(run):
                continue
        except OSError:
            continue
        out.append((cfg.get('name') or latest.parent.name, releases.running(cfg), cfg))
    return out


def release_dir(rel, cfg, link=None):
    """The directory of release ``rel`` on this machine: beside the installed one under ``fleetmux-releases``, else
    the release directory in the run config's ``python`` path; None when neither exists."""
    found = []
    path = Path(link or releases.LINK).expanduser()
    if path.exists():
        found.append(path.resolve().parent / rel)
    parts = Path((cfg or {}).get('python') or '').parts
    if 'fleetmux-releases' in parts[:-1]:
        i = parts.index('fleetmux-releases')
        found.append(Path(*parts[:i + 2]))
    return next((d for d in found if d.name == rel and d.is_dir()), None)


def activated(link=None, home=None, stream=None):
    """The lines ``install_release.sh --activate`` prints last, one per running fleet on another release than the
    installed one: when its daemon switches it, if its release has the automatic switch (``fleetmux/
    release_drift.py`` in its release directory); otherwise that it does not switch by itself and how to restart it
    by hand. Releases from before this module never switch by themselves."""
    stream = stream or sys.stdout
    inst = releases.installed(link)
    if not inst:
        return 0
    k, _ = knobs()
    stale = [(n, r, c) for n, r, c in live_fleets(home) if r and not releases.same(r, inst)]
    if not stale:
        print('no running fleet on this machine runs another release than ' + inst, file=stream)
        return 0
    for name, rel, cfg in stale:
        head = 'fleet ' + name + ' runs release ' + rel + ' (installed now: ' + inst + '): '
        rel_dir = release_dir(rel, cfg, link)
        inst_dir = Path(link or releases.LINK).expanduser().resolve()
        restart = ('move it to release ' + inst + ' with one command: ' + str(inst_dir / 'bin' / 'fleetmux')
                   + ' upgrade --to ' + str(inst_dir) + ' --config ' + str(Path(str(cfg.get('run'))) / 'config.json'))
        if rel_dir is None:
            print(head + 'it will not switch by itself (release directory not found, so its daemon cannot be '
                  'known to have the automatic switch); ' + restart, file=stream)
        elif not (rel_dir / 'fleetmux' / 'release_drift.py').is_file():
            print(head + 'it will not switch by itself (no fleetmux/release_drift.py in ' + str(rel_dir)
                  + ': its release predates the automatic switch); ' + restart, file=stream)
        elif k['enabled']:
            print(head + 'its daemon switches it to release ' + inst + ' by itself within ' + str(minutes(k))
                  + ' minutes (fleet.yaml release_drift: a check every ' + str(int(k['check_interval_s'] // 60))
                  + ' min, grace ' + str(int(k['grace_s'] // 60)) + ' min; later while a worker is in a turn or a '
                  'blackout window is open)', file=stream)
        else:
            print(head + 'automatic switching is off (fleet.yaml release_drift.enabled: false); ' + restart,
                  file=stream)
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ['switch'] and len(argv) == 2:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)  # outlives the terminal of the daemon that started it
        plan = _json(argv[1])
        if not isinstance(plan, dict):
            print('release_drift switch: unreadable plan ' + argv[1], file=sys.stderr)
            return 2
        outcome = switch(plan)
        print(json.dumps(outcome, ensure_ascii=False))
        return 0 if outcome['ok'] else 1
    if argv == ['activated']:
        return activated()
    print('usage: python -m fleetmux.release_drift switch <plan.json> | activated', file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())
