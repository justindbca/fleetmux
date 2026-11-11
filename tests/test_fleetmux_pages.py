"""M4 pages: one fleetmux server per fleet tree, one page per fleet (design-1/m4-design.md v0.7 §4.2 / §6.1 / §8.2).

Red until phase B (``bind_page`` / ``archive_page`` / ``unarchive_page`` / ``pages_problem`` in
``backends/fleetmux.py``, pages in ``tests/fake_fleetmux.py`` and in the Rust server). Every test runs on ``fake``
(the protocol fake) and ``real`` (this worktree's ``rust/target/debug/fleetmux``); the two old-server tests run on
the fake only, because only the fake can play an M3 server (``FAKE_FM_NO_PAGES=1``, design §6.2).

What the server *really* holds is read straight from the root's control socket (``_fm4_world.ctl`` / ``on_page``),
never from the manager's own account. Evidence roots ``~/.m4e/fm4-py/pg-*`` are kept; teardown is the product's stop
plus an identity-checked pid fallback (``_fm4_world``).

``test_unbound_manager_is_unchanged`` is a pin: it holds on M3 already and must stay green through M4.
"""
import shutil
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _fm4_world as W  # noqa: E402
from _fleetmux_milestone import milestone  # noqa: E402
from fleetmux import fleet  # noqa: E402

RS = W.ROOT_SESSION


@pytest.fixture(params=['fake', 'real'])
def pg(request, monkeypatch):
    root = W.new_root('pg', request, request.param)
    W.install_fleetmux(monkeypatch, root, request.param)
    state = root / 'state'
    state.mkdir()
    s = W.Servers(root, request.param, state)
    yield s
    s.close()


@pytest.fixture(params=['fake'])
def pg_old(request, monkeypatch):
    """A root server that plays M3: it ignores ``hello.page`` and never answers ``pages`` (fake only)."""
    monkeypatch.setenv('FAKE_FM_NO_PAGES', '1')
    root = W.new_root('pgo', request, 'fake FAKE_FM_NO_PAGES=1')
    W.install_fleetmux(monkeypatch, root, 'fake')
    state = root / 'state'
    state.mkdir()
    s = W.Servers(root, 'fake', state)
    yield s
    s.close()


def up(s):
    """The root manager with its server running (the root is the only one that ever starts it, design §6.1 S2)."""
    root = s.root_mgr()
    assert root.get_or_create_session(), root.last_error
    return root


def spawn(mgr, name, type_='worker', cwd='/tmp', command='sleep 600'):
    res = mgr.spawn_window(name, command, metadata={'type': type_}, start_directory=str(cwd))
    assert res.success, (res.error, mgr.last_error)
    return res.pane_id


def ids(rows):
    return [str(r.get('pane')) for r in rows]


def page_groups(s, page):
    """``layout.get`` groups on a connection bound to ``page`` ('' = the root connection)."""
    if page == '':
        reply = W.ctl(s.sock, 'layout.get')
    else:
        _, reply = W.on_page(s.sock, page, 'layout.get')
    assert reply.get('ok'), reply
    return reply['groups']


def page_list(s, page):
    """``pane.list`` (no ``all``) on a connection bound to ``page`` ('' = root connection)."""
    if page == '':
        reply = W.ctl(s.sock, 'pane.list')
    else:
        hello, reply = W.on_page(s.sock, page, 'pane.list')
        assert hello.get('pages') == 1 and hello.get('page') == page, hello
    assert reply.get('ok'), reply
    return reply.get('panes') or []


def archive_rows(mgr, page=None, sock=None):
    """Live ``type: archive`` entries of ``mgr``'s registry (optionally only those whose pane is in ``page``)."""
    rows = []
    for sid, info in mgr._surfaces().items():
        if info.get('type') != 'archive':
            continue
        rec = W.record(sock, sid) if sock else {}
        if sock and not rec:
            continue
        if page is not None and rec.get('page') != page:
            continue
        rows.append(sid)
    return rows


# ================================================================ S0 identity / S1 page check / S2 no server

