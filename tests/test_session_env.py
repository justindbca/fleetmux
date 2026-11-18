"""Session environment from an applet (docs/distributed-development.md, section "Session environment from an applet").

Declaration and its checks, the run config key, the environment of every launch path, and fail closed (launch
wait, no model-chain step, the mid-turn guard). Fakes, ``tmp_path``, the repo's private test Redis and loopback
ports this test opens itself only. No real Claude is started.
"""
import json
import os
import socket
import sys
import time
from pathlib import Path

import pytest
import yaml

import test_fleet  # noqa: F401  (puts the checkout on sys.path)
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import fx  # noqa: F401  (fixture: a v2 service fleet)
from test_applet_host import host  # noqa: F401  (fixture: fx with one fake applet)
from test_subfleet import svc  # noqa: F401  (fixture: a service fleet with a named sub-fleet)
from test_stop_failure_retry import fail, handed, head
from test_claude_hook_delivery import hooked  # noqa: F401  (fixture: task fleet, Claude hook-channel workers)
from test_v2_codex_claude import FakeMgr, WORKERS as MIXED_WORKERS
from fleetmux import advisor, brake, fleet, fleet_config, roster, runtime_v2, session_env, subfleet
from fleetmux.channels import reader

PROXY = 'http://127.0.0.1:{}'
WORKERS = 'workers:\n  - {name: worker-1}\n  - {name: worker-2}\n'
OUTSIDE = [{'kind': 'forwarded', 'text': 'outside text'}]


def closed_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def listener():
    """A loopback port that answers (a stand-in for the applet's proxy)."""
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    s.listen(16)
    yield s.getsockname()[1]
    s.close()


def route(port, clis=('claude',), **extra):
    return {'applet': 'applet-tunnel-1', 'port': port, 'clis': list(clis),
            'vars': dict({'HTTPS_PROXY': PROXY.format(port), 'HTTP_PROXY': PROXY.format(port)}, **extra)}


def applet_row(name='applet-tunnel-1', port=17890, clis=('claude',), **env):
    return {'name': name, 'command': ['x'], 'session_env': {
        'port': port, 'vars': dict({'HTTPS_PROXY': PROXY.format(port)}, **env), 'clis': list(clis)}}


def roster_text(*rows):
    return WORKERS + 'applets:\n' + ''.join('  - ' + json.dumps(r) + '\n' for r in rows)


# ====================================================================== declaration
def test_shipped_block_verifies_only_claude():
    shipped = yaml.safe_load(fleet_config.SHIPPED.read_text())
    assert shipped['session_env']['verified_clis'] == ['claude']
    assert 'session_env' in fleet_config.BLOCKS and 'session_env' not in fleet_config.USER_BLOCKS
    conf = session_env.settings()
    assert conf['wait_interval_s'] > 0 and conf['probe_timeout_s'] > 0


def test_roster_accepts_a_claude_route_and_keeps_it(tmp_path):
    text = roster_text(applet_row())
    assert roster.parse_entries(text, tmp_path)[0] == ['worker-1', 'worker-2']
    [entry] = roster.applets(text, tmp_path)
    assert entry['session_env']['clis'] == ['claude']


def test_roster_with_an_unverified_cli_is_refused_whole(tmp_path):
    with pytest.raises(ValueError, match='pi is not in fleet.yaml#session_env.verified_clis'):
        roster.parse_entries(roster_text(applet_row(clis=['claude', 'pi'])), tmp_path)


