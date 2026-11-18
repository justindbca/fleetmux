"""Applet tabs in the daemon area (fleetmux/applet_view.py, docs/applet-interface.md section 2.1).

The daemon side runs on the applet host fixture of test_applet_host (the repo's private test Redis, fake applet
processes) with a fake terminal manager. The viewer program runs for real against the same Redis. The page test
(an attached / named sub-fleet's applet in its own page of the root window) runs on the protocol fake server and,
under the m4 gate, on the real server.
"""
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_message_bus_v2 import private_redis_v2  # noqa: F401,E402  (fixture)
from test_runtime_v2 import fx  # noqa: F401,E402  (fixture)
from test_applet_host import beat, host  # noqa: F401,E402  (fixture + helper)
from _fleetmux_milestone import milestone  # noqa: E402
from fleetmux import applet_host, applet_view, fleet  # noqa: E402

NAME = 'applet-a-1'


class Mgr:
    """Terminal manager stand-in: records spawns, docks, renames and kills; panes keep their metadata."""

    def __init__(self):
        self.panes, self.calls, self.n = {}, [], 0

    def spawn_window(self, label, command, metadata=None, start_directory=None):
        self.n += 1
        sid = 'p%d' % self.n
        self.panes[sid] = {'title': label, 'meta': dict(metadata or {}), 'command': command, 'cwd': start_directory}
        self.calls.append(('spawn', sid, label))
        return SimpleNamespace(success=True, pane_id=sid, error='')

    def dock_tab(self, sid, history, after=None):
        self.calls.append(('dock', sid, history, after))
        return True

    def rename(self, sid, label):
        self.panes[sid]['title'] = label
        self.calls.append(('rename', sid, label))

    def kill_pane(self, sid):
        self.panes.pop(sid, None)
        self.calls.append(('kill', sid))
        return True

    def tab_panes(self):
        return {s: p['title'] for s, p in self.panes.items() if p['meta'].get('type') == 'tab'}

    def get_pane_status(self, sid):
        p = self.panes.get(sid)
        return SimpleNamespace(metadata=p['meta']) if p else None

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


@pytest.fixture
def view(host):
    f = host
    f.mgr = Mgr()
    f.r.hset(fleet.SERVICE, 'history_pane', 'hist')
    return f


def ticks(f, now):
    applet_host.tick(f)
    applet_view.tick(f, now=now)


# ====================================================================== tab title
def test_title_is_name_and_state_with_a_mark_for_abnormal_states():
    assert applet_view.title(NAME, 'up') == 'applet-a-1 up'
    assert applet_view.title(NAME, 'starting') == 'applet-a-1 starting'
    assert applet_view.title(NAME, 'stopping') == 'applet-a-1 stopping'
    assert applet_view.title(NAME, None) == 'applet-a-1 declared'
    for state in ('stalled', 'exited', 'crashloop', 'config_error'):
        assert applet_view.title(NAME, state) == '! applet-a-1 ' + state


# ====================================================================== daemon side: open, retitle, close
def test_load_opens_one_tab_beside_the_daemon_and_the_title_follows_the_state(view):
    f = view
    ticks(f, 0)
    [(_, sid, label)] = f.mgr.of('spawn')
    assert label == 'applet-a-1 starting'
    pane = f.mgr.panes[sid]
    assert pane['meta'] == {'type': 'tab', 'tab': NAME, 'applet': NAME}  # a tab of the daemon area (lower group)
    assert f.mgr.of('dock') == [('dock', sid, 'hist', None)]  # None: right after the control (daemon) tab
    assert '-m fleetmux.applet_view' in pane['command'] and str(Path(f.cfg['run']) / 'config.json') in pane['command']
    assert pane['cwd'] == f.cfg['run']
    beat(f)
    ticks(f, 1)
    assert f.mgr.of('rename') == [('rename', sid, 'applet-a-1 up')] and f.mgr.panes[sid]['title'] == 'applet-a-1 up'
    ticks(f, 2)
    assert len(f.mgr.of('rename')) == 1 and len(f.mgr.of('spawn')) == 1  # unchanged state: no call
    f.test_procs[-1].code = 0  # exit 0: exited, held
    ticks(f, 3)
    assert f.mgr.panes[sid]['title'] == '! applet-a-1 exited' and len(f.mgr.of('spawn')) == 1  # held rows keep it


