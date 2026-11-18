"""`start` with a live root fleet on this machine attaches to it (fleetmux/join.py): it writes a join stub and
starts nothing; `task` / `status` / `stop` / `result` on the stub talk to the root through its command line
(stubbed here: ``join.fleet_cli``). Sub-fleets are listed in the order they were created."""
import io
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fleetmux import fleet, join, subfleet  # noqa: E402
from test_message_bus_v2 import private_redis_v2  # noqa: E402,F401  (fixture)
from test_runtime_v2 import event, fx  # noqa: E402,F401  (fx: a v2 fleet on a private Redis)


# ------------------------------------------------------------------ fixtures
class Live(set):
    """Run names whose daemon holds its lock; ``silent``: those whose Redis does not answer (a stopping root)."""
    silent = None


@pytest.fixture
def alive(monkeypatch):
    """Run directories whose daemon counts as alive (by name)."""
    names = Live()
    names.silent = set()
    monkeypatch.setattr(fleet, 'daemon_running', lambda run: Path(run).name in names)
    monkeypatch.setattr(join, 'root_answers', lambda config: Path(config).parent.name in names - names.silent,
                        raising=False)
    return names


def make_run(home, name, mode='service', backend='fleetmux', parent=None, alive=None, fleet_name=None):
    """A fleet home whose latest.json names run ``name`` (a daemon.lock exists; ``alive`` marks it live)."""
    home = Path(home)
    run = home / 'runs' / name
    run.mkdir(parents=True)
    (run / 'daemon.lock').touch()
    cfg = {'run': str(run), 'mode': mode, 'backend': backend, 'fleet_home': str(home)}
    if parent:
        cfg['parent_config'] = parent
    if fleet_name:
        cfg['name'] = fleet_name
    fleet.save(run / 'config.json', cfg)
    fleet.save(home / 'latest.json', dict(cfg, config=str(run / 'config.json')))
    if alive is not None:
        alive.add(name)
    return run / 'config.json'


@pytest.fixture
def scan(monkeypatch, tmp_path):
    root = tmp_path / 'user-root'
    root.mkdir()
    monkeypatch.setenv(join.SCAN_ENV, str(root))
    return root


def test_the_suite_never_searches_the_real_home(tmp_path):
    assert join.scan_root() == tmp_path / 'no-join-root'  # tests/conftest.py, inherited by subprocesses


# ------------------------------------------------------------------ discovery
def test_discovery_prefers_service_root(tmp_path, scan, alive):
    make_run(scan, 'fleet-20260101-000000-aaaa', mode='task', alive=alive)  # earliest, same home, task
    cfg = make_run(scan / 'fleets' / 'svc', 'fleet-20260102-000000-bbbb', mode='service', alive=alive)
    assert join.find_root(scan)['config'] == str(cfg)


def test_discovery_prefers_same_home_then_earliest(tmp_path, scan, alive):
    make_run(scan, 'fleet-20260101-000000-aaaa', mode='task', alive=alive)
    mine = make_run(scan / 'fleets' / 'mine', 'fleet-20260105-000000-cccc', mode='task', alive=alive)
    assert join.find_root(scan / 'fleets' / 'mine')['config'] == str(mine)
    other = tmp_path / 'elsewhere'
    other.mkdir()
    assert join.find_root(other)['config'] == str(scan / 'runs' / 'fleet-20260101-000000-aaaa' / 'config.json')


def test_discovery_ignores_dead_sub_fleet_other_backend_and_stubs(tmp_path, scan, alive):
    make_run(scan, 'fleet-20260101-000000-dead', mode='service')  # daemon not alive
    make_run(scan / 'fleets' / 'child', 'fleet-20260101-000000-chld', parent='/x/config.json', alive=alive)
    make_run(scan / 'fleets' / 'cmux', 'fleet-20260101-000000-cmux', backend='cmux', alive=alive)
    stub = scan / 'fleets' / 'stub'
    stub.mkdir(parents=True)
    fleet.save(stub / 'latest.json', {'join': {'root_config': '/x'}})
    (scan / 'fleets' / 'gone').mkdir()
    fleet.save(scan / 'fleets' / 'gone' / 'latest.json', {'run': str(tmp_path / 'no-such-run')})
    assert join.find_root(tmp_path) is None
    assert not (tmp_path / 'no-such-run').exists()  # liveness never creates a missing run's lock


# ------------------------------------------------------------------ start -> attach
# a pi supervisor / pi workers cannot apply to a sub-fleet (join.start_settings): attaching starts drop them
JOIN_DROP = (('--supervisor-cli', 'pi'), ('--worker-cli', 'pi'), ('--pi-model', 'local/test'))


