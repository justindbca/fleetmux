"""Release drift (docs/design.md section 56, "Release drift"): a root daemon compares the release of every process of
its run with the installed one and switches the run to it when the drift outlived the grace, the installed release
passes its self-check, no blackout window is open and no worker is in a turn.

Everything here is stubbed: a fake fleet object, a fake ``Ops`` (clock, process table, Redis, start command, notices)
and run directories under ``tmp_path``. No real process is signalled, no Redis is reached, no notice leaves the test,
and neither ``~/fleetmux-current`` nor the private fleet.yaml is read.
"""
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from fleetmux import fleet_config, release_drift, releases

KNOBS = dict(release_drift.DEFAULTS)
ORIG = dict(vars(release_drift.Ops))  # the real methods, before the autouse fixture replaces them
T0 = 1000.0
LATER = T0 + KNOBS['grace_s'] + 1   # past the grace, and past the check the first tick scheduled


@pytest.fixture(autouse=True)
def no_real_side_effects(monkeypatch):
    """Nothing in this file may signal, spawn or read the operator's files: the real side effects fail the test."""
    def refuse(*a, **k):
        raise AssertionError('a real side effect was reached in a stubbed test: %r %r' % (a, k))
    monkeypatch.setattr(os, 'kill', refuse)
    monkeypatch.setattr(os, 'killpg', refuse)
    monkeypatch.setattr(subprocess, 'Popen', refuse)
    for name in ('now', 'sleep', 'installed_dir', 'own', 'own_dir', 'cmdline', 'run', 'spawn', 'notify_user',
                 'ps', 'kill', 'alive', 'switcher_alive', 'start',
                 'daemon_running', 'redis_get', 'redis_shutdown', 'sub_busy', 'knobs'):
        monkeypatch.setattr(release_drift.Ops, name, refuse, raising=False)
    fleet_config.pin(fleet_config.NONE)  # never the private fleet.yaml
    yield
    fleet_config.pin(None)


def make_release(root, name):
    d = root / 'fleetmux-releases' / name
    for rel in ('fleetmux/fleet.py', '.venv/bin/python', 'bin/fleetmux', 'pyproject.toml'):
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text('')
    return d


def daemon_args(rel_dir, cfg_path):
    return '%s/.venv/bin/python %s/fleetmux/fleet.py daemon %s --resume' % (rel_dir, rel_dir, cfg_path)


@pytest.fixture
def world(tmp_path):
    old, new = make_release(tmp_path, 'old'), make_release(tmp_path, 'new')
    home = tmp_path / 'home'
    run = home / 'runs' / 'fleet-1'
    sub = run / 'sub' / 'M1'
    rt = tmp_path / 'state' / 'fleetmux' / 'fleetmux'
    for d in (sub, rt):
        d.mkdir(parents=True)
    (home / 'start.sh').write_text('#!/bin/sh\n')
    cfg = {'run': str(run), 'name': 'demo', 'fleet_home': str(home), 'backend': 'fleetmux', 'mode': 'service',
           'redis_socket': str(tmp_path / 'root.sock'), 'workers': ['worker-1', 'worker-2']}
    (run / 'config.json').write_text(json.dumps(cfg))
    sub_cfg = {'run': str(sub), 'name': 'M1', 'parent_config': str(run / 'config.json'),
               'redis_socket': str(tmp_path / 'sub.sock'), 'workers': ['worker-1']}
    (sub / 'config.json').write_text(json.dumps(sub_cfg))
    (sub / 'subfleet.json').write_text('{}')
    for d in (run, sub):
        (d / 'release').write_text('old\n')
    (rt / 'server.json').write_text(json.dumps({'pid': 300}))
    return SimpleNamespace(tmp=tmp_path, old=old, new=new, home=home, run=run, sub=sub, rt=rt, cfg=cfg)


class FakeFleet:
    def __init__(self, w, states=None):
        self.cfg = dict(w.cfg)
        self.workers = list(w.cfg['workers'])
        self.states = states or {}
        self.logs, self.queued = [], []
        self.service, self.v2 = True, None
        self.mgr = SimpleNamespace(runtime_root=w.rt, server_json=w.rt / 'server.json')

    def agent(self, name):
        return {'state': self.states.get(name, 'idle')}

    def log(self, event, **fields):
        self.logs.append((event, fields))

    def queue(self, name, text, **kw):
        self.queued.append((name, text))

    def running(self):
        return True

    def events(self, name):
        return [f for e, f in self.logs if e == name]


