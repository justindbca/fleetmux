"""Companion tabs (docs/design.md §31): roster ``tabs:`` programs opened as tabs after control.

The fleet that loads the roster opens them once per daemon start (start, start --resume, a named sub-fleet's
``daemon --resume``), on the first tick after the roster is accepted: in its own workspace, in the pane of
history / control, after control (roster order), ``--focus false``, titled with their name. They are surfaces of
this fleet's cmux registry (``type: tab``), so stop / close_all close them too; they are no agent. Nothing
watches them afterwards: a tab closed while the daemon runs stays closed until the fleet is started again.

Before a tab opens, every process whose command line is that tab's program is stopped (SIGTERM, up to
KILL_WAIT s, then SIGKILL) so a restart never runs the program twice. Matching (``same_program``) is exact:
the ``ps`` command line must equal the tab's argv (``shlex.split(command)``, joined by spaces), except that the
first word may also be argv[0]'s resolved path (``which`` + ``realpath``), or - for a macOS framework Python -
that interpreter's ``Python.app/Contents/MacOS/Python`` stub of the same ``Versions/X.Y`` directory (framework
Python re-execs itself there, so ``ps`` shows the stub as argv[0]). Every other word must be identical.

Internal API
  open_all(fleet)   Fleet.tick: once per daemon, after the roster is accepted
  stop_all(fleet)   explicit stop tail: close every tab of this registry; True when all are gone
"""
import os
import re
import shlex
import shutil
import signal
import subprocess
import time

KILL_WAIT = 5.0


def heads(argv0):
    """The first words a process running ``argv0`` may show in ``ps``."""
    out = {argv0}
    path = shutil.which(argv0)
    if path:
        real = os.path.realpath(path)
        out |= {path, real}
        m = re.fullmatch(r'(.*/Python\.framework/Versions/[^/]+)/bin/python[0-9.]*', real)
        if m:
            out.add(m.group(1) + '/Resources/Python.app/Contents/MacOS/Python')
    return out


def same_program(line, argv, first=None):
    """Is the ``ps`` command ``line`` exactly the program ``argv``? (rule in the module docstring)"""
    rest = ''.join(' ' + w for w in argv[1:])
    return any(line == h + rest for h in (first if first is not None else heads(argv[0])))


def matching(argv):
    """PIDs (not ours) whose command line is exactly ``argv``; raises when ``ps`` fails."""
    p = subprocess.run(['ps', '-axww', '-o', 'pid=,command='], capture_output=True, text=True, timeout=20)
    if p.returncode:
        raise RuntimeError('ps failed: ' + (p.stderr or '').strip()[:200])
    first, found = heads(argv[0]), set()
    for line in p.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit() and same_program(parts[1], argv, first):
            found.add(int(parts[0]))
    return found - {os.getpid()}


def signal_all(pids, sig):
    for pid in pids:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass


def wait_gone(argv, pids, timeout):
    deadline = time.monotonic() + timeout
    left = matching(argv) & pids
    while left and time.monotonic() < deadline:
        time.sleep(.2)
        left = matching(argv) & pids
    return left


def clear(f, spec):
    """Stop every running copy of this tab's program; True when none is left."""
    argv = shlex.split(spec['command'])
    pids = matching(argv)
    if not pids:
        return True
    signal_all(pids, signal.SIGTERM)
    left = wait_gone(argv, pids, KILL_WAIT)
    if left:
        signal_all(left, signal.SIGKILL)
        left = wait_gone(argv, left, 2.0)
    f.log('tab.cleared', level='error' if left else 'info', tab=spec['name'], pids=sorted(pids),
          killed=sorted(pids - left), left=sorted(left))
    return not left


def launch(spec):
    env = ' '.join(shlex.quote(k + '=' + v) for k, v in sorted(spec['env'].items()))
    return ('env ' + env + ' ' if env else '') + spec['command']  # spawn_window runs ``exec <this>``


def open_all(f):
    if f.tab_specs is None or f.tabs_opened:
        return
    f.tabs_opened = True
    from cmux_fleet.fleet import SERVICE
    for sid, name in f.mgr.tab_panes().items():  # a previous daemon's tab still shown: close it first
        f.mgr.kill_pane(sid)
    history, prev = f.r.hget(SERVICE, 'history_pane'), None
    for spec in f.tab_specs:
        name = spec['name']
        try:
            ok = clear(f, spec)
        except Exception as exc:  # ps failed: fail visibly, never open a possible second copy
            f.log('tab.start_failed', level='error', tab=name, error='process scan: ' + str(exc))
            continue
        if not ok:
            f.log('tab.start_failed', level='error', tab=name, error='old process still running')
            continue
        res = f.mgr.spawn_window(name, launch(spec), metadata={'type': 'tab', 'tab': name},
                                 start_directory=spec['cwd'])
        if not res.success:
            f.log('tab.start_failed', level='error', tab=name, error=res.error or 'no surface')
            continue
        if history and not f.mgr.dock_tab(res.pane_id, history, after=prev):
            f.log('tab.dock_failed', level='warn', tab=name, pane=res.pane_id)
        prev = res.pane_id
        f.log('tab.started', tab=name, pane=res.pane_id, command=spec['command'], cwd=spec['cwd'])


def stop_all(f):
    """Explicit stop: close every tab of this registry; True when all are gone."""
    f.tab_specs = None
    ok = True
    for sid, name in f.mgr.tab_panes().items():
        gone = f.mgr.kill_pane(sid) or f.mgr.get_pane_status(sid) is None
        f.log('tab.stopped', level='info' if gone else 'error', tab=name, pane=sid, confirmed=gone)
        ok = ok and gone
    return ok
