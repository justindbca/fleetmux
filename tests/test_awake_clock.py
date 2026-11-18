"""Awake time: every deadline counts only the time the machine was awake (fleetmux/awake.py, design.md section 35).

A closed laptop lid stops the fleet; the time it slept is not time anybody waited. Each test below runs a fake
machine (conftest ``fake_awake``): ``sleep(s)`` moves the wall clock (``time.time`` too) and leaves the monotonic
clock where it was, ``run(s)`` moves both. A sleep longer than a deadline must not expire it; the same amount of
awake time must.
"""
import json
import threading
import time

import pytest

from fleetmux import applet_lease as al, awake, fleet, lifecycle_close, runtime_v2
from fleetmux.applet import core as sdk
from fleetmux.applet.lease import Lease

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_applet_lease import NAME, ask, decide, grant, host, open_lease, rec  # noqa: F401  (fixture)
from test_runtime_v2 import fx  # noqa: F401  (fixture)
from test_v2_codex_service import service  # noqa: F401  (fixture)
from test_request_no_deadline import post, result
from test_lifecycle_close import notes, table  # noqa: F401  (fixtures)

DAY = 86400.0
SHIPPED_CLOCK = al.clock  # before test_applet_lease.host puts its own fake in


class Hash:
    """The two Redis calls the clock makes, on a dict."""

    def __init__(self):
        self.data = {}

    def hgetall(self, key):
        return dict(self.data.get(key, {}))

    def hset(self, key, mapping):
        self.data.setdefault(key, {}).update({k: str(v) for k, v in mapping.items()})


class Down:
    def hgetall(self, key):
        raise ConnectionError('host Redis is down')


@pytest.fixture
def machine(fake_awake, monkeypatch):
    """fake_awake, with ``time.time`` on the machine's wall clock (what the code before awake time read)."""
    monkeypatch.setattr(time, 'time', fake_awake.wall)
    return fake_awake


# ====================================================================== the clock
def test_a_sleep_is_not_awake_time(fake_awake):
    t0 = awake.now()
    assert t0 == fake_awake.wall()  # nothing slept yet: awake time is the Unix time
    fake_awake.sleep(DAY)
    assert awake.now() == t0 and awake.since(t0) == 0
    fake_awake.run(5)
    assert awake.since(t0) == 5
    deadline = awake.deadline(60)
    fake_awake.sleep(DAY)
    assert not awake.passed(deadline)
    fake_awake.run(60)
    assert awake.passed(deadline)


def test_from_wall_leaves_out_the_sleeps_after_a_unix_stamp(fake_awake):
    before = fake_awake.wall()
    fake_awake.run(10)
    awake.CLOCK.fold()  # a daemon tick
    fake_awake.sleep(3600)
    fake_awake.run(2)
    awake.CLOCK.fold()  # the first tick after the wake logs the gap
    after = fake_awake.wall()
    fake_awake.run(30)
    assert awake.since_wall(before) == pytest.approx(42)  # 10 + 2 + 30: the hour asleep is no age
    assert awake.since_wall(after) == pytest.approx(30)
    assert awake.between_wall(before, after) == pytest.approx(12)
    inside = before + 10 + 1800  # written somewhere in the window that held the sleep: never older than the window
    assert 30 <= awake.since_wall(inside) <= 32


def test_many_sleeps_are_merged_not_forgotten(fake_awake):
    start = fake_awake.wall()
    for _ in range(awake.GAPS_KEEP + 10):  # a night of dark wakes
        fake_awake.run(1)
        awake.CLOCK.fold()
        fake_awake.sleep(300)
        awake.CLOCK.fold()
    assert len(awake.CLOCK.gaps) == awake.GAPS_KEEP
    assert awake.since_wall(start) == pytest.approx(awake.GAPS_KEEP + 10)  # only the awake seconds


