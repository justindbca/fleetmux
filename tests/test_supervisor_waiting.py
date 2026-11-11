"""Declared dependency waiting (M15): `RUNNING` + empty dispatch + `waiting` on a task whose slots are all idle.

Contract (M15, `workspace/impl-1/waiting-implementation.md`):

* `waiting = {"for": <what>, "check": <how you will know>, "for_hours": 2}` is allowed only on `status: RUNNING`
  with an **empty** `dispatch` (waiting + dispatch is refused: one reply cannot both wait and dispatch);
* `for` / `check` are descriptions (>= 12 chars), never host commands; `for_hours` is a finite number in (0, 12],
  default 2, clamped to the task's own deadline;
* the task stays `running` (no fourth status), no worker gets a delivery, the `hours` wall-clock budget keeps
  running, and exactly **one** reminder is queued when the horizon is reached (no auto-dispatch, no BLOCKED, no
  renewal by the daemon);
* any later accepted reply without `waiting` clears the wait; the next-turn context and `status` show it.

The unit rules run against `parse_decision` (the one entry point v1 and v2 share); the flows run on the v1
in-memory fleet (`fx`) and on the real-Redis v2 fleet (`task_fleet`, Claude and Pi supervisors).
"""
import json
import time

import pytest

from test_claude_supervisor import decision as v2_decision  # noqa: F401  (dispatch-list reply)
from test_claude_supervisor import no_trust, stop_hook, submit, task_fleet, typed  # noqa: F401  (fixtures)
from test_fleet import decision, fx, last_to, traffic, turn  # noqa: F401  (fixture + helpers)
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)

from fleetmux import fleet

WAIT = {'for': 'owner native receipts for the 42 acceptance cases on both platforms',
        'check': 'operator-receipts/*.json newer than matrix.json and all 42 rows pass'}


def wait_reply(**extra):
    body = {'status': 'RUNNING', 'completion': 40, 'reason': 'preparation delivered; native evidence pending',
            'dispatch': [], 'waiting': dict(WAIT, **extra)}
    return json.dumps(body)


def parse(text, others_active=False):
    return fleet.parse_decision(text, ['worker-1'], others_active)


def kinds(f):
    return [json.loads(x).get('kind') for x in f.r.lrange(fleet.LOG, 0, -1)]


def waiting_of(f):
    return fleet.task_waiting(f.task())


def rewind(f, seconds=1):
    """Move the recorded horizon into the past, as a real clock would."""
    w = waiting_of(f)
    w['until'] = time.time() - seconds
    f.r.hset(fleet.TASK, mapping={'waiting': json.dumps(w)})


# ------------------------------------------------------------------ unit: the shared gate
def test_waiting_is_accepted_with_an_empty_dispatch_and_defaults_to_two_hours():
    d = parse(wait_reply())
    assert d['waiting'] == dict(WAIT, for_hours=2)


def test_waiting_keeps_its_bounded_for_hours():
    assert parse(wait_reply(for_hours=6))['waiting']['for_hours'] == 6
    assert parse(wait_reply(for_hours=0.5))['waiting']['for_hours'] == 0.5


def test_all_idle_running_without_waiting_is_still_refused():
    with pytest.raises(ValueError) as exc:
        parse(decision(dispatch=[]))
    assert 'no worker is busy: RUNNING must dispatch at least one instruction' in str(exc.value)


def test_others_active_still_exempts_an_empty_dispatch():
    assert parse(decision(dispatch=[]), others_active=True)['dispatch'] == []


