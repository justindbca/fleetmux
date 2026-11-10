"""Service-mode Claude hook-channel worker: a StopFailure (API error / model refusal) ends the turn.

2026-10-26 a service sub-fleet: one review request ended with a model refusal (Claude StopFailure, error
``invalid_request``). Service mode only audited it, so the delivery stayed in flight until the request
timed out 2 minutes later, and that timeout held the slot and paused the whole fleet ("执行状态未确认，
已暂停派工"). Nobody resumed it; 426 review requests failed over a week.

Now (design §24): the StopFailure proves the turn ended, so the bound request fails at once (never
re-sent by the fleet; the requester owns its retries), only that slot is held for a backoff, a refusal
gives the slot a fresh session for its next request (§37 ``fresh_pending``), and consecutive failures
up to ``service_stopfail_max`` hold only that slot and tell the supervisor. The fleet is never paused.

Real private Redis; the waiting Stop hook is the tests/hook_sim.py stand-in (conftest).
"""
import json
import time
from pathlib import Path

from test_clear_after_result import SLOT
from test_clear_after_result_redis import inflight
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_v2_codex_claude import answer_turn
from test_v2_codex_service import request, route, service  # noqa: F401  (fixture)
import hook_sim
from cmux_fleet import fleet, runtime_v2

REFUSAL_ROW = {'type': 'assistant', 'isSidechain': False,
               'message': {'model': '<synthetic>', 'role': 'assistant', 'stop_reason': 'refusal', 'content': []}}


def fail(f, error='server_error', refusal=False):
    """The slot's StopFailure hook for its in-flight turn, then one daemon step."""
    path = Path(f.agent(SLOT)['transcript'])
    if refusal:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as fh:
            fh.write(json.dumps(REFUSAL_ROW) + '\n')
    raw = dict(session_id=f.agent(SLOT)['thread'], transcript_path=str(path), error=error,
               hook_event_name='StopFailure')
    assert fleet.claude_stop_failure(f.path, SLOT, int(f.agent(SLOT)['generation']), json.dumps(raw))
    for _ in range(5):
        f.v2.step(20)


def result(f, rid):
    raw = f.r.get(fleet.RESULT + rid)
    return json.loads(raw) if raw else None


def audit(f):
    return [x for _, x in f.r.xrange(f.v2.bus.audit_key) if x.get('slot') == SLOT]


def cards(f, slot):
    return [json.loads(x['data']).get('render', '') for _, x in f.r.xrange(f.v2.prefix + ':ui:' + slot)]


def due(f):
    f.setf(SLOT, stopfail_retry_at=time.time() - 1)
    f.v2.tick()
    f.pump()


def rewake(f):
    """No Stop hook waits after a StopFailure: its asyncRewake entry hands the next delivery over (as in
    test_stop_failure_retry); the stand-in's one pass of the waiting hook does the same here."""
    info = f.agent(SLOT)
    assert hook_sim.next_delivery(f.cfg, SLOT, int(info['generation']), info['thread'], time.time() + 5)


def test_stop_failure_fails_the_request_now_and_holds_only_that_slot(service):
    f = service
    msg = inflight(f, 'r1')
    fail(f)
    assert result(f, 'r1')['status'] == 'failed', 'the request must fail now, not wait for its timeout'
    assert 'server_error' in result(f, 'r1')['error']
    assert f.v2.bus.message(msg['message_id'])['status'] == 'committed'
    info = f.agent(SLOT)
    assert info['state'] == 'held' and not f.paused(), 'one slot\'s API error must not pause the fleet'
    wait = runtime_v2.STOP_FAILURE_BACKOFF[0]
    assert abs(float(info['stopfail_retry_at']) - time.time() - wait) < 10
    assert info.get('fresh_pending') != '1', 'a plain API error keeps the session'
    assert f.v2.bus.pending(SLOT) == [], 'the fleet never re-sends a service request'
    assert any(SLOT in c and '其他槽照常' in c for c in cards(f, 'supervisor')), cards(f, 'supervisor')
    # the other worker keeps serving
    request(f, 'r2')
    assert answer_turn(f, 'supervisor', route('r2', to='worker-2'))['status'] == 'committed'
    f.pump()
    assert f.v2.bus.slot_state('worker-2').get('status') == 'received'


def test_refusal_gives_the_slot_a_fresh_session_for_its_next_request(service):
    f = service
    inflight(f, 'r1')
    fail(f, error='invalid_request', refusal=True)
    assert result(f, 'r1')['status'] == 'failed' and 'refus' in result(f, 'r1')['error']
    info = f.agent(SLOT)
    assert info['state'] == 'held' and not f.paused()
    assert info.get('fresh_pending') == '1', 'a refused session must not take the next request'
    assert any('拒答' in c and '新会话' in c for c in cards(f, 'supervisor')), cards(f, 'supervisor')


