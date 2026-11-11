"""fleetmux sidebar payload + daemon tick (plan fleetmux-sidebar-20261029 §4.5).

No fleetmux server, no Redis, no daemon: the fake fleet carries exactly what ``payload()`` reads
(``cfg``, ``workers``, ``agent()``, ``task()``, ``paused()``, ``stopping``, ``service``, ``mgr``) and the
fake manager records kv writes. ``subfleet.tree`` is monkeypatched, so no run directory is scanned.
"""
import inspect
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fleetmux import fleet, fleetmux_sidebar, subfleet  # noqa: E402
from fleetmux.backends import fleetmux as fm  # noqa: E402

RUN = 'fleet-20261029-231820-ab12cd34'


class FakeMgr:
    """``kv_get`` answers what was written (a fresh server has nothing); ``writes`` is the kv.set log."""

    def __init__(self):
        self.kv = {}
        self.writes = []

    def kv_get(self, key):
        return self.kv.get(key)

    def _kv_set(self, key, value):
        self.kv[key] = value
        self.writes.append((key, value))
        return True


class TrapMgr:
    """Any attribute access is a failure: a non-fleetmux tick must not touch ``f.mgr`` at all."""

    def __getattr__(self, name):
        raise AssertionError('the sidebar tick touched mgr.%s on a non-fleetmux fleet' % name)


class FakeFleet:
    def __init__(self, run, workers=('worker-1', 'worker-2'), busy=('worker-1',), backend='fleetmux',
                 fleet_home=None, paused=False, stopping=False, service=False, goal='', name=None):
        self.cfg = {'run': str(run), 'workers': list(workers), 'backend': backend}
        if fleet_home is not None:
            self.cfg['fleet_home'] = str(fleet_home)
        if name is not None:
            self.cfg['name'] = name
        self.workers = list(workers)
        self.busy = set(busy)
        self._paused = paused
        self.stopping = stopping
        self.service = service
        self._goal = goal
        self.mgr = FakeMgr()

    def agent(self, name):
        return {'state': 'busy' if name in self.busy else 'idle'}

    def task(self):
        return {'goal': self._goal} if self._goal else {}

    def paused(self):
        return self._paused


def run_dir(tmp_path, name=RUN):
    return tmp_path / 'runs' / name


def shown_path(path):
    """The card's public path rule, written out here (not the module's helper): the operator's home is ``~``, a
    path under it is ``~/<the rest>``, any other path is shown in full. pytest's tmp_path may or may not sit under
    HOME (the m4 gate keeps it in ``~/.m4e/...``), so the expected text follows where it really is."""
    home, p = Path.home(), Path(path)
    if p == home:
        return '~'
    try:
        return '~/' + p.relative_to(home).as_posix()
    except ValueError:
        return str(p)


def test_payload_root_only(tmp_path, monkeypatch):
    monkeypatch.setattr(subfleet, 'tree', lambda cfg: {})
    run = run_dir(tmp_path)
    f = FakeFleet(run, name='fleetmux-demo')
    p = fleetmux_sidebar.payload(f)
    assert p == {
        'v': 1,
        'current': RUN,
        'fleets': [{
            'id': RUN,
            'title': 'fleetmux-demo',
            'state': 'running',
            'line1': '运行中 · 监工+2 worker（1忙）',
            'line2': shown_path(tmp_path),
        }],
        'close': {
            'title': '关闭整个 fleet「fleetmux-demo」？',
            'body': '会停掉监工、2 个 worker。正在做的活会中断。运行记录保留，之后可以在侧栏点 ↺ 恢复，接着原来的活。',
        },
    }, p


