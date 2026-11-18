"""Bare `fleetmux` with no live root fleet starts the default one from fleet.yaml#bare (docs/design.md section 50,
"Bare `fleetmux`", item 5).

`gui()` asks `open_window.open_root_window` first; with no root and no fleet of this HOME's own it calls
`open_window.start_bare_root`: one line, then `start --lifecycle <lifecycle> --name <name> --resume auto --no-join`
in the bare fleet home (FLEETMUX_HOME / FLEETMUX_STATE), stdin from /dev/null, through `fleet.run_start`. The new
root daemon's gui_keep opens the window: bare waits up to bare.window_wait_s for it and raises it; when the wait runs
out it falls back once to the open-and-raise step (an open window first, else a new one) and returns. Over ssh or
without a desktop it attaches instead.

Nothing real happens here: tmp HOME (conftest + the `world` fixture), `find_root`, `fleet.run_start` (a fake that
writes the latest.json `start` would write), the desktop check, the window scan, the launcher, the raise, the
clock of the wait (`open_window.time`) and `execvp` are stubs, and any other `subprocess.Popen` fails the test.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from fleetmux import fleet, fleet_config, gui_keep, join, open_window  # noqa: E402
from fleetmux.backends import fleetmux as fleetmux_backend  # noqa: E402
from test_fleetmux_history import gui_world, make_run  # noqa: E402

BIN = '/opt/fmx/bin/fleetmux'
WEB = '/opt/fmx/bin/fleetmux-web'
DAEMON_GUI_PID = 5151
SPAWNED_PID = 778
MODEL_FLAGS = ('--supervisor-cli', '--worker-cli', '--supervisor-model', '--worker-model', '--pi-model',
               '--supervisor-pi-model', '--profile', '--supervisor-profile', '--worker-profile')


class Mgr:
    def __init__(self, runtime):
        self.runtime_root = Path(runtime)
        self.created = False

    def get_or_create_session(self):
        self.created = True
        return True


class Proc:
    pid = SPAWNED_PID


class Clock:
    """time.monotonic / time.sleep of the wait: a sleep advances the clock, nothing really waits."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s)
        self.now += s


