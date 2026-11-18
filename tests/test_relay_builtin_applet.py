"""The relay as a built-in applet of the root fleet (relay design: architecture, "where the relay runs").

With ``relay.enabled: true`` (default false) the daemon of a root transport v2 service fleet declares the applet
``applet-relay-1`` itself, without a roster row, running ``python -m fleetmux.relay run --node <name>``, and starts,
watches, restarts and stops it like any applet: no cron, no launchd, no watcher of its own. The switch is read at
daemon start (``Fleet.builtin_applets`` from ``Fleet.run``) and at every roster reload.
Fake processes, a fake applet clock and recorded signals (tests/test_applet_host.py); the relay block comes from a
monkeypatched accessor (``fleet_config.relay``) and the machine name from the temporary inventory.
"""
import inspect
import signal
import time
import types
from pathlib import Path

import pytest

from fleetmux import applet_cli, applet_host, fleet, fleet_config, fleet_superpowers as fs, relay_applet
from test_applet_host import FakeProc, audits, write_roster, WORKERS
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import fx  # noqa: F401  (a root v2 service fleet)
from test_v2_codex_claude import mixed  # noqa: F401  (a v2 task fleet)

NAME = relay_applet.NAME
MACHINE = 'laptop'
COMMAND = ['-m', 'fleetmux.relay', 'run', '--node', MACHINE]  # after {python}: the relay entry


def switch(monkeypatch, enabled, machine=MACHINE):
    """relay.enabled as the loader reports it; this machine's own inventory entry names it."""
    if machine and fs.self_name() != machine:
        fs.write_self({'source': 'self'}, name=machine)
    monkeypatch.setattr(fleet_config, 'relay', lambda **kw: types.SimpleNamespace(enabled=enabled))


def fake_processes(f, monkeypatch):
    clock = [1000.0]
    f.applet_clock = lambda: clock[0]
    procs, signals, spawned = [], [], []
    monkeypatch.setattr(applet_host, '_signal', lambda pid, sig, group=False: signals.append((pid, sig, group)))
    monkeypatch.setattr(applet_host, 'clear_stale', lambda *a, **k: None)

    def spawn(cfg, spec, token):  # the process start is a stub: nothing of the relay entry runs
        applet_host.run_dir(cfg, spec['name']).mkdir(parents=True, exist_ok=True)
        spawned.append(applet_host.argv(cfg, spec))
        procs.append(FakeProc())
        return procs[-1]
    f.applet_spawn = spawn
    f.test_clock, f.test_procs, f.test_signals, f.test_spawned = clock, procs, signals, spawned
    return f


@pytest.fixture
def root(fx, monkeypatch):  # noqa: F811
    return fake_processes(fx, monkeypatch)


def started(f):
    """Daemon start: Fleet.run reads the switch (builtin_applets), then the loop ticks."""
    f.builtin_applets()
    f.tick()


def state(f):
    return f.r.hgetall(applet_host.key(NAME))


def beat(f):
    f.r.hset(applet_host.key(NAME), mapping={'pid': '4242', 'hb_at': repr(f.test_clock[0])})


# -- the switch -------------------------------------------------------------------------------------------

def test_enabled_root_fleet_starts_the_relay_entry_at_daemon_start(root, monkeypatch):
    switch(monkeypatch, True)
    started(root)
    assert [s['name'] for s in root.applet_specs] == [NAME]
    argv, = root.test_spawned
    assert argv[1:] == COMMAND
    assert state(root)['state'] == 'starting'


def test_the_daemon_reads_the_switch_when_it_starts_or_resumes():
    src = inspect.getsource(fleet.Fleet.run)
    assert src.index('self.builtin_applets()') < src.index('return self.v2.run()')


def test_off_by_default(root):
    started(root)  # the real loader: no relay block, or one with enabled: false
    assert not root.applet_specs and not root.test_spawned and not state(root)


def test_turned_off_explicitly(root, monkeypatch):
    switch(monkeypatch, False)
    started(root)
    assert not root.test_spawned


def _raise(exc):
    def accessor(**kw):
        raise exc
    return accessor


@pytest.mark.parametrize('accessor', [
    _raise(KeyError('relay')),                               # no relay block registered in this release
    _raise(ValueError('bad')),                               # a broken configuration file (relay ConfigError)
    _raise(OSError('unreadable')),                           # the file cannot be read
    _raise(RuntimeError('anything else')),                   # any other error still reads as off
    lambda **kw: types.SimpleNamespace(enabled='yes'),       # not a boolean
    lambda **kw: types.SimpleNamespace(),                    # attribute absent
])
def test_the_accessor_reads_anything_but_true_as_off(monkeypatch, accessor):
    monkeypatch.setattr(fleet_config, 'relay', accessor)
    assert relay_applet.enabled() is False