def test_payload_line2_shows_the_home_as_tilde_and_other_paths_in_full(tmp_path, monkeypatch):
    """The rule behind ``shown_path``, with HOME pinned: at the home, under it, outside it, and a look-alike prefix."""
    monkeypatch.setattr(subfleet, 'tree', lambda cfg: {})
    home = tmp_path / 'op'
    cases = [
        (home, '~'),
        (home / 'fleets', '~/fleets'),
        (tmp_path / 'elsewhere', str(tmp_path / 'elsewhere')),
        (tmp_path / 'op-other', str(tmp_path / 'op-other')),  # shares the home's prefix, is not under it
    ]
    monkeypatch.setenv('HOME', str(home))
    for fleet_home, want in cases:
        line2 = fleetmux_sidebar.payload(FakeFleet(run_dir(fleet_home)))['fleets'][0]['line2']
        assert line2 == want, (fleet_home, line2)


def test_payload_paused_and_stopping(tmp_path, monkeypatch):
    monkeypatch.setattr(subfleet, 'tree', lambda cfg: {})
    paused = fleetmux_sidebar.payload(FakeFleet(run_dir(tmp_path), workers=('worker-1',), busy=(), paused=True))
    assert paused['fleets'][0]['state'] == 'paused', paused
    assert paused['fleets'][0]['line1'] == '已暂停 · 监工+1 worker（0忙）', paused
    stopping = fleetmux_sidebar.payload(
        FakeFleet(run_dir(tmp_path), workers=('worker-1', 'worker-2'), busy=('worker-1', 'worker-2'),
                  stopping=True))
    assert stopping['fleets'][0]['state'] == 'stopping', stopping
    assert stopping['fleets'][0]['line1'] == '正在关闭 · 监工+2 worker（2忙）', stopping


def test_payload_children_are_flat_preorder(tmp_path, monkeypatch):
    tree = {
        'M0': {'run': str(tmp_path / 'runs' / 'fleet-child-a'), 'title': 'sub A', 'state': 'running',
               'slot': 'worker-1', 'workers': 2,
               'subfleets': {'M0.1': {'run': str(tmp_path / 'runs' / 'fleet-grand-b'), 'title': '',
                                      'state': 'stopped', 'outcome': 'ok', 'workers': ['w1', 'w2', 'w3']}}},
        'M1': {'run': str(tmp_path / 'runs' / 'fleet-child-c'), 'state': 'running', 'slot': 'worker-2',
               'workers': ['w1']},
    }
    monkeypatch.setattr(subfleet, 'tree', lambda cfg: tree)
    p = fleetmux_sidebar.payload(FakeFleet(run_dir(tmp_path), name='fleetmux-demo'))
    assert [x['id'] for x in p['fleets']] == [RUN, 'fleet-child-a', 'fleet-grand-b', 'fleet-child-c'], p
    assert [x['state'] for x in p['fleets']] == ['running', 'running', 'reclaimed', 'running'], p
    assert [x['title'] for x in p['fleets']] == ['fleetmux-demo', 'sub A', 'M0.1', 'M1'], p
    assert p['fleets'][1]['line1'] == '运行中 · 2 worker', p
    assert p['fleets'][1]['line2'] == '由 worker-1 委派', p
    assert p['fleets'][2]['line1'] == '已收回 · 结局 ok', p
    assert p['fleets'][2]['line2'] == '只读，可看记录', p
    assert p['fleets'][3]['line2'] == '由 worker-2 委派', p


def test_payload_caps_at_16(tmp_path, monkeypatch):
    tree = {'M%d' % i: {'run': str(tmp_path / 'runs' / ('fleet-c%d' % i)), 'state': 'running',
                        'slot': 'worker-1', 'workers': 1} for i in range(20)}
    monkeypatch.setattr(subfleet, 'tree', lambda cfg: tree)
    p = fleetmux_sidebar.payload(FakeFleet(run_dir(tmp_path)))
    assert len(p['fleets']) == 16, p
    assert p['fleets'][0]['id'] == RUN, p
    assert p['current'] == RUN, p