def test_state_writes_record_when_the_state_changed(view):
    f = view
    before = time.time()
    applet_host.tick(f)
    h = f.r.hgetall(applet_host.key(NAME))
    assert h['state'] == 'starting' and before <= float(h['state_at']) <= time.time()


def test_unload_keeps_the_tab_while_it_drains_then_removes_it(view):
    f = view
    ticks(f, 0)
    beat(f)
    ticks(f, 1)
    [(_, sid, _)] = f.mgr.of('spawn')
    f.applet_specs = []  # row removed: detach
    ticks(f, 2)
    assert sid in f.mgr.panes and f.mgr.panes[sid]['title'] == 'applet-a-1 stopping'
    f.test_procs[-1].code = 0
    ticks(f, 3)
    assert NAME not in f.applet_runners and f.mgr.of('kill') == [('kill', sid)] and not f.mgr.panes
    ticks(f, 30)
    assert len(f.mgr.of('spawn')) == 1  # gone for good


def test_a_declared_row_that_never_ran_is_removed_at_once(view):
    f = view
    f.applet_specs = [dict(f.applet_specs[0], autostart=False)]
    ticks(f, 0)
    [(_, sid, label)] = f.mgr.of('spawn')
    assert label == 'applet-a-1 declared'
    f.applet_specs = []
    ticks(f, 1)
    assert f.mgr.of('kill') == [('kill', sid)]


def test_two_applets_are_docked_in_roster_order(view):
    f = view
    f.applet_specs = f.applet_specs + [dict(f.applet_specs[0], name='applet-b-1')]
    ticks(f, 0)
    (_, a, la), (_, b, lb) = f.mgr.of('spawn')
    assert (la, lb) == ('applet-a-1 starting', 'applet-b-1 starting')
    assert f.mgr.of('dock') == [('dock', a, 'hist', None), ('dock', b, 'hist', a)]


def test_a_closed_tab_comes_back_and_a_previous_daemons_applet_tab_is_closed(view):
    f = view
    old = f.mgr.spawn_window('applet-a-1 up', 'x', metadata={'type': 'tab', 'tab': NAME, 'applet': NAME})
    other = f.mgr.spawn_window('index-service', 'y', metadata={'type': 'tab', 'tab': 'index-service'})
    ticks(f, 0)
    assert ('kill', old.pane_id) in f.mgr.calls and other.pane_id in f.mgr.panes  # roster tabs are not touched
    [sid] = [s for s, p in f.mgr.panes.items() if p['meta'].get('applet')]
    f.mgr.panes.pop(sid)  # closed by hand
    ticks(f, 1)
    assert [s for s, p in f.mgr.panes.items() if p['meta'].get('applet')] == []  # checked every CHECK_S
    ticks(f, 1 + applet_view.CHECK_S)
    assert len([s for s, p in f.mgr.panes.items() if p['meta'].get('applet')]) == 1


def test_nothing_opens_before_the_tabs_clear_and_no_history_pane_means_no_dock(view):
    f = view
    f.tab_specs, f.tabs_opened = [], False  # tabs.open_all has not cleared a previous daemon's tabs yet
    ticks(f, 0)
    assert not f.mgr.of('spawn')
    f.tabs_opened = True
    f.r.hdel(fleet.SERVICE, 'history_pane')  # a backend without a history pane (WezTerm): open, do not dock
    ticks(f, 1)
    assert len(f.mgr.of('spawn')) == 1 and not f.mgr.of('dock')


def test_fleet_tick_runs_the_tab_part_after_the_applets(view):
    f = view
    f.tick()
    assert [c[2] for c in f.mgr.of('spawn')] == ['applet-a-1 starting']


# ====================================================================== the viewer: header and refresh
def record(state='up', **extra):
    h = {'state': state, 'started_at': '1000.0', 'hb_at': '1095.0', 'pid': '4242', 'restarts': '2',
         'crashes': json.dumps([990.0, 995.0]), 'state_at': '1010.0',
         'phase': 'serving 127.0.0.1:17891, 0 tunnels open'}
    h.update(extra)
    return h


