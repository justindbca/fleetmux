"""A delivery to an idle Claude session whose hooks are gone (brief idle-reclaim-prereq 3.2 / 4.2).

Claude Code killed the Stop hook and its asyncRewake fallback at their timeout (or Escape / a crash took them):
the session sits idle and nothing waits on its mailbox (``armed=false``). Before, a delivery then waited for the
stall path or, for a handoff, the whole ``handoff_timeout`` (900 s). Now the daemon sees the session is really idle
and wakes it without typing (design 48): it resumes the same Claude session as the next generation and hands it
the untaken delivery again (back at the head of the queue, no attempt used); an untaken handoff is offered again
to the resumed session, so the model still writes its note.

Real fleet: tests/hook_world.py (demo World, real daemon, scripted actors running the real hooks)."""
import json

from hook_world import hook_world  # noqa: F401  (fixture)
from fleetmux import claude_hook, demo_actor, runtime_v2

HOOK_CHANNEL = True  # the real Stop / rewake hooks run (tests/conftest.py: no hook_sim stand-in)

WAKE = 60  # seconds: detection (2 x GRACE) + launch + READY of the resumed session, far below handoff_timeout (900 s)


def resumed(w, slot, thread):
    info = w.agent(slot)
    return (info.get('generation') == '1' and info.get('runtime_ready') == '1'
            and info.get('resume_session') == thread)


def hooked(w, slot, generation, prefix=''):
    """tui.hooked audits of ``slot`` for ``generation`` (message ids starting with ``prefix``)."""
    return [row for row in w.audit('tui.hooked') if row.get('slot') == slot and row.get('generation') == str(generation)
            and (row.get('message_id') or '').startswith(prefix)]


def test_unarmed_idle_worker_is_resumed_and_takes_the_delivery(hook_world):  # noqa: F811
    w = hook_world
    thread = w.agent('worker-1')['thread']
    w.end_hooks('worker-1')  # the demo supervisor dispatches to worker-1
    w.w.submit()
    assert w.wait(lambda: hooked(w, 'worker-1', 0), 30), 'the task never reached worker-1'
    assert w.wait(lambda: resumed(w, 'worker-1', thread), WAKE), (
        'the unarmed idle worker was not woken within ' + str(WAKE) + ' s: ' + json.dumps(w.agent('worker-1')))
    assert w.audit('hook.unarmed_wake'), 'no hook.unarmed_wake audit'
    # the same work reaches the resumed session through its Stop hook and is done there
    assert w.wait(lambda: (w.workspace() / demo_actor.SLUG).is_file(), 30), \
        'the resumed worker never did the work'
    first = hooked(w, 'worker-1', 0)[0]
    again = hooked(w, 'worker-1', 1)
    assert again, 'the resumed generation never got the delivery'
    old, new = w.bus.message(first['message_id']), w.bus.message(again[0]['message_id'])
    assert (new.get('replay') or '1') == (old.get('replay') or '1'), 'waking it used up an attempt'
    assert not w.audit('handoff.timeout')


def test_unarmed_idle_worker_with_a_handoff_is_resumed_and_writes_it(hook_world):  # noqa: F811
    w = hook_world
    thread = w.agent('worker-1')['thread']
    w.end_hooks('worker-1')
    # over its context budget after one delivery: the dispatch starts a handoff first (fleet.deliver)
    w.r.hset('fx:agent:worker-1', mapping={'ctx_used': 10 ** 9, 'delivered': 1})
    w.w.submit()
    assert w.wait(lambda: hooked(w, 'worker-1', 0, 'm-handoff-'), 30), 'no handoff was offered to worker-1'
    assert w.wait(lambda: resumed(w, 'worker-1', thread), WAKE), (
        'the unarmed idle worker with a handoff was not woken within ' + str(WAKE) + ' s (handoff_timeout is 900 s): '
        + json.dumps(w.agent('worker-1')))
    # the handoff is offered again to the resumed session, which takes it through its own Stop hook
    assert w.wait(lambda: [row for row in hooked(w, 'worker-1', 1, 'm-handoff-') if row.get('armed') == 'true'], 30), \
        'the handoff was not taken by the resumed session'


def test_unarmed_idle_direct_supervisor_is_resumed_and_takes_the_task(hook_world):  # noqa: F811
    w = hook_world
    thread = w.agent('supervisor')['thread']
    w.end_hooks('supervisor')
    w.w.submit()
    assert w.wait(lambda: hooked(w, 'supervisor', 0), 30), 'the task never reached the supervisor'
    assert w.wait(lambda: resumed(w, 'supervisor', thread), WAKE), (
        'the unarmed idle supervisor was not woken within ' + str(WAKE) + ' s: ' + json.dumps(w.agent('supervisor')))
    assert w.wait(lambda: hooked(w, 'supervisor', 1), 30), 'the resumed supervisor never got the task'
    # it dispatches as before: the work reaches worker-1
    assert w.wait(lambda: hooked(w, 'worker-1', 0), 30), 'the resumed supervisor did not dispatch'
    assert w.audit('supervisor.revived'), 'no revive summary for the resumed supervisor'


# ------------------------------------------------------------------ "really idle": the transcript ends a turn
def rows(path, *items):
    path.write_text(''.join(json.dumps(item) + '\n' for item in items))
    return str(path)


def test_turn_done_reads_the_transcript_tail(tmp_path):
    user = {'type': 'user', 'message': {'role': 'user', 'content': 'do it'}}
    text = {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': 'done'}]}}
    tool = {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'type': 'tool_use', 'name': 'Bash'}]}}
    result = {'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'content': 'ok'}]}}
    meta = {'type': 'user', 'isMeta': True, 'message': {'role': 'user', 'content': 'Stop hook feedback:\nnext'}}
    escape = {'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'text',
                                                                        'text': claude_hook.INTERRUPTED + ']'}]}}
    summary = {'type': 'system', 'subtype': 'stop_hook_summary'}
    done = runtime_v2.transcript_turn_done
    assert done(rows(tmp_path / 'a.jsonl', user, text, summary))
    assert not done(rows(tmp_path / 'b.jsonl', user))  # the turn just started
    assert not done(rows(tmp_path / 'c.jsonl', user, tool))  # a tool runs
    assert not done(rows(tmp_path / 'd.jsonl', user, tool, result))  # the model answers the tool
    assert not done(rows(tmp_path / 'e.jsonl', user, text, meta))  # a block started the next turn
    assert done(rows(tmp_path / 'f.jsonl', user, tool, escape))  # Escape ended the turn
    assert not done(str(tmp_path / 'missing.jsonl'))
