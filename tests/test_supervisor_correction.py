"""A supervisor reply the v2 validator rejects gets a correction naming the real reason.

Real e2e (worker-3 run A'): the correction said "(substring not found)" because runtime_v2 handed
the legacy parser the placeholder 'invalid candidate'; the model then resent an old dispatch."""
from test_claude_supervisor import decision, no_trust, stop_hook, submit, task_fleet, typed  # noqa: F401
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)


def test_rejected_decision_correction_names_the_validator_error(task_fleet):
    f = task_fleet
    submit(f)
    stop_hook(f, 'supervisor', decision([]))  # RUNNING, nobody busy, no dispatch: the v2 validator rejects it
    f.pump()
    correction = typed(f, 'supervisor')[-1]
    assert 'no worker is busy: RUNNING must dispatch at least one instruction' in correction
    assert 'substring not found' not in correction
    assert f.task()['status'] == 'running'


def test_prose_reply_still_gets_the_parse_error(task_fleet):
    f = task_fleet
    submit(f)
    stop_hook(f, 'supervisor', 'I will ask worker-1 to do it.')
    f.pump()
    assert 'not a valid decision JSON' in typed(f, 'supervisor')[-1]
