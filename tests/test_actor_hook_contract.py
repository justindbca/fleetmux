"""Actor hook contract: the scripted agent and the real Claude Code CLI keep one hook protocol.

The fleet talks to a Claude pane only through what ``fleet.claude_argv`` writes (the argv and the ``--settings``
hooks: ``claude-stop``, ``claude-rewake``, ``claude-stop-failure``, ``claude-posttool``, ``claude-prompt``) and
reads back only the hook payloads, the hook outputs it returns, the transcript (``fleet.claude_user_prompts``,
``claude_hook.feedback_text`` / ``rewake_prompt_text``) and the screen (``claude_input.visible_editor``). Every case
below runs the same body against two subjects:

- ``fake``: ``fleetmux/demo_actor.py`` behind the demo's ``#!/bin/sh`` launcher. Always runs.
- ``real``: the Claude Code CLI at ``$FLEETMUX_CONTRACT_REAL_CLAUDE``. Opt-in; without the variable the case is a
  skip, allowlisted in tests/skip_allowlist.yaml and listed in the skip report (never left uncollected).

Each case uses the settings ``fleet.claude_argv`` writes, with only each hook's ``command`` replaced by a recorder
(tests/actor_contract_kit.py) that records the payload and answers from a plan. What a case asserts is what the
fleet's readers consume, as measured on Claude Code 2.1.280 (fleetmux/claude_hook.py docstring,
tests/test_claude_hook_delivery.py docstring). StopFailure and PostToolUse are not shared cases: the scripted agent
never fails a turn and never calls a tool, so neither event occurs on it (tests/test_claude_hook_delivery.py covers
the daemon side of StopFailure).
"""
import os
import re
import subprocess

import pytest

import actor_contract_kit as kit
from fleetmux import claude_hook, claude_input, discover

HOOK_CHANNEL = True  # tests/conftest.py: no in-process stand-in for the waiting hook; this module runs none anyway

REAL = os.environ.get(kit.REAL_ENV, '')


@pytest.fixture(params=['fake', pytest.param('real', marks=pytest.mark.skipif(
    not REAL, reason='opt-in: %s is not set (the real Claude CLI side of the actor hook contract)' % kit.REAL_ENV))])
def subject(request):
    return request.param


@pytest.fixture
def world(subject, tmp_path):
    w = kit.World(tmp_path, subject, REAL)
    yield w
    w.close()


# ---------------------------------------------------------------- launch and the end of a turn (claude-stop, claude-rewake)

def test_the_cli_reports_a_version_the_fleet_accepts(world):
    """deps.yaml `claude` minimum, read by discover.below_min from `<cli> --version` (setup / start)."""
    out = subprocess.run([world.cli, '--version'], capture_output=True, text=True, env=world.env, timeout=60)
    text = out.stdout + out.stderr
    assert out.returncode == 0 and discover.version(text) is not None, text
    assert discover.below_min('claude', text) is None, text


def test_a_turn_end_runs_both_stop_hooks_with_the_payload_the_fleet_reads(world):
    """claude-stop / claude-rewake: the launch prompt is the first turn; its end runs the Stop entry's blocking hook
    and its asyncRewake hook, each with the Stop payload on stdin, the settings env and the agent cwd."""
    s = world.start('worker-1')
    stop = world.wait_calls('stop', 1)[0]
    rewake = world.wait_calls('rewake', 1)[0]
    for call in (stop, rewake):
        p = call['payload']
        assert p.get('hook_event_name') == 'Stop' and p.get('stop_hook_active') is False, p
        assert isinstance(p.get('session_id'), str) and p['session_id'], p
        assert world.inside(p.get('transcript_path')), p          # the world's own HOME / config dir, never the user's
        assert world.same_dir(p.get('cwd'), s.cwd) and world.same_dir(call['cwd'], s.cwd), call
        assert call['env'].get('CLAUDE_CODE_STOP_HOOK_BLOCK_CAP') == '0', call['env']
        assert call['env'].get('DISABLE_AUTOUPDATER') == '1', call['env']
    assert rewake['payload']['session_id'] == stop['payload']['session_id']
    assert kit.reply_text(stop['payload']).strip(), stop['payload']            # what claude_stop reports
    prompts = world.prompts(stop['payload'])
    assert prompts and prompts[-1] == kit.LAUNCH_PROMPT and prompts[-1].origin == 'user', prompts


def test_a_block_decision_is_the_next_turn_and_reads_as_a_hook_delivery(world):
    """claude-stop answers {"decision": "block", "reason": text}: the CLI runs it as the next turn of the same
    session, the transcript row reads back as a hook delivery, the next Stop has stop_hook_active true."""
    world.plan('stop', [{'stdout': claude_hook.block(kit.text('delivery 1'))}])
    world.start('worker-1')
    first, second = world.wait_calls('stop', 2)
    assert second['payload']['session_id'] == first['payload']['session_id']
    assert second['payload']['transcript_path'] == first['payload']['transcript_path']
    assert second['payload'].get('stop_hook_active') is True, second['payload']
    prompts = world.prompts(second['payload'])
    assert prompts[-1].strip() == kit.text('delivery 1') and prompts[-1].origin == 'hook', prompts


