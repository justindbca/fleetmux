"""Companion tabs (docs/design.md §31): the roster ``tabs:`` section, tabs opened once after control, and the
program-by-command-line clear before each open. Fake fleet / fake ``_run``; the clear runs real processes."""
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from cmux_fleet import cmux_manager, fleet, roster, tabs


# ------------------------------------------------------------------ 1. the roster section
def parse(tmp_path, body):
    path = tmp_path / 'r.yaml'
    path.write_text('workers:\n  - name: w1\n' + body)
    roster.load_entries(path)
    return roster.tabs(path.read_bytes(), path.parent)


def test_tabs_section_resolves_cwd_and_env(tmp_path, monkeypatch):
    (tmp_path / 'sub').mkdir()
    monkeypatch.setenv('HOME', str(tmp_path))
    got = parse(tmp_path, 'tabs:\n  - {name: a, command: run a}\n  - {name: b-2, command: run b, cwd: sub, '
                          'env: {X: "1"}}\n  - {name: c, command: run c, cwd: ~/sub}\n')
    sub = str((tmp_path / 'sub').resolve())
    assert got == [{'name': 'a', 'command': 'run a', 'cwd': str(tmp_path.resolve()), 'env': {}},
                   {'name': 'b-2', 'command': 'run b', 'cwd': sub, 'env': {'X': '1'}},
                   {'name': 'c', 'command': 'run c', 'cwd': sub, 'env': {}}]
    assert parse(tmp_path, '') == []


@pytest.mark.parametrize('body,why', [
    ('tabs: {name: a}\n', 'tabs must be a list'),
    ('tabs:\n  - {name: A, command: x}\n', 'name must match'),
    ('tabs:\n  - {name: a, command: x}\n  - {name: a, command: y}\n', 'duplicate tab name'),
    ('tabs:\n  - {name: a}\n', 'command must be a non-empty string'),
    ('tabs:\n  - {name: a, command: x, cwd: nowhere}\n', 'cwd directory not found'),
    ('tabs:\n  - {name: a, command: x, env: {PORT: 8895}}\n', 'quote numbers'),
    ('tabs:\n  - {name: a, command: x, restart: never}\n', 'keys command, cwd, env, name only'),
])
def test_bad_tabs_reject_the_whole_roster(tmp_path, body, why):
    with pytest.raises(ValueError, match=why):
        parse(tmp_path, body)


# ------------------------------------------------------------------ 2. matching by the full command line
def test_same_program_is_exact_with_the_framework_python_stub():
    argv = shlex.split('/opt/homebrew/bin/python3.14 /Users/j/shell.py --fleet-home /Users/j/.ops --port 8895')
    first = {'/opt/homebrew/bin/python3.14', '/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/'
             'Versions/3.14/Resources/Python.app/Contents/MacOS/Python'}
    stub = sorted(first)[0]
    assert tabs.same_program(' '.join(argv), argv, first)
    assert tabs.same_program(stub + ' /Users/j/shell.py --fleet-home /Users/j/.ops --port 8895', argv, first)
    assert not tabs.same_program(' '.join(argv) + ' --x', argv, first)  # extra argument
    assert not tabs.same_program(' '.join(argv[:-1]), argv, first)  # prefix only
    assert not tabs.same_program('/usr/bin/python3 /Users/j/shell.py --fleet-home /Users/j/.ops --port 8895',
                                 argv, first)  # another interpreter
    assert not tabs.same_program('grep ' + ' '.join(argv), argv, first)


def test_heads_add_the_framework_stub(monkeypatch):
    monkeypatch.setattr(tabs.shutil, 'which', lambda x: '/opt/homebrew/bin/python3.14')
    monkeypatch.setattr(tabs.os.path, 'realpath', lambda p: '/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/'
                        'Python.framework/Versions/3.14/bin/python3.14')
    assert ('/opt/homebrew/Cellar/python@3.14/3.14.7/Frameworks/Python.framework/Versions/3.14/Resources/'
            'Python.app/Contents/MacOS/Python') in tabs.heads('/opt/homebrew/bin/python3.14')


def log_of(f, kind):
    return [d for k, d in f.logs if k == kind]


def fake_fleet(specs, mgr=None):
    f = SimpleNamespace(tab_specs=specs, tabs_opened=False, logs=[], mgr=mgr,
                        r=SimpleNamespace(hget=lambda key, field: 'history'))
    f.log = lambda kind, **d: f.logs.append((kind, d))
    return f


def test_clear_stops_a_running_copy_and_sigkills_one_that_ignores_sigterm(tmp_path, monkeypatch):
    script = tmp_path / 'stubborn.py'
    script.write_text('import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)\n')
    plain = ['/bin/sleep', '3001.25']
    stubborn = [sys.executable, str(script), '--tab', 'x']
    procs = [subprocess.Popen(plain), subprocess.Popen(stubborn)]
    other = subprocess.Popen(['/bin/sleep', '3001.5'])  # same program, other arguments: left alone
    try:
        time.sleep(.5)
        monkeypatch.setattr(tabs, 'KILL_WAIT', 1.0)
        f = fake_fleet([])
        for argv, proc in zip((plain, stubborn), procs):
            assert tabs.matching(argv) == {proc.pid}
            assert tabs.clear(f, {'name': 't', 'command': shlex.join(argv)})
            proc.wait(5)
            assert not tabs.matching(argv)
        assert other.poll() is None
        assert tabs.clear(f, {'name': 't', 'command': shlex.join(plain)})  # nothing running: just True
    finally:
        for p in procs + [other]:
            p.kill()


