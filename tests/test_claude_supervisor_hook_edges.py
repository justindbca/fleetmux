"""Independent-review regressions for supervisor hook lifetime and legacy launch fencing."""
import json
import time

import pytest

from fleetmux import claude_hook, fleet
from test_message_bus_v2 import private_redis_v2  # noqa: F401
from test_claude_supervisor_hook import supervisor, operator  # noqa: F401
from test_claude_hook_delivery import stop, step, delivered, append, rewake, typed_any, rewake_rows

# §48 (user 2026-11-09:「任何场景都不用 claude 输入框打字」): these tests exercised a channel that typed into a
# Claude input box; it is refused at start now (tests/test_no_claude_typing.py covers the refusal).
RETIRED = pytest.mark.skip(reason='docs/design.md §48: fleet never types into a Claude input box (2026-11-09)')


HOOK_CHANNEL = True


def test_ended_but_unsettled_delivery_does_not_strand_blocked_input(supervisor):
    f = supervisor
    assert '已排队' in operator(f, 'first marker')
    got = delivered(f, f.ready_hook)
    k = f.v2.hook_keys('supervisor', 0)
    path = f.ready_hook.transcript
    lease = f.v2.bus.slot_state('supervisor')
    append(path, [{'type': 'queue-operation', 'operation': 'enqueue', 'content': 'later marker'}])
    # Manual turn is ignored, so the old bind/received lease survive this real Stop callback.
    hook = stop(f, 'supervisor', 'ignored', user_prompt='unrelated manual turn')
    assert step(f, lambda: not hook.alive(), timeout=8)
    assert hook.decision() is None
    assert f.r.exists(k['human'])
    assert f.v2.bus.slot_state('supervisor')['current_delivery'] == lease['current_delivery']
    assert not f.r.exists(k['stopping'])
    before = f.r.get(k['rewake'])
    _, proc = rewake(f, 'supervisor')
    assert step(f, lambda: f.r.get(k['rewake']) != before)
    append(path, [{'type': 'queue-operation', 'operation': 'dequeue'}])
    assert '已排队' in operator(f, 'later marker')
    assert not f.r.exists(k['human'])
    assert json.loads(f.r.get(k['prompt_blocked']))['session_id'] == f.test_threads['supervisor']
    # A subsequent valid settlement of D releases its lease; the accepted operator input follows.
    append(path, [{'type': 'user', 'isMeta': True, 'message': {'content': 'Stop hook feedback:\n' + got['reason']}}])
    answer = (dict(status='RUNNING', skip=[dict(reason='nothing required')], dispatch=[]) if f.service else
              dict(status='RUNNING', completion=10, reason='delegate',
                   dispatch=[dict(to='worker-1', instruction='Write a marker in your own folder.')]))
    settled = stop(f, 'supervisor', json.dumps(answer))
    assert step(f, lambda: f.v2.bus.message(lease['message_id'])['status'] == 'committed')
    # Normal Stop is allowed to take the queued followup; no typed fallback is ever used.
    assert 'later marker' in delivered(f, settled)['reason']
    assert typed_any(f, 'supervisor') == []


def test_rewake_timeout_keepalive_rearms_after_rejected_input(supervisor):
    f = supervisor
    k = f.v2.hook_keys('supervisor', 0)
    append(f.ready_hook.transcript, [{'type': 'queue-operation', 'operation': 'enqueue', 'content': '[Image #1]'}])
    assert step(f, lambda: not f.ready_hook.alive(), timeout=8)
    assert f.ready_hook.decision() is None
    append(f.ready_hook.transcript, [{'type': 'queue-operation', 'operation': 'dequeue'}])
    assert '图片' in operator(f, '[Image #1]')
    cfg = fleet.load(f.path)
    fleet.save(f.path, dict(cfg, claude_hook_timeout=10))
    _, proc = rewake(f, 'supervisor')
    assert step(f, lambda: proc.poll() is not None, timeout=12)
    assert proc.returncode == 2
    assert claude_hook.KEEPALIVE in proc.stderr.read()
    assert json.loads(f.r.get(k['bind']))['kind'] == 'keepalive'
    wrapped = rewake_rows(claude_hook.KEEPALIVE)[0]['message']['content']
    assert fleet.claude_prompt(f.path, 'supervisor', 0, json.dumps({
        'session_id': f.test_threads['supervisor'], 'prompt': wrapped})) is None
    append(f.ready_hook.transcript, rewake_rows(claude_hook.KEEPALIVE))
    # Restore ordinary timeout for the successor Stop; keepalive must not become a decision.
    fleet.save(f.path, cfg)
    waiting = stop(f, 'supervisor', 'IDLE')
    assert step(f, lambda: f.r.exists(k['armed']))
    assert waiting.alive() and f.agent('supervisor')['state'] == 'idle'
    assert '已排队' in operator(f, 'after keepalive')
    assert 'after keepalive' in delivered(f, waiting)['reason']
    assert typed_any(f, 'supervisor') == []