@pytest.mark.parametrize('value, reason', [
    ({'port': 17890, 'vars': {'HTTPS_PROXY': 'x'}}, 'exactly the keys'),
    ({'port': 17890, 'vars': {'HTTPS_PROXY': 'x'}, 'clis': ['claude'], 'other': 1}, 'exactly the keys'),
    ({'port': 0, 'vars': {'HTTPS_PROXY': 'x'}, 'clis': ['claude']}, 'port number'),
    ({'port': True, 'vars': {'HTTPS_PROXY': 'x'}, 'clis': ['claude']}, 'port number'),
    ({'port': 17890, 'vars': {}, 'clis': ['claude']}, 'non-empty mapping'),
    ({'port': 17890, 'vars': {'BAD-NAME': 'x'}, 'clis': ['claude']}, 'bad variable name'),
    ({'port': 17890, 'vars': {'HTTPS_PROXY': 'a\nb'}, 'clis': ['claude']}, 'one-line string'),
    ({'port': 17890, 'vars': {'HTTPS_PROXY': 'x'}, 'clis': []}, 'non-empty list'),
])
def test_a_bad_declaration_is_refused(tmp_path, value, reason):
    row = {'name': 'applet-tunnel-1', 'command': ['x'], 'session_env': value}
    with pytest.raises(ValueError, match=reason):
        roster.parse_entries(roster_text(row), tmp_path)


def test_two_applets_routing_one_cli_are_refused(tmp_path):
    rows = (applet_row(), applet_row(name='applet-tunnel-2', port=17891))
    with pytest.raises(ValueError, match='one route per CLI'):
        roster.parse_entries(roster_text(*rows), tmp_path)


# ====================================================================== carry to every fleet in the tree
def test_root_writes_the_union_of_its_own_and_its_sub_fleets_applets(tmp_path):
    child = tmp_path / 'child.yaml'
    child.write_text(roster_text(applet_row(name='applet-home-1', port=17891)))
    cfg = {}
    own = roster.applets(roster_text(applet_row()), tmp_path)
    assert session_env.update_config(cfg, own, [{'name': 'ops', 'roster': str(child)}]) is True
    assert [(d['applet'], d['port']) for d in cfg['session_env']] == [('applet-tunnel-1', 17890),
                                                                      ('applet-home-1', 17891)]
    assert session_env.update_config(cfg, own, [{'name': 'ops', 'roster': str(child)}]) is False  # unchanged
    stored = session_env.carry({'workers': []}, cfg)
    assert stored['session_env'] == cfg['session_env']
    assert session_env.update_config(cfg, [], []) is True and 'session_env' not in cfg  # no applet: no key
    assert 'session_env' not in session_env.carry(stored, cfg)


def test_a_child_keeps_the_key_its_config_got_at_up():
    cfg = {'parent_config': '/x/config.json', 'session_env': [route(17890)]}
    assert session_env.update_config(cfg, [], []) is False and cfg['session_env'] == [route(17890)]


def test_reload_roster_persists_the_key_into_the_run_config(fx):  # noqa: F811
    f = fx
    path = Path(f.cfg['run']) / 'roster.yaml'
    path.write_text(roster_text(applet_row()))
    f.reload_roster(path)
    assert f.r.hget(fleet.ROSTER, 'rejected') == ''
    assert [d['port'] for d in fleet.load(f.path)['session_env']] == [17890]


def test_a_sub_fleet_config_carries_the_key_at_up(svc):  # noqa: F811
    t, f = svc, svc.f
    f.cfg['session_env'] = [route(17890)]
    [entry] = __import__('fleetmux.named_subfleet', fromlist=['x']).declared(f).values()
    cfg = subfleet.child_config(f, 'worker-1', 'ops', 1, t.tmp / 'c', 'pane-x', None, {}, 'n', named=entry)
    assert cfg['session_env'] == [route(17890)]


# ====================================================================== rules 1, 2, 4, 5
def test_rules_clear_inherited_proxies_and_always_exempt_loopback(monkeypatch):
    cfg = {'session_env': [route(17890, NO_PROXY='internal.example')]}
    base = {'PATH': '/bin', 'ALL_PROXY': 'socks5://x', 'all_proxy': 'x', 'HTTP_PROXY': 'x', 'http_proxy': 'x',
            'https_proxy': 'x', 'no_proxy': 'x', 'NO_PROXY': 'x'}
    env = session_env.environ(cfg, 'claude', base)
    assert env['HTTPS_PROXY'] == env['HTTP_PROXY'] == 'http://127.0.0.1:17890'
    for name in ('ALL_PROXY', 'all_proxy', 'http_proxy', 'https_proxy'):
        assert name not in env
    assert env['NO_PROXY'] == env['no_proxy'] == 'internal.example,127.0.0.1,localhost,::1'
    assert base['HTTP_PROXY'] == 'x' and env['PATH'] == '/bin'  # the caller's dict is never changed (rule 5)