def test_ten_blocks_in_a_row_all_run_with_the_block_cap_off(world):
    """claude_hook.ENV (CLAUDE_CODE_STOP_HOOK_BLOCK_CAP=0 in the settings env): the 9th and later consecutive blocks
    are not overridden (measured: without it the 9th is)."""
    texts = [kit.text('delivery %d' % i) for i in range(1, 11)]
    world.plan('stop', [{'stdout': claude_hook.block(t)} for t in texts])
    world.start('worker-1')
    calls = world.wait_calls('stop', 11)
    assert all(c['payload'].get('stop_hook_active') is True for c in calls[1:]), [c['payload'] for c in calls]
    hooked = [p.strip() for p in world.prompts(calls[-1]['payload'], count=50) if p.origin == 'hook']
    assert hooked == texts, hooked


def test_an_async_rewake_exit_2_is_the_next_turn_in_the_shape_the_fleet_parses(world):
    """claude-rewake: exit 2 with the delivery on stderr wakes the session; the row is a task notification whose
    whole content claude_hook.rewake_prompt_text accepts and claude_user_prompts reads as a hook delivery."""
    world.plan('rewake', [{'stderr': kit.text('rewake 1'), 'exit': 2}])
    world.start('worker-1')
    second = world.wait_calls('stop', 2)[1]
    prompts = world.prompts(second['payload'])
    assert prompts[-1].strip() == kit.text('rewake 1') and prompts[-1].origin == 'hook', prompts
    rows = [r for r in kit.rows(second['payload']['transcript_path']) if r.get('type') == 'user'
            and (r.get('origin') or {}).get('kind') == 'task-notification']
    assert rows and claude_hook.rewake_prompt_text(kit.content_text(rows[-1])) == kit.text('rewake 1'), rows


def test_hooks_that_exit_0_without_output_leave_the_session_idle(world):
    """A Stop that hands nothing over (both hooks exit 0, no output) starts no turn."""
    s = world.start('worker-1')
    first = world.wait_calls('stop', 1)[0]
    world.wait_calls('rewake', 1)
    world.stays('no second Stop', lambda: len(world.calls('stop')) == 1)
    assert len(world.prompts(first['payload'])) == 1
    view = s.editor()
    assert view is not None and view.idle, s.text()


def test_a_hook_past_its_timeout_is_killed_and_its_answer_dropped(world):
    """A blocking Stop hook still running at its settings timeout is killed and its output dropped: no turn, the
    session sits idle with no hook waiting (claude_hook docstring, "unarmed")."""
    world.plan('stop', [{'sleep': 60, 'stdout': claude_hook.block(kit.text('too late'))}])
    s = world.start('worker-1', stop_timeout=3)
    call = world.wait_calls('stop', 1)[0]
    world.wait('the timed-out hook process is gone', lambda: not kit.alive(call['pid']), world.turn_wait)
    world.stays('no turn from the killed hook', lambda: len(world.calls('stop')) == 1)
    view = s.editor()
    assert view is not None and view.idle, s.text()


# ---------------------------------------------------------------- the operator at the pane while a hook waits

def test_a_line_typed_while_the_stop_hook_waits_is_queued_then_runs_as_typed_input(world):
    """claude_hook OperatorQueue: Enter while the Stop hook waits writes a queue-operation enqueue row at once and
    runs nothing; when the hook returns the line runs as its own turn, read back as user input (not a delivery)."""
    release = world.root / 'release-stop'
    world.plan('stop', [{'wait_file': str(release)}])
    s = world.start('worker-1')
    first = world.wait_calls('stop', 1)[0]
    s.type(kit.TYPED_LINE)
    s.key('Enter')
    path = first['payload']['transcript_path']
    world.wait('an enqueue row for the typed line', lambda: any(
        r.get('type') == 'queue-operation' and r.get('operation') == 'enqueue' and kit.TYPED_LINE in str(r.get('content'))
        for r in kit.rows(path)), 5)
    assert world.prompts(first['payload'])[-1] == kit.LAUNCH_PROMPT       # queued, not run
    release.write_text('go')
    second = world.wait_calls('stop', 2)[1]
    prompts = world.prompts(second['payload'])
    assert prompts[-1].strip() == kit.TYPED_LINE and prompts[-1].origin == 'user', prompts


def test_escape_while_the_stop_hook_waits_kills_it_and_leaves_the_session_idle(world):
    """Escape SIGTERMs the waiting Stop hook (test_claude_hook_delivery docstring); no turn starts."""
    world.plan('stop', [{'wait_file': str(world.root / 'never')}])
    s = world.start('worker-1')
    call = world.wait_calls('stop', 1)[0]
    world.wait('the screen shows the hook running', lambda: s.state()[1] is False, 10)
    s.key('Escape')
    world.wait('the waiting hook process is gone', lambda: not kit.alive(call['pid']), 10)
    world.stays('no turn after Escape', lambda: len(world.calls('stop')) == 1)
    world.wait('the editor is idle', lambda: s.state()[1] is True, 10)