@pytest.mark.parametrize('mutate,text', [
    (lambda d: d.update(status='COMPLETED', completion=100), 'waiting is only for RUNNING'),
    (lambda d: d.update(status='BLOCKED'), 'waiting is only for RUNNING'),
    (lambda d: d['dispatch'].append({'to': 'worker-1', 'instruction': 'Write worker-1/x.md with the findings.'}),
     'waiting needs an empty dispatch'),
    (lambda d: d['waiting'].pop('for'), 'waiting.for must state'),
    (lambda d: d['waiting'].pop('check'), 'waiting.check must state'),
    (lambda d: d['waiting'].update({'for': 'too short'}), 'waiting.for must state'),
    (lambda d: d['waiting'].update(check='   '), 'waiting.check must state'),
    (lambda d: d['waiting'].update(for_hours=0), 'waiting.for_hours must be a number > 0 and <= 12'),
    (lambda d: d['waiting'].update(for_hours=-1), 'waiting.for_hours must be a number > 0 and <= 12'),
    (lambda d: d['waiting'].update(for_hours=13), 'waiting.for_hours must be a number > 0 and <= 12'),
    (lambda d: d['waiting'].update(for_hours=True), 'waiting.for_hours must be a number > 0 and <= 12'),
    (lambda d: d['waiting'].update(for_hours='2'), 'waiting.for_hours must be a number > 0 and <= 12'),
    (lambda d: d['waiting'].update(for_hours=float('nan')), 'waiting.for_hours must be a number > 0 and <= 12'),
    (lambda d: d['waiting'].update(for_hours=float('inf')), 'waiting.for_hours must be a number > 0 and <= 12'),
    (lambda d: d['waiting'].update(eta='later'), 'waiting has unknown keys: eta'),
    (lambda d: d.update(waiting='for receipts'), 'waiting must be an object'),
    (lambda d: d['waiting'].update({'for': 'sudo systemctl restart fleetmux'}),
     'is a description, not a host command or a path'),
    (lambda d: d['waiting'].update({'check': '/Users/x/operator-receipts'}),
     'is a description, not a host command or a path'),
])
def test_waiting_shape_is_refused_by_name(mutate, text):
    body = json.loads(wait_reply())
    mutate(body)
    with pytest.raises(ValueError) as exc:
        parse(json.dumps(body))
    assert text in str(exc.value)


# ------------------------------------------------------------------ v1 flow (in-memory fleet, all slots idle)
def test_v1_waiting_keeps_the_task_running_without_a_delivery(fx):
    fx.pump()  # the goal reaches the supervisor
    before = len(traffic(fx.mgr))
    fx.handle(turn('supervisor', wait_reply()))
    fx.pump()
    t = fx.task()
    assert t['status'] == 'running' and t['bad_replies'] == '0'
    w = waiting_of(fx)
    assert (w['for'], w['check'], w['for_hours']) == (WAIT['for'], WAIT['check'], 2)
    assert float(w['since']) <= time.time() < float(w['until'])
    assert float(w['until']) <= time.time() + 2 * 3600 + 5
    assert w['renewals'] == 0 and w['nudged'] is False
    assert len(traffic(fx.mgr)) == before  # no filler delivery to the worker, no correction
    assert 'task.waiting' in kinds(fx)


def test_v1_the_correction_for_an_all_idle_reply_names_waiting(fx):
    fx.pump()
    fx.handle(turn('supervisor', decision(dispatch=[])))
    fx.pump()
    correction = last_to(fx, 'supervisor')
    assert 'no worker is busy: RUNNING must dispatch at least one instruction' in correction
    assert '"waiting"' in correction and 'for_hours' in correction
    assert fx.task()['bad_replies'] == '1' and not waiting_of(fx)


def test_v1_renewal_keeps_since_and_counts_the_renewals(fx):
    fx.pump()
    fx.handle(turn('supervisor', wait_reply()))
    fx.pump()
    first = waiting_of(fx)
    fx.handle(turn('supervisor', wait_reply(for_hours=1)))
    fx.pump()
    w = waiting_of(fx)
    assert w['since'] == first['since'] and w['renewals'] == 1
    assert float(w['until']) <= time.time() + 3600 + 5 < float(first['until'])
    assert 'task.waiting_renewed' in kinds(fx)


def test_v1_a_dispatch_clears_the_wait(fx):
    fx.pump()
    fx.handle(turn('supervisor', wait_reply()))
    fx.pump()
    fx.handle(turn('supervisor', decision()))  # a real dispatch ends the wait
    fx.pump()
    assert not waiting_of(fx) and not fx.task().get('waiting')
    assert 'task.waiting_cleared' in kinds(fx)
    assert fx.agent('worker-1')['state'] == 'busy'


def test_v1_waiting_is_clamped_to_the_task_deadline(fx):
    fx.r.hset(fleet.TASK, mapping={'deadline': repr(time.time() + 600)})
    fx.pump()
    fx.handle(turn('supervisor', wait_reply(for_hours=12)))
    fx.pump()
    assert float(waiting_of(fx)['until']) == pytest.approx(float(fx.task()['deadline']), abs=2)