@pytest.mark.parametrize('cli', ['pi', 'opencode', 'dsh', 'codex'])
def test_unverified_clis_are_never_routed_even_if_listed(cli):
    cfg = {'session_env': [route(17890, clis=(cli, 'claude'))]}
    base = {'HTTPS_PROXY': 'inherited'}
    assert session_env.routes(cfg, cli) == []
    assert session_env.environ(cfg, cli, base) is base  # inherited value untouched
    assert session_env.settings_env(cfg, cli, {'A': '1'}) == {'A': '1'}
    assert session_env.run_kwargs(cfg, cli) == {}


def test_no_key_no_change():
    assert session_env.routes({}, 'claude') == [] and session_env.run_kwargs({}, 'claude') == {}


# ====================================================================== launch paths
def exec_slot(tmp_path, monkeypatch, slot, port, *, cli='claude', supervisor='claude', state=None, **extra):
    """fleet.actor for ``slot`` up to execvp with the route declared; -> (environment, argv, hash writes)."""
    cfg = dict(run=str(tmp_path / 'run'), workers=['worker-1'], supervisor_cli=supervisor, worker_cli=cli,
               claude='/nonexistent/claude', codex='/nonexistent/codex', python=sys.executable,
               transport_version=2, allow_development_v2=True, claude_supervisor_hook_delivery=True, mode='task',
               **dict({'session_env': [route(port, clis=('claude', cli))]}, **extra))
    path = tmp_path / 'config.json'
    fleet.save(path, cfg)
    fleet.agent_cwd(cfg, slot).mkdir(parents=True, exist_ok=True)
    h = state if state is not None else {}
    h.setdefault('generation', '0')
    h.setdefault('state', 'starting')
    h.setdefault('launch_ready', '1')
    h.setdefault('label', slot)
    writes = []

    class R:
        def hgetall(self, key):
            return dict(h)

        def hset(self, key, mapping):
            writes.append(dict(mapping))
            h.update({k: str(v) for k, v in mapping.items()})
    monkeypatch.setattr(fleet, 'connect', lambda c: R())
    monkeypatch.setattr(fleet.os, 'chdir', lambda p: None)
    monkeypatch.setattr(fleet, 'trust_claude_dir', lambda cwd: None)
    env = {k: v for k, v in os.environ.items() if k.upper() not in session_env.PROXY_VARS}
    env.update(ALL_PROXY='socks5://inherited', https_proxy='http://inherited', NO_PROXY='inherited')
    monkeypatch.setattr(fleet.os, 'environ', env)
    seen = []

    def execvp(prog, argv):
        seen.append((dict(fleet.os.environ), argv))
        raise SystemExit(0)
    monkeypatch.setattr(fleet.os, 'execvp', execvp)
    with pytest.raises(SystemExit):
        fleet.actor(path, slot, 'prompt', 0)
    return seen[0][0], seen[0][1], writes


def settings_env(argv):
    return json.loads(Path(argv[argv.index('--settings') + 1]).read_text()).get('env') or {}


@pytest.mark.parametrize('slot', ['supervisor', 'worker-1'])
def test_actor_execs_claude_with_the_route_and_nothing_inherited(tmp_path, monkeypatch, listener, slot):
    env, argv, writes = exec_slot(tmp_path, monkeypatch, slot, listener)
    assert env['HTTPS_PROXY'] == PROXY.format(listener)
    assert env['NO_PROXY'] == env['no_proxy'] == '127.0.0.1,localhost,::1'
    assert 'ALL_PROXY' not in env and 'https_proxy' not in env
    assert env['DISABLE_AUTOUPDATER'] == '1'  # the per-CLI update still applies
    assert writes == []  # port open: no hold