@milestone(4)
def test_bind_page_shares_the_root_runtime_and_keeps_its_own_registry(pg):
    root = pg.root_mgr()
    child = pg.child_mgr('M0')
    assert child.page == 'M0' and child.session_name == RS + ':M0'
    for attr in ('runtime_root', 'ctl_socket', 'server_json', 'start_lock', 'panes_json', 'server_log'):
        assert Path(getattr(child, attr)) == Path(getattr(root, attr)), attr
    assert Path(child.runtime_root).resolve() == W.runtime(pg.state)
    # the registry stays per fleet: <state>/<child session>.fleetmux.json
    assert child.meta_path == pg.state.resolve() / (RS + ':M0.fleetmux.json')
    assert child.meta_path != root.meta_path
    # the page id is never cut like _token(): a long id keeps its name
    long_id = 'memx-' + 'a' * 50
    other = pg.child_mgr(long_id)
    assert other.page == long_id and Path(other.runtime_root) == Path(root.runtime_root)
    # manager_for binds from the child config's two keys (design §6.2 fleet.py manager_for)
    cfg = {'session': RS + ':M0', 'backend': 'fleetmux', 'cmux_state_dir': str(pg.state),
           'fleetmux_root_session': RS, 'fleetmux_page': 'M0'}
    built = pg.track(fleet.manager_for(cfg))
    assert getattr(built, 'page', None) == 'M0' and Path(built.ctl_socket) == Path(root.ctl_socket)


@milestone(4)
def test_child_daemon_pane_is_the_first_pane_of_its_page(pg):
    root = up(pg)
    sup = spawn(root, 'fx-supervisor', 'supervisor')
    run, _ = W.make_run(pg.root, 'sub-M0')
    child = pg.child_mgr('M0')
    # what subfleet.default_launch does from the PARENT process, before any child daemon exists
    res = child.spawn_window('daemon', 'sleep 600', metadata={'type': 'daemon'}, start_directory=str(run))
    assert res.success, (res.error, child.last_error)
    table = W.pages(pg.sock)
    assert table['M0']['state'] == 'live' and table['M0']['title'] == 'M0', table
    assert '' in table and list(table)[0] == ''  # the root page is listed first
    assert ids(page_list(pg, 'M0')) == [res.pane_id]
    assert W.page_of(pg.sock, res.pane_id) == 'M0'
    assert W.record(pg.sock, res.pane_id).get('group') == 'lower'
    assert res.pane_id not in ids(page_list(pg, '')) and sup in ids(page_list(pg, ''))
    assert res.pane_id in child._surfaces() and res.pane_id not in root._surfaces()


@milestone(4)
def test_unbound_manager_is_unchanged(pg):
    """Pin (green on M3 too): without the binding keys nothing changes, byte for byte on the wire."""
    session = RS + ':run/7'
    m = pg.root_mgr(session)
    assert Path(m.runtime_root).resolve() == W.runtime(pg.state, session)
    assert m.workspace_id() == 'fleetmux:' + W.token(session)
    assert getattr(m, 'page', None) in (None, '')
    built = pg.track(fleet.manager_for({'session': session, 'backend': 'fleetmux', 'cmux_state_dir': str(pg.state)}))
    assert Path(built.runtime_root) == Path(m.runtime_root) and getattr(built, 'page', None) in (None, '')
    rec = W.Recorder(m)
    assert m.get_or_create_session(), m.last_error
    sid = spawn(m, 'fx-worker-1')
    assert m.get_pane_status(sid) is not None
    hellos = [r for r in rec.requests if r.get('cmd') == 'hello']
    assert hellos and all('page' not in r for r in hellos), hellos
    spawns = [r for r in rec.requests if r.get('cmd') == 'pane.spawn']
    assert spawns and all('page' not in r for r in spawns), spawns
    assert not any(str(r.get('cmd', '')).startswith('page.') for r in rec.requests), rec.requests
    assert m.kill_pane(sid)


@milestone(4)
def test_bound_manager_never_starts_a_server(pg):
    child = pg.child_mgr('M0')
    assert child.get_or_create_session() is False
    assert 'not running' in child.last_error and RS in child.last_error, child.last_error
    res = child.spawn_window('daemon', 'sleep 600', metadata={'type': 'daemon'}, start_directory='/tmp')
    assert not res.success
    assert not Path(child.server_json).exists() and not Path(child.ctl_socket).exists()
    assert not (pg.state / 'fleetmux').is_dir() or not any((pg.state / 'fleetmux').glob('*/server.json'))


