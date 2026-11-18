"""A loaded workflow survives a restart of its host fleet (docs/distributed-development.md, "Lifecycle details").

``fleetmux superpowers load`` writes its rows into the run's own roster copy and keeps a record in
``<run>/superpowers-load/<workflow>.json``. A fresh ``start --roster`` builds a new copy from the home roster and
``start --resume --roster`` rebuilds the copy; both re-apply every active record, so the applet rows come back and
``unload`` still restores the right bytes. A record that fails the load checks is not started and is listed.
Temporary directories and stubs only (the fixtures of test_superpowers_peer.py); no session starts.
"""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import yaml

from test_advisor import fake_start
from test_superpowers_peer import (ROSTER, WORKFLOW_ROOT, commit, git, loaded, main, world)  # noqa: F401
from fleetmux import fleet, fleet_superpowers as fs, roster as roster_mod
from fleetmux import superpowers_peer as peer


def home_roster(tmp_path):
    """The roster the fleet's start arguments name: outside every run, never written by load."""
    (tmp_path / 'roles').mkdir(exist_ok=True)
    (tmp_path / 'roles' / 'liaison.md').write_text('liaison\n')
    path = tmp_path / 'home-roster.yaml'
    path.write_text(ROSTER)
    return path


def service_start(monkeypatch, tmp_path):
    argv = fake_start(monkeypatch, tmp_path)
    argv[argv.index('dev_task')] = 'service'
    return argv


def point_latest_at(run):
    fleet.save(fleet.LATEST, {'run': str(run), 'config': str(run / 'config.json')})


def follow(run):
    """What `superpowers refresh` does after the restart: the inventory row names the new run."""
    entry = fs.machine('field')
    fs.write_self(dict(entry, fleets=[{'name': 'field', 'config': str(run / 'config.json'), 'running': True}]))


def restart(monkeypatch, tmp_path, old_run, extra=()):
    argv = service_start(monkeypatch, tmp_path) + ['--roster', str(home_roster(tmp_path))] + list(extra)
    point_latest_at(old_run)  # after fake_start moved LATEST
    fleet.main(argv)
    latest = fleet.load(fleet.LATEST)
    run = Path(latest['run'])
    return run, fleet.load(run / 'config.json')


def applet_names(path):
    return [e['name'] for e in roster_mod.load_applets(path)]


# -- fresh start -------------------------------------------------------------------------------------------

def test_a_fresh_start_loads_the_workflow_again(loaded, monkeypatch, tmp_path, capsys):
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    run, cfg = restart(monkeypatch, tmp_path, loaded['run'])
    copy = Path(cfg['roster'])
    assert applet_names(fleet.roster_file(cfg)) == ['applet-tunnel-1']  # what the daemon's applet host starts
    rows = yaml.safe_load(copy.read_text())['applets']
    assert rows[0]['durable_state'] is False and rows[0]['cwd'] == str(loaded['repo'].resolve())
    record = json.loads((run / peer.RECORDS / 'route.json').read_text())
    assert record['roster'] == str(copy) and record['after'] == copy.read_text()
    want = dict(yaml.safe_load(copy.read_text()))
    del want['applets']
    assert yaml.safe_load(record['before']) == want  # the copy as start built it from --roster
    assert not (loaded['run'] / peer.RECORDS / 'route.json').exists()  # moved to the new run, not left behind
    assert 'route' in capsys.readouterr().out


def test_unload_after_a_fresh_start_restores_the_new_copy(loaded, monkeypatch, tmp_path):
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    run, cfg = restart(monkeypatch, tmp_path, loaded['run'])
    copy = Path(cfg['roster'])
    before = json.loads((run / peer.RECORDS / 'route.json').read_text())['before']
    follow(run)
    code, _, err = main(['unload', 'route'])
    assert code == 0, err
    assert copy.read_text() == before and 'applets' not in yaml.safe_load(copy.read_text())
    assert not (run / peer.RECORDS / 'route.json').exists()


def test_a_second_restart_keeps_it_loaded(loaded, monkeypatch, tmp_path):
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    first, _ = restart(monkeypatch, tmp_path, loaded['run'])
    second, cfg = restart(monkeypatch, tmp_path, first)
    assert second != first and applet_names(cfg['roster']) == ['applet-tunnel-1']
    follow(second)
    assert main(['unload', 'route'])[0] == 0
    assert 'applets' not in yaml.safe_load(Path(cfg['roster']).read_text())