def test_tick_pushes_only_when_changed(tmp_path, monkeypatch):
    monkeypatch.setattr(subfleet, 'tree', lambda cfg: {})
    f = FakeFleet(run_dir(tmp_path), name='fleetmux-demo')
    fleetmux_sidebar.tick(f, now=0.0)
    assert [k for k, _ in f.mgr.writes] == ['sidebar'], f.mgr.writes
    pushed = json.loads(f.mgr.writes[0][1])
    assert pushed['fleets'][0]['line1'] == '运行中 · 监工+2 worker（1忙）', pushed
    assert f.mgr.writes[0][1] == json.dumps(pushed, ensure_ascii=False, separators=(',', ':')), \
        f.mgr.writes[0][1]  # compact, ensure_ascii=False
    fleetmux_sidebar.tick(f, now=2.0)  # same cards: compare first, so no second kv.set
    assert len(f.mgr.writes) == 1, f.mgr.writes
    f.busy = set()  # a worker went idle: the card text changed
    fleetmux_sidebar.tick(f, now=3.0)
    assert len(f.mgr.writes) == 2, f.mgr.writes
    assert json.loads(f.mgr.writes[1][1])['fleets'][0]['line1'] == '运行中 · 监工+2 worker（0忙）', f.mgr.writes


def test_tick_is_throttled_to_one_second(tmp_path, monkeypatch):
    monkeypatch.setattr(subfleet, 'tree', lambda cfg: {})
    f = FakeFleet(run_dir(tmp_path), name='fleetmux-demo')
    fleetmux_sidebar.tick(f, now=100.0)
    assert len(f.mgr.writes) == 1, f.mgr.writes
    assert f._fmx_sidebar_at == 100.0, f._fmx_sidebar_at
    f.busy = set()
    fleetmux_sidebar.tick(f, now=100.5)  # inside the second: nothing is written, the clock does not move
    assert len(f.mgr.writes) == 1, f.mgr.writes
    assert f._fmx_sidebar_at == 100.0, f._fmx_sidebar_at
    fleetmux_sidebar.tick(f, now=101.0)
    assert len(f.mgr.writes) == 2, f.mgr.writes
    assert f._fmx_sidebar_at == 101.0, f._fmx_sidebar_at


def test_tick_skips_other_backends_without_touching_mgr(tmp_path, monkeypatch):
    monkeypatch.setattr(subfleet, 'tree', lambda cfg: {})
    f = FakeFleet(run_dir(tmp_path), backend='cmux')
    f.mgr = TrapMgr()
    assert fleetmux_sidebar.tick(f, now=0.0) is None
    assert not hasattr(f, '_fmx_sidebar_at'), 'a non-fleetmux tick must not record the throttle time'


def test_kv_get_returns_value_or_none(tmp_path, monkeypatch):
    mgr = fm.FleetmuxSessionManager('cmux-fleet', state_dir=tmp_path)
    seen = []

    def ok(cmd, **args):
        seen.append((cmd, args))
        return {'value': 'x'}

    monkeypatch.setattr(mgr, '_call', ok)
    assert mgr.kv_get('sidebar') == 'x'
    assert seen == [('kv.get', {'key': 'sidebar'})], seen

    def down(cmd, **args):
        raise fm.FleetmuxError('no server', code='internal')

    monkeypatch.setattr(mgr, '_call', down)
    assert mgr.kv_get('sidebar') is None
    monkeypatch.setattr(mgr, '_call', lambda cmd, **args: {'value': None})
    assert mgr.kv_get('sidebar') is None
    monkeypatch.setattr(mgr, '_call', lambda cmd, **args: {'value': 5})
    assert mgr.kv_get('sidebar') is None


def test_fleet_tick_runs_the_sidebar_part():
    assert fleet.fleetmux_sidebar is fleetmux_sidebar
    src = inspect.getsource(fleet.Fleet.tick)
    assert 'fleetmux_sidebar.tick(self)' in src, src


