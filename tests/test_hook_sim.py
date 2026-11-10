"""The in-process waiting-hook stand-in (tests/hook_sim.py) keeps the real delivery and settlement rules.

Legacy tests run on hook_sim, so it must not make anything easier than the real hook: the hand-over
goes through the real ``fleet.hook_session`` accept (lease re-validation, bind write), settlement still
needs this session's bind plus the delivery's isMeta feedback row as the latest start input, and the
daemon's ``tui.hooked`` audit names the generation and message that the hook actually handed over.
"""
import inspect
import json

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_v2_codex_claude import answer_turn, drive, mixed, submit, typed  # noqa: F401  (mixed fixture)
import hook_sim
from cmux_fleet import claude_hook, fleet

REPORT = 'Done:\n- wrote {0}\nEvidence:\n- worker-1/{0}\nNot done: none\nBlocked: none'


def hooked_audit(f, slot):
    return [x for _, x in f.r.xrange(f.v2.bus.audit_key) if x.get('event') == 'tui.hooked' and x.get('slot') == slot]


def bind(f, slot, generation):
    k = fleet.hook_keys(f.cfg, f.r, slot, generation)
    return fleet.hook_bind(f.r, k), k


def thread(f, slot, generation):
    return f.test_threads[slot] if generation == 0 else f.test_threads[slot] + '-g' + str(generation)


def dispatch(f, instruction='Write worker-1/part.md with the findings.'):
    if not typed(f, 'supervisor'):
        submit(f)
    plan = json.dumps({'status': 'RUNNING', 'completion': 10, 'reason': 'go',
                       'dispatch': [{'to': 'worker-1', 'instruction': instruction}]})
    assert answer_turn(f, 'supervisor', plan)['status'] == 'committed'
    f.pump()


def test_hand_over_writes_the_real_bind_and_audits_generation_and_message(mixed):
    f = mixed
    dispatch(f)
    lease = f.v2.bus.slot_state('worker-1')
    got, k = bind(f, 'worker-1', 0)
    # the real accept() wrote the bind for exactly this lease and session
    assert got['kind'] == 'delivery' and got['message_id'] == lease['message_id']
    assert got['delivery_id'] == lease['current_delivery'] and got['session_id'] == thread(f, 'worker-1', 0)
    assert f.r.llen(k['mailbox']) == 0  # taken from the real mailbox, not bypassed
    audit = hooked_audit(f, 'worker-1')[-1]
    assert audit['message_id'] == lease['message_id'] and audit['generation'] == '0' and audit['armed'] == 'true'
    assert not [t for p, t in f.mgr.sent if p == f.agent('worker-1')['pane']]  # never typed


