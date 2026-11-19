"""Bare `fleetmux` (= `fleetmux gui`) shows this machine's root fleet window (docs/design.md section 50).

The Rust binary hands a bare `fleetmux` to the Python `gui`; `fleet.gui()` calls `open_root_window` first, then,
with no root and no fleet of its HOME's own, `start_bare_root`, and keeps its history window for the rest. This is
the only place that finds the root and opens its window for a person at a terminal; it reuses the existing pieces
and adds no process scan or launcher of its own:

* the root: `join.find_root` (a service root first), its runtime from `fleet.manager_for` on its config.json;
* no root: the fleet.yaml#bare root fleet, started (or resumed) by `start` in a child (`fleet.run_start`), taken
  from its fleet home's latest.json;
* no desktop, no `fleetmux-web`, or an ssh session (`fleet.window_here`, the rule `start`'s auto view uses): exec
  `fleetmux attach --runtime <runtime>`, also when a window is open (the root daemon keeps one open on the
  desktop, which a person over ssh does not see);
* a host terminal (`in_host_terminal`: a terminal that is itself a pane of another program, marked by one of the
  variables in fleet.yaml#bare.host_terminal_env, with stdin and stdout at that terminal and not inside a fleetmux
  pane): the same attach, also when a window is open; `fleet.window_here` and `start`'s auto view are unchanged;
* an open window: `gui_keep.gui_running` (the `fleetmux gui --runtime <runtime>` pid), raised with
  `fleet.bring_window_to_front` on macOS; nothing new is started;
* a root just started: its daemon's gui_keep opens the window; bare looks for it every `bare.window_poll_s` up to
  `bare.window_wait_s` and raises it. When the wait runs out (the daemon is still in start_all or a resume's
  replay) it falls back once to the steps below (an open window first, else a new one) and returns; with
  `gui.keep_open: false` no daemon opens it, so there is no wait;
* no window: `gui_keep.spawn` starts `fleetmux gui --runtime <runtime>` in the background (output to
  `<run>/gui.log`), raised on macOS, one line, and the command returns.

Unlike `start`'s auto view it also opens from inside a fleetmux pane and without a terminal: the person typed the
command to get the window.
"""
import os
import sys
import time
from pathlib import Path

from fleetmux import fleet_config, gui_keep, join


def open_root_window(home, live=None):
    """Show the window of this machine's live root fleet. ``home`` is this HOME (it is searched too); ``live`` is
    the run this HOME's own daemon runs, if any: then the root is used only when it is that run.

    Returns None when there is no such root (the caller keeps its own path), else 0 after raising or opening the
    window; with no desktop it execs `fleetmux attach` and does not return.
    """
    root = join.find_root(home)
    if root is None:
        return None
    if live is not None and os.path.realpath(str(live)) != os.path.realpath(root['run']):
        return None
    return show(root)


def start_bare_root():
    """No root fleet runs: start the fleet.yaml#bare one (``start --resume auto`` in its fleet home), then show its
    window. Returns None when the block is off (``enabled: false``), else 0; a failed start exits with its code."""
    from fleetmux import fleet
    try:
        s = fleet_config.bare()
    except (OSError, ValueError) as exc:
        raise SystemExit('fleetmux: fleet.yaml#bare: ' + str(exc)) from exc
    if not s['enabled']:
        return None
    from fleetmux.backends.fleetmux import fleetmux_bin
    if fleet.window_here(fleetmux_bin()):
        host_terminal_env()  # a bad host list is refused before the start, where show() would read it
    home = Path(s['home'])
    print('No root fleet is running on this machine; starting ' + s['name'] + ' (' + s['lifecycle']
          + ' lifecycle, fleet home ' + str(home) + '; a resumable earlier run there is resumed)')
    sys.stdout.flush()
    argv = ['--lifecycle', s['lifecycle'], '--name', s['name'], '--resume', 'auto', '--no-join']
    if s['roster'] is not None:
        argv += ['--roster', s['roster']]
    if s['workers'] is not None:
        argv += ['--workers', str(s['workers'])]
    argv += s['start_args']
    env = dict(os.environ, FLEETMUX_HOME=str(home), FLEETMUX_STATE=str(home / 'state'))
    code = fleet.run_start(argv, env)
    if code:
        raise SystemExit(code)
    root = home_root(home)
    if root is None:
        raise SystemExit('fleetmux: start finished, but ' + str(home / 'latest.json') + ' names no run')
    return show(root, wait=s)


