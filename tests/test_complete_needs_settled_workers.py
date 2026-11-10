"""The supervisor cannot end a task while dispatched work is still out (no-typing batch, step 3c).

COMPLETED is accepted only when every worker it dispatched has settled (no delivery being worked on,
queued, held, or a sub-fleet running), or when the decision withdraws those workers explicitly
(``"withdraw": [...]``). A refused COMPLETED counts as a false completion (three block the task).
v2 path: private Redis + pi bridges driven by their events (tests/test_runtime_v2.py fixture).
"""
import json

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import event, fx, receive, settle  # noqa: F401  (fx fixture)
from cmux_fleet import fleet

WRITE = {'to': 'worker-1', 'instruction': 'Write worker-1/report.md with Conclusion evidence.'}
ART = [{'path': 'worker-1/report.md', 'contains': ['Conclusion']}]
DONE = {'status': 'COMPLETED', 'completion': 100, 'dispatch': [], 'reason': 'verified'}
REPORT = {'done': ['report'], 'evidence': ['worker-1/report.md'], 'not_done': 'none', 'blocked': 'none'}


def dispatched(f):
    """A task whose supervisor sent worker-1 off; worker-1's artifact already exists on disk."""
    f.service = False; f.cfg['mode'] = 'task'
    event(f, dict(kind='task', request_id='new-task', spec={'goal': 'write an evidence report'}))
    f.pump(); env = receive(f, 'supervisor')
    settle(f, 'supervisor', env, {'status': 'RUNNING', 'completion': 20, 'dispatch': [WRITE], 'artifacts': ART})
    f.pump(); worker = receive(f, 'worker-1')
    (f.workspace() / 'worker-1/report.md').write_text('Conclusion: verified evidence')
    return worker


def supervisor_says(f, decision):
    """Push one supervisor decision through the v2 settle path (a fresh supervisor delivery if needed)."""
    f.pump()
    env = receive(f, 'supervisor')
    if env is None:  # nothing queued for the supervisor: an operator note wakes it, as in a real run
        f.queue('supervisor', '[Operator instruction] status?\nReply with the JSON format from AGENTS.md.')
        f.pump(); env = receive(f, 'supervisor')
    assert env, 'no supervisor delivery to answer'
    settle(f, 'supervisor', env, decision)
    return env


def last_supervisor_text(f):
    return f.v2.pending_text('supervisor')[-1] if f.v2.depth('supervisor') else ''


def test_completed_while_a_worker_still_works_is_refused_and_counted(fx):
    f = fx
    dispatched(f)
    assert f.agent('worker-1')['state'] == 'busy'
    supervisor_says(f, DONE)
    task = f.task()
    assert task['status'] == 'running' and task['bounces'] == '1'
    text = last_supervisor_text(f)
    assert text.startswith('COMPLETED refused: worker-1 (working on a delivery)') and '"withdraw": ["worker-1"]' in text


def test_completed_after_the_worker_settled_is_accepted(fx):
    f = fx
    worker = dispatched(f)
    settle(f, 'worker-1', worker, REPORT)
    assert f.agent('worker-1')['state'] == 'idle'
    supervisor_says(f, DONE)
    assert f.task()['status'] == 'completed'


def test_explicit_withdraw_lets_the_task_end_and_cancels_the_open_delivery(fx):
    f = fx
    dispatched(f)
    supervisor_says(f, dict(DONE, withdraw=['worker-1']))
    assert f.task()['status'] == 'completed'
    # finish() stops the withdrawn delivery as for any ended task: held, its late result is never applied
    assert f.agent('worker-1')['state'] == 'held' and f.v2.bus.slot_state('worker-1')['status'] == 'uncertain'


def test_withdrawing_one_worker_does_not_excuse_another(fx):
    f = fx
    dispatched(f)
    supervisor_says(f, dict(DONE, withdraw=['worker-2']))  # worker-2 never had work; worker-1 still does
    assert f.task()['status'] == 'running' and 'worker-1 (working on a delivery)' in last_supervisor_text(f)


def test_three_completions_over_unsettled_work_block_the_task(fx):
    f = fx
    dispatched(f)
    for _ in range(3):
        supervisor_says(f, DONE)
    task = f.task()
    assert task['status'] == 'blocked' and 'unsettled deliveries: worker-1' in task['reason']


@pytest.mark.parametrize('decision,error', [
    (dict(DONE, withdraw=['worker-9']), 'withdraw must be a list of worker names'),
    (dict(DONE, withdraw='worker-1'), 'withdraw must be a list of worker names'),
    ({'status': 'RUNNING', 'completion': 30, 'dispatch': [WRITE], 'withdraw': ['worker-1']}, 'withdraw is only for COMPLETED'),
])
def test_withdraw_is_validated_with_the_decision(decision, error):
    with pytest.raises(ValueError, match=error):
        fleet.parse_decision(json.dumps(decision), ['worker-1', 'worker-2'], True)


@pytest.mark.parametrize('state,reason', [('busy', 'working on a delivery'), ('held', 'delivery held, not settled'),
                                          ('delegated', 'sub-fleet running'), ('idle', ''), ('handoff', '')])
def test_unsettled_reason_per_state(fx, state, reason):
    fx.setf('worker-2', state=state)
    assert fx.unsettled_reason('worker-2') == reason
    assert ('worker-2' in fx.unsettled_workers()) == bool(reason)


def test_a_delivery_still_queued_for_an_idle_worker_is_unsettled(fx):
    f = fx
    f.service = False; f.cfg['mode'] = 'task'
    event(f, dict(kind='task', request_id='new-task', spec={'goal': 'write an evidence report'}))
    f.pump(); env = receive(f, 'supervisor')
    f.cfg['max_busy'] = 0
    f.r.hset(fleet.SERVICE, 'paused', '1')  # paused: the dispatch stays queued, nothing is delivered
    settle(f, 'supervisor', env, {'status': 'RUNNING', 'completion': 20, 'dispatch': [WRITE], 'artifacts': ART})
    assert f.agent('worker-1')['state'] == 'idle' and f.inbox_depth('worker-1') == 1
    assert f.unsettled_reason('worker-1') == 'delivery queued'