# ------------------------------------------------------------------ user 2026-11-09: sidebar bash tabs in the worker area
class ShellMgr(FakeMgr):
    """A fake manager whose ``adopt_shells`` answers ``changes`` in turn (then: no change) and counts its calls;
    ``shell_slots`` answers ``slots`` (the registered bash tabs)."""

    def __init__(self, changes):
        super().__init__()
        self.changes = list(changes)
        self.adopted = 0
        self.slots = [('zzzz-3', 'bash-1', 'bash-1')]

    def adopt_shells(self):
        self.adopted += 1
        return self.changes.pop(0) if self.changes else False

    def shell_slots(self):
        return list(self.slots)


def _shell_fleet(tmp_path, changes):
    f = FakeFleet(run_dir(tmp_path), name='fleetmux-demo')
    f.mgr = ShellMgr(changes)
    f.run_dir = run_dir(tmp_path)
    f.history_slots = lambda: [('supervisor', 'supervisor-1', '监工'), ('zzzz-3', 'bash-1', 'bash-1')]
    f.history_links = lambda: None
    return f


def test_tick_adopts_bash_tabs_and_rewrites_the_history_page_on_a_change(tmp_path, monkeypatch):
    """Every new sidebar bash is mapped into the main fleet's worker area and managed by it: the root daemon's tick
    takes it into the registry (``adopt_shells``) and, when the set changed, rewrites the history page."""
    monkeypatch.setattr(subfleet, 'tree', lambda cfg: {})
    written = []
    monkeypatch.setattr(fleetmux_sidebar.history_page, 'write_page',
                        lambda run, slots, links=None: written.append((run, slots, links)))
    f = _shell_fleet(tmp_path, [True])
    fleetmux_sidebar.tick(f, now=0.0)
    assert f.mgr.adopted == 1, 'the root tick did not take the sidebar bash tabs in'
    assert written == [(f.run_dir, f.history_slots(), None)], written
    fleetmux_sidebar.tick(f, now=2.0)
    assert f.mgr.adopted == 2
    assert len(written) == 1, 'the history page was rewritten although no bash came or went'


def test_tick_lists_bash_tabs_arrange_took_in_before_the_first_tick(tmp_path, monkeypatch):
    """start_all writes the history page before ``arrange`` takes a bash opened while no fleet ran into the registry,
    so the first tick finds nothing new to adopt: it must still rewrite the page once (the registered set is new to
    it), and again only when that set changes."""
    monkeypatch.setattr(subfleet, 'tree', lambda cfg: {})
    written = []
    monkeypatch.setattr(fleetmux_sidebar.history_page, 'write_page',
                        lambda run, slots, links=None: written.append(slots))
    f = _shell_fleet(tmp_path, [])  # adopt_shells: no change (arrange already took the bash in)
    fleetmux_sidebar.tick(f, now=0.0)
    assert len(written) == 1, 'the first tick did not list the bash arrange took in on the history page'
    fleetmux_sidebar.tick(f, now=2.0)
    assert len(written) == 1, 'the page was rewritten with the same bash tabs'
    f.mgr.slots = []  # the bash went away
    fleetmux_sidebar.tick(f, now=4.0)
    assert len(written) == 2, 'a bash that went away was not taken off the history page'


def test_a_sub_fleet_tick_never_adopts_bash_tabs(tmp_path, monkeypatch):
    monkeypatch.setattr(subfleet, 'tree', lambda cfg: {})
    f = _shell_fleet(tmp_path, [True])
    f.cfg['parent_config'] = str(tmp_path / 'parent.json')
    fleetmux_sidebar.tick(f, now=0.0)
    assert f.mgr.adopted == 0, 'a sub-fleet daemon took the root page bash tabs in'


def test_history_slots_list_the_registered_bash_tabs(tmp_path):
    """The history page lists every registered bash tab (slot = pane id, shown as its title) after the agents."""
    from types import SimpleNamespace
    ns = SimpleNamespace(run_dir=tmp_path, names=['supervisor', 'worker-1'], label=lambda n: n + '-live',
                         mgr=SimpleNamespace(shell_slots=lambda: [('zzzz-3', 'bash-1', 'bash-1')]))
    slots = fleet.Fleet.history_slots(ns)
    assert [s[0] for s in slots] == ['supervisor', 'worker-1', 'zzzz-3'], slots
    assert slots[-1] == ('zzzz-3', 'bash-1', 'bash-1'), slots
    ns.mgr = SimpleNamespace()  # another backend: no bash tabs, nothing added
    assert [s[0] for s in fleet.Fleet.history_slots(ns)] == ['supervisor', 'worker-1']


