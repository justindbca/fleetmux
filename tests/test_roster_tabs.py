"""Companion tabs (docs/design.md §31): the roster ``tabs:`` section, tabs opened once after control, and the
program-by-command-line clear before each open. Fake fleet / fake ``_run``; the clear runs real processes."""
import json
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from fleetmux import cmux_manager, fleet, roster, tabs


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
    ('tabs:\n  - {name: a, command: x, respawn: yes}\n', 'keys backoff, command, crashloop, cwd, env, name, restart only'),
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


# ------------------------------------------------------------------ 4. restart: on-exit (keepalive), kit titles
def test_restart_on_exit_parses_with_defaults_and_overrides(tmp_path):
    got = parse(tmp_path, 'tabs:\n  - {name: a, command: run a, restart: on-exit}\n'
                          '  - {name: b, command: run b, restart: on-exit, backoff: {first_s: 2, max_s: 60},'
                          ' crashloop: {restarts: 3, minutes: 1.5}}\n  - {name: c, command: run c}\n')
    assert got[0]['restart'] == {'first_s': 5.0, 'max_s': 300.0, 'restarts': 5, 'window_s': 600.0}
    assert got[1]['restart'] == {'first_s': 2.0, 'max_s': 60.0, 'restarts': 3, 'window_s': 90.0}
    assert 'restart' not in got[2]  # no restart: the entry is exactly today's


@pytest.mark.parametrize('body,why', [
    ('tabs:\n  - {name: a, command: x, restart: never}\n', 'restart must be on-exit'),
    ('tabs:\n  - {name: a, command: x, restart: true}\n', 'restart must be on-exit'),
    ('tabs:\n  - {name: a, command: x, backoff: {first_s: 1}}\n', 'backoff need restart: on-exit'),
    ('tabs:\n  - {name: a, command: x, crashloop: {restarts: 2}}\n', 'crashloop need restart: on-exit'),
    ('tabs:\n  - {name: a, command: x, restart: on-exit, backoff: {first: 1}}\n', 'backoff must be a mapping'),
    ('tabs:\n  - {name: a, command: x, restart: on-exit, backoff: 5}\n', 'backoff must be a mapping'),
    ('tabs:\n  - {name: a, command: x, restart: on-exit, backoff: {first_s: 0}}\n', 'positive seconds'),
    ('tabs:\n  - {name: a, command: x, restart: on-exit, backoff: {first_s: 90, max_s: 60}}\n', 'first_s <= max_s'),
    ('tabs:\n  - {name: a, command: x, restart: on-exit, backoff: {max_s: true}}\n', 'positive seconds'),
    ('tabs:\n  - {name: a, command: x, restart: on-exit, crashloop: {restarts: 0}}\n', 'integer >= 1'),
    ('tabs:\n  - {name: a, command: x, restart: on-exit, crashloop: {restarts: 2.5}}\n', 'integer >= 1'),
    ('tabs:\n  - {name: a, command: x, restart: on-exit, crashloop: {minutes: -1}}\n', 'integer >= 1'),
])
def test_bad_restart_values_reject_the_whole_roster(tmp_path, body, why):
    with pytest.raises(ValueError, match=why):
        parse(tmp_path, body)


class Redis:
    """The few hash / string calls tabs.py makes, in memory."""
    def __init__(self, history='history'):
        self.h, self.kv, self.lists = {'fx:service': {'history_pane': history}}, {}, {}

    def hget(self, key, field):
        return self.h.get(key, {}).get(field)

    def hgetall(self, key):
        return dict(self.h.get(key, {}))

    def hset(self, key, field, value):
        self.h.setdefault(key, {})[field] = value

    def hdel(self, key, field):
        self.h.get(key, {}).pop(field, None)

    def set(self, key, value, ex=None):
        self.kv[key] = value

    def get(self, key):
        return self.kv.get(key)

    def lpush(self, key, value):
        self.lists.setdefault(key, []).insert(0, value)


class LiveMgr(Mgr):
    """Mgr whose spawned programs are 'running' until the test kills them."""
    def tab_panes(self):
        return dict(self.registered)