def start_world(tmp_path, monkeypatch, session_exists=False, join=False):
    from test_pi import mock_start
    args, ran = mock_start(tmp_path, monkeypatch)
    for pair in JOIN_DROP if join else ():
        i = args.index(pair[0])
        assert args[i + 1] == pair[1]
        del args[i:i + 2]
    spawned, views = [], []
    mgr = SimpleNamespace(meta_path=tmp_path / 'state' / 'fleetmux.fleetmux.json', runtime_root=tmp_path / 'rt',
                          session_exists=lambda: session_exists, claim_run=lambda run: None,
                          spawn_window=lambda *a, **kw: spawned.append(a) or SimpleNamespace(success=True, error=None))
    monkeypatch.setattr(fleet, 'resolve_backend', lambda requested=None, platform=None: (requested or 'fleetmux', None))
    monkeypatch.setattr(fleet, 'backends_check', lambda name: True)
    monkeypatch.setattr(fleet, 'manager_for', lambda cfg: mgr)
    monkeypatch.setattr(fleet, 'auto_view', lambda *a: views.append(a))
    monkeypatch.setattr(os, 'execvp', lambda *a: views.append(a))
    return args, ran, spawned, views


def test_start_with_live_root_writes_stub_and_starts_nothing(tmp_path, monkeypatch, scan, alive, capsys):
    root_cfg = make_run(scan / 'fleets' / 'svc', 'fleet-20260101-000000-root', alive=alive)
    args, ran, spawned, views = start_world(tmp_path, monkeypatch, join=True)
    roster = tmp_path / 'roster.yaml'
    roster.write_text('workers: [{name: worker-1}]\n')
    fleet.main(args + ['--roster', str(roster), '--name', 'feature x'])  # returns: exit 0
    stub = fleet.load(tmp_path / 'latest.json')['join']
    assert stub['root_config'] == str(root_cfg)
    assert stub['roster'] == str(roster.resolve()) and stub['lifecycle'] == 'dev_task' and stub['name'] == 'feature x'
    assert stub['jobs'] == []
    assert ran == [] and spawned == [] and views == []  # no Redis, no daemon pane, no view
    assert not (tmp_path / 'runs').exists()
    out = capsys.readouterr().out
    assert 'attached to the running service fleet ' + str(root_cfg) in out and 'fleetmux task -' in out


def test_start_in_the_roots_own_home_writes_a_separate_stub(tmp_path, monkeypatch, scan, alive, capsys):
    args, ran, spawned, views = start_world(tmp_path, monkeypatch, join=True, session_exists=True)
    root_cfg = make_run(tmp_path, 'fleet-20260101-000000-root', alive=alive)  # latest.json of this home
    before = (tmp_path / 'latest.json').read_text()
    fleet.main(args + ['--name', 'second'])
    assert (tmp_path / 'latest.json').read_text() == before  # the root's latest.json is never overwritten
    stub = fleet.load(tmp_path / 'joins' / 'second.json')['join']
    assert stub['root_config'] == str(root_cfg)
    assert '--config ' + str(tmp_path / 'joins' / 'second.json') in capsys.readouterr().out
    assert ran == [] and spawned == [] and views == []


def test_a_second_start_keeps_the_first_stub_and_its_jobs(tmp_path, monkeypatch, scan, alive, capsys):
    make_run(scan / 'fleets' / 'svc', 'fleet-20260101-000000-root', alive=alive)
    args, ran, spawned, views = start_world(tmp_path, monkeypatch, join=True)
    fleet.main(args)
    data = fleet.load(tmp_path / 'latest.json')
    data['join']['jobs'] = ['job-abc']  # as `task` records it
    fleet.save(tmp_path / 'latest.json', data)
    fleet.main(args + ['--name', 'next'])
    fleet.main(args + ['--name', 'next'])
    assert fleet.load(tmp_path / 'latest.json')['join']['jobs'] == ['job-abc']
    assert (tmp_path / 'joins' / 'next.json').exists()
    first = fleet.load(tmp_path / 'joins' / 'next.json')
    first['join']['jobs'] = ['job-def']
    fleet.save(tmp_path / 'joins' / 'next.json', first)
    fleet.main(args + ['--name', 'next'])  # the name holds a job now: the next stub gets its own file
    assert fleet.load(tmp_path / 'joins' / 'next.json')['join']['jobs'] == ['job-def']
    assert (tmp_path / 'joins' / 'next-2.json').exists()


def test_start_keeps_an_unfinished_runs_latest_json(tmp_path, monkeypatch, scan, alive):
    make_run(scan / 'fleets' / 'svc', 'fleet-20260101-000000-root', alive=alive)
    args, ran, spawned, views = start_world(tmp_path, monkeypatch, join=True)
    make_run(tmp_path, 'fleet-20250101-000000-old')  # dead, no result.json: `start --resume auto` needs it
    before = (tmp_path / 'latest.json').read_text()
    fleet.main(args + ['--name', 'x'])
    assert (tmp_path / 'latest.json').read_text() == before and (tmp_path / 'joins' / 'x.json').exists()


def root_cli(monkeypatch, reply=None):
    """``join.fleet_cli`` stubbed: records each call, answers ``reply`` (default: the op accepted)."""
    calls = []

    def fake_cli(argv, stdin=None, timeout=join.CLI_TIMEOUT):
        calls.append((list(argv), stdin))
        spec = json.loads(stdin) if stdin else {}
        return 0, json.dumps(reply or {'status': 'accepted', 'id': spec.get('id')}), ''
    monkeypatch.setattr(join, 'fleet_cli', fake_cli)
    return calls


def service_args(args, extra):
    if extra[0] == '--lifecycle':
        args = args[:args.index('--lifecycle')] + args[args.index('--lifecycle') + 2:]
    return args + extra


