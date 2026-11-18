"""The root fleet daemon keeps its `fleetmux gui` window open (fleetmux/gui_keep.py, design.md "Window keep-open").

No test opens a real window: the launcher (`gui_keep.spawn`), the window check (`gui_keep.gui_running`) and the
desktop check (`gui_keep.desktop`) are stubbed. conftest stubs the first two and the tick itself for every other test.
"""
import subprocess
from pathlib import Path

import pytest

import test_fleet  # noqa: F401  (puts the checkout on sys.path)
from fleetmux import fleet, fleet_config, gui_keep

REAL_TICK = gui_keep.tick  # taken at import, before conftest's per-test stubs
REAL_GUI_RUNNING = gui_keep.gui_running
REAL_DESKTOP = gui_keep.desktop


class Mgr:
    def __init__(self, runtime):
        self.runtime_root = Path(runtime)


class F:
    """The parts of a Fleet that gui_keep.tick reads."""

    def __init__(self, tmp_path, **cfg):
        self.cfg = dict({'backend': 'fleetmux', 'run': str(tmp_path)}, **cfg)
        self.run_dir = Path(tmp_path)
        self.mgr = Mgr(tmp_path / 'rt')
        self.logs = []

    def log(self, kind, **data):
        self.logs.append((kind, data))

    def kinds(self):
        return [k for k, _ in self.logs]


