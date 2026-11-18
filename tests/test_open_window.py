"""Bare `fleetmux` (= `fleetmux gui`) opens this machine's root fleet window (docs/design.md section 50, "Bare
`fleetmux`").

Rust hands a bare `fleetmux` to the Python `gui` (rust/crates/app/tests/forward_cli.rs). `gui()` asks
`open_window.open_root_window` first: it finds the live root fleet with `join.find_root`, raises its open window,
or starts one in the background, or execs `attach` without a desktop; with no root `gui()` keeps its history window.

Nothing real happens here: tmp HOME (conftest), `find_root`, the desktop check, `fleetmux-web`, the window scan
(`gui_keep.gui_running`), the launcher (`gui_keep.spawn`), the raise and `execvp` are all stubs, and any other
`subprocess.Popen` fails the test. With no root, `gui()` starts the fleet.yaml#bare root fleet
(tests/test_open_window_bare.py).
"""
import subprocess
import sys
from pathlib import Path

import pytest

from fleetmux import fleet, gui_keep, join, open_window  # noqa: E402
from fleetmux.backends import fleetmux as fleetmux_backend  # noqa: E402
from test_fleetmux_history import bare_off, gui_world, make_run  # noqa: E402

BIN = '/opt/fmx/bin/fleetmux'
WEB = '/opt/fmx/bin/fleetmux-web'
GUI_PID = 4242
SPAWNED_PID = 777


class RootMgr:
    """The root fleet's manager: only its runtime directory matters here."""

    def __init__(self, runtime):
        self.runtime_root = Path(runtime)
        self.created = False

    def get_or_create_session(self):  # pragma: no cover - the root path never starts a server
        self.created = True
        return True


class Proc:
    pid = SPAWNED_PID