@milestone(4)
def test_bound_manager_fails_closed_without_the_page_echo(pg_old):
    root = up(pg_old)
    before = ids(W.ctl(pg_old.sock, 'pane.list')['panes'])
    child = pg_old.child_mgr('M0')
    assert child.get_or_create_session() is False
    assert 'pages' in child.last_error, child.last_error
    res = child.spawn_window('fx-worker-1', 'sleep 600', metadata={'type': 'worker'}, start_directory='/tmp')
    assert not res.success and 'pages' in (res.error or ''), res.error
    # nothing of the child's landed in the old server's (only) page
    assert ids(W.ctl(pg_old.sock, 'pane.list')['panes']) == before
    assert child.session_exists() is False
    assert root.session_exists() is False  # the root itself keeps working on the old server
    assert W.server_up(pg_old.sock)


@milestone(4)
def test_pages_problem_names_an_old_server(pg_old):
    root = pg_old.root_mgr()
    assert W.need(root, 'pages_problem')() is None  # no server answers: the version is the starting root's job
    assert root.get_or_create_session(), root.last_error
    why = root.pages_problem()
    assert why and 'fleetmux' in why and 'pages' in why, why
    assert W.need(root, 'server_has_pages')() is False


# ================================================================ where panes land

@milestone(4)
def test_child_spawn_lands_in_its_page(pg):
    root = up(pg)
    rw = spawn(root, 'fx-worker-1')
    child = pg.child_mgr('M0')
    assert child.get_or_create_session(), child.last_error
    cw = spawn(child, 'fx-M0/worker-1')
    assert W.page_of(pg.sock, cw) == 'M0' and W.page_of(pg.sock, rw) == ''
    assert ids(page_list(pg, 'M0')) == [cw]
    assert cw not in ids(page_list(pg, '')) and rw in ids(page_list(pg, ''))
    assert child.get_pane_status(cw).metadata.get('page') == 'M0'
    assert W.need(child, 'server_has_pages')() is True and W.need(child, 'pages_problem')() is None


@milestone(4)
def test_root_pane_list_sees_only_the_root_page(pg):
    root = up(pg)
    rw = spawn(root, 'fx-worker-1')
    child = pg.child_mgr('M0')
    cw = spawn(child, 'fx-M0/worker-1')
    unbound = page_list(pg, '')
    assert ids(unbound) == [rw] and all(r.get('page') == '' for r in unbound), unbound
    rows = {str(r['pane']): r for r in W.all_panes(pg.sock)}
    assert set(rows) == {rw, cw}
    assert rows[rw]['page'] == '' and rows[cw]['page'] == 'M0'
    assert rows[cw]['group'] == 'workers' and rows[cw]['index'] == 0  # its own page's layout (F8)


@milestone(4)
def test_child_session_exists_counts_only_its_live_page(pg):
    root = up(pg)
    spawn(root, 'fx-worker-1')
    child = pg.child_mgr('M0')
    assert child.session_exists() is False  # the page does not exist yet: [] by design, not a swallowed error
    other = pg.child_mgr('M1')
    cw = spawn(child, 'fx-M0/worker-1')
    assert child.session_exists() is True
    assert other.session_exists() is False  # root and M0 panes are not M1's
    assert child.close_all(), child.last_error
    assert child.session_exists() is False
    run, _ = W.make_run(pg.root, 'sub-M0')
    assert W.need(root, 'archive_page')('M0', str(run), 'M0'), root.last_error
    assert archive_rows(root, 'M0', pg.sock)  # the reclaimed page holds the record pane ...
    assert child.session_exists() is False  # ... and is still not a live session (spawn_child:714 re-up)
    assert W.record(pg.sock, cw) == {}


@milestone(4)
def test_root_session_exists_counts_every_page(pg):
    root = up(pg)
    assert root.session_exists() is False
    child = pg.child_mgr('M0')
    cw = spawn(child, 'fx-M0/worker-1')
    assert root.session_exists() is True  # lifecycle_close.all_gone: the tree is not gone while a page has a pane
    assert child.kill_pane(cw)
    assert root.session_exists() is False


# ================================================================ a child never stops the root's server

