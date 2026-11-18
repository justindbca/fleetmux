"""The production backend: `fleetmux relay` commands on the git state layer (R18, R21, R24), on a local bare hub.

The machine's name comes from a temporary inventory ($FLEETMUX_SUPERPOWERS); the relay state stays in a temporary
dir; no fleet is started (one-shot commands never start one)."""
import io
import json

import pytest

from fleetmux.relay import backend, cli
from relay_kit import git, isolated_git, machine_info, machines, remote_refs, settings  # noqa: F401
from test_relay_flow_git import JOB, project_files


@pytest.fixture
def laptop(tmp_path, monkeypatch):
    hub, base, ms = machines(tmp_path, names=('laptop',), job=JOB, files=project_files())
    inv = tmp_path / 'inv' / 'fleet-superpowers.yaml'
    inv.parent.mkdir()
    inv.write_text('machines:\n  laptop:\n    source: self\n    always_on: false\n'
                   '    toolchains: {python: "3.12", rust: "1.83"}\n')
    monkeypatch.setenv('FLEETMUX_SUPERPOWERS', str(inv))
    for var in ('FLEETMUX_APPLET_NAME', 'FLEETMUX_APPLET_CONFIG'):
        monkeypatch.delenv(var, raising=False)
    m = ms['laptop']
    b = backend.GitBackend(state_dir=m.state_dir, settings=settings(), measure=lambda path, toolchains, always_on:
                           machine_info(10, 32, 200, always_on=always_on, toolchains=toolchains))
    return {'hub': hub, 'base': base, 'm': m, 'b': b, 'inv': inv}