def keep_fleet(specs, cfg=None, v2=None):
    f = fake_fleet(specs, LiveMgr())
    f.r, f.cfg, f.v2, f.stopping, f.explicit_stop = Redis(), dict(cfg or {}), v2, False, False
    return f


def spec(name, restart=None, command=None):
    out = {'name': name, 'command': command or '/bin/prog --' + name, 'cwd': '/tmp', 'env': {}}
    if restart is not None:
        out['restart'] = dict({'first_s': 5.0, 'max_s': 40.0, 'restarts': 3, 'window_s': 600.0}, **restart)
    return out


@pytest.fixture
def procs(monkeypatch):
    """Running programs by command line; ps is never run for real."""
    running = set()
    monkeypatch.setattr(tabs, 'clear', lambda f, s: running.discard(s['command']) or True)
    monkeypatch.setattr(tabs, 'ps_lines', lambda: ['%d %s' % (100 + i, c) for i, c in enumerate(sorted(running))])
    monkeypatch.setattr(tabs, 'heads', lambda argv0: {argv0})
    return running


def opened(f, procs, now):
    """open_all at ``now`` (the programs start running)."""
    import time as _time
    real = _time.time
    tabs.time.time = lambda: now
    try:
        tabs.open_all(f)
    finally:
        tabs.time.time = real
    procs.update(st['spec']['command'] for st in f.tab_watch.values() if st['pane'])


def spawns(f):
    return [c for c in f.mgr.calls if c[0] == 'spawn']


def test_no_restart_key_is_never_reopened(procs):
    f = keep_fleet([spec('a')])
    opened(f, procs, 1000)
    procs.clear()  # the program exits
    for t in range(1000, 2000, 5):
        tabs.watch(f, now=t)
    assert len(spawns(f)) == 1 and not log_of(f, 'tab.down')


def test_on_exit_reopens_after_the_backoff_with_the_clear_first(procs, monkeypatch):
    order = []
    monkeypatch.setattr(tabs, 'clear', lambda f, s: order.append('clear') or procs.discard(s['command']) or True)
    f = keep_fleet([spec('a', {})])
    opened(f, procs, 1000)
    tabs.watch(f, now=1005)  # inside the start grace: no scan verdict
    procs.clear()
    tabs.watch(f, now=1009)
    assert not log_of(f, 'tab.down')  # still within GRACE_S of the open
    tabs.watch(f, now=1011)
    assert log_of(f, 'tab.down')[-1]['restart_in_s'] == 5.0 and log_of(f, 'tab.down')[-1]['reason'] == 'exited'
    tabs.watch(f, now=1015)
    assert len(spawns(f)) == 1  # waiting for the backoff
    order.clear()
    tabs.watch(f, now=1016)
    assert len(spawns(f)) == 2 and order == ['clear']
    assert ('kill', 's1') in f.mgr.calls  # the old surface (still shown) closed before reopening
    started = log_of(f, 'tab.started')[-1]
    assert started['reason'] == 'exited' and started['attempt'] == 1 and started['pane'] == 's2'


def test_backoff_doubles_to_the_cap_and_resets_after_a_long_run(procs):
    f = keep_fleet([spec('a', {'restarts': 99, 'window_s': 600.0})])
    opened(f, procs, 0)
    st, now, delays = f.tab_watch['a'], 0, []
    for _ in range(6):
        procs.clear()
        now = st['started'] + 11
        f.tab_next_check = 0
        tabs.watch(f, now=now)
        delays.append(st['delay'])
        tabs.watch(f, now=st['next'])
        procs.add(st['spec']['command'])
    assert delays == [5.0, 10.0, 20.0, 40.0, 40.0, 40.0]
    procs.clear()  # this time it ran longer than the crash-loop window: back to first_s
    f.tab_next_check = 0
    tabs.watch(f, now=st['started'] + 700)
    assert st['delay'] == 5.0


class V2:
    def __init__(self):
        self.cards = []

    def ui(self, slot, card):
        self.cards.append((slot, card))


