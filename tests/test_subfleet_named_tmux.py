"""Named service sub-fleets (§29) x dual display (§3.1) after the A1 + A2 merge: the real paths.

The supervisor_slot is declared in config before the first spawn, so it starts in the fleet's tmux; a
named service sub-fleet then shows the same session in both workspaces. The fake cmux world records
every ``adopt`` / ``release`` (the §19 move-surface primitives): on the dual-display path the parent's
tab of the slot must never be moved. Same two-Redis / in-process daemon world as test_subfleet.py,
plus the tmux primitives of test_subfleet_tmux.py (real tmux: test_tmux_view.py).
"""
import json
import os
from pathlib import Path

import pytest

from test_subfleet import (World, adopt_by_hook, WorldMgr, ask, child_daemon, die, hook, no_launch, parent_hook, result,
                           start_tree, step, stop_child_like_daemon, supervisor_answers, supervisor_texts,
                           typed_return)
from test_subfleet_tmux import attach_view, detach_view, meta, tmux_view_info
from cmux_fleet import fleet, named_subfleet, roster, subfleet

# §48 (user 2026-11-09:「任何场景都不用 claude 输入框打字」): these tests exercised a channel that typed into a
# Claude input box; it is refused at start now (tests/test_no_claude_typing.py covers the refusal).
RETIRED = pytest.mark.skip(reason='docs/design.md §48: fleet never types into a Claude input box (2026-11-09)')


SUP_ROLE = 'You are the ops sub-fleet supervisor. ROLE-MARKER-OPS.\n'


def write_roster(tmp, autostart=False, declare=True, bump=0):
    (tmp / 'sup.md').write_text(SUP_ROLE)
    (tmp / 'child.yaml').write_text('workers:\n  - name: ops-1\n')
    body = 'workers:\n  - name: worker-1\n  - name: worker-2\n'
    if declare:
        body += ('subfleets:\n  - name: ops\n    supervisor_slot: worker-1\n    supervisor_role: sup.md\n'
                 '    roster: child.yaml\n    autostart: ' + str(autostart).lower() + '\n    drain_s: 60\n')
    path = tmp / 'roster.yaml'
    path.write_text(body)
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000 * (bump + 1)))
    return path


def tmux_world(monkeypatch):
    """World + managers with the dual-display primitives; tmux spawns register the session owner;
    every adopt / release (move-surface) is recorded."""
    init = World.__init__

    def world_init(self, state_dir):
        init(self, state_dir)
        self.views, self.detached, self.tmux_owner, self.moves = {}, [], {}, []
    monkeypatch.setattr(World, '__init__', world_init)
    spawn, adopt, release = WorldMgr.spawn_interactive_cli, WorldMgr.adopt, WorldMgr.release

    def spawn_cli(self, **kw):
        res = spawn(self, **kw)
        if kw.get('tmux_session'):  # cmux_manager starts this generation inside the fleet's tmux server
            self.w.tmux_owner[res.pane_id] = kw['tmux_session']
        return res

    def adopt_rec(self, sid, peer, info):
        self.w.moves.append(('adopt', self.session, sid))
        return adopt(self, sid, peer, info)

    def release_rec(self, sid):
        self.w.moves.append(('release', self.session, sid))
        return release(self, sid)
    for name, fn in (('tmux_view_info', tmux_view_info), ('attach_view', attach_view), ('detach_view', detach_view),
                     ('_meta', meta), ('spawn_interactive_cli', spawn_cli), ('adopt', adopt_rec),
                     ('release', release_rec)):
        monkeypatch.setattr(WorldMgr, name, fn, raising=False)
    monkeypatch.setattr(subfleet, 'default_launch', no_launch)


def service_tree(tmp_path, monkeypatch, declared_at_start=True, autostart=False):
    """A service main fleet started like `start --roster`: cfg['subfleets'] is in config before start_all."""
    tmux_world(monkeypatch)
    path = write_roster(tmp_path, autostart=autostart)
    over = dict(mode='service', max_attempts=1, roster=str(path))
    if declared_at_start:
        over['subfleets'] = roster.load_subfleets(path)
    gen = start_tree(tmp_path, monkeypatch, **over)
    t = next(gen)
    t.gen = gen
    t.f.roster_tick()  # first daemon tick: the roster declares the named sub-fleet
    return t


@pytest.fixture
def ntmux(tmp_path, monkeypatch):
    t = service_tree(tmp_path, monkeypatch)
    yield t
    for _ in t.gen:
        pass


def moved(t, sid):
    return [m for m in t.world.moves if m[2] == sid]


