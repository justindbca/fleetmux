"""Gaps of attached (``fleetmux start`` while a root runs) and delegated sub-fleets.

Only private test Redis, fake cmux and fake CLIs (fixtures of test_subfleet / test_service_attach).
"""
from pathlib import Path

from test_service_attach import attach_up, op, root  # noqa: F401  (fixture)
from test_subfleet import hook, result, step, stop_child_like_daemon, svc  # noqa: F401  (fixture)
from fleetmux import fleet, named_subfleet, remote, subfleet


# ------------------------------------------------------------------ a drain with a dead child supervisor
def test_detach_ends_the_drain_at_once_when_the_child_supervisor_is_dead(root):
    t, f = root, root.f
    _, rec, cf = attach_up(t)
    cf.r.rpush(fleet.REQQ, 'stuck')  # work in flight: the child is never quiet
    cf.setf('supervisor', state='dead')  # e.g. its session died with the network
    rid, answer = op(t, 'detach')
    assert answer['status'] == 'accepted'
    assert remote.result(f.r, rid)[0] == remote.PENDING  # `fleetmux result <detach id>` knows it
    subfleet.tick(f)
    child = subfleet.record(rec['run'])
    assert child['state'] == 'stopping' and 'supervisor is dead' in child['reason']
    assert result(f, rid) is None and remote.result(f.r, rid)[0] == remote.PENDING
    stop_child_like_daemon(cf)
    subfleet.tick(f)
    res = result(f, rid)
    assert res['status'] == 'ok' and res['state'] == 'down' and 'supervisor is dead' in res['reason']
    assert remote.result(f.r, rid)[0] == remote.FOUND


def test_detach_with_a_live_busy_supervisor_still_drains(root):
    t, f = root, root.f
    _, rec, cf = attach_up(t)
    cf.r.rpush(fleet.REQQ, 'stuck')
    op(t, 'detach')
    subfleet.tick(f)
    subfleet.tick(f)
    assert subfleet.record(rec['run'])['state'] == 'running'
    assert named_subfleet.state(f, 'docs')['state'] == 'draining'


def test_an_attach_id_is_pending_until_the_child_is_up(root):
    t, f = root, root.f
    rid, answer = op(t, 'attach')
    assert answer['status'] == 'accepted'
    assert remote.result(f.r, rid)[0] == remote.PENDING
    assert remote.result(f.r, 'attach-nobody')[0] == remote.UNKNOWN


# ------------------------------------------------------------------ the service stub: applet / logs / own config
from types import SimpleNamespace  # noqa: E402

from test_start_join import (alive, make_run, root_cli, run_main, scan, service_args, service_stub,  # noqa: E402,F401
                             start_world, stubbed)
from fleetmux import applet_cli, control_view  # noqa: E402


def test_attach_output_prints_the_child_config_path(tmp_path, monkeypatch, scan, alive, capsys):  # noqa: F811
    root_cfg = make_run(scan / 'fleets' / 'svc', 'fleet-20260101-000000-root', alive=alive)
    args, _, _, _ = start_world(tmp_path, monkeypatch, join=True)
    root_cli(monkeypatch)
    fleet.main(service_args(args, ['--lifecycle', 'service']) + ['--name', 'docs'])
    child = Path(fleet.load(root_cfg)['run']) / 'sub' / 'docs' / 'config.json'
    assert str(child) in capsys.readouterr().out
    assert fleet.load(tmp_path / 'latest.json')['join']['child_config'] == str(child)


def test_logs_and_operator_on_a_service_stub_run_on_the_child(service_stub, monkeypatch, capsys):  # noqa: F811
    t, seen = service_stub, []
    monkeypatch.setattr(control_view, 'iter_events', lambda cfg, **kw: seen.append(cfg['run']) or iter(()))
    assert run_main(['logs', '--config', str(t.path)], monkeypatch) == 0
    monkeypatch.setattr(fleet, 'operator_send', lambda cfg, text, wait=30: seen.append(cfg['run'])
                        or {'status': 'accepted'})
    assert run_main(['operator', 'hello', '--config', str(t.path)], monkeypatch) == 0
    assert seen == [str(t.child), str(t.child)] and t.calls == []


def test_logs_on_a_service_stub_before_the_child_is_up_is_refused(service_stub, monkeypatch, capsys):  # noqa: F811
    service_stub.alive.discard('docs')
    assert run_main(['logs', '--config', str(service_stub.path)], monkeypatch) == 2
    assert 'not up' in capsys.readouterr().err


