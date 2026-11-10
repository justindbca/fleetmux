"""Exercise the existing daemon and Pi operator-input path with fake infrastructure."""
import copy
import json

import pytest

from cmux_fleet import fleet
from test_fleet import FakeMgr, FakeRedis, decision, fx, last_to, traffic, turn, write_rollout, THREADS
from test_pi import callback, operator_input, pfx, pi_turn


@pytest.fixture
def routed(fx):
    fx.finish('cancelled', 'replace fixture task')
    fx.submit({'goal': 'implement dynamic router', 'auto_agents': True})
    return fx


def dispatch(f, profile='flashnext-coder', **kwargs):
    f.on_supervisor(decision(dispatch=[{
        'to': kwargs.get('to', 'worker-1'),
        'instruction': 'Recommended profile: ' + profile + '\n' + kwargs.get('instruction', 'Do the assigned work.'),
    }]))


def forbid_process_changes(monkeypatch, f):
    def forbidden(*args, **kwargs):
        raise AssertionError('profile application attempted a process change')
    for name in ('spawn', 'start_rotation', 'finish_rotation'):
        monkeypatch.setattr(f, name, forbidden)
    monkeypatch.setattr(f.mgr, 'spawn_interactive_cli', forbidden)
    monkeypatch.setattr(f.mgr, 'kill_pane', forbidden)


def test_advisory_metadata_preserves_identity_runtime_and_role(routed, monkeypatch):
    f = routed
    forbid_process_changes(monkeypatch, f)
    f.setf('worker-1', generation=3, label='worker-1.3')
    f.r.hset(fleet.NAMES, mapping={'worker-1.3': 'worker-1'})
    before = f.agent('worker-1')
    cfg = copy.deepcopy(f.cfg)
    role = fleet.agent_cwd(f.cfg, 'worker-1') / 'AGENTS.md'
    role_before = role.read_text()
    dispatch(f, to='worker-1.3')
    info = f.agent('worker-1')
    assert info['desired_profile'] == info['advisory_profile'] == 'flashnext-coder'
    assert info['profile_state'] == 'applied-metadata'
    assert info['profile_task'] == f.task()['id']
    assert json.loads(info['profile_capabilities']) == ['code-edit', 'test-fix', 'planning']
    f.deliver('worker-1')
    assert last_to(f, 'worker-1').count('[Recommended agent profile:') == 1
    for field in ('label', 'generation', 'pane', 'thread'):
        assert f.agent('worker-1')[field] == before[field]
    assert f.cfg == cfg and role.read_text() == role_before
    assert f.r.llen('fx:inbox:flashnext-coder') == 0


@pytest.mark.parametrize('state', ['busy', 'handoff', 'starting'])
def test_queue_during_unsafe_boundary_waits_for_ready_or_idle(routed, monkeypatch, state):
    f = routed
    forbid_process_changes(monkeypatch, f)
    # A starting session which has already done work is not starting-before-task.
    f.setf('worker-1', state=state, delivered=1)
    before = f.agent('worker-1')
    dispatch(f)
    info = f.agent('worker-1')
    assert info['profile_state'] == 'pending' and info['advisory_profile'] == ''
    assert info['desired_profile'] == 'flashnext-coder'
    sent = traffic(f.mgr)
    f.deliver('worker-1')
    assert traffic(f.mgr) == sent and f.r.llen('fx:inbox:worker-1') == 1
    assert f.agent('worker-1')['state'] == state
    # Simulate the existing completion/READY path reaching an idle boundary.
    f.setf('worker-1', state='idle')
    f.deliver('worker-1')
    info = f.agent('worker-1')
    assert info['profile_state'] == 'applied-metadata' and info['state'] == 'busy'
    assert (info['generation'], info['label']) == (before['generation'], before['label'])


def test_fifo_profile_intent_and_different_profile_remains_pending(routed, monkeypatch):
    f = routed
    forbid_process_changes(monkeypatch, f)
    dispatch(f)
    dispatch(f, 'claude-reviewer')
    assert f.agent('worker-1')['desired_profile'] == 'flashnext-coder'
    f.deliver('worker-1')
    assert 'profile: flashnext-coder]' in last_to(f, 'worker-1')
    f.handle(turn('worker-1', 'Implementation finished'))
    f.deliver('worker-1')
    info = f.agent('worker-1')
    assert info['advisory_profile'] == 'flashnext-coder'
    assert info['desired_profile'] == 'claude-reviewer' and info['profile_state'] == 'pending'
    assert info['generation'] == '0' and info['label'] == 'worker-1'
    assert 'Capabilities: code-review, safety-audit, planning' in last_to(f, 'worker-1')
    assert 'No CLI/model switch' in last_to(f, 'worker-1')


def test_wrong_worker_profile_rejected_before_any_part_of_decision_is_queued(routed):
    f = routed
    assignments = json.loads(f.task()['profile_assignments'])
    assignments['assignments'][1]['worker'] = 'worker-2'
    f.r.hset(fleet.TASK, mapping={'profile_assignments': json.dumps(assignments)})
    f.on_supervisor(decision(dispatch=[
        {'to': 'worker-1', 'instruction': 'Recommended profile: flashnext-coder'},
        {'to': 'worker-1', 'instruction': 'Recommended profile: claude-reviewer'},
    ]))
    assert f.r.llen('fx:inbox:worker-1') == 0
    assert f.agent('worker-1')['profile_state'] == 'blocked'
    assert f.agent('worker-1')['advisory_profile'] == ''
    assert 'not assigned to worker-1' in f.r.l['fx:inbox:supervisor'][-1]
    assert f.task()['bad_replies'] == '1'
    dispatch(f)  # the original correction path can recover
    assert f.agent('worker-1')['profile_state'] == 'applied-metadata'