def test_the_accessor_goes_through_the_one_relay_accessor(monkeypatch):
    monkeypatch.setattr(fleet_config, 'relay', lambda **kw: types.SimpleNamespace(enabled=True))
    assert relay_applet.enabled() is True


def test_the_private_file_switches_it_on(tmp_path, monkeypatch):
    private = tmp_path / 'fleet.yaml'
    private.write_text('relay:\n  enabled: true\n')
    monkeypatch.setenv('FLEETMUX_CONFIG', str(private))
    assert relay_applet.enabled() is True


def test_a_private_block_that_fails_validation_stays_off(tmp_path, monkeypatch):
    private = tmp_path / 'fleet.yaml'
    private.write_text('relay:\n  enabled: true\n  hot:\n    beat_s: -1\n')
    monkeypatch.setenv('FLEETMUX_CONFIG', str(private))
    assert relay_applet.enabled() is False


def test_the_shipped_default_is_off(tmp_path, monkeypatch):
    monkeypatch.setenv('FLEETMUX_CONFIG', str(tmp_path / 'absent.yaml'))
    assert relay_applet.enabled() is False


def test_only_a_root_v2_service_fleet_hosts_it(tmp_path, monkeypatch):
    switch(monkeypatch, True)
    base = dict(run=str(tmp_path), mode='service', transport_version=2)
    workers = ['worker-1']
    assert [e['name'] for e in relay_applet.specs(base, workers)] == [NAME]
    assert relay_applet.specs(dict(base, parent_config=str(tmp_path / 'p.json')), workers) == []  # a sub-fleet
    assert relay_applet.specs(dict(base, mode='task'), workers) == []
    assert relay_applet.specs(dict(base, transport_version=1), workers) == []


def test_a_task_fleet_never_starts_it(mixed, monkeypatch):  # noqa: F811
    switch(monkeypatch, True)
    fake_processes(mixed, monkeypatch)
    started(mixed)
    assert not mixed.test_spawned


def test_the_spec_has_every_applet_default(tmp_path, monkeypatch):
    switch(monkeypatch, True)
    spec, = relay_applet.specs(dict(run=str(tmp_path), mode='service', transport_version=2), ['worker-1'])
    assert spec['command'] == ['{python}'] + COMMAND
    assert spec['autostart'] is True and spec['stale_after_s'] > spec['heartbeat_s']
    assert {'lease', 'review', 'notify', 'waits'} <= set(spec)


def test_a_switch_change_takes_effect_at_the_next_roster_reload(root, monkeypatch):
    switch(monkeypatch, True)
    started(root)
    pid = root.test_procs[-1].pid
    switch(monkeypatch, False)
    root.tick()
    assert not root.test_signals, 'no reload yet: the switch is not read on every tick'
    write_roster(root, WORKERS)  # roster reload
    for _ in range(3):
        root.tick()
    assert any(p == pid and sig == signal.SIGTERM for p, sig, _ in root.test_signals)
    root.test_procs[-1].code = 0
    root.tick()
    assert not state(root).get('state') and audits(root, 'applet.detached')
    switch(monkeypatch, True)
    write_roster(root, WORKERS + '# reloaded\n')
    root.tick()
    assert len(root.test_spawned) == 2


# -- the daemon keeps it running ---------------------------------------------------------------------------

def test_a_crash_is_restarted_by_the_daemon(root, monkeypatch):
    switch(monkeypatch, True)
    started(root)
    root.test_procs[-1].code = 1
    root.tick()
    assert len(root.test_spawned) == 2 and audits(root, 'applet.restarted')


def test_sleeps_do_not_stall_it_into_a_crashloop(root, monkeypatch, fake_awake):
    """R11: a laptop sleeps and wakes several times within the crash window; the relay applet stays up."""
    switch(monkeypatch, True)
    root.applet_clock, root.machine = None, fake_awake  # the awake clock of a fake machine (fleetmux/awake.py)

    def beat():
        root.r.hset(applet_host.key(NAME), mapping={'pid': '4242', 'hb_at': repr(fake_awake.wall())})
    started(root)
    beat()
    root.tick()
    assert state(root)['state'] == 'up'
    for _ in range(3):
        fake_awake.sleep(900)
        root.v2.last_tick = time.time() - 900  # the daemon slept as long as the machine
        root.tick()
        assert state(root)['state'] == 'up' and not audits(root, 'applet.stalled')
        fake_awake.run(5)
        beat()
        root.tick()
    assert len(root.test_spawned) == 1 and not audits(root, 'applet.crashloop')


