"""A service fleet started while a root service fleet runs becomes a service sub-fleet of that root (design §29 / §58).

`fleetmux start` of a service fleet sends the root the op ``subfleet.attach``: the program declares the child at run
time from the start options (no roster edit, no supervisor approval), gives it an idle worker (or adds one) as its
supervisor, brings it up like an autostart entry and answers the op with the child's address. ``subfleet.detach``
takes it down again. Only private test Redis, fake cmux, fake CLIs (fixtures of test_subfleet).
"""
import json
import uuid
from pathlib import Path

import pytest

from test_subfleet import (adopt_child, drive, hook, result, step, stop_child_like_daemon, supervisor_texts,  # noqa: F401
                           svc, write_roster)
from test_named_subfleet_chain import named_chain_as_before  # noqa: F401  (fixture)
from fleetmux import fleet, named_subfleet, subfleet


def op(t, kind, name='docs', rid=None, **extra):
    rid = rid or kind + '-' + uuid.uuid4().hex[:8]
    ev = dict(kind='request', spec=dict(id=rid, op='subfleet.' + kind, name=name, **extra))
    return rid, drive(t, lambda: fleet.request(t.f.cfg, ev))


@pytest.fixture
def root(svc):
    """The root service fleet of test_subfleet.svc with a roster that declares no sub-fleet."""
    write_roster(svc, declare=False)
    svc.f.roster_tick()
    return svc


def attach_up(t, name='docs', **extra):
    rid, answer = op(t, 'attach', name, **extra)
    assert answer['status'] == 'accepted', answer
    subfleet.tick(t.f)  # a slot, then the program's own approved up
    subfleet.tick(t.f)
    assert named_subfleet.state(t.f, name)['state'] == 'starting'
    rec, cf = adopt_child(t, name)
    subfleet.tick(t.f)
    return rid, rec, cf


@pytest.mark.usefixtures('named_chain_as_before')  # §29 chains: the attached child's slot keeps its model
def test_attach_brings_a_service_child_up_on_an_idle_worker_and_answers_its_address(root):
    t, f = root, root.f
    rid, rec, cf = attach_up(t, workers=2)
    res = result(f, rid)
    child_cfg = fleet.load(Path(rec['run']) / 'config.json')
    assert res['status'] == 'ok' and res['state'] == 'up' and res['run_dir'] == str(f.run_dir / 'sub' / 'docs')
    assert res['redis_socket'] == child_cfg['redis_socket']
    assert rec['slot'] == 'worker-1' and f.agent('worker-1')['state'] == 'delegated'
    assert child_cfg['mode'] == 'service' and child_cfg['lifecycle'] is None
    assert child_cfg['workers'] == ['worker-1', 'worker-2'] and child_cfg['named']['autostart'] is True
    assert 'docs' not in (f.cfg.get('subfleets') or []) and 'docs' not in (t.tmp / 'roster.yaml').read_text()
    assert not any(rid in x for x in supervisor_texts(f))  # never asked the root supervisor
    # a request sent to the child's own bus reaches the child supervisor
    spec = dict(id='q1', payload={'message': 'index the docs'}, reply={'required': ['reply']})
    assert drive(t, lambda: fleet.request(child_cfg, dict(kind='request', spec=spec)))['status'] == 'accepted'
    step(t)
    assert any('index the docs' in x for x in supervisor_texts(cf))


@pytest.mark.usefixtures('named_chain_as_before')  # §29 chains: the attached child's slot keeps its model
def test_a_roster_reload_keeps_the_attached_child_up(root):
    t, f = root, root.f
    _, rec, cf = attach_up(t)
    write_roster(t, declare=False)  # the root's roster changes (as a hot-added worker row would)
    f.roster_tick()
    subfleet.tick(f)
    assert named_subfleet.state(f, 'docs')['state'] == 'up'
    assert subfleet.record(rec['run'])['state'] == 'running'


