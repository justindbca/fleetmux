"""M4 sub-fleets on the fleetmux backend: helper tests, not acceptance (design-1/m4-design.md v0.7 §8.3).

``tests/test_subfleet.py``'s world (real private Redis, in-process daemons, ``hook_sim`` turns), with the terminal
manager swapped for the real fleetmux backend on the fake server (``fake``) or this worktree's binary (``real``). The
panes are real PTYs on one fleetmux server; only the CLI a pane would run is replaced (``claude -> /bin/sleep``) and
what fleet would type into a pane is recorded instead (``world.sent``, as ``WorldMgr`` does), so no real CLI ever
runs. brief §5's acceptance evidence is §8.4 E2 only; these tests locate regressions quickly.

Red until phase B: today ``subfleet.backend_problem`` refuses fleetmux, the child config carries no binding keys, and
the M4 manager methods / pages do not exist. A test reaches an M4-only API through ``_fm4_world.need`` (a failure,
never an error); fixtures only use what M3 has. Evidence roots ``~/.m4e/fm4-py/sf-*`` (and ``st-*`` / ``jp-*``) are
kept; teardown is the product's stop (Redis ``SHUTDOWN``, ``stop_tmux``, ``close_all`` / ``end_server``) plus the
identity-checked pid fallback, never ``rmtree`` / ``pkill``.
"""
import json
import os
import shlex
import shutil
import socket
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _fake_claude  # noqa: E402
import _fm4_world as W  # noqa: E402
import hook_sim  # noqa: E402,F401  (the conftest stand-in drives the Claude turns)
import test_subfleet as ts  # noqa: E402  (helpers only: no fixture of it is used here)
from _fleetmux_milestone import milestone  # noqa: E402
from test_fleet import received  # noqa: E402
from fleetmux import fleet, fleetmux_sidebar, named_subfleet, roster, subfleet  # noqa: E402
from fleetmux.backends import fleetmux as fm  # noqa: E402

RS = W.ROOT_SESSION
OLD_REFUSAL = 'sub-fleets are not supported on the fleetmux backend'
FX_FIELDS = {'state', 'slot', 'redis_socket', 'run_dir', 'up_at'}  # named_subfleet.py:7-11 (documented hash fields)


# ================================================================ the fleetmux world

def idle_launch(path, cfg, mgr):
    """``default_launch``'s pane (``type: daemon`` in the child's page) running nothing: the test drives the
    child daemon in process (``test_subfleet.child_daemon``)."""
    return mgr.spawn_window('daemon', 'sleep 600', metadata={'type': 'daemon'}, start_directory=str(cfg['run']))


def fm_world(root, monkeypatch):
    """Patch the real fleetmux manager class: panes stay real, the CLI and the typing are stand-ins."""
    cli = root / 'cli'
    cli.mkdir()
    claude = _fake_claude.make(cli)  # comm 'claude' for has_cli_process; never a real CLI
    world = SimpleNamespace(sent=[], keys=[], queued=[], spawned=[], root=root, state_dir=root / 'state',
                            claude=str(claude))
    world.manager = fleet.manager_for  # every in-process daemon builds its manager like the real one
    cls = fm.FleetmuxSessionManager
    spawn = cls.spawn_interactive_cli

    def spawn_cli(self, window_name, cli_command, prompt_file=None, output_file=None, metadata=None,
                  start_directory=None, auto_close=True, env_vars=None, tmux_socket=None, tmux_session=None, **kw):
        world.spawned.append({'session': self.session_name, 'window_name': window_name, 'cli': cli_command,
                              'tmux_session': tmux_session})
        return spawn(self, window_name, shlex.quote(str(claude)) + ' ' + _fake_claude.args(600), None, output_file,
                     metadata, start_directory, auto_close, env_vars, tmux_socket, tmux_session)

    def send_prompt_to_idle(self, pane, text, bracketed=False):
        world.sent.append((pane, text))
        return self.owns(pane)

    def send_key(self, pane, key):
        world.keys.append((pane, key))
        world.sent.append((pane, '<key ' + key + '>'))
        return self.owns(pane)

    monkeypatch.setattr(cls, 'spawn_interactive_cli', spawn_cli)
    monkeypatch.setattr(cls, 'send_prompt_to_idle', send_prompt_to_idle)
    monkeypatch.setattr(cls, 'send_key', send_key)
    monkeypatch.setattr(cls, 'w', world, raising=False)  # conftest's Codex queue stand-in logs into mgr.w
    return world


