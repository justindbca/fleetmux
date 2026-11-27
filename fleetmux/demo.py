"""`fleetmux demo`: a one-off fleet whose agents are scripted, so the acceptance can be watched without a model.

What runs is the product: `start` (task mode, the `smoke` lifecycle, the fleetmux terminal backend, 1 supervisor +
2 workers), `task`, the daemon, the v2 bus, the Claude Stop-hook delivery, the literal artifact check of a
COMPLETED claim (fleet.check_artifacts in Fleet.on_supervisor) and the audit stream. Only the agents are replaced:
``fleetmux/demo_actor.py`` answers in place of the Claude Code CLI from a fixed script. The supervisor claims
COMPLETED after the worker's first report although a declared file is missing; the daemon rejects the claim and the
task stays open; the worker adds the file; the second claim passes and the task completes. The summary at the end is
read from the run's audit stream (``acceptance.verdict`` events), not from the script.

Isolation (nothing of the user's is read or written): one new directory ``/tmp/fmxd-XXXXXX`` (short: the private
Redis and the terminal server use Unix sockets inside it) holds HOME, FLEETMUX_HOME, FLEETMUX_STATE, the Redis
runtime root, the runs and the actor launcher (``bin/claude``, not on PATH). The orphan-Redis sweep of `start` is
limited to that Redis root. Clean-up stops only what this run recorded (its daemon through `stop`, its Redis through
its own socket, its terminal server through its own runtime directory, processes by recorded pid) and removes the
directory; it runs on the normal end, on an error and on Ctrl-C. The one setting taken from the caller is the
interface language (``ui.language``, docs/design.md §63), resolved before the world is made and passed in
``FLEETMUX_UI_LANGUAGE``.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from fleetmux import discover
from fleetmux.demo_actor import BANNER, GOAL_MARK, SLUG, TEST

PKG = Path(__file__).resolve().parent
FLEET_PY = PKG / 'fleet.py'
ACTOR_PY = PKG / 'demo_actor.py'
TIME_LIMIT_S = 90          # the task must end within this (the target is 60 s for the whole demo) ...
TURNS = 8                  # ... or within this many scripted turns of FLEETMUX_DEMO_TURN_S plus a minute, if longer
TURN_ENV = 'FLEETMUX_DEMO_TURN_S'  # seconds per scripted turn (tests and recordings slow the actors down)
SWEEP_ENV = 'FLEETMUX_ORPHAN_SWEEP_ROOT'  # fleet.sweep_orphan_redis: limit the sweep to this Redis root
# Environment never passed to the demo's processes: the caller's fleet, Claude / model sessions, terminals.
SCRUB_PREFIX = ('FLEETMUX_', 'CLAUDE', 'ANTHROPIC_', 'OPENAI_', 'CODEX_', 'CMUX_', 'TMUX', 'WEZTERM_')
SCRUB = ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'XPC_SERVICE_NAME')
NEEDED = ('redis-server', 'tmux', 'git', 'fleetmux')  # deps.yaml programs the demo's run uses (backend fleetmux)


def say(*parts):
    print(*parts, flush=True)


# ----------------------------------------------------------------------------------------------- dependencies
def check_dependencies(which=None, probe=None):
    """The deps.yaml rows of the programs the demo needs, found the way `start` finds them (discover.which).
    Returns (rows, paths, missing): ``rows`` are check_deps.py-style lines, ``missing`` the names that fail."""
    which = which or discover.which
    probe = probe or _version_of
    spec = discover.deps_programs()
    rows, paths, missing = [], {}, []
    for name in NEEDED:
        item = spec.get(name) or {}
        need = str(item.get('min') or '')
        path = (os.environ.get('FLEETMUX_BIN') if name == 'fleetmux' else None) or which(name) or shutil.which(name)
        if not path:
            rows.append('FAIL  ' + name + ' missing (need ' + (need or 'any') + '): ' + (item.get('why') or ''))
            missing.append(name)
            continue
        cmd = [path] + list((item.get('cmd') or [name, '--version'])[1:])
        have = discover.version(probe(cmd) or '')
        if need and (have is None or not discover.at_least(have, need)):
            shown = '.'.join(map(str, have)) if have else 'unreadable version'
            rows.append('FAIL  ' + name + ' ' + shown + ' at ' + path + ' (need ' + need + ')')
            missing.append(name)
            continue
        rows.append('ok    ' + name + ' ' + ('.'.join(map(str, have)) if have else '') + ' ' + path)
        paths[name] = path
    return rows, paths, missing


def _version_of(cmd):
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
        return (done.stdout or '') + (done.stderr or '')
    except (OSError, subprocess.SubprocessError):
        return ''


# ----------------------------------------------------------------------------------------------- the world
LIVE_ENV = 'FLEETMUX_DEMO_LIVE'   # the actor's liveness marker (demo_actor.Actor.mark_alive)
SLEEPS = ('/bin/sleep', '/usr/bin/gnusleep')  # GNU sleep under its own name where coreutils is uutils (Ubuntu 25.10+)


def live_marker(directory):
    """``<directory>/claude``: a link to the host's ``sleep`` that runs under that name, or None. The scripted
    supervisor runs it on its pane while it lives, so the daemon's check for an official CLI on that pane
    (RuntimeV2.cli_check: a process named ``claude`` on the pane's tty) sees the actor as the pane's live CLI. A
    link, never a copy (macOS kills a copied system binary); uutils' multicall sleep refuses another name."""
    directory.mkdir(mode=0o700)
    link = directory / 'claude'
    for target in SLEEPS:
        if not Path(target).exists():
            continue
        if link.is_symlink():
            link.unlink()
        link.symlink_to(target)
        try:
            if subprocess.run([str(link), '0'], capture_output=True, timeout=10).returncode == 0:
                return link
        except (OSError, subprocess.SubprocessError):
            pass
    if link.is_symlink():
        link.unlink()
    return None


class World:
    """The demo's directory and everything recorded about what it started."""

    def __init__(self, paths, turn_s=None):
        # ui.language (docs/design.md §63): the demo follows the caller's setting. Its world has its own HOME and no
        # FLEETMUX_* variable, so the language is resolved here, before the world exists, and handed down.
        from fleetmux import i18n
        self.lang = i18n.language()
        self.root = Path(tempfile.mkdtemp(prefix='fmxd-', dir='/tmp')).resolve()
        os.chmod(self.root, 0o700)
        self.paths = paths
        self.home, self.fh, self.state = self.root / 'h', self.root / 'fh', self.root / 's'
        self.redis_root, self.runs, self.bin = self.root / 'r', self.root / 'runs', self.root / 'bin'
        for d in (self.home, self.fh, self.state, self.redis_root, self.runs, self.bin):
            d.mkdir(mode=0o700)
        self.claude = self.bin / 'claude'
        self.claude.write_text('#!/bin/sh\n# fleetmux demo: the scripted actor, not Claude Code; no model is called\n'
                               'exec ' + shlex.quote(sys.executable) + ' -I ' + shlex.quote(str(ACTOR_PY)) + ' "$@"\n')
        self.claude.chmod(0o700)
        self.live = live_marker(self.bin / 'live')
        self.actor_log = self.root / 'actor.jsonl'
        self.turn_s = turn_s
        self.cfg_path = None
        self.cfg = {}
        self.view = None  # the viewer process this demo started (attach or gui)
        self.raising = None  # the thread that raises the window (macOS), not waited on

    def env(self):
        env = {k: v for k, v in os.environ.items() if k not in SCRUB and not k.startswith(SCRUB_PREFIX)}
        dirs = []
        for path in self.paths.values():
            d = os.path.dirname(path)
            if d not in dirs:
                dirs.append(d)
        env.update(HOME=str(self.home), FLEETMUX_HOME=str(self.fh), FLEETMUX_STATE=str(self.state),
                   FLEETMUX_DISCOVERY='off', FLEETMUX_BIN=self.paths['fleetmux'],
                   FLEETMUX_DEMO_ACTOR_LOG=str(self.actor_log), PATH=os.pathsep.join(dirs + [env.get('PATH', '')]))
        env[SWEEP_ENV] = str(self.redis_root)
        env['FLEETMUX_UI_LANGUAGE'] = self.lang  # the caller's ui.language (i18n.ENV)
        if self.live:
            env[LIVE_ENV] = str(self.live)
        if self.turn_s is not None:
            env[TURN_ENV] = str(self.turn_s)
        return env

    def fleet(self, *args, stdin=None, timeout=120):
        return subprocess.run([sys.executable, str(FLEET_PY)] + list(args), env=self.env(), cwd=str(self.root),
                              input=stdin, capture_output=True, text=True, timeout=timeout)

    def start(self):
        done = self.fleet('start', '--allow-development-v2', '--mode', 'task', '--lifecycle', 'smoke', '--backend', 'fleetmux', '--no-view',
                          '--no-join', '--workers', '2', '--supervisor-cli', 'claude', '--worker-cli', 'claude',
                          '--claude', str(self.claude), '--redis-server', self.paths['redis-server'],
                          '--redis-runtime-dir', str(self.redis_root), '--fleet-home', str(self.fh),
                          '--cmux-state-dir', str(self.state), '--runs-dir', str(self.runs), timeout=180)
        latest = self.fh / 'latest.json'
        if done.returncode or not latest.is_file():
            raise RuntimeError('start failed (exit ' + str(done.returncode) + '):\n' + (done.stdout + done.stderr)[-3000:])
        self.cfg = json.loads(latest.read_text())
        self.cfg_path = Path(self.cfg['config'])
        return done

    def submit(self):
        spec = {'goal': GOAL_MARK + ' Write ' + SLUG + ' with a slugify(text) function and ' + TEST
                + ' with its test.', 'hours': 1,
                'artifacts': [{'path': SLUG, 'contains': ['def slugify']},
                              {'path': TEST, 'contains': ['def test_']}]}
        done = self.fleet('task', '--config', str(self.cfg_path), '-', stdin=json.dumps(spec))
        if done.returncode:
            raise RuntimeError('task submit failed (exit ' + str(done.returncode) + '):\n' + (done.stdout + done.stderr)[-3000:])
        return spec

    def redis(self):
        from fleetmux.fleet import connect
        return connect(self.cfg, socket_timeout=5)


# ----------------------------------------------------------------------------------------------- the audit
class Audit(threading.Thread):
    """Follows the run's audit stream (control_view.iter_events, the reader of `fleetmux logs`) and keeps every
    record; ``on_event`` sees each one as it arrives."""

    def __init__(self, cfg, on_event=None):
        super().__init__(daemon=True)
        self.cfg, self.on_event = cfg, on_event
        self.records, self.error = [], None
        self.stop_flag = threading.Event()

    def run(self):
        from fleetmux.control_view import iter_events
        after = '0-0'
        while not self.stop_flag.is_set():
            try:
                for rec in iter_events(self.cfg, after=after, follow=True, limit=200):
                    if rec.get('stream_id'):
                        after = rec['stream_id']
                    self.records.append(rec)
                    if self.on_event:
                        self.on_event(rec)
                    if self.stop_flag.is_set():
                        return
            except Exception as exc:  # the run's Redis went away (the run closed): stop following
                self.error = str(exc)[:300]
                return

    def verdicts(self):
        return [r for r in self.records if r.get('event') == 'acceptance.verdict']


def field(rec, name):
    """An audit record's field: control_view keeps known fields at the top level and the others under ``detail``."""
    for where in (rec, rec.get('detail'), rec.get('audit'), (rec.get('audit') or {}).get('detail')):
        if isinstance(where, dict) and where.get(name) is not None:
            return where[name]
    return None


def summary_lines(verdicts, started):
    lines = ['', 'Acceptance record (read from the audit stream, event acceptance.verdict):']
    if not verdicts:
        lines.append('  (no acceptance.verdict event was recorded)')
    for i, v in enumerate(verdicts, 1):
        at = float(field(v, 'at') or 0) / 1000.0
        ok = str(field(v, 'ok')).lower() == 'true'
        when = time.strftime('%H:%M:%S', time.localtime(at)) + (' (+%.1fs)' % (at - started) if at else '')
        line = '  %d. %s  %s  %s claimed done -> %s' % (i, when, field(v, 'mode'), field(v, 'claimed_by'),
                                                        'PASS' if ok else 'REJECTED')
        if v.get('stream_id'):  # the id of the audit record this line was read from
            line += '  (audit ' + str(v['stream_id']) + ')'
        if not ok:
            line += '\n     reason: ' + str(field(v, 'problem'))
        lines.append(line)
    return lines


# ----------------------------------------------------------------------------------------------- the view
def open_view(world, no_view):
    """Desktop (not ssh) with fleetmux-web: a window, in the background. Otherwise: attach in this terminal, in
    the foreground (the caller waits on it). --no-view: nothing."""
    if no_view:
        return None
    from fleetmux import fleet
    binary = world.paths['fleetmux']
    runtime = runtime_of(world)
    gui = [binary, 'gui', '--runtime', runtime]
    attach = [binary, 'attach', '--runtime', runtime]
    env = world.env()
    if fleet.window_here(binary):
        with open(world.root / 'gui.log', 'ab') as log:
            world.view = subprocess.Popen(gui, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env,
                                          start_new_session=True)
        world.view_kind = 'window'
        if sys.platform == 'darwin':  # start's own step: a background window opens behind. It waits for
            # fleetmux-web and retries osascript for seconds, so it runs beside the demo; a failure leaves the
            # window where it is.
            world.raising = threading.Thread(target=raise_window, args=(fleet.bring_window_to_front, world.view.pid),
                                             name='demo-raise-window', daemon=True)
            world.raising.start()
        say('Opened a window; the steps follow here.')
        return world.view
    if sys.stdin.isatty() and sys.stdout.isatty():
        say('Attaching in this terminal; the view closes by itself when the run ends. Detach (C-b d) to stop early.')
        world.view = subprocess.Popen(attach, env=env)
        world.view_kind = 'attach'
        return world.view
    return None


def raise_window(front, pid):
    try:
        front(pid)
    except Exception:  # best effort, like start: the demo goes on with the window behind
        pass


def runtime_of(world):
    from fleetmux.fleet import manager_for
    saved = {k: os.environ.get(k) for k in ('FLEETMUX_STATE', 'FLEETMUX_HOME')}
    os.environ.update(FLEETMUX_STATE=str(world.state), FLEETMUX_HOME=str(world.fh))
    try:
        return str(manager_for(world.cfg).runtime_root)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def wait_view(world, limit=15):
    proc = world.view
    if proc is not None:
        try:
            proc.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            close_view(world)


def close_view(world):
    """Close the viewer this demo started. A window (`fleetmux gui`, started in its own session) goes with its
    process group, so its `fleetmux-web` child closes too; an attach client is this process's child."""
    proc = world.view
    if proc is None or proc.poll() is not None:
        return
    window = getattr(world, 'view_kind', '') == 'window'
    try:
        os.killpg(proc.pid, signal.SIGTERM) if window else proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL) if window else proc.kill()
        proc.wait(timeout=5)
    except ProcessLookupError:
        pass