class Proc:
    """A launched `fleetmux gui`: running until a test sets ``code``."""
    pid = 4242

    def __init__(self):
        self.code = None

    def poll(self):
        return self.code

    @property
    def returncode(self):
        return self.code


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A desktop, binaries present, no window open; spawn records its calls."""
    state = {'open': False, 'desktop': (None, None), 'launched': [], 'knobs': {}}

    def spawn(argv, log, env):
        proc = Proc()
        state['launched'].append((argv, Path(log), env, proc))
        return proc
    monkeypatch.setattr(gui_keep, 'spawn', spawn)
    monkeypatch.setattr(gui_keep, 'gui_running', lambda runtime: state['open'])
    monkeypatch.setattr(gui_keep, 'desktop', lambda: state['desktop'])
    monkeypatch.setattr(gui_keep, 'binaries', lambda: ('/opt/fmx/fleetmux', '/opt/fmx/fleetmux-web'))
    monkeypatch.setattr(gui_keep, 'settings', lambda: dict(gui_keep.DEFAULTS, **state['knobs']))
    state['f'] = F(tmp_path)
    return state


def test_window_missing_opens_it_once(world):
    f = world['f']
    REAL_TICK(f, now=0)
    assert len(world['launched']) == 1
    argv, log, env, _ = world['launched'][0]
    assert argv == ['/opt/fmx/fleetmux', 'gui', '--runtime', str(f.mgr.runtime_root)]
    assert log == f.run_dir / 'gui.log' and env is None
    assert f.kinds() == ['gui.opened']


def test_window_present_opens_nothing(world):
    world['open'] = True
    REAL_TICK(world['f'], now=0)
    REAL_TICK(world['f'], now=1000)
    assert world['launched'] == [] and world['f'].logs == []


def test_two_checks_while_opening_open_no_second_window(world):
    f = world['f']
    REAL_TICK(f, now=0)
    REAL_TICK(f, now=0.5)  # same check interval
    REAL_TICK(f, now=31)  # next check: the window is not listed yet, but our launch is still running
    assert len(world['launched']) == 1


def test_checks_once_per_interval(world):
    f = world['f']
    REAL_TICK(f, now=0)
    world['launched'][0][3].code = 0  # the operator closed the window
    REAL_TICK(f, now=10)  # reaped, but the next check is not due
    assert len(world['launched']) == 1 and 'gui.closed' in f.kinds()
    REAL_TICK(f, now=30)
    assert len(world['launched']) == 2  # reopened on the next check


def test_started_without_a_terminal_first_tick_opens_it(world, monkeypatch):
    """A fleet started over ssh or by a scheduler: no TTY, SSH_CONNECTION set. The daemon does not care."""
    monkeypatch.setenv('SSH_CONNECTION', '10.0.0.1 50000 10.0.0.2 22')
    monkeypatch.setattr(fleet, 'interactive_terminal', lambda: False)
    REAL_TICK(world['f'], now=0)
    assert len(world['launched']) == 1


def test_no_desktop_logged_once_retried_after_interval(world):
    f = world['f']
    world['desktop'] = ('nobody is logged in at the desktop', None)
    for now in (0, 1, 2, 29):
        REAL_TICK(f, now=now)
    assert world['launched'] == [] and f.kinds() == ['gui.no_desktop']
    REAL_TICK(f, now=30)  # retried after the interval: still nobody, still one log line
    REAL_TICK(f, now=60)
    assert f.kinds() == ['gui.no_desktop']
    world['desktop'] = (None, None)  # somebody logged in
    REAL_TICK(f, now=90)
    assert len(world['launched']) == 1 and f.kinds() == ['gui.no_desktop', 'gui.opened']


def test_desktop_check_runs_once_per_interval_not_per_tick(world, monkeypatch):
    calls = []
    monkeypatch.setattr(gui_keep, 'desktop', lambda: calls.append(1) or ('no desktop', None))
    for now in range(0, 30):
        REAL_TICK(world['f'], now=now)
    assert len(calls) == 1


def test_failed_opens_back_off_and_are_logged(world):
    f = world['f']
    starts = []
    now = 0
    for _ in range(400):
        REAL_TICK(f, now=now)
        if len(world['launched']) > len(starts):
            starts.append(now)
            world['launched'][-1][3].code = 4  # fleetmux gui: cannot open the window
        now += 1
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert starts[0] == 0 and gaps == sorted(gaps) and all(g >= 30 for g in gaps)
    assert f.kinds().count('gui.open_failed') == len(starts)
    # bounded: the wait never exceeds backoff_max_s
    for _ in range(20000):
        REAL_TICK(f, now=now)
        if world['launched'][-1][3].code is None:
            starts.append(now)
            world['launched'][-1][3].code = 4
        now += 5
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert max(gaps) <= gui_keep.DEFAULTS['backoff_max_s'] + 5 and gaps[-1] >= gui_keep.DEFAULTS['backoff_max_s']


def test_spawn_error_counts_as_a_failure(world, monkeypatch):
    f = world['f']

    def broken(argv, log, env):
        raise OSError('exec format error')
    monkeypatch.setattr(gui_keep, 'spawn', broken)
    REAL_TICK(f, now=0)
    REAL_TICK(f, now=30)  # backoff: second failure waits 2 x 30 s from here
    REAL_TICK(f, now=60)
    assert f.kinds() == ['gui.open_failed', 'gui.open_failed']
    REAL_TICK(f, now=90)
    assert f.kinds().count('gui.open_failed') == 3


def test_success_after_failures_resets_the_backoff(world):
    f = world['f']
    REAL_TICK(f, now=0)
    world['launched'][-1][3].code = 4
    REAL_TICK(f, now=1)
    REAL_TICK(f, now=31)  # retry after 30 s, this one stays open
    assert len(world['launched']) == 2
    world['launched'][-1][3].code = 0  # later closed by the operator
    REAL_TICK(f, now=61)
    assert len(world['launched']) == 3  # back to the plain interval


def test_missing_fleetmux_web_logged_once(world, monkeypatch):
    f = world['f']
    monkeypatch.setattr(gui_keep, 'binaries', lambda: ('/opt/fmx/fleetmux', None))
    for now in (0, 30, 60):
        REAL_TICK(f, now=now)
    assert world['launched'] == [] and f.kinds() == ['gui.unavailable']


def test_sub_fleet_daemon_never_opens_a_window(world, tmp_path):
    f = F(tmp_path, parent_config=str(tmp_path / 'parent.json'))
    for now in (0, 30, 60):
        REAL_TICK(f, now=now)
    assert world['launched'] == [] and f.logs == []


def test_keep_open_false_never_opens(world):
    world['knobs'] = {'keep_open': False}
    for now in (0, 30, 60):
        REAL_TICK(world['f'], now=now)
    assert world['launched'] == [] and world['f'].logs == []
    world['knobs'] = {}  # read again on every check: turning it back on needs no restart
    REAL_TICK(world['f'], now=90)
    assert len(world['launched']) == 1


def test_start_no_view_pins_it_off_for_the_run(world, tmp_path):
    f = F(tmp_path, gui_keep_open=False)
    REAL_TICK(f, now=0)
    assert world['launched'] == []


def test_other_backends_open_nothing(world, tmp_path):
    f = F(tmp_path, backend='cmux')
    REAL_TICK(f, now=0)
    assert world['launched'] == []


def test_linux_display_env_is_passed_to_the_window(world):
    world['desktop'] = (None, {'WAYLAND_DISPLAY': 'wayland-0', 'XDG_RUNTIME_DIR': '/run/user/1000'})
    REAL_TICK(world['f'], now=0)
    assert world['launched'][0][2]['WAYLAND_DISPLAY'] == 'wayland-0'


# -- the real helpers, with the system calls stubbed --

def test_gui_running_matches_gui_only_and_compares_real_paths(tmp_path, monkeypatch):
    real = tmp_path / 'home' / 'state' / 'fleetmux' / 'fleetmux'
    real.mkdir(parents=True)
    link = tmp_path / 'old-home'
    link.symlink_to(tmp_path / 'home')
    spelled = str(link / 'state' / 'fleetmux' / 'fleetmux')
    asked = []

    def ps(lines):
        def run(argv, *a, **k):
            asked.append(argv)
            return subprocess.CompletedProcess(argv, 0, ''.join(line + '\n' for line in lines), '')
        monkeypatch.setattr(gui_keep.subprocess, 'run', run)
    ps(['  101 /usr/bin/ssh -t host fleetmux attach --runtime ' + str(real),
        '  102 /opt/fmx/fleetmux attach --runtime ' + str(real)])
    assert not REAL_GUI_RUNNING(real)  # a terminal viewer is not the desktop window
    ps(['  103 /opt/fmx/fleetmux gui --runtime ' + spelled])
    assert REAL_GUI_RUNNING(real)  # the launchd item's spelling of the same directory
    ps(['  104 /opt/fmx/fleetmux gui --runtime ' + str(tmp_path / 'other')])
    assert not REAL_GUI_RUNNING(real)
    assert asked[0] == ['ps', '-axo', 'pid=,command='], asked


def test_gui_running_returns_the_window_pid(tmp_path, monkeypatch):
    """Bare `fleetmux` raises the open window by the pid of its `fleetmux gui` (fleetmux/open_window.py)."""
    runtime = tmp_path / 'rt'
    runtime.mkdir()
    out = ''.join(line + '\n' for line in [
        '   55 /opt/fmx/fleetmux attach --runtime ' + str(runtime),
        '48805 /opt/fmx/fleetmux gui --runtime ' + str(runtime),
        'garbage',
        ''])
    monkeypatch.setattr(gui_keep.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 0, out, ''))
    assert REAL_GUI_RUNNING(runtime) == 48805
    assert REAL_GUI_RUNNING(tmp_path / 'none') is None


def test_macos_desktop_is_the_console_owner(monkeypatch):
    monkeypatch.setattr(gui_keep.sys, 'platform', 'darwin')
    monkeypatch.setattr(gui_keep.os, 'getuid', lambda: 501)
    monkeypatch.setattr(gui_keep.os, 'stat', lambda path: type('S', (), {'st_uid': 501})())
    assert REAL_DESKTOP() == (None, None)
    monkeypatch.setattr(gui_keep.os, 'stat', lambda path: type('S', (), {'st_uid': 0})())
    assert REAL_DESKTOP()[0] == 'nobody is logged in at the desktop'
    monkeypatch.setattr(gui_keep.os, 'stat', lambda path: type('S', (), {'st_uid': 502})())
    assert 'another user' in REAL_DESKTOP()[0]


def test_linux_desktop_from_env_or_the_users_wayland_socket(tmp_path, monkeypatch):
    monkeypatch.setattr(gui_keep.sys, 'platform', 'linux')
    monkeypatch.setattr(gui_keep.os, 'getuid', lambda: 1000)
    monkeypatch.setenv('DISPLAY', ':0')
    monkeypatch.delenv('WAYLAND_DISPLAY', raising=False)
    assert REAL_DESKTOP() == (None, None)  # the daemon's own environment has a display
    monkeypatch.delenv('DISPLAY')
    monkeypatch.setenv('XDG_RUNTIME_DIR', str(tmp_path))
    assert REAL_DESKTOP()[0] and REAL_DESKTOP()[1] is None  # ssh, nobody's graphical session
    (tmp_path / 'wayland-0').touch()  # a logged-in Wayland session's socket
    assert REAL_DESKTOP() == (None, {'WAYLAND_DISPLAY': 'wayland-0', 'XDG_RUNTIME_DIR': str(tmp_path)})


def test_shipped_gui_block_and_the_private_file_may_override_it():
    assert fleet_config.block('gui') == {'keep_open': True, 'check_s': 30, 'backoff_max_s': 600}
    assert 'gui' in fleet_config.USER_BLOCKS


def test_settings_reject_a_bad_interval(monkeypatch):
    monkeypatch.setattr(fleet_config, 'block', lambda name: {'check_s': 0})
    with pytest.raises(ValueError):
        gui_keep.settings()


def test_fleet_tick_runs_the_keep_open_part():
    import inspect
    assert "('gui_keep', lambda: gui_keep.tick(self))" in inspect.getsource(fleet.Fleet.tick)