def second_workflow(loaded):
    repo = loaded['repo']
    (repo / 'teams' / 'other-applet.yaml').write_text(
        'applets:\n  - name: applet-other-1\n    command: ["{python}", "route_field.py"]\n    cwd: ..\n')
    ref = commit(repo, 'other')
    fs.save_workflow('other', dict(WORKFLOW_ROOT, source={'repo': str(repo), 'ref': ref, 'from': 'home'},
                                   applets=[{'machine': 'field', 'fleet': 'field',
                                             'roster_rows': 'teams/other-applet.yaml'}]))


def test_two_workflows_come_back_in_load_order_and_unload_in_reverse(loaded, monkeypatch, tmp_path):
    second_workflow(loaded)
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    loaded['redis'].hashes['fx:applet:applet-other-1'] = {'state': 'up'}
    assert main(['load', 'other', '--poll', '0'])[0] == 0
    records = loaded['run'] / peer.RECORDS
    for name, stamp in (('route', 2_000_000_000), ('other', 1_000_000_000)):  # records of an older release: no
        path = records / (name + '.json')                                    # sequence, mtimes in the wrong order
        path.write_text(json.dumps({k: v for k, v in json.loads(path.read_text()).items() if k != 'at'}))
        os.utime(path, (stamp, stamp))
    run, cfg = restart(monkeypatch, tmp_path, loaded['run'])
    assert applet_names(cfg['roster']) == ['applet-tunnel-1', 'applet-other-1']
    follow(run)
    assert main(['unload', 'other'])[0] == 0
    assert applet_names(cfg['roster']) == ['applet-tunnel-1']
    assert main(['unload', 'route'])[0] == 0
    assert 'applets' not in yaml.safe_load(Path(cfg['roster']).read_text())


def test_a_host_item_brings_back_the_supervisor_row_and_the_subfleet_row(loaded, monkeypatch, tmp_path):
    (loaded['repo'] / 'teams' / 'home.yaml').write_text('workers:\n  - name: proxy-1\n')  # a valid sub-fleet roster
    ref = commit(loaded['repo'], 'home roster')
    fs.save_workflow('route', dict(fs.workflow('route'), source=dict(fs.workflow('route')['source'], ref=ref), applets=[{'machine': 'field', 'fleet': 'field', 'host': {
        'subfleet': 'route-home', 'supervisor_slot': 'route-host-1', 'supervisor_role': 'teams/host.md',
        'roster': 'teams/home.yaml'}}]))
    loaded['redis'].hashes['fx:subfleet:route-home'] = {'applets': json.dumps({'applet-proxy-1': 'up'})}
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    run, cfg = restart(monkeypatch, tmp_path, loaded['run'])
    repo = loaded['repo']
    assert cfg['workers'] == ['onsite-1', 'onsite-2', 'route-host-1']
    assert cfg['worker_roles']['route-host-1'] == str((repo / 'teams' / 'host.md').resolve())
    (sub,) = cfg['subfleets']  # the root daemon starts it like any autostart row; its roster stays in the repo
    assert sub['name'] == 'route-home' and sub['autostart'] is True and sub['mode'] == 'service'
    assert sub['roster'] == str((repo / 'teams' / 'home.yaml').resolve())
    follow(run)
    assert main(['unload', 'route'])[0] == 0
    data = yaml.safe_load(Path(cfg['roster']).read_text())
    assert 'subfleets' not in data and [w['name'] for w in data['workers']] == ['onsite-1', 'onsite-2']


def test_an_unload_that_timed_out_is_not_loaded_again(loaded, monkeypatch, tmp_path):
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    record = json.loads((loaded['run'] / peer.RECORDS / 'route.json').read_text())
    loaded['roster'].write_text(record['before'])  # unload put the rows back, then timed out keeping the record
    run, cfg = restart(monkeypatch, tmp_path, loaded['run'])
    assert applet_names(cfg['roster']) == []
    assert not (run / peer.RECORDS / 'route.json').exists()
    assert peer.run_loads(run) is None


# -- a record that fails the load checks -------------------------------------------------------------------