# ------------------------------------------------------------------ S2: the sidebar stop request
SIDEBAR_REASON = 'fleetmux 侧栏「关闭整个 fleet」'


def stop_request(**fields):
    return json.dumps(dict({'v': 1}, **fields), ensure_ascii=False)


def request_ready(f):
    """A fleet the tick may close: ``lifecycle_close`` starts empty and ``request_close`` can log."""
    f.lifecycle_close = None
    f.explicit_stop = False
    f.log = lambda *a, **k: None
    return f


def test_payload_has_the_close_text(tmp_path, monkeypatch):
    tree = {
        'M0': {'run': str(tmp_path / 'runs' / 'fleet-child-a'), 'state': 'running', 'slot': 'worker-1',
               'workers': 2},
        'M1': {'run': str(tmp_path / 'runs' / 'fleet-child-c'), 'state': 'running', 'slot': 'worker-2',
               'workers': ['w1']},
    }
    monkeypatch.setattr(subfleet, 'tree', lambda cfg: tree)
    p = fleetmux_sidebar.payload(FakeFleet(run_dir(tmp_path), name='fleetmux-demo'))
    assert p['close'] == {
        'title': '关闭整个 fleet「fleetmux-demo」？',
        'body': ('会停掉监工、2 个 worker，以及它的子 fleet M0（含 2 个 worker）、M1（含 1 个 worker）'
                 '。正在做的活会中断。运行记录保留，之后可以在侧栏点 ↺ 恢复，接着原来的活。'),
    }, p.get('close')


def test_stop_request_calls_request_close_resumable(tmp_path, monkeypatch):
    monkeypatch.setattr(subfleet, 'tree', lambda cfg: {})
    f = request_ready(FakeFleet(run_dir(tmp_path), name='fleetmux-demo'))
    f.cfg['lifecycle'] = 'dev_task'  # lifecycle_close.managed() is true for this run
    f.v2 = object()
    f.mgr.kv['stop_request'] = stop_request(state='requested')
    fleetmux_sidebar.tick(f, now=0.0)
    closing = f.lifecycle_close or {}
    assert closing.get('closed_by') == 'user', closing
    assert closing.get('reason') == SIDEBAR_REASON, closing
    assert closing.get('resumable') is True, closing
    assert closing.get('via') == 'fleetmux-sidebar', closing
    assert f.stopping is True and f.explicit_stop is True, (f.stopping, f.explicit_stop)
    assert json.loads(f.mgr.kv['stop_request']) == {'v': 1, 'state': 'accepted'}, f.mgr.kv['stop_request']
    assert [k for k, _ in f.mgr.writes].count('stop_request') == 1, f.mgr.writes


def test_stop_request_refused_when_unmanaged(tmp_path, monkeypatch):
    monkeypatch.setattr(subfleet, 'tree', lambda cfg: {})
    f = request_ready(FakeFleet(run_dir(tmp_path)))  # no cfg['lifecycle']: managed() is false
    f.mgr.kv['stop_request'] = stop_request(state='requested')
    fleetmux_sidebar.tick(f, now=0.0)
    assert json.loads(f.mgr.kv['stop_request']) == {
        'v': 1, 'state': 'refused', 'reason': '这个 run 没有 lifecycle，请在终端跑 start.sh stop'}, \
        f.mgr.kv['stop_request']
    assert f.lifecycle_close is None and f.stopping is False, (f.lifecycle_close, f.stopping)