def test_v1_expiry_sends_exactly_one_reminder_and_keeps_the_task_running(fx):
    fx.pump()
    fx.handle(turn('supervisor', wait_reply()))
    fx.pump()
    rewind(fx)
    before = len(traffic(fx.mgr))
    fx.tick()
    assert fx.task()['status'] == 'running' and waiting_of(fx)['nudged'] is True
    assert fx.task()['bad_replies'] == '0'
    assert 'task.waiting_expired' in kinds(fx)
    fx.pump()
    reminder = last_to(fx, 'supervisor')
    assert 'waiting horizon reached' in reminder and WAIT['check'] in reminder
    sent = len(traffic(fx.mgr))
    assert sent == before + 1
    fx.tick()
    fx.pump()
    assert len(traffic(fx.mgr)) == sent  # one bounded reminder per horizon, never a loop
    assert fx.task()['status'] == 'running'


def test_v1_report_context_names_the_declared_wait(fx):
    fx.pump()
    fx.handle(turn('supervisor', decision()))  # worker-1 works while the supervisor waits for the external part
    fx.pump()
    fx.handle(turn('supervisor', wait_reply()))
    fx.pump()
    fx.handle(turn('worker-1', 'Done: wrote report.md'))
    fx.pump()
    msg = last_to(fx, 'supervisor')
    assert '[worker-1 report]' in msg
    assert 'Task waiting since ' in msg and WAIT['for'] in msg and 'check: ' + WAIT['check'] in msg


def test_v1_no_waiting_line_when_there_is_no_wait(fx):
    """The line is only added while a wait exists: an ordinary report keeps its exact previous text."""
    fx.pump()
    fx.handle(turn('supervisor', decision()))
    fx.pump()
    fx.handle(turn('worker-1', 'Done: wrote report.md'))
    fx.pump()
    assert 'Task waiting' not in last_to(fx, 'supervisor')


# ------------------------------------------------------------------ v2 flow (real Redis, Claude and Pi supervisors)
def test_v2_waiting_keeps_the_task_running_without_a_delivery(task_fleet):
    f = task_fleet
    submit(f)
    stop_hook(f, 'supervisor', wait_reply())
    f.pump()
    t = f.task()
    assert t['status'] == 'running' and t['bad_replies'] == '0'
    w = waiting_of(f)
    assert w['check'] == WAIT['check'] and float(w['until']) > time.time()
    assert not [x for x in typed(f, 'worker-1') if 'Supervisor instruction' in x]
    events = [e.get('event') for _, e in f.r.xrange(f.v2.bus.audit_key)]
    assert 'task.waiting' in events


def test_v2_expiry_reminds_once_then_a_dispatch_clears_the_wait(task_fleet):
    f = task_fleet
    submit(f)
    stop_hook(f, 'supervisor', wait_reply())
    f.pump()
    since = waiting_of(f)['since']
    # a renewal through a second supervisor turn (a plain note is enough to have a turn to answer)
    f.queue('supervisor', 'operator note: nothing has arrived yet')
    f.pump()
    stop_hook(f, 'supervisor', wait_reply(for_hours=1))
    f.pump()
    w = waiting_of(f)
    assert w['renewals'] == 1 and w['since'] == since
    # the horizon is reached: exactly one reminder, the task keeps running and nothing is dispatched
    rewind(f)
    f.tick()
    f.pump()
    said = [x for x in typed(f, 'supervisor') if 'waiting horizon reached' in x]
    assert len(said) == 1 and WAIT['check'] in said[0]
    f.tick()
    f.pump()
    assert len([x for x in typed(f, 'supervisor') if 'waiting horizon reached' in x]) == 1
    assert f.task()['status'] == 'running' and f.task()['bad_replies'] == '0'
    events = [e.get('event') for _, e in f.r.xrange(f.v2.bus.audit_key)]
    assert 'task.waiting_expired' in events
    # the supervisor answers the reminder with the dispatch: the wait is cleared and the task continues
    assert stop_hook(f, 'supervisor', v2_decision(['worker-1']))['status'] == 'committed'
    f.pump(); f.pump()
    assert not waiting_of(f) and not f.task().get('waiting')
    assert f.agent('worker-1')['state'] == 'busy'  # the dispatch went out after the reminder
    assert f.v2.bus.slot_state('worker-1').get('current_delivery')
    assert 'task.waiting_cleared' in [e.get('event') for _, e in f.r.xrange(f.v2.bus.audit_key)]