@milestone(4)
def test_child_close_all_reclaims_the_page_and_keeps_the_server(pg):
    root = up(pg)
    rw = spawn(root, 'fx-worker-1')
    pid = root._server_pid()
    child = pg.child_mgr('M0')
    cw = spawn(child, 'fx-M0/worker-1')
    cd = spawn(child, 'daemon', 'daemon')
    assert child.close_all() is True, child.last_error
    assert W.server_up(pg.sock) and root._server_pid() == pid
    assert W.record(pg.sock, rw) and not W.record(pg.sock, cw) and not W.record(pg.sock, cd)
    assert W.pages(pg.sock)['M0']['state'] == 'reclaimed'
    assert child._surfaces() == {}
    assert root.get_pane_status(rw) is not None


@milestone(4)
def test_child_close_never_touches_a_bash_page(pg):
    """A sub-fleet's close (bound manager) leaves every bash alone, even an idle one (user 2026-11-08: only the root
    fleet's close takes its idle bashes with it). User 2026-11-09: a bash is a tab of the root page's worker area and
    only the root fleet takes it into its registry; a sub-fleet never does."""
    root = up(pg)
    spawn(root, 'fx-worker-1')
    sh = W.ctl(pg.sock, 'pane.spawn', argv=['sleep', '600'], type='shell', title='bash-1', group='workers')
    assert sh['ok'], sh
    child = pg.child_mgr('M0')
    spawn(child, 'fx-M0/worker-1')
    assert child.adopt_shells() is False, 'a sub-fleet manager took a root-page bash in'
    assert sh['pane'] not in child._surfaces(), child._surfaces()
    assert child.close_panes() is True, child.last_error
    assert child.close_all() is True, child.last_error
    assert W.record(pg.sock, sh['pane']), 'a sub-fleet close killed the idle bash page'


@milestone(4)
def test_child_end_server_sends_nothing(pg):
    root = up(pg)
    rw = spawn(root, 'fx-worker-1')
    child = pg.child_mgr('M0')
    spawn(child, 'fx-M0/worker-1')
    rec = W.Recorder(child)
    assert child.end_server() is False
    assert rec.requests == []
    assert W.server_up(pg.sock) and W.record(pg.sock, rw)
    # second guard (server side): a page connection's server.shutdown is refused
    _, reply = W.on_page(pg.sock, 'M0', 'server.shutdown', force=True)
    assert reply.get('ok') is False and reply.get('code') == 'bad_request', reply
    assert W.server_up(pg.sock)


# ================================================================ handover across pages (§5.2)

def _pid(s, sid):
    return W.record(s.sock, sid).get('pid')


@milestone(4)
def test_adopt_moves_a_parent_pane_into_the_child_page_same_pid(pg):
    root = up(pg)
    rw = spawn(root, 'fx-worker-1')
    pid = _pid(pg, rw)
    child = pg.child_mgr('M0')
    peer = spawn(child, 'fx-M0/supervisor-x', 'supervisor')
    info = root.release(rw)
    assert info is not None
    assert child.adopt(rw, peer, info) is True, child.last_error
    rec = W.record(pg.sock, rw)
    assert rec['page'] == 'M0' and rec['pid'] == pid and rec['group'] == 'supervisor', rec
    assert page_groups(pg, 'M0')['supervisor'] == [rw, peer]  # right before the peer
    assert rw not in sum(page_groups(pg, '').values(), [])
    assert child.owns(rw) and rw not in root._surfaces()


@milestone(4)
def test_adopt_moves_across_pages_even_in_the_same_group(pg):
    root = up(pg)
    rw = spawn(root, 'fx-worker-1')  # workers group of the root page
    pid = _pid(pg, rw)
    child = pg.child_mgr('M0')
    peer = spawn(child, 'fx-M0/worker-1')  # workers group of M0: same group name
    assert child.adopt(rw, peer, root.release(rw) or {}) is True, child.last_error
    rec = W.record(pg.sock, rw)
    assert rec['page'] == 'M0' and rec['pid'] == pid and rec['group'] == 'workers', rec
    assert page_groups(pg, 'M0')['workers'] == [rw, peer]
    assert rw not in page_groups(pg, '')['workers']