def start_fm_tree(request, monkeypatch, kind, tag='sf', prepare=None, **over):
    """``test_subfleet.start_tree`` on fleetmux: a v2 main fleet on its own private Redis, kept evidence root."""
    for dep in ('redis-server', 'tmux'):
        if shutil.which(dep) is None:
            pytest.fail('missing dependency: ' + dep)
    root = W.new_root(tag, request, kind)
    W.install_fleetmux(monkeypatch, root, kind)
    (root / 'tmux-default').mkdir()
    monkeypatch.setenv('TMUX_TMPDIR', str(root / 'tmux-default'))
    monkeypatch.setattr(fleet, 'trust_claude_dir', lambda cwd: None)  # never touch ~/.claude.json
    world = fm_world(root, monkeypatch)
    rcfg, r = ts.start_private(root / 'r')
    run = root / 'fleet-20261104-000000-a1b2c3d4'
    (run / 'workspace').mkdir(parents=True)
    cfg = dict(rcfg, run=str(run), python=sys.executable, workers=['worker-1', 'worker-2'], mode='task',
               supervisor_cli='codex', worker_cli='claude', codex_home=str(root / 'codex-home'),
               allow_development_v2=True, stall_timeout=0, v2_lease_ms=300000, max_busy=0, max_attempts=2,
               name='研报', token='a1b2c3d4', backend='fleetmux', cmux_state_dir=str(root / 'state'),
               redis_runtime_root=str(root / 'r'))
    cfg.update(over)
    if prepare:
        cfg.update(prepare(root))
    path = run / 'config.json'
    fleet.save(path, cfg)
    t = SimpleNamespace(world=world, root=root, sessions=root / 'codex-home' / 'sessions', kind=kind,
                        claude=root / 'claude-projects', threads={}, fleets=[], tmp=root)
    t.servers = W.Servers(root, kind, root / 'state')
    t.sock = W.ctl_sock(root / 'state')
    try:
        f = fleet.Fleet(path, r, fleet.manager_for(cfg), sessions=t.sessions)
        t.f = f
        t.fleets.append(f)
        f.start_all()
        f.r.set(f.v2.prefix + ':boot', 'running')
        for slot in f.names:
            assert ts.hook(t, f, slot, 'READY', prompt='startup')['status'] == 'ready'
        if cfg['mode'] == 'task':
            from fleetmux import runtime_v2
            runtime_v2.post_event(f.v2.bus, dict(kind='task', request_id='new-task',
                                                 spec={'goal': 'write the report'}), 'new-task')
        ts.step(t)
    except BaseException:
        stop_fm_tree(t)
        raise
    return t


def _depth(p):
    return len(Path(p).parts)


def stop_fm_tree(t):
    """Product stop of the whole tree (deepest child first), then the identity-checked fallback."""
    s, root = t.servers, t.root
    s.register_servers()
    for pid_file in sorted(root.glob('r/*/redis.pid')):
        try:
            s.ledger.register('redis:' + pid_file.parent.name, pid_file.read_text().strip())
        except OSError:
            pass
    for sock in sorted(root.rglob('tmux.sock')):
        done = subprocess.run(['tmux', '-S', str(sock), 'display-message', '-p', '#{pid}'],
                                capture_output=True, text=True, timeout=10)
        s.ledger.register('tmux-server:' + str(sock.parent), done.stdout.strip())
    log = []
    for x in getattr(t, 'fleets', []):
        try:
            x.v2.close()
        except Exception as exc:  # noqa: BLE001
            log.append(['v2.close', str(exc)])
    run = root / 'fleet-20261104-000000-a1b2c3d4'
    configs = sorted(run.rglob('sub/*/config.json'), key=_depth, reverse=True) + [run / 'config.json']
    for path in configs:
        try:
            cfg = fleet.load(path)
        except (OSError, ValueError):
            continue
        for name, step in (('shutdown_redis', lambda: subfleet.shutdown_redis(cfg)),
                           ('stop_tmux', lambda: subfleet.stop_tmux(cfg)),
                           ('close_all', lambda: fleet.manager_for(cfg).close_all())):
            try:
                log.append([name, str(path), str(step())])
            except Exception as exc:  # noqa: BLE001 - teardown goes on
                log.append([name, str(path), 'error: %s' % exc])
    log += s.stop()
    W.wait_for(lambda: not any(s.ledger.alive(r['role']) for r in s.ledger.rows), timeout=5)
    W.fail_if_signalled(root, s.ledger.fallback(), log)


@pytest.fixture(params=['fake', 'real'])
def fmtree(request, monkeypatch):
    t = start_fm_tree(request, monkeypatch, request.param)
    yield t
    stop_fm_tree(t)


def fm_service_tree(request, monkeypatch, kind, autostart=False):
    """A service main fleet as ``start --roster`` makes it (cfg['subfleets'] in config before start_all, so the
    supervisor_slot worker-1 starts in the fleet's tmux), with ``default_launch`` = :func:`idle_launch`."""
    monkeypatch.setattr(subfleet, 'default_launch', idle_launch)

    def declare(root):
        path = ts.write_roster(SimpleNamespace(tmp=root), autostart=autostart)
        return {'roster': str(path), 'subfleets': roster.load_subfleets(path)}
    t = start_fm_tree(request, monkeypatch, kind, tag='sf', prepare=declare, mode='service', max_attempts=1)
    t.f.roster_tick()  # first daemon tick: the roster declares the named sub-fleet
    return t


@pytest.fixture(params=['fake', 'real'])
def fmsvc(request, monkeypatch):
    t = fm_service_tree(request, monkeypatch, request.param)
    yield t
    stop_fm_tree(t)


# ---------------------------------------------------------------- shared steps

def page_of(t, pane):
    return W.page_of(t.sock, pane)


def pid_of(t, pane):
    return W.record(t.sock, pane).get('pid')


def slot_hook(t, fl, slot, text, prompt):
    """A turn end of ``fl``'s ``slot`` (the delegated pane's CLI still runs that slot's hook)."""
    gen = int(fl.agent(slot)['generation'])
    thread = t.threads[(str(fl.run_dir), slot, gen)]
    return ts.claude_turn(t, fl.path, slot, gen, thread, prompt, text)


def spawn_m0(t, fl=None, task_id='M0', slot='worker-1', workers=2):
    fl = fl or t.f
    out = t.root / (task_id + '.md')
    rec = subfleet.spawn_child(fl, slot, task_id, 'Research five sources and merge them', workers,
                               output=str(out), launch=idle_launch)
    return rec, out