class FakeOps(release_drift.Ops):
    """The whole machine as seen by release_drift: a clock, a process table, two Redis instances, start.sh."""

    def __init__(self, w, knobs=None, help_rc=0, start_ok=('new', 'old'), problem=None):
        self.w, self.t, self.knobs_, self.problem = w, T0, dict(KNOBS, **(knobs or {})), problem
        self.help_rc, self.start_ok = help_rc, start_ok
        self.events, self.notices, self.spawned, self.started, self.busy = [], [], [], [], {}
        self.spawn_envs, self.start_envs = [], []
        self.jobs = {'J1': {'state': 'running'}, 'J2': {'state': 'queued'}}
        self.redis_up = {w.cfg['redis_socket'], str(w.tmp / 'sub.sock')}
        self.procs = {}
        self.boot(w.old)

    def boot(self, rel_dir):
        w = self.w
        self.procs = {100: daemon_args(rel_dir, w.run / 'config.json'),
                      101: 'caffeinate -i ' + daemon_args(rel_dir, w.run / 'config.json'),
                      200: daemon_args(rel_dir, w.sub / 'config.json'),
                      300: '%s/bin/fleetmux server --runtime %s --session fleetmux --daemonize' % (rel_dir, w.rt)}
        for d in (w.run, w.sub):
            (d / 'release').write_text(rel_dir.name + '\n')
        self.current = rel_dir.name
        self.redis_up = {w.cfg['redis_socket'], str(w.tmp / 'sub.sock')}

    # clock
    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s

    # releases
    def installed_dir(self):
        return self.w.new

    def own(self):
        return self.current

    def own_dir(self):
        return self.w.old

    def knobs(self):
        return dict(self.knobs_), self.problem

    # processes
    def ps(self):
        return sorted(self.procs.items())

    def cmdline(self, pid):
        return self.procs[pid].split() if pid in self.procs else None

    def alive(self, pid):
        return pid in self.procs

    def switcher_alive(self, pid):
        return pid in self.procs

    def daemon_running(self, run):
        return any(' daemon %s/config.json' % run in a and not a.startswith('caffeinate') for a in self.procs.values())

    def kill(self, pid, sig):
        jobs = self.w.run / 'jobs.json'
        self.events.append(('kill', pid, json.loads(jobs.read_text()) if jobs.exists() else None))
        self.procs.pop(pid, None)
        if pid == 100:
            self.procs.pop(101, None)  # caffeinate ends with its child

    # redis
    def redis_get(self, sock, key):
        assert key == 'fx:jobs'
        if sock not in self.redis_up:
            return None
        return '' if self.jobs is None else json.dumps(self.jobs)  # '' = the key is absent

    def redis_shutdown(self, sock):
        self.events.append(('redis_shutdown', sock))
        self.redis_up.discard(sock)

    def sub_busy(self, cfg):
        return list(self.busy.get(cfg['run'], []))

    # commands
    def run(self, argv, env=None, timeout=None):
        assert argv[-1] == '--help', argv
        return self.help_rc, 'usage'

    def start(self, argv, env, log_path, timeout):
        assert Path(log_path) == self.w.run / release_drift.LOG
        self.start_envs.append(dict(env))
        assert argv[0] == str(self.w.home / 'start.sh') and argv[1:] == ['--resume', str(self.w.run)]
        target = Path(env['FLEETMUX_REL'])
        self.started.append(target.name)
        self.events.append(('start', target.name))
        if target.name in self.start_ok:
            self.boot(target)
        return 0

    def spawn(self, argv, env, log_path):
        self.spawned.append(argv)
        self.spawn_envs.append(dict(env))
        self.procs[4242] = ' '.join(argv)  # the switcher runs until switch() is called
        return 4242

    def notify_user(self, f, text):
        self.notices.append(text)

    # the checks after the start (no applet, no window, the supervisor idle)
    def redis_answers(self, sock):
        return sock in self.redis_up

    def redis_hget(self, sock, key, field):
        return 'idle' if sock in self.redis_up and (key, field) == ('fx:agent:supervisor', 'state') else None

    def applets(self, run):
        return {}

    def stop_applet(self, run, name):
        raise AssertionError('no applet in this world: ' + name)

    def windows(self):
        return None

    def descendants(self, pid):
        return {pid}

    def run_input(self, argv, text, env, timeout):
        raise AssertionError('no request in this world: %r' % argv)