def test_a_broken_record_is_not_started_and_is_listed(loaded, monkeypatch, tmp_path, capsys):
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    (loaded['repo'] / 'route_field.py').write_text('print(2)\n')  # an applet file changed in the checkout
    run, cfg = restart(monkeypatch, tmp_path, loaded['run'])
    assert applet_names(cfg['roster']) == [] and not (run / peer.RECORDS / 'route.json').exists()
    out = capsys.readouterr().out
    assert 'route' in out and 'not loaded' in out and 'uncommitted changes to applet files' in out
    loads = peer.run_loads(run)
    assert list(loads['skipped']) == ['route'] and 'uncommitted' in loads['skipped']['route']
    assert loads['loaded'] == []
    # the operator fixes the checkout and loads it by hand: the listing goes
    git(loaded['repo'], 'checkout', '--', 'route_field.py')
    follow(run)
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    assert peer.run_loads(run) == {'loaded': ['route'], 'skipped': {}}


def test_a_skipped_record_is_tried_again_on_the_next_start(loaded, monkeypatch, tmp_path):
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    (loaded['repo'] / 'route_field.py').write_text('print(2)\n')
    first, _ = restart(monkeypatch, tmp_path, loaded['run'])
    git(loaded['repo'], 'checkout', '--', 'route_field.py')
    second, cfg = restart(monkeypatch, tmp_path, first)
    assert applet_names(cfg['roster']) == ['applet-tunnel-1']
    assert peer.run_loads(second) == {'loaded': ['route'], 'skipped': {}}


def test_unload_drops_a_skipped_record(loaded, monkeypatch, tmp_path):
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    (loaded['repo'] / 'route_field.py').write_text('print(2)\n')
    run, _ = restart(monkeypatch, tmp_path, loaded['run'])
    follow(run)
    code, out, err = main(['unload', 'route'])
    assert code == 0, err
    assert peer.run_loads(run) is None


def test_a_workflow_that_no_longer_validates_is_skipped(loaded, monkeypatch, tmp_path):
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    fs.save_workflow('route', dict(fs.workflow('route'), kind='note'))
    run, cfg = restart(monkeypatch, tmp_path, loaded['run'])
    assert applet_names(cfg['roster']) == []
    assert 'kind: note' in peer.run_loads(run)['skipped']['route']
    follow(run)
    code, _, err = main(['unload', 'route'])  # the way out the message names works without a valid workflow
    assert code == 0, err
    assert peer.run_loads(run) is None


def test_a_malformed_record_or_inventory_never_stops_the_start(loaded, monkeypatch, tmp_path):
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    (loaded['run'] / peer.RECORDS / 'bad.json').write_text(json.dumps({'added': {'applets': [7]}, 'before': '',
                                                                       'after': ''}))
    old = loaded['roster'].read_text()
    loaded['roster'].write_text(old + '  - 7\n')  # its "row" is in the old copy, so it counts as in effect
    run, cfg = restart(monkeypatch, tmp_path, loaded['run'])
    loads = peer.run_loads(run)
    assert 'bad' in loads['skipped'] and loads['loaded'] == ['route']
    assert applet_names(cfg['roster']) == ['applet-tunnel-1']
    path = Path(os.environ[fs.ENV])
    path.write_text('machines: [not, a, mapping]\n')  # an inventory the commands cannot read
    second, cfg = restart(monkeypatch, tmp_path, run)
    assert applet_names(cfg['roster']) == [] and set(peer.run_loads(second)['skipped']) == {'bad', 'route'}


def test_a_task_start_keeps_the_records_for_the_next_service_start(loaded, monkeypatch, tmp_path):
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    argv = fake_start(monkeypatch, tmp_path) + ['--roster', str(home_roster(tmp_path))]  # task mode (dev_task)
    point_latest_at(loaded['run'])
    fleet.main(argv)
    task_run = Path(fleet.load(fleet.LATEST)['run'])
    assert 'applets' not in yaml.safe_load((task_run / 'roster.yaml').read_text())  # a task fleet hosts none
    assert 'task fleet' in peer.run_loads(task_run)['skipped']['route']
    run, cfg = restart(monkeypatch, tmp_path, task_run)
    assert applet_names(cfg['roster']) == ['applet-tunnel-1']
    assert peer.run_loads(run) == {'loaded': ['route'], 'skipped': {}}


