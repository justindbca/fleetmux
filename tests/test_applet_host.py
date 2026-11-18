"""Applet processes and hot load (docs/applets.md section 2, docs/applet-interface.md sections 1 and 2).

Fakes and temporary resources only: the repo's private test Redis, ``tmp_path`` for the run, the fleet home and
``HOME``. The acceptance tests start the fixture applet ``tests/fixtures/applet_minimal`` as a real process; every
other test uses fake process handles and records signals instead of sending them.
"""
import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture: the repo's private test Redis)
from test_runtime_v2 import fx  # noqa: F401  (a v2 service fleet: pi supervisor + 2 pi workers)
from fleetmux import applet_host, fleet, roster

FIXTURE = Path(__file__).resolve().parent / 'fixtures' / 'applet_minimal'
WORKERS = 'workers:\n  - {name: worker-1}\n  - {name: worker-2}\n'


def applet_rows(**extra):
    row = {'name': 'applet-minimal-1', 'command': ['{python}', 'minimal_applet.py'], 'cwd': str(FIXTURE),
           'heartbeat_s': 0.5, 'stale_after_s': 10, 'stop_grace_s': 5, 'durable_state': False}
    row.update(extra)
    return 'applets:\n  - ' + json.dumps(row) + '\n'


def write_roster(f, text):
    path = Path(f.cfg['run']) / 'roster.yaml'
    path.write_text(text)
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))  # a new stamp even within one second
    return path


def parse(text, base):
    return roster.applet_entries(__import__('yaml').safe_load(text).get('applets'), base, ['worker-1', 'worker-2'])