@pytest.mark.parametrize('extra', [['--lifecycle', 'service'], ['--mode', 'service']])
def test_a_service_start_attaches_as_a_service_sub_fleet(tmp_path, monkeypatch, scan, alive, capsys, extra):
    root_cfg = make_run(scan / 'fleets' / 'svc', 'fleet-20260101-000000-root', alive=alive)
    args, ran, spawned, views = start_world(tmp_path, monkeypatch, join=True)
    calls = root_cli(monkeypatch)
    roles = tmp_path / 'roles'
    roles.mkdir()
    fleet.main(service_args(args, extra) + ['--name', 'Docs Index', '--workers', '2', '--roles', str(roles),
                                            '--worker-model', 'opus'])
    (argv, stdin), = calls
    assert argv == ['request', '--config', str(root_cfg), '-']
    spec = json.loads(stdin)
    assert spec['op'] == 'subfleet.attach' and spec['name'] == 'docs-index' and spec['workers'] == 2
    assert spec['roles'] == str(roles.resolve()) and spec['entry']['worker_model'] == 'opus'
    stub = fleet.load(tmp_path / 'latest.json')['join']
    assert stub['kind'] == 'service' and stub['child'] == 'docs-index' and stub['attach_id'] == spec['id']
    assert stub['root_config'] == str(root_cfg)
    assert ran == [] and spawned == [] and views == []  # nothing started here: the root runs it
    out = capsys.readouterr().out
    assert 'service sub-fleet docs-index' in out and 'fleetmux request -' in out


def test_a_refused_service_attach_starts_nothing_and_writes_no_stub(tmp_path, monkeypatch, scan, alive):
    make_run(scan / 'fleets' / 'svc', 'fleet-20260101-000000-root', alive=alive)
    args, ran, spawned, views = start_world(tmp_path, monkeypatch, join=True)
    root_cli(monkeypatch, {'status': 'rejected', 'reason': 'a sub-fleet named docs is already declared in this fleet'})
    with pytest.raises(SystemExit, match='already declared.*--no-join'):
        fleet.main(service_args(args, ['--lifecycle', 'service']) + ['--name', 'docs'])
    assert not (tmp_path / 'latest.json').exists() and spawned == [] and ran == []


def test_a_service_stub_keeps_its_place_in_latest_json(tmp_path, monkeypatch, scan, alive):
    make_run(scan / 'fleets' / 'svc', 'fleet-20260101-000000-root', alive=alive)
    args, ran, spawned, views = start_world(tmp_path, monkeypatch, join=True)
    root_cli(monkeypatch)
    fleet.main(service_args(args, ['--lifecycle', 'service']) + ['--name', 'docs'])
    fleet.main(service_args(args, ['--lifecycle', 'service']) + ['--name', 'ops'])
    assert fleet.load(tmp_path / 'latest.json')['join']['child'] == 'docs'
    assert fleet.load(tmp_path / 'joins' / 'ops.json')['join']['child'] == 'ops'


def test_a_task_start_attaches_to_a_live_task_root(tmp_path, monkeypatch, scan, alive, capsys):
    root_cfg = make_run(scan / 'fleets' / 'tsk', 'fleet-20260101-000000-root', mode='task', alive=alive)
    args, ran, spawned, views = start_world(tmp_path, monkeypatch, join=True)
    calls = root_cli(monkeypatch)
    fleet.main(args + ['--no-view'])
    stub = fleet.load(tmp_path / 'latest.json')['join']
    assert stub['root_config'] == str(root_cfg) and stub['root_mode'] == 'task' and stub['jobs'] == []
    assert calls == [] and ran == [] and spawned == [] and views == []  # a job stub; nothing started
    assert 'attached to the running task fleet ' + str(root_cfg) in capsys.readouterr().out


def test_a_task_start_in_the_task_roots_own_home_attaches(tmp_path, monkeypatch, scan, alive, capsys):
    args, ran, spawned, views = start_world(tmp_path, monkeypatch, join=True, session_exists=True)
    root_cfg = make_run(tmp_path, 'fleet-20260101-000000-root', mode='task', alive=alive)
    before = (tmp_path / 'latest.json').read_text()
    fleet.main(args + ['--name', 'second'])
    assert (tmp_path / 'latest.json').read_text() == before  # the root's latest.json is never overwritten
    assert fleet.load(tmp_path / 'joins' / 'second.json')['join']['root_config'] == str(root_cfg)
    assert spawned == [] and ran == []


def test_with_only_a_task_root_live_a_service_start_runs_its_own_fleet(tmp_path, monkeypatch, scan, alive, capsys):
    make_run(scan / 'fleets' / 'tsk', 'fleet-20260101-000000-root', mode='task', alive=alive)
    args, ran, spawned, views = start_world(tmp_path, monkeypatch)
    calls = root_cli(monkeypatch)
    fleet.main(service_args(args, ['--lifecycle', 'service']) + ['--no-view'])
    assert calls == [] and spawned and spawned[0][0] == 'daemon'  # a task root takes no service sub-fleet
    assert 'join' not in fleet.load(tmp_path / 'latest.json')
    assert 'is a task fleet; it takes jobs, not service sub-fleets' in capsys.readouterr().err