def adopt_m0(t, rec, fl=None, slot='worker-1'):
    """The child daemon boots in process and the adopted pane answers READY through the parent's slot hook."""
    fl = fl or t.f
    cf = ts.child_daemon(t, rec)
    for w in cf.workers:
        assert ts.hook(t, cf, w, 'READY', prompt='startup')['status'] == 'ready'
    typed = received(t.world, rec['pane'])
    assert typed, ('the child daemon did not take over the delegated pane %s (child task %r, supervisor %r)'
                   % (rec['pane'], cf.task().get('status'), cf.agent('supervisor')))
    assert slot_hook(t, fl, slot, 'READY', typed[-1])['status'] == 'ready'
    ts.step(t)
    return cf


def fm_bring_up(t, name='ops'):
    """``test_subfleet.bring_up`` for a tmux supervisor_slot: the role change goes to the child's view tab of the
    session (as ``tests/test_subfleet_named_tmux.py`` adopt_view_child), READY comes through the parent's hook."""
    rid, answer = ts.ask(t, 'up', name)
    assert answer['status'] == 'accepted' and ts.result(t.f, rid) is None, (answer, ts.result(t.f, rid))
    ts.supervisor_answers(t, rid)
    subfleet.tick(t.f)
    assert named_subfleet.state(t.f, name)['state'] == 'starting', named_subfleet.state(t.f, name)
    rec = subfleet.record(t.f.run_dir / 'sub' / name)
    cf = ts.child_daemon(t, rec)
    for w in cf.workers:
        assert ts.hook(t, cf, w, 'READY', prompt='startup')['status'] == 'ready'
    view = cf.agent('supervisor')['pane']
    typed = [x for p, x in t.world.sent if p == view and view]
    assert typed, ('the child daemon did not show the delegated session in its page (child task %r, supervisor %r)'
                   % (cf.task().get('status'), cf.agent('supervisor')))
    assert 'Your role changed' in typed[-1]
    assert slot_hook(t, t.f, 'worker-1', 'READY', typed[-1])['status'] == 'ready'
    subfleet.tick(t.f)
    return rid, rec, cf


def fm_take_down(t, cf, name='ops'):
    """``test_subfleet.take_down``, ticking the parent until the drained child is asked to stop (at most 5 ticks)."""
    rid, _ = ts.ask(t, 'down', name)
    ts.supervisor_answers(t, rid)
    run = t.f.run_dir / 'sub' / name
    for _ in range(5):
        subfleet.tick(t.f)
        if subfleet.record(run).get('state') == 'stopping':
            break
    assert subfleet.record(run).get('state') == 'stopping', (subfleet.record(run), named_subfleet.state(t.f, name))
    ts.stop_child_like_daemon(cf)
    subfleet.tick(t.f)  # collected
    return rid


def finish_child(t, cf, rec, fl=None):
    """The child completes with its deliverable, the parent asks it to stop, collects it."""
    fl = fl or t.f
    for src, _ in rec.get('deliver') or []:
        Path(src).parent.mkdir(parents=True, exist_ok=True)
        Path(src).write_text('merged findings')
    cf.v2.finish('completed', 'all parts merged')
    cf.write_result()
    subfleet.tick(fl)
    assert subfleet.record(rec['run'])['state'] == 'stopping'
    ts.stop_child_like_daemon(cf)
    subfleet.tick(fl)
    return subfleet.record(rec['run'])


def run_m0_to_return(t, typed=False):
    if typed:
        ts.typed_return(t)
    sid = t.f.agent('worker-1')['pane']
    rec, _ = spawn_m0(t)
    cf = adopt_m0(t, rec)
    done = finish_child(t, cf, rec)
    assert done['state'] == 'returned', done
    return rec, sid, cf


def archives(mgr, page, sock):
    out = []
    for sid, info in mgr._surfaces().items():
        if info.get('type') == 'archive' and W.record(sock, sid).get('page') == page:
            out.append(sid)
    return out


def light_fleet(root, backend, **cfg_over):
    """Just enough of a Fleet for ``subfleet.child_config`` (no Redis, no panes)."""
    run = root / ('run-' + backend)
    (run / 'workspace').mkdir(parents=True, exist_ok=True)
    cfg = dict({'run': str(run), 'workers': ['worker-1'], 'supervisor_cli': 'codex', 'worker_cli': 'claude',
                'backend': backend, 'cmux_state_dir': str(root / 'state'), 'session': RS, 'name': 'tree'},
               **cfg_over)
    mgr = SimpleNamespace(workspace_id=lambda: 'ws')
    return SimpleNamespace(cfg=cfg, path=run / 'config.json', mgr=mgr, runtime_cfg=lambda slot: cfg,
                           agent=lambda slot: {'generation': '0', 'thread': ''}, run_dir=run,
                           label=lambda slot: slot)  # §46: child_config names the supervising generation


# ================================================================ 1. the refusal is gone, the gates stay

@milestone(4)
def test_backend_problem_allows_fleetmux_and_still_refuses_wezterm():
    assert subfleet.backend_problem({'backend': 'fleetmux'}) is None
    assert subfleet.backend_problem({'backend': 'cmux'}) is None and subfleet.backend_problem({}) is None
    wz = subfleet.backend_problem({'backend': 'wezterm'})
    assert wz and 'wezterm' in wz and 'sub-fleet' in wz and 'fleetmux' not in wz  # the old sentence, unchanged
    f = SimpleNamespace(cfg={'backend': 'wezterm', 'task_id': None})
    assert subfleet.check(f, 'worker-1', 'M1', 1) == wz