def plan_of(ops):
    return json.loads(Path(ops.spawned[-1][-1]).read_text())


def tick_at(f, ops, t):
    ops.t = t
    release_drift.tick(f, ops=ops)


# ---------------------------------------------------------------- the shipped block
def test_the_shipped_block_holds_every_parameter_and_the_switch_is_on():
    shipped = fleet_config.block('release_drift', path=fleet_config.SHIPPED)
    assert shipped == release_drift.DEFAULTS
    assert shipped['enabled'] is True
    assert (shipped['check_interval_s'], shipped['grace_s'], shipped['blackout']) == (600, 900, [])
    assert 'release_drift' in fleet_config.USER_BLOCKS  # the operator sets blackout windows in the private file


def test_knobs_keep_the_default_for_a_bad_value(monkeypatch):
    monkeypatch.setattr(fleet_config, 'block', lambda name, path=None: {'grace_s': 'soon', 'blackout': ['25:00-26:00'],
                                                                        'timezone': 'Nowhere/Else', 'idle_wait_s': 30})
    knobs, problem = release_drift.knobs()
    assert knobs['grace_s'] == 900 and knobs['blackout'] == [] and knobs['timezone'] == ''
    assert knobs['idle_wait_s'] == 30
    assert 'grace_s' in problem and 'blackout' in problem and 'timezone' in problem


def test_blackout_windows_in_a_time_zone():
    noon_utc = 12 * 3600.0  # 1970-01-01 12:00 UTC = 07:00 in New York
    assert release_drift.in_blackout(noon_utc, ['06:55-14:35'], 'America/New_York')
    assert not release_drift.in_blackout(noon_utc, ['08:55-14:35'], 'America/New_York')
    assert release_drift.in_blackout(noon_utc, ['22:00-08:00'], 'America/New_York')  # across midnight
    assert not release_drift.in_blackout(noon_utc, [], 'UTC')


def test_compare_sees_every_process_of_the_run(world):
    f, ops = FakeFleet(world), FakeOps(world)
    rows = release_drift.processes(f, ops)
    assert [(r['kind'], r['release']) for r in rows] == [
        ('root daemon', 'old'), ('sub-fleet daemon', 'old'), ('fleetmux server', 'old')]
    applet = world.run / 'applets' / 'relay'
    applet.mkdir(parents=True)
    (applet / 'pid').write_text('400')
    ops.procs[400] = '%s/.venv/bin/python -m fleetmux.relay_applet' % world.new
    rows = release_drift.processes(f, ops)
    assert ('applet', 'relay', 'new') in [(r['kind'], r['name'], r['release']) for r in rows]


# ---------------------------------------------------------------- 1: drift past the grace, idle, no blackout
def test_1_drift_past_grace_switches_once_snapshot_first_root_first(world):
    f, ops = FakeFleet(world), FakeOps(world)
    tick_at(f, ops, T0)
    assert not ops.spawned  # inside the grace
    tick_at(f, ops, LATER)
    assert len(ops.spawned) == 1
    argv = ops.spawned[0]
    assert argv[1:4] == ['-m', 'fleetmux.release_drift', 'switch']
    tick_at(f, ops, LATER + 5)
    assert len(ops.spawned) == 1  # one switch, not one per tick
    # the notice before the switch: root supervisor + user channel, from / to / processes / jobs
    assert len(ops.notices) == 1 and len(f.queued) == 1 and f.queued[0][0] == 'supervisor'
    before = ops.notices[0]
    assert 'from release old to release new' in before
    assert 'root daemon' in before and 'sub-fleet daemon M1' in before and 'fleetmux server' in before
    plan = plan_of(ops)
    assert plan['new']['dir'] == str(world.new) and plan['old']['release'] == 'old'

    outcome = release_drift.switch(plan, ops)
    kills = [e for e in ops.events if e[0] == 'kill']
    assert kills[0][1] == 100  # the root daemon first
    assert kills[0][2] == ops.jobs  # jobs.json already holds the fx:jobs snapshot when the first daemon is stopped
    assert [k[1] for k in kills[:2]] == [100, 200]  # then the sub-fleet
    assert ops.events.index(('start', 'new')) > ops.events.index(kills[-1])
    index = world.home / 'job-runs' / 'fleet-1.json'
    assert json.loads(index.read_text())['run'] == str(world.run)
    assert outcome['ok'] is True and outcome['to'] == 'new' and outcome['jobs']['missing'] == []
    assert ops.started == ['new']

    # the next daemon (the new release) reports once through the same route
    f2, ops2 = FakeFleet(world), FakeOps(world)
    ops2.boot(world.new)
    tick_at(f2, ops2, ops.t + 1)
    tick_at(f2, ops2, ops.t + 2)
    assert len(ops2.notices) == 1 and len(f2.queued) == 1
    after = ops2.notices[0]
    assert 'switched from release old to release new' in after and 'jobs back: 2 of 2' in after


