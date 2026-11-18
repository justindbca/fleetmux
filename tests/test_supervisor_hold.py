"""A supervisor whose turn ends with an API error (Claude StopFailure) is recovered by the daemon (design.md §24).

Incident 2026-11-14 (remote-dev, sub-fleet M0): the sub-fleet supervisor's turn ended with "API Error: Connection
lost mid-response". The StopFailure hook posted no event for a supervisor, so the daemon kept renewing its lease;
the watchdog froze it 23 minutes later, held it and paused the sub-fleet, and nothing moved for 70 more minutes
until an outside session ran `fleetmux resume`. The daemon should have found this itself.

Now a Claude supervisor (also the adopted supervisor of a sub-fleet, routed by ``subfleet.delegated_route``) posts
the ``stop_failure`` event like a worker: the delivery is settled as not adopted, the same message goes to the
head of its queue, only that slot is held for the backoff (no pause), and the daemon's own tick hands it back.

Stubbed: fake cmux world, private Redis under /tmp, in-process daemons, hook_sim; the real ~/.fleetmux is never
written (tests/conftest.py pins FLEETMUX_HOME / FLEETMUX_STATE to tmp_path).
"""
import json
import threading
import time

import pytest

import hook_sim  # noqa: E402
from fleetmux import advisor, claude_hook, fleet, runtime_v2
from test_subfleet import adopt_by_hook, hooked_turn, route_on, spawn_m0, step
from test_subfleet import tree  # noqa: F401  (fixture)

DROPPED = 'API Error: Connection lost mid-response. The response above may be incomplete.'


def audit_rows(f):
    return [x for _, x in f.r.xrange(f.v2.bus.audit_key)]


def queued_texts(f, slot):
    out = []
    for x in audit_rows(f):
        if x.get('event') == 'queued' and x.get('slot') == slot:
            try:
                out.append(json.loads(x.get('detail') or '{}').get('text') or '')
            except ValueError:
                pass
    return out


def pending_texts(f, slot):
    return [f.v2.bus.message(m)['body'].get('text') or '' for m in f.v2.bus.pending(slot)]


def no_outside_command(f):
    """Nothing came from outside the fleet: no `resume` command event, no operator input (the daemon's own
    auto-resume audits execution.auto_resumed, not a resume event)."""
    events = {x.get('event') for x in audit_rows(f)}
    assert not events & {'resume', 'operator_input'}, events


def adopted_supervisor_working(t, monkeypatch):
    """The real-run start: a sub-fleet M0 whose supervisor is worker-1's pane (adopted by hook), handed its task."""
    route_on(t)
    rec, _ = spawn_m0(t)
    cf, thread = adopt_by_hook(t, monkeypatch, rec)
    step(t)
    task = hook_sim.handed_to(cf, 'supervisor')[-1]
    assert 'Research five sources' in task and cf.v2.bus.slot_state('supervisor').get('current_delivery')
    return cf, rec, thread


def failure_raw(t, thread, text=DROPPED, error='server_error'):
    transcript = t.claude / (thread + '.jsonl')
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.touch()
    return json.dumps({'session_id': thread, 'transcript_path': str(transcript), 'error': error,
                       'last_assistant_message': text})


def start_rewake(t, cf, raw, monkeypatch):
    """The pane's StopFailure asyncRewake (worker-1's pinned hook; it follows the route into the child)."""
    monkeypatch.setattr(claude_hook, 'GRACE', 0)
    cf.r.delete(cf.v2.hook_keys('supervisor', 0)['stopping'])
    hook_sim._armed.pop(cf.v2.hook_keys('supervisor', 0)['mailbox'], None)  # nothing waits on a Stop: it failed
    box = {}
    th = threading.Thread(target=lambda: box.setdefault('text', fleet.claude_rewake(t.f.path, 'worker-1', '0', raw)),
                          daemon=True)
    th.start()
    return th, box


def backoff_due(t, cf):
    cf.setf('supervisor', stopfail_retry_at=time.time() - 1)
    cf.tick()
    step(t)