@milestone(4)
def test_start_roster_with_subfleets_on_fleetmux_is_accepted(request, monkeypatch):
    from test_pi import mock_start
    root = W.new_root('st', request, 'mock start')
    args, ran = mock_start(root, monkeypatch)
    mgr = SimpleNamespace(meta_path=root / 'state' / 'fleetmux.json', session_exists=lambda: False,
                          runtime_root=root / 'state' / 'fleetmux' / RS,
                          spawn_window=lambda *a, **kw: SimpleNamespace(success=True, pane_id='p-1', error=None))
    monkeypatch.setattr(fleet, 'resolve_backend', lambda requested=None, platform=None: ('fleetmux', None))
    monkeypatch.setattr(fleet, 'backends_check', lambda name: True)
    monkeypatch.setattr(fleet, 'manager_for', lambda cfg: mgr)
    path = ts.write_roster(SimpleNamespace(tmp=root))
    try:
        fleet.main(args + ['--backend', 'fleetmux', '--roster', str(path)])
        why = ''
    except SystemExit as exc:
        why = str(exc)
    W.note(root, 'start exit: %r' % why)
    assert OLD_REFUSAL not in why and 'rejected' not in why, why
    assert any('--unixsocket' in argv for argv in ran), 'start never got past the roster gate: %r' % why


@milestone(4)
def test_resume_with_subfleets_on_fleetmux_is_accepted(request, monkeypatch):
    root = W.new_root('st', request, 'resume')
    run = root / 'fleet-20261104-000000-b1b2c3d4'
    run.mkdir()
    sub = roster.load_subfleets(ts.write_roster(SimpleNamespace(tmp=root)))
    fleet.save(run / 'config.json', {'run': str(run), 'backend': 'fleetmux', 'subfleets': sub, 'codex_queue': False,
                                     'codex_version': '', 'cmux_state_dir': str(root / 'state'),
                                     'fleet_home': str(root / 'home')})

    class PastTheGate(Exception):
        pass

    def gate_passed(keep=None):
        raise PastTheGate()
    monkeypatch.setattr(fleet.runtime_v2, 'validate_config', lambda cfg: None)  # not under test here
    monkeypatch.setattr(fleet, 'resolve_backend', lambda requested=None, platform=None: ('fleetmux', None))
    monkeypatch.setattr(fleet, 'backends_check', lambda name: True)
    monkeypatch.setattr(fleet, 'sweep_orphan_redis', gate_passed)  # the step right after the sub-fleet gate
    args = SimpleNamespace(max_attempts=None, roster=None, advisor=False)
    with pytest.raises(PastTheGate):
        try:
            fleet.resume(run, args)
        except SystemExit as exc:
            pytest.fail('resume refused a fleetmux run with subfleets: %s' % exc)


@milestone(4)
def test_roster_hot_reload_adds_subfleets_on_fleetmux(request, monkeypatch):
    t = start_fm_tree(request, monkeypatch, 'fake', mode='service', max_attempts=1)
    try:
        f = t.f
        events = []
        real_log = f.log
        f.log = lambda kind, **data: (events.append((kind, data)), real_log(kind, **data))[1]
        f.cfg['roster'] = str(ts.write_roster(t))
        f.roster_tick()
        assert not [e for e in events if e[0] == 'roster.rejected'], events
        assert [e.get('name') for e in f.cfg.get('subfleets') or []] == ['ops']
        assert named_subfleet.state(f, 'ops').get('state') in (None, 'down', '')
    finally:
        stop_fm_tree(t)


@milestone(4)
def test_old_fleetmux_server_without_pages_refuses_with_reason(request, monkeypatch):
    monkeypatch.setenv('FAKE_FM_NO_PAGES', '1')
    root = W.new_root('st', request, 'fake FAKE_FM_NO_PAGES=1')
    W.install_fleetmux(monkeypatch, root, 'fake')
    (root / 'state').mkdir()
    s = W.Servers(root, 'fake', root / 'state')
    try:
        mgr = s.root_mgr()
        assert mgr.get_or_create_session(), mgr.last_error
        f = SimpleNamespace(cfg={'backend': 'fleetmux', 'task_id': None, 'run': str(root)}, mgr=mgr, run_dir=root)
        why = subfleet.check(f, 'worker-1', 'M0', 1)
        assert why and 'pages' in why and OLD_REFUSAL not in why, why
        named = {'name': 'ops', 'mode': 'service'}
        assert subfleet.check(f, 'worker-1', 'ops', 1, ready=False, named=named) == why
        # the child daemon side refuses with the same reason (start_adopted's capability gate)
        child = s.track(fleet.manager_for({'session': RS + ':M0', 'backend': 'fleetmux',
                                           'cmux_state_dir': str(root / 'state'),
                                           'fleetmux_root_session': RS, 'fleetmux_page': 'M0'}))
        finished = []
        cf = SimpleNamespace(cfg={'backend': 'fleetmux', 'adopt': {'pane': 'x-1', 'slot': 'worker-1'},
                                  'fleetmux_root_session': RS, 'fleetmux_page': 'M0'},
                             mgr=child, finish=lambda status, reason: finished.append((status, reason)))
        subfleet.start_adopted(cf)
        assert finished and finished[0][0] == 'blocked' and 'pages' in finished[0][1], finished
    finally:
        s.close()