def test_it_needs_no_cron_or_launchd():
    source = Path(relay_applet.__file__).read_text()
    assert 'launchctl' not in source and 'crontab' not in source and 'systemctl' not in source


def test_a_fleet_without_workers_is_told_once(root, monkeypatch):
    switch(monkeypatch, True)
    root.workers = []
    started(root)
    root.builtin_applets()
    assert not root.test_spawned
    assert len(audits(root, 'applet.builtin_error')) == 1


# -- names ------------------------------------------------------------------------------------------------

def test_a_roster_row_may_not_take_the_name(root, monkeypatch):
    switch(monkeypatch, True)
    started(root)
    write_roster(root, WORKERS + 'applets:\n  - {name: ' + NAME + ', command: [x], cwd: /}\n')
    root.tick()
    assert audits(root, 'roster.rejected')
    spec, = root.applet_specs
    assert spec['command'] == ['{python}'] + COMMAND


def test_the_applet_command_line_knows_it(root, monkeypatch):
    switch(monkeypatch, True)
    assert NAME in applet_cli.declared(root.cfg)
    switch(monkeypatch, False)
    assert NAME not in applet_cli.declared(root.cfg)


def test_without_a_machine_name_it_is_not_started_and_said_once(root, monkeypatch):
    switch(monkeypatch, True, machine=None)  # no self entry: `superpowers refresh --name` first
    started(root)
    root.builtin_applets()
    assert not root.test_spawned
    err, = audits(root, 'applet.builtin_error')
    assert 'superpowers refresh --name' in err['error']


def test_the_node_is_the_inventory_self_name(tmp_path, monkeypatch):
    switch(monkeypatch, True, machine='desktop')
    spec, = relay_applet.specs(dict(run=str(tmp_path), mode='service', transport_version=2), ['worker-1'])
    assert spec['command'][-2:] == ['--node', 'desktop']


# -- the heartbeat comes from the relay main loop, not from a free-running thread -----------------------------

class Redis:
    def __init__(self):
        self.beats = []

    def hset(self, key, mapping):
        self.beats.append((key, dict(mapping)))


def sdk_applet(tmp_path):
    from fleetmux.applet import Applet
    return Applet(name=NAME, token='t', redis=Redis(), config_path=tmp_path / 'config.json', state_dir=tmp_path,
                  run_dir=tmp_path, heartbeat_s=10)


def test_the_loop_beats_once_per_heartbeat_period_and_no_thread_beats(tmp_path):
    import threading
    app = sdk_applet(tmp_path)
    clock = [100.0]
    before = threading.active_count()
    beat = relay_applet.LoopHeartbeat(app, clock=lambda: clock[0])
    assert beat.tick() is True  # the first loop pass beats
    clock[0] += 4
    assert beat.tick() is False  # within heartbeat_s: no write
    clock[0] += 6
    assert beat.tick() is True
    assert [k for k, _ in app.redis.beats] == [applet_host.key(NAME)] * 2
    assert all('hb_at' in m for _, m in app.redis.beats)
    assert threading.active_count() == before and app._thread is None


def test_a_hung_loop_stops_beating(tmp_path):
    """The point of the loop beat: while the loop does not come round, nothing beats, so the host sees a stall."""
    app = sdk_applet(tmp_path)
    clock = [0.0]
    beat = relay_applet.LoopHeartbeat(app, clock=lambda: clock[0])
    beat.tick()
    clock[0] += 3600  # the loop hung for an hour: no call, no beat
    assert len(app.redis.beats) == 1


def test_loop_heartbeat_uses_the_sdk_without_its_thread(monkeypatch, tmp_path):
    from fleetmux import applet as sdk
    calls = []
    monkeypatch.setattr(sdk.Applet, 'from_env', classmethod(
        lambda cls, env=None, start=True: calls.append(start) or sdk_applet(tmp_path)))
    beat = relay_applet.loop_heartbeat({'FLEETMUX_APPLET_NAME': NAME})
    assert calls == [False] and isinstance(beat, relay_applet.LoopHeartbeat)
    assert len(beat.applet.redis.beats) == 1  # the first beat at once, before the first loop pass


def test_outside_the_applet_host_there_is_no_heartbeat():
    assert relay_applet.loop_heartbeat({}) is None  # `fleetmux relay run` typed by hand