@pytest.mark.parametrize('change', ['assignment', 'unknown_card', 'live_flag', 'task_id'])
def test_revalidate_before_delivery_and_before_context_rotation(routed, monkeypatch, change):
    f = routed
    dispatch(f)
    assignments = json.loads(f.task()['profile_assignments'])
    if change == 'assignment':
        assignments['assignments'][0]['worker'] = 'worker-2'
        f.r.hset(fleet.TASK, mapping={'profile_assignments': json.dumps(assignments)})
    elif change == 'unknown_card':
        # The marker and trusted assignment agree, but the registry no longer has the card.
        from cmux_fleet.agent_registry import AgentRegistry
        registry = AgentRegistry()
        registry._cards = ()
        monkeypatch.setattr(fleet, 'AgentRegistry', lambda *args: registry)
    elif change == 'live_flag':
        f.r.hset(fleet.TASK, mapping={'apply_profiles_live': 'true'})
    else:
        f.r.hset(fleet.TASK, mapping={'id': 'task-replacement'})
    f.setf('worker-1', delivered=1)
    write_rollout(f.sessions, THREADS['worker-1'], fleet.agent_cwd(f.cfg, 'worker-1'), used=90000)
    forbid_process_changes(monkeypatch, f)
    sent = traffic(f.mgr)
    f.deliver('worker-1')
    assert f.task()['status'] == 'blocked'
    assert traffic(f.mgr) == sent
    assert f.agent('worker-1')['generation'] == '0'


@pytest.mark.parametrize('value', [True, 'true', 1, None, []])
def test_task_rejects_live_opt_in_and_non_boolean_values(fx, value):
    fx.finish('cancelled', 'replace fixture task')
    before = fx.task()
    fx.handle({'kind': 'task', 'request_id': 'live', 'spec': {
        'goal': 'implement router', 'auto_agents': True, 'apply_profiles_live': value,
    }})
    result = json.loads(fx.r.get('fx:reply:live'))
    assert result['status'] == 'rejected' and 'apply_profiles_live' in result['reason']
    assert fx.task() == before


def test_config_rejects_live_opt_in_before_fleet_start(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text(json.dumps({'apply_profiles_live': True}))
    mgr = FakeMgr()
    with pytest.raises(ValueError, match='requires allow_profile_live_switch'):
        fleet.Fleet(path, FakeRedis(), mgr)
    assert mgr.n == 0


def test_existing_handoff_revalidates_profile_after_ready(routed):
    f = routed
    dispatch(f)
    f.setf('worker-1', delivered=1)
    write_rollout(f.sessions, THREADS['worker-1'], fleet.agent_cwd(f.cfg, 'worker-1'), used=90000)
    f.deliver('worker-1')
    assert f.agent('worker-1')['state'] == 'handoff'
    assert f.r.llen('fx:inbox:worker-1') == 1
    f.finish_rotation('worker-1', timed_out=True)
    assert f.agent('worker-1')['label'] == 'worker-1.1'
    assert f.agent('worker-1')['state'] == 'starting'
    f.deliver('worker-1')
    assert f.r.llen('fx:inbox:worker-1') == 1
    f.handle(turn('worker-1', 'READY', generation=1))
    f.deliver('worker-1')
    assert f.agent('worker-1')['generation'] == '1'
    assert f.agent('worker-1')['profile_state'] == 'applied-metadata'
    assert last_to(f, 'worker-1').count('[Recommended agent profile:') == 1


def test_pi_supervisor_operator_input_keeps_profile_metadata_and_delivery_checks(pfx, monkeypatch):
    f = pfx
    f.service = False
    f.submit({'goal': 'implement dynamic router', 'auto_agents': True})
    forbid_process_changes(monkeypatch, f)
    f.pump()
    callback(f, pi_turn(f, 'supervisor', decision(dispatch=[{
        'to': 'worker-1', 'instruction': 'Subtask: implementation\nRecommended profile: flashnext-coder\nFix tests.',
    }])))
    f.pump()
    f.handle(operator_input(f, 'Have worker-1 rerun tests for the assigned subtask and report back.'))
    assert json.loads(f.r.get('fx:reply:click1'))['status'] == 'accepted'
    f.pump()
    assert '[Operator instruction]' in f.agent('supervisor')['last_msg']
    callback(f, pi_turn(f, 'supervisor', decision(dispatch=[{
        'to': 'worker-1', 'instruction': 'Subtask: implementation\nRecommended profile: flashnext-coder\nRerun tests.',
    }])))
    info = f.agent('worker-1')
    assert info['profile_state'] == 'pending' and info['label'] == 'worker-1'
    assert info['msg_id'].startswith('msg-')
    callback(f, dict(pi_turn(f, 'worker-1', 'Wrong delivery'), msg_id='old-delivery'))
    assert f.agent('worker-1')['state'] == 'busy'
    callback(f, pi_turn(f, 'worker-1', 'Implementation done'))
    assert f.agent('worker-1')['state'] == 'idle'
    f.pump()
    assert '[worker-1 report]' in f.agent('supervisor')['last_msg']
    assert 'Rerun tests.' in f.agent('worker-1')['last_msg']
    assert f.agent('worker-1')['profile_state'] == 'applied-metadata'
    assert f.agent('worker-1')['msg_id'] != info['msg_id']