@milestone(4)
def test_adopt_back_returns_the_pane_before_the_placeholder(pg):
    root = up(pg)
    rw = spawn(root, 'fx-worker-1')
    other = spawn(root, 'fx-worker-2')
    pid = _pid(pg, rw)
    ph = spawn(root, 'placeholder-worker-1', 'placeholder')
    assert root.tab_into(ph, rw)
    child = pg.child_mgr('M0')
    peer = spawn(child, 'fx-M0/worker-1')
    assert child.adopt(rw, peer, root.release(rw) or {}), child.last_error
    assert W.page_of(pg.sock, rw) == 'M0'
    info = child.release(rw)  # hand_back on the child side
    assert info is not None
    assert root.adopt(rw, ph, info) is True, root.last_error
    rec = W.record(pg.sock, rw)
    assert rec['page'] == '' and rec['pid'] == pid, rec
    workers = page_groups(pg, '')['workers']
    assert workers.index(rw) == workers.index(ph) - 1 and other in workers
    assert rw not in sum(page_groups(pg, 'M0').values(), [])
    assert root.owns(rw)


# ================================================================ workspace ids, kv, arrange

@milestone(4)
def test_workspace_id_is_per_page_and_select_workspace_selects_the_page(pg):
    root = up(pg)
    spawn(root, 'fx-worker-1')
    child = pg.child_mgr('M0')
    spawn(child, 'fx-M0/worker-1')
    assert root.workspace_id() == 'fleetmux:' + RS
    assert child.workspace_id() == 'fleetmux:' + RS + '/M0'
    assert W.current_page(pg.sock) == ''
    assert root.select_workspace(child.workspace_id()) is True
    assert W.current_page(pg.sock) == 'M0'
    assert root.select_workspace(root.workspace_id()) is True
    assert W.current_page(pg.sock) == ''
    assert root.select_workspace('fleetmux:' + RS + '/nope') is False
    assert root.select_workspace('fleetmux:other/M0') is False
    assert W.current_page(pg.sock) == ''


@milestone(4)
def test_kv_run_and_history_url_are_page_scoped(pg):
    root_run, root_url = W.make_run(pg.root, 'run-root')
    child_run, child_url = W.make_run(pg.root, 'run-M0')
    root = pg.root_mgr()
    root.claim_run(str(root_run))
    assert root.get_or_create_session(), root.last_error
    assert root._kv_set('history_url', root_url)
    child = pg.child_mgr('M0')
    child.claim_run(str(child_run))
    assert child.get_or_create_session(), child.last_error
    assert child._kv_set('history_url', child_url)
    assert W.ctl(pg.sock, 'kv.get', key='run').get('value') == str(root_run)
    assert W.ctl(pg.sock, 'kv.get', key='history_url').get('value') == root_url
    assert child.kv_get('run') == str(child_run) and child.kv_get('history_url') == child_url
    hello, reply = W.on_page(pg.sock, 'M0', 'kv.get', key='run')
    assert hello.get('run') == str(child_run) and reply.get('value') == str(child_run), (hello, reply)
    # the reserved keys live on the root page only
    assert child._kv_set('sidebar', '{"v":1}') is False
    _, reply = W.on_page(pg.sock, 'M0', 'kv.set', key='stop_request', value='{}')
    assert reply.get('ok') is False and reply.get('code') == 'bad_request', reply


@milestone(4)
def test_arrange_keeps_one_history_pane_per_page(pg):
    root_run, root_url = W.make_run(pg.root, 'run-root')
    child_run, child_url = W.make_run(pg.root, 'run-M0')
    root = up(pg)
    rs, rw = spawn(root, 'fx-supervisor', 'supervisor'), spawn(root, 'fx-worker-1')
    hist_root = root.arrange(rs, [rw], root_url)
    assert hist_root, root.last_error
    child = pg.child_mgr('M0')
    assert child.get_or_create_session(), child.last_error
    cs, cw = spawn(child, 'fx-M0/supervisor', 'supervisor'), spawn(child, 'fx-M0/worker-1')
    hist_child = child.arrange(cs, [cw], child_url)
    assert hist_child and hist_child != hist_root, child.last_error
    assert root.arrange(rs, [rw], root_url) == hist_root  # the root's own history pane is kept, not respawned
    per_page = {}
    for row in W.all_panes(pg.sock):
        if row.get('type') == 'history':
            per_page.setdefault(row.get('page'), []).append(str(row['pane']))
    assert per_page == {'': [hist_root], 'M0': [hist_child]}, per_page
    assert page_groups(pg, 'M0')['lower'][0] == hist_child and page_groups(pg, '')['lower'][0] == hist_root
    assert child.kv_get('history_url') == child_url and root.kv_get('history_url') == root_url