class _NoRedis:
    def close(self):
        pass

    connection_pool = SimpleNamespace(disconnect=lambda: None)


def test_applet_on_a_service_stub_runs_on_the_child(service_stub, monkeypatch, capsys):  # noqa: F811
    seen = []
    monkeypatch.setattr(fleet, 'connect', lambda cfg: _NoRedis())
    monkeypatch.setitem(applet_cli.COMMANDS, 'list', applet_cli.Command(
        lambda cfg, r, args: seen.append(cfg['run']) or (0, {'status': 'ok'}), None, ''))
    assert applet_cli.main(['list', '--config', str(service_stub.path)]) == 0
    assert seen == [str(service_stub.child)]


def test_applet_on_a_task_stub_is_refused(stubbed, monkeypatch, capsys):  # noqa: F811
    assert applet_cli.main(['list', '--config', str(stubbed.path)]) == 2
    assert 'join stub for jobs' in capsys.readouterr().out


# ------------------------------------------------------------------ a detached child: no viewer, row says detached
def _pages(monkeypatch, f):
    calls = []
    monkeypatch.setattr(subfleet, 'on_fleetmux', lambda cfg: True)
    monkeypatch.setattr(f.mgr, 'archive_page', lambda page, run, title: calls.append(('archive', page)) or True,
                        raising=False)
    monkeypatch.setattr(f.mgr, 'unarchive_page', lambda page: calls.append(('unarchive', page)) or True,
                        raising=False)
    return calls


def _bind_page(rec):
    path = Path(rec['run']) / 'config.json'
    cfg = fleet.load(path)
    cfg['fleetmux_page'] = rec['task_id']
    fleet.save(path, cfg)


def test_a_detached_child_keeps_no_history_viewer_and_shows_detached(root, monkeypatch):
    t, f = root, root.f
    _, rec, cf = attach_up(t)
    _bind_page(rec)
    calls = _pages(monkeypatch, f)
    op(t, 'detach')
    subfleet.tick(f)
    stop_child_like_daemon(cf)
    subfleet.tick(f)
    assert calls == [('unarchive', 'docs')]  # no record page with a history_view tab; an old one is killed
    row = subfleet.tree(f.cfg)['docs']
    assert row['state'] == 'detached' and row['detached'] is True and row['autostart'] is False
    subfleet.resume_children(f)  # a root --resume rebuilds no page for it either
    assert ('archive', 'docs') not in calls


def test_detaching_a_child_that_is_already_down_marks_it_and_drops_its_page(root, monkeypatch):
    t, f = root, root.f
    _, rec, cf = attach_up(t)
    _bind_page(rec)
    calls = _pages(monkeypatch, f)
    subfleet.stop_children(f, wait=0)  # it went down (here: with a root stop) and stays declared
    stop_child_like_daemon(cf)
    assert subfleet.stop_children(f, wait=0)
    assert subfleet.tree(f.cfg)['docs']['state'] == 'down' and calls == [('archive', 'docs')]
    rid, answer = op(t, 'detach')
    assert answer['status'] == 'accepted'
    subfleet.tick(f)
    assert result(f, rid)['status'] == 'ok'
    row = subfleet.tree(f.cfg)['docs']
    assert row['state'] == 'detached' and row['autostart'] is False
    assert calls == [('archive', 'docs'), ('unarchive', 'docs')] and not f.r.hgetall(named_subfleet.DETACHED)


def test_an_attached_child_stopped_with_the_root_keeps_its_record_page(root, monkeypatch):
    t, f = root, root.f
    _, rec, cf = attach_up(t)
    _bind_page(rec)
    calls = _pages(monkeypatch, f)
    subfleet.stop_children(f, wait=0)  # not detached: it comes back with the root's --resume
    stop_child_like_daemon(cf)
    assert subfleet.stop_children(f, wait=0)
    assert calls == [('archive', 'docs')]
    assert subfleet.tree(f.cfg)['docs']['state'] == 'down'


# ------------------------------------------------------------------ applet state of an attached child
import json  # noqa: E402

from fleetmux import applet_host  # noqa: E402


def test_an_attached_childs_applet_state_is_keyed_by_the_sub_fleet(root):
    t, f = root, root.f
    _, rec, _ = attach_up(t)
    child_cfg = fleet.load(Path(rec['run']) / 'config.json')
    spec = {'name': 'applet-route-1'}
    home = Path(f.cfg.get('fleet_home') or fleet.HOME)
    assert applet_host.state_dir(child_cfg, spec) == home / 'subfleets' / 'docs' / 'applets' / 'applet-route-1'
    assert applet_host.state_dir(f.cfg, spec) == home / 'applets' / 'applet-route-1'  # the root's: another place
    env = applet_host.environment(child_cfg, spec, 'tok', base={})
    assert env['FLEETMUX_APPLET_STATE'] == str(home / 'subfleets' / 'docs' / 'applets' / 'applet-route-1')


