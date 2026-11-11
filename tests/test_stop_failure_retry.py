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

from test_claude_hook_delivery import (REPORT, append, audit, delivered, handed, payload, reports,
                                       rewake_rows, settings, status_of, step, stop, thread_of, typed, typed_any)
from test_claude_hook_delivery import hooked  # noqa: F401  (fixture)
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from fleetmux import fleet, runtime_v2

HOOK_CHANNEL = True  # tests/conftest.py: the real hook commands run, no stand-in

LOGIN = 'Login expired · Please run /login'


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