def adopt_view_child(t, name='ops'):
    """The child daemon boots; its supervisor is a second client tab of the slot's tmux session."""
    rec = subfleet.record(t.f.run_dir / 'sub' / name)
    # §48: the role change reaches the view through the pane's (parent-pinned) Stop hook, never typed
    cf, _ = adopt_by_hook(t, t.monkeypatch, rec)
    view = cf.agent('supervisor')['pane']
    assert not [x for p, x in t.world.sent if p == view]
    return rec, cf, view


def approve_up(t, name='ops'):
    rid, answer = ask(t, 'up', name)
    assert answer['status'] == 'accepted', answer
    supervisor_answers(t, rid)
    subfleet.tick(t.f)
    return rid


def test_start_writes_the_declaration_into_config_before_the_first_spawn(tmp_path, monkeypatch):
    from test_pi import mock_start
    args, _ = mock_start(tmp_path, monkeypatch)
    path = write_roster(tmp_path)
    fleet.main(args + ['--roster', str(path)])
    cfg = fleet.load(tmp_path / 'latest.json')
    assert [(e['name'], e['supervisor_slot']) for e in cfg['subfleets']] == [('ops', 'worker-1')]
    assert fleet.load(Path(cfg['config']))['subfleets'] == cfg['subfleets']  # the daemon reads this file
    assert subfleet.tmux_spawn_args(cfg, 'worker-1', 0) == {'tmux_socket': str(Path(cfg['run']) / 'tmux.sock'),
                                                            'tmux_session': 'worker-1-g0'}
    assert subfleet.tmux_spawn_args(cfg, 'worker-2', 0) == {}


@RETIRED
def test_service_up_request_and_down_show_one_session_in_both_workspaces_without_moving_it(ntmux):
    t, f = ntmux, ntmux.f
    typed_return(t)  # same-session return: typed path only (see test_subfleet.typed_return)
    sid = f.agent('worker-1')['pane']
    first = {kw['window_name']: kw for kw in f.mgr.spawned}
    assert first['fx-worker-1']['tmux_session'] == 'worker-1-g0' and 'tmux_session' not in first['fx-worker-2']
    rid = approve_up(t)
    rec = subfleet.record(f.run_dir / 'sub' / 'ops')
    # the parent keeps its own tab: no placeholder, nothing released, the child registry has nothing of it
    assert rec['view'] == 1 and rec['placeholder'] == ''
    assert not [w for w, _ in getattr(f.mgr, 'windows', []) if w.startswith('placeholder-')]
    info = f.agent('worker-1')
    assert info['state'] == 'delegated' and info['pane'] == sid and t.world.owner[sid] == 'cmux-fleet'
    assert sid not in json.loads(Path(t.world.mgrs['cmux-fleet:ops'].meta_path).read_text()).get('surfaces', {})
    rec, cf, view = adopt_view_child(t)
    assert view != sid and t.world.views[view]['tmux_session'] == 'worker-1-g0'
    assert t.world.owner[view] == 'cmux-fleet:ops' and t.world.owner[sid] == 'cmux-fleet'  # two clients
    subfleet.tick(f)
    res = result(f, rid)
    child_cfg = fleet.load(Path(rec['run']) / 'config.json')
    assert res['status'] == 'ok' and res['state'] == 'up' and res['redis_socket'] == child_cfg['redis_socket']
    assert 'subfleets' not in child_cfg  # never inherited by the child
    assert not any(kw.get('tmux_session') for kw in cf.mgr.spawned)  # child workers are plain panes
    # a service request at the child's socket is typed into the child's client tab, never the parent's
    spec = dict(id='q1', payload={'message': 'check the gateway'}, reply={'required': ['reply']})
    from test_subfleet import drive
    assert drive(t, lambda: fleet.request(child_cfg, dict(kind='request', spec=spec)))['status'] == 'accepted'
    step(t)
    assert 'check the gateway' in [x for p, x in t.world.sent if p == view][-1]
    assert not any('check the gateway' in x for p, x in t.world.sent if p == sid)
    # the session answers it (turn end through the parent's worker-1 hook, routed into the child)
    prompt = [x for p, x in t.world.sent if p == view][-1]
    skip = json.dumps({'status': 'RUNNING', 'dispatch': [], 'skip': [{'request_id': 'q1', 'reason': 'test'}]})
    assert parent_hook(t, skip, prompt)['status'] == 'committed'
    assert json.loads(fleet.connect(child_cfg).get(fleet.RESULT + 'q1'))['status'] == 'skipped'
    # down: drained, the child detaches its client, the parent tab is told it is a worker again
    down, _ = ask(t, 'down')
    supervisor_answers(t, down)
    subfleet.tick(f)  # approved: draining
    assert named_subfleet.state(f, 'ops')['state'] == 'draining'
    subfleet.tick(f)  # quiet: stop posted to the child
    stop_child_like_daemon(cf)
    assert t.world.detached == [view] and fleet.load(Path(rec['run']) / 'handback.json')['pane'] == sid
    subfleet.tick(f)
    rec = subfleet.record(rec['run'])
    assert rec['state'] == 'down' and result(f, down)['state'] == 'down'
    info = f.agent('worker-1')
    assert info['pane'] == sid and info['generation'] == '0' and info['state'] == 'starting'
    assert 'worker of the main fleet again' in [x for p, x in t.world.sent if p == sid][-1]
    assert not any('[Sub-fleet' in x for x in supervisor_texts(f))
    assert moved(t, sid) == []  # never a move-surface of the parent's tab