def test_stop_request_is_handled_once(tmp_path, monkeypatch):
    monkeypatch.setattr(subfleet, 'tree', lambda cfg: {})
    f = request_ready(FakeFleet(run_dir(tmp_path)))
    f.cfg['lifecycle'] = 'dev_task'
    f.v2 = object()
    f.mgr.kv['stop_request'] = stop_request(state='requested')
    fleetmux_sidebar.tick(f, now=0.0)
    closing = dict(f.lifecycle_close or {})
    assert closing.get('via') == 'fleetmux-sidebar', closing
    f.mgr.writes.clear()
    fleetmux_sidebar.tick(f, now=2.0)  # the stop was requested: the second tick must not ask again
    assert [k for k, _ in f.mgr.writes if k == 'stop_request'] == [], f.mgr.writes
    assert f.lifecycle_close == closing, f.lifecycle_close


# ------------------------------------------------------------------ S2b: the daemon clears kv restorable
def test_tick_clears_restorable_when_the_daemon_is_back(tmp_path, monkeypatch):
    monkeypatch.setattr(subfleet, 'tree', lambda cfg: {})
    f = FakeFleet(run_dir(tmp_path), name='fleetmux-demo')
    f.mgr.kv['restorable'] = json.dumps({'v': 1, 'runs': [{'id': RUN, 'title': 'fleetmux-demo'}]})
    fleetmux_sidebar.tick(f, now=0.0)
    assert f.mgr.kv['restorable'] == '', 'a live daemon\'s cards replace the closed cards: %r' % (f.mgr.writes,)
    assert [k for k, _ in f.mgr.writes] == ['restorable', 'sidebar'], f.mgr.writes


# ------------------------------------------------------------------ S2b ⑤: the docs carry the restore (kv restorable, C-b r)
DOCS = Path(__file__).resolve().parents[1]


def _doc(*parts):
    return DOCS.joinpath(*parts).read_text(encoding='utf-8')


def _lines_with(text, needle):
    return '\n'.join(line for line in text.splitlines() if needle in line) or '(no line with %r)' % needle


def test_protocol_reserves_kv_restorable():
    proto = _doc('rust', 'PROTOCOL.md')
    start = proto.index('**Reserved keys.**')
    reserved = proto[start:proto.index('### 4.3', start)]
    assert '- `restorable`' in reserved, \
        'PROTOCOL §4.2 Reserved keys must list kv `restorable`; bullets were:\n' + \
        '\n'.join(l for l in reserved.splitlines() if l.startswith('- `'))
    for key in ('"runs"', '"closed_at"', '"argv"', '"cwd"', '"env"'):
        assert key in reserved, 'kv restorable must document %s (§18.3):\n%s' % (key, reserved)
    assert '`restorable`' in reserved and 'empty string' in reserved, \
        'kv restorable must say the daemon clears it with an empty string:\n' + reserved


def test_protocol_key_table_documents_the_restore_key():
    proto = _doc('rust', 'PROTOCOL.md')
    start = proto.index('## 10. Keys')
    table = proto[start:proto.index('## 11. Mouse', start)]
    row = [line for line in table.splitlines() if line.startswith('| `r` |')]
    assert row, 'PROTOCOL §10 must bind the prefix key `r`; table rows were:\n' + \
        '\n'.join(line for line in table.splitlines() if line.startswith('| `'))
    assert 'restorable' in row[0] and '没有可恢复的 fleet' in row[0], \
        'the `r` row must name kv restorable and the nothing-to-restore notice, got:\n' + row[0]


def test_the_docs_describe_the_real_restore_not_the_s2b_placeholder():
    for path in (('rust', 'PROTOCOL.md'), ('README.md',)):
        text = _doc(*path)
        assert '恢复已关闭的 fleet：S2b 开放' not in text, \
            '%s still carries the S2b placeholder notice:\n%s' % ('/'.join(path), _lines_with(text, 'S2b 开放'))
        assert '没有可恢复的 fleet' in text, \
            '%s must name the nothing-to-restore notice `没有可恢复的 fleet`:\n%s' % (
                '/'.join(path), _lines_with(text, '↺'))
        assert 'restorable' in text, \
            '%s must say the restore reads kv restorable:\n%s' % ('/'.join(path), _lines_with(text, '↺'))