@pytest.mark.parametrize('slot, extra', [('worker-1', {}), (fleet.ADVISOR, {})])
def test_settings_file_env_carries_the_route_in_both_branches(tmp_path, monkeypatch, listener, slot, extra):
    """Normal branch (worker) and resident advisor branch of claude_argv."""
    _env, argv, _ = exec_slot(tmp_path, monkeypatch, slot, listener, **extra)
    s = settings_env(argv)
    assert s['HTTPS_PROXY'] == PROXY.format(listener) and s['no_proxy'] == '127.0.0.1,localhost,::1'
    assert s['DISABLE_AUTOUPDATER'] == '1'


def test_codex_pane_is_not_routed_while_unverified(tmp_path, monkeypatch):
    port = closed_port()  # a routed slot would wait here for ever
    env, argv, writes = exec_slot(tmp_path, monkeypatch, 'supervisor', port, supervisor='codex',
                                  session_env=[route(port, clis=('codex', 'claude'))])
    assert argv[0] == '/nonexistent/codex'
    assert env['ALL_PROXY'] == 'socks5://inherited' and 'HTTPS_PROXY' not in env  # untouched
    assert writes == []


def test_advisor_one_shot_passes_the_route_in_its_env(tmp_path, monkeypatch, listener):
    seen = {}

    def run(argv, **kw):
        seen.update(kw)
        return type('D', (), {'returncode': 0, 'stdout': '{}', 'stderr': ''})()
    monkeypatch.setattr(advisor.subprocess, 'run', run)
    monkeypatch.setenv('HTTP_PROXY', 'http://inherited')
    advisor.run_claude({}, cfg={'session_env': [route(listener)]}, run_dir=tmp_path)
    assert seen['env']['HTTPS_PROXY'] == PROXY.format(listener) and seen['env']['DISABLE_AUTOUPDATER'] == '1'
    assert seen['env']['HTTP_PROXY'] == PROXY.format(listener) and 'localhost' in seen['env']['NO_PROXY']
    assert os.environ['HTTP_PROXY'] == 'http://inherited'  # the daemon's own environment is unchanged
    advisor.run_claude({}, cfg={}, run_dir=tmp_path)
    assert seen['env']['HTTP_PROXY'] == 'http://inherited'  # no route: as before


def test_channel_reader_one_shot_carries_the_route(tmp_path, monkeypatch, listener):
    seen = {}

    def run(argv, **kw):
        seen.update(kw)
        return type('D', (), {'returncode': 0, 'stdout': ''})()
    monkeypatch.setattr(reader.subprocess, 'run', run)
    policy = {'reader': {'mode': 'on'}}
    cfg = {'run': str(tmp_path), 'session_env': [route(listener)]}
    reader.summarize(OUTSIDE, [], policy=policy, run=reader.run_cli, cfg=cfg)
    assert seen['env']['HTTPS_PROXY'] == PROXY.format(listener) and 'localhost' in seen['env']['no_proxy']
    seen.clear()
    reader.summarize(OUTSIDE, [], policy=policy, run=reader.run_cli, cfg={'run': str(tmp_path)})
    assert seen.get('env') is None  # no route: inherits as before


@pytest.fixture
def routed(private_redis_v2, tmp_path):  # noqa: F811
    """A v2 task fleet (Codex supervisor, Claude workers) whose run config declares a Claude route."""
    r = private_redis_v2
    r.flushdb()
    run = tmp_path / 'run'
    (run / 'workspace').mkdir(parents=True)
    claude_bin = tmp_path / 'bin' / 'claude'
    claude_bin.parent.mkdir()
    claude_bin.write_text('#!/bin/sh\nexit 1\n')
    claude_bin.chmod(0o755)
    cfg = dict(run=str(run), redis_socket=r.connection_pool.connection_kwargs['path'], python=sys.executable,
               workers=list(MIXED_WORKERS), mode='task', supervisor_cli='codex', worker_cli='claude',
               claude=str(claude_bin), codex_home=str(tmp_path / 'codex-home'), transport_version=2,
               allow_development_v2=True, stall_timeout=0, v2_lease_ms=300000, max_busy=0,
               session_env=[route(17890)])
    path = run / 'config.json'
    fleet.save(path, cfg)
    f = fleet.Fleet(path, r, FakeMgr(), sessions=tmp_path / 'codex-home' / 'sessions')
    real = fleet.trust_claude_dir
    fleet.trust_claude_dir = lambda cwd: None
    try:
        f.start_all()
        yield f
    finally:
        fleet.trust_claude_dir = real
        f.v2.close()