def test_header_shows_state_phase_heartbeat_restarts_crashes_leases_and_state_age():
    first, second = applet_view.header(NAME, record(), 1, 1100.0)
    assert first == 'applet-a-1  UP  phase: serving 127.0.0.1:17891, 0 tunnels open'
    assert second.startswith('heartbeat 5s ago · pid 4242 · restarts 2 · crashes 2 · leases 1 open · up since ')
    assert second.endswith('(1m30s ago)')
    first, second = applet_view.header(NAME, record('crashloop', phase=''), 0, 1100.0)
    assert first == '! applet-a-1  CRASHLOOP  phase: -'
    _, second = applet_view.header(NAME, record('starting', hb_at='900.0'), 0, 1100.0)
    assert second.startswith('no heartbeat yet')  # a beat older than this start does not count
    assert 'no record' in applet_view.header(NAME, {}, 0, 1100.0)[0]
    assert applet_view.header(NAME, {}, 0, 1100.0, error='boom') == ['applet-a-1  (fleet bus unreachable)', 'boom']


class Out(io.BytesIO):
    def __init__(self, tty):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


class Bus:
    """A read-recording Redis stand-in holding one applet hash."""

    def __init__(self, h, leases=0):
        self.h, self.leases, self.calls = h, leases, []

    def hgetall(self, k):
        self.calls.append(('hgetall', k))
        return dict(self.h)

    def scard(self, k):
        self.calls.append(('scard', k))
        return self.leases


def test_header_is_redrawn_with_each_heartbeat_and_only_when_it_changed(tmp_path):
    bus, out = Bus(record()), Out(False)
    screen, tail = applet_view.Screen(out), applet_view.Tail(tmp_path / 'applet.log')
    applet_view.step(NAME, lambda: bus, tail, screen, now=1100.0)
    assert 'heartbeat 5s ago' in out.getvalue().decode()
    n = len(out.getvalue())
    applet_view.step(NAME, lambda: bus, tail, screen, now=1100.0)
    assert len(out.getvalue()) == n  # nothing changed: nothing written
    bus.h.update(hb_at='1099.0', phase='up: 127.0.0.1:17892 -> 10.0.0.1:17891 (ssh pid 77)')
    bus.leases = 2
    applet_view.step(NAME, lambda: bus, tail, screen, now=1100.0)
    text = out.getvalue()[n:].decode()
    assert 'phase: up: 127.0.0.1:17892 -> 10.0.0.1:17891 (ssh pid 77)' in text
    assert 'heartbeat 1s ago' in text and 'leases 2 open' in text


def test_on_a_terminal_the_header_is_fixed_above_a_scrolling_log(tmp_path, monkeypatch):
    monkeypatch.setattr(applet_view.shutil, 'get_terminal_size', lambda default: os.terminal_size((60, 20)))
    log = tmp_path / 'applet.log'
    log.write_bytes(b'line one\n')
    bus, out = Bus(record()), Out(True)
    screen, tail = applet_view.Screen(out), applet_view.Tail(log)
    applet_view.step(NAME, lambda: bus, tail, screen, now=1100.0)
    raw = out.getvalue()
    # the alternate screen (no scrollback to fill), then the scrolling region below the 3 header rows
    assert raw.startswith(b'\x1b[?1049h\x1b[4;20r\x1b[20;1H')
    assert b'\x1b7\x1b[1;1H\x1b[2Kapplet-a-1  UP' in raw and b'\x1b[3;1H\x1b[2K' + ('─' * 60).encode() in raw
    assert raw.endswith(b'\x1b8line one\n')  # the cursor goes back into the log region before the log bytes
    n = len(raw)
    bus.h['state'] = 'stalled'
    log.write_bytes(b'line one\nline two\n')
    applet_view.step(NAME, lambda: bus, tail, screen, now=1100.0)
    new = out.getvalue()[n:]
    assert b'\x1b[1;1H\x1b[2K! applet-a-1  STALLED' in new and new.endswith(b'\x1b8line two\n')
    assert b'\x1b[4;20r' not in new and b'\x1b[?1049h' not in new  # only the header rows are rewritten
    screen.resized = True  # SIGWINCH: the region is set again, still on the alternate screen, the log kept
    applet_view.step(NAME, lambda: bus, tail, screen, now=1100.0)
    again = out.getvalue()[n + len(new):]
    assert again.startswith(b'\x1b[4;20r\x1b[20;1H') and b'\x1b[?1049h' not in again and b'\x1b[2J' not in again
    screen.reset()
    assert out.getvalue().endswith(b'\x1b[r\x1b[?1049l')  # back to the main screen on exit