def test_a_task_root_in_the_same_home_refuses_a_service_start(tmp_path, monkeypatch, scan, alive):
    args, ran, spawned, views = start_world(tmp_path, monkeypatch, session_exists=True)
    make_run(tmp_path, 'fleet-20260101-000000-root', mode='task', alive=alive)
    before = (tmp_path / 'latest.json').read_text()
    with pytest.raises(SystemExit, match='runs in this fleet home.*Nothing was started.*--no-join'):
        fleet.main(service_args(args, ['--lifecycle', 'service']) + ['--name', 'second'])
    assert (tmp_path / 'latest.json').read_text() == before and not (tmp_path / 'joins').exists()
    assert spawned == [] and ran == []


def test_no_join_keeps_the_same_home_refusal(tmp_path, monkeypatch, scan, alive):
    args, ran, spawned, views = start_world(tmp_path, monkeypatch, session_exists=True)
    make_run(tmp_path, 'fleet-20260101-000000-root', alive=alive)
    with pytest.raises(SystemExit, match='tabs already exist'):
        fleet.main(args + ['--no-join'])
    assert not (tmp_path / 'joins').exists()


def test_no_join_starts_a_fleet_of_its_own(tmp_path, monkeypatch, scan, alive):
    make_run(scan / 'fleets' / 'svc', 'fleet-20260101-000000-root', alive=alive)
    args, ran, spawned, views = start_world(tmp_path, monkeypatch)
    fleet.main(args + ['--no-join', '--no-view'])
    assert spawned and spawned[0][0] == 'daemon'
    assert 'join' not in fleet.load(tmp_path / 'latest.json')


def test_start_without_a_live_root_is_unchanged(tmp_path, monkeypatch, scan, alive):
    make_run(scan / 'fleets' / 'svc', 'fleet-20260101-000000-root')  # dead
    args, ran, spawned, views = start_world(tmp_path, monkeypatch)
    fleet.main(args + ['--no-view'])
    assert spawned and spawned[0][0] == 'daemon'


# ------------------------------------------------------------------ a stopped root is no attach target
def own_start_world(tmp_path, monkeypatch, session_exists=False):
    """start_world for a start that may run its own fleet without pi options: a claude binary is found."""
    world = start_world(tmp_path, monkeypatch, session_exists=session_exists, join=True)
    which = fleet.shutil.which
    monkeypatch.setattr(fleet.shutil, 'which', lambda name: '/bin/claude' if name == 'claude' else which(name))
    run = fleet.subprocess.run  # mock_start's recorder; every probed CLI is new enough
    monkeypatch.setattr(fleet.subprocess, 'run',
                        lambda argv, **kw: run(argv, **kw) and SimpleNamespace(stdout='9.9.9\n', returncode=0))
    return world


def test_a_stopped_root_is_no_attach_target(tmp_path, monkeypatch, scan, alive, capsys):
    """`stop`, then `start` with the same arguments at once: the old daemon still holds its lock while it winds
    down, its Redis no longer answers and its tabs are still up. That root is not attached to; `start` waits for
    it to let go of its lock (its tabs close with it), then starts its own fleet."""
    args, ran, spawned, views = own_start_world(tmp_path, monkeypatch)
    old = 'fleet-20260101-000000-root'
    make_run(tmp_path, old, alive=alive, fleet_name='team')  # this very home
    alive.silent.add(old)
    fleet.manager_for({}).session_exists = lambda: old in alive  # the old fleet's tabs live as long as its daemon
    waits = []
    monkeypatch.setattr(join, '_pause', lambda: waits.append(1) or (len(waits) == 3 and alive.discard(old)),
                        raising=False)
    calls = root_cli(monkeypatch, {'status': 'error', 'reason': 'redis.exceptions.ConnectionError'})
    fleet.main(service_args(args, ['--lifecycle', 'service']) + ['--name', 'team', '--no-view'])
    assert calls == [] and len(waits) == 3 and spawned and spawned[0][0] == 'daemon'
    latest = fleet.load(tmp_path / 'latest.json')
    assert 'join' not in latest and not (tmp_path / 'joins').exists()
    assert latest['name'] == 'team'  # what live_root reads to tell this fleet from another one in this home
    assert 'is stopping; waiting up to' in capsys.readouterr().err


def test_the_wait_for_a_stopping_root_is_bounded(tmp_path, monkeypatch, scan, alive, capsys):
    make_run(tmp_path, 'fleet-20260101-000000-root', alive=alive)
    alive.silent.add('fleet-20260101-000000-root')  # never lets go
    clock = iter(range(0, 10 ** 6, 10))
    monkeypatch.setattr(join.time, 'monotonic', lambda: next(clock))
    monkeypatch.setattr(join, '_pause', lambda: None)
    join.wait_stopping(tmp_path)
    assert 'still stopping after' in capsys.readouterr().err
    alive.silent.clear()
    join.wait_stopping(tmp_path)  # a live, answering root: no wait at all
    assert capsys.readouterr().err == ''