def test_settlement_still_needs_the_feedback_row_and_the_bind(mixed):
    f = mixed
    dispatch(f)
    env = f.v2.bus.slot_state('worker-1')
    # the operator's own line as the latest start input is a manual turn: not settled, although
    # this session's bind for the delivery exists and the feedback row is in the history
    hook_sim_row = hook_sim.user_row(typed(f, 'worker-1')[-1], True)
    transcript = f.test_claude / 'worker-1' / (thread(f, 'worker-1', 0) + '.jsonl')
    transcript.parent.mkdir(parents=True, exist_ok=True)
    rows = [hook_sim_row, {'type': 'user', 'message': {'role': 'user', 'content': 'what are you doing?'}},
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': REPORT.format('part.md')}]}}]
    transcript.write_text(''.join(json.dumps(x) + '\n' for x in rows))
    raw = json.dumps({'session_id': thread(f, 'worker-1', 0), 'transcript_path': str(transcript),
                      'last_assistant_message': REPORT.format('part.md')})
    assert drive(f, lambda: hook_sim.claude_stop(f.path, 'worker-1', '0', raw))['status'] == 'ignored'
    # without this session's bind the feedback row alone settles nothing either
    saved, k = bind(f, 'worker-1', 0)
    f.r.delete(k['bind'])
    assert answer_turn(f, 'worker-1', REPORT.format('part.md'))['status'] == 'ignored'
    f.r.set(k['bind'], json.dumps(saved))
    assert answer_turn(f, 'worker-1', REPORT.format('part.md'))['status'] == 'committed'
    assert f.v2.bus.message(env['message_id'])['outcome'] == 'completed'


def test_stale_mailbox_entry_is_dropped_not_handed_over(mixed):
    f = mixed
    k = fleet.hook_keys(f.cfg, f.r, 'worker-1', 0)
    # an entry whose lease is not this session's received delivery (e.g. left from an older attempt)
    claude_hook.post(f.r, k, {'kind': 'delivery', 'generation': 0, 'message_id': 'm-old', 'delivery_id': 'd-old',
                              'text': 'stale work'})
    assert 'stale work' not in hook_sim.handed_to(f, 'worker-1')
    drops = [x for _, x in f.r.xrange(f.v2.bus.audit_key) if x.get('event') == 'hook.dropped']
    assert drops and drops[-1]['message_id'] == 'm-old'


def test_generation_change_hands_queued_work_to_the_new_generation_only(mixed):
    f = mixed
    dispatch(f)
    first = f.v2.bus.slot_state('worker-1')
    assert answer_turn(f, 'worker-1', REPORT.format('part.md'))['status'] == 'committed'
    f.pump()  # the report reaches the supervisor before its next decision
    f.setf('worker-1', ctx_used=990000, ctx_window=1000000, delivered=1)
    f.usage = lambda name, _real=f.usage: (990000, 1000000) if name == 'worker-1' else _real(name)
    dispatch(f, 'Write worker-1/after.md with the findings.')  # queued behind the handoff
    assert f.agent('worker-1')['state'] == 'handoff'
    handoff = typed(f, 'worker-1')[-1]
    assert 'after.md' not in handoff
    answer_turn(f, 'worker-1', 'handoff written')
    f.pump()
    assert f.agent('worker-1')['generation'] == '1' and f.agent('worker-1')['state'] == 'starting'
    # nothing was handed to the retired generation after its handoff turn
    assert not [t for t in hook_sim.texts('worker-1', f.v2.bus.prefix, 0) if 'after.md' in t]
    assert answer_turn(f, 'worker-1', 'READY', startup=True)['status'] == 'ready'
    f.pump()
    lease = f.v2.bus.slot_state('worker-1')
    assert lease['generation'] == '1' and lease['message_id'] != first['message_id']
    assert any('after.md' in t for t in hook_sim.texts('worker-1', f.v2.bus.prefix, 1))
    got, _ = bind(f, 'worker-1', 1)
    assert got['message_id'] == lease['message_id'] and got['session_id'] == thread(f, 'worker-1', 1)
    audit = [a for a in hooked_audit(f, 'worker-1') if a['message_id'] == lease['message_id']]
    assert audit and audit[-1]['generation'] == '1'
    # a Stop of the retired generation cannot settle it; the new generation's own Stop does
    assert answer_turn(f, 'worker-1', REPORT.format('after.md'), generation=0)['status'] == 'stale'
    assert answer_turn(f, 'worker-1', REPORT.format('after.md'))['status'] == 'committed'
    assert not [t for p, t in f.mgr.sent if p in (f.agent('worker-1')['pane'],)]


def test_stand_in_takes_exactly_the_real_wait_signature():
    # hook_sim replaces only the blocking wait: an argument the real wait gains (e.g. transcript=)
    # must not be swallowed by a catch-all, or the legacy tests would hide an interface break
    stand_in = inspect.signature(hook_sim.next_delivery).parameters
    assert list(stand_in.items()) == list(inspect.signature(fleet.claude_hook_next).parameters.items())
    assert not [p for p in stand_in.values() if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD)]