def test_pane_spawn_puts_no_proxy_variable_on_the_command_line(routed):
    assert routed.mgr.spawned
    for s in routed.mgr.spawned:
        assert not {k.upper() for k in s['env_vars']} & set(session_env.PROXY_VARS), s['env_vars']


# ====================================================================== fail closed
def test_launch_waits_on_a_closed_port_then_execs_once_it_answers(tmp_path, monkeypatch):
    port = closed_port()
    calls = []

    def probe(p, timeout):
        calls.append(p)
        return len(calls) > 3  # the port answers from the 4th probe on
    monkeypatch.setattr(session_env, 'port_open', probe)
    monkeypatch.setattr(session_env.time, 'sleep', lambda s: None)
    h = {'startup_deadline': '1'}
    env, argv, writes = exec_slot(tmp_path, monkeypatch, 'worker-1', port, state=h)
    assert writes[0] == {'route_wait': 1}
    assert writes[-1]['route_wait'] == 0 and float(writes[-1]['startup_deadline']) > time.time() + 100
    assert argv[0] == '/nonexistent/claude' and env['HTTPS_PROXY'] == PROXY.format(port)


def test_launch_wait_ends_when_the_generation_is_retired(tmp_path, monkeypatch):
    port = closed_port()
    h = {}
    monkeypatch.setattr(session_env.time, 'sleep', lambda s: h.update(generation='1'))
    with pytest.raises(RuntimeError, match='retired'):
        exec_slot(tmp_path, monkeypatch, 'worker-1', port, state=h)


def test_a_held_launch_is_no_startup_timeout(routed):
    f = routed
    f.r.set(f.v2.prefix + ':boot', 'running')
    f.setf('worker-1', state='starting', route_wait=1, startup_deadline=time.time() - 5)
    f.v2.tick()
    assert f.agent('worker-1')['state'] == 'starting'  # not dead: no startup timeout, no new generation
    f.setf('worker-1', route_wait=0)
    f.v2.tick()
    assert f.agent('worker-1')['state'] == 'dead'  # the same deadline without the hold: as before


def test_a_refused_proxy_is_no_wall():
    text = ('API Error: Connection error. (connect ECONNREFUSED 127.0.0.1:17890) '
            'Unable to connect to API due to poor internet connection')
    assert brake.classify_wall(text) is None
    assert runtime_v2.OFFLINE_ERROR.search(text)


def test_a_refused_proxy_takes_no_model_chain_step(routed):
    f = routed
    assert f.on_wall('worker-1', 'API Error: Connection error. connect ECONNREFUSED 127.0.0.1:17890', 'turn') is None
    assert not f.agent('worker-1').get('wall_downgrade') and not f.agent('worker-1').get('wall')


def test_route_closed_holds_and_releases_a_running_slot(routed, listener):
    f = routed
    f.cfg['session_env'] = [route(closed_port())]
    f.setf('worker-1', state='busy')
    assert session_env.route_closed(f, 'worker-1') is True and f.agent('worker-1')['route_wait'] == '1'
    f.cfg['session_env'] = [route(listener)]
    f.__dict__.pop('route_probe', None)
    assert session_env.route_closed(f, 'worker-1') is False and f.agent('worker-1')['route_wait'] == '0'
    f.setf('supervisor', route_wait=1)
    assert session_env.route_closed(f, 'supervisor') is False  # Codex supervisor: not routed, never held
    assert f.agent('supervisor')['route_wait'] == '1' and not session_env.held(f, 'supervisor', f.agent('supervisor'))