def test_a_stopped_root_in_another_home_is_no_attach_target(tmp_path, monkeypatch, scan, alive):
    make_run(scan / 'fleets' / 'svc', 'fleet-20260101-000000-root', alive=alive)
    alive.silent.add('fleet-20260101-000000-root')
    assert join.find_root(tmp_path) is None
    args, ran, spawned, views = own_start_world(tmp_path, monkeypatch)
    calls = root_cli(monkeypatch)
    fleet.main(service_args(args, ['--lifecycle', 'service']) + ['--name', 'docs', '--no-view'])
    assert calls == [] and spawned and spawned[0][0] == 'daemon'


def test_a_live_root_in_another_home_is_still_attached_to(tmp_path, scan, alive):
    cfg = make_run(scan / 'fleets' / 'svc', 'fleet-20260101-000000-root', alive=alive, fleet_name='team')
    assert join.find_root(tmp_path, name='team')['config'] == str(cfg)  # same name, other home: a real root


def test_start_never_attaches_to_the_fleet_it_starts(tmp_path, monkeypatch, scan, alive):
    """Same home, same name: the root is this fleet itself. `start` does not attach to it; it goes the normal way
    (which refuses a fleet whose tabs are still up)."""
    args, ran, spawned, views = own_start_world(tmp_path, monkeypatch, session_exists=True)
    make_run(tmp_path, 'fleet-20260101-000000-root', alive=alive, fleet_name='team')
    assert join.find_root(tmp_path, name='team') is None
    assert join.find_root(tmp_path, name='other') is not None  # another fleet started here still attaches
    calls = root_cli(monkeypatch)
    with pytest.raises(SystemExit, match='tabs already exist'):
        fleet.main(service_args(args, ['--lifecycle', 'service']) + ['--name', 'team'])
    assert calls == [] and not (tmp_path / 'joins').exists()


def test_a_root_that_stops_during_the_attach_lets_start_run_its_own(tmp_path, monkeypatch, scan, alive, capsys):
    make_run(scan / 'fleets' / 'svc', 'fleet-20260101-000000-root', alive=alive)
    args, ran, spawned, views = own_start_world(tmp_path, monkeypatch)

    def stopped_cli(argv, stdin=None, timeout=join.CLI_TIMEOUT):
        alive.silent.add('fleet-20260101-000000-root')  # its Redis went down under the request
        return 1, '', 'redis.exceptions.ConnectionError: Error 61 connecting to unix socket'
    monkeypatch.setattr(join, 'fleet_cli', stopped_cli)
    fleet.main(service_args(args, ['--lifecycle', 'service']) + ['--name', 'docs', '--no-view'])
    assert spawned and spawned[0][0] == 'daemon'
    assert 'stopped before it took this fleet' in capsys.readouterr().err


class StubRedis:
    def __init__(self, service=None, task=None):
        self.service, self.task, self.closed = service or {}, task or {}, False
        self.connection_pool = type('Pool', (), {'disconnect': lambda self: None})()

    def hget(self, key, field):
        return (self.service if key == fleet.SERVICE else self.task).get(field)

    def hmget(self, key, *fields):
        return [(self.service if key == fleet.SERVICE else self.task).get(f) for f in fields]

    def close(self):
        self.closed = True


@pytest.mark.parametrize('service,task,answers', [
    ({'paused': '0', 'reason': ''}, {}, True),
    ({'paused': '1', 'reason': 'budget'}, {}, True),
    ({'paused': '1', 'reason': 'cancelled: explicit fleet stop; runtime retirement required'}, {}, False),
    ({}, {'status': 'running', 'reason': ''}, True),
    ({}, {'status': 'cancelled', 'reason': 'explicit fleet stop; runtime retirement required'}, False),
    ({}, {'status': 'cancelled', 'reason': 'stopped by operator'}, False),
])
def test_root_answers_reads_the_stop_from_redis(tmp_path, monkeypatch, service, task, answers):
    fleet.save(tmp_path / 'config.json', {'run': str(tmp_path)})
    r = StubRedis(service, task)
    monkeypatch.setattr(fleet, 'connect', lambda cfg, socket_timeout=5: r)
    assert join.root_answers(tmp_path / 'config.json') is answers and r.closed


@pytest.mark.parametrize('mode', ['service', 'task'])
def test_a_v2_stop_is_read_as_stopping(fx, mode):
    """The daemon's own v2 `stop` handling, on its private Redis: join sees the stop, through the run's config."""
    f = fx
    if mode == 'task':
        f.service = False
        f.cfg.update(mode='task', lifecycle='program')
        event(f, dict(kind='task', request_id='task-1', spec={'goal': 'x'}))
        assert f.task()['status'] == 'running'
    assert join.root_answers(f.path)
    event(f, {'kind': 'stop', 'request_id': 'stop-1'})
    assert f.stopping and join._stopping(f.r) and not join.root_answers(f.path)


def test_root_answers_is_false_when_redis_does_not_answer(tmp_path, monkeypatch):
    fleet.save(tmp_path / 'config.json', {'run': str(tmp_path)})

    def down(cfg, socket_timeout=5):
        raise ConnectionError('Error 2 connecting to unix socket')
    monkeypatch.setattr(fleet, 'connect', down)
    assert join.root_answers(tmp_path / 'config.json') is False
    assert join.root_answers(tmp_path / 'missing.json') is False