@pytest.mark.usefixtures('named_chain_as_before')  # §29 chains: the attached child's slot keeps its model
def test_detach_takes_only_that_child_down(root):
    t, f = root, root.f
    _, rec, cf = attach_up(t)
    rid, answer = op(t, 'detach')
    assert answer['status'] == 'accepted'
    subfleet.tick(f)
    assert named_subfleet.state(f, 'docs')['state'] == 'draining'
    subfleet.tick(f)  # quiet: stop posted to the child
    stop_child_like_daemon(cf)
    subfleet.tick(f)  # collected
    assert result(f, rid)['status'] == 'ok' and result(f, rid)['state'] == 'down'
    assert subfleet.record(rec['run'])['state'] == 'down' and not named_subfleet.attached(f)
    assert not getattr(f, 'stopped', False) and f.r.hget(fleet.SERVICE, 'paused') != '1'  # the root runs on
    _, again = op(t, 'detach')
    assert again['status'] == 'rejected' and 'not an attached sub-fleet' in again['reason']


@pytest.mark.usefixtures('named_chain_as_before')  # §29 chains: the attached child's slot keeps its model
def test_root_stop_takes_the_attached_child_down(root):
    t, f = root, root.f
    rid, answer = op(t, 'attach')
    subfleet.tick(f)
    subfleet.tick(f)
    rec = subfleet.record(f.run_dir / 'sub' / 'docs')
    assert rec['state'] == 'running'
    assert subfleet.stop_children(f, wait=0)
    assert subfleet.record(rec['run'])['state'] == 'down'
    assert named_subfleet.state(f, 'docs')['state'] == 'down'


@pytest.mark.parametrize('name, extra, words', [
    ('worker-1', {}, 'worker name'),
    ('supervisor', {}, 'must match'),
    ('Docs', {}, 'must match'),
    ('docs', {'workers': 0}, 'positive integer'),
    ('docs', {'roster': 'relative.yaml'}, 'absolute path'),
    ('docs', {'entry': {'timeouts': {}}}, 'entry takes only'),
    ('docs', {'colour': 'red'}, 'unknown keys'),
])
def test_attach_is_refused_at_entry(root, name, extra, words):
    _, answer = op(root, 'attach', name, **extra)
    assert answer['status'] == 'rejected' and words in answer['reason'], answer
    assert not named_subfleet.attached(root.f)


def test_attach_name_clash_with_a_declared_or_attached_sub_fleet_is_refused(svc):
    t, f = svc, svc.f  # its roster declares sub-fleet ops
    _, answer = op(t, 'attach', 'ops')
    assert answer['status'] == 'rejected' and 'already declared' in answer['reason']
    _, first = op(t, 'attach', 'docs')
    _, second = op(t, 'attach', 'docs')
    assert first['status'] == 'accepted' and second['status'] == 'rejected' and 'already declared' in second['reason']


@pytest.mark.usefixtures('named_chain_as_before')  # §29 chains: the attached child's slot keeps its model
def test_attach_skips_a_roster_supervisor_slot_and_a_job_never_takes_its_slot(svc):
    t, f = svc, svc.f  # ops is declared on worker-1
    op(t, 'attach')
    subfleet.tick(f)
    assert named_subfleet.attached(f)['docs']['entry']['supervisor_slot'] == 'worker-2'
    assert 'attached sub-fleet docs' in f.job_slot_busy('worker-2')


def test_no_idle_worker_adds_one_and_detach_removes_it(root):
    t, f = root, root.f
    f.setf('worker-1', state='busy')
    f.setf('worker-2', state='busy')
    rid, _ = op(t, 'attach')
    subfleet.tick(f)
    rec = named_subfleet.attached(f)['docs']
    path = Path(f.cfg['roster'])
    assert rec['hot_slot'] == 'worker-3' and rec['entry']['supervisor_slot'] == 'worker-3'
    assert '- {name: worker-3}' in path.read_text()
    f.roster_tick()  # the reload starts it
    assert 'worker-3' in f.workers
    assert hook(t, f, 'worker-3', 'READY', prompt='startup')['status'] == 'ready'
    step(t)
    subfleet.tick(f)
    child = subfleet.record(f.run_dir / 'sub' / 'docs')
    assert child['slot'] == 'worker-3' and child['state'] == 'running'
    op(t, 'detach')
    subfleet.tick(f)
    subfleet.stop_children(f, wait=0)  # its daemon never booted here: collected at once
    subfleet.tick(f)
    assert 'worker-3' not in path.read_text() and not f.r.hgetall(named_subfleet.DETACHED)


@pytest.mark.usefixtures('named_chain_as_before')  # §29 chains: the attached child's slot keeps its model
def test_attached_children_have_no_default_sub_fleet_cap(root):
    t, f = root, root.f
    assert subfleet.limits(f.cfg)['child_workers_max'] == 3  # a split's default
    _, answer = op(t, 'attach', workers=5)
    assert answer['status'] == 'accepted', answer
    subfleet.tick(f)
    subfleet.tick(f)
    assert subfleet.record(f.run_dir / 'sub' / 'docs')['state'] == 'running'