# ---------------------------------------------------------------- 2: no switch
def test_2_no_switch_inside_the_grace(world):
    f, ops = FakeFleet(world), FakeOps(world)
    tick_at(f, ops, T0)
    tick_at(f, ops, T0 + KNOBS['grace_s'] - 1)
    assert not ops.spawned and not ops.notices


def test_2_no_switch_inside_a_blackout_window(world):
    f, ops = FakeFleet(world), FakeOps(world, knobs={'blackout': ['00:00-23:59'], 'timezone': 'UTC'})
    tick_at(f, ops, T0)
    tick_at(f, ops, LATER)
    assert not ops.spawned and f.events('release_drift.blackout')


def test_2_no_switch_while_a_worker_is_in_a_turn_then_the_next_round(world):
    f, ops = FakeFleet(world, states={'worker-2': 'busy'}), FakeOps(world)
    tick_at(f, ops, T0)
    tick_at(f, ops, LATER)
    tick_at(f, ops, LATER + KNOBS['idle_wait_s'] - 1)
    assert not ops.spawned  # waits up to idle_wait_s
    tick_at(f, ops, LATER + KNOBS['idle_wait_s'] + 1)
    assert not ops.spawned and f.events('release_drift.deferred')  # never a hard switch
    f.states = {}
    tick_at(f, ops, LATER + KNOBS['idle_wait_s'] + 2)
    assert not ops.spawned  # deferred to the next round, not the next tick
    tick_at(f, ops, LATER + KNOBS['idle_wait_s'] + KNOBS['check_interval_s'] + 2)
    assert len(ops.spawned) == 1


def test_2_a_busy_worker_of_a_sub_fleet_counts(world):
    f, ops = FakeFleet(world), FakeOps(world, knobs={'idle_wait_s': 60})
    ops.busy[str(world.sub)] = ['worker-1']
    tick_at(f, ops, T0)
    tick_at(f, ops, LATER)
    tick_at(f, ops, LATER + 61)
    assert not ops.spawned and f.events('release_drift.deferred')


def test_2_no_switch_when_the_installed_release_fails_its_self_check_reported_once(world):
    f, ops = FakeFleet(world), FakeOps(world, help_rc=1)
    tick_at(f, ops, T0)
    tick_at(f, ops, LATER)
    tick_at(f, ops, LATER + KNOBS['check_interval_s'] + 1)
    assert not ops.spawned
    assert len(ops.notices) == 1 and 'self-check' in ops.notices[0]
    (world.new / 'bin' / 'fleetmux').unlink()  # an incomplete release directory fails as well
    assert 'bin/fleetmux' in release_drift.selfcheck(world.new, 'fleetmux', FakeOps(world))


def test_2_the_master_switch_off_does_nothing(world):
    f, ops = FakeFleet(world), FakeOps(world, knobs={'enabled': False})
    tick_at(f, ops, T0)
    tick_at(f, ops, LATER)
    assert not ops.spawned and not ops.notices