# ------------------------------------------------------------------ commands on a stub
@pytest.fixture
def stubbed(tmp_path, monkeypatch, scan, alive):
    """A live service root and a join stub at <tmp>/home/latest.json; ``calls`` records the root CLI calls."""
    root_cfg = make_run(scan / 'fleets' / 'svc', 'fleet-20260101-000000-root', alive=alive)
    path = tmp_path / 'home' / 'latest.json'
    fleet.save(path, {'join': {'root_config': str(root_cfg), 'roster': '/abs/roster.yaml', 'roles': None,
                               'lifecycle': 'dev_task', 'name': 'feature-x', 'budget': None, 'jobs': []}})
    calls, replies = [], {}

    def fake_cli(argv, stdin=None, timeout=join.CLI_TIMEOUT):
        calls.append((list(argv), stdin))
        return replies.get(argv[0], (0, '{}', ''))
    monkeypatch.setattr(join, 'fleet_cli', fake_cli)
    return SimpleNamespace(root=root_cfg, path=path, calls=calls, replies=replies, scan=scan, alive=alive)


def run_main(argv, monkeypatch, stdin=''):
    monkeypatch.setattr(sys, 'stdin', io.StringIO(stdin))
    try:
        fleet.main(argv)
    except SystemExit as exc:
        return exc.code or 0
    return 0


def test_task_on_stub_submits_merged_spec_and_records_job(stubbed, monkeypatch, capsys):
    stubbed.replies['task'] = (0, json.dumps({'status': 'accepted', 'job_id': 'job-abc', 'task_id': 'job-abc'}), '')
    spec = {'goal': 'build it', 'hours': 3, 'lifecycle': 'review_loop'}
    assert run_main(['task', '--config', str(stubbed.path), '-'], monkeypatch, json.dumps(spec)) == 0
    (argv, stdin), = stubbed.calls
    assert argv == ['task', '--config', str(stubbed.root), '--job', '-']  # never the root's own task
    # the spec wins; the stub's roster / name (and lifecycle, set by the spec) fill the keys the spec lacks
    assert json.loads(stdin) == dict(spec, roster='/abs/roster.yaml', name='feature-x')
    assert fleet.load(stubbed.path)['join']['jobs'] == ['job-abc']
    assert json.loads(capsys.readouterr().out)['job_id'] == 'job-abc'
    data = fleet.load(stubbed.path)
    data['join']['budget'] = {'max_usd': 5}
    fleet.save(stubbed.path, data)
    stubbed.replies['task'] = (0, json.dumps({'status': 'accepted', 'job_id': 'job-def', 'task_id': 'job-def'}), '')
    assert run_main(['task', '--config', str(stubbed.path), '-'], monkeypatch, '{"goal": "next"}') == 0
    assert json.loads(stubbed.calls[-1][1]) == {'goal': 'next', 'lifecycle': 'dev_task', 'budget': {'max_usd': 5},
                                                 'roster': '/abs/roster.yaml', 'name': 'feature-x'}
    assert fleet.load(stubbed.path)['join']['jobs'] == ['job-abc', 'job-def']


@pytest.mark.parametrize('reply, words', [
    ({'status': 'rejected', 'reason': 'this fleet runs in service mode: submit requests, not tasks'},
     'cannot run jobs on a service fleet'),
    ({'status': 'rejected', 'reason': 'a job spec takes only goal'}, 'a job spec takes only goal'),
    ({'status': 'pending', 'request_id': 'r1'}, 'did not answer in time'),
    ({'status': 'accepted', 'task_id': 'task-1'}, 'refused the job'),  # no job id: never counted as a job
])
def test_task_on_stub_reports_a_refusal_and_exits_non_zero(stubbed, monkeypatch, capsys, reply, words):
    stubbed.replies['task'] = (0, json.dumps(reply), '')
    assert run_main(['task', '--config', str(stubbed.path), '-'], monkeypatch, '{"goal": "g"}') == 2
    assert words in capsys.readouterr().err
    assert fleet.load(stubbed.path)['join']['jobs'] == []


def test_task_on_stub_submits_a_job_to_a_task_mode_root(stubbed, monkeypatch, capsys):
    cfg = fleet.load(stubbed.root)
    fleet.save(stubbed.root, dict(cfg, mode='task'))
    stubbed.replies['task'] = (0, json.dumps({'status': 'accepted', 'job_id': 'job-abc', 'task_id': 'job-abc'}), '')
    assert run_main(['task', '--config', str(stubbed.path), '-'], monkeypatch, '{"goal": "g"}') == 0
    assert stubbed.calls[-1][0] == ['task', '--config', str(stubbed.root), '--job', '-']
    assert fleet.load(stubbed.path)['join']['jobs'] == ['job-abc']
    reason = 'this task fleet runs no task of its own now; it takes jobs only while its task runs'
    stubbed.replies['task'] = (0, json.dumps({'status': 'rejected', 'reason': reason}), '')
    assert run_main(['task', '--config', str(stubbed.path), '-'], monkeypatch, '{"goal": "g2"}') == 2
    assert reason in capsys.readouterr().err and fleet.load(stubbed.path)['join']['jobs'] == ['job-abc']