@pytest.mark.usefixtures('named_chain_as_before')  # §29 chains: the attached child's slot keeps its model
def test_attached_child_survives_in_the_store_and_its_roles_reach_the_child(root, tmp_path):
    t, f = root, root.f
    roles = tmp_path / 'roles'
    roles.mkdir()
    (roles / 'worker-1.md').write_text('ROLE-MARKER-DOCS')
    rid, rec, cf = attach_up(t, roles=str(roles))
    child_cfg = fleet.load(Path(rec['run']) / 'config.json')
    assert child_cfg['roles'] == str(roles.resolve())
    stored = json.loads(f.r.get(named_subfleet.ATTACHED))
    assert stored['docs']['id'] == rid and stored['docs']['entry']['attached'] is True


def test_a_worker_added_for_an_attached_child_is_not_refused_by_the_reload(root, monkeypatch):
    """On a manager with tmux views the added worker's tmux mark must not create its agent hash before the roster
    reload: a name with agent state counts as used and the reload would refuse the whole roster."""
    t, f = root, root.f
    monkeypatch.setattr(f.mgr, 'tmux_view_info', lambda *a, **kw: None, raising=False)
    f.setf('worker-1', state='busy')
    f.setf('worker-2', state='busy')
    op(t, 'attach')
    subfleet.tick(f)
    assert named_subfleet.attached(f)['docs']['hot_slot'] == 'worker-3'
    assert not f.r.hgetall(fleet.key('agent', 'worker-3'))  # nothing written for it before the reload
    f.roster_tick()
    assert not f.r.hget(fleet.ROSTER, 'rejected') and 'worker-3' in f.workers
    assert f.agent('worker-3').get('tmux_view') == '1'  # marked when it was created


@pytest.mark.usefixtures('named_chain_as_before')  # §29 chains: the attached child's slot keeps its model
def test_detach_inside_the_request_writes_no_drain_the_tick_drains(root):
    t, f = root, root.f
    _, rec, cf = attach_up(t)
    op(t, 'detach')
    assert named_subfleet.state(f, 'docs')['state'] == 'up'  # the request event only records the detach
    subfleet.tick(f)
    assert named_subfleet.state(f, 'docs')['state'] == 'draining'


@pytest.mark.usefixtures('named_chain_as_before')  # §29 chains: the attached child's slot keeps its model
def test_a_restarted_root_daemon_keeps_the_attached_child(root):
    t, f = root, root.f
    _, rec, cf = attach_up(t)
    restarted = fleet.Fleet(f.path, f.r, f.mgr, sessions=t.sessions)  # a new daemon on the same Redis (--resume)
    assert 'docs' in named_subfleet.declared(restarted)
    restarted.roster_tick()
    subfleet.tick(restarted)
    assert named_subfleet.state(f, 'docs')['state'] == 'up'


@pytest.mark.usefixtures('named_chain_as_before')  # §29 chains: the attached child's slot keeps its model
def test_the_op_start_sends_is_taken_by_a_real_root(root):
    from types import SimpleNamespace
    from fleetmux import join
    t, f = root, root.f
    args = SimpleNamespace(workers=2, worker_names=None, worker_model=None, claude=None, worker_cli=None)
    options = {'roster': None, 'roles': None, 'name': 'Docs Index'}
    spec = join.attach_spec(args, options, join.child_name(options['name']))
    answer = drive(t, lambda: fleet.request(f.cfg, dict(kind='request', spec=spec)))
    assert answer == {'status': 'accepted', 'id': spec['id']}
    subfleet.tick(f)
    subfleet.tick(f)
    assert subfleet.record(f.run_dir / 'sub' / 'docs-index')['state'] == 'running'


@pytest.mark.parametrize('name', ['opq', 'op'])
def test_names_of_the_sub_fleet_store_keys_are_refused(root, name):
    _, answer = op(root, 'attach', name)
    assert answer['status'] == 'rejected' and 'reserved' in answer['reason']


def test_a_job_may_still_take_a_roster_declared_slot(svc):
    assert svc.f.job_slot_busy('worker-1') == ''  # ops on worker-1 is down: unchanged for roster entries