def test_crashloop_stops_reopening_with_a_card_and_a_channel_push(procs):
    v2 = V2()
    f = keep_fleet([spec('rollout', {'restarts': 3, 'window_s': 600.0})],
                   cfg={'run': '/runs/r1', 'name': 'ops', 'kit_fleet': {'name': 'ops'}, 'mode': 'service',
                        'channels': [{'type': 'telegram', 'account': 'bot-1'}]}, v2=v2)
    f.service = True
    opened(f, procs, 0)
    st = f.tab_watch['rollout']
    for _ in range(3):
        procs.clear()
        f.tab_next_check = 0
        tabs.watch(f, now=st['started'] + 11)
        tabs.watch(f, now=st['next'])
        procs.add(st['spec']['command'])
    assert len(spawns(f)) == 4 and not log_of(f, 'tab.crashloop')
    procs.clear()
    f.tab_next_check = 0
    tabs.watch(f, now=st['started'] + 11)
    loop = log_of(f, 'tab.crashloop')
    assert len(loop) == 1 and loop[0]['restarts'] == 3 and loop[0]['title'] == 'kit-1-rollout'
    assert st['state'] == 'crashloop'
    assert 'fleetmux tab restart rollout --config /runs/r1/config.json' in loop[0]['line']
    (slot, card), = v2.cards
    assert slot == 'supervisor' and card['metadata']['event_id'].startswith('ui-tab-crashloop-')
    from fleetmux import channels
    item, = [json.loads(x) for x in f.r.lists[channels.OUTQ]]
    assert item['event'] == 'tab_crashloop' and item['target'] == 'last' and 'kit-1-rollout' in item['text']
    for t in range(int(st['started']) + 11, int(st['started']) + 3000, 5):  # never reopened again
        tabs.watch(f, now=t)
    assert len(spawns(f)) == 4 and len(log_of(f, 'tab.crashloop')) == 1


def test_crashloop_without_v2_or_channels_is_audit_only(procs):
    f = keep_fleet([spec('a', {'restarts': 1})])
    opened(f, procs, 0)
    st = f.tab_watch['a']
    procs.clear()
    tabs.watch(f, now=11)
    tabs.watch(f, now=st['next'])
    procs.clear()
    f.tab_next_check = 0
    tabs.watch(f, now=st['started'] + 11)
    assert st['state'] == 'crashloop' and not f.r.lists and not log_of(f, 'tab.notice_failed')


def test_tab_restart_request_resets_the_crashloop_and_reopens(procs):
    f = keep_fleet([spec('a', {'restarts': 1})], cfg={'run': '/runs/r1'})
    opened(f, procs, 0)
    st = f.tab_watch['a']
    st.update(state='crashloop', restarts=[5.0], delay=40.0)
    procs.clear()
    clock = iter([100.0, 100.0, 100.0, 100.0])
    sent = []

    def sleep(_):  # the daemon's tick runs while the CLI waits
        sent.append(1)
        tabs.watch(f, now=100.0)
    ans = tabs.request_restart(f.r, 'a', wait=30, clock=lambda: next(clock, 100.0), sleep=sleep)
    assert ans == {'status': 'accepted', 'tab': 'a', 'title': 'a', 'pane': 's2'}
    assert st['state'] == 'running' and st['restarts'] == [] and st['delay'] is None
    assert log_of(f, 'tab.restart_requested')[0]['was'] == 'crashloop'
    assert log_of(f, 'tab.started')[-1]['reason'] == 'operator' and not f.r.hgetall(tabs.REQ)


def test_tab_restart_refuses_unknown_and_stale_requests(procs):
    f = keep_fleet([spec('a', {})])
    opened(f, procs, 0)
    f.r.hset(tabs.REQ, 'zz', json.dumps({'id': 'r1', 'at': 150}))
    f.r.hset(tabs.REQ, 'a', json.dumps({'id': 'r2', 'at': 1}))
    tabs.watch(f, now=200)
    assert 'opened no tab zz' in json.loads(f.r.get(tabs.REPLY + 'r1'))['reason']
    assert 'stale' in json.loads(f.r.get(tabs.REPLY + 'r2'))['reason']
    assert len(spawns(f)) == 1