def test_the_screen_shows_the_ruled_editor_the_fleet_reads(world):
    """claude_input.visible_editor: busy while the Stop hook runs, idle once it returned, and a typed draft is read
    back from the screen exactly (this is the only way the fleet looks at a Claude input box; it never types)."""
    release = world.root / 'release-stop'
    world.plan('stop', [{'wait_file': str(release)}])
    s = world.start('worker-1')
    world.wait_calls('stop', 1)
    world.wait('busy while the Stop hook runs', lambda: s.state()[1] is False, 10)
    release.write_text('go')
    world.wait('idle and empty after the hook returned',
               lambda: s.state() == ('', True), world.turn_wait)
    s.type(kit.DRAFT)
    world.wait('the draft on screen', lambda: s.state()[0] == kit.DRAFT, 10)
    view = s.editor()
    assert view.idle and view.exact, view


# ---------------------------------------------------------------- operator input in a supervisor slot (claude-prompt)

def test_a_typed_line_in_a_supervisor_slot_goes_through_the_prompt_hook_and_its_block_holds_it(world):
    """claude-prompt (fleet.prompt_hook_slot: a v2 Claude supervisor): a line the operator types is first handed to
    the UserPromptSubmit hook (payload: the prompt, session, transcript); a block decision keeps it from running
    (the fleet queues it as a daemon request instead, design section 5)."""
    world.plan('prompt', [{'stdout': claude_hook.block('[contract] queued by the fleet')}])
    s = world.start('supervisor')
    first = world.wait_calls('stop', 1)[0]
    world.wait('the editor is idle', lambda: s.state()[1] is True, world.turn_wait)
    s.type(kit.TYPED_LINE)
    s.key('Enter')
    call = world.wait_calls('prompt', 1)[0]
    p = call['payload']
    assert p.get('hook_event_name') == 'UserPromptSubmit' and p.get('prompt') == kit.TYPED_LINE, p
    assert p.get('session_id') == first['payload']['session_id'], p
    assert p.get('transcript_path') == first['payload']['transcript_path'], p
    world.stays('the blocked line starts no turn', lambda: len(world.calls('stop')) == 1)
    assert all(x.strip() != kit.TYPED_LINE for x in world.prompts(first['payload'])), world.prompts(first['payload'])



def test_a_rewake_in_a_supervisor_slot_also_passes_the_prompt_hook(world):
    """claude-prompt + claude-rewake: Claude Code submits an asyncRewake notification through UserPromptSubmit too
    (measured on 2.1.307, fleet.supervisor_rewake_prompt); the hook gets the whole notification, which
    claude_hook.rewake_prompt_text accepts, and letting it through (exit 0) runs it as the next turn."""
    world.plan('rewake', [{'stderr': kit.text('rewake 1'), 'exit': 2}])
    world.start('supervisor')
    world.wait_calls('stop', 1)
    call = world.wait_calls('prompt', 1)[0]
    assert call['payload'].get('hook_event_name') == 'UserPromptSubmit', call['payload']
    assert claude_hook.rewake_prompt_text(call['payload'].get('prompt')) == kit.text('rewake 1'), call['payload']
    second = world.wait_calls('stop', 2)[1]
    prompts = world.prompts(second['payload'])
    assert prompts[-1].strip() == kit.text('rewake 1') and prompts[-1].origin == 'hook', prompts

# ---------------------------------------------------------------- the harness itself

def test_the_settings_keep_the_fleet_structure_and_only_the_commands_change(world):
    """Guards every case above against passing vacuously: the hook layout is fleet.claude_argv's own, and no hook
    command of the fleet is left (a leftover would talk to no daemon)."""
    for slot in ('worker-1', 'supervisor'):
        original, rewritten = world.settings(slot)
        assert set(original['hooks']) == set(rewritten['hooks']), (original['hooks'].keys(), rewritten['hooks'].keys())
        stop = rewritten['hooks']['Stop'][0]['hooks']
        assert [bool(h.get('asyncRewake')) for h in stop] == [False, True], stop
        assert stop[1].get('rewakeMessage') == claude_hook.REWAKE_MESSAGE
        assert {'StopFailure', 'PostToolUse'} <= set(rewritten['hooks'])
        assert ('UserPromptSubmit' in rewritten['hooks']) == (slot == 'supervisor'), rewritten['hooks'].keys()
        assert rewritten['env'] == original['env'] and rewritten['env'].get('CLAUDE_CODE_STOP_HOOK_BLOCK_CAP') == '0'
        verbs = re.findall(r'fleet\.py (claude-[a-z-]+)', str(original['hooks']))
        assert set(verbs) == ({'claude-stop', 'claude-rewake', 'claude-stop-failure', 'claude-posttool'}
                              | ({'claude-prompt'} if slot == 'supervisor' else set())), verbs
        assert 'fleet.py' not in str(rewritten['hooks']), rewritten['hooks']