def test_stop_failure_backoff_does_not_release_while_the_port_is_closed(routed, listener):
    f = routed
    f.cfg['session_env'] = [route(closed_port())]
    now = time.time()
    f.setf('worker-1', state='held', stopfail_at=now - 100, stopfail_g=f.agent('worker-1')['generation'],
           stopfail_retry_at=now - 1)
    f.v2.stopfail_tick('worker-1', f.agent('worker-1'), now)
    assert f.agent('worker-1')['state'] == 'held' and f.agent('worker-1')['route_wait'] == '1'
    f.cfg['session_env'] = [route(listener)]
    f.__dict__.pop('route_probe', None)
    f.v2.stopfail_tick('worker-1', f.agent('worker-1'), now)
    assert f.agent('worker-1')['state'] == 'idle' and f.agent('worker-1')['route_wait'] == '0'


def test_watchdog_never_escapes_or_rotates_a_slot_whose_port_is_closed(routed, monkeypatch):
    f = routed
    f.cfg['session_env'] = [route(closed_port())]
    wd = f.v2.watchdog
    rotated, keys = [], []
    monkeypatch.setattr(f.mgr, 'send_key', lambda pane, k: keys.append(k) or True, raising=False)
    info = dict(f.agent('worker-1'), pane=f.agent('worker-1').get('pane') or 'pane-1')
    assert wd.rotate('worker-1', 'frozen', '') is False
    f.setf('worker-1', watch_state='stalled', watch_since=1, stall_escapes=2)
    monkeypatch.setattr(wd, 'capture', lambda *a: 'screen')
    monkeypatch.setattr(wd, 'rotate', lambda *a, **k: rotated.append(a))
    wd.check('worker-1', info, {'current_delivery': 'd-1', 'status': 'received'}, True, wd.cache.get(wd.audit),
             time.time())
    assert rotated == [] and keys == []
    got = f.agent('worker-1')
    assert got['route_wait'] == '1' and got['watch_state'] == 'idle' and got['stall_escapes'] == '0'


def test_a_stop_failure_while_the_port_is_closed_spends_no_attempt_and_waits(hooked, listener):
    f = hooked
    got, env = handed(f)
    f.cfg['session_env'] = [route(closed_port())]  # the port closes during the turn
    fail(f, error='unknown', text='API Error: Connection error. connect ECONNREFUSED 127.0.0.1:17890')
    again = head(f)
    assert str(again.get('replay')) == '1', 'a lost route is no failed attempt'
    assert f.agent('worker-1')['state'] == 'held' and f.agent('worker-1')['route_wait'] == '1'
    f.setf('worker-1', stopfail_retry_at=time.time() - 1)
    f.v2.tick()
    f.pump()
    assert f.agent('worker-1')['state'] == 'held', 'nothing is re-delivered while the port is closed'
    assert not f.v2.bus.slot_state('worker-1').get('current_delivery')
    f.cfg['session_env'] = [route(listener)]
    f.__dict__.pop('route_probe', None)
    f.v2.tick()
    f.pump()
    assert f.agent('worker-1')['route_wait'] == '0'
    assert f.v2.bus.slot_state('worker-1').get('message_id') == again['message_id']


def test_a_supervisor_held_for_the_route_lets_the_applet_start_on_the_daemon_tick(host, monkeypatch):  # noqa: F811
    """applet-1: applets start on the daemon tick, not after the supervisor's READY, so the supervisor may wait
    for the applet's own port. Pi stands in for a routed supervisor CLI here (verified only in this test)."""
    f = host
    monkeypatch.setattr(session_env, 'settings', lambda: dict(session_env.SETTINGS, verified_clis=['claude', 'pi']))
    f.cfg['session_env'] = [route(closed_port(), clis=('pi',))]
    f.setf('supervisor', state='starting', route_wait=1, startup_deadline=time.time() - 5)
    f.tick()
    assert f.test_procs, 'the applet starts while the supervisor waits for its port'
    assert f.agent('supervisor')['state'] == 'starting'  # no startup timeout, no new generation
    f.setf('supervisor', route_wait=0)
    f.tick()
    assert f.agent('supervisor')['state'] == 'dead'  # the same deadline without the hold: as before