def test_tail_starts_at_a_line_and_follows_truncation(tmp_path):
    log = tmp_path / 'applet.log'
    tail = applet_view.Tail(log, backlog=10)
    assert tail.read() == b''  # no log yet
    log.write_bytes(b'aaaaaaaaaa\nbbbb\ncc\n')
    assert tail.read() == b'bbbb\ncc\n'  # the last 10 bytes start inside a line: from the next line start
    log.write_bytes(b'new\n')
    assert tail.read() == b'new\n'


# ====================================================================== read only
def test_viewer_only_reads_and_drops_typed_keys(tmp_path):
    bus = Bus(record())
    applet_view.step(NAME, lambda: bus, applet_view.Tail(tmp_path / 'x.log'), applet_view.Screen(Out(False)))
    assert bus.calls == [('hgetall', 'fx:applet:' + NAME), ('scard', 'fx:applet:' + NAME + ':leases'),
                         ('hgetall', 'fx:clock')]  # the host's awake clock: a beat of this start (design.md 35)
    rfd, wfd = os.pipe()
    os.write(wfd, b'stop\n\x03')
    assert applet_view.drain(rfd, 0.5) is True
    assert applet_view.drain(rfd, 0.05) is True  # consumed: nothing left
    os.close(wfd)
    assert applet_view.drain(rfd, 0.5) is False  # input closed: the viewer only sleeps from now on
    os.close(rfd)