def test_a_reader_measures_the_sleep_since_the_daemon_published(fake_awake):
    """No lag after a wake: a process that reads the published clock before the daemon's next tick still leaves
    the sleep out (it measures it from the published point itself)."""
    r = Hash()
    awake.tick(r)  # the daemon publishes
    daemon_now = awake.now()
    fake_awake.sleep(3600)  # the daemon has not ticked since the wake
    reader = awake.of(r)
    assert reader.now() == pytest.approx(daemon_now)
    fake_awake.run(7)
    assert reader.now() == pytest.approx(daemon_now + 7) == pytest.approx(awake.now())


def test_a_restarted_daemon_goes_on_from_the_published_clock(fake_awake):
    r = Hash()
    awake.tick(r)
    fake_awake.sleep(500)
    fake_awake.run(1)
    awake.tick(r)
    assert float(r.data[awake.KEY]['slept_s']) == pytest.approx(500)
    before = awake.now()
    fake_awake.sleep(200)  # the daemon was down and the machine slept: the next daemon still leaves it out
    awake.reset(fake_awake.wall, fake_awake.mono, boot=1)
    awake.tick(r)
    assert awake.now() == pytest.approx(before) and float(r.data[awake.KEY]['slept_s']) == pytest.approx(700)


def test_a_reader_of_another_boot_keeps_the_total_and_measures_from_its_read(fake_awake):
    r = Hash()
    awake.tick(r)
    fake_awake.sleep(500)
    awake.tick(r)
    other = awake.Clock(fake_awake.wall, fake_awake.mono, boot=99)  # another boot: the published point is not ours
    assert other.sync(r)
    assert other.now() == pytest.approx(awake.now())
    fake_awake.sleep(100)
    fake_awake.run(3)
    assert other.now() - awake.now() == pytest.approx(0)


def test_the_monotonic_clock_is_the_one_that_stops_in_sleep():
    """Documented behaviour this design relies on (fleetmux/awake.py docstring)."""
    import sys
    info = time.get_clock_info('monotonic')
    if sys.platform == 'darwin':
        assert info.implementation == 'mach_absolute_time()'
        assert abs(time.monotonic() - time.clock_gettime(time.CLOCK_UPTIME_RAW)) < 0.01
    elif sys.platform.startswith('linux'):
        assert 'CLOCK_MONOTONIC' in info.implementation
    assert awake.boot_id() == awake.boot_id()


# ====================================================================== deadlines across a sleep
def test_request_timeout_counts_awake_time_only(machine, service):
    f = service
    post(f, 'r1', to='worker-1', timeout_s=60)
    machine.sleep(3600)
    f.tick()
    assert result(f, 'r1') is None, 'an hour asleep is not the request taking an hour'
    machine.run(61)
    f.tick()
    assert result(f, 'r1')['status'] == 'timeout'


@pytest.fixture
def lease_host(machine, host, monkeypatch):
    """test_applet_lease.host on the shipped lease clock (not its own fake ``al.clock``)."""
    monkeypatch.setattr(al, 'clock', SHIPPED_CLOCK)
    host.machine = machine
    return host


def test_decision_wait_counts_awake_time_only(lease_host):
    f, m = lease_host, lease_host.machine
    ask(f, 'a.1')
    lid = open_lease(f)
    m.sleep(3 * 3600)  # decision_wait_s is 600
    al.tick(f)
    assert rec(f, lid)['state'] == 'requested' and f.r.get(fleet.RESULT + 'a.1') is None
    m.run(601)
    al.tick(f)
    assert (rec(f, lid)['state'], rec(f, lid)['end_reason']) == ('denied', 'no_decision')


def test_escalation_wait_counts_awake_time_only(lease_host):
    from test_applet_lease import op
    f, m = lease_host, lease_host.machine
    assert op(f, 'applet.escalate', 'e.1', **{'class': 'plan'}, what='x', why='y', wait_s=120) is None
    m.sleep(3600)
    al.tick(f)
    assert f.r.get(fleet.RESULT + 'e.1') is None, 'no_decision right after a wake'
    m.run(121)
    al.tick(f)
    assert json.loads(f.r.get(fleet.RESULT + 'e.1'))['reason'] == 'no_decision'


def test_lease_term_counts_awake_time_only(lease_host):
    f, m = lease_host, lease_host.machine
    lid = grant(f)  # ttl 600
    assert rec(f, lid)['state'] == 'active'
    m.sleep(3 * 3600)
    al.tick(f)
    assert rec(f, lid)['state'] == 'active'
    m.run(601)
    al.tick(f)
    assert rec(f, lid)['state'] == 'expired'