@pytest.fixture(autouse=True)
def no_real_process(monkeypatch):
    """Any subprocess this file did not stub is a test failure (no ps, no osascript, no window)."""
    def refuse(*a, **k):
        raise AssertionError('test_open_window starts no process: %r' % (a,))
    monkeypatch.setattr(subprocess, 'Popen', refuse)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A live root fleet elsewhere on the machine, a desktop with fleetmux-web, no window open yet."""
    home_mgr, execs = gui_world(tmp_path, monkeypatch)
    root_home = tmp_path / 'fleets' / 'svc'
    run = root_home / 'runs' / 'fleet-root'
    run.mkdir(parents=True)
    config = run / 'config.json'
    fleet.save(config, {'run': str(run), 'backend': 'fleetmux', 'root_marker': True,
                        'cmux_state_dir': str(root_home / 'state')})
    root = {'config': str(config), 'home': str(root_home), 'mode': 'service', 'run': str(run), 'name': 'svc'}
    root_mgr = RootMgr(root_home / 'state' / 'fleetmux' / 'fleetmux')
    asked = []

    def find_root(current, homes=None, name=None):
        asked.append(Path(current))
        return w['root']

    monkeypatch.setattr(join, 'find_root', find_root)
    monkeypatch.setattr(fleet, 'manager_for', lambda cfg: root_mgr if cfg.get('root_marker') else home_mgr)
    monkeypatch.setattr(fleetmux_backend, 'fleetmux_bin', lambda: BIN)
    monkeypatch.setattr(fleet, 'fleetmux_web_bin', lambda binary: WEB)
    monkeypatch.setattr(fleet, 'has_desktop', lambda: True)
    monkeypatch.delenv('SSH_CONNECTION', raising=False)
    monkeypatch.setattr(fleet, 'daemon_running', lambda run: False)
    monkeypatch.setattr(sys, 'platform', 'darwin')
    spawned, raised = [], []

    def spawn(argv, log, env):
        spawned.append((list(argv), Path(log), env))
        return Proc()

    monkeypatch.setattr(gui_keep, 'spawn', spawn)
    monkeypatch.setattr(gui_keep, 'gui_running', lambda runtime: None)
    monkeypatch.setattr(fleet, 'bring_window_to_front', lambda pid, wait_s=5.0: raised.append(pid) or True)
    w = {'root': root, 'run': run, 'root_mgr': root_mgr, 'home_mgr': home_mgr, 'execs': execs,
         'spawned': spawned, 'raised': raised, 'asked': asked, 'tmp': tmp_path}
    return w


def gui_window(w):
    return [BIN, 'gui', '--runtime', str(w['root_mgr'].runtime_root)]


# ---- R1: the root find_root names, on its own runtime ------------------------------------------------------------

def test_bare_gui_uses_the_root_find_root_returns_and_its_runtime(world, capsys):
    fleet.main(['gui'])
    assert world['asked'] == [Path(fleet.HOME)], 'find_root searches from this HOME: %r' % (world['asked'],)
    assert [s[0] for s in world['spawned']] == [gui_window(world)], world['spawned']
    assert world['execs'] == [], 'the root window is started in the background, never exec: %r' % (world['execs'],)
    assert not world['home_mgr'].created, 'no server of this HOME is started for the root window'


def test_a_join_stub_latest_json_does_not_hide_the_root(world, capsys):
    """The reported case: ~/.fleetmux/latest.json is a join stub, the root runs in ~/.fleetmux/fleets/<name>."""
    fleet.save(fleet.LATEST, {'join': {'root_config': str(world['root']['config'])}})
    fleet.main(['gui'])
    out = capsys.readouterr().out
    assert fleet.GUI_NO_HISTORY not in out, out
    assert [s[0] for s in world['spawned']] == [gui_window(world)], world['spawned']


# ---- R2: an open window is raised; nothing new starts ------------------------------------------------------------

def test_an_open_window_is_raised_and_nothing_is_started(world, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(gui_keep, 'gui_running', lambda runtime: seen.append(Path(runtime)) or GUI_PID)
    assert open_window.open_root_window(fleet.HOME) == 0
    assert seen == [world['root_mgr'].runtime_root], seen
    assert world['raised'] == [GUI_PID], 'the open window is brought to the front: %r' % (world['raised'],)
    assert world['spawned'] == [] and world['execs'] == [], (world['spawned'], world['execs'])
    assert not world['root_mgr'].created and not world['home_mgr'].created
    assert str(world['root_mgr'].runtime_root) in capsys.readouterr().out


def test_an_open_window_via_the_gui_command_exits_0(world, monkeypatch):
    monkeypatch.setattr(gui_keep, 'gui_running', lambda runtime: GUI_PID)
    fleet.main(['gui'])  # returns: exit 0
    assert world['raised'] == [GUI_PID] and world['spawned'] == [] and world['execs'] == []


# ---- R3: no window: start one in the background, raise it, one line, return -----------------------------------

def test_no_window_starts_one_in_the_background_and_returns(world, capsys):
    assert open_window.open_root_window(fleet.HOME) == 0
    assert len(world['spawned']) == 1, world['spawned']
    argv, log, env = world['spawned'][0]
    assert argv == gui_window(world) and log == world['run'] / 'gui.log' and env is None, world['spawned']
    assert world['raised'] == [SPAWNED_PID], world['raised']
    assert world['execs'] == []
    out = capsys.readouterr().out
    assert out.count('\n') == 1 and 'Opened a window' in out and str(world['run'] / 'gui.log') in out, out


def test_no_window_on_linux_is_not_raised(world, monkeypatch):
    monkeypatch.setattr(sys, 'platform', 'linux')
    assert open_window.open_root_window(fleet.HOME) == 0
    assert len(world['spawned']) == 1 and world['raised'] == [], (world['spawned'], world['raised'])


def test_inside_a_fleetmux_pane_it_still_opens(world, monkeypatch):
    monkeypatch.setattr(fleet, 'inside_fleetmux_pane', lambda: True)
    monkeypatch.setattr(fleet, 'interactive_terminal', lambda: False)
    fleet.main(['gui'])
    assert [s[0] for s in world['spawned']] == [gui_window(world)], world['spawned']


# ---- R4: no desktop, ssh or no fleetmux-web: attach ---------------------------------------------------------------

@pytest.mark.parametrize('why', ['no-desktop', 'ssh', 'no-web'])
def test_no_desktop_ssh_or_no_fleetmux_web_execs_attach(world, monkeypatch, why):
    if why == 'no-desktop':
        monkeypatch.setattr(fleet, 'has_desktop', lambda: False)
        # without a desktop the history path would refuse; the root comes first
        monkeypatch.setattr(fleet, 'gui_problem', lambda: 'gui needs a desktop -- use attach instead')
    elif why == 'ssh':
        monkeypatch.setenv('SSH_CONNECTION', '10.0.0.2 50000 10.0.0.1 22')
    else:
        monkeypatch.setattr(fleet, 'fleetmux_web_bin', lambda binary: None)
    fleet.main(['gui'])
    attach = [BIN, 'attach', '--runtime', str(world['root_mgr'].runtime_root)]
    assert world['execs'] == [(BIN, attach)], world['execs']
    assert world['spawned'] == [] and world['raised'] == [], (world['spawned'], world['raised'])


@pytest.mark.parametrize('why', ['no-desktop', 'ssh'])
def test_with_a_window_already_open_ssh_or_no_desktop_still_attaches(world, monkeypatch, why):
    """The root daemon keeps its window open, so over ssh there nearly always is one: never raise a window on a
    desktop the person is not at; attach here instead."""
    monkeypatch.setattr(gui_keep, 'gui_running', lambda runtime: GUI_PID)
    if why == 'no-desktop':
        monkeypatch.setattr(fleet, 'has_desktop', lambda: False)
    else:
        monkeypatch.setenv('SSH_CONNECTION', '10.0.0.2 50000 10.0.0.1 22')
    fleet.main(['gui'])
    attach = [BIN, 'attach', '--runtime', str(world['root_mgr'].runtime_root)]
    assert world['execs'] == [(BIN, attach)], world['execs']
    assert world['raised'] == [] and world['spawned'] == [], (world['raised'], world['spawned'])


def test_an_open_window_on_linux_is_not_raised_and_the_line_says_only_open(world, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'platform', 'linux')
    monkeypatch.setattr(gui_keep, 'gui_running', lambda runtime: GUI_PID)
    assert open_window.open_root_window(fleet.HOME) == 0
    out = capsys.readouterr().out
    assert world['raised'] == [] and world['spawned'] == [], (world['raised'], world['spawned'])
    assert 'already open' in out and 'front' not in out, out


def test_a_failed_raise_does_not_claim_the_window_came_to_the_front(world, monkeypatch, capsys):
    monkeypatch.setattr(gui_keep, 'gui_running', lambda runtime: GUI_PID)
    monkeypatch.setattr(fleet, 'bring_window_to_front', lambda pid, wait_s=5.0: False)
    assert open_window.open_root_window(fleet.HOME) == 0
    out = capsys.readouterr().out
    assert 'already open' in out and 'brought it to the front' not in out, out


def test_the_desktop_rule_gets_the_found_binary_not_a_bare_name(world, monkeypatch):
    """`fleetmux_web_bin('fleetmux')` would also look in the current directory; with no binary found it gets None."""
    seen = []
    monkeypatch.setattr(fleetmux_backend, 'fleetmux_bin', lambda: None)
    monkeypatch.setattr(fleet, 'fleetmux_web_bin', lambda binary: seen.append(binary) or WEB)
    open_window.open_root_window(fleet.HOME)
    assert seen == [None], seen
    assert world['spawned'][0][0][0] == 'fleetmux', world['spawned']


# ---- R5: no root and fleet.yaml#bare off: the history window as before ------------------------------------------
# (bare on, the default, starts the bare root fleet: tests/test_open_window_bare.py)

def test_no_root_with_bare_off_falls_back_to_the_history_window(world, monkeypatch):
    bare_off(world['tmp'], monkeypatch)
    world['root'] = None
    make_run(world['tmp'], 'fleet-old', closed_at=1790000000)
    fleet.main(['gui'])
    assert world['spawned'] == []
    assert [e[1][1:] for e in world['execs']] == [['gui', '--runtime', str(world['home_mgr'].runtime_root)]], \
        world['execs']
    assert world['home_mgr'].created, 'the history window needs this HOME\'s server, as before'


def test_no_root_with_bare_off_no_history_one_line_exit_1_no_usage(world, monkeypatch, capsys):
    bare_off(world['tmp'], monkeypatch)
    world['root'] = None
    with pytest.raises(SystemExit) as exc:
        fleet.main(['gui'])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert fleet.GUI_NO_HISTORY in captured.out, captured
    assert 'usage' not in (captured.out + captured.err).lower(), captured
    assert world['spawned'] == [] and world['execs'] == [] and not world['home_mgr'].created


@pytest.mark.parametrize('history', [True, False])
def test_no_root_with_bare_on_starts_the_bare_root_not_the_history(world, monkeypatch, capsys, history):
    """bare.enabled true (the shipped default): no root -> the bare root start, neither the grey-card window nor the
    exit-1 line. The start here fails (exit 3) so this file needs no window world; tests/test_open_window_bare.py
    covers the window after a start."""
    world['root'] = None
    if history:
        make_run(world['tmp'], 'fleet-old', closed_at=1790000000)
    starts = []
    monkeypatch.setattr(fleet, 'run_start', lambda argv, env: starts.append(argv) or 3)
    with pytest.raises(SystemExit) as exc:
        fleet.main(['gui'])
    assert exc.value.code == 3 and len(starts) == 1 and starts[0][:2] == ['--lifecycle', 'service'], starts
    assert world['execs'] == [] and world['spawned'] == [] and not world['home_mgr'].created
    assert fleet.GUI_NO_HISTORY not in capsys.readouterr().out


def test_open_root_window_without_a_root_returns_none(world):
    world['root'] = None
    assert open_window.open_root_window(fleet.HOME) is None
    assert world['spawned'] == [] and world['execs'] == []


# ---- R6: `fleetmux gui` with a live run in this HOME ---------------------------------------------------------------

def test_gui_with_a_live_sub_fleet_run_here_keeps_the_old_path(world, monkeypatch):
    """This HOME runs a fleet that is not the root (a sub-fleet's home, a --no-join fleet): gui shows it as before."""
    other = make_run(world['tmp'], 'fleet-here', result=False)
    monkeypatch.setattr(fleet, 'gui_live_run', lambda: other)
    fleet.main(['gui'])
    assert world['spawned'] == [] and world['raised'] == []
    assert [e[1][1:] for e in world['execs']] == [['gui', '--runtime', str(world['home_mgr'].runtime_root)]], \
        world['execs']


def test_gui_in_the_roots_own_home_raises_instead_of_a_second_window(world, monkeypatch):
    """A shell whose FLEETMUX_HOME is the root's home (a pane of the root): R2 / R3, never a foreground exec."""
    monkeypatch.setattr(fleet, 'gui_live_run', lambda: Path(world['root']['run']))
    monkeypatch.setattr(gui_keep, 'gui_running', lambda runtime: GUI_PID)
    fleet.main(['gui'])
    assert world['raised'] == [GUI_PID] and world['spawned'] == [] and world['execs'] == []


# ---- R7: one implementation -----------------------------------------------------------------------------------------

def test_one_implementation(world, monkeypatch):
    calls = []
    monkeypatch.setattr(open_window, 'open_root_window', lambda home, live=None: calls.append((home, live)) or 0)
    fleet.main(['gui'])
    assert calls == [(fleet.HOME, None)], 'gui() opens the root through open_window.open_root_window: %r' % calls
    src = Path(open_window.__file__).read_text()
    for own in ('subprocess', "'ps'", 'Popen', 'pgrep', 'osascript'):
        assert own not in src, 'open_window.py must reuse the scan / launcher / raise, not carry its own ' + own
    assert 'gui_keep.gui_running' in src and 'gui_keep.spawn' in src and 'fleet.window_here' in src, src


def test_auto_view_and_open_window_share_the_desktop_rule(monkeypatch):
    monkeypatch.setattr(fleet, 'has_desktop', lambda: True)
    monkeypatch.setattr(fleet, 'fleetmux_web_bin', lambda binary: WEB)
    monkeypatch.delenv('SSH_CONNECTION', raising=False)
    assert fleet.window_here(BIN)
    monkeypatch.setenv('SSH_CONNECTION', 'x')
    assert not fleet.window_here(BIN)