def test_2_a_sub_fleet_daemon_never_switches(world):
    f, ops = FakeFleet(world), FakeOps(world)
    f.cfg['parent_config'] = str(world.run / 'config.json')
    tick_at(f, ops, T0)
    tick_at(f, ops, LATER)
    assert not ops.spawned


def test_2_an_unknown_release_is_no_drift(world):
    f, ops = FakeFleet(world), FakeOps(world)
    ops.own = lambda: None
    for d in (world.run, world.sub):
        (d / 'release').unlink()
    ops.procs[300] = 'fleetmux server --runtime %s' % world.rt
    tick_at(f, ops, T0)
    tick_at(f, ops, LATER)
    assert not ops.spawned


# ---------------------------------------------------------------- 3: failed check -> back, then stop after 2
def test_3_a_failed_check_goes_back_to_the_old_release_and_reports(world):
    f, ops = FakeFleet(world), FakeOps(world, start_ok=('old',))  # the new release never comes up
    tick_at(f, ops, T0)
    tick_at(f, ops, LATER)
    outcome = release_drift.switch(plan_of(ops), ops)
    assert outcome['ok'] is False and outcome['rolled_back'] is True
    assert ops.started == ['new', 'old']  # back on the original release, same run
    assert 'root daemon' in outcome['reason']
    kills = [e for e in ops.events if e[0] == 'kill']
    assert kills[0][2] is not None  # the snapshot again before the first stop
    state = json.loads((world.run / release_drift.STATE).read_text())
    assert state['failures'] == {'new': 1}
    f2, ops2 = FakeFleet(world), FakeOps(world)  # the old release's daemon again
    tick_at(f2, ops2, ops.t + 1)
    assert len(ops2.notices) == 1 and not ops2.spawned  # a failure starts a new grace
    text = ops2.notices[0]
    assert 'failed' in text and 'back on release old' in text and outcome['reason'] in text


def test_3_a_release_on_the_wrong_version_after_the_start_fails_the_check(world):
    f, ops = FakeFleet(world), FakeOps(world)
    tick_at(f, ops, T0)
    tick_at(f, ops, LATER)
    real_boot = ops.boot

    def half(rel_dir):  # the root comes up on the new release, the sub-fleet stays on the old one
        real_boot(rel_dir)
        if rel_dir.name == 'new':
            ops.procs[200] = daemon_args(world.old, world.sub / 'config.json')
            (world.sub / 'release').write_text('old\n')
    ops.boot = half
    outcome = release_drift.switch(plan_of(ops), ops)
    assert outcome['ok'] is False and 'sub-fleet daemon M1' in outcome['reason']
    assert ops.started == ['new', 'old']


def test_3_two_failures_of_one_release_stop_the_automatic_switch(world):
    t = T0
    for _ in range(2):
        f, ops = FakeFleet(world), FakeOps(world, start_ok=('old',))
        tick_at(f, ops, t)
        tick_at(f, ops, t + KNOBS['grace_s'] + 1)
        assert len(ops.spawned) == 1
        release_drift.switch(plan_of(ops), ops)
        t = ops.t + 1
        tick_at(FakeFleet(world), FakeOps(world), t)  # the old release's daemon reports the failure
    assert json.loads((world.run / release_drift.STATE).read_text())['failures'] == {'new': 2}
    f, ops = FakeFleet(world), FakeOps(world)
    tick_at(f, ops, t)
    tick_at(f, ops, t + KNOBS['grace_s'] + 1)
    tick_at(f, ops, t + KNOBS['grace_s'] + KNOBS['check_interval_s'] + 2)
    assert not ops.spawned
    assert len(ops.notices) == 1 and 'stopped' in ops.notices[0] and '2' in ops.notices[0]