# ----------------------------------------------------------------------------------------------- clean-up
def daemon_alive(world):
    from fleetmux.fleet import daemon_running
    return bool(world.cfg.get('run')) and daemon_running(world.cfg['run'])


def found_config(world):
    """The run's config: what start reported, else what start already wrote under this demo's runs/ (a Ctrl-C or a
    failure in the middle of start)."""
    if world.cfg.get('run'):
        return world.cfg
    for path in sorted(world.runs.glob('*/config.json')):
        try:
            cfg = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        world.cfg, world.cfg_path = dict(cfg, config=str(path)), path
        return world.cfg
    return {}


def cleanup(world, keep=False):
    """Stop what this run started, then remove its directory (kept with --keep). Every target is found inside the
    demo's own directory: its daemon (through `stop`), its Redis (its socket), its terminal server (its control
    socket, else `fleetmux reap` of its runtime directory). Returns what is still running."""
    close_view(world)
    found_config(world)
    if world.cfg_path and daemon_alive(world):
        try:
            world.fleet('stop', '--config', str(world.cfg_path), timeout=60)
        except (OSError, subprocess.SubprocessError):
            pass
        deadline = time.time() + 30
        while daemon_alive(world) and time.time() < deadline:
            time.sleep(0.3)
    deadline = time.time() + 30  # the product's own detached closer (lifecycle-close) finishes by itself
    while any('lifecycle-close' in line for line in leftovers(world)) and time.time() < deadline:
        time.sleep(0.3)
    for runtime in sorted((world.state / 'fleetmux').glob('*')):  # its panes: daemon, agents, their hooks
        stop_terminal_server(world, runtime)
    for sock in sorted(world.redis_root.glob('*/redis.sock')):
        shutdown_redis(sock)
    deadline = time.time() + 10
    left = leftovers(world)
    while left and time.time() < deadline:
        time.sleep(0.3)
        left = leftovers(world)
    if not keep:
        shutil.rmtree(world.root, ignore_errors=True)
    return left