@milestone(4)
def test_child_config_binds_the_root_session_and_page_only_on_fleetmux(request):
    root = W.new_root('st', request, 'child_config')
    redis_cfg = {'redis_socket': '/x/redis.sock'}
    fmf = light_fleet(root, 'fleetmux')
    child = subfleet.child_config(fmf, 'worker-1', 'M0', 1, Path(fmf.run_dir) / 'sub' / 'M0', 'p-1', None,
                                  redis_cfg, 'tree')
    assert child.get('fleetmux_root_session') == RS and child.get('fleetmux_page') == 'M0', child
    assert child['session'] == RS + ':M0'
    # a grandchild: the root session travels down, the page is its own task id
    mid = light_fleet(root / 'mid', 'fleetmux', session=RS + ':M0', task_id='M0', fleetmux_root_session=RS,
                      fleetmux_page='M0')
    grand = subfleet.child_config(mid, 'worker-1', 'M0.1', 1, Path(mid.run_dir) / 'sub' / 'M0.1', 'p-2', None,
                                  redis_cfg, 'tree')
    assert grand.get('fleetmux_root_session') == RS and grand.get('fleetmux_page') == 'M0.1', grand
    # cmux (and wezterm) child configs do not get either key
    for backend in ('cmux', None):
        other = light_fleet(root / str(backend), backend or 'cmux')
        if backend is None:
            other.cfg.pop('backend')
        c = subfleet.child_config(other, 'worker-1', 'M0', 1, Path(other.run_dir) / 'sub' / 'M0', 'p-3', None,
                                  redis_cfg, 'tree')
        assert 'fleetmux_root_session' not in c and 'fleetmux_page' not in c, c


# ================================================================ 2.-3. named sub-fleets (§29) on fleetmux

@milestone(4)
def test_named_subfleet_on_fleetmux_appends_and_restores_supervisor_role(fmsvc):
    t, f = fmsvc, fmsvc.f
    ts.typed_return(t)
    rules = fleet.agent_cwd(f.cfg, 'worker-1') / 'CLAUDE.md'
    before = rules.read_text()
    _, rec, cf = fm_bring_up(t)
    now = rules.read_text()
    assert '## 4. Your role (supervisor)' in now and 'ROLE-MARKER-OPS' in now
    assert named_subfleet.state(f, 'ops')['state'] == 'up'
    fm_take_down(t, cf)
    assert subfleet.record(rec['run'])['state'] == 'down'
    assert rules.read_text() == before


@milestone(4)
def test_named_tmux_view_lands_in_the_child_page(fmsvc):
    t, f = fmsvc, fmsvc.f
    owner = f.agent('worker-1')['pane']
    assert f.mgr.tmux_view_info(owner), 'worker-1 is not a tmux slot in this world'
    pid = pid_of(t, owner)
    _, rec, cf = fm_bring_up(t)
    views = [sid for sid, info in cf.mgr._surfaces().items() if info.get('tmux_role') == 'view']
    assert len(views) == 1, cf.mgr._surfaces()
    assert page_of(t, views[0]) == 'ops' and page_of(t, owner) == ''
    assert pid_of(t, owner) == pid  # the parent's tab of the session never moved (dual display)
    assert cf.agent('supervisor')['pane'] == views[0]
    groups = W.on_page(t.sock, 'ops', 'layout.get')[1]['groups']
    assert groups['supervisor'][:1] == [views[0]], groups
    daemons = [r for r in W.all_panes(t.sock) if r.get('type') == 'daemon' and r.get('page') == 'ops']
    assert len(daemons) == 1  # the child daemon's tab is in the child's page too
    assert W.pages(t.sock)['ops']['state'] == 'live'


@milestone(4)
def test_fx_subfleet_hash_fields_on_fleetmux_match_cmux(fmsvc):
    t, f = fmsvc, fmsvc.f
    _, rec, cf = fm_bring_up(t)
    h = named_subfleet.state(f, 'ops')
    assert FX_FIELDS <= set(h), h
    assert h['state'] == 'up' and h['slot'] == 'worker-1'
    assert h['run_dir'] == str(f.run_dir / 'sub' / 'ops')
    sock = h['redis_socket']
    assert os.path.isabs(sock) and sock == fleet.load(Path(rec['run']) / 'config.json')['redis_socket']
    assert sock != f.cfg['redis_socket']  # the child's own private Redis, not the shared fleetmux server
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(5)
        conn.connect(sock)
        conn.sendall(b'PING\r\n')
        assert conn.recv(64).startswith(b'+PONG')


# ================================================================ 3. / 13. task sub-fleets (§19 / §20)

@milestone(4)
def test_task_subfleet_pane_moves_to_the_child_page_and_back_same_pid(fmtree):
    t, f = fmtree, fmtree.f
    ts.typed_return(t)  # the same session comes back (on the hook channel it is retired instead)
    sid = f.agent('worker-1')['pane']
    pid = pid_of(t, sid)
    rec, _ = spawn_m0(t)
    assert page_of(t, sid) == '' and page_of(t, rec['placeholder']) == ''
    daemons = [r for r in W.all_panes(t.sock) if r.get('type') == 'daemon' and r.get('page') == 'M0']
    assert len(daemons) == 1
    cf = adopt_m0(t, rec)
    assert page_of(t, sid) == 'M0' and pid_of(t, sid) == pid
    groups = W.on_page(t.sock, 'M0', 'layout.get')[1]['groups']
    assert groups['supervisor'][:1] == [sid], groups
    assert sid not in sum(W.ctl(t.sock, 'layout.get')['groups'].values(), [])
    done = finish_child(t, cf, rec)
    assert done['state'] == 'returned', done
    assert page_of(t, sid) == '' and pid_of(t, sid) == pid
    assert W.record(t.sock, rec['placeholder']) == {}
    assert f.mgr.owns(sid)
    assert W.pages(t.sock)['M0']['state'] == 'reclaimed'