@pytest.mark.parametrize('refusal', ['two roots', 'no fx:jobs'])
def test_3_a_refusal_that_stopped_nothing_is_not_a_failed_switch(world, refusal):
    """No single root daemon, or fx:jobs that cannot be read: the switch stops nothing and the run goes on. That says
    nothing about the release, so it is not counted towards max_failures; a switch that stopped the root is."""
    for n in range(3):  # more than max_failures refusals in a row
        f, ops = FakeFleet(world), FakeOps(world)
        if refusal == 'two roots':
            ops.procs[102] = daemon_args(world.old, world.run / 'config.json')
        else:
            ops.redis_up.discard(world.cfg['redis_socket'])
        tick_at(f, ops, T0 + n * 10 * KNOBS['grace_s'])
        tick_at(f, ops, T0 + n * 10 * KNOBS['grace_s'] + KNOBS['grace_s'] + 1)
        assert len(ops.spawned) == 1  # never stopped by the refusals before
        outcome = release_drift.switch(plan_of(ops), ops)
        assert outcome['ok'] is False and outcome['rolled_back'] is True
        assert outcome['step'] == ('preflight' if refusal == 'two roots' else 'backup')
        assert not [e for e in ops.events if e[0] == 'kill'] and not ops.started
        assert outcome['failures'] == 0
        assert not release_drift.load_state(world.run)['failures'].get('new')
        assert 'not counted' in release_drift.outcome_text(outcome)
    ops = FakeOps(world, start_ok=('old',))  # a real failure after the stop counts as before
    f = FakeFleet(world)
    tick_at(f, ops, T0 + 50 * KNOBS['grace_s'])
    tick_at(f, ops, T0 + 50 * KNOBS['grace_s'] + KNOBS['grace_s'] + 1)
    outcome = release_drift.switch(plan_of(ops), ops)
    assert outcome['ok'] is False and outcome['failures'] == 1


def test_the_root_daemon_tick_runs_the_compare():
    import inspect
    from fleetmux import fleet
    assert "('release_drift', lambda: release_drift.tick(self))" in inspect.getsource(fleet.Fleet.tick)


# ---------------------------------------------------------------- edge cases of the switch
def test_1_a_run_without_fx_jobs_switches_with_an_empty_snapshot(world):
    f, ops = FakeFleet(world), FakeOps(world)
    ops.jobs = None  # the key was never set: not the same as a Redis that cannot be read
    tick_at(f, ops, T0)
    tick_at(f, ops, LATER)
    outcome = release_drift.switch(plan_of(ops), ops)
    assert outcome['ok'] is True and json.loads((world.run / 'jobs.json').read_text()) == {}


def test_3_a_root_daemon_that_does_not_exit_stops_nothing_else(world):
    f, ops = FakeFleet(world), FakeOps(world, knobs={'stop_wait_s': 30})
    tick_at(f, ops, T0)
    tick_at(f, ops, LATER)
    real_kill = ops.kill

    def stubborn(pid, sig):
        if pid == 100:
            ops.events.append(('kill', pid, None))
            return
        real_kill(pid, sig)
    ops.kill = stubborn
    outcome = release_drift.switch(plan_of(ops), ops)
    assert [e[1] for e in ops.events if e[0] == 'kill'] == [100]  # no sub-fleet is signalled while the root lives
    assert not ops.started and outcome['ok'] is False and 'root daemon' in outcome['reason']
    assert outcome['rolled_back'] is True  # nothing else was stopped: the run is still on the old release


def test_the_tick_skips_while_a_switch_holds_the_run_lock(world):
    """A manual `fleetmux upgrade` (or another switch) holds <run>/release-drift.lock; the daemon's
    tick starts no second switch meanwhile, and goes ahead once the lock is free."""
    import fcntl
    f, ops = FakeFleet(world), FakeOps(world)
    fh = open(world.run / release_drift.LOCK, 'a+')
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        tick_at(f, ops, T0)
        tick_at(f, ops, LATER)
        tick_at(f, ops, LATER + KNOBS['check_interval_s'] + 1)
    finally:
        fh.close()
    assert not ops.spawned
    tick_at(f, ops, LATER + 2 * KNOBS['check_interval_s'] + 2)
    assert len(ops.spawned) == 1


def test_2_a_bad_blackout_or_timezone_value_never_switches(world):
    f, ops = FakeFleet(world), FakeOps(world, problem='fleet.yaml#release_drift: bad value kept at its default: '
                                                      'blackout')
    tick_at(f, ops, T0)
    tick_at(f, ops, LATER)
    assert not ops.spawned and f.events('release_drift.config')


# ---------------------------------------------------------------- the real Ops (no process, no Redis)
class StubClient:
    def __init__(self, values=None, states=None):
        self.values, self.states = values or {}, states or {}

    def get(self, key):
        return self.values.get(key)

    def scan_iter(self, pattern):
        return list(self.states)

    def hget(self, key, field):
        return self.states.get(key)

    def close(self):
        pass