def test_backoff_over_releases_the_slot(service):
    f = service
    inflight(f, 'r1')
    fail(f)
    due(f)
    assert f.agent(SLOT)['state'] == 'idle' and not f.paused()
    assert any(e.get('event') == 'stop_failure.released' for e in audit(f))


def test_consecutive_failures_up_to_the_cap_hold_only_that_slot_without_a_timer(service):
    f = service
    sent = []
    f.notifier = lambda rec: sent.append(rec) or 'sent'  # a managed run (lifecycle set, no parent) also notifies
    cap = 3
    assert int(f.cfg.get('service_stopfail_max') or runtime_v2.SERVICE_STOPFAIL_MAX) == cap
    for i in range(1, cap + 1):
        inflight(f, 'r' + str(i))
        if i > 1:
            rewake(f)
        fail(f)
        if i < cap:
            due(f)
            assert f.agent(SLOT)['state'] == 'idle'
    info = f.agent(SLOT)
    assert info['state'] == 'held' and not float(info.get('stopfail_retry_at') or 0), 'no timer releases a used-up slot'
    assert int(info['stopfail_streak']) == cap
    assert not f.paused(), 'used-up failures still never pause the fleet'
    assert any(e.get('event') == 'stop_failure.exhausted' for e in audit(f))
    assert any(SLOT in c and '连续 3 次' in c for c in cards(f, 'supervisor')), cards(f, 'supervisor')
    f.v2.tick()
    f.pump()
    assert f.agent(SLOT)['state'] == 'held'
    assert sent and any(SLOT in rec['text'] and '连续 3 次' in rec['text'] for rec in sent), sent


def test_a_good_answer_resets_the_failure_streak(service):
    f = service
    inflight(f, 'r1')
    fail(f)
    due(f)
    assert f.agent(SLOT)['stopfail_streak'] == '1'
    inflight(f, 'r2')
    rewake(f)
    assert answer_turn(f, SLOT, '{"reply": "fills ok"}')['status'] == 'committed'
    assert result(f, 'r2')['status'] == 'ok'
    assert f.agent(SLOT).get('stopfail_streak') in ('0', '', None)


def test_deadline_expiry_without_a_stop_failure_holds_only_that_slot(service):
    """Unknown execution state still holds the slot, but a request timeout never pauses the fleet (design §43)."""
    f = service
    f.cfg['request_timeout'] = 120
    inflight(f, 'r1')
    f.r.hset(fleet.REQ + 'r1', 'created', 0)
    f.v2.tick()
    assert result(f, 'r1')['status'] == 'timeout' and not f.paused() and f.agent(SLOT)['state'] == 'held'


def test_service_worker_settings_carry_the_stop_failure_rewake(service):
    """Without it, a released slot's next request sits in the mailbox after a plain API error (no Stop hook waits)."""
    f = service
    gen = int(f.agent(SLOT)['generation'])
    fleet.claude_argv(f.path, f.cfg, SLOT, gen, 'launch prompt')  # writes this generation's settings file
    settings = json.loads((fleet.agent_cwd(f.cfg, SLOT) / ('.fleet-settings-g' + str(gen) + '.json')).read_text())
    entries = settings['hooks']['StopFailure'][0]['hooks']
    assert 'claude-stop-failure' in entries[0]['command']
    assert len(entries) == 2 and entries[1].get('asyncRewake') is True and 'claude-rewake' in entries[1]['command']


def test_after_a_refusal_the_next_request_goes_to_a_fresh_generation(service):
    f = service
    inflight(f, 'r1')
    old = int(f.agent(SLOT)['generation'])
    fail(f, error='invalid_request', refusal=True)
    due(f)
    request(f, 'r2')
    assert answer_turn(f, 'supervisor', route('r2'))['status'] == 'committed'
    for _ in range(5):
        f.pump()
        f.v2.step(20)
    assert int(f.agent(SLOT)['generation']) == old + 1, 'the refused session must not take the next request'
    assert f.agent(SLOT).get('fresh_pending') in ('0', '', None)
    assert any(x.get('event') == 'dispatch.fresh' for _, x in f.r.xrange(f.v2.bus.audit_key)) or \
        any(json.loads(x).get('kind') == 'dispatch.fresh' for x in f.r.lrange(fleet.LOG, 0, -1))