def test_task_on_stub_with_dead_root(stubbed, monkeypatch, capsys):
    stubbed.alive.clear()
    assert run_main(['task', '--config', str(stubbed.path), '-'], monkeypatch, '{"goal": "g"}') == 2
    assert stubbed.calls == [] and 'no longer running' in capsys.readouterr().err


def with_job(stubbed, tmp_path, state='running', child=True):
    data = fleet.load(stubbed.path)
    data['join']['jobs'] = ['job-abc']
    fleet.save(stubbed.path, data)
    run = Path(fleet.load(stubbed.root)['run']) / 'sub' / 'M3'
    if child:
        run.mkdir(parents=True)
        fleet.save(run / 'config.json', {'run': str(run), 'parent_config': str(stubbed.root)})
    status = {'mode': 'service', 'jobs': [{'id': 'job-other', 'state': 'running', 'subfleet': 'M1'},
                                          {'id': 'job-abc', 'state': state, 'subfleet': 'M3' if child else ''}],
              'subfleets': {'M1': {'run': '/elsewhere'}, 'M3': {'state': 'running', 'run': str(run)}}}
    stubbed.replies['status'] = (0, json.dumps(status, indent=2), '')
    return run


def test_status_on_stub_shows_only_the_job(stubbed, tmp_path, monkeypatch, capsys):
    run = with_job(stubbed, tmp_path)
    assert run_main(['status', '--config', str(stubbed.path)], monkeypatch) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['job'] == 'job-abc' and out['state'] == 'running' and out['subfleet']['run'] == str(run)
    assert [c[0] for c in stubbed.calls] == [['status', '--config', str(stubbed.root)]]


def test_status_on_stub_picks_the_active_attempt(stubbed, tmp_path, monkeypatch, capsys):
    run = with_job(stubbed, tmp_path)
    status = json.loads(stubbed.replies['status'][1])
    status['subfleets'] = {'M3 (M3-attempt-1)': {'state': 'reclaimed', 'run': '/old'},
                           'M3': {'state': 'running', 'run': str(run)}, 'M4': {'state': 'running', 'run': '/x'}}
    stubbed.replies['status'] = (0, json.dumps(status), '')
    assert run_main(['status', '--config', str(stubbed.path)], monkeypatch) == 0
    assert json.loads(capsys.readouterr().out)['subfleet']['run'] == str(run)


def test_stop_on_stub_stops_the_jobs_sub_fleet_never_the_root(stubbed, tmp_path, monkeypatch):
    run = with_job(stubbed, tmp_path)
    stubbed.replies['stop'] = (0, '{"status": "accepted"}\n', '')
    assert run_main(['stop', '--config', str(stubbed.path)], monkeypatch) == 0
    stops = [c[0] for c in stubbed.calls if c[0][0] == 'stop']
    assert stops == [['stop', '--config', str(run / 'config.json')]]


def test_stop_on_stub_refuses_a_queued_job(stubbed, tmp_path, monkeypatch, capsys):
    with_job(stubbed, tmp_path, state='queued', child=False)
    assert run_main(['stop', '--config', str(stubbed.path)], monkeypatch) == 2
    assert not [c for c in stubbed.calls if c[0][0] == 'stop']
    assert 'no sub-fleet yet' in capsys.readouterr().err


def test_stop_on_stub_without_job_never_stops_the_root(stubbed, monkeypatch):
    assert run_main(['stop', '--config', str(stubbed.path)], monkeypatch) == 2
    assert stubbed.calls == []


def test_result_on_stub_asks_the_root_for_the_job(stubbed, tmp_path, monkeypatch, capsys):
    with_job(stubbed, tmp_path)
    stubbed.replies['result'] = (3, '{"id": "job-abc", "status": "pending"}\n', '')
    assert run_main(['result', '--config', str(stubbed.path)], monkeypatch) == 3
    assert stubbed.calls[0][0][:4] == ['result', 'job-abc', '--config', str(stubbed.root)]


def test_other_commands_refuse_a_stub(stubbed, monkeypatch, capsys):
    assert run_main(['results', '--config', str(stubbed.path)], monkeypatch) == 2
    assert 'join stub' in capsys.readouterr().err and stubbed.calls == []


@pytest.fixture
def service_stub(stubbed):
    """The stub of service sub-fleet docs, attached to the root; the child's run is <root run>/sub/docs."""
    fleet.save(stubbed.path, {'join': {'kind': 'service', 'child': 'docs', 'attach_id': 'attach-1',
                                       'root_config': str(stubbed.root), 'jobs': []}})
    child = Path(fleet.load(stubbed.root)['run']) / 'sub' / 'docs'
    child.mkdir(parents=True)
    fleet.save(child / 'config.json', {'run': str(child), 'parent_config': str(stubbed.root)})
    stubbed.child = child
    stubbed.alive.add('docs')
    return stubbed