def audits(f, name):
    return [x for _, x in f.r.xrange(f.v2.bus.audit_key) if x.get('event') == name]


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_for(check, timeout=15.0, tick=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if tick:
            tick()
        if check():
            return True
        time.sleep(.1)
    return False


def snapshot(*roots, skip=()):
    out = {}
    for root in roots:
        for path in Path(root).rglob('*'):
            if any(path == s or s in path.parents for s in skip):
                continue
            try:
                st = path.lstat()
            except OSError:
                continue
            out[str(path)] = (st.st_size, st.st_mtime_ns)
    return out


# ====================================================================== roster: applets: block
def test_roster_accepts_applets_and_fills_every_default(tmp_path):
    params = tmp_path / 'app.yaml'
    params.write_text('applet: {}\n')
    text = WORKERS + ('applets:\n  - {name: applet-rollout-1, command: ["{python}", "-m", "x"], '
                      'params: {file: app.yaml, block: applet}, session_env: {port: 17890, '
                      'vars: {HTTPS_PROXY: "http://127.0.0.1:17890"}, clis: [claude]}}\n')
    assert roster.parse_entries(text, tmp_path)[0] == ['worker-1', 'worker-2']
    [entry] = roster.applets(text, tmp_path)
    assert entry == {
        'name': 'applet-rollout-1', 'command': ['{python}', '-m', 'x'], 'cwd': str(tmp_path.resolve()), 'env': {},
        'params': {'file': str(params.resolve()), 'block': 'applet'}, 'autostart': True, 'interface': 1,
        'heartbeat_s': 10, 'stale_after_s': 60, 'stop_grace_s': 30, 'detach_wait_s': 60, 'durable_state': True,
        'notify': {'max_per_hour': 6}, 'session_env': {
            'port': 17890, 'vars': {'HTTPS_PROXY': 'http://127.0.0.1:17890'}, 'clis': ['claude']},
        'lease': {'max_active': 2, 'ttl_s': 1800, 'ttl_max_s': 7200, 'max_renewals': 3, 'renew_margin_s': 300,
                  'renew_grace_s': 0, 'prepare_timeout_s': 300, 'eligible': ['worker-1', 'worker-2'],
                  'decision_wait_s': 600, 'hot_add_max': 2, 'hot_add_like': 'worker-1'},
        'review': {'max_rounds': 3, 'timeout_s': 1800},
        'waits': {'request_lease': 660, 'renew': 660, 'send': 9360, 'release': 60, 'notify': 60}}
    path = tmp_path / 'roster.yaml'
    path.write_text(text)
    assert roster.load_applets(path) == [entry]


def test_roster_without_applets_has_none(tmp_path):
    assert roster.applets(WORKERS, tmp_path) == []
    assert 'session_env' not in parse('applets: [{name: applet-a-1, command: [x]}]', tmp_path)[0][0]


@pytest.mark.parametrize('row, reason', [
    ('{name: rollout-1, command: [x]}', 'name must match'),
    ('{name: applet-a-1, command: x}', 'command must be a non-empty list'),
    ('{name: applet-a-1, command: []}', 'command must be a non-empty list'),
    ('{name: applet-a-1, command: [x], cwd: missing}', 'cwd directory not found'),
    ('{name: applet-a-1, command: [x], env: {FLEETMUX_X: "1"}}', 'not starting with FLEETMUX_'),
    ('{name: applet-a-1, command: [x], params: {file: none.yaml, block: b}}', 'params file not found'),
    ('{name: applet-a-1, command: [x], params: {file: none.yaml}}', 'params must be'),
    ('{name: applet-a-1, command: [x], heartbeat_s: 10, stale_after_s: 10}', 'stale_after_s must be greater'),
    ('{name: applet-a-1, command: [x], stop_grace_s: 0}', 'stop_grace_s must be a number > 0'),
    ('{name: applet-a-1, command: [x], autostart: "yes"}', 'autostart must be true or false'),
    ('{name: applet-a-1, command: [x], durable_state: 0}', 'durable_state must be true or false'),
    ('{name: applet-a-1, command: [x], interface: 0}', 'interface must be an integer'),
    ('{name: applet-a-1, command: [x], notify: {max_per_hour: -1}}', 'notify must be'),
    ('{name: applet-a-1, command: [x], lease: {max_active: 0}}', 'lease.max_active'),
    ('{name: applet-a-1, command: [x], lease: {hot_add_max: -1}}', 'lease.hot_add_max'),
    ('{name: applet-a-1, command: [x], lease: {hot_add_like: nobody-1}}', 'lease.hot_add_like'),
    ('{name: applet-a-1, command: [x], review: {max_rounds: 0}}', 'review.max_rounds'),
    ('{name: applet-a-1, command: [x], review: {timeout_s: 5}}', 'review.timeout_s'),
    ('{name: applet-a-1, command: [x], review: {other: 1}}', 'review must be a mapping'),
    ('{name: applet-a-1, command: [x], lease: {ttl_s: 30}}', 'lease.ttl_s'),
    ('{name: applet-a-1, command: [x], lease: {ttl_max_s: 59}}', 'lease.ttl_max_s'),
    ('{name: applet-a-1, command: [x], lease: {renew_margin_s: 1800}}', 'lease.renew_margin_s'),
    ('{name: applet-a-1, command: [x], lease: {eligible: [nobody-1]}}', 'lease.eligible'),
    ('{name: applet-a-1, command: [x], lease: {decision_wait_s: 5}}', 'lease.decision_wait_s'),
    ('{name: applet-a-1, command: [x], lease: {max_renewals: -1}}', 'lease.max_renewals'),
    ('{name: applet-a-1, command: [x], lease: {other: 1}}', 'lease must be a mapping'),
    ('{name: applet-a-1, command: [x], color: red}', 'must be a mapping with "name"'),
])
def test_roster_rejects_a_bad_applet_row_whole(tmp_path, row, reason):
    with pytest.raises(ValueError, match=reason):
        roster.parse_entries(WORKERS + 'applets:\n  - ' + row + '\n', tmp_path)


def test_roster_rejects_duplicate_and_taken_names(tmp_path):
    entries, errors = parse('applets: [{name: applet-a-1, command: [x]}, {name: applet-a-1, command: [x]}]',
                            tmp_path)
    assert [e['name'] for e in entries] == ['applet-a-1'] and 'duplicate applet name' in errors[0]
    assert roster.applet_entries([{'name': 'applet-a-1', 'command': ['x']}], tmp_path, ['worker-1'],
                                 ['applet-a-1'])[1]
    assert parse('applets: [{name: applet-' + 'a' * 44 + '-1, command: [x]}]', tmp_path)[1]


# ====================================================================== acceptance: the fixture applet
def test_fixture_applet_hot_loads_unloads_and_stops_with_the_fleet(fx, tmp_path, monkeypatch):
    """Field workflow Rules 1: loaded while running, its child runs, unload and fleet stop leave no process,
    and nothing outside the fleet is created or changed."""
    f = fx
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    run, fleet_home = Path(f.cfg['run']), fleet.HOME
    before = snapshot(tmp_path, FIXTURE, skip=(run, fleet_home))
    write_roster(f, WORKERS)
    f.tick()
    assert applet_host.states(f.r) == {}

    # 1. hot load: the row appears in the roster of the running fleet
    write_roster(f, WORKERS + applet_rows())
    key = applet_host.key('applet-minimal-1')
    assert wait_for(lambda: f.r.hget(key, 'state') == 'up', tick=f.tick), f.r.hgetall(key)
    rd = run / 'applets' / 'applet-minimal-1'
    assert wait_for(lambda: (rd / 'child.pid').exists())
    applet_pid, child = int(f.r.hget(key, 'pid')), int((rd / 'child.pid').read_text())
    # 2. the process it started runs
    assert alive(applet_pid) and alive(child)
    assert f.r.hget(key, 'phase') == 'serving' and f.r.hget(key, 'interface') == '1'
    assert [a['applet'] for a in audits(f, 'applet.declared')] == ['applet-minimal-1']
    assert len(audits(f, 'applet.up')) == 1

    # 3. unload: removing the row stops the applet and its child
    write_roster(f, WORKERS)
    assert wait_for(lambda: not f.r.exists(key) and not getattr(f, 'applet_runners', {}),  # detached: keys cleared
                    tick=f.tick)
    assert wait_for(lambda: not alive(applet_pid) and not alive(child))
    assert audits(f, 'applet.detached')[-1]['reason'] == 'detach'

    # 4. load again, then stop the fleet through its explicit stop path
    write_roster(f, WORKERS + applet_rows())
    assert wait_for(lambda: f.r.hget(key, 'state') == 'up' and (rd / 'child.pid').exists()
                    and int((rd / 'child.pid').read_text()) != child, tick=f.tick)
    applet_pid, child = int(f.r.hget(key, 'pid')), int((rd / 'child.pid').read_text())
    assert alive(applet_pid) and alive(child)
    events = []
    real_stop = applet_host.stop_all
    monkeypatch.setattr(applet_host, 'stop_all', lambda fl, reason='stop': events.append(
        [json.loads(x['data'])['type'] for _, x in fl.r.xrange(key + ':events')]) or real_stop(fl, reason))
    f.stopping = f.explicit_stop = True
    f.v2.close()
    f.v2.reader = f.v2.bus.blocking_client(1000)
    f.run()
    assert events == [[]]  # stop_all ran on the stop path (the detach in step 3 cleared the old stream)
    reasons = [json.loads(x['data'])['reason'] for _, x in f.r.xrange(key + ':events')]
    assert reasons == ['stop']  # stop_all sends its own host.draining
    # 5. after the fleet stop no process of the applet is left
    assert wait_for(lambda: not alive(applet_pid) and not alive(child))
    # 6. nothing outside the fleet (its run directory and fleet home) was created or changed
    assert snapshot(tmp_path, FIXTURE, skip=(run, fleet_home)) == before
    assert list(home.iterdir()) == []


def test_durable_state_lives_under_the_fleet_home_and_workflow_state_inside_the_run(fx):
    f = fx
    base = {'name': 'applet-a-1', 'command': ['{python}', '-c', 'pass', '{state}', '{run}'], 'cwd': '/',
            'lease': {'eligible': ['worker-1']}}
    durable = dict(base, durable_state=True)
    assert applet_host.state_dir(f.cfg, durable) == fleet.HOME / 'applets' / 'applet-a-1'
    workflow = dict(base, durable_state=False)
    run = Path(f.cfg['run'])
    assert applet_host.state_dir(f.cfg, workflow) == run / 'applets' / 'applet-a-1' / 'state'
    assert applet_host.argv(f.cfg, workflow) == [f.cfg['python'], '-c', 'pass',
                                               str(run / 'applets' / 'applet-a-1' / 'state'),
                                               str(run / 'applets' / 'applet-a-1')]
    env = applet_host.environment(f.cfg, dict(workflow, env={'LOG_LEVEL': 'info'},
                                              params={'file': '/p.yaml', 'block': 'b'}), 'tok',
                                  base={'PYTHONPATH': '/x', 'FLEETMUX_APPLET_TOKEN': 'old', 'KEEP': '1'})
    assert env['FLEETMUX_APPLET_TOKEN'] == 'tok' and env['KEEP'] == '1' and env['LOG_LEVEL'] == 'info'
    assert env['FLEETMUX_APPLET_CONFIG'] == str(run / 'config.json')
    assert env['FLEETMUX_APPLET_REDIS'] == f.cfg['redis_socket']
    assert env['FLEETMUX_APPLET_WORKERS'] == 'worker-1' and env['FLEETMUX_APPLET_INTERFACE'] == '1'
    assert env['FLEETMUX_APPLET_PARAMS_FILE'] == '/p.yaml' and env['FLEETMUX_APPLET_PARAMS_BLOCK'] == 'b'
    assert env['PYTHONPATH'] == applet_host.sdk_dir() + os.pathsep + '/x'
    assert (Path(env['FLEETMUX_APPLET_SDK']) / 'fleetmux' / 'applet' / '__init__.py').is_file()
    assert env['FLEETMUX_APPLET_STOP_GRACE_S'] == '30'  # the SDK bounds its lease release on stop by it
    assert env['FLEETMUX_APPLET_PYTHON'] == f.cfg['python']  # the SDK runs `fleetmux request` with it (2.3)
    assert env['FLEETMUX_HOME'] == str(fleet.HOME)


# ====================================================================== process rules with fake processes
class FakeProc:
    _pid = [9_000_000]

    def __init__(self):
        FakeProc._pid[0] += 1
        self.pid = FakeProc._pid[0]
        self.code = None

    def poll(self):
        return self.code


@pytest.fixture
def host(fx, monkeypatch):
    """fx with fake applet processes, a fake clock and recorded signals; one declared applet."""
    f = fx
    clock = [1000.0]
    f.applet_clock = lambda: clock[0]
    procs, signals = [], []
    monkeypatch.setattr(applet_host, '_signal', lambda pid, sig, group=False: signals.append((pid, sig, group)))
    monkeypatch.setattr(applet_host, 'clear_stale', lambda *a, **k: None)

    def spawn(cfg, spec, token):
        applet_host.run_dir(cfg, spec['name']).mkdir(parents=True, exist_ok=True)
        procs.append(FakeProc())
        return procs[-1]
    f.applet_spawn = spawn
    f.applet_specs = [{'name': 'applet-a-1', 'command': ['x'], 'cwd': '/', 'autostart': True, 'interface': 1,
                       'heartbeat_s': 10, 'stale_after_s': 60, 'stop_grace_s': 30, 'durable_state': False,
                       'lease': {'eligible': ['worker-1']}}]
    f.test_clock, f.test_procs, f.test_signals = clock, procs, signals
    return f


def h(f):
    return f.r.hgetall(applet_host.key('applet-a-1'))


def beat(f, at=None):
    f.r.hset(applet_host.key('applet-a-1'), mapping={'pid': '4242', 'hb_at': repr(at or f.test_clock[0])})


def test_first_heartbeat_moves_starting_to_up_and_a_stale_one_does_not(host):
    f = host
    f.r.hset(applet_host.key('applet-a-1'), mapping={'state': '', 'hb_at': '999.0', 'pid': '1'})
    applet_host.tick(f)
    assert h(f)['state'] == 'starting' and 'hb_at' not in h(f) and len(f.test_procs) == 1
    assert len(h(f)['token_sha256']) == 64 and h(f)['restarts'] == '0'
    beat(f, 999.0)
    applet_host.tick(f)
    assert h(f)['state'] == 'starting'
    beat(f)
    applet_host.tick(f)
    assert h(f)['state'] == 'up' and audits(f, 'applet.up')[0]['pid'] == '4242'


def test_exit_codes_zero_restart_and_config_error(host):
    f = host
    applet_host.tick(f)
    f.test_procs[-1].code = 75
    applet_host.tick(f)
    assert len(f.test_procs) == 2 and h(f)['state'] == 'starting' and h(f)['restarts'] == '1'
    assert json.loads(h(f)['crashes']) == []
    assert audits(f, 'applet.restarted')[-1]['reason'].startswith('exit 75')
    log = applet_host.run_dir(f.cfg, 'applet-a-1') / 'applet.log'
    log.write_text('starting\nbad value for poll_s\n\n')
    f.test_procs[-1].code = 78
    applet_host.tick(f)
    applet_host.tick(f)
    assert len(f.test_procs) == 2 and h(f)['state'] == 'config_error'
    assert audits(f, 'applet.config_error')[-1]['reason'] == 'bad value for poll_s'
    applet_host.request(f.r, 'applet-a-1', 'start', wait=0)
    applet_host.tick(f)
    assert len(f.test_procs) == 3 and h(f)['state'] == 'starting'
    f.test_procs[-1].code = 0
    applet_host.tick(f)
    applet_host.tick(f)
    assert len(f.test_procs) == 3 and h(f)['state'] == 'exited'
    assert audits(f, 'applet.exited')[-1]['code'] == '0'


def test_crashes_restart_at_once_and_the_third_in_the_window_is_a_crashloop(host):
    f = host
    applet_host.tick(f)
    for n in (1, 2):
        f.test_clock[0] += 100
        f.test_procs[-1].code = -signal.SIGKILL
        applet_host.tick(f)
        assert len(f.test_procs) == n + 1 and h(f)['state'] == 'starting' and h(f)['restarts'] == str(n)
    f.test_clock[0] += 100
    f.test_procs[-1].code = 1
    applet_host.tick(f)
    applet_host.tick(f)
    assert len(f.test_procs) == 3 and h(f)['state'] == 'crashloop'
    assert len(json.loads(h(f)['crashes'])) == 3
    row = audits(f, 'applet.crashloop')[0]
    assert row['crashes'] == '3' and row['window_s'] == '3600' and 'fleetmux applet start applet-a-1' in row['line']
    assert any(p == f.test_procs[-1].pid and s == signal.SIGKILL and g for p, s, g in f.test_signals)  # swept
    applet_host.request(f.r, 'applet-a-1', 'start', wait=0)
    applet_host.tick(f)
    assert len(f.test_procs) == 4 and json.loads(h(f)['crashes']) == []


def test_crashes_outside_the_window_do_not_add_up(host):
    f = host
    f.cfg['applet_host'] = {'crash_window_s': 50}
    applet_host.tick(f)
    for _ in range(4):
        f.test_clock[0] += 100
        f.test_procs[-1].code = 1
        applet_host.tick(f)
    assert h(f)['state'] == 'starting' and len(f.test_procs) == 5


def test_a_stale_heartbeat_is_a_stall_then_a_crash(host):
    f = host
    applet_host.tick(f)
    beat(f)
    applet_host.tick(f)
    f.test_clock[0] += 61
    applet_host.tick(f)
    proc = f.test_procs[-1]
    assert h(f)['state'] == 'stalled' and (proc.pid, signal.SIGTERM, False) in f.test_signals
    assert audits(f, 'applet.stalled')
    f.test_clock[0] += 31
    applet_host.tick(f)
    assert (proc.pid, signal.SIGKILL, True) in f.test_signals  # grace over: the group is killed
    proc.code = -signal.SIGKILL
    applet_host.tick(f)
    assert len(f.test_procs) == 2 and h(f)['state'] == 'starting'
    assert audits(f, 'applet.restarted')[-1]['reason'] == 'stalled'
    assert len(json.loads(h(f)['crashes'])) == 1


def test_no_heartbeat_at_all_after_start_is_a_stall(host):
    f = host
    applet_host.tick(f)
    f.test_clock[0] += 61
    applet_host.tick(f)
    assert h(f)['state'] == 'stalled'


@pytest.fixture
def asleep_host(host, fake_awake):
    """host on the process awake clock of a fake machine (fleetmux/awake.py): the applet writes Unix seconds
    (``hb_at``), the host reads them through ``awake.from_wall``."""
    host.applet_clock = None
    host.machine = fake_awake
    return host


def slept(f, seconds):
    """The machine slept: its wall clock jumps, its monotonic clock does not, and the daemon's previous tick is
    that long ago (RuntimeV2.daemon_gap still audits the gap for the bridges)."""
    f.machine.sleep(seconds)
    f.v2.last_tick = time.time() - seconds


def test_a_daemon_gap_is_not_an_applet_stall(asleep_host):
    """Lid closed: the daemon and the applet sleep together. The wake tick runs the whole Fleet.tick (production
    order: the applets part runs before the runtime tick) and must not call the applet stalled."""
    f = asleep_host
    applet_host.tick(f)
    beat(f, f.machine.wall())
    applet_host.tick(f)
    assert h(f)['state'] == 'up'
    for _ in range(3):  # three dark wakes in one crash window
        slept(f, 900)
        f.tick()
        assert h(f)['state'] == 'up' and not audits(f, 'applet.stalled'), 'the sleep is not the applet being still'
        f.machine.run(5)
        beat(f, f.machine.wall())  # the applet beats again soon after the wake
        f.tick()
    assert len(f.test_procs) == 1 and not audits(f, 'applet.restarted') and not audits(f, 'applet.crashloop')
    assert json.loads(h(f).get('crashes') or '[]') == []
    assert len(audits(f, 'daemon.gap')) == 3


def test_an_applet_silent_for_a_whole_window_after_the_wake_still_stalls(asleep_host):
    f = asleep_host
    applet_host.tick(f)
    beat(f, f.machine.wall())
    applet_host.tick(f)
    slept(f, 900)
    f.tick()
    assert h(f)['state'] == 'up'
    f.machine.run(61)  # awake, and no beat for stale_after_s of awake time
    f.tick()
    assert h(f)['state'] == 'stalled' and audits(f, 'applet.stalled')


def test_exits_right_after_wakes_are_restarted_without_a_crashloop(asleep_host):
    """The sleep broke the applet (its health check timed out on the wake, its ssh child was stopped): each such
    exit is restarted and logged, none counts toward crash_max."""
    f = asleep_host
    applet_host.tick(f)
    beat(f, f.machine.wall())
    applet_host.tick(f)
    for n in range(3):  # three lid closes in one crash window
        slept(f, 900)
        f.tick()  # the first tick after the wake measures the sleep
        f.machine.run(5)
        f.test_procs[-1].code = 1  # it exits on its own a few seconds after the wake
        f.tick()
        assert len(f.test_procs) == n + 2 and h(f)['state'] == 'starting'
        f.machine.run(1)
        beat(f, f.machine.wall())
        f.tick()
    assert len(audits(f, 'applet.exit_after_wake')) == 3
    assert not audits(f, 'applet.crashloop') and json.loads(h(f).get('crashes') or '[]') == []


def test_exits_without_a_gap_still_make_a_crashloop(asleep_host):
    f = asleep_host
    applet_host.tick(f)
    for _ in range(3):
        f.machine.run(60)  # awake all along
        f.test_procs[-1].code = 1
        f.tick()
    assert h(f)['state'] == 'crashloop' and audits(f, 'applet.crashloop')
    assert not audits(f, 'applet.exit_after_wake') and len(f.test_procs) == 3


def test_an_exit_long_after_the_wake_counts_again(asleep_host):
    f = asleep_host
    applet_host.tick(f)
    slept(f, 900)
    f.tick()
    f.machine.run(21)  # past two heartbeat periods (heartbeat_s 10) of awake time
    f.test_procs[-1].code = 1
    f.tick()
    assert not audits(f, 'applet.exit_after_wake') and len(json.loads(h(f)['crashes'])) == 1


def test_the_crash_window_is_awake_time(asleep_host):
    """crash_window_s counts awake time: three real crashes (each well past the wake grace) with hours of sleep
    between them are still three crashes within the window."""
    f = asleep_host
    applet_host.tick(f)
    for _ in range(3):
        slept(f, 2 * 3600)
        f.tick()
        f.machine.run(60)
        f.test_procs[-1].code = 1
        f.tick()
    assert h(f)['state'] == 'crashloop' and not audits(f, 'applet.exit_after_wake')


def test_a_stall_without_a_gap_still_trips_on_the_full_tick(host):
    f = host
    applet_host.tick(f)
    beat(f)
    f.tick()
    f.test_clock[0] += 61  # the applet clock moves, the daemon ticked all along
    f.tick()
    assert h(f)['state'] == 'stalled' and not audits(f, 'daemon.gap')


def test_operator_stop_and_restart(host):
    f = host
    applet_host.tick(f)
    first = f.test_procs[-1]
    applet_host.request(f.r, 'applet-a-1', 'restart', wait=0)
    applet_host.tick(f)
    assert h(f)['state'] == 'stopping' and (first.pid, signal.SIGTERM, False) in f.test_signals
    first.code = 0
    applet_host.tick(f)
    assert len(f.test_procs) == 2 and h(f)['state'] == 'starting' and h(f)['restarts'] == '0'
    applet_host.request(f.r, 'applet-a-1', 'stop', wait=0)
    applet_host.tick(f)
    f.test_procs[-1].code = 0
    applet_host.tick(f)
    applet_host.tick(f)
    assert h(f)['state'] == 'exited' and len(f.test_procs) == 2
    assert [a['reason'] for a in audits(f, 'applet.stopped')] == ['operator restart', 'operator']
    replies = [json.loads(f.r.get(k)) for k in f.r.scan_iter(applet_host.CTL_REPLY + '*')]
    assert sorted(r['status'] for r in replies) == ['ok', 'ok']
    applet_host.request(f.r, 'applet-nobody-1', 'start', wait=0)
    applet_host.tick(f)
    replies = [json.loads(f.r.get(k)) for k in f.r.scan_iter(applet_host.CTL_REPLY + '*')]
    assert any(r['status'] == 'rejected' and 'no applet applet-nobody-1' in r['reason'] for r in replies)


def test_autostart_false_waits_for_an_operator_start(host):
    f = host
    f.applet_specs[0]['autostart'] = False
    applet_host.tick(f)
    assert h(f)['state'] == 'declared' and not f.test_procs
    applet_host.request(f.r, 'applet-a-1', 'start', wait=0)
    applet_host.tick(f)
    assert h(f)['state'] == 'starting' and len(f.test_procs) == 1


def test_an_unsupported_interface_is_a_config_error_without_a_process(host):
    f = host
    f.applet_specs[0]['interface'] = 2
    applet_host.tick(f)
    assert h(f)['state'] == 'config_error' and not f.test_procs
    assert 'interface 2' in audits(f, 'applet.config_error')[0]['reason']


def test_daemon_resume_keeps_a_held_state_and_the_crash_history(host):
    f = host
    f.r.hset(applet_host.key('applet-a-1'), mapping={'state': 'crashloop', 'crashes': '[1.0, 2.0, 3.0]',
                                                     'restarts': '2'})
    applet_host.tick(f)
    assert h(f)['state'] == 'crashloop' and not f.test_procs
    f.applet_runners, f.applet_seen = {}, set()
    f.r.hset(applet_host.key('applet-a-1'), mapping={'state': 'up', 'crashes': '[999.0]', 'restarts': '1'})
    applet_host.tick(f)
    # no owner record: not adoptable, so the restart counts as one crash (applets.md 2.3)
    assert len(f.test_procs) == 1 and h(f)['restarts'] == '2' and json.loads(h(f)['crashes']) == [999.0, 1000.0]
    assert 'no owner record' in audits(f, 'applet.restarted')[-1]['reason']


def test_an_edited_row_applies_at_the_next_start(host):
    f = host
    applet_host.tick(f)
    f.applet_specs = [dict(f.applet_specs[0], stop_grace_s=7)]
    applet_host.tick(f)
    assert len(f.test_procs) == 1 and f.applet_runners['applet-a-1'].spec['stop_grace_s'] == 7


def test_stop_all_sends_host_draining_then_terms_and_kills(host):
    f = host
    applet_host.tick(f)
    proc = f.test_procs[-1]
    f.applet_sleep = lambda s: f.test_clock.__setitem__(0, f.test_clock[0] + s)
    assert applet_host.stop_all(f) is False  # the fake never exits
    data = [json.loads(x['data']) for _, x in f.r.xrange(applet_host.key('applet-a-1') + ':events')]
    assert [d['type'] for d in data] == ['host.draining'] and data[0]['reason'] == 'stop'
    assert (proc.pid, signal.SIGTERM, False) in f.test_signals and (proc.pid, signal.SIGKILL, True) in f.test_signals
    assert h(f)['state'] == 'exited' and f.applet_runners == {}
    assert applet_host.draining_event(f.r, 'applet-a-1', 'again', 0, since=0) is False  # one per start


def test_mirror_writes_the_parent_hash(fx):
    f = fx
    applet_host.mirror(f, 'team', {'applets': {'applet-a-1': 'up', 'applet-b-1': 'crashloop'}})
    got = f.r.hgetall('fx:subfleet:team')
    assert json.loads(got['applets']) == {'applet-a-1': 'up', 'applet-b-1': 'crashloop'}
    assert got['applet_crashloop'] == 'applet-b-1'
    applet_host.mirror(f, 'team', None)
    applet_host.mirror(f, 'other', {'ready': True})
    assert not f.r.exists('fx:subfleet:other')


def test_status_rows_read_state_phase_and_open_leases(fx):
    f = fx
    f.r.hset(applet_host.key('applet-a-1'), mapping={'state': 'up', 'phase': 'merging'})
    f.r.sadd(applet_host.key('applet-a-1') + ':leases', 'L-00000001')
    f.r.hset(applet_host.key('applet-a-1') + ':params', 'x', '1')
    assert applet_host.rows(f.r) == [{'name': 'applet-a-1', 'state': 'up', 'phase': 'merging',
                                      'leases': ['L-00000001']}]
    assert applet_host.tree_rows(f.cfg) == applet_host.rows(f.r)


def test_task_fleet_refuses_an_applets_roster(fx):
    f = fx
    f.service = False
    write_roster(f, WORKERS + applet_rows())
    f.roster_tick()
    assert 'only a service fleet' in f.r.hget(fleet.ROSTER, 'rejected')
    assert not getattr(f, 'applet_specs', None)


def test_tick_table_ends_with_the_applets_segment():
    import inspect
    src = inspect.getsource(fleet.Fleet.tick)
    assert src.index("('applets', lambda: applet_host.tick(self))") > src.index("('security', self.security_tick)")


def test_fixture_applet_is_python_only_and_calls_no_model():
    text = (FIXTURE / 'minimal_applet.py').read_text()
    for word in ('anthropic', 'openai', 'claude', 'codex'):
        assert word not in text
    assert sys.executable


def test_daemon_resume_starts_an_applet_that_ran_even_without_autostart(host):
    f = host
    f.applet_specs[0]['autostart'] = False
    f.r.hset(applet_host.key('applet-a-1'), mapping={'state': 'up', 'restarts': '0'})
    applet_host.tick(f)
    assert len(f.test_procs) == 1 and h(f)['state'] == 'starting'


def test_daemon_resume_of_an_applet_being_stopped_leaves_it_exited(host):
    f = host
    f.r.hset(applet_host.key('applet-a-1'), mapping={'state': 'stopping'})
    applet_host.tick(f)
    assert h(f)['state'] == 'exited' and not f.test_procs


# ====================================================================== named sub-fleet host: parent side
def test_parent_mirror_writes_only_changes_and_a_collected_host_clears_it(fx):
    from fleetmux import named_subfleet
    f = fx
    applet_host.mirror(f, 'team', {'applets': {}})
    assert not f.r.exists('fx:subfleet:team')  # a host without applets: no write
    applet_host.mirror(f, 'team', {'applets': {'applet-a-1': 'up'}})
    assert f.r.hget('fx:subfleet:team', 'applets') == '{"applet-a-1": "up"}'
    named_subfleet.collected(f, {'name': 'team', 'outcome': 'down'}, returned=True)
    got = f.r.hgetall('fx:subfleet:team')
    assert got['state'] == 'down' and got['applets'] == '{}' and got['applet_crashloop'] == ''


def test_child_peek_reports_the_child_applet_states(fx):
    from fleetmux import named_subfleet
    f = fx
    f.r.hset(applet_host.key('applet-a-1'), mapping={'state': 'crashloop'})
    assert named_subfleet.child_peek(f.cfg)['applets'] == {'applet-a-1': 'crashloop'}


def test_parent_drain_sends_host_draining_to_the_child_applets(fx, monkeypatch):
    from fleetmux import named_subfleet
    f = fx  # stands for the child: its run directory and Redis
    run = Path(f.cfg['run'])
    monkeypatch.setattr(named_subfleet, 'live', lambda fl: {'team': run})
    monkeypatch.setattr(named_subfleet, 'declared', lambda fl: {'team': {'drain_s': 120}})
    f.r.hset(applet_host.key('applet-a-1'), mapping={'state': 'up', 'started_at': '1.0'})
    f.r.hset(applet_host.key('applet-b-1'), mapping={'state': 'exited', 'started_at': '1.0'})
    named_subfleet.begin_drain(f, 'team', 'external')
    data = [json.loads(x['data']) for _, x in f.r.xrange(applet_host.key('applet-a-1') + ':events')]
    assert [(d['type'], d['reason']) for d in data] == [('host.draining', 'down')]  # documented reasons only
    assert 100 < data[0]['deadline_at'] - data[0]['at'] <= 120
    assert not f.r.exists(applet_host.key('applet-b-1') + ':events')


def test_day_and_night_rosters_must_declare_the_same_applets(tmp_path):
    from fleetmux import night
    day, nite = tmp_path / 'day.yaml', tmp_path / 'night.yaml'
    day.write_text(WORKERS + 'applets:\n  - {name: applet-a-1, command: [x]}\n')
    nite.write_text(WORKERS)
    with pytest.raises(ValueError, match='"applets:"'):
        night.check_rosters({}, day, nite)
    nite.write_text(day.read_text())
    night.check_rosters({}, day, nite)


# ====================================================================== leases end before an applet is stopped (M9)
def leases(f, *ids, name='applet-a-1'):
    k = applet_host.key(name) + ':leases'
    f.r.delete(k)
    if ids:
        f.r.sadd(k, *ids)


def drained(f, name='applet-a-1'):
    return [json.loads(x['data']) for _, x in f.r.xrange(applet_host.key(name) + ':events')
            if json.loads(x['data'])['type'] == 'host.draining']


def test_detach_waits_for_the_lease_set_then_stops(host):
    f = host
    applet_host.tick(f)
    proc = f.test_procs[-1]
    beat(f)
    applet_host.tick(f)
    leases(f, 'L-00000001')
    f.applet_specs = []
    applet_host.tick(f)
    [ev] = drained(f)
    assert ev['reason'] == 'detach' and ev['deadline_at'] == f.test_clock[0] + 60  # detach_wait_s default
    assert h(f)['state'] == 'up' and not any(s == signal.SIGTERM for _, s, _ in f.test_signals)
    f.test_clock[0] += 59
    applet_host.tick(f)  # heartbeat stale on purpose: no stall while waiting for the leases
    assert h(f)['state'] == 'up' and not f.test_signals
    leases(f)
    applet_host.tick(f)
    assert h(f)['state'] == 'stopping' and (proc.pid, signal.SIGTERM, False) in f.test_signals
    proc.code = 0
    applet_host.tick(f)
    assert h(f) == {} and f.applet_runners == {}  # detached: the applet's keys are cleared


def test_detach_is_stopped_at_detach_wait_s(host):
    f = host
    f.applet_specs[0]['detach_wait_s'] = 50
    applet_host.tick(f)
    proc = f.test_procs[-1]
    leases(f, 'L-00000001')
    f.applet_specs = []
    applet_host.tick(f)
    f.test_clock[0] += 49
    applet_host.tick(f)
    assert not f.test_signals
    f.test_clock[0] += 1
    applet_host.tick(f)
    assert h(f)['state'] == 'stopping' and (proc.pid, signal.SIGTERM, False) in f.test_signals
    f.test_clock[0] += 30
    applet_host.tick(f)
    assert (proc.pid, signal.SIGKILL, True) in f.test_signals  # then stop_grace_s as before


def test_operator_stop_does_not_wait_for_leases(host):
    f = host
    applet_host.tick(f)
    proc = f.test_procs[-1]
    leases(f, 'L-00000001')
    applet_host.request(f.r, 'applet-a-1', 'stop', wait=0)
    applet_host.tick(f)
    assert h(f)['state'] == 'stopping' and (proc.pid, signal.SIGTERM, False) in f.test_signals
    assert drained(f) == []


def test_root_fleet_stop_waits_for_leases_up_to_detach_wait_s(host):
    f = host
    f.applet_specs[0]['detach_wait_s'] = 50
    assert applet_host.stop_wait(f) is False  # not stopping
    applet_host.tick(f)
    f.explicit_stop = f.stopping = True
    assert applet_host.stop_wait(f) is False  # no open lease: stop at once
    f.applet_stop_deadline = None
    leases(f, 'L-00000001')
    assert applet_host.stop_wait(f) is True
    [ev] = drained(f)
    assert ev['reason'] == 'stop' and ev['deadline_at'] == f.test_clock[0] + 50
    f.test_clock[0] += 10
    assert applet_host.stop_wait(f) is True and len(drained(f)) == 1
    leases(f)
    assert applet_host.stop_wait(f) is False  # the leases ended
    leases(f, 'L-00000002')
    f.test_clock[0] += 40
    assert applet_host.stop_wait(f) is False  # the limit is the whole wait of this stop
    assert not f.test_signals  # stop_wait never signals; stop_all does


def test_named_sub_fleet_stop_does_not_wait_again(host):
    f = host
    applet_host.tick(f)
    leases(f, 'L-00000001')
    f.explicit_stop = f.stopping = True
    f.cfg['parent_config'] = '/parent/config.json'
    assert applet_host.stop_wait(f) is False and drained(f) == []


@pytest.mark.parametrize('release', [True, False])
def test_fixture_applet_fleet_stop_waits_for_its_lease(fx, monkeypatch, release):
    """The real stop path keeps the daemon loop running until the lease set is empty or the limit is reached."""
    f = fx
    from fleetmux import applet_lease
    monkeypatch.setattr(applet_lease, 'tick', lambda fl: None)  # the seeded set below stands for the lease side
    write_roster(f, WORKERS + applet_rows(detach_wait_s=3.0))
    key = applet_host.key('applet-minimal-1')
    rd = Path(f.cfg['run']) / 'applets' / 'applet-minimal-1'
    assert wait_for(lambda: f.r.hget(key, 'state') == 'up' and (rd / 'child.pid').exists(), tick=f.tick)
    applet_pid, child = int(f.r.hget(key, 'pid')), int((rd / 'child.pid').read_text())
    f.r.sadd(key + ':leases', 'L-00000001')
    seen = {}
    real_tick = f.tick

    def tick():
        seen['ticks'] = seen.get('ticks', 0) + 1
        if release:
            f.r.delete(key + ':leases')  # what the lease side does once the applet released its lease
        # the host never sends SIGTERM while it still waits (the applet itself may end on host.draining)
        assert all(r.stop_deadline is None for r in f.applet_runners.values())
        real_tick()
    monkeypatch.setattr(f, 'tick', tick)
    f.v2.close()
    f.v2.reader = f.v2.bus.blocking_client(1000)
    f.stopping = f.explicit_stop = True
    started = time.time()
    f.run()
    took = time.time() - started
    assert seen  # the loop ran on after the stop
    # Without a release the applet still sees host.draining (SDK heartbeat thread), ends its run and exits 0, so
    # the wait ends with the process, before detach_wait_s (the limit: test_root_fleet_stop_waits_for_leases_...).
    assert took < (2.8 if release else 3.0)  # each loop pass blocks up to 1 s on the bus
    assert release or f.r.hget(key, 'state') == 'exited'
    assert [d['reason'] for d in drained(f, 'applet-minimal-1')] == ['stop']
    assert wait_for(lambda: not alive(applet_pid) and not alive(child))