@pytest.fixture(autouse=True)
def no_real_process(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError('test_open_window_bare starts no process: %r' % (a,))
    monkeypatch.setattr(subprocess, 'Popen', refuse)


def private(tmp_path, monkeypatch, blocks):
    path = tmp_path / 'private-fleet.yaml'
    path.write_text(yaml.safe_dump(blocks))
    monkeypatch.setenv(fleet_config.USER_ENV, str(path))
    return path


@pytest.fixture
def world(tmp_path, monkeypatch):
    """No root fleet anywhere, nothing running in this HOME, a desktop with fleetmux-web; the user's home is tmp."""
    home_mgr, execs = gui_world(tmp_path, monkeypatch)
    user_home = tmp_path / 'user'
    user_home.mkdir()
    monkeypatch.setenv('HOME', str(user_home))
    w = {'root': None, 'starts': [], 'start_rc': 0, 'write_latest': True, 'execs': execs, 'home_mgr': home_mgr,
         'spawned': [], 'raised': [], 'gui_pids': [], 'tmp': tmp_path, 'user': user_home, 'order': []}
    monkeypatch.setattr(join, 'find_root', lambda current, homes=None, name=None: w['root'])
    mgrs = {}

    def manager_for(cfg):
        if cfg.get('bare_marker'):
            return mgrs.setdefault(cfg['run'], Mgr(Path(cfg['run']).parent.parent / 'state' / 'fleetmux' / 'rt'))
        return home_mgr

    monkeypatch.setattr(fleet, 'manager_for', manager_for)

    def run_start(argv, env):
        w['order'].append('start')
        w['starts'].append((list(argv), dict(env)))
        if w['start_rc'] == 0 and w['write_latest']:
            home = Path(env['FLEETMUX_HOME'])
            run = home / 'runs' / 'fleet-bare'
            run.mkdir(parents=True, exist_ok=True)
            cfg = {'run': str(run), 'backend': 'fleetmux', 'bare_marker': True, 'name': 'main', 'mode': 'service'}
            fleet.save(run / 'config.json', cfg)
            fleet.save(home / 'latest.json', dict(cfg, config=str(run / 'config.json')))
            w['run'] = run
        return w['start_rc']

    monkeypatch.setattr(fleet, 'run_start', run_start)
    monkeypatch.setattr(fleetmux_backend, 'fleetmux_bin', lambda: BIN)
    monkeypatch.setattr(fleet, 'fleetmux_web_bin', lambda binary: WEB)
    monkeypatch.setattr(fleet, 'has_desktop', lambda: True)
    monkeypatch.delenv('SSH_CONNECTION', raising=False)
    monkeypatch.setattr(fleet, 'daemon_running', lambda run: False)
    monkeypatch.setattr(sys, 'platform', 'darwin')

    def gui_running(runtime):
        w['order'].append('scan')
        w.setdefault('scanned', []).append(Path(runtime))
        return w['gui_pids'].pop(0) if w['gui_pids'] else None

    monkeypatch.setattr(gui_keep, 'gui_running', gui_running)

    def spawn(argv, log, env):
        w['spawned'].append((list(argv), Path(log), env))
        return Proc()

    monkeypatch.setattr(gui_keep, 'spawn', spawn)
    monkeypatch.setattr(fleet, 'bring_window_to_front', lambda pid, wait_s=5.0: w['raised'].append(pid) or True)
    clock = Clock()
    monkeypatch.setattr(open_window.time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(open_window.time, 'sleep', clock.sleep)
    w['clock'] = clock
    return w


def bare_home(w, name='main'):
    return w['user'] / '.fleetmux' / 'fleets' / name


def fresh_runtime(w):
    return str(Path(w['run']).parent.parent / 'state' / 'fleetmux' / 'rt')


# ---- N1: no root fleet: start the default one, then its window is in front ---------------------------------------

def test_no_root_starts_the_bare_root_fleet(world):
    fleet.main(['gui'])
    assert len(world['starts']) == 1, world['starts']
    argv, env = world['starts'][0]
    assert argv == ['--lifecycle', 'service', '--name', 'main', '--resume', 'auto', '--no-join'], argv
    home = bare_home(world)
    assert env['FLEETMUX_HOME'] == str(home) and env['FLEETMUX_STATE'] == str(home / 'state'), env
    assert not world['home_mgr'].created, 'no server of this HOME is started: the bare root has its own'


def test_it_says_what_it_starts_before_starting(world, capsys, monkeypatch):
    world['gui_pids'] = [DAEMON_GUI_PID]
    seen = []
    real = fleet.run_start
    monkeypatch.setattr(fleet, 'run_start', lambda argv, env: seen.append(capsys.readouterr().out) or real(argv, env))
    fleet.main(['gui'])
    assert len(seen) == 1 and seen[0].count('\n') == 1, seen
    assert 'main' in seen[0] and str(bare_home(world)) in seen[0], seen


def test_the_daemon_window_is_waited_for_and_raised(world, capsys):
    """The window comes from the new root daemon's gui_keep: bare looks every window_poll_s and raises it."""
    world['gui_pids'] = [None, None, DAEMON_GUI_PID]
    fleet.main(['gui'])
    assert world['order'][0] == 'start', world['order']
    assert set(map(str, world['scanned'])) == {fresh_runtime(world)}, world['scanned']
    assert world['raised'] == [DAEMON_GUI_PID], world['raised']
    assert world['spawned'] == [] and world['execs'] == [], 'the daemon opens the window; bare only raises it'
    assert world['clock'].slept == [0.5, 0.5], world['clock'].slept
    assert 'brought it to the front' in capsys.readouterr().out


def test_a_window_the_daemon_already_opened_is_raised_without_a_wait(world):
    world['gui_pids'] = [DAEMON_GUI_PID]
    fleet.main(['gui'])
    assert world['raised'] == [DAEMON_GUI_PID] and world['spawned'] == [], (world['raised'], world['spawned'])
    assert world['clock'].slept == [], world['clock'].slept


def test_the_wait_comes_from_fleet_yaml(world, monkeypatch):
    private(world['tmp'], monkeypatch, {'bare': {'window_wait_s': 5, 'window_poll_s': 1}})
    fleet.main(['gui'])
    assert world['clock'].slept == [1, 1, 1, 1, 1], world['clock'].slept


def test_no_window_within_the_wait_falls_back_once_to_open_and_raise(world, capsys):
    """The wait runs out (the daemon is still in start_all or a resume's replay): one fallback through the
    open-and-raise step, which opens the window itself, then the command returns. Never more than window_wait_s."""
    fleet.main(['gui'])  # shipped window_wait_s 30, poll 0.5; no window ever
    assert sum(world['clock'].slept) == 30, sum(world['clock'].slept)
    assert [s[0] for s in world['spawned']] == [[BIN, 'gui', '--runtime', fresh_runtime(world)]], world['spawned']
    argv, log, env = world['spawned'][0]
    assert log == Path(world['run']) / 'gui.log' and env is None, world['spawned']
    assert world['raised'] == [SPAWNED_PID] and world['execs'] == [], (world['raised'], world['execs'])
    out = capsys.readouterr().out
    assert 'within 30 s' in out and 'Opened a window' in out, out


def test_the_fallback_checks_for_an_open_window_first(world):
    """The daemon's window turned up right after the last look: the fallback raises it, no second window."""
    world['gui_pids'] = [None] * 61 + [DAEMON_GUI_PID]  # 61 looks in the wait (0 .. 30 s), then the fallback's
    fleet.main(['gui'])
    assert world['spawned'] == [] and world['raised'] == [DAEMON_GUI_PID], (world['spawned'], world['raised'])


def test_keep_open_false_skips_the_wait(world, monkeypatch):
    """gui.keep_open false: no daemon will open the window, so bare opens it at once."""
    private(world['tmp'], monkeypatch, {'gui': {'keep_open': False}})
    fleet.main(['gui'])
    assert world['clock'].slept == [] and [s[0][1] for s in world['spawned']] == ['gui'], world['spawned']
    assert world['raised'] == [SPAWNED_PID]


@pytest.mark.parametrize('where', ['no-tty', 'pane'])
def test_no_tty_and_inside_a_pane_still_end_with_the_window_in_front(world, monkeypatch, where):
    monkeypatch.setattr(fleet, 'interactive_terminal', lambda: False)
    if where == 'pane':
        monkeypatch.setattr(fleet, 'inside_fleetmux_pane', lambda: True)
    world['gui_pids'] = [None, DAEMON_GUI_PID]
    fleet.main(['gui'])
    assert world['raised'] == [DAEMON_GUI_PID], world['raised']


def test_start_never_gets_no_view_or_a_view_flag(world):
    world['gui_pids'] = [DAEMON_GUI_PID]
    fleet.main(['gui'])
    argv = world['starts'][0][0]
    assert not {'--no-view', '--gui', '--attach'} & set(argv), 'gui_keep must stay on for the run: %r' % (argv,)


def test_a_home_running_another_fleet_does_not_start_the_bare_root(world, monkeypatch):
    other = make_run(world['tmp'], 'fleet-here', result=False)
    monkeypatch.setattr(fleet, 'gui_live_run', lambda: other)
    fleet.main(['gui'])
    assert world['starts'] == [], 'a HOME that runs its own fleet keeps the old path'
    assert [e[1][1:] for e in world['execs']] == [['gui', '--runtime', str(world['home_mgr'].runtime_root)]]


def test_start_failing_exits_with_its_code(world):
    world['start_rc'] = 3
    with pytest.raises(SystemExit) as exc:
        fleet.main(['gui'])
    assert exc.value.code == 3
    assert world['spawned'] == [] and world['execs'] == [] and world['raised'] == []
    assert not world['home_mgr'].created, 'no history window after a failed start'


def test_start_without_a_run_in_latest_json_is_an_error(world):
    world['write_latest'] = False
    with pytest.raises(SystemExit) as exc:
        fleet.main(['gui'])
    assert exc.value.code not in (0, None) and 'latest.json' in str(exc.value.code), exc.value.code


# ---- N2: the defaults are fleet.yaml#bare, the private file replaces the block whole -------------------------------

def test_bare_block_is_shipped_and_user_overridable():
    assert 'bare' in fleet_config.BLOCKS and 'bare' in fleet_config.USER_BLOCKS
    shipped = yaml.safe_load(fleet_config.SHIPPED.read_text())['bare']
    assert shipped['name'] and shipped['lifecycle'] == 'service', shipped
    assert shipped['home'] == '~/.fleetmux/fleets/{name}', shipped
    assert shipped['enabled'] is True and shipped['window_wait_s'] > 0 and shipped['window_poll_s'] > 0, shipped
    assert not [a for a in shipped['start_args'] if 'sandbox' in a], 'no sandbox waiver by default: %r' % (shipped,)
    assert not {k for k in shipped if 'model' in k or k.endswith('_cli')}, 'no model in bare: ' + repr(shipped)


def test_shipped_settings_expand_the_home_under_the_user_home(world):
    s = fleet_config.bare()
    assert s['home'] == str(bare_home(world)) and s['lifecycle'] == 'service' and s['enabled'] is True, s


def test_private_bare_block_replaces_the_shipped_one(world, monkeypatch):
    roster = world['tmp'] / 'roster.yaml'
    private(world['tmp'], monkeypatch, {'bare': {'name': 'dev', 'roster': str(roster),
                                                 'start_args': ['--allow-unsandboxed-pi']}})
    world['gui_pids'] = [DAEMON_GUI_PID]
    fleet.main(['gui'])
    argv, env = world['starts'][0]
    assert argv == ['--lifecycle', 'service', '--name', 'dev', '--resume', 'auto', '--no-join',
                    '--roster', str(roster), '--allow-unsandboxed-pi'], argv
    assert env['FLEETMUX_HOME'] == str(bare_home(world, 'dev')), env


def test_workers_and_home_template(world, monkeypatch):
    private(world['tmp'], monkeypatch, {'bare': {'name': 'x', 'workers': 3, 'home': '~/fleets-elsewhere/{name}-home'}})
    s = fleet_config.bare()
    assert s['home'] == str(world['user'] / 'fleets-elsewhere' / 'x-home'), s
    world['gui_pids'] = [DAEMON_GUI_PID]
    fleet.main(['gui'])
    assert world['starts'][0][0][-2:] == ['--workers', '3'], world['starts']


@pytest.mark.parametrize('block', [
    {'nope': 1},
    {'workers': 0},
    {'workers': 2, 'roster': '/r.yaml'},
    {'start_args': '--x'},
    {'start_args': ['--supervisor-model', 'opus']},
    {'start_args': ['--worker-cli=pi']},
    {'start_args': ['--no-view']},
    {'start_args': ['--resume', 'x']},
    {'name': ''},
    {'roster': ''},
    {'window_wait_s': 0},
    {'window_poll_s': -1},
    {'enabled': 'yes'},
])
def test_bad_bare_block_is_refused_before_any_start(world, monkeypatch, block):
    private(world['tmp'], monkeypatch, {'bare': block})
    with pytest.raises(ValueError):
        fleet_config.bare()
    with pytest.raises(SystemExit) as exc:
        fleet.main(['gui'])
    assert 'bare' in str(exc.value.code), exc.value.code
    assert world['starts'] == []


def test_the_shipped_block_writes_no_model(world):
    world['gui_pids'] = [DAEMON_GUI_PID]
    fleet.main(['gui'])
    argv = world['starts'][0][0]
    assert not [a for a in argv if a.split('=')[0] in MODEL_FLAGS], argv


def test_bare_off_keeps_the_history_window(world, monkeypatch):
    private(world['tmp'], monkeypatch, {'bare': {'enabled': False}})
    make_run(world['tmp'], 'fleet-old', closed_at=1790000000)
    fleet.main(['gui'])
    assert world['starts'] == []
    assert [e[1][1:] for e in world['execs']] == [['gui', '--runtime', str(world['home_mgr'].runtime_root)]]


# ---- N3: resume before new; only the bare home ----------------------------------------------------------------------

def test_resume_auto_is_asked_in_the_bare_home_only(world):
    other = world['user'] / '.fleetmux' / 'fleets' / 'other'
    other.mkdir(parents=True)
    fleet.save(other / 'latest.json', {'run': str(other / 'runs' / 'r')})
    before = (other / 'latest.json').read_bytes()
    world['gui_pids'] = [DAEMON_GUI_PID]
    fleet.main(['gui'])
    argv, env = world['starts'][0]
    assert argv[argv.index('--resume') + 1] == 'auto', argv
    assert '--fleet-home' not in argv and env['FLEETMUX_HOME'] == str(bare_home(world)), (argv, env)
    assert (other / 'latest.json').read_bytes() == before


def test_a_resumed_root_gets_its_window_too(world):
    """start --resume auto reattached the earlier run: latest.json still names it, and that run's window is shown."""
    home = bare_home(world)
    run = home / 'runs' / 'fleet-earlier'
    run.mkdir(parents=True)
    cfg = {'run': str(run), 'backend': 'fleetmux', 'bare_marker': True, 'name': 'main'}
    fleet.save(run / 'config.json', cfg)
    fleet.save(home / 'latest.json', dict(cfg, config=str(run / 'config.json')))
    world['write_latest'] = False
    world['run'] = run
    world['gui_pids'] = [DAEMON_GUI_PID]
    fleet.main(['gui'])
    assert set(map(str, world['scanned'])) == {fresh_runtime(world)} and world['raised'] == [DAEMON_GUI_PID]


# ---- N4: ssh / no desktop: start, then attach ------------------------------------------------------------------------

@pytest.mark.parametrize('why', ['ssh', 'no-desktop'])
def test_over_ssh_with_no_root_it_starts_then_attaches(world, monkeypatch, why):
    if why == 'ssh':
        monkeypatch.setenv('SSH_CONNECTION', '10.0.0.2 50000 10.0.0.1 22')
    else:
        monkeypatch.setattr(fleet, 'has_desktop', lambda: False)
        monkeypatch.setattr(fleet, 'gui_problem', lambda: 'gui needs a desktop -- use attach instead')
    fleet.main(['gui'])
    assert len(world['starts']) == 1
    assert world['execs'] == [(BIN, [BIN, 'attach', '--runtime', fresh_runtime(world)])], world['execs']
    assert world['raised'] == [] and world['spawned'] == [] and world['clock'].slept == []


def test_a_live_root_never_starts_the_bare_root(world, monkeypatch):
    run = world['tmp'] / 'fleets' / 'svc' / 'runs' / 'fleet-root'
    run.mkdir(parents=True)
    fleet.save(run / 'config.json', {'run': str(run), 'backend': 'fleetmux', 'bare_marker': True})
    world['root'] = {'config': str(run / 'config.json'), 'home': str(run.parent.parent), 'mode': 'service',
                     'run': str(run), 'name': 'svc'}
    world['gui_pids'] = [DAEMON_GUI_PID]
    fleet.main(['gui'])
    assert world['starts'] == [] and world['raised'] == [DAEMON_GUI_PID]


# ---- N5: the start is one seam ------------------------------------------------------------------------------------

def test_run_start_runs_the_python_start_with_stdin_from_dev_null(monkeypatch):
    seen = []

    class Done:
        returncode = 0

    monkeypatch.setattr(subprocess, 'run', lambda argv, **kw: seen.append((argv, kw)) or Done())
    real = fleet.run_start.real  # conftest's guard keeps the real function here
    assert real(['--lifecycle', 'service'], {'FLEETMUX_HOME': '/x'}) == 0
    argv, kw = seen[0]
    assert argv[:2] == [sys.executable, str(Path(fleet.__file__).resolve())] and argv[2:] == ['start', '--lifecycle',
                                                                                           'service'], argv
    assert kw['stdin'] is subprocess.DEVNULL and kw['env'] == {'FLEETMUX_HOME': '/x'}, kw


def test_conftest_guard_refuses_a_real_start(monkeypatch):
    """Outside this file's fake, a gui test that reached the bare start would fail, not start a fleet."""
    with pytest.raises(AssertionError):
        fleet.run_start(['--lifecycle', 'service'], dict(os.environ))