# ================================================================ read-only record pages (§5.4)

@milestone(4)
def test_archive_page_opens_a_reclaimed_page_with_a_readable_history(pg):
    root = up(pg)
    rw = spawn(root, 'fx-worker-1')
    child_run, child_url = W.make_run(pg.root, 'run-M0')
    assert W.need(root, 'archive_page')('M0', str(child_run), 'sub-fleet M0') is True, root.last_error
    row = W.pages(pg.sock)['M0']
    assert row['state'] == 'reclaimed' and row['title'] == 'sub-fleet M0', row
    _, reply = W.on_page(pg.sock, 'M0', 'kv.get', key='history_url')
    assert reply.get('value') == child_url, reply
    archived = archive_rows(root, 'M0', pg.sock)
    assert len(archived) == 1, root._surfaces()
    rec = W.record(pg.sock, archived[0])
    assert rec['type'] == 'history', rec
    args = W.identity(int(rec['pid']))
    assert args and 'fleetmux.history_view' in args[1] and str(child_run) in args[1], args
    assert ids(page_list(pg, '')) == [rw]  # the root page did not get it
    assert W.pages(pg.sock)['M0']['state'] == 'reclaimed'  # a history pane never reopens the page


@milestone(4)
def test_archive_page_is_idempotent(pg):
    root = up(pg)
    child_run, _ = W.make_run(pg.root, 'run-M0')
    archive = W.need(root, 'archive_page')
    assert archive('M0', str(child_run), 'M0') is True, root.last_error
    first = archive_rows(root, 'M0', pg.sock)
    assert archive('M0', str(child_run), 'M0') is True, root.last_error
    assert archive_rows(root, 'M0', pg.sock) == first and len(first) == 1
    assert [p for p in W.pages(pg.sock) if p == 'M0'] == ['M0']


@milestone(4)
def test_unarchive_page_kills_only_the_archive_pane(pg):
    root = up(pg)
    rw = spawn(root, 'fx-worker-1')
    m0_run, _ = W.make_run(pg.root, 'run-M0')
    m1_run, _ = W.make_run(pg.root, 'run-M1')
    archive = W.need(root, 'archive_page')
    assert archive('M0', str(m0_run), 'M0') and archive('M1', str(m1_run), 'M1'), root.last_error
    m0, m1 = archive_rows(root, 'M0', pg.sock), archive_rows(root, 'M1', pg.sock)
    assert len(m0) == len(m1) == 1
    assert W.need(root, 'unarchive_page')('M0') is True, root.last_error
    assert W.record(pg.sock, m0[0]) == {} and m0[0] not in root._surfaces()
    assert W.record(pg.sock, m1[0]) and W.record(pg.sock, rw)
    assert root.unarchive_page('nope') is True  # nothing to do


@milestone(4)
def test_unarchive_page_also_clears_descendant_records(pg):
    root = up(pg)
    m0_run, _ = W.make_run(pg.root, 'run-M0')
    W.sub_run(m0_run, 'M0.1', 'M0.1', state='returned')  # a reclaimed grandchild under M0 (§5.4 item 6)
    m1_run, _ = W.make_run(pg.root, 'run-M1')
    m10_run, _ = W.make_run(pg.root, 'run-M10')
    archive = W.need(root, 'archive_page')
    for page, run in (('M0', m0_run), ('M1', m1_run), ('M10', m10_run)):
        assert archive(page, str(run), page), root.last_error
    assert len(archive_rows(root, 'M0.1', pg.sock)) == 1  # archived by subtree, registered in the root's registry
    assert W.pages(pg.sock)['M0.1']['state'] == 'reclaimed'
    keep = archive_rows(root, 'M10', pg.sock)
    assert root.unarchive_page('M1') is True
    assert archive_rows(root, 'M1', pg.sock) == [] and archive_rows(root, 'M10', pg.sock) == keep  # 'M1.' prefix only
    assert root.unarchive_page('M0') is True
    assert archive_rows(root, 'M0', pg.sock) == [] and archive_rows(root, 'M0.1', pg.sock) == []
    assert archive_rows(root, 'M10', pg.sock) == keep