# ------------------------------------------------------------------ R1 + R2 + R5: the incident
def test_subfleet_supervisor_stop_failure_is_recovered_and_redelivered_without_any_command(tree, monkeypatch):
    """Incident reproduction: sub-fleet supervisor (delegated_route) -> StopFailure -> held -> the daemon alone
    releases it and the same message is handed to the same session again; the sub-fleet never pauses."""
    t = tree
    cf, rec, thread = adopted_supervisor_working(t, monkeypatch)
    original = cf.v2.bus.slot_state('supervisor')['message_id']
    raw = failure_raw(t, thread)
    assert fleet.claude_stop_failure(t.f.path, 'worker-1', 0, raw)  # the pane's hook is pinned to the parent slot
    th, box = start_rewake(t, cf, raw, monkeypatch)
    for _ in range(3):
        step(t)
    # R1: the event reached the child daemon; R2: settled as not adopted, requeued at the head, only this slot held
    events = audit_rows(cf)
    assert any(x.get('event') == 'stop_failure.retry' and x.get('slot') == 'supervisor' for x in events), \
        'no stop_failure event was handled for the sub-fleet supervisor'
    assert cf.v2.bus.message(original)['status'] == 'committed'
    assert cf.v2.bus.message(original)['outcome'] != 'completed'
    head = pending_texts(cf, 'supervisor')
    assert len(head) == 1 and head[0].startswith(runtime_v2.STOP_FAILURE_RESUME_SUPERVISOR)
    assert 'Research five sources' in head[0]
    info = cf.agent('supervisor')
    assert info['state'] == 'held' and info.get('stopfail_at') and not cf.paused()
    assert not t.f.paused()
    # During the backoff nothing takes it back a second time (adopted_held_tick stays out of a StopFailure hold)
    keys_before = list(t.world.keys)
    for _ in range(3):
        cf.tick()
        step(t)
    assert len(pending_texts(cf, 'supervisor')) == 1
    assert not any(x.get('event') == 'adopted.reattached' for x in audit_rows(cf))
    assert t.world.keys == keys_before, 'an Escape was sent during a StopFailure backoff'
    # The backoff ends on the daemon's own tick: the waiting rewake hands the same message over
    backoff_due(t, cf)
    th.join(20)
    assert not th.is_alive() and box.get('text'), box
    assert box['text'].startswith(runtime_v2.STOP_FAILURE_RESUME_SUPERVISOR) and 'Research five sources' in box['text']
    decision = json.dumps({'status': 'RUNNING', 'completion': 10, 'reason': 'split', 'dispatch': [
        {'to': w, 'instruction': 'Write ' + w + '/part.md with two sources each'} for w in cf.workers]})
    assert hooked_turn(t, 'worker-1', 0, thread, box['text'], decision)['status'] == 'committed'
    no_outside_command(cf)
    assert not cf.paused() and cf.task()['status'] == 'running'


def test_subfleet_supervisor_retries_used_up_escalate_to_the_parent_supervisor(tree, monkeypatch):
    """max_attempts=2: the retry fails again -> the slot stays held (no pause) and, since a supervisor cannot be
    told about itself, the parent fleet's supervisor gets a [sub-fleet held] notice."""
    t = tree
    cf, rec, thread = adopted_supervisor_working(t, monkeypatch)
    raw = failure_raw(t, thread)
    assert fleet.claude_stop_failure(t.f.path, 'worker-1', 0, raw)
    th, box = start_rewake(t, cf, raw, monkeypatch)
    step(t)
    backoff_due(t, cf)
    th.join(20)
    assert box.get('text', '').startswith(runtime_v2.STOP_FAILURE_RESUME_SUPERVISOR), box
    assert fleet.claude_stop_failure(t.f.path, 'worker-1', 0, raw)  # the retried turn fails too
    for _ in range(3):
        step(t)
    info = cf.agent('supervisor')
    assert info['state'] == 'held' and not cf.paused()
    assert any(x.get('event') == 'stop_failure.exhausted' and x.get('slot') == 'supervisor' for x in audit_rows(cf))
    # The message is not dropped: a supervisor keeps being retried at the longest backoff (its turns are decisions,
    # and nobody else can answer them), so the run recovers once the API answers again, however long that takes.
    head = pending_texts(cf, 'supervisor')
    assert len(head) == 1 and 'Research five sources' in head[0]
    assert float(cf.agent('supervisor').get('stopfail_retry_at') or 0) > time.time()
    cf.tick()  # the escalation leaves on the daemon's next tick (outside the intent that decided it)
    assert any(x.get('event') == 'held_watch.escalated' for x in audit_rows(cf))
    t.f.tick()  # the parent's notices part
    step(t)
    assert any('[sub-fleet held]' in x and 'supervisor' in x for x in queued_texts(t.f, 'supervisor')), \
        queued_texts(t.f, 'supervisor')
    no_outside_command(cf)