def test_prepare_timeout_counts_awake_time_only(lease_host):
    f, m = lease_host, lease_host.machine
    f.setf('worker-1', delivered=1)
    ask(f, 'a.1')
    decide(f, {'id': 'a.1', 'decision': 'grant', 'slot': 'worker-1'})
    lid = json.loads(f.r.get(fleet.RESULT + 'a.1'))['lease']['id']
    m.sleep(3600)  # prepare_timeout_s is 300
    al.tick(f)
    assert rec(f, lid)['state'] == 'granted'
    m.run(301)
    al.tick(f)
    assert (rec(f, lid)['state'], rec(f, lid)['end_reason']) == ('revoked', 'prepare_failed')


def test_idle_reclaim_counts_awake_time_only(machine, fx, table, notes):
    f = fx
    f.cfg['lifecycle'] = 'service'
    f.cfg['worker_idle_reclaim_min'] = 30.0
    for slot in f.workers:
        f.setf(slot, busy_since=time.time(), transcript='')
    lifecycle_close.tick(f)
    machine.sleep(3 * 3600)
    f.tick()  # the daemon's first tick after the wake
    lifecycle_close.tick(f)
    assert f.agent('worker-1')['state'] == 'idle', 'three hours asleep is no idleness'
    machine.run(31 * 60)
    lifecycle_close.tick(f)
    assert f.agent('worker-1')['state'] == 'reclaimed'


# ====================================================================== the applet SDK
class FailingRedis(Hash):
    def hset(self, key, mapping):
        raise ConnectionError('host Redis is down')


def test_sdk_stale_exit_counts_awake_time_only(machine, tmp_path):
    app = sdk.Applet(name='applet-a-1', token='t', redis=FailingRedis(), config_path=tmp_path / 'c.yaml',
                     state_dir=tmp_path, run_dir=tmp_path, heartbeat_s=0.01, stale_after_s=60)
    thread = threading.Thread(target=app._heartbeat, daemon=True)
    thread.start()
    time.sleep(0.05)
    machine.sleep(3600)  # one failed beat right after the wake
    time.sleep(0.1)
    assert not app.stopping.is_set(), 'the hour asleep is not an hour without Redis'
    machine.run(61)
    thread.join(2)
    assert app.stopping.is_set()


def _applet(redis, tmp_path):
    return sdk.Applet(name='applet-a-1', token='t', redis=redis, config_path=tmp_path / 'c.yaml',
                      state_dir=tmp_path, run_dir=tmp_path)


def test_sdk_reads_the_published_host_clock(fake_awake, tmp_path):
    r = Hash()
    fake_awake.sleep(5000)
    fake_awake.run(1)
    awake.tick(r)  # the host has measured 5000 s asleep: its awake time is 5000 s behind the Unix time
    expires = awake.now() + 600
    app = _applet(r, tmp_path)
    app.fleet_clock.sync(r)
    lease = Lease(app, {'id': 'L1', 'state': 'active', 'expires_at': expires})
    assert lease.remaining_s() == pytest.approx(600)
    fake_awake.sleep(3600)
    assert lease.remaining_s() == pytest.approx(600)
    fake_awake.run(100)
    assert lease.remaining_s() == pytest.approx(500)


def test_sdk_measures_locally_while_the_host_redis_is_down(fake_awake, tmp_path):
    app = _applet(Down(), tmp_path)
    assert not app.fleet_clock.sync(app.redis)
    lease = Lease(app, {'id': 'L1', 'state': 'active', 'expires_at': app.fleet_clock.now() + 600})
    fake_awake.sleep(3600)
    assert lease.remaining_s() == pytest.approx(600)
    fake_awake.run(100)
    assert lease.remaining_s() == pytest.approx(500)