def test_a_name_already_in_the_home_roster_is_skipped(loaded, monkeypatch, tmp_path):
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    path = home_roster(tmp_path)
    argv = service_start(monkeypatch, tmp_path) + ['--roster', str(path)]
    point_latest_at(loaded['run'])
    path.write_text(ROSTER + 'applets:\n  - {name: applet-tunnel-1, command: [x]}\n')
    fleet.main(argv)
    run = Path(fleet.load(fleet.LATEST)['run'])
    cfg = fleet.load(run / 'config.json')
    assert applet_names(cfg['roster']) == ['applet-tunnel-1']  # the home roster's own row, once
    assert 'duplicate applet name' in peer.run_loads(run)['skipped']['route']


# -- load / unload right after a restart, before the inventory names the new run ---------------------------

def stale_inventory(loaded, tmp_path):
    """The old run's config names the fleet home whose latest.json the restart rewrote (as on a real machine);
    the inventory row still names the old run (the daemon refreshes it about 30 s after start)."""
    path = loaded['run'] / 'config.json'
    path.write_text(json.dumps(dict(json.loads(path.read_text()), fleet_home=str(tmp_path))))


def test_restart_then_load_unload_load_agree(loaded, monkeypatch, tmp_path):
    stale_inventory(loaded, tmp_path)
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    run, cfg = restart(monkeypatch, tmp_path, loaded['run'])
    copy, old = Path(cfg['roster']), loaded['roster'].read_text()
    code, _, err = main(['load', 'route', '--poll', '0'])
    assert code == 1 and 'already loaded' in err  # the record belongs to the new run
    code, _, err = main(['unload', 'route'])
    assert code == 0, err
    assert 'applets' not in yaml.safe_load(copy.read_text()) and peer.run_loads(run) is None
    code, _, err = main(['load', 'route', '--poll', '0'])
    assert code == 0, err
    assert applet_names(copy) == ['applet-tunnel-1'] and peer.run_loads(run) == {'loaded': ['route'], 'skipped': {}}
    assert loaded['roster'].read_text() == old  # the stopped run is not edited again


def test_a_record_left_in_a_stopped_run_is_stale_for_load_and_unload_alike(loaded, monkeypatch, tmp_path):
    stale_inventory(loaded, tmp_path)
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    left = (loaded['run'] / peer.RECORDS / 'route.json').read_text()
    run, cfg = restart(monkeypatch, tmp_path, loaded['run'])
    peer.forget(run / peer.RECORDS)  # a start of an older release: nothing carried over ...
    peer._write(Path(cfg['roster']), json.loads(left)['before'])  # the stub host stops what it no longer lists
    (loaded['run'] / peer.RECORDS / 'route.json').write_text(left)  # ... and the old record still there
    code, _, err = main(['unload', 'route'])
    assert code == 1 and 'not loaded' in err and 'stale' in err
    assert not (loaded['run'] / peer.RECORDS / 'route.json').exists()
    (loaded['run'] / peer.RECORDS / 'route.json').write_text(left)
    code, _, err = main(['load', 'route', '--poll', '0'])
    assert code == 0 and 'stale' in err
    assert applet_names(cfg['roster']) == ['applet-tunnel-1']
    assert not (loaded['run'] / peer.RECORDS / 'route.json').exists()


# -- the daemon starts the applet from the re-applied copy -------------------------------------------------

from test_message_bus_v2 import private_redis_v2  # noqa: E402,F401  (fixture: the repo's private test Redis)
from test_runtime_v2 import fx  # noqa: E402,F401  (a v2 service fleet)


def spawned_by_the_host(f, monkeypatch, copy):
    """One daemon pass over ``copy``: the roster reload's applet list, then the real applet host tick."""
    from fleetmux import applet_host
    started = []
    monkeypatch.setattr(applet_host, 'clear_stale', lambda *a, **k: None)
    f.applet_clock = lambda: 1000.0
    f.applet_spawn = lambda cfg, spec, token: started.append(spec['name']) or SimpleNamespace(pid=9_000_001,
                                                                                            poll=lambda: None)
    f.applet_specs = roster_mod.applets(Path(copy).read_bytes(), Path(copy).parent)  # as reload_roster does
    applet_host.tick(f)
    return started


def test_the_applet_is_started_after_a_fresh_start(loaded, fx, monkeypatch, tmp_path):
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    _, cfg = restart(monkeypatch, tmp_path, loaded['run'])
    assert spawned_by_the_host(fx, monkeypatch, fleet.roster_file(cfg)) == ['applet-tunnel-1']