def bare_ops():
    return release_drift.Ops.__new__(release_drift.Ops)


def test_real_ops_redis_get_tells_an_absent_key_from_a_redis_that_is_down(monkeypatch):
    monkeypatch.setattr(release_drift.Ops, '_redis', lambda self, sock: StubClient())
    assert ORIG['redis_get'](bare_ops(), '/x.sock', 'fx:jobs') == ''

    def down(self, sock):
        raise OSError('no socket')
    monkeypatch.setattr(release_drift.Ops, '_redis', down)
    assert ORIG['redis_get'](bare_ops(), '/x.sock', 'fx:jobs') is None


def test_real_ops_sub_busy_counts_a_sub_fleet_supervisor_in_a_turn(monkeypatch):
    client = StubClient(states={'fx:agent:supervisor': 'busy', 'fx:agent:worker-1': 'idle',
                                'fx:agent:advisor': 'busy', 'fx:agent:worker-2': 'handoff'})
    monkeypatch.setattr(release_drift.Ops, '_redis', lambda self, sock: client)
    assert sorted(ORIG['sub_busy'](bare_ops(), {'redis_socket': '/x.sock'})) == ['supervisor', 'worker-2']


def test_real_ops_alive_never_reaps_a_child_only_the_switcher_check_does(monkeypatch):
    reaped = []
    monkeypatch.setattr(os, 'waitpid', lambda pid, flags: reaped.append(pid) or (0, 0))
    monkeypatch.setattr(os, 'kill', lambda pid, sig: None)
    monkeypatch.setattr(release_drift.Ops, 'alive', ORIG['alive'])
    assert ORIG['alive'](bare_ops(), 4321) is True and reaped == []  # an applet: its Popen keeps its exit code
    assert ORIG['switcher_alive'](bare_ops(), 4321) is True and reaped == [4321]


def test_real_ops_start_writes_its_output_to_the_log_not_a_pipe(monkeypatch, tmp_path):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(kw, argv=argv)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(subprocess, 'run', fake_run)
    log = tmp_path / 'release-drift.log'
    assert ORIG['start'](bare_ops(), ['/x/start.sh', '--resume', '/r'], {'FLEETMUX_REL': '/n'}, str(log), 600) == 0
    assert 'capture_output' not in seen and seen['stdout'] not in (None, subprocess.PIPE)
    assert seen['stdin'] == subprocess.DEVNULL and seen['env'] == {'FLEETMUX_REL': '/n'}


# ---------------------------------------------------------------- round 2: P0-01
def _inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def test_p0_start_sh_gets_no_pythonpath_into_the_old_or_the_new_release(world, monkeypatch):
    """The switch process imports its own release through PYTHONPATH; start.sh runs the new (or, going back, the
    old) release's interpreter, which must import that release's own fleetmux, so neither start may inherit a
    PYTHONPATH entry inside either release directory. Other entries of the daemon's PYTHONPATH stay."""
    monkeypatch.setattr(releases, 'ROOT', world.old)  # the daemon runs the old release
    monkeypatch.setenv('PYTHONPATH', os.pathsep.join([str(world.new / 'lib'), '/opt/site-extra']))
    f, ops = FakeFleet(world), FakeOps(world, start_ok=('old',))  # the new release fails: both starts run
    tick_at(f, ops, T0)
    tick_at(f, ops, LATER)
    switcher_env = ops.spawn_envs[-1]
    assert str(world.old) in switcher_env['PYTHONPATH'].split(os.pathsep)  # the switch process imports its release
    monkeypatch.setenv('PYTHONPATH', switcher_env['PYTHONPATH'])  # the environment the switch process runs in
    release_drift.switch(plan_of(ops), ops)
    assert [e['FLEETMUX_REL'] for e in ops.start_envs] == [str(world.new), str(world.old)]
    for env in ops.start_envs:
        parts = [x for x in env.get('PYTHONPATH', '').split(os.pathsep) if x]
        assert not [x for x in parts if _inside(x, world.old) or _inside(x, world.new)], parts
        assert '/opt/site-extra' in parts