# ================================================================ dual tmux view: the visible client sizes the window

@pytest.fixture(params=['real'])
def pg_real(request, monkeypatch):
    if not shutil.which('tmux'):
        pytest.skip('tmux not installed (deps.yaml requires it)')
    root = W.new_root('pgt', request, request.param)
    W.install_fleetmux(monkeypatch, root, request.param)
    state = root / 'state'
    state.mkdir()
    s = W.Servers(root, request.param, state)
    s.tmux_sock = Path('/tmp/fmx-' + uuid.uuid4().hex[:8]) / 'tmux.sock'  # short: macOS unix socket limit
    yield s
    from fleetmux.backends.cmux import _tmux
    _tmux(str(s.tmux_sock), 'kill-server')
    shutil.rmtree(s.tmux_sock.parent, ignore_errors=True)
    s.close()


def sized(s, pane_id):
    """(window w, window h, client w, client h) as tmux sees it from the client of ``pane_id``'s tab."""
    from fleetmux.backends.cmux import _tmux
    tty = W.record(s.sock, pane_id).get('tty')
    out = _tmux(str(s.tmux_sock), 'list-clients', '-F',
                '#{client_tty} #{window_width} #{window_height} #{client_width} #{client_height}').stdout
    row = next((line.split()[1:] for line in out.splitlines() if line.split()[:1] == [tty]), None)
    return tuple(map(int, row)) if row else None


def fills(s, pane_id):
    """No `·` fill in that client: the window is the client's width and height (less tmux's one status line)."""
    got = sized(s, pane_id)
    return bool(got) and got[0] == got[2] and got[1] == got[3] - 1


@milestone(4)
@pytest.mark.parametrize('current', ['', 'M0'])
def test_the_visible_tmux_client_sizes_the_window_after_attach_and_page_switch(pg_real, current):
    """The parent page's live tab and the child page's view are two clients of one tmux session with different
    sizes (`window-size latest`). Only one page shows at a time: the client on the current page must size the
    window -- right after the child starts (no page switch: the parent page or the child page in view while it
    attaches and `arrange` moves the view top-left), and after every page switch (plan worker-area-mapping
    follow-up 4; user 2026-11-09: the fill was still there right after the start, gone after one switch)."""
    s = pg_real
    root = up(s)
    spawn(root, 'fx-supervisor', 'supervisor')
    sock = str(s.tmux_sock)
    s.tmux_sock.parent.mkdir()
    res = root.spawn_interactive_cli('worker-1', 'sleep 600', metadata={'type': 'worker'}, start_directory='/tmp',
                                     tmux_socket=sock, tmux_session='worker-1-g1')
    assert res.success, res.error
    owner = res.pane_id
    assert W.wait_for(lambda: sized(s, owner))
    child = s.child_mgr('M0')
    assert child.spawn_window('daemon', 'sleep 600', metadata={'type': 'daemon'}, start_directory='/tmp').success
    if current:
        assert W.ctl(s.sock, 'page.select', page=current).get('ok')
    # subfleet.start_adopted's order: the child's workers, the view before the first one, then arrange
    workers = [spawn(child, 'fx-M0/worker-' + str(i), 'worker') for i in (1, 2)]
    view = child.attach_view({'type': 'worker', 'label': 'fx-worker-1.1', 'tmux_socket': sock,
                              'tmux_session': 'worker-1-g1'}, workers[0])
    assert view, child.last_error
    run, url = W.make_run(s.root, 'run-root/sub/M0')
    assert child.arrange(view, workers, url), child.last_error
    assert W.wait_for(lambda: sized(s, view))
    assert sized(s, owner)[2:] != sized(s, view)[2:], 'the two tabs must differ in size for this test'
    assert W.current_page(s.sock) == current
    shown = view if current else owner
    assert W.wait_for(lambda: fills(s, shown), timeout=5), ('no switch yet', current, sized(s, shown))
    for page, shown in (('M0', view), ('', owner), ('M0', view), ('', owner)):
        assert W.ctl(s.sock, 'page.select', page=page).get('ok')
        assert W.wait_for(lambda: fills(s, shown), timeout=5), (page, sized(s, shown))