def test_request_on_a_service_stub_goes_to_the_child(service_stub, monkeypatch, capsys):
    t = service_stub
    t.replies['request'] = (0, '{"status": "accepted", "id": "q1"}\n', '')
    spec = '{"id": "q1", "payload": {"message": "index"}, "reply": {"required": ["reply"]}}'
    assert run_main(['request', '--config', str(t.path), '-'], monkeypatch, spec) == 0
    assert t.calls == [(['request', '--config', str(t.child / 'config.json'), '-'], spec)]
    assert json.loads(capsys.readouterr().out)['id'] == 'q1'
    assert run_main(['results', '--config', str(t.path), '--drain'], monkeypatch) == 0
    assert t.calls[-1][0] == ['results', '--config', str(t.child / 'config.json'), '--drain']
    assert run_main(['result', 'q1', '--config', str(t.path)], monkeypatch) == 0
    assert t.calls[-1][0][:4] == ['result', 'q1', '--config', str(t.child / 'config.json')]


def test_result_without_id_on_a_service_stub_is_the_attach_answer(service_stub, monkeypatch):
    t = service_stub
    t.replies['result'] = (0, '{"id": "attach-1", "status": "ok", "state": "up"}\n', '')
    assert run_main(['result', '--config', str(t.path)], monkeypatch) == 0
    assert t.calls[0][0][:4] == ['result', 'attach-1', '--config', str(t.root)]


def test_request_before_the_child_is_up_is_refused(service_stub, monkeypatch, capsys):
    service_stub.alive.discard('docs')
    assert run_main(['request', '--config', str(service_stub.path), '-'], monkeypatch, '{}') == 2
    assert service_stub.calls == [] and 'not up' in capsys.readouterr().err


def test_stop_on_a_service_stub_detaches_only_the_child(service_stub, monkeypatch):
    t = service_stub
    t.replies['request'] = (0, '{"status": "accepted", "id": "detach-x"}\n', '')
    assert run_main(['stop', '--config', str(t.path)], monkeypatch) == 0
    (argv, stdin), = t.calls
    assert argv == ['request', '--config', str(t.root), '-']  # the root takes it down; never `stop` of anything
    assert json.loads(stdin)['op'] == 'subfleet.detach' and json.loads(stdin)['name'] == 'docs'


def test_status_and_task_on_a_service_stub(service_stub, monkeypatch, capsys):
    t = service_stub
    status = {'mode': 'service', 'subfleets': {'docs': {'name': 'docs', 'state': 'running', 'run': str(t.child)}}}
    t.replies['status'] = (0, json.dumps(status), '')
    assert run_main(['status', '--config', str(t.path)], monkeypatch) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['child'] == 'docs' and out['state'] == 'running'
    assert run_main(['task', '--config', str(t.path), '-'], monkeypatch, '{"goal": "g"}') == 2
    assert 'fleetmux request' in capsys.readouterr().err


def test_result_without_id_on_a_fleet_config_is_a_usage_error(tmp_path, monkeypatch):
    fleet.save(tmp_path / 'c.json', {'run': str(tmp_path)})
    with pytest.raises(SystemExit) as exc:
        fleet.main(['result', '--config', str(tmp_path / 'c.json')])
    assert 'needs a request id' in str(exc.value.code)


# ------------------------------------------------------------------ sidebar order
def test_children_dirs_follow_creation_order(tmp_path):
    base = time.time()
    for n in (10, 2, 0, 1):  # created M0, M1, M2, ..., M10 in this order; written out of order
        d = tmp_path / 'sub' / ('M' + str(n))
        d.mkdir(parents=True)
        fleet.save(d / 'subfleet.json', {'task_id': 'M' + str(n), 'created': base + n})
    assert [d.name for d in subfleet.children_dirs(tmp_path)] == ['M0', 'M1', 'M2', 'M10']


def test_children_dirs_fall_back_to_directory_time(tmp_path):
    for i, name in enumerate(('M10', 'M2')):
        d = tmp_path / 'sub' / name
        d.mkdir(parents=True)
        fleet.save(d / 'subfleet.json', {'task_id': name})
        os.utime(d, (1000 + i, 1000 + i))
    assert [d.name for d in subfleet.children_dirs(tmp_path)] == ['M10', 'M2']


def test_stub_forwards_name_roster_roles_and_the_spec_deliver(stubbed, monkeypatch):
    data = fleet.load(stubbed.path)
    data['join']['roles'] = '/abs/roles'
    fleet.save(stubbed.path, data)
    stubbed.replies['task'] = (0, json.dumps({'status': 'accepted', 'job_id': 'job-1', 'task_id': 'job-1'}), '')
    deliver = {'repo': '/abs/repo', 'feature_branch': 'feat/x', 'deploy_branch': 'main', 'test_cmd': ['true'],
               'queue': '/abs/q.json', 'review_file': 'review.json'}
    assert run_main(['task', '--config', str(stubbed.path), '-'], monkeypatch,
                    json.dumps({'goal': 'ship it', 'deliver': deliver})) == 0
    sent = json.loads(stubbed.calls[-1][1])
    assert sent == {'goal': 'ship it', 'deliver': deliver, 'lifecycle': 'dev_task', 'name': 'feature-x',
                    'roster': '/abs/roster.yaml', 'roles': '/abs/roles'}
    assert set(join.FORWARD) >= {'name', 'roster', 'roles', 'lifecycle', 'budget'}