# ------------------------------------------------------------------ S2c ②: the history cards in the sidebar (§18.4-4)
def history_run(tmp_path, name, *, state_dir='state', closed_via='fleetmux-sidebar',
                closed_by='user', resumable=True, closed_at=1790000000, home='home'):
    """One run of the ↺ history under ``<tmp_path>/<home>/runs/``: config.json + result.json (§18.3)."""
    run = tmp_path / home / 'runs' / name
    run.mkdir(parents=True, exist_ok=True)
    fleet.save(run / 'config.json', {
        'run': str(run), 'python': '/py/bin/python', 'backend': 'fleetmux', 'name': 'fmx-' + name,
        'fleet_home': str(tmp_path / home), 'cmux_state_dir': str(tmp_path / state_dir),
        'private_config': 'none'})
    res = {'status': 'running', 'goal': 'ship it', 'resumable': resumable,
           'closed_by': closed_by, 'closed_at': closed_at}
    if closed_via is not None:
        res['closed_via'] = closed_via
    fleet.save(run / 'result.json', res)
    return run


def history_fleet(tmp_path):
    """A fleetmux fleet whose run sits in its own home, with this HOME's state dir pinned in the config."""
    me = tmp_path / 'home' / 'runs' / 'fleet-me'
    me.mkdir(parents=True)
    f = FakeFleet(me, fleet_home=tmp_path / 'home')
    f.cfg['cmux_state_dir'] = str(tmp_path / 'state')
    return f


def test_payload_appends_history_cards_after_this_fleet(tmp_path, monkeypatch):
    """§18.4-4: the closed runs come after this run's card, this run never appears twice (§18.6 texts)."""
    monkeypatch.setattr(subfleet, 'tree', lambda cfg: {})
    monkeypatch.setattr(fleet, 'daemon_running', lambda run: False)
    f = history_fleet(tmp_path)
    fleet.save(Path(f.cfg['run']) / 'result.json', {  # this run is resumable too: it must still be excluded
        'status': 'running', 'goal': '', 'resumable': True, 'closed_by': 'user',
        'closed_via': 'fleetmux-sidebar', 'closed_at': 1790000000})
    history_run(tmp_path, 'fleet-old', closed_at=1790000000)
    history_run(tmp_path, 'fleet-timeout', closed_by='timeout', closed_via=None, closed_at=1789000000)

    p = fleetmux_sidebar.payload(f)

    assert [c['id'] for c in p['fleets']] == ['fleet-me', 'fleet-old', 'fleet-timeout'], \
        'history cards follow this fleet\'s card, this run once (§18.4-4): %r' % (p['fleets'],)
    assert [c['state'] for c in p['fleets']] == ['running', 'closed', 'closed'], p['fleets']
    assert [c['line1'] for c in p['fleets'][1:]] == ['已关闭 · 可恢复', '到点停 · 可恢复'], \
        'sidebar-closed and timeout runs read differently (§18.6): %r' % (p['fleets'],)
    assert p['fleets'][1]['line2'].startswith('关闭于 '), p['fleets'][1]
    assert p['current'] == 'fleet-me', 'current stays this run while its daemon pushes: %r' % (p,)