def home_root(home):
    """The run ``<home>/latest.json`` names, as a root dict (config, home, run, name), or None. Right after `start`
    its daemon may not hold its lock or answer yet, so this does not ask `join.find_root`."""
    from fleetmux import fleet
    try:
        latest = fleet.load(Path(home) / 'latest.json')
    except (OSError, ValueError):
        return None
    if not isinstance(latest, dict) or join.is_stub(latest) or not latest.get('run'):
        return None
    run = Path(latest['run'])
    return {'config': str(latest.get('config') or run / 'config.json'), 'home': str(home), 'run': str(run),
            'name': latest.get('name')}


def show(root, wait=None):
    """Bring the window of ``root`` (a `join.find_root` / `home_root` dict) to the person: attach without a window
    here, raise an open one, or open one. ``wait`` is the bare settings when bare just started the root: then the
    daemon's window is waited for first (at most ``window_wait_s``). 0, or no return (exec)."""
    from fleetmux import fleet
    from fleetmux.backends.fleetmux import fleetmux_bin
    runtime = str(fleet.manager_for(fleet.load(root['config'])).runtime_root)
    found = fleetmux_bin()
    binary = found or 'fleetmux'
    # over ssh the daemon's window is open on a desktop nobody here sees; in a host terminal the view goes into it.
    # The host list is read only where a window could open (a bad one is refused there, before any raise / spawn /
    # exec); without a window here this is the attach path as before, whatever the list holds.
    if not fleet.window_here(found) or in_host_terminal(host_terminal_env()):
        attach = [binary, 'attach', '--runtime', runtime]
        sys.stdout.flush()
        os.execvp(attach[0], attach)
        return 0  # only reached when execvp is stubbed
    name = str(root.get('name') or root['run'])
    if wait is not None and daemon_opens_window():
        pid = wait_for_window(runtime, wait['window_wait_s'], wait['window_poll_s'])
        if pid:
            raised = sys.platform == 'darwin' and fleet.bring_window_to_front(pid)
            print('The window of ' + name + ' is open' + ('; brought it to the front' if raised else '')
                  + ' (runtime: ' + runtime + ')')
            return 0
        print('The daemon of ' + name + ' opened no window within ' + str(wait['window_wait_s'])
              + ' s; opening it here')
    pid = gui_keep.gui_running(runtime)
    if pid:
        raised = sys.platform == 'darwin' and fleet.bring_window_to_front(pid)
        print('The window of ' + name + ' is already open' + ('; brought it to the front' if raised else '')
              + ' (runtime: ' + runtime + ')')
        return 0
    log = Path(root['run']) / 'gui.log'
    proc = gui_keep.spawn([binary, 'gui', '--runtime', runtime], log, None)
    if sys.platform == 'darwin':
        fleet.bring_window_to_front(proc.pid)
    print('Opened a window (log: ' + str(log) + ')')
    return 0


def host_terminal_env():
    """fleet.yaml#bare.host_terminal_env, validated; a bad value ends the command before it acts."""
    try:
        return fleet_config.host_terminal_env()
    except (OSError, ValueError) as exc:
        raise SystemExit('fleetmux: fleet.yaml#bare: ' + str(exc)) from exc


def in_host_terminal(names=None):
    """A person at a host terminal: a terminal that is itself a pane of another program (one of ``names``, default
    fleet.yaml#bare.host_terminal_env, is set and non-empty), stdin and stdout at that terminal, and not inside a
    fleetmux pane (panes inherit the variable from a fleet started there). Cheapest check first."""
    from fleetmux import fleet
    if names is None:
        names = host_terminal_env()
    return bool(any(os.environ.get(n) for n in names) and fleet.interactive_terminal()
                and not fleet.inside_fleetmux_pane())


def daemon_opens_window():
    """fleet.yaml#gui.keep_open: the root daemon opens its own window (gui_keep)."""
    try:
        return bool(gui_keep.settings()['keep_open'])
    except (OSError, ValueError) as exc:
        raise SystemExit('fleetmux: ' + str(exc)) from exc


def wait_for_window(runtime, wait_s, poll_s):
    """The pid of `fleetmux gui --runtime <runtime>` once the daemon has started it, or None after ``wait_s`` (a
    look at once, then one every ``poll_s``; never longer than ``wait_s``)."""
    deadline = time.monotonic() + wait_s
    while True:
        pid = gui_keep.gui_running(runtime)
        if pid:
            return pid
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        time.sleep(min(poll_s, left))