def test_adopted_supervisor_refusal_is_escalated_not_redelivered(tree, monkeypatch):
    """A model refusal: that session takes no more work. The child cannot replace a pane the parent owns, so the
    message is not sent to it again; the slot stays held and the parent's supervisor is told."""
    t = tree
    cf, rec, thread = adopted_supervisor_working(t, monkeypatch)
    raw = failure_raw(t, thread, text='I cannot help with that.', error='invalid_request')
    transcript = json.loads(raw)['transcript_path']
    with open(transcript, 'a') as fh:
        fh.write(json.dumps({'type': 'assistant', 'message': {'stop_reason': 'refusal', 'content': []}}) + '\n')
    assert fleet.claude_stop_failure(t.f.path, 'worker-1', 0, raw)
    for _ in range(3):
        step(t)
    assert cf.agent('supervisor')['state'] == 'held' and not cf.paused()
    assert pending_texts(cf, 'supervisor') == [], 'a refused message went back to the same session'
    cf.tick()
    assert any(x.get('event') == 'held_watch.escalated' for x in audit_rows(cf))
    t.f.tick()
    step(t)
    assert any('[sub-fleet held]' in x and 'refus' in x for x in queued_texts(t.f, 'supervisor'))


# ------------------------------------------------------------------ R1 + R2: a root Claude supervisor
from test_claude_supervisor_hook import operator, supervisor  # noqa: E402,F401  (fixture)
from test_claude_hook_delivery import delivered, settings  # noqa: E402


def root_failure(f, text='API Error: Connection lost mid-response.', refusal=False):
    thread = f.test_threads['supervisor']
    raw = {'session_id': thread, 'error': 'server_error', 'last_assistant_message': text}
    if refusal:
        path = f.run_dir / ('refusal-' + thread + '.jsonl')
        path.write_text(json.dumps({'type': 'assistant', 'message': {'stop_reason': 'refusal', 'content': []}}) + '\n')
        raw['transcript_path'] = str(path)
    fleet.claude_stop_failure(f.path, 'supervisor', 0, json.dumps(raw))
    for _ in range(5):
        f.v2.step(20)


@pytest.mark.parametrize('supervisor', ['task'], indirect=True)
def test_root_claude_supervisor_stop_failure_requeues_its_message(supervisor):
    f = supervisor
    assert '已排队' in operator(f, 'marker one')
    delivered(f, f.ready_hook)
    before = f.v2.bus.slot_state('supervisor')
    root_failure(f)
    assert f.v2.bus.message(before['message_id'])['status'] == 'committed'
    head = pending_texts(f, 'supervisor')
    assert len(head) == 1 and head[0].startswith(runtime_v2.STOP_FAILURE_RESUME_SUPERVISOR) and 'marker one' in head[0]
    assert f.agent('supervisor')['state'] == 'held' and not f.paused()
    # its StopFailure group carries the asyncRewake that hands the retry over (no Stop hook waits after it)
    group = settings(f, 'supervisor', 0)['hooks']['StopFailure'][0]['hooks']
    assert any(h.get('asyncRewake') and 'claude-rewake' in h['command'] for h in group)