@RETIRED
def test_old_v2_run_keeps_typed_channel_and_prompt_exemption(supervisor, monkeypatch):
    f = supervisor
    f.ready_hook.kill()
    cfg = fleet.load(f.path)
    cfg.pop('claude_supervisor_hook_delivery')
    fleet.save(f.path, cfg)
    f.cfg.pop('claude_supervisor_hook_delivery')
    assert not f.v2.hooked('supervisor')
    monkeypatch.setattr(fleet, 'claude_hook_next', lambda *a, **kw: (_ for _ in ()).throw(AssertionError('legacy Stop waited')))
    assert '已排队' in operator(f, 'legacy marker')
    f.pump()
    body = typed_any(f, 'supervisor')[-1]
    assert 'legacy marker' in body
    assert fleet.claude_prompt(f.path, 'supervisor', 0, json.dumps({
        'session_id': f.test_threads['supervisor'], 'prompt': body})) is None
    # A duplicate/manual stop with no matching typed input is ignored, never waits for a mailbox.
    out = fleet.claude_stop(f.path, 'supervisor', 0, json.dumps({
        'session_id': f.test_threads['supervisor'], 'last_assistant_message': 'ignored',
        'transcript_path': f.ready_hook.transcript}))
    assert out['status'] == 'ignored'


def test_block_checkpoint_rejects_old_session_and_a_new_unfinished_bind(supervisor):
    f = supervisor
    k = f.v2.hook_keys('supervisor', 0)
    assert '已排队' in operator(f, 'marker')
    delivered(f, f.ready_hook)
    f.r.delete(k['prompt_blocked'])
    for generation, thread in [(1, f.test_threads['supervisor']), (0, 'other')]:
        fleet.supervisor_prompt_blocked(f.cfg, f.r, 'supervisor', generation, thread)
        assert not f.r.exists(k['prompt_blocked'])
    # No Stop has ended this new bind: a manual input must not clear its busy proof.
    fleet.supervisor_prompt_blocked(f.cfg, f.r, 'supervisor', 0, f.test_threads['supervisor'])
    assert not f.r.exists(k['prompt_blocked'])


def test_keepalive_cannot_overwrite_unflushed_delivery_or_another_live_hook(supervisor):
    f = supervisor
    assert '已排队' in operator(f, 'marker')
    delivered(f, f.ready_hook)
    k, thread = f.v2.hook_keys('supervisor', 0), f.test_threads['supervisor']
    mine = f.r.incr(k['rewake'])
    original = f.r.get(k['bind'])
    assert not fleet.supervisor_rewake_keepalive(f.cfg, f.r, k, 0, thread, mine)
    assert f.r.get(k['bind']) == original
    fleet.hook_mark_stopping(f.cfg, 'supervisor', 0, thread)
    assert not fleet.supervisor_rewake_keepalive(f.cfg, f.r, k, 0, thread, mine)
    f.r.delete(k['stopping'])
    assert fleet.supervisor_rewake_keepalive(f.cfg, f.r, k, 0, thread, mine)
    assert json.loads(f.r.get(k['bind']))['kind'] == 'keepalive'


def test_keepalive_rechecks_concurrent_handover_atomically(supervisor, monkeypatch):
    f = supervisor
    f.ready_hook.kill()
    k, thread = f.v2.hook_keys('supervisor', 0), f.test_threads['supervisor']
    f.r.delete(k['armed'], k['stopping'], k['human'])
    mine = f.r.incr(k['rewake'])
    new = json.dumps(dict(kind='delivery', generation=0, session_id=thread,
                         message_id='new-message', delivery_id='new-delivery', at=time.time()))
    real_eval = f.r.eval
    def racing_eval(*args, **kwargs):
        f.r.set(k['bind'], new)  # another Stop handed it over after the caller's idle check
        return real_eval(*args, **kwargs)
    monkeypatch.setattr(f.r, 'eval', racing_eval)
    assert not fleet.supervisor_rewake_keepalive(f.cfg, f.r, k, 0, thread, mine)
    assert f.r.get(k['bind']) == new


def test_wrapped_manual_echo_still_cannot_settle_a_hook_delivery(supervisor):
    f = supervisor
    assert '已排队' in operator(f, 'marker')
    got = delivered(f, f.ready_hook)
    lease = f.v2.bus.slot_state('supervisor')
    k, thread = f.v2.hook_keys('supervisor', 0), f.test_threads['supervisor']
    wrapped = rewake_rows(got['reason'])[0]['message']['content']
    info = f.agent('supervisor')
    assert not fleet.supervisor_rewake_prompt(f.cfg, f.r, 'supervisor', 0, thread, wrapped, info)  # via Stop
    bind = json.loads(f.r.get(k['bind']))
    bind['via'] = 'rewake'
    f.r.set(k['bind'], json.dumps(bind))
    assert fleet.supervisor_rewake_prompt(f.cfg, f.r, 'supervisor', 0, thread, wrapped, info)
    assert not fleet.supervisor_rewake_prompt(f.cfg, f.r, 'supervisor', 1, thread, wrapped, info)
    assert not fleet.supervisor_rewake_prompt(f.cfg, f.r, 'supervisor', 0, 'other-session', wrapped, info)
    # Even while a valid rewake bind exists, manually echoing its wrapper has user origin, not hook origin.
    stop(f, 'supervisor', '{"status":"RUNNING","dispatch":[],"skip":[]}', user_prompt=wrapped)
    assert step(f, lambda: any(row.get('event') == 'turn.ignored' for _, row in f.r.xrange(f.v2.bus.audit_key)))
    assert f.v2.bus.slot_state('supervisor')['current_delivery'] == lease['current_delivery']
    assert f.v2.bus.message(lease['message_id'])['status'] != 'committed'