def test_stopping_or_stopped_fleet_never_reopens(procs):
    for how in ('stopping', 'explicit_stop', 'stop_all'):
        f = keep_fleet([spec('a', {})])
        opened(f, procs, 0)
        procs.clear()
        if how == 'stop_all':
            tabs.stop_all(f)
        else:
            setattr(f, how, True)
        for t in range(11, 500, 5):
            tabs.watch(f, now=t)
        assert len(spawns(f)) == 1, how


def test_failed_first_open_of_a_keepalive_tab_is_retried(procs, monkeypatch):
    results = iter([False, True])
    monkeypatch.setattr(tabs, 'clear', lambda f, s: next(results))
    f = keep_fleet([spec('a', {})])
    opened(f, procs, 0)
    st = f.tab_watch['a']
    assert st['state'] == 'waiting' and log_of(f, 'tab.down')[0]['reason'] == 'start_failed'
    tabs.watch(f, now=st['next'])
    assert st['state'] == 'running' and len(spawns(f)) == 1


def test_reopened_tab_docks_back_after_the_previous_tab_in_roster_order(procs):
    f = keep_fleet([spec('a'), spec('b', {}), spec('c')])
    opened(f, procs, 0)
    procs.discard(f.tab_watch['b']['spec']['command'])
    tabs.watch(f, now=11)
    tabs.watch(f, now=f.tab_watch['b']['next'])
    assert f.mgr.calls[-1] == ('dock', 's4', 'history', 's1')  # after a, the earlier tab still shown


def test_a_failed_ps_scan_gives_no_verdict(procs, monkeypatch):
    f = keep_fleet([spec('a', {})])
    opened(f, procs, 0)
    monkeypatch.setattr(tabs, 'ps_lines', lambda: (_ for _ in ()).throw(RuntimeError('ps failed')))
    tabs.watch(f, now=11)
    f.tab_next_check = 0
    tabs.watch(f, now=20)
    assert len(log_of(f, 'tab.check_failed')) == 1 and not log_of(f, 'tab.down')


def test_kit_fleet_titles_kit_n_name_and_keeps_the_identity(procs):
    f = keep_fleet([spec('rollout'), spec('shell')], cfg={'kit_fleet': {'name': 'rollout'}})
    opened(f, procs, 0)
    assert [(c[1], c[3]) for c in spawns(f)] == [('kit-1-rollout', {'type': 'tab', 'tab': 'rollout'}),
                                                  ('kit-2-shell', {'type': 'tab', 'tab': 'shell'})]
    assert [d['title'] for d in log_of(f, 'tab.started')] == ['kit-1-rollout', 'kit-2-shell']
    assert [d['tab'] for d in log_of(f, 'tab.started')] == ['rollout', 'shell']


def test_non_kit_fleet_titles_stay_the_name(procs):
    f = keep_fleet([spec('rollout')])
    opened(f, procs, 0)
    assert spawns(f)[0][1] == 'rollout' and 'title' not in log_of(f, 'tab.started')[0]


def test_the_fleet_tick_watches_the_tabs_after_opening():
    import inspect
    src = inspect.getsource(fleet.Fleet.tick)
    assert src.index('tabs.open_all(self)') < src.index('tabs.watch(self)')


def test_cli_tab_restart_checks_the_roster_first(tmp_path, monkeypatch):
    path = tmp_path / 'r.yaml'
    path.write_text('workers:\n  - name: w-1\ntabs:\n  - {name: a, command: x, restart: on-exit}\n')
    monkeypatch.setattr(fleet, 'connect', lambda cfg: pytest.fail('no Redis for an undeclared tab'))
    assert 'declares no tab b' in fleet.tab_restart({'roster': str(path)}, 'b')['reason']
    assert fleet.tab_restart({}, 'a')['status'] == 'rejected'
    seen = {}

    class R(Redis):
        def close(self):
            seen['closed'] = True
        connection_pool = SimpleNamespace(disconnect=lambda: None)
    monkeypatch.setattr(fleet, 'connect', lambda cfg: R())
    monkeypatch.setattr(tabs, 'request_restart', lambda r, name, wait: seen.setdefault('req', (name, wait)) and
                        {'status': 'accepted'})
    assert fleet.tab_restart({'roster': str(path)}, 'a', 7) == {'status': 'accepted'} and seen['req'] == ('a', 7)