@milestone(4)
def test_task_subfleet_m0_on_fleetmux_delegates_and_returns(fmtree, monkeypatch):
    """escalate -> approve (no models) on a plain pane: one same-model handoff into the fleet's tmux, then the
    new session is sub-fleet M0's supervisor shown on both pages (plan fleetmux-worker-area-mapping §2)."""
    t, f = fmtree, fmtree.f
    decision = {'status': 'RUNNING', 'completion': 5, 'reason': 'go', 'dispatch': [
        {'id': 'M0', 'to': 'worker-1', 'instruction': 'Research five sources and write worker-1/m0.md'}]}
    assert ts.hook(t, f, 'supervisor', json.dumps(decision))['status'] == 'committed'
    ts.step(t)
    work = ts.given(t, f, 'worker-1')[-1]
    proposal = [{'id': 'M0.0', 'instruction': 'Fetch sources one to three into m0.0.md', 'output': 'm0.0.md'},
                {'id': 'M0.1', 'instruction': 'Fetch sources four and five into m0.1.md', 'output': 'm0.1.md'}]
    text = ('Done:\n- read the goal\nEvidence:\n- five sources listed in the instruction\nNot done: all of it\n'
            'Blocked: none\n' + json.dumps({'escalate': 'M0 is too big: five sources to fetch and merge',
                                            'proposal': proposal, 'workers': 2}))
    assert ts.hook(t, f, 'worker-1', text, prompt=work)['status'] == 'committed'
    ts.step(t)
    asked = received(t.world, f.agent('supervisor')['pane'])[-1]
    assert 'worker-1 asks to split M0 into 2 pieces' in asked
    approve = {'status': 'RUNNING', 'completion': 10, 'reason': 'split', 'dispatch': [],
               'escalations': [{'task': 'M0', 'decision': 'approve'}]}
    assert ts.hook(t, f, 'supervisor', json.dumps(approve), prompt=asked)['status'] == 'committed'
    monkeypatch.setattr(subfleet, 'default_launch', idle_launch)
    assert f.escalation_state()['M0']['status'] == 'rotating'  # worker-1 runs in a plain pane: handoff first
    f.tick()  # the handoff turn starts
    ts.step(t)
    ask = ts.given(t, f, 'worker-1')[-1]
    assert 'sub-fleet M0' in ask
    assert ts.hook(t, f, 'worker-1', 'handoff written', prompt=ask)['status'] in ('committed', 'handoff')
    ts.step(t)
    assert f.agent('worker-1')['generation'] == '1'
    assert t.world.spawned[-1]['tmux_session'] == 'worker-1-g1'
    owner = f.agent('worker-1')['pane']
    assert f.mgr.tmux_view_info(owner), 'the new generation does not run in the fleet tmux'
    assert ts.hook(t, f, 'worker-1', 'READY', prompt='startup')['status'] == 'ready'
    f.tick()  # READY: the spawn request is written
    f.tick()  # ... and runs
    assert f.agent('worker-1')['state'] == 'delegated'
    rec = subfleet.record(f.run_dir / 'sub' / 'M0')
    assert rec['view'] == 1 and rec['placeholder'] == '' and rec['pane'] == owner
    pid = pid_of(t, owner)
    cf = ts.child_daemon(t, rec)
    for w in cf.workers:
        assert ts.hook(t, cf, w, 'READY', prompt='startup')['status'] == 'ready'
    view = cf.agent('supervisor')['pane']
    assert view != owner and page_of(t, view) == 'M0'
    assert page_of(t, owner) == '' and pid_of(t, owner) == pid  # the parent page keeps its live tab
    groups = W.on_page(t.sock, 'M0', 'layout.get')[1]['groups']
    assert groups['supervisor'][:1] == [view], groups
    typed = [x for p, x in t.world.sent if p == view]
    assert typed and 'Your role changed' in typed[-1]
    assert slot_hook(t, f, 'worker-1', 'READY', typed[-1])['status'] == 'ready'
    ts.step(t)
    goal = cf.task()['goal']
    assert 'Research five sources and write worker-1/m0.md' in goal and '"M0.1"' in goal
    cf.v2.finish('completed', 'merged')
    cf.write_result()
    f.tick()
    ts.stop_child_like_daemon(cf)
    f.tick()
    assert subfleet.record(rec['run'])['state'] == 'returned'
    assert f.escalation_state() == {}
    assert f.agent('worker-1')['generation'] == '2' and t.world.spawned[-1]['tmux_session'] == 'worker-1-g2'
    assert page_of(t, f.agent('worker-1')['pane']) == ''  # back as the worker in the parent page, still in tmux
    assert W.pages(t.sock)['M0']['state'] == 'reclaimed'
    assert W.server_up(t.sock)  # the child never stopped the tree's one server


@milestone(4)
def test_limits_apply_on_fleetmux(fmtree):
    t, f = fmtree, fmtree.f
    assert 'must be M<n>' in subfleet.check(f, 'worker-1', 'X1', 1)
    assert 'child_workers_max 3' in subfleet.check(f, 'worker-1', 'M0', 4)
    yaml = t.root / 'limits.yaml'
    yaml.write_text('workers:\n  - name: worker-1\n  - name: worker-2\nlimits:\n  max_agents_total: 4\n')
    f.cfg['roster'] = str(yaml)
    assert 'max_agents_total 4: the tree has 3 agents' in subfleet.check(f, 'worker-1', 'M0', 2)
    yaml.write_text('workers:\n  - name: worker-1\n  - name: worker-2\nlimits:\n  max_children: 1\n')
    spawn_m0(t)
    assert 'max_children 1' in subfleet.check(f, 'worker-2', 'M1', 1)
    with pytest.raises(subfleet.Refused, match='max_children 1'):
        subfleet.spawn_child(f, 'worker-2', 'M1', 'other', 1, launch=idle_launch)