def run(env, *argv):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(list(argv), backend=env['b'], state_dir=env['m'].state_dir, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_join_init_status_take_park_on_the_hub(laptop):
    clone = laptop['m'].clone
    code, out, err = run(laptop, 'join', JOB, '--repo', str(clone))
    assert code == 0, err
    assert 'refs/relay/demo/nodes/laptop' in remote_refs(laptop['hub'])
    code, out, err = run(laptop, 'init', JOB, '--from', str(clone / '.relay/demo/job.yaml'))
    assert code == 0, err
    refs = remote_refs(laptop['hub'])
    assert {'refs/relay/demo/tasks/%s' % s for s in ('M0', 'M1', 'M3', 'merge', 'coordinator')} <= set(refs)
    code, out, err = run(laptop, 'status', JOB)
    assert code == 0 and out.startswith('job demo')
    m1 = [l for l in out.splitlines() if l.startswith('M1 ')][0].split()
    assert m1[1] == 'blocked' and m1[-1] == 'M3'
    code, out, err = run(laptop, 'take', 'M3')
    assert code == 0, err
    code, out, _ = run(laptop, 'status', JOB)
    assert [l for l in out.splitlines() if l.startswith('M3 ')][0].split()[:4] == ['M3', 'working', 'laptop', '1']
    code, out, err = run(laptop, 'park', 'M3', '--reason', 'needs a person')
    assert code == 0, err
    code, out, _ = run(laptop, 'status', JOB)
    assert [l for l in out.splitlines() if l.startswith('M3 ')][0].split()[1] == 'parked'


def test_machine_block_comes_from_measurement_and_inventory(laptop):
    m = backend.GitBackend(state_dir=laptop['m'].state_dir, settings=settings()).machine()     # this host, measured
    assert m['toolchains'] == {'python': '3.12', 'rust': '1.83'} and m['always_on'] is False
    assert m['cores'] >= 1 and m['disk_free_gb'] is not None


def test_without_a_self_entry_the_relay_is_unavailable(laptop):
    laptop['inv'].write_text('machines: {}\n')
    code, _, err = run(laptop, 'join', JOB, '--repo', str(laptop['m'].clone))
    assert code == cli.EXIT_UNAVAILABLE and 'source: self' in err


def test_run_outside_the_root_fleet_refuses_to_start_work(laptop):
    """Only the relay process the root fleet's daemon starts may start sub-fleets."""
    run(laptop, 'join', JOB, '--repo', str(laptop['m'].clone))
    code, _, err = run(laptop, 'run', '--node', 'laptop')
    assert code == cli.EXIT_UNAVAILABLE and 'root fleet' in err


def test_worktrees_live_beside_the_clone(laptop):
    run(laptop, 'join', JOB, '--repo', str(laptop['m'].clone))
    ws = laptop['b'].workspace(JOB)
    assert ws.path('M3', 2) == laptop['m'].clone.parent / 'clone-relay' / JOB / 'M3-e2'
    member = json.loads((laptop['m'].state_dir / 'jobs' / 'demo.json').read_text())
    assert member['node'] == 'laptop'


@pytest.fixture
def three(tmp_path, monkeypatch):
    """Three machines on one bare hub, each with its own inventory file. A command runs as that machine: its
    $FLEETMUX_SUPERPOWERS and a fresh backend (one process per command, as on a real machine)."""
    hub, base, ms = machines(tmp_path, job=JOB, files=project_files())
    for var in ('FLEETMUX_APPLET_NAME', 'FLEETMUX_APPLET_CONFIG'):
        monkeypatch.delenv(var, raising=False)
    cores = {'laptop': 10, 'desktop': 16, 'server': 8}
    invs = {}
    for name in ms:
        inv = tmp_path / ('inv-' + name) / 'fleet-superpowers.yaml'
        inv.parent.mkdir()
        inv.write_text('machines:\n  %s:\n    source: self\n    always_on: true\n'
                       '    toolchains: {python: "3.12"}\n' % name)
        invs[name] = inv

    def as_machine(name):
        monkeypatch.setenv('FLEETMUX_SUPERPOWERS', str(invs[name]))
        b = backend.GitBackend(state_dir=ms[name].state_dir, settings=settings(),
                               measure=lambda path, toolchains, always_on:
                               machine_info(cores[name], 32, 100, always_on=always_on, toolchains=toolchains))
        return {'m': ms[name], 'b': b}
    return {'ms': ms, 'inv': invs, 'cores': cores, 'as': as_machine}


def inventory_of(three, name):
    from fleetmux import fleet_superpowers as fs
    return fs.read(three['inv'][name])['machines']


def heartbeat(three, name):
    """One round of this machine's relay loop (the heartbeat): read the hub, copy the peers, push its record."""
    from fleetmux.relay import launch
    env = three['as'](name)
    env['b'].make_executor = lambda job: launch.FakeExecutor()     # no fleet here; nothing is started
    ctx = cli.Ctx(env['b'], env['m'].state_dir, io.StringIO(), io.StringIO())
    ctx.flow(JOB, name, run=True, settle_on_start=False).step()


def test_join_and_heartbeat_copy_the_peers_into_each_inventory(three):
    """R20: each machine publishes its measured block; on join and on each heartbeat it copies its peers'
    blocks into its own inventory file. After the joins and one heartbeat each holds the latest of the other two."""
    for name in ('laptop', 'desktop', 'server'):
        code, _, err = run(three['as'](name), 'join', JOB, '--repo', str(three['ms'][name].clone))
        assert code == 0, err
    server = inventory_of(three, 'server')                  # the last joiner already holds both peers
    assert server['laptop']['source'] == 'copy' and server['laptop']['hardware']['cores'] == 10
    assert server['desktop']['hardware']['cores'] == 16
    assert set(inventory_of(three, 'laptop')) == {'laptop'}  # the first joiner saw no peer yet
    three['cores']['desktop'] = 24                           # desktop's measurement changes
    for name in ('laptop', 'desktop', 'server'):
        heartbeat(three, name)
    heartbeat(three, 'laptop')
    for name in ('laptop', 'desktop', 'server'):
        inv = inventory_of(three, name)
        assert set(inv) == {'laptop', 'desktop', 'server'} and inv[name]['source'] == 'self'
        for peer in set(inv) - {name}:
            assert inv[peer]['source'] == 'copy'
    assert inventory_of(three, 'laptop')['desktop']['hardware']['cores'] == 24
    assert inventory_of(three, 'server')['desktop']['hardware']['cores'] == 24


def test_run_exits_75_when_the_real_hub_is_gone(laptop, monkeypatch):
    """On a real bare hub: after join and init the hub disappears; run backs off and exits 75, never 1."""
    from fleetmux.relay import launch
    from test_relay_cli import FakeTime, LoopApplet, use_stub_heartbeat
    clone = laptop['m'].clone
    assert run(laptop, 'join', JOB, '--repo', str(clone))[0] == 0
    assert run(laptop, 'init', JOB, '--from', str(clone / '.relay/demo/job.yaml'))[0] == 0
    laptop['hub'].rename(laptop['hub'].with_name('gone.git'))
    laptop['b'].make_executor = lambda job: launch.FakeExecutor()
    LoopApplet.hooks, LoopApplet.max_rounds, FakeTime.now = {}, 200, 0.0
    use_stub_heartbeat(monkeypatch, LoopApplet)
    monkeypatch.setattr(cli, 'time', FakeTime)
    monkeypatch.setenv(cli.APPLET_NAME, 'relay')
    monkeypatch.setattr(cli.signal, 'signal', lambda *_: None)
    with pytest.raises(SystemExit) as exc:
        run(laptop, 'run', '--node', 'laptop')
    assert exc.value.code == 75


def test_status_node_rows_come_from_the_run_on_the_hub(laptop, monkeypatch):
    """On the real hub: after a few run passes, `status` lists this machine with its own record's state and a
    SEEN time from the run's view."""
    from fleetmux.relay import launch
    from test_relay_cli import FakeTime, LoopApplet, node_rows, use_stub_heartbeat
    clone = laptop['m'].clone
    assert run(laptop, 'join', JOB, '--repo', str(clone))[0] == 0
    assert run(laptop, 'init', JOB, '--from', str(clone / '.relay/demo/job.yaml'))[0] == 0
    laptop['b'].make_executor = lambda job: launch.FakeExecutor()
    LoopApplet.hooks, LoopApplet.max_rounds, FakeTime.now = {}, 3, 0.0
    use_stub_heartbeat(monkeypatch, LoopApplet)
    monkeypatch.setattr(cli, 'time', FakeTime)
    monkeypatch.setenv(cli.APPLET_NAME, 'relay')
    monkeypatch.setattr(cli.signal, 'signal', lambda *_: None)
    with pytest.raises(SystemExit):
        run(laptop, 'run', '--node', 'laptop')
    monkeypatch.delenv(cli.APPLET_NAME)
    code, out, err = run(laptop, 'status', JOB)
    assert code == 0, err
    rows = node_rows(out)
    assert rows['laptop'][0] == 'leaving' and rows['laptop'][1] != '-'


class FakeRedis:
    """The host Redis as far as the relay's hot parameters use it: one hash."""

    def __init__(self):
        self.hashes = {}

    def hget(self, name, key):
        return self.hashes.get(name, {}).get(key)

    def hset(self, name, key=None, value=None, mapping=None):
        h = self.hashes.setdefault(name, {})
        if mapping:
            h.update(mapping)
        if key is not None:
            h[key] = value


def test_run_seeds_hot_values_into_redis_and_writes_back_on_schedule(laptop, monkeypatch):
    """Under the applet, run seeds every hot key of the relay block into the host Redis hash fx:relay:params at
    start, reads its hot values from there (a changed value takes effect), and writes changed values back every
    write_back_seconds."""
    import json
    from fleetmux.relay import launch, params
    from test_relay_cli import FakeTime, LoopApplet, use_stub_heartbeat
    wb = 40
    laptop['b'] = backend.GitBackend(state_dir=laptop['m'].state_dir, settings=settings(write_back_seconds=wb),
                                     measure=lambda path, toolchains, always_on:
                                     machine_info(10, 32, 200, always_on=always_on, toolchains=toolchains))
    clone = laptop['m'].clone
    assert run(laptop, 'join', JOB, '--repo', str(clone))[0] == 0
    assert run(laptop, 'init', JOB, '--from', str(clone / '.relay/demo/job.yaml'))[0] == 0
    laptop['b'].make_executor = lambda job: launch.FakeExecutor()
    redis = FakeRedis()
    LoopApplet.redis = redis
    calls = []
    monkeypatch.setattr(cli, 'params_write_back', lambda hot, block, path=None: calls.append(
        (FakeTime.now, params.changed(hot, block))) or {})
    poll = laptop['b'].settings.hot('poll_s')
    hooks = {5: lambda: redis.hset(params.REDIS_KEY, mapping={'poll_s': json.dumps(poll * 2)})}
    LoopApplet.hooks, LoopApplet.max_rounds, FakeTime.now = hooks, 20, 0.0
    use_stub_heartbeat(monkeypatch, LoopApplet)
    monkeypatch.setattr(cli, 'time', FakeTime)
    monkeypatch.setenv(cli.APPLET_NAME, 'relay')
    monkeypatch.setattr(cli.signal, 'signal', lambda *_: None)
    try:
        with pytest.raises(SystemExit) as exc:
            run(laptop, 'run', '--node', 'laptop')
    finally:
        del LoopApplet.redis
    assert exc.value.code == 0
    seeded = redis.hashes[params.REDIS_KEY]
    assert set(params.HOT_KEYS) <= set(seeded)
    assert laptop['b'].settings.hot('poll_s') == poll * 2                 # read from Redis while running
    times = [t for t, _ in calls]
    assert times and all(b - a >= wb for a, b in zip(times, times[1:]))
    assert any(ch.get('poll_s') == poll * 2 for _, ch in calls)


def test_hot_values_written_back_to_a_temp_private_file(tmp_path):
    """A temp private fleet.yaml and a fake Redis: a hot value changed in Redis is written to the private file's
    relay.hot after write_back_seconds; nothing is written before."""
    import json
    from fleetmux.relay import params
    s = settings(write_back_seconds=60)
    redis = FakeRedis()
    private = tmp_path / 'fleet.yaml'
    clock = [0.0]
    hot = cli.HotParams(s, params.RedisHot(redis), err=io.StringIO(), path=private, clock=lambda: clock[0])
    hot.start()
    redis.hset(params.REDIS_KEY, mapping={'poll_s': json.dumps(s.hot('poll_s') + 5)})
    clock[0] = 30.0
    hot.tick()
    assert not private.exists()
    clock[0] = 61.0
    hot.tick()
    import yaml
    assert yaml.safe_load(private.read_text())['relay']['hot']['poll_s'] == s.block['hot']['poll_s'] + 5
