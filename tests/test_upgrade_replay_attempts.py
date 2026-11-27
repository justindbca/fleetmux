"""An operator resume (``start --resume``, which ``fleetmux upgrade`` runs) spends no attempt of a lost dispatch.

Seen after two upgrades in a row: job sub-fleets ended ``blocked`` with
"the dispatch to <slot> was lost again after 2 attempt(s) (max-attempts 2)" right after an upgrade, seconds after
their new generations started and before any of them said READY. The work itself had not failed: the same
instruction had been re-queued by the previous resume, delivered after READY and was still running when
the next upgrade stopped the tree. Every resume counted one attempt, so the second resume of one long instruction
always blocked the task (docs/design.md section 17 "Attempt limit").

Fixed rule: a dispatch re-queued by an operator resume keeps its ``replay`` value and is
never judged against max-attempts there. Losses that are the session's own -- the session ended (restart_lost), a
frozen session restarted (watchdog -> redeliver), an API error retry -- still count exactly as before.

Real private Redis with AOF, real hook entry points, fake cmux (the fixtures of tests/test_resume.py).
"""
from test_resume import (REPORT, answer_turn, crash_and_resume, half_done, managed, task_run)  # noqa: F401
from test_v2_codex_claude import typed

INSTRUCTION = 'Write worker-2/b.md'


def given(f, slot='worker-2'):
    """How many times the slot's current generation was handed worker-2's instruction."""
    return len([t for t in typed(f, slot) if INSTRUCTION in t])


def ready(f, *slots):
    for slot in [s for s in slots if f.agent(s)['state'] == 'starting']:
        assert answer_turn(f, slot, 'READY', startup=True)['status'] == 'ready'


def in_flight(f, slot='worker-2'):
    state = f.v2.bus.slot_state(slot)
    assert state['status'] == 'received', state
    return f.v2.bus.message(state['message_id'])


def test_two_resumes_of_one_inflight_dispatch_do_not_block_the_task(task_run):
    """The same instruction is in flight at two upgrades in a row."""
    state = task_run
    half_done(state)
    assert str(in_flight(state['f']).get('replay') or 1) == '1'
    f = crash_and_resume(state)                     # first upgrade
    ready(f, 'supervisor', 'worker-2')
    f.pump()
    assert given(f) == 1
    first = str(in_flight(f).get('replay') or 1)
    f = crash_and_resume(state)                     # second upgrade, the instruction still in flight
    task = f.task()
    assert task['status'] == 'running', task.get('reason')
    assert 'lost again' not in (task.get('reason') or '')
    ready(f, 'supervisor', 'worker-2')
    f.pump()
    assert given(f) == 1                            # handed to the newest generation exactly once
    assert first == '1' and str(in_flight(f).get('replay') or 1) == '1'  # neither resume spent an attempt
    brief = typed(f, 'supervisor')[-1]
    assert brief.startswith('[Resumed task ') and INSTRUCTION in brief


def test_resume_redelivers_once_and_only_after_ready(task_run):
    """The re-queued instruction waits in the queue until the new generation said READY, then goes once."""
    state = task_run
    half_done(state)
    f = crash_and_resume(state)
    ready(f, 'supervisor')
    assert f.agent('worker-2')['state'] == 'starting'
    for _ in range(5):                              # the unready window: the daemon keeps working
        f.pump()
        assert given(f) == 0
        assert f.v2.bus.slot_state('worker-2').get('status') != 'received'
        assert f.v2.depth('worker-2') == 1          # queued once, not twice
    ready(f, 'worker-2')
    f.pump()
    assert given(f) == 1 and in_flight(f)
    f.pump()
    assert given(f) == 1 and f.v2.depth('worker-2') == 0
    (f.workspace() / 'worker-2').mkdir(exist_ok=True)
    (f.workspace() / 'worker-2' / 'b.md').write_text('part B')
    assert answer_turn(f, 'worker-2', REPORT.format('worker-2/b.md'))['status'] == 'committed'
    assert f.task()['status'] == 'running'


def frozen_restart(f, slot='worker-2'):
    """What the watchdog does for a frozen session (watchdog.py: the slot's work is cancelled with the slot, then
    ``rt.redeliver(slot, msg, 'after the session of <label> froze')``): a counted loss. ``msg``: the in-flight
    instruction, else the one still queued for the slot."""
    state = f.v2.bus.slot_state(slot)
    mid = state['message_id'] if state.get('status') == 'received' else f.v2.bus.pending(slot)[0]
    msg = f.v2.bus.message(mid)
    assert f.v2.clear_slot(slot, False)
    f.v2.redeliver(slot, msg, 'after the session of ' + f.agent(slot)['label'] + ' froze')
    return msg


def queued(f, slot='worker-2'):
    return f.v2.bus.message(f.v2.bus.pending(slot)[0])


def test_a_frozen_restart_still_counts_an_attempt(task_run):
    """Control: the session's own losses keep counting; at max-attempts the next one blocks the task."""
    state = task_run
    half_done(state)
    f = state['f']
    frozen_restart(f)
    assert queued(f)['replay'] == '2'               # counted
    frozen_restart(f)                               # attempt 2 of 2 is lost by the session as well
    task = f.task()
    assert task['status'] == 'blocked'
    assert 'lost again after 2 attempt(s)' in task['reason'] and 'max-attempts 2' in task['reason']


def test_a_counted_restart_then_a_resume_does_not_block(task_run):
    """A frozen restart spent the one retry, then an upgrade resumed the tree."""
    state = task_run
    half_done(state)
    f = state['f']
    frozen_restart(f)
    assert queued(f)['replay'] == '2'
    f = crash_and_resume(state)
    task = f.task()
    assert task['status'] == 'running', task.get('reason')
    ready(f, 'supervisor', 'worker-2')
    f.pump()
    assert given(f) == 1 and in_flight(f)['replay'] == '2'  # carried, not raised
