"""Task-mode Claude hook-channel worker: an API error (StopFailure) holds only that slot and retries.

2026-10-29 user: 「只扣出错误那个槽」. A turn that ends with StopFailure (e.g. ``Login expired · Please run
/login``) has ended, so its execution has stopped: the delivery is committed as not adopted and the same
instruction is queued again for the same slot (``replay`` attempt, capped by ``max_attempts``) after a
backoff (``runtime_v2.STOP_FAILURE_BACKOFF``). Meanwhile only that slot is held: the fleet is not paused and
the supervisor is not held. The backoff ends on the daemon's own tick (or earlier, when a turn of the same
session ends normally). Retries used up: still only that slot is held; the supervisor is told.

Also: a Claude worker's lease that ran out while its work deadline had not (the daemon renews it; it ran out
because the daemon did not run, e.g. a lid-closed sleep) holds only that slot (design §24).

Real private Redis, real hook commands where the delivery channel matters (fixture ``hooked``).
"""
import json
import os
import shlex
import subprocess
import time
from datetime import datetime, timezone

from test_claude_hook_delivery import (REPORT, append, audit, delivered, feedback_rows, handed, mailbox_len, payload,
                                       reports, rewake_rows, settings, status_of, step, stop, thread_of, typed,
                                       typed_any)
from test_claude_hook_delivery import hooked  # noqa: F401  (fixture)
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from fleetmux import claude_hook, fleet, runtime_v2

HOOK_CHANNEL = True  # tests/conftest.py: the real hook commands run, no stand-in

LOGIN = 'Login expired · Please run /login'
DROPPED = 'API Error: Connection dropped (ECONNRESET).'


def fail(f, slot='worker-1', error='authentication_failed', text=LOGIN):
    """The generation's StopFailure recorder (settings StopFailure hooks[0]), in process, then one daemon step."""
    raw = dict(payload(f, slot, thread_of(f, slot, int(f.agent(slot)['generation'])), text, False),
               hook_event_name='StopFailure', error=error)
    assert fleet.claude_stop_failure(f.path, slot, int(f.agent(slot)['generation']), json.dumps(raw))
    for _ in range(5):
        f.v2.step(20)
    return raw