def test_payload_history_uses_the_cfg_home_and_state_dir(tmp_path, monkeypatch):
    """§18.7-3: the scan reads cfg fleet_home / cmux_state_dir only — never the environment."""
    monkeypatch.setattr(subfleet, 'tree', lambda cfg: {})
    monkeypatch.setattr(fleet, 'daemon_running', lambda run: False)
    f = history_fleet(tmp_path)
    history_run(tmp_path, 'fleet-ok')
    history_run(tmp_path, 'fleet-other-state', state_dir='other-state')
    history_run(tmp_path, 'fleet-elsewhere', home='elsewhere')
    monkeypatch.setenv('FLEETMUX_HOME', str(tmp_path / 'elsewhere'))
    monkeypatch.setenv('FLEETMUX_STATE', str(tmp_path / 'other-state'))

    p = fleetmux_sidebar.payload(f)

    assert [c['id'] for c in p['fleets']] == ['fleet-me', 'fleet-ok'], \
        'only the cfg home + cfg state dir runs are listed (§18.7-3): %r' % (p['fleets'],)


def test_tick_rescans_the_history_at_most_every_thirty_seconds(tmp_path, monkeypatch):
    """§18.4-4: a directory scan costs more than a kv compare, so at most one per 30 s."""
    monkeypatch.setattr(subfleet, 'tree', lambda cfg: {})
    monkeypatch.setattr(fleet, 'daemon_running', lambda run: False)
    f = history_fleet(tmp_path)

    fleetmux_sidebar.tick(f, now=0.0)
    assert [c['id'] for c in json.loads(f.mgr.writes[-1][1])['fleets']] == ['fleet-me'], f.mgr.writes
    assert f._fmx_history_at == 0.0, 'the first tick scans and records the time: %r' % (f._fmx_history_at,)

    history_run(tmp_path, 'fleet-old')
    fleetmux_sidebar.tick(f, now=29.0)
    assert len(f.mgr.writes) == 1, 'inside 30 s the cached history is reused (§18.4-4): %r' % (f.mgr.writes,)
    assert [c['id'] for c in json.loads(f.mgr.writes[-1][1])['fleets']] == ['fleet-me'], f.mgr.writes

    fleetmux_sidebar.tick(f, now=30.0)
    assert [c['id'] for c in json.loads(f.mgr.writes[-1][1])['fleets']] == ['fleet-me', 'fleet-old'], \
        'after 30 s the new history card is pushed: %r' % (f.mgr.writes,)
    assert f._fmx_history_at == 30.0, f._fmx_history_at


# ------------------------------------------------------------------ S2c ④: the docs carry the history entry point (§18.4-6)
def test_the_docs_carry_the_history_entry_point_and_the_grey_card_rules():
    """§18.4-6 / §18.7-1 / §18.7-2: README names `./start.sh gui`, both docs name the two history lines,
    the restore-while-running notice and the no-fleet placeholder the server really draws."""
    readme = _doc('README.md')
    proto = _doc('rust', 'PROTOCOL.md')
    start = proto.index('**Reserved keys.**')
    reserved = proto[start:proto.index('### 4.3', start)]
    i = reserved.index('- `restorable`')
    bullet = reserved[i:reserved.index('\n- `', i + 1)]
    for key, text in (('README.md', readme), ('PROTOCOL.md', proto)):
        assert '到点停 · 可恢复' in text, \
            '%s must name the timeout line `到点停 · 可恢复` (§18.6):\n%s' % (
                key, _lines_with(text, '可恢复'))
        assert '没有在跑的 fleet；点侧栏卡片上的 ↺ 恢复' in text, \
            '%s must carry the no-fleet placeholder (§18.7-6):\n%s' % (key, _lines_with(text, '↺'))
        assert '先关闭正在运行的 fleet 再恢复' in text, \
            '%s must carry the restore-while-running notice (§18.7-4):\n%s' % (key, _lines_with(text, '↺'))
    assert 'start.sh gui' in readme, \
        'README must name the `./start.sh gui` entry point (§18.4-6):\n%s' % _lines_with(readme, '↺')
    assert '这个 HOME 没有可恢复的 fleet' in readme, \
        'README must carry the empty-history message of `gui` (§18.7-2):\n%s' % _lines_with(readme, 'gui')
    assert 'timeout' in bullet and '到点停 · 可恢复' in bullet, \
        'PROTOCOL §4.2 `restorable` must list the timeout-stopped runs too (§18.6):\n' + bullet