def test_sdk_local_waits_do_not_count_a_sleep(machine, tmp_path):
    r = Hash()
    app = _applet(r, tmp_path)
    seen = []

    def poll(*a):
        seen.append(1)
        if len(seen) == 2:
            machine.sleep(3600)  # the lid closes during the wait
        if len(seen) == 5:
            r.data.setdefault('answers', {})['x'] = 1
    r.get = lambda key: json.dumps({'status': 'ok'}) if r.data.get('answers') else None
    import fleetmux.applet.lease as sdk_lease
    orig = sdk_lease.time.sleep
    sdk_lease.time.sleep = poll
    try:
        out = app._wait_result('x', 60)
    finally:
        sdk_lease.time.sleep = orig
    assert out == {'status': 'ok'}, 'the wait ran out across the sleep'


def test_a_new_fleet_process_stamps_in_the_fleets_awake_time(fake_awake, fx):
    """A process that stamps a deadline before the daemon's first tick (a CLI, a closer, a resumed daemon) uses
    the published total, not Unix time: otherwise the deadline would sit the whole slept total late."""
    f = fx
    fake_awake.sleep(5000)
    fake_awake.run(1)
    awake.tick(f.r)  # the fleet's daemon has measured 5000 s asleep
    awake.reset(fake_awake.wall, fake_awake.mono, boot=1)  # a new process
    fleet.Fleet(f.path, f.r, f.mgr)
    assert awake.deadline(60) == pytest.approx(fake_awake.wall() - 5000 + 60)


# ====================================================================== the task hours budget
from test_v2_codex_claude import mixed, submit as submit_task  # noqa: E402,F401  (fixture: a v2 task fleet)


def test_task_budget_counts_awake_time_only(machine, mixed):
    f = mixed
    submit_task(f)
    task = f.task()
    assert float(task['deadline']) == pytest.approx(awake.now() + float(task['budget_s']), abs=2)
    f.r.hset(fleet.TASK, mapping={'deadline': awake.now() + 60})  # one awake minute left
    msg = f.v2.bus.message(f.v2.bus.slot_state('supervisor')['message_id'])
    assert int(msg['deadline_at']) == runtime_v2.NO_DEADLINE_MS  # the bus does not carry the (awake) budget
    machine.sleep(8 * 3600)  # a lid-closed night
    f.tick()
    assert f.task()['status'] == 'running', 'the night asleep used none of the budget'
    assert f.agent('supervisor')['state'] != 'held'
    shown = fleet.task_clock(dict(f.task()))
    assert shown['budget_remaining_s'] == pytest.approx(60, abs=1)
    machine.run(61)
    f.tick()
    assert f.task()['status'] == 'budget_exhausted'


def test_task_waiting_horizon_counts_awake_time_only(machine, mixed):
    f = mixed
    submit_task(f)
    f.apply_waiting({'waiting': {'for': 'CI', 'check': 'the CI page', 'for_hours': 1}})
    f.setf('supervisor', state='idle')  # the reminder goes to an idle supervisor
    machine.sleep(5 * 3600)
    f.waiting_tick()
    assert not json.loads(f.task()['waiting']).get('nudged')
    machine.run(3601)
    f.waiting_tick()
    assert json.loads(f.task()['waiting']).get('nudged')


def test_scheduled_hard_timeout_counts_awake_time_only(machine, fx, table, notes):
    from test_lifecycle_close import as_task, submit as submit_close
    from fleetmux import lifecycle
    f = as_task(fx, 'scheduled')
    submit_close(f, {'goal': 'sector rotation 09:00'})
    limit = lifecycle.mode_policy('scheduled', 'hard_timeout_min')
    lifecycle_close.tick(f)
    machine.sleep(limit * 60 * 3)
    f.tick()
    lifecycle_close.tick(f)
    assert f.task()['status'] == 'running'
    machine.run(limit * 60 + 1)
    lifecycle_close.tick(f)
    assert f.task()['status'] == 'budget_exhausted'


def test_brake_hours_count_awake_time_only(machine):
    from fleetmux import brake
    state = {'started': repr(time.time())}
    machine.run(10)
    awake.CLOCK.fold()
    machine.sleep(5 * 3600)
    machine.run(3590)
    assert brake.counters(state, {})['hours'] == pytest.approx(1.0, abs=0.01)