def failure_rewake(f, slot='worker-1', text=LOGIN):
    """The background asyncRewake entry of the same StopFailure event (settings StopFailure hooks[1])."""
    gen = int(f.agent(slot)['generation'])
    entry = settings(f, slot, gen)['hooks']['StopFailure'][0]['hooks'][1]
    assert entry.get('asyncRewake') is True and 'claude-rewake' in entry['command']
    key = f.v2.bus.prefix + ':hook:' + slot + ':rewake'
    before = f.r.get(key)
    proc = subprocess.Popen(shlex.split(entry['command']), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
    data = dict(payload(f, slot, thread_of(f, slot, gen), text, False), hook_event_name='StopFailure',
                error='authentication_failed')
    proc.stdin.write(json.dumps(data))
    proc.stdin.close()
    f.test_procs = getattr(f, 'test_procs', []) + [proc]
    assert step(f, lambda: f.r.get(key) != before, timeout=10), 'the StopFailure rewake never started'
    return proc


def due(f, slot='worker-1'):
    """Fast-forward the backoff instead of waiting for it, then one daemon tick + pump."""
    f.setf(slot, stopfail_retry_at=time.time() - 1)
    f.v2.tick()
    f.pump()


def cards(f, slot):
    return [json.loads(x['data']).get('render', '') for _, x in f.r.xrange(f.v2.prefix + ':ui:' + slot)]


def head(f, slot='worker-1'):
    mids = f.v2.bus.pending(slot)
    return f.v2.bus.message(mids[0]) if mids else {}


def test_stop_failure_holds_only_that_slot_and_queues_the_same_instruction(hooked):
    f = hooked
    got, env = handed(f)
    supervisor = f.agent('supervisor').get('state')
    fail(f)
    msg = f.v2.bus.message(env['message_id'])
    assert msg['status'] == 'committed' and msg['outcome'] == 'failed', 'the failed turn must be settled, not left in flight'
    assert reports(f) == 0, 'an API error is not a report'
    lease = f.v2.bus.slot_state('worker-1')
    assert lease.get('status') == 'idle' and not lease.get('current_delivery')
    info = f.agent('worker-1')
    assert info['state'] == 'held'
    assert not f.paused(), 'one slot\'s API error must not pause the fleet'
    assert f.agent('supervisor').get('state') == supervisor, 'the supervisor is not held'
    again = head(f)
    assert again.get('request_id') == msg['request_id'] and str(again.get('replay')) == '2'
    assert 'Write worker-1/part.md' in again['body']['text'] and again.get('stopfail_of') == env['message_id']
    wait = runtime_v2.STOP_FAILURE_BACKOFF[0]
    assert abs(float(info['stopfail_retry_at']) - time.time() - wait) < 10
    note = [c for c in cards(f, 'supervisor') if 'worker-1' in c and LOGIN[:12] in c]
    assert note and str(wait) in note[-1] and '第 1 次' in note[-1], cards(f, 'supervisor')
    assert any(e.get('event') == 'stop_failure.retry' for e in audit(f, 'worker-1'))
    # the other slots keep working
    f.queue('worker-2', 'Write worker-2/part.md with the findings.')
    f.pump()
    assert f.v2.bus.slot_state('worker-2').get('status') == 'received'
    assert typed_any(f, 'worker-1') == []


def test_backoff_due_redelivers_to_the_same_slot_through_the_stop_failure_rewake(hooked):
    f = hooked
    got, env = handed(f)
    fail(f)
    again = head(f)
    proc = failure_rewake(f)
    try:
        f.v2.tick()
        f.pump()
        assert f.agent('worker-1')['state'] == 'held', 'nothing is re-delivered before the backoff ends'
        due(f)
        lease = f.v2.bus.slot_state('worker-1')
        assert lease.get('status') == 'received' and lease.get('message_id') == again['message_id']
        assert step(f, lambda: proc.poll() is not None, timeout=40), 'the StopFailure rewake never handed it over'
        err = proc.stderr.read()
        assert proc.returncode == 2 and 'Write worker-1/part.md' in err
        append(payload(f, 'worker-1', f.test_threads['worker-1'], '', False)['transcript_path'], rewake_rows(err))
        stop(f, 'worker-1', REPORT.format(name='part.md'), active=False)
        assert step(f, lambda: status_of(f, lease) == 'committed'), 'the retried turn did not settle'
        assert f.v2.bus.message(again['message_id'])['outcome'] == 'completed'
        assert step(f, lambda: reports(f) == 1)
        assert typed_any(f, 'worker-1') == [] and not f.paused()
    finally:
        if proc.poll() is None:
            proc.kill()


def stamp(at):
    """A transcript row's ``timestamp`` as Claude Code writes it."""
    return datetime.fromtimestamp(at, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def background_turn_rows(*ids, at=None):
    """Finished run_in_background tasks start a turn on their own (measured 2.1.322: dequeue + task-notification rows)."""
    when = {} if at is None else {'timestamp': stamp(at)}
    return ([dict({'type': 'queue-operation', 'operation': 'dequeue'}, **when) for _ in ids]
            + [dict({'type': 'user', 'origin': {'kind': 'task-notification', 'producer': 'session-task'},
                     'message': {'role': 'user', 'content': '<task-notification>\n<task-id>' + i + '</task-id>\n'
                                                            '<status>completed</status>\n</task-notification>'}}, **when)
               for i in ids])


def failure_rows(at, text=DROPPED):
    """What an API error leaves at the end of its turn (measured 2.1.322): the error as an assistant row, then
    turn_duration."""
    return [{'type': 'assistant', 'isApiErrorMessage': True, 'timestamp': stamp(at),
             'message': {'content': [{'type': 'text', 'text': text}], 'stop_reason': 'stop_sequence'}},
            {'type': 'system', 'subtype': 'turn_duration', 'timestamp': stamp(at + 0.01)}]


def kept_off(f, proc):
    step(f, lambda: proc.poll() is not None, timeout=claude_hook.GRACE + 5)
    assert proc.poll() != 2, 'the StopFailure rewake handed the retry into a running turn'
    assert mailbox_len(f) == 1 and typed_any(f, 'worker-1') == []


def retried_after_that_turn(f, again):
    """The turn the notifications started ends with its own Stop: that hook hands the retry over and it settles."""
    lease = f.v2.bus.slot_state('worker-1')
    assert lease.get('status') == 'received' and lease.get('message_id') == again['message_id']
    got = delivered(f, stop(f, 'worker-1', 'Both background tasks are checked.', active=False))
    assert got['reason'].startswith(runtime_v2.STOP_FAILURE_RESUME) and 'Write worker-1/part.md' in got['reason']
    assert mailbox_len(f) == 0
    stop(f, 'worker-1', REPORT.format(name='part.md'))
    assert step(f, lambda: status_of(f, lease) == 'committed'), 'the retried turn did not settle'
    assert f.v2.bus.message(again['message_id'])['outcome'] == 'completed'
    assert step(f, lambda: reports(f) == 1)
    assert typed_any(f, 'worker-1') == [] and not f.paused()


def test_stop_failure_rewake_waits_for_a_turn_the_queue_started_at_the_failure(hooked):
    # Live run: a StopFailure does not hold the queue back as a Stop does. Two finished background tasks started
    # a turn within 50 ms of the API error, before the StopFailure hooks ran, so their rows were already in the
    # transcript when the rewake began to watch it. Measured row order: the dequeues sit before the error row.
    f = hooked
    got, env = handed(f)
    transcript = payload(f, 'worker-1', f.test_threads['worker-1'], '', False)['transcript_path']
    at = time.time()
    started = background_turn_rows('b1', 'b2', at=at + 0.05)
    append(transcript, started[:2] + failure_rows(at) + started[2:])
    fail(f, error='unknown', text=DROPPED)
    again = head(f)
    proc = failure_rewake(f, text=DROPPED)
    try:
        due(f)
        kept_off(f, proc)
        retried_after_that_turn(f, again)
        assert proc.poll() != 2
    finally:
        if proc.poll() is None:
            proc.kill()


def test_stop_failure_does_not_end_a_turn_that_started_after_it(hooked):
    # The same sequence with the rewake already watching: it sees the new turn start, and the turn-end mark the
    # StopFailure recorder writes a moment later belongs to the failed turn, not to this one.
    f = hooked
    got, env = handed(f)
    transcript = payload(f, 'worker-1', f.test_threads['worker-1'], '', False)['transcript_path']
    at = time.time()
    append(transcript, failure_rows(at))
    proc = failure_rewake(f, text=DROPPED)
    try:
        append(transcript, background_turn_rows('b1', 'b2', at=at + 0.05))
        step(f, lambda: False, timeout=3)  # the rewake polls the transcript once a second
        fail(f, error='unknown', text=DROPPED)
        again = head(f)
        due(f)
        kept_off(f, proc)
        retried_after_that_turn(f, again)
        assert proc.poll() != 2
    finally:
        if proc.poll() is None:
            proc.kill()


def test_failure_look_back_only_trusts_a_failure_end(tmp_path):
    at = time.time()
    started = background_turn_rows('b1', at=at + 0.05)

    def watch(rows, failed=True):
        path = tmp_path / (str(len(list(tmp_path.iterdir()))) + '.jsonl')
        append(path, rows)
        return claude_hook.TurnWatch(str(path), failed=failed).poll()
    assert watch(started[:1] + failure_rows(at) + started[1:])
    refusal = [{'type': 'assistant', 'timestamp': stamp(at), 'message': {'content': [], 'stop_reason': 'refusal'}}]
    assert watch(refusal + started)
    # a normal Stop holds the queue back: rows before it belong to the turn that just stopped
    assert not watch(started[:1] + failure_rows(at) + started[1:], failed=False)
    # the failed turn's own start, written before the failure, is not a running turn
    assert not watch(background_turn_rows('b0', at=at - 30) + failure_rows(at))
    # no failure end in sight (rows not written yet, or without a time): never guess a running turn
    assert not watch(started)
    assert not watch(background_turn_rows('b1') + failure_rows(at))
    done = [{'type': 'assistant', 'timestamp': stamp(at), 'message': {'content': [], 'stop_reason': 'end_turn'}}]
    assert not watch(done + started)
    # a turn that started after the failure and was interrupted since is over
    interrupted = [{'type': 'user', 'timestamp': stamp(at + 1),
                    'message': {'role': 'user', 'content': '[Request interrupted by user]'}}]
    assert not watch(failure_rows(at) + started + interrupted)


def absorbed_rows(err, origin='task-notification'):
    """A rewake notification Claude Code absorbed into the running turn (measured 2.1.322): no user row, only
    queue-operation enqueue / remove (absorbed_mid_turn) and a queued_command attachment."""
    content = rewake_rows(err)[0]['message']['content']
    return [{'type': 'queue-operation', 'operation': 'enqueue', 'content': content},
            {'type': 'queue-operation', 'operation': 'remove', 'content': content, 'reason': 'absorbed_mid_turn'},
            {'type': 'attachment', 'attachment': {'type': 'queued_command', 'prompt': content, 'commandMode': origin,
                                                  'origin': {'kind': origin}}}]


def test_retry_absorbed_into_a_background_task_turn_settles_once(hooked):
    # The race the turn watch cannot close: finished background tasks start a turn after the rewake took the
    # retry from the mailbox and before Claude Code woke on it. The retry is absorbed into that running turn,
    # which ends with the report.
    f = hooked
    got, env = handed(f)
    fail(f, error='unknown', text=DROPPED)
    again = head(f)
    transcript = payload(f, 'worker-1', f.test_threads['worker-1'], '', False)['transcript_path']
    proc = failure_rewake(f)
    try:
        due(f)
        lease = f.v2.bus.slot_state('worker-1')
        assert lease.get('status') == 'received' and lease.get('message_id') == again['message_id']
        assert step(f, lambda: proc.poll() is not None, timeout=40), 'the StopFailure rewake never handed it over'
        err = proc.stderr.read()
        assert proc.returncode == 2 and 'Write worker-1/part.md' in err
        append(transcript, background_turn_rows('b1', 'b2'))
        append(transcript, absorbed_rows(err))
        stop(f, 'worker-1', REPORT.format(name='part.md'), active=False)
        assert step(f, lambda: status_of(f, lease) == 'committed'), 'the turn that answered the retry did not settle'
        assert f.v2.bus.message(again['message_id'])['outcome'] == 'completed'
        assert step(f, lambda: reports(f) == 1)
        assert not any(e.get('event') in ('turn.ignored', 'turn.unsolicited') for e in audit(f, 'worker-1')), \
            'the turn that answered the retry was taken as unsolicited'
        # A later Stop-hook feedback turn of the same text settles nothing a second time.
        append(transcript, feedback_rows(err))
        stop(f, 'worker-1', REPORT.format(name='part.md'))
        step(f, lambda: False, timeout=3)
        assert reports(f) == 1, 'the retried delivery was reported twice'
        assert typed_any(f, 'worker-1') == [] and not f.paused()
    finally:
        if proc.poll() is None:
            proc.kill()


def test_an_older_retry_row_never_settles_the_next_retry(hooked):
    # With three attempts the second and third retry carry the same text. A turn that ends after the rewake
    # took the third one, but before Claude Code showed it to the model, has the second retry's row as its
    # latest hook input: that row was handed over for another delivery and must not settle this one.
    f = hooked
    f.cfg['max_attempts'] = 3
    got, env = handed(f)
    transcript = payload(f, 'worker-1', f.test_threads['worker-1'], '', False)['transcript_path']
    procs = []
    try:
        fail(f)
        procs.append(failure_rewake(f))
        due(f)
        assert step(f, lambda: procs[0].poll() is not None, timeout=40)
        second = procs[0].stderr.read()
        assert procs[0].returncode == 2
        append(transcript, rewake_rows(second))
        fail(f)  # attempt 2 fails as well
        again = head(f)
        assert str(again.get('replay')) == '3' and again['body']['text'] == second.strip()
        procs.append(failure_rewake(f))
        due(f)
        lease = f.v2.bus.slot_state('worker-1')
        assert lease.get('status') == 'received' and lease.get('message_id') == again['message_id']
        assert step(f, lambda: procs[1].poll() is not None, timeout=40)
        third = procs[1].stderr.read()
        assert procs[1].returncode == 2 and third == second
        # a finished background task started a turn meanwhile; it ends before the queued notification is read
        append(transcript, background_turn_rows('b1'))
        stop(f, 'worker-1', 'The background task is checked.', active=False)
        armed = f.v2.bus.prefix + ':hook:worker-1:armed'
        assert step(f, lambda: f.r.exists(armed) or status_of(f, lease) == 'committed', timeout=15)
        assert status_of(f, lease) != 'committed', 'a turn that never saw the retry settled it'
        assert any(e.get('event') == 'turn.ignored' for e in audit(f, 'worker-1'))
        assert reports(f) == 0
        # the queued notification then starts its own turn, which settles the retry
        append(transcript, rewake_rows(third))
        stop(f, 'worker-1', REPORT.format(name='part.md'), active=False)
        assert step(f, lambda: status_of(f, lease) == 'committed'), 'the turn that answered the retry did not settle'
        assert f.v2.bus.message(again['message_id'])['outcome'] == 'completed'
        assert step(f, lambda: reports(f) == 1)
        assert typed_any(f, 'worker-1') == [] and not f.paused()
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()


def test_first_retry_text_never_matches_the_original_delivery_row():
    # Why the default of two attempts needs no more than the text: the retry's compared head is the resume
    # paragraph, which the original delivery's row does not contain.
    body = 'Write worker-1/part.md with the findings.'
    assert not runtime_v2.turn_answers(runtime_v2.STOP_FAILURE_RESUME + body, [body])
    assert runtime_v2.turn_answers(runtime_v2.STOP_FAILURE_RESUME + body, [runtime_v2.STOP_FAILURE_RESUME + body])


def test_a_normal_turn_end_of_the_same_session_ends_the_backoff(hooked):
    f = hooked
    got, env = handed(f)
    fail(f)
    again = head(f)
    f.setf('worker-1', stopfail_at=time.time() - 60)  # past the stale-armed guard (claude_hook.ARMED_TTL)
    # the operator logged in again and typed a line: that turn ends with a normal Stop
    hook = stop(f, 'worker-1', 'hello', active=False, user_prompt='hello')
    assert step(f, lambda: f.r.exists(f.v2.bus.prefix + ':hook:worker-1:armed'), timeout=15)
    f.v2.tick()
    f.pump()
    assert f.agent('worker-1')['state'] != 'held', 'a normal turn end of the same session must end the hold'
    got = delivered(f, hook)
    assert 'Write worker-1/part.md' in got['reason']
    assert f.v2.bus.slot_state('worker-1').get('message_id') == again['message_id']
    assert any(e.get('event') == 'stop_failure.released' for e in audit(f, 'worker-1'))


def test_retries_used_up_still_hold_only_that_slot_and_tell_the_supervisor(hooked):
    f = hooked
    sent = []
    f.notifier = lambda rec: sent.append(rec) or 'sent'
    got, env = handed(f)
    f.cfg['lifecycle'] = 'dev_task'  # lifecycle notifications (tg / parent) are on for managed runs only
    fail(f)
    proc = failure_rewake(f)
    try:
        due(f)
        assert step(f, lambda: proc.poll() is not None, timeout=40)
        err = proc.stderr.read()
        assert proc.returncode == 2
        append(payload(f, 'worker-1', f.test_threads['worker-1'], '', False)['transcript_path'], rewake_rows(err))
        before = len(typed(f, 'supervisor')) + f.r.llen(f.v2.bus.pending_key('supervisor'))
        fail(f)  # attempt 2 of max_attempts 2 fails again
        f.v2.tick()
        f.pump()
        assert f.agent('worker-1')['state'] == 'held'
        assert f.v2.bus.pending('worker-1') == [], 'no attempt beyond max-attempts'
        assert not f.paused() and f.task().get('status') == 'running', 'used-up retries never pause or block the task'
        assert any(e.get('event') == 'stop_failure.exhausted' for e in audit(f, 'worker-1'))
        told = typed(f, 'supervisor') + [f.v2.bus.message(m)['body']['text'] for m in f.v2.bus.pending('supervisor')]
        assert len(told) > before and any('API ERROR' in t and 'worker-1' in t for t in told), told
        assert sent and any('worker-1' in rec['text'] for rec in sent)
        f.v2.tick()
        f.pump()
        assert f.agent('worker-1')['state'] == 'held' and not float(f.agent('worker-1').get('stopfail_retry_at') or 0), \
            'no timer releases a used-up slot'
    finally:
        if proc.poll() is None:
            proc.kill()


def test_stop_failure_after_a_wake_hold_settles_and_retries(hooked):
    # 10-29 01:25: the lid-closed sleep let every Claude lease run out; the turn then ended with StopFailure.
    f = hooked
    f.cfg['lease_reclaim'] = 'off'  # §24 (11-10): on, the lease is taken back (tests/test_lease_reclaim.py)
    got, env = handed(f)
    f.r.hset(f.v2.bus.lease_key('worker-1'), 'lease_until', int(time.time() * 1000) - 1000)
    f.v2.tick()
    assert f.agent('worker-1')['state'] == 'held'
    assert f.v2.bus.message(env['message_id'])['status'] == 'uncertain'
    assert not f.paused(), 'a Claude lease the daemon did not renew holds only that slot'
    assert any(e.get('event') == 'execution.held' for e in audit(f, 'worker-1'))
    assert any('worker-1' in c and '其他槽照常' in c for c in cards(f, 'supervisor')), 'the supervisor must see the hold'
    fail(f, error='server_error', text='API Error: Connection error.')
    assert f.v2.bus.message(env['message_id'])['status'] == 'committed'
    assert str(head(f).get('replay')) == '2' and f.agent('worker-1')['state'] == 'held' and not f.paused()


def test_deadline_expiry_still_pauses(hooked):
    f = hooked
    got, env = handed(f)
    f.r.hset(f.v2.bus.msg_key(env['message_id']), 'deadline_at', int(time.time() * 1000) - 1000)
    f.v2.tick()
    assert f.agent('worker-1')['state'] == 'held' and f.paused()


def test_stop_failure_without_a_delivery_is_only_audit(hooked):
    f = hooked
    fail(f)
    assert f.agent('worker-1')['state'] == 'idle' and not f.paused()
    assert any(e.get('event') == 'hook.stop_failure' for e in audit(f, 'worker-1'))
    assert any(e.get('event') == 'stop_failure.ignored' for e in audit(f, 'worker-1'))
