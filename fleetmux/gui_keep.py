"""The root fleet daemon keeps its `fleetmux gui` window open on the desktop (design.md "Window keep-open").

Every ``gui.check_s`` seconds (fleet.yaml#gui) a root fleet daemon on the fleetmux backend checks for a
``fleetmux gui --runtime <its runtime>`` process. The process is the window: it exits when the window is closed.
When there is none, the daemon starts one in the background (new session, stdin from /dev/null, output to
``<run>/gui.log``), the same command ``start`` uses. This works the same when the fleet was started over ssh or
by a scheduler, so no launchd / cron item is needed to open or reopen the window.

* macOS: a window opens on the desktop of the user at the console, also from an ssh-started process. Nobody
  logged in at the console (``/dev/console`` owned by root) or another user there: no window, logged once.
* Linux: the daemon's own ``DISPLAY`` / ``WAYLAND_DISPLAY``; without them, the user's Wayland socket in
  ``$XDG_RUNTIME_DIR`` (else ``/run/user/<uid>``). Neither: no window, logged once.
* One window: a check finds the window (any ``fleetmux gui`` on the same runtime, compared by real path) or the
  window this daemon launched and is still running; then nothing is opened.
* A launch that fails (exec error, or ``fleetmux gui`` exiting non-zero, e.g. 4 = cannot open the window) is
  logged and the next try waits ``check_s``, then twice as long each time, at most ``backoff_max_s``.
* Sub-fleets never open a window (they show in the root's sidebar); ``gui.keep_open: false`` and a run started
  with ``start --no-view`` (config.json ``gui_keep_open: false``) open nothing.

The window is not raised: a reopen never takes the focus from what the person at the desktop is doing.
"""
import os
import subprocess
import sys
import time
from pathlib import Path

from fleetmux import fleet_config

DEFAULTS = {'keep_open': True, 'check_s': 30, 'backoff_max_s': 600}


def settings():
    """fleet.yaml#gui over DEFAULTS; read on every check, so a change needs no daemon restart."""
    knobs = dict(DEFAULTS, **(fleet_config.block('gui') or {}))
    for key in ('check_s', 'backoff_max_s'):
        if type(knobs[key]) not in (int, float) or knobs[key] <= 0:
            raise ValueError('fleet.yaml#gui.' + key + ' must be seconds > 0, got ' + repr(knobs[key]))
    return knobs


def gui_running(runtime):
    """The pid of a running `fleetmux gui --runtime <runtime>` (the window; an `attach` is a terminal), else None.
    The runtime is compared by real path: a migrated home is often reached through a symlink. The pid lets bare
    `fleetmux` raise that window (fleetmux/open_window.py)."""
    try:
        out = subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    want = os.path.realpath(str(runtime))
    for line in out.splitlines():
        pid, *argv = line.split()[:5] or ['']
        if (len(argv) > 3 and pid.isdigit() and Path(argv[0]).name == 'fleetmux' and argv[1] == 'gui'
                and argv[2] == '--runtime' and os.path.realpath(argv[3]) == want):
            return int(pid)
    return None


def desktop():
    """(reason, env): reason is why no window can open here (None: it can); env is what the window needs on top
    of the daemon's environment (None: nothing)."""
    if sys.platform == 'darwin':
        try:
            owner = os.stat('/dev/console').st_uid
        except OSError as exc:
            return 'cannot read the console owner: ' + str(exc), None
        if owner == os.getuid():
            return None, None
        return ('nobody is logged in at the desktop' if owner == 0
                else 'another user (uid ' + str(owner) + ') holds the desktop'), None
    if not sys.platform.startswith('linux'):
        return 'no desktop support on ' + sys.platform, None
    if os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'):
        return None, None
    rundir = Path(os.environ.get('XDG_RUNTIME_DIR') or '/run/user/' + str(os.getuid()))
    sockets = sorted(p.name for p in rundir.glob('wayland-*') if not p.name.endswith('.lock'))
    if sockets:
        return None, {'WAYLAND_DISPLAY': sockets[0], 'XDG_RUNTIME_DIR': str(rundir)}
    return 'no DISPLAY / WAYLAND_DISPLAY and no Wayland session in ' + str(rundir), None


def binaries():
    """(fleetmux, fleetmux-web) as `start` finds them; either may be None."""
    from fleetmux import fleet
    from fleetmux.backends.fleetmux import fleetmux_bin
    binary = fleetmux_bin()
    return binary, (fleet.fleetmux_web_bin(binary) if binary else None)


def spawn(argv, log, env):
    """Start the window in the background, detached from the daemon's session (as `start` does)."""
    with open(log, 'ab') as out:
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=out, start_new_session=True,
                                env=dict(os.environ, **env) if env else None)


def _note(f, st, kind, reason):
    """Log a condition that stops the window once, until it changes."""
    if st.get('noted') != (kind, reason):
        st['noted'] = (kind, reason)
        f.log(kind, reason=reason)


def _failed(f, st, now, knobs, reason):
    st['failures'] = st.get('failures', 0) + 1
    wait = min(knobs['check_s'] * 2 ** (st['failures'] - 1), knobs['backoff_max_s'])
    st['next_at'] = now + wait
    f.log('gui.open_failed', reason=reason, attempt=st['failures'], retry_in_s=wait)


def tick(f, now=None):
    """One daemon tick: reap the window this daemon launched, then check on the interval (module docstring)."""
    if f.cfg.get('parent_config') or f.cfg.get('gui_keep_open') is False:
        return
    from fleetmux import fleet
    if fleet.run_backend(f.cfg) != 'fleetmux':
        return
    now = time.monotonic() if now is None else now
    st = f.__dict__.setdefault('_gui_keep', {'next_at': 0})
    proc = st.get('proc')
    if proc is not None and proc.poll() is not None:  # also reaps it
        st['proc'] = None
        if proc.returncode == 0:  # the window was closed: reopen on the next check
            st['failures'] = 0
            f.log('gui.closed', pid=proc.pid)
        else:
            _failed(f, st, now, settings(), 'fleetmux gui exited ' + str(proc.returncode) + ' (see gui.log)')
    if now < st['next_at']:
        return
    knobs = settings()
    st['next_at'] = now + knobs['check_s']
    if not knobs['keep_open'] or st.get('proc') is not None:  # off, or our window is open / opening
        return
    runtime = f.mgr.runtime_root
    if gui_running(runtime):
        st['noted'] = None
        return
    reason, env = desktop()
    if reason:
        return _note(f, st, 'gui.no_desktop', reason)
    binary, web = binaries()
    if not binary or not web:
        return _note(f, st, 'gui.unavailable', 'fleetmux not found' if not binary else 'fleetmux-web not found')
    argv = [binary, 'gui', '--runtime', str(runtime)]
    try:
        st['proc'] = spawn(argv, Path(f.run_dir) / 'gui.log', env)
    except OSError as exc:
        return _failed(f, st, now, knobs, str(exc))
    st['noted'] = None
    f.log('gui.opened', pid=st['proc'].pid, runtime=str(runtime))