def test_status_lists_loaded_and_skipped_workflows(fx, capsys, monkeypatch):
    records = Path(fx.cfg['run']) / peer.RECORDS
    (records / peer.SKIPPED).mkdir(parents=True)
    (records / 'route.json').write_text(json.dumps({'added': {'applets': []}}))
    (records / peer.SKIPPED / 'other.json').write_text(json.dumps({'added': {}, 'reason': 'checkout moved'}))
    monkeypatch.setattr('fleetmux.control_view.snapshot', lambda cfg: {'agents': {}})
    fleet.status(fx.cfg)
    out = json.loads(capsys.readouterr().out)
    assert out['superpowers_load'] == {'loaded': ['route'], 'skipped': {'other': 'checkout moved'}}


# -- start --resume ----------------------------------------------------------------------------------------

def resumable(run, roster):
    cfg = dict(json.loads((run / 'config.json').read_text()), python=sys.executable, workers=['onsite-1', 'onsite-2'],
               lifecycle='service', mode='service', supervisor_cli='codex', worker_cli='claude', transport_version=2,
               allow_development_v2=True, roster=str(roster))
    fleet.save(run / 'config.json', cfg)


def resume_stubs(monkeypatch, tmp_path):
    from fleetmux import cmux_manager
    monkeypatch.setattr(fleet, 'daemon_running', lambda path: False)
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: True)
    monkeypatch.setattr(fleet, 'connect', lambda cfg: object())
    mgr = SimpleNamespace(spawn_window=lambda *a, **kw: SimpleNamespace(success=True))
    monkeypatch.setattr(fleet, 'manager_for', lambda cfg: mgr)


def test_resume_with_the_home_roster_loads_it_again(loaded, fx, monkeypatch, tmp_path):
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    resumable(loaded['run'], loaded['roster'])
    resume_stubs(monkeypatch, tmp_path)
    home = home_roster(tmp_path)
    assert fleet.resume(loaded['run'], SimpleNamespace(max_attempts=None, advisor=False, roster=str(home)))
    cfg = fleet.load(loaded['run'] / 'config.json')
    copy = Path(cfg['roster'])
    assert copy == loaded['roster'].resolve() and applet_names(copy) == ['applet-tunnel-1']
    record = json.loads((loaded['run'] / peer.RECORDS / 'route.json').read_text())
    assert record['after'] == copy.read_text() and 'liaison.md' in record['before']
    assert spawned_by_the_host(fx, monkeypatch, copy) == ['applet-tunnel-1']
    assert main(['unload', 'route'])[0] == 0
    assert copy.read_text() == record['before']


def test_resume_without_a_roster_leaves_the_record_alone(loaded, monkeypatch, tmp_path):
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    resumable(loaded['run'], loaded['roster'])
    resume_stubs(monkeypatch, tmp_path)
    path = loaded['run'] / peer.RECORDS / 'route.json'
    before = path.read_bytes(), loaded['roster'].read_bytes()
    assert fleet.resume(loaded['run'], SimpleNamespace(max_attempts=None, advisor=False, roster=None))
    assert (path.read_bytes(), loaded['roster'].read_bytes()) == before
    assert main(['unload', 'route'])[0] == 0
    assert loaded['roster'].read_text() == ROSTER


def test_the_docs_name_the_restart_rule():
    root = Path(__file__).resolve().parents[1]
    text = (root / 'docs' / 'distributed-development.md').read_text()
    assert 'survives a restart of the host fleet' in text
    assert 'survives a restart of the host fleet' in (root / 'docs' / 'design.md').read_text()


def test_a_kit_fleet_start_loads_it_into_its_fleet_config_copy(loaded, monkeypatch, tmp_path):
    from test_kit_fleet import kit_with_fleet, writable
    assert main(['load', 'route', '--poll', '0'])[0] == 0
    kit = kit_with_fleet(tmp_path / 'kit')
    try:
        argv = service_start(monkeypatch, tmp_path) + ['--worker-kit', str(kit), '--kit-fleet', 'demo']
        point_latest_at(loaded['run'])
        fleet.main(argv)
    finally:
        writable(kit)
    run = Path(fleet.load(fleet.LATEST)['run'])
    cfg = fleet.load(run / 'config.json')
    assert cfg['roster'] == str((run / 'fleet-config').resolve() / 'roster.yaml')
    assert applet_names(cfg['roster']) == ['applet-tunnel-1']