@pytest.mark.parametrize('supervisor', ['service'], indirect=True)
def test_service_supervisor_stop_failure_fails_its_request_and_holds_only_that_slot(supervisor):
    """service mode keeps §24's service rule: the request the supervisor was routing fails at once (never re-sent:
    the requester owns retries), only that slot is held for a backoff, the run is not paused."""
    f = supervisor
    assert '已排队' in operator(f, 'marker two')
    delivered(f, f.ready_hook)
    before = f.v2.bus.slot_state('supervisor')
    rid = f.v2.bus.message(before['message_id'])['request_id']
    assert f.r.hgetall(fleet.REQ + rid)
    root_failure(f)
    assert f.v2.bus.message(before['message_id'])['status'] == 'committed'
    assert not any('marker two' in x for x in pending_texts(f, 'supervisor'))
    info = f.agent('supervisor')
    assert info['state'] == 'held' and info.get('stopfail_retry_at') and not f.paused()


@pytest.mark.parametrize('supervisor', ['service'], indirect=True)
def test_service_supervisor_good_decision_ends_its_stop_failure_streak(supervisor):
    """service_stopfail_max counts consecutive failures: a supervisor decision that settles ends the streak (else
    three API errors over a service fleet's life would hold its supervisor for good)."""
    from test_claude_hook_delivery import stop, step as hstep
    f = supervisor
    f.setf('supervisor', stopfail_streak=2)
    assert '已排队' in operator(f, 'marker four')
    delivered(f, f.ready_hook)
    lease = f.v2.bus.slot_state('supervisor')
    rid = f.v2.bus.message(lease['message_id'])['request_id']
    decision = dict(status='RUNNING', skip=[], dispatch=[dict(to='worker-1', request_id=rid, instruction='Write the marker.')])
    stop(f, 'supervisor', json.dumps(decision))
    assert hstep(f, lambda: f.v2.bus.message(lease['message_id'])['status'] == 'committed')
    assert f.agent('supervisor').get('stopfail_streak') in ('0', '', None)


@pytest.mark.parametrize('supervisor', ['task'], indirect=True)
def test_root_supervisor_refusal_gets_a_fresh_session(supervisor):
    f = supervisor
    assert '已排队' in operator(f, 'marker three')
    delivered(f, f.ready_hook)
    root_failure(f, text='I cannot help with that.', refusal=True)
    info = f.agent('supervisor')
    assert info['state'] == 'held' and info.get('fresh_pending') == '1' and info.get('stopfail_fresh') == '1'
    assert any('marker three' in x for x in pending_texts(f, 'supervisor'))


# ------------------------------------------------------------------ advisor (decision: §27 fault branch)
from test_claude_hook_delivery import hooked, handed  # noqa: E402,F401  (fixture)
from test_message_bus_v2 import private_redis_v2  # noqa: E402,F401  (fixture)


def test_advisor_stop_failure_takes_the_advisor_fault_branch(hooked, monkeypatch):
    """The advisor posts the event too, but is never re-sent its consultation (§27: faults fall back, never re-run):
    the daemon holds it on the advisor branch (advisor_fault; advisor_tick restarts it), no pause, no retry."""
    f = hooked
    handed(f)
    monkeypatch.setattr(advisor, 'SLOT', 'worker-1')  # stand-in: worker-1 plays the resident advisor slot
    monkeypatch.setattr(fleet, 'ADVISOR', 'worker-1')
    thread = f.test_threads['worker-1']
    fleet.claude_stop_failure(f.path, 'worker-1', int(f.agent('worker-1')['generation']), json.dumps(
        {'session_id': thread, 'error': 'server_error', 'last_assistant_message': DROPPED}))
    for _ in range(5):
        f.v2.step(20)
    assert f.agent('worker-1').get('advisor_fault', '').startswith('API error')
    assert not any(x.get('event') == 'stop_failure.retry' for x in audit_rows(f))
    assert not f.paused() and f.v2.bus.pending('worker-1') == []