# ================================================================ 11. the child daemon and the sidebar

@milestone(4)
def test_child_daemon_does_not_touch_the_sidebar_kv(request):
    root = W.new_root('st', request, 'sidebar')
    touched = []

    class KvTrap:
        def kv_get(self, key):
            touched.append(('get', key))

        def _kv_set(self, key, value):
            touched.append(('set', key))
            return True

    run = root / 'fleet-20261104-000000-a1b2c3d4' / 'sub' / 'M0'
    run.mkdir(parents=True)
    f = SimpleNamespace(cfg={'run': str(run), 'workers': ['worker-1'], 'backend': 'fleetmux',
                             'parent_config': str(run.parent.parent / 'config.json'), 'task_id': 'M0'},
                        workers=['worker-1'], stopping=False, service=False, mgr=KvTrap(),
                        agent=lambda name: {'state': 'idle'}, task=lambda: {}, paused=lambda: False)
    fleetmux_sidebar.tick(f, now=100.0)
    fleetmux_sidebar.tick(f, now=200.0)
    assert touched == [], 'a child daemon read or wrote the root-only sidebar keys: %r' % touched


class PagesMgr:
    def __init__(self):
        self.kv = {}

    def server_has_pages(self):
        return True

    def kv_get(self, key):
        return self.kv.get(key)

    def _kv_set(self, key, value):
        self.kv[key] = value
        return True


def sidebar_fleet(run):
    return SimpleNamespace(cfg={'run': str(run), 'workers': ['worker-1', 'worker-2'], 'backend': 'fleetmux',
                                'name': 'tree'},
                           workers=['worker-1', 'worker-2'], stopping=False, service=False, mgr=PagesMgr(),
                           agent=lambda name: {'state': 'idle'}, task=lambda: {}, paused=lambda: False)


@milestone(4)
def test_sidebar_cards_carry_page_ids(request):
    root = W.new_root('st', request, 'sidebar cards')
    # (1) a first attempt sub/M0
    first = root / 'one' / 'fleet-20261104-000000-a1b2c3d4'
    W.sub_run(first, 'M0', 'M0', state='running', attempt=1)
    cards = fleetmux_sidebar.payload(sidebar_fleet(first), history=[])['fleets']
    assert cards[0].get('page') == '' and cards[0]['id'] == first.name, cards
    assert [c.get('page') for c in cards if c['id'] == 'M0'] == ['M0'], cards
    # (2) a retry sub/M0-attempt-2 (running) next to the reclaimed first attempt; (4) a named memx; a grandchild
    run = root / 'two' / 'fleet-20261104-000000-a1b2c3d4'
    W.sub_run(run, 'M0', 'M0', state='returned', attempt=1, outcome='failed')
    retry = W.sub_run(run, 'M0-attempt-2', 'M0', state='running', attempt=2)
    W.sub_run(retry, 'M0.1', 'M0.1', state='running', attempt=1)
    W.sub_run(run, 'memx', 'memx', state='running', attempt=3, named=True, name='memx', mode='service')
    tree_keys = set(subfleet.tree({'run': str(run)}))
    cards = fleetmux_sidebar.payload(sidebar_fleet(run), history=[])['fleets']
    by_id = {c['id']: c for c in cards}
    assert by_id['M0-attempt-2'].get('page') == 'M0', cards
    assert 'page' not in by_id['M0'], cards  # the older attempt's card does not claim the reused page
    assert by_id['memx'].get('page') == 'memx' and by_id['M0.1'].get('page') == 'M0.1', cards
    # (3) every page id is a valid page id, never a tree() display key
    for c in cards:
        if 'page' in c and c['page'] != '':
            assert W.PAGE_ID.match(c['page']) and ' (' not in c['page'], c
    assert len([c for c in cards if c.get('page') == 'M0']) == 1
    # (5) subfleet.tree() keeps its M3 keys
    assert tree_keys == {'M0', 'M0 (M0-attempt-2)', 'memx'}, tree_keys
    assert len(cards) <= fleetmux_sidebar.MAX_FLEETS


# ================================================================ read-only record pages of reclaimed fleets (§5.4)

@milestone(4)
def test_collect_archives_the_child_page_in_the_parent_registry(fmtree):
    t, f = fmtree, fmtree.f
    server = f.mgr._server_pid()
    rec, sid, _ = run_m0_to_return(t)
    rows = archives(f.mgr, 'M0', t.sock)
    assert len(rows) == 1, f.mgr._surfaces()
    assert W.record(t.sock, rows[0]).get('type') == 'history'
    assert W.pages(t.sock)['M0']['state'] == 'reclaimed'
    _, reply = W.on_page(t.sock, 'M0', 'kv.get', key='history_url')
    assert reply.get('value') == (Path(rec['run']) / 'history.html').as_uri(), reply
    assert not (t.root / 'state' / (RS + ':M0.fleetmux.json')).exists()  # the child's registry went as before
    assert f.mgr._server_pid() == server and W.server_up(t.sock)
    assert W.record(t.sock, f.agent('supervisor')['pane'])