# ------------------------------------------------------------------ 3. opened once, after control, closed on stop
class Mgr:
    def __init__(self, old=None):
        self.registered, self.calls, self.n = dict(old or {}), [], 0

    def tab_panes(self):
        return dict(self.registered)

    def kill_pane(self, sid):
        self.calls.append(('kill', sid))
        return self.registered.pop(sid, None) is not None

    def get_pane_status(self, sid):
        return None

    def spawn_window(self, name, command, metadata=None, start_directory=None):
        self.n += 1
        sid = 's' + str(self.n)
        self.registered[sid] = name
        self.calls.append(('spawn', name, command, dict(metadata), start_directory))
        return SimpleNamespace(success=True, pane_id=sid, error=None)

    def dock_tab(self, sid, history, after=None):
        self.calls.append(('dock', sid, history, after))
        return True


def test_open_all_clears_then_opens_in_order_after_control_once(monkeypatch):
    cleared = []
    monkeypatch.setattr(tabs, 'clear', lambda f, spec: cleared.append(spec['name']) or True)
    mgr = Mgr(old={'old1': 'shell'})  # a previous daemon's tab still shown
    specs = [{'name': 'shell', 'command': '/bin/echo a', 'cwd': '/tmp', 'env': {'PORT': '8895'}},
             {'name': 'logs', 'command': 'tail -f x.log', 'cwd': '/tmp', 'env': {}}]
    f = fake_fleet(specs, mgr)
    tabs.open_all(f)
    tabs.open_all(f)  # only once per daemon
    assert cleared == ['shell', 'logs']
    assert mgr.calls == [('kill', 'old1'),
                         ('spawn', 'shell', 'env PORT=8895 /bin/echo a', {'type': 'tab', 'tab': 'shell'}, '/tmp'),
                         ('dock', 's1', 'history', None),
                         ('spawn', 'logs', 'tail -f x.log', {'type': 'tab', 'tab': 'logs'}, '/tmp'),
                         ('dock', 's2', 'history', 's1')]
    assert tabs.stop_all(f) and mgr.registered == {} and len(log_of(f, 'tab.stopped')) == 2


def test_open_all_waits_for_a_roster_and_skips_a_tab_it_cannot_clear(monkeypatch):
    mgr = Mgr()
    f = fake_fleet(None, mgr)
    tabs.open_all(f)
    assert not f.tabs_opened and not mgr.calls
    monkeypatch.setattr(tabs, 'clear', lambda f, spec: False)
    f.tab_specs = [{'name': 'shell', 'command': 'x', 'cwd': '/tmp', 'env': {}}]
    tabs.open_all(f)
    assert not mgr.calls and log_of(f, 'tab.start_failed')[0]['error'] == 'old process still running'


def test_the_fleet_tick_opens_the_tabs():
    import inspect
    assert "('tabs', lambda: tabs.open_all(self))" in inspect.getsource(fleet.Fleet.tick)


def test_manager_docks_after_control_then_after_the_previous_tab(tmp_path, monkeypatch):
    live = {'agents': {'sup': {'pane_id': 'left'}, 'history': {'pane_id': 'bottom'}, 'control': {'pane_id': 'bottom'},
                       't1': {'pane_id': 'left'}, 't2': {'pane_id': 'left'}}}
    calls = []

    def run(*args):
        calls.append(args)
        if args[0] == 'move-surface':
            live['agents'][args[args.index('--surface') + 1]] = {'pane_id': args[args.index('--pane') + 1]}
        return SimpleNamespace(returncode=0, stdout='OK', stderr='')
    monkeypatch.setattr(cmux_manager, '_run', run)
    mgr = cmux_manager.CmuxSessionManager('t', state_dir=tmp_path)
    monkeypatch.setattr(mgr, '_surfaces', lambda ws: {s: dict(v) for s, v in live.get(ws, {}).items()})
    mgr._save_meta({'workspace': 'agents', 'control_workspace': 'agents', 'surfaces': {
        'sup': {'type': 'supervisor'}, 'history': {'type': 'history'}, 'control': {'type': 'daemon'},
        't1': {'type': 'tab', 'tab': 'shell'}, 't2': {'type': 'tab', 'tab': 'logs'}, 'gone': {'type': 'tab', 'tab': 'x'}}})
    assert mgr.dock_tab('t1', 'history')
    assert mgr.dock_tab('t2', 'history', after='t1')
    assert [c for c in calls if c[0] == 'move-surface'] == [
        ('move-surface', '--workspace', 'agents', '--surface', 't1', '--pane', 'bottom', '--after', 'control',
         '--focus', 'false'),
        ('move-surface', '--workspace', 'agents', '--surface', 't2', '--pane', 'bottom', '--after', 't1',
         '--focus', 'false')]
    assert mgr.tab_panes() == {'t1': 'shell', 't2': 'logs'}


def test_spawn_window_registers_tab_metadata(tmp_path, monkeypatch):
    live = {}

    def run(*args):
        if args[:3] == ('--id-format', 'uuids', 'new-surface'):
            live['s9'] = {'pane_id': 'p1'}
            return SimpleNamespace(returncode=0, stdout='OK s9', stderr='')
        return SimpleNamespace(returncode=0, stdout='OK', stderr='')
    monkeypatch.setattr(cmux_manager, '_run', run)
    mgr = cmux_manager.CmuxSessionManager('t', state_dir=tmp_path)
    monkeypatch.setattr(mgr, '_surfaces', lambda ws: dict(live, sup={'pane_id': 'p1'}) if ws == 'agents' else {})
    mgr._save_meta({'workspace': 'agents', 'control_workspace': None, 'surfaces': {'sup': {'type': 'supervisor'}}})
    res = mgr.spawn_window('shell', 'true', metadata={'type': 'tab', 'tab': 'shell'}, start_directory=str(tmp_path))
    assert res.success and mgr._meta()['surfaces']['s9'] == {'type': 'tab', 'tab': 'shell', 'label': 'shell'}