def test_viewer_program_shows_header_and_log_and_typing_changes_nothing(view):
    f = view
    applet_host.tick(f)
    beat(f)
    applet_host.tick(f)
    f.r.hset(applet_host.key(NAME), 'phase', 'serving 127.0.0.1:17891, 0 tunnels open')
    log = applet_host.run_dir(f.cfg, NAME) / 'applet.log'
    log.write_text('hello from the applet\n')
    before = {k: (f.r.type(k), f.r.hgetall(k) if f.r.type(k) == 'hash' else None) for k in f.r.keys('fx:applet*')}
    env = dict(os.environ, PYTHONPATH=applet_host.sdk_dir())
    p = subprocess.Popen([sys.executable, '-m', 'fleetmux.applet_view', str(Path(f.cfg['run']) / 'config.json'),
                          NAME, '--interval', '0.1'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, env=env)
    try:
        p.stdin.write(b'stop\nfleetmux applet stop ' + NAME.encode() + b'\n')
        p.stdin.flush()
        time.sleep(1.0)
        assert p.poll() is None  # typing does not end it
    finally:
        p.terminate()
        out = p.communicate(timeout=10)[0].decode()
    assert 'applet-a-1  UP  phase: serving 127.0.0.1:17891, 0 tunnels open' in out, out
    assert 'hello from the applet' in out and 'stop' not in out.replace('stopping', '')
    after = {k: (f.r.type(k), f.r.hgetall(k) if f.r.type(k) == 'hash' else None) for k in f.r.keys('fx:applet*')}
    assert after == before and not f.r.exists(applet_host.key(NAME) + ':inbox') and not f.r.exists(applet_host.CTL)


# ====================================================================== a sub-fleet's page in the root window
@pytest.fixture(params=['fake', pytest.param('real', marks=milestone(4))])
def pages(monkeypatch, request):
    """The protocol fake server, and under the m4 gate this worktree's real server (``rust/target/debug``)."""
    import _fm4_world as W
    root = W.new_root('av', request, request.param)
    W.install_fleetmux(monkeypatch, root, request.param)
    state = root / 'state'
    state.mkdir()
    s = W.Servers(root, request.param, state)
    yield W, s
    s.close()


class Hash:
    def __init__(self, data):
        self.data = data

    def hget(self, k, field):
        return self.data.get((k, field))


def test_a_sub_fleets_applet_tab_lands_beside_its_daemon_in_its_own_page(pages, tmp_path):
    W, s = pages
    root = s.root_mgr()
    assert root.get_or_create_session(), root.last_error
    child = s.child_mgr('M0')
    assert child.get_or_create_session(), child.last_error

    def spawn(mgr, name, kind):
        res = mgr.spawn_window(name, 'sleep 600', metadata={'type': kind}, start_directory='/tmp')
        assert res.success, (res.error, mgr.last_error)
        return res.pane_id
    root_control = spawn(root, 'control', 'daemon')
    hist, control = spawn(child, 'history', 'history'), spawn(child, 'control', 'daemon')
    run = tmp_path / 'sub-run'
    run.mkdir()
    (run / 'config.json').write_text('{}')
    f = SimpleNamespace(cfg={'run': str(run), 'python': sys.executable}, mgr=child, stopping=False,
                        applet_runners={NAME: object()}, logs=[],
                        r=Hash({(fleet.SERVICE, 'history_pane'): hist, (applet_host.key(NAME), 'state'): 'up'}))
    f.log = lambda kind, **d: f.logs.append((kind, d))
    applet_view.tick(f, now=0)
    [sid] = [v['pane'] for v in f.applet_views.values()]
    assert W.page_of(s.sock, sid) == 'M0'
    _, reply = W.on_page(s.sock, 'M0', 'layout.get')
    assert reply['groups']['lower'] == [hist, control, sid]  # the daemon area of the sub-fleet's page
    assert sid not in [str(r['pane']) for r in W.ctl(s.sock, 'pane.list').get('panes') or []]  # not the root page
    assert W.record(s.sock, sid).get('title') == 'applet-a-1 up'
    assert root_control in [str(r['pane']) for r in W.ctl(s.sock, 'pane.list').get('panes') or []]


def test_the_root_lower_group_keeps_one_history_through_load_job_unload_load(pages, tmp_path):
    """The reported sequence: restart, load, a job sub-fleet runs and finishes, unload, load again. The job's record
    tab (archive_page) stays in the job's own page; the root page's lower group never gets a second history tab."""
    W, s = pages
    root = s.root_mgr()
    assert root.get_or_create_session(), root.last_error

    def spawn(mgr, name, kind):
        res = mgr.spawn_window(name, 'sleep 600', metadata={'type': kind}, start_directory='/tmp')
        assert res.success, (res.error, mgr.last_error)
        return res.pane_id

    def lower(page=''):
        reply = W.ctl(s.sock, 'layout.get') if page == '' else W.on_page(s.sock, page, 'layout.get')[1]
        return reply['groups']['lower']

    def kinds(ids):
        return [W.record(s.sock, sid).get('type') for sid in ids]

    root_run, root_url = W.make_run(tmp_path, 'run-root')
    control = spawn(root, 'daemon', 'daemon')
    sup, worker = spawn(root, 'fx-supervisor', 'supervisor'), spawn(root, 'fx-worker-1', 'worker')
    hist = root.arrange(sup, [worker], root_url)
    assert hist, root.last_error
    (root_run / 'config.json').write_text('{}')
    f = SimpleNamespace(cfg={'run': str(root_run), 'python': sys.executable}, mgr=root, stopping=False,
                        applet_runners={}, logs=[],
                        r=Hash({(fleet.SERVICE, 'history_pane'): hist, (applet_host.key(NAME), 'state'): 'up'}))
    f.log = lambda kind, **d: f.logs.append((kind, d))
    assert lower() == [hist, control]  # restart

    f.applet_runners = {NAME: object()}  # load
    applet_view.tick(f, now=0)
    first = f.applet_views[NAME]['pane']
    assert lower() == [hist, control, first]

    child = s.child_mgr('M0')  # a job sub-fleet runs in page M0 and finishes
    assert child.get_or_create_session(), child.last_error
    job_run = W.sub_run(root_run, 'M0', 'M0')
    spawn(child, 'daemon', 'daemon')
    child_hist = child.arrange(spawn(child, 'fx-M0/supervisor', 'supervisor'),
                               [spawn(child, 'fx-M0/worker-1', 'worker')], (job_run / 'history.html').as_uri())
    assert child_hist, child.last_error
    assert child.close_all() is True, child.last_error
    assert root.archive_page('M0', str(job_run), 'M0') is True, root.last_error
    [record_tab] = lower('M0')
    assert kinds([record_tab]) == ['history']

    f.applet_runners = {}  # unload
    applet_view.tick(f, now=10)
    assert lower() == [hist, control]

    f.applet_runners = {NAME: object()}  # load again
    applet_view.tick(f, now=20)
    second = f.applet_views[NAME]['pane']
    assert lower() == [hist, control, second] and kinds(lower()).count('history') == 1
    assert lower('M0') == [record_tab]  # the job's record tab never left its own page
    root_rows = W.ctl(s.sock, 'pane.list').get('panes') or []
    assert [str(r['pane']) for r in root_rows if r.get('type') == 'history'] == [hist]


@pytest.fixture(params=[pytest.param('real', marks=milestone(4))])
def server(monkeypatch, request):
    """This worktree's real server only: ``pane.read`` needs its terminal emulator (scrollback, scroll regions)."""
    import _fm4_world as W
    root = W.new_root('avs', request, request.param)
    W.install_fleetmux(monkeypatch, root, request.param)
    state = root / 'state'
    state.mkdir()
    s = W.Servers(root, request.param, state)
    yield W, s
    s.close()


def test_the_header_stays_once_at_the_top_of_the_tab_text_through_refreshes_resizes_and_log_lines(view, server):
    """The viewer program in a real pane: N header redraws, M log lines and several PTY resizes (a tab moved between
    groups gets SIGWINCH). ``pane.read`` (scrollback + screen) holds the header exactly once, on its first row, and
    the log lines that are shown are the newest ones, in order."""
    W, s = server
    f = view
    applet_host.tick(f)
    beat(f)
    applet_host.tick(f)
    log = applet_host.run_dir(f.cfg, NAME) / 'applet.log'
    log.write_text('')
    mgr = s.root_mgr()
    assert mgr.get_or_create_session(), mgr.last_error
    command = applet_view.command(f.cfg, NAME) + ' --interval 0.1'
    res = mgr.spawn_window('viewer', command, metadata={'type': 'tab'}, start_directory=str(f.cfg['run']))
    assert res.success, (res.error, mgr.last_error)
    sid = res.pane_id

    def text():
        return W.ctl(s.sock, 'pane.read', pane=sid, lines=100000)['text']
    assert W.wait_for(lambda: NAME + '  UP' in text(), timeout=15), text()
    n = 0
    for group in ('workers', 'lower', 'supervisor', 'lower', 'workers', 'lower'):
        for _ in range(10):
            f.r.hset(applet_host.key(NAME), 'phase', 'refresh %d' % n)  # a new header on the next redraw
            with log.open('a') as fh:
                fh.write('log line %03d\n' % n)
            n += 1
            time.sleep(0.12)
        W.ctl(s.sock, 'pane.move', pane=sid, group=group)  # a new PTY size: SIGWINCH
        time.sleep(0.3)
    last = 'log line %03d' % (n - 1)
    assert W.wait_for(lambda: last in text() and 'refresh %d' % (n - 1) in text(), timeout=10), text()
    rows = text().split('\n')
    assert [i for i, row in enumerate(rows) if row.startswith(NAME + '  ')] == [0], rows
    assert rows[0].startswith(NAME + '  UP  phase: refresh %d' % (n - 1)) and rows[1].startswith('heartbeat ')
    assert rows[2] and set(rows[2]) == {'─'}, rows  # the rule, nothing wrapped below it
    assert all(row == '' or re.fullmatch(r'log line \d{3}', row) for row in rows[3:]), rows
    shown = [int(row.split()[-1]) for row in rows if row.startswith('log line ')]
    assert shown and shown == list(range(shown[0], n)), rows