def test_a_roster_named_sub_fleet_keeps_the_shared_state_directory(tmp_path):
    cfg = {'fleet_home': str(tmp_path), 'run': str(tmp_path / 'run'), 'named': {'name': 'ops', 'autostart': True}}
    assert applet_host.state_dir(cfg, {'name': 'applet-x-1'}) == tmp_path / 'applets' / 'applet-x-1'


def _host(tmp_path):
    cfg = {'fleet_home': str(tmp_path / 'home'), 'run': str(tmp_path / 'run'), 'redis_socket': str(tmp_path / 'no.sock'),
           'named': {'name': 'docs', 'autostart': True, 'attached': True}}
    path = tmp_path / 'config.json'
    fleet.save(path, cfg)
    state = tmp_path / 'home' / 'subfleets' / 'docs' / 'applets' / 'applet-route-1'
    (state / 'queue').mkdir(parents=True)
    (state / 'queue' / 'item-1.json').write_text('{}')
    (state / 'cursor').write_text('7')
    return path, state


def test_clear_state_lists_without_yes_and_deletes_with_it_while_the_host_is_down(tmp_path, capsys):
    path, state = _host(tmp_path)
    assert applet_cli.main(['clear-state', 'applet-route-1', '--config', str(path)]) == 0  # listed, nothing removed
    out = json.loads(capsys.readouterr().out)
    assert out['status'] == 'ok' and out['removed'] == [] and '--yes' in out['note']
    assert out['files'] == ['cursor', 'queue/item-1.json'] and state.is_dir()
    assert applet_cli.main(['clear-state', 'applet-route-1', '--config', str(path), '--yes']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['removed'] == ['cursor', 'queue/item-1.json'] and out['state_dir'] == str(state)
    assert not state.exists()


def test_clear_state_refuses_while_the_applet_runs(tmp_path, monkeypatch, capsys):
    path, state = _host(tmp_path)
    (state / applet_host.OWNER).write_text(json.dumps({'host': 'docs', 'run': 'r', 'pid': 4242}))
    monkeypatch.setattr(applet_host, '_leads_session', lambda pid: pid == 4242)
    assert applet_cli.main(['clear-state', 'applet-route-1', '--config', str(path), '--yes']) == 2
    assert 'is running' in json.loads(capsys.readouterr().out)['reason'] and state.is_dir()
    assert applet_cli.main(['clear-state', 'applet-route-1', '--config', str(path)]) == 0  # the dry run still lists
    out = json.loads(capsys.readouterr().out)
    assert 'is running' in out['note'] and out['removed'] == [] and out['files'] and state.is_dir()


# ------------------------------------------------------------------ the task clock stops while a report_up waits
import time  # noqa: E402

from test_contracts import cf, sup, submit  # noqa: E402,F401  (fixture + helpers)
from test_subfleet_boundaries import ITEM, as_child, as_top, notices, relay_one  # noqa: E402


def test_a_report_up_held_past_the_remaining_budget_does_not_exhaust_it(cf, tmp_path):  # noqa: F811
    parent_run = as_child(cf, tmp_path)
    submit(cf, hours=1)
    sup(cf, {'status': 'RUNNING', 'completion': 10, 'dispatch': [], 'report_up': [ITEM]})
    task = cf.task()
    assert task['report_wait_at'], task  # the clock stopped with the report
    # the top took two hours to rule: past the hour the task had left
    now = time.time()
    cf.r.hset(fleet.TASK, mapping={'report_wait_at': now - 7200, 'deadline': now - 3600})
    assert fleet.task_deadline(cf.task(), now) >= now + 3599
    cf.tick()
    assert cf.task()['status'] == 'running'
    out = {'task': dict(cf.task())}
    fleet.task_clock(out['task'], now)
    assert out['task']['clock_paused_s'] >= 7199 and out['task']['clock'].startswith('paused')
    # the ruling comes back down: the clock runs again, the deadline moved by the wait
    note, = notices(parent_run)
    as_top(cf)
    relay_one(cf, note)
    sup(cf, {'status': 'RUNNING', 'completion': 10, 'dispatch': [],
             'escalations': [{'task': note['id'], 'decision': 'approve', 'note': 'ok'}]})
    as_child(cf, tmp_path)
    cf.apply_vetoes()
    task = cf.task()
    assert task['report_wait_at'] == '' and float(task['report_wait_s']) >= 7199
    assert float(task['deadline']) >= now + 3599
    cf.tick()
    assert cf.task()['status'] == 'running'


def test_without_a_report_the_time_budget_still_runs_out(cf):  # noqa: F811
    submit(cf, hours=1)
    cf.r.hset(fleet.TASK, mapping={'deadline': time.time() - 1})
    cf.tick()
    assert cf.task()['status'] == 'budget_exhausted'


from test_message_bus_v2 import private_redis_v2  # noqa: E402,F401  (fixture)
from test_v2_codex_claude import mixed, submit as submit_v2  # noqa: E402,F401  (fixture)


def test_the_v2_daemon_tick_keeps_a_stopped_clock_running(mixed):  # noqa: F811
    f = mixed
    submit_v2(f)
    now = time.time()
    f.r.hset(fleet.TASK, mapping={'report_wait_at': now - 7200, 'deadline': now - 3600})
    f.v2.tick()
    assert f.task()['status'] == 'running'
    f.r.hset(fleet.TASK, mapping={'report_wait_at': ''})
    f.v2.tick()
    assert f.task()['status'] == 'budget_exhausted'


# ------------------------------------------------------------------ start options an attaching start forwards
import pytest  # noqa: E402

from fleetmux import join  # noqa: E402


def parsed(*argv):
    return fleet.cli().parse_args(['start', '--lifecycle', 'service'] + list(argv))


def test_every_start_option_is_classified_once():
    groups = [set(join.START_TAKEN), set(join.START_LOCAL), set(join.START_MACHINE), set(join.START_FORWARD),
              set(join.START_REFUSED)]
    assert set().union(*groups) == set(join.start_defaults())
    assert sum(len(g) for g in groups) == len(set().union(*groups))  # no option in two groups


def test_fleet_settings_are_forwarded_and_the_roots_kit_is_not_inherited(tmp_path):
    overrides, model, machine = join.start_settings(parsed(
        '--supervisor-split', '--request-timeout', '120', '--max-busy', '1', '--stall-timeout', '900',
        '--handoff-ratio', '0.5', '--worker-idle-reclaim-min', 'off', '--redis-server', '/opt/redis'), service=True)
    assert overrides == {'supervisor_split': True, 'request_timeout': 120.0, 'max_busy': 1, 'stall_timeout': 900.0,
                         'handoff_ratio': 0.5, 'worker_idle_reclaim_min': None, 'worker_kit': None,
                         'worker_domains': []}
    assert model is None and machine == ['redis_server']
    assert join.start_settings(parsed(), service=True)[0] == join.START_RESET  # nothing given: no root kit / split


def test_supervisor_and_worker_settings_are_forwarded(tmp_path):
    kit = tmp_path / 'kit'
    kit.mkdir()
    overrides, model, _ = join.start_settings(parsed('--supervisor-model', 'fable', '--supervisor-cli', 'claude',
                                                     '--worker-kit', str(kit)), service=True)
    assert model == 'fable' and overrides['worker_kit'] == str(kit.resolve())
    claude = str(tmp_path / 'tools' / 'claude')  # not /bin/claude: on merged-/usr Linux /bin is a symlink to usr/bin
    overrides, _, _ = join.start_settings(parsed('--worker-model', 'opus', '--claude', claude), service=False)
    assert overrides['worker_model'] == 'opus' and overrides['claude'] == claude


@pytest.mark.parametrize('argv, service, words', [
    (['--supervisor-cli', 'pi'], True, '--supervisor-cli'),
    (['--supervisor-profile', 'fleet-astra'], True, '--supervisor-profile'),
    (['--profile', 'fleet'], True, '--profile'),
    (['--worker-cli', 'codex'], True, '--worker-cli'),
    (['--night-roster', '/abs/night.yaml'], True, '--night-roster'),
    (['--supervisor-model', 'fable'], False, 'supervisor_profile'),
    (['--supervisor-split'], False, '--supervisor-split'),
])
def test_options_that_cannot_apply_to_a_sub_fleet_are_refused(argv, service, words):
    with pytest.raises(SystemExit) as exc:
        join.start_settings(parsed(*argv), service=service)
    assert words in str(exc.value.code) and 'Nothing was started' in str(exc.value.code)


def test_the_root_applies_the_forwarded_settings_to_the_attached_child(root, tmp_path):
    t, f = root, root.f
    kit = tmp_path / 'root-kit'
    kit.mkdir()
    f.cfg.update(worker_kit=str(kit), supervisor_split=False, request_timeout=None)
    overrides, _, _ = join.start_settings(parsed('--supervisor-split', '--request-timeout', '120', '--max-busy', '1'),
                                          service=True)
    _, rec, _ = attach_up(t, overrides=overrides)
    child = fleet.load(Path(rec['run']) / 'config.json')
    assert child['supervisor_split'] is True and child['request_timeout'] == 120 and child['max_busy'] == 1
    assert child['worker_kit'] is None and child['worker_domains'] == []  # the root's kit stays the root's


def test_an_attach_with_bad_overrides_is_refused(root):
    _, answer = op(root, 'attach', overrides={'colour': 'red'})
    assert answer['status'] == 'rejected' and 'overrides take only' in answer['reason']
    _, answer = op(root, 'attach', overrides={'max_busy': -1})
    assert answer['status'] == 'rejected' and 'bad value for max_busy' in answer['reason']


def test_a_supervisor_model_gets_a_worker_on_that_model(root):
    t, f = root, root.f
    rid, answer = op(t, 'attach', supervisor_model='fable-test')
    assert answer['status'] == 'accepted', answer
    subfleet.tick(f)
    rec = named_subfleet.attached(f)['docs']
    assert rec['hot_slot'] == 'worker-3' and rec['entry']['supervisor_model'] == 'fable-test'
    f.roster_tick()  # the reload starts it, on the asked model
    assert 'worker-3' in f.workers and f.agent('worker-3')['state'] == 'starting'
    snapshot = fleet.load(f.run_dir / 'runtime' / 'worker-3-g0.json')  # what its actor builds the argv from
    assert snapshot['worker_model'] == 'fable-test' and snapshot['worker_cli'] == 'claude'
    assert fleet.claude_model(f.runtime_cfg('worker-1'), 'worker-1') != 'fable-test'
    assert hook(t, f, 'worker-3', 'READY', prompt='startup')['status'] == 'ready'
    step(t)
    subfleet.tick(f)
    child = subfleet.record(f.run_dir / 'sub' / 'docs')
    assert child['slot'] == 'worker-3' and child['state'] == 'running'
    child_cfg = fleet.load(Path(child['run']) / 'config.json')
    assert child_cfg['supervisor_model'] == 'fable-test'  # the adopting slot's model, not the root's


def test_a_child_config_names_the_adopting_slots_model_not_the_roots(root):
    t, f = root, root.f
    f.cfg['supervisor_model'] = 'root-supervisor-model'
    _, rec, _ = attach_up(t)
    child_cfg = fleet.load(Path(rec['run']) / 'config.json')
    assert child_cfg['supervisor_model'] == fleet.claude_model(f.runtime_cfg('worker-1'), 'worker-1')
    assert child_cfg['supervisor_model'] != 'root-supervisor-model'


from test_service_job import dev_job  # noqa: E402
from test_service_split import split_tree  # noqa: E402,F401  (fixture)
from test_escalate import ok_check  # noqa: E402,F401  (fixture)


def test_a_job_from_a_task_stub_takes_the_forwarded_settings(split_tree, tmp_path, monkeypatch):  # noqa: F811
    t, f = split_tree, split_tree.f
    f.cfg.update(worker_kit=str(tmp_path), supervisor_split=True)
    overrides = dict(join.START_RESET, max_busy=1, worker_model='opus-test')
    _, _, cfg, _ = dev_job(t, tmp_path, monkeypatch, overrides=overrides)
    assert cfg['max_busy'] == 1 and cfg['worker_model'] == 'opus-test'
    assert cfg['worker_kit'] is None and cfg['supervisor_split'] is False


def test_a_task_start_keeps_its_settings_in_the_stub_for_its_jobs(tmp_path, monkeypatch, scan, alive):  # noqa: F811
    make_run(scan / 'fleets' / 'svc', 'fleet-20260101-000000-root', alive=alive)
    args, _, _, _ = start_world(tmp_path, monkeypatch, join=True)
    fleet.main(args + ['--name', 'feature x', '--max-busy', '1'])
    stub = fleet.load(tmp_path / 'latest.json')['join']
    assert stub['overrides'] == dict(join.START_RESET, max_busy=1)
    assert join.merged_spec(stub, {'goal': 'g'})['overrides'] == stub['overrides']
