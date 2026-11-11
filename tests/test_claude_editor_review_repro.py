"""Review repros (supervisor-editor-residue, d055d3c2..c1c22932): normal fleet traffic through the
checked Claude editor path. In-memory editor + fixture-owned private Redis only."""
import pytest

from fleetmux import fleet

from test_claude_editor_send import editor_backend  # noqa: F401
from test_claude_supervisor import service_fleet, no_trust, event  # noqa: F401
from test_message_bus_v2 import private_redis_v2  # noqa: F401

# §48 (user 2026-11-09:「任何场景都不用 claude 输入框打字」): these tests exercised a channel that typed into a
# Claude input box; it is refused at start now (tests/test_no_claude_typing.py covers the refusal).
RETIRED = pytest.mark.skip(reason='docs/design.md §48: fleet never types into a Claude input box (2026-11-09)')


def _legacy_typed_run(f):
    """Since the hook line, new v2 runs deliver to a Claude supervisor by Stop hook; the typed channel these tests
    check is the one old/resumed v2 runs (no launch marker) keep."""
    cfg = fleet.load(f.path)
    cfg.pop('claude_supervisor_hook_delivery', None)
    fleet.save(f.path, cfg)
    f.cfg.pop('claude_supervisor_hook_delivery', None)
    if getattr(f, 'ready_hook', None):
        f.ready_hook.kill()



@RETIRED
def test_v2_claude_supervisor_gets_the_routing_message(service_fleet, editor_backend):
    """P1-01: every daemon->Claude-supervisor delivery is multiline; it must be submitted, not held."""
    f = service_fleet
    _legacy_typed_run(f)
    mgr, editor = editor_backend
    f.mgr = mgr
    f.setf('supervisor', pane='p')
    editor.text = ''
    event(f, dict(kind='request', request_id='cmd-r1', spec={'id': 'r1', 'payload': {'message': 'hi'},
                                                             'reply': {'required': ['reply']}}))
    f.pump()
    assert f.agent('supervisor')['state'] != 'held' and not f.paused(), getattr(mgr, 'last_error', '')
    assert editor.submitted and 'Reply with the service decision JSON from AGENTS.md.' in editor.submitted[-1]


@pytest.mark.parametrize('text', [
    # v1 Fleet.send pointer for a Claude supervisor (fleet.py:5447-5454), real run path shape
    '[Request r-20261102-1] A new request needs routing. -- the full message is in '
    '/Users/j/stte-replay-tests/studio-wb-20261101/fleet-live/runs/fleet-20261101-120647-4f5a01f0/'
    'agents/supervisor/.inbox/msg-20261102-101500-a1b2c3.md . Read that whole file first and follow it.',
    # subfleet.py:862 role-change prompt typed into the adopted Claude pane (378 chars)
    'Your role changed. You are now supervisor of sub-fleet M0 (workers: worker-1, worker-2). The files '
    'AGENTS.md / CLAUDE.md in your current directory were replaced with the supervisor rules: read AGENTS.md '
    'now and follow it from now on; your earlier worker rules no longer apply. The daemon sends the task next. '
    'In this turn do no task work: your final reply must be exactly READY.',
    # runtime_v2 STARTUP_NUDGE (113 chars) at the r6 supervisor pane width
    'Your startup reply was not READY. Do no task work in this turn: reply exactly READY (one word, nothing else).',
])
@RETIRED
def test_single_line_fleet_prompt_wider_than_pane_is_submitted(editor_backend, text):
    """P1-02: real panes are 56-120 columns (r6 56, Studio 101, recordings 120)."""
    mgr, editor = editor_backend
    editor.text, editor.width = '', 101
    assert mgr.send_prompt_to_idle('p', text, bracketed=True), mgr.last_error
    assert editor.submitted == [text]


@RETIRED
def test_claude_2_1_280_idle_footer_is_recognized(editor_backend, monkeypatch):
    """P1-03: footer row copied from rust/fixtures/recordings/claude-1.rec (Claude Code 2.1.280)."""
    mgr, editor = editor_backend
    editor.text, editor.width = '', 120
    real = '  ⏵⏵ bypass permissions on (shift+tab to cycle)' + ' ' * 55 + '● high · /effort'
    screen = editor.screen
    monkeypatch.setattr(mgr, 'capture_output', lambda *a, **kw: screen().replace('  ? for shortcuts', real))
    assert mgr.send_prompt_to_idle('p', 'short request', bracketed=True), mgr.last_error
    assert editor.submitted == ['short request']


@RETIRED
def test_claude_slash_clear_with_command_menu_is_submitted(editor_backend, monkeypatch):
    """P1-04: typing a slash command opens the command menu under the box (claude-1.rec frames 129-142)."""
    mgr, editor = editor_backend
    editor.text, editor.width = '', 120
    menu = ('  /clear                        Clear conversation history and free up context\n'
            '  /context                      Visualize current context usage as a colored grid\n')
    screen = editor.screen
    monkeypatch.setattr(mgr, 'capture_output', lambda *a, **kw: screen().replace(
        '  ? for shortcuts\n', menu if editor.text.startswith('/') else '  ? for shortcuts\n'))
    assert mgr.send_prompt_to_idle('p', '/clear', bracketed=False), mgr.last_error
    assert editor.submitted == ['/clear']