@RETIRED
def test_autostart_crash_revive_and_crashloop_leave_the_parent_tab_in_place(tmp_path, monkeypatch):
    t = service_tree(tmp_path, monkeypatch, autostart=True)
    try:
        f = t.f
        typed_return(t)  # same-session return: typed path only (see test_subfleet.typed_return)
        sid = f.agent('worker-1')['pane']
        subfleet.tick(f)  # declared + idle READY: delegated at once
        rec, cf, view = adopt_view_child(t)
        child_cfg = fleet.load(Path(rec['run']) / 'config.json')
        mgr = t.world.mgrs['cmux-fleet:ops']
        import time
        for crash in range(1, 4):
            die(cf)
            subfleet.shutdown_redis(child_cfg)
            rec = subfleet.record(rec['run'])
            rec['launched'] = rec['created'] = time.time() - subfleet.LAUNCH_GRACE - 1
            subfleet.save_record(rec['run'], rec)
            subfleet.tick(f)
            rec = subfleet.record(rec['run'])
            if crash < 3:
                assert rec['state'] == 'running' and '--resume' in mgr.windows[-1][1]
                assert t.world.detached == [] and sid in t.world.alive  # relaunch never touches the tabs
                cf = fleet.Fleet(Path(rec['run']) / 'config.json', fleet.connect(child_cfg),
                                 t.world.manager(child_cfg), sessions=t.sessions)
                import fcntl
                cf.lock = (Path(rec['run']) / 'daemon.lock').open('a+')
                fcntl.flock(cf.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                t.fleets.append(cf)
        # crashloop: collected; the client tab the dead child left is detached, the session stays
        assert rec['state'] == 'failed' and rec['outcome'] == 'crashloop'
        assert t.world.detached == [view] and view not in t.world.alive
        assert f.agent('worker-1')['pane'] == sid and sid in t.world.alive and t.world.owner[sid] == 'cmux-fleet'
        assert named_subfleet.state(f, 'ops')['crashloop'] == '1' and moved(t, sid) == []
    finally:
        for _ in t.gen:
            pass


def test_delegation_after_rotation_uses_the_next_generations_tmux_session(ntmux, monkeypatch):
    t, f = ntmux, ntmux.f
    usage = {'worker-1': (90, 100)}
    monkeypatch.setattr(f, 'usage', lambda name: usage.get(name, (0, 0)))
    rotations = []
    monkeypatch.setattr(f, 'start_rotation', lambda name, used, window: rotations.append(name)
                        or f.setf(name, state='handoff'))
    f.setf('worker-1', delivered=3)
    approve_up(t)
    assert rotations == ['worker-1'] and not subfleet.active_children(f.run_dir)
    # the successor generation is spawned by the normal path: it starts in tmux as <slot>-g1
    f.setf('worker-1', generation=1, label='worker-1.1')
    new = f.spawn('worker-1', 'READY please')
    assert f.mgr.spawned[-1]['tmux_session'] == 'worker-1-g1' and t.world.tmux_owner[new] == 'worker-1-g1'
    usage['worker-1'] = (5, 100)
    f.setf('worker-1', pane=new, state='idle', runtime_ready=1, delivered=0)
    subfleet.tick(f)
    rec = subfleet.record(f.run_dir / 'sub' / 'ops')
    assert rec['view'] == 1 and rec['pane'] == new and rec['generation'] == 1
    cf = child_daemon(t, rec)
    view = cf.agent('supervisor')['pane']
    assert t.world.views[view]['tmux_session'] == 'worker-1-g1' and moved(t, new) == []


@RETIRED
def test_parent_resume_takes_back_external_and_reattaches_autostart_views(ntmux):
    t, f = ntmux, ntmux.f
    typed_return(t)  # same-session return: typed path only (see test_subfleet.typed_return)
    sid = f.agent('worker-1')['pane']
    approve_up(t)
    rec, cf, view = adopt_view_child(t)
    die(cf)  # the parent daemon restarts; this child is gone but its client tab is still open
    subfleet.resume_children(f, launch=lambda *a: pytest.fail('an external sub-fleet is never relaunched'))
    assert t.world.detached == [view] and subfleet.record(rec['run'])['state'] == 'down'
    assert named_subfleet.state(f, 'ops')['state'] == 'down' and f.agent('worker-1')['pane'] == sid
    parent_hook(t, 'READY', [x for p, x in t.world.sent if p == sid][-1])
    path = write_roster(t.tmp, autostart=True, bump=1)
    assert path == Path(f.cfg['roster'])
    f.roster_tick()
    subfleet.tick(f)
    rec, cf, view = adopt_view_child(t)
    die(cf)
    launched = []
    subfleet.resume_children(f, launch=lambda p, cfg, mgr: launched.append(p))
    assert launched == [Path(rec['run']) / 'config.json']
    # the relaunched child (daemon --resume) keeps the same client tab as its supervisor: no new client
    cfg = fleet.load(Path(rec['run']) / 'config.json')
    again = fleet.Fleet(Path(rec['run']) / 'config.json', fleet.connect(cfg), t.world.manager(cfg), sessions=t.sessions)
    t.fleets.append(again)
    again.resume_requested = True
    views = dict(t.world.views)
    again.v2.resume_run()
    assert again.agent('supervisor')['pane'] == view and t.world.views == views
    assert moved(t, sid) == []


def test_hot_declared_plain_slot_falls_back_once_then_its_next_generation_runs_in_tmux(tmp_path, monkeypatch):
    t = service_tree(tmp_path, monkeypatch, declared_at_start=False)
    try:
        f = t.f
        sid = f.agent('worker-1')['pane']
        assert 'tmux_session' not in f.mgr.spawned[1] and sid not in t.world.tmux_owner  # a plain cmux pane
        # the reload put the declaration into memory and into config.json (resume reads it)
        assert [e['supervisor_slot'] for e in f.cfg['subfleets']] == ['worker-1']
        assert fleet.load(f.path)['subfleets'] == f.cfg['subfleets']
        approve_up(t)
        rec = subfleet.record(f.run_dir / 'sub' / 'ops')
        assert rec['view'] == 0 and rec['placeholder']  # chosen behaviour: this delegation is a move-surface
        assert ('release', 'cmux-fleet', sid) in t.world.moves
        audit = [x[1] for x in f.r.xrange(f.v2.bus.audit_key) if x[1].get('event') == 'subfleet.view_fallback']
        assert len(audit) == 1 and audit[0]['slot'] == 'worker-1'
        f.setf('worker-1', generation=1)
        f.spawn('worker-1', 'READY please')
        assert f.mgr.spawned[-1]['tmux_session'] == 'worker-1-g1'  # from its next generation: dual display
    finally:
        for _ in t.gen:
            pass


def test_hot_added_supervisor_slot_spawns_in_tmux_from_its_first_generation(tmp_path, monkeypatch):
    """10-14 Studio: a worker added together with its sub-fleet entry (hot roster) spawned before
    cfg['subfleets'] knew it, so it came up as a plain tab (and, with the advisor on, held for a sheet)."""
    t = service_tree(tmp_path, monkeypatch, declared_at_start=False)
    try:
        f = t.f
        (tmp_path / 'sup.md').write_text(SUP_ROLE)
        (tmp_path / 'child.yaml').write_text('workers:\n  - name: ops-1\n')
        path = tmp_path / 'roster.yaml'
        path.write_text('workers:\n  - name: worker-1\n  - name: worker-2\n  - name: worker-3\n'
                        'subfleets:\n  - name: ops\n    supervisor_slot: worker-3\n    supervisor_role: sup.md\n'
                        '    roster: child.yaml\n    autostart: false\n    drain_s: 60\n')
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 9_000_000_000))
        f.roster_tick()
        assert 'worker-3' in f.workers
        assert [e['supervisor_slot'] for e in f.cfg['subfleets']] == ['worker-3']
        assert fleet.load(f.path)['subfleets'] == f.cfg['subfleets']
        spawned = [s for s in f.mgr.spawned if s.get('tmux_session', '').startswith('worker-3')]
        assert spawned and spawned[-1]['tmux_session'] == 'worker-3-g0'  # dual display from generation 0
    finally:
        for _ in t.gen:
            pass