def shutdown_redis(sock):
    """SHUTDOWN NOSAVE on this demo's own Redis socket, one attempt (a stale socket is refused at once; the
    client library's connection retries would only add seconds)."""
    import socket
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(5)
            s.connect(str(sock))
            s.sendall(b'*2\r\n$8\r\nSHUTDOWN\r\n$6\r\nNOSAVE\r\n')
            while s.recv(4096):  # Redis closes the connection when it exits
                pass
    except OSError:
        pass


def stop_terminal_server(world, runtime):
    """One fleetmux server of this demo (``runtime`` = its runtime directory): see ``stop_server``."""
    stop_server(Path(runtime), world.paths['fleetmux'], world.env())


def stop_server(runtime, binary, env=None):
    """One fleetmux server (``runtime`` = its runtime directory): `server.shutdown` with ``force`` through its own
    control socket (every pane's process group is killed, PROTOCOL.md §4.16); without an answering socket,
    `<binary> reap`, which signals only the process groups that server recorded (start time checked). Shared by the
    demo's clean-up and `fleetmux upgrade` (fleetmux/upgrade.py)."""
    runtime = Path(runtime)
    sock = runtime / 'ctl.sock'
    if sock.exists():
        import socket
        try:
            with socket.socket(socket.AF_UNIX) as s:
                s.settimeout(30)
                s.connect(str(sock))
                f = s.makefile('rwb')
                for req in ({'id': 1, 'cmd': 'hello'}, {'id': 2, 'cmd': 'server.shutdown', 'force': True}):
                    f.write((json.dumps(req) + '\n').encode())
                    f.flush()
                    f.readline()
        except OSError:
            pass
        deadline = time.time() + 10
        while sock.exists() and time.time() < deadline:
            time.sleep(0.2)
    if (runtime / 'panes.json').exists():
        try:
            subprocess.run([binary, 'reap', '--runtime', str(runtime)], env=env, capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass


def leftovers(world):
    """This user's processes whose command line names the demo's directory (unique: mkdtemp). Reported, never
    signalled here: what is left is a bug of the clean-up above."""
    try:
        out = subprocess.run(['ps', '-U', str(os.getuid()), '-o', 'pid=,command='], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    mine = {os.getpid()}
    return [line.strip() for line in out.splitlines()
            if str(world.root) in line and int(line.split(None, 1)[0]) not in mine]


# ----------------------------------------------------------------------------------------------- main
def run(keep=False, no_view=False, turn_s=None, out=say):
    started = time.time()
    rows, paths, missing = check_dependencies()
    if missing:
        out('fleetmux demo needs these programs (nothing is installed by fleetmux):')
        for row in rows:
            out('  ' + row)
        return 1
    out('=' * len(BANNER))
    out(BANNER)
    out('=' * len(BANNER))
    world = World(paths, turn_s if turn_s is not None else (None if no_view else 2.0))
    interrupted = []

    def on_signal(sig, _frame):
        interrupted.append(sig)
        raise KeyboardInterrupt

    old = {s: signal.signal(s, on_signal) for s in (signal.SIGINT, signal.SIGTERM)}
    audit = None
    code = 1
    try:
        out('Starting a one-off fleet in ' + str(world.root) + ' (1 supervisor + 2 workers, all scripted) ...')
        world.start()
        # the steps go to this terminal unless the fleet's panes are attached in it
        audit = Audit(world.cfg, on_event=lambda rec: narrate(
            rec, out, getattr(world, 'view_kind', '') != 'attach', started))
        audit.start()
        spec = world.submit()
        out('Task: ' + spec['goal'].replace(GOAL_MARK + ' ', ''))
        out('Declared artifacts: ' + ', '.join(a['path'] for a in spec['artifacts']))
        view = open_view(world, no_view)
        status = wait_for_end(world, view, started)
        ended = time.time()
        attached = getattr(world, 'view_kind', '') == 'attach'
        if attached:  # the run closes itself; its server goes and the attach client exits with it (exit 0)
            wait_closed(world)
            wait_view(world)
        verdicts = settle_audit(audit, status)
        for line in summary_lines(verdicts, started):
            out(line)
        if audit.error and not verdicts:
            out('  (audit stream: ' + audit.error + ')')
        out('')
        out('Task status: ' + status + ' after %.1f s.' % (ended - started))
        passed = bool(verdicts) and str(field(verdicts[-1], 'ok')).lower() == 'true'
        code = 0 if status == 'completed' and passed else 1
        if not attached:
            wait_closed(world)
    except KeyboardInterrupt:
        out('\nInterrupted: cleaning up.')
        code = 130
    except Exception as exc:  # noqa: BLE001  (one line for the person; clean-up below runs either way)
        out('fleetmux demo failed: ' + (str(exc) or type(exc).__name__))
        code = 1
    finally:
        if audit:
            audit.stop_flag.set()
            try:
                with open(world.root / 'audit.jsonl', 'w', encoding='utf-8') as fh:
                    fh.writelines(json.dumps(r, ensure_ascii=False) + '\n' for r in audit.records)
            except OSError:
                pass
        for s in old:  # a second Ctrl-C must not cut the clean-up short (every wait in it is bounded)
            signal.signal(s, signal.SIG_IGN)
        try:
            left = cleanup(world, keep)
        finally:
            for s, h in old.items():
                signal.signal(s, h)
        if left:
            out('Left running (report this): ' + '; '.join(left))
            code = code or 1
        out(('Kept for inspection: ' + str(world.root)) if keep else 'Cleaned up: processes stopped, '
            + str(world.root) + ' removed.')
        out(BANNER)
    return code


def narrate(rec, out, show, started=None):
    """One line per step that matters, from the audit stream, when ``show`` (the fleet's panes are not attached in
    this terminal): what the scripted agents said (``committed`` replies) and what the program judged
    (``acceptance.verdict``)."""
    if not show:
        return
    ev, slot = rec.get('event'), field(rec, 'slot')
    at = field(rec, 'at')
    when = ('+%5.1fs ' % (float(at) / 1000.0 - started)) if at and started else ''
    if ev == 'acceptance.verdict':
        ok = str(field(rec, 'ok')).lower() == 'true'
        out('  ' + when + '[fleetmux]   checks the claim: '
            + ('PASS' if ok else 'REJECTED, ' + str(field(rec, 'problem'))))
    elif ev == 'committed' and slot:
        detail = str(field(rec, 'detail') or '')
        said = re.search(r"'instruction': '(.*?)(?:'|$)", detail)
        if slot == 'supervisor' and "'status': 'COMPLETED'" in detail:
            out('  ' + when + '[scripted]   supervisor claims COMPLETED')
        elif slot == 'supervisor' and said:
            out('  ' + when + '[scripted]   supervisor -> worker-1: ' + said.group(1).replace('[scripted] ', ''))
        elif slot != 'supervisor':
            done = re.search(r"'done': \['(.*?)'", detail)
            out('  ' + when + '[scripted]   ' + slot + ' reports: '
                + (done.group(1).replace('[scripted ' + slot + '] ', '') if done else 'done'))
    elif ev in ('task.completed', 'task.blocked', 'task.failed'):
        out('  ' + when + '[fleetmux]   task ' + ev.split('.', 1)[1])


def settle_audit(audit, status, limit=5.0):
    """The verdicts once the audit thread has caught up with the end of the task: a completed task ends with a
    passing verdict, so wait (bounded) for that one to arrive before the summary is printed."""
    deadline = time.time() + limit
    while time.time() < deadline and audit.is_alive():
        got = audit.verdicts()
        if got and (status != 'completed' or str(field(got[-1], 'ok')).lower() == 'true'):
            break
        time.sleep(0.1)
    audit.stop_flag.set()
    return audit.verdicts()


def wait_closed(world, limit=40):
    """The run closes itself when its task ends (smoke lifecycle: stop_and_close): the daemon stops, then its
    detached closer (lifecycle-close) closes the panes and the terminal server and marks result.json
    ``workspace: closed``. Wait for that, so clean-up finds nothing of the product's own path still running."""
    result = Path(world.cfg['run']) / 'result.json'
    deadline = time.time() + limit
    while time.time() < deadline:
        try:
            if json.loads(result.read_text()).get('workspace') == 'closed' and not daemon_alive(world):
                return True
        except (OSError, ValueError):
            pass
        time.sleep(0.3)
    return False


ENDED = ('completed', 'blocked', 'failed', 'cancelled', 'budget_exhausted')


def task_status(world, r):
    """The task's status from Redis; once the run has closed itself (its Redis is gone), from its result.json."""
    try:
        return r.hget('fx:task', 'status') or ''
    except Exception:  # noqa: BLE001  (the run closed: Redis shut down with it)
        try:
            return json.loads((Path(world.cfg['run']) / 'result.json').read_text()).get('status') or 'closed'
        except (OSError, ValueError):
            return 'closed'


def wait_for_end(world, view, started):
    r = world.redis()
    deadline = started + time_limit(world.turn_s)
    while time.time() < deadline:
        status = task_status(world, r)
        if status in ENDED or status == 'closed':
            return status
        if view is not None and getattr(world, 'view_kind', '') == 'attach' and view.poll() is not None:
            if task_status(world, r) in ENDED:
                return task_status(world, r)
            raise KeyboardInterrupt  # the person detached: end the demo
        time.sleep(0.3)
    return 'timed out after %d s' % time_limit(world.turn_s)


def time_limit(turn_s):
    return max(TIME_LIMIT_S, TURNS * float(turn_s or 0) + 60)


def main(argv=None):
    p = argparse.ArgumentParser(prog='fleetmux demo', description='Watch fleetmux reject a false "done" and accept '
                                'the fixed work. ' + BANNER + '.')
    p.add_argument('--keep', action='store_true', help='keep the demo directory for inspection')
    p.add_argument('--no-view', action='store_true', help='do not open a window or attach; print the steps')
    a = p.parse_args(argv)
    turn = os.environ.get(TURN_ENV)
    return run(keep=a.keep, no_view=a.no_view, turn_s=float(turn) if turn else None)


if __name__ == '__main__':
    sys.exit(main())
