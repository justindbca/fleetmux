"""acceptance.verdict (launch-demo brief §1.2): one audit event at every "the supervisor reports done -> the
program judges" point, in the two modes: task artifacts (Fleet.on_supervisor COMPLETED) and dev_task (the
lifecycle delivery gate). Bookkeeping only: every judgement, return value
and status is asserted unchanged next to the new event.

Fake cmux and fake Pi agents on a private Redis (the tests/test_runtime_v2.py fixture), as in
tests/test_lifecycle_close.py."""
import sys

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import audits, fx, receive, settle  # noqa: F401  (fx fixture)
from test_lifecycle_close import as_task, complete, deliver_spec, notes, repo, submit, table  # noqa: F401
import test_complete_needs_settled_workers as settled_workers

FIELDS = {'mode', 'task_id', 'ok', 'problem', 'claimed_by', 'event', 'run_id', 'at'}


def verdicts(f, mode=None):
    return [v for v in audits(f, 'acceptance.verdict') if mode is None or v['mode'] == mode]


def claim(f):
    """The supervisor's real COMPLETED reply through the v2 settle path."""
    f.pump()
    env = receive(f, 'supervisor')
    settle(f, 'supervisor', env, {'status': 'COMPLETED', 'completion': 100, 'dispatch': [], 'reason': 'done'})


# ------------------------------------------------------------------ task artifacts
def test_task_artifacts_bounce_then_pass_each_write_one_verdict(fx):
    f = fx
    f.service = False
    f.cfg.update(mode='task')
    task = submit(f, {'goal': 'write a function and its test',
                      'artifacts': [{'path': 'demo/slug.py', 'contains': ['def slugify']},
                                    {'path': 'demo/test_slug.py', 'contains': ['def test_']}]})
    (f.workspace() / 'demo').mkdir()
    (f.workspace() / 'demo' / 'slug.py').write_text('def slugify(s):\n    return s\n')
    claim(f)
    # the judgement itself is unchanged: bounced, still running, one strike
    assert f.task()['status'] == 'running' and f.task()['bounces'] == '1'
    [first] = verdicts(f)
    assert set(first) == FIELDS | {'bounce'}
    assert first['mode'] == 'task_artifacts' and first['task_id'] == task['id']
    assert first['ok'] == 'false' and first['claimed_by'] == 'supervisor' and first['bounce'] == '1'
    assert first['problem'] == 'artifact not written: demo/test_slug.py'
    (f.workspace() / 'demo' / 'test_slug.py').write_text('def test_slug():\n    pass\n')
    claim(f)
    assert f.task()['status'] == 'completed'
    second = verdicts(f)[1]
    assert second['ok'] == 'true' and second['problem'] == '' and second['bounce'] == '1'
    assert len(verdicts(f)) == 2
    assert audits(f, 'task.completed')  # the existing events stay


def test_task_artifacts_three_failed_claims_still_block(fx):
    f = fx
    f.service = False
    f.cfg.update(mode='task')
    submit(f, {'goal': 'x', 'artifacts': [{'path': 'missing.md'}]})
    for _ in range(3):
        claim(f)
    assert f.task()['status'] == 'blocked' and 'three times' in f.task()['reason']
    assert [v['ok'] for v in verdicts(f)] == ['false'] * 3
    assert [v['bounce'] for v in verdicts(f)] == ['1', '2', '3']


def test_unsettled_refusal_writes_no_verdict(fx):
    """complete.refused (workers still out) is not a judgement of the deliverable: no verdict (ruling b)."""
    f = fx
    settled_workers.dispatched(f)
    assert f.agent('worker-1')['state'] == 'busy'
    settled_workers.supervisor_says(f, settled_workers.DONE)
    assert f.task()['bounces'] == '1' and audits(f, 'complete.refused') and not verdicts(f)


# ------------------------------------------------------------------ dev_task
def test_dev_task_failed_gate_writes_one_verdict_next_to_lifecycle_gate(fx, table, notes, repo, tmp_path):
    f = as_task(fx, 'dev_task')
    complete(f, {'goal': 'ship', 'deliver': deliver_spec(repo, tmp_path, [sys.executable, '-c', 'raise SystemExit(3)'])})
    f.tick()
    f.tick()
    assert f.task()['status'] == 'blocked' and 'tests failed (exit 3)' in f.task()['reason']
    [v] = verdicts(f, 'dev_task')
    assert set(v) == FIELDS
    assert v['mode'] == 'dev_task' and v['ok'] == 'false' and v['claimed_by'] == 'supervisor'
    assert v['task_id'] == f.task()['id'] and v['problem'].startswith('tests failed (exit 3)')
    assert '\n' not in v['problem']
    [gate] = audits(f, 'lifecycle.gate')  # the existing event stays, written once
    assert gate['ok'] == 'false'


def test_dev_task_passing_gate_writes_ok_verdict(fx, table, notes, repo, tmp_path):
    f = as_task(fx, 'dev_task')
    complete(f, {'goal': 'ship', 'deliver': deliver_spec(repo, tmp_path)})
    f.tick()
    [v] = verdicts(f, 'dev_task')
    assert v['ok'] == 'true' and v['problem'] == '' and v['mode'] == 'dev_task'
    [art] = verdicts(f, 'task_artifacts')  # the COMPLETED claim itself passed the literal check first
    assert art['ok'] == 'true'
    assert f.lifecycle_close['closed_by'] == 'auto'