@milestone(4)
def test_reup_unarchives_and_reuses_the_page(fmtree):
    t, f = fmtree, fmtree.f
    rec, sid, _ = run_m0_to_return(t, typed=True)
    old = archives(f.mgr, 'M0', t.sock)
    assert len(old) == 1
    back = [x for p, x in t.world.sent if p == sid][-1]
    assert slot_hook(t, f, 'worker-1', 'READY', back)['status'] == 'ready'
    rec2, _ = spawn_m0(t)
    assert Path(rec2['run']).name == 'M0-attempt-2'
    assert W.record(t.sock, old[0]) == {} and old[0] not in f.mgr._surfaces()
    table = W.pages(t.sock)
    assert table['M0']['state'] == 'live' and [p for p in table if p == 'M0'] == ['M0']
    assert page_of(t, rec2['pane']) == ''  # still the parent's tab until the child daemon adopts it
    assert [r for r in W.all_panes(t.sock) if r.get('type') == 'daemon' and r.get('page') == 'M0']


@milestone(4)
def test_resume_children_rebuilds_reclaimed_pages_after_a_server_restart(fmtree):
    t, f = fmtree, fmtree.f
    rec, _, _ = run_m0_to_return(t)
    assert f.mgr.end_server(), f.mgr.last_error  # the server is gone: its page table with it (§4.4 ②)
    assert f.mgr.get_or_create_session(), f.mgr.last_error
    t.servers.register_servers()
    assert 'M0' not in W.pages(t.sock)
    subfleet.resume_children(f, launch=idle_launch)
    row = W.pages(t.sock)['M0']
    assert row['state'] == 'reclaimed', row
    _, reply = W.on_page(t.sock, 'M0', 'kv.get', key='history_url')
    assert reply.get('value') == (Path(rec['run']) / 'history.html').as_uri()
    assert len(archives(f.mgr, 'M0', t.sock)) == 1
    subfleet.resume_children(f, launch=idle_launch)  # idempotent
    assert len(archives(f.mgr, 'M0', t.sock)) == 1


@milestone(4)
def test_grandchild_record_survives_the_middle_fleet_being_reclaimed(fmtree):
    t, f = fmtree, fmtree.f
    f.cfg['limits'] = {'max_depth': 3}
    fleet.save(f.path, dict(fleet.load(f.path), limits={'max_depth': 3}))
    rec, _ = spawn_m0(t)
    cf = adopt_m0(t, rec)
    grec, _ = spawn_m0(t, fl=cf, task_id='M0.1', workers=1)
    gcf = adopt_m0(t, grec, fl=cf)
    assert page_of(t, grec['pane']) == 'M0.1'
    done = finish_child(t, gcf, grec, fl=cf)
    assert done['state'] not in subfleet.ACTIVE
    assert len(archives(cf.mgr, 'M0.1', t.sock)) == 1  # the middle fleet holds its child's record
    done = finish_child(t, cf, rec)
    assert done['state'] not in subfleet.ACTIVE
    rows = archives(f.mgr, 'M0.1', t.sock)
    assert len(rows) == 1, 'the grandchild record died with the middle fleet: %r' % f.mgr._surfaces()
    assert W.record(t.sock, rows[0]).get('type') == 'history'
    assert len(archives(f.mgr, 'M0', t.sock)) == 1
    _, reply = W.on_page(t.sock, 'M0.1', 'kv.get', key='history_url')
    assert reply.get('value') == (Path(grec['run']) / 'history.html').as_uri(), reply
    assert W.pages(t.sock)['M0.1']['state'] == 'reclaimed'


# ================================================================ 12. history jump without any daemon (real binary)

@pytest.fixture(params=['real'])
def real_pg(request, monkeypatch):
    root = W.new_root('jp', request, request.param)
    W.install_fleetmux(monkeypatch, root, request.param)
    (root / 'state').mkdir()
    s = W.Servers(root, request.param, root / 'state')
    yield s
    s.close()


def _frame(kind, payload: bytes) -> bytes:
    return struct.pack('>BI', kind, len(payload)) + payload


@milestone(4)
def test_jump_from_a_reclaimed_child_page_to_the_parent_needs_no_daemon(real_pg):
    s = real_pg
    root_run, root_url = W.make_run(s.root, 'run-root')
    child_run, child_url = W.make_run(s.root, 'run-root/sub/M0')
    mgr = s.root_mgr()
    mgr.claim_run(str(root_run))
    assert mgr.get_or_create_session(), mgr.last_error
    assert mgr._kv_set('history_url', root_url)
    assert W.need(mgr, 'archive_page')('M0', str(child_run), 'M0'), mgr.last_error  # no child daemon anywhere
    assert W.ctl(s.sock, 'page.select', page='M0').get('ok')
    assert W.current_page(s.sock) == 'M0'
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as view:
        view.settimeout(10)
        view.connect(str(s.sock))
        view.sendall((json.dumps({'view': 1, 'cols': 120, 'rows': 40, 'term': 'xterm-256color',
                                  'colorterm': '', 'overlay': 1}) + '\n').encode())
        line = b''
        while not line.endswith(b'\n'):
            chunk = view.recv(1)
            if not chunk:
                break
            line += chunk
        ok = json.loads(line or b'{}')
        assert ok.get('ok') is True and ok.get('pages') == 1, ok
        url = child_url + '?' + 'fleet-jump=k1-abc123&rel=parent&to=' + quote(root_url, safe='')
        view.sendall(_frame(0x04, json.dumps({'event': 'jump', 'url': url}).encode()))
        assert W.wait_for(lambda: W.current_page(s.sock) == '', timeout=5), 'the jump did not select the parent page'
        view.sendall(_frame(0x03, b''))
    assert not any(r.get('type') == 'daemon' for r in W.all_panes(s.sock))

