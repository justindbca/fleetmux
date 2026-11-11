"""Exercise the real three backend send methods with an in-memory Claude editor.

No terminal, CLI, server, fleet or Redis is started. A successful write/key ACK does
not imply a successful edit: this fake deliberately retains drafts and appends pastes.
"""
import base64
import copy
from types import SimpleNamespace

import pytest

from fleetmux.backends import cmux, fleetmux, wezterm


class Editor:
    def __init__(self):
        self.text = 'earlier startup request\nlast line'
        self.submitted = []
        self.keys = []
        self.pastes = []
        self.clear_works = True
        self.corrupt_paste = False
        self.restore = False
        self.enter_ok = True
        self.busy = False
        self.readable = True
        self.width = 1024
        self.after_paste = lambda: None
        self.after_enter = lambda: None
        self.paste_ok = True

    def screen(self, *_args, **_kwargs):
        if not self.readable:
            return ''
        rule = '─' * self.width
        status = '✻ Working… (running Stop hooks… 1/2)' if self.busy else '⏺ READY'
        return (status + '\n' + rule + '\n❯ ' + self.text.replace('\n', '\n  ')
                + '\n' + rule + '\n  ? for shortcuts\n')

    def paste(self, text):
        self.pastes.append(text)
        self.text += text[:-1] if self.corrupt_paste else text
        self.after_paste()
        return self.paste_ok

    def key(self, key):
        self.keys.append(key)
        if key == 'C-u':
            self.text = self.text.rsplit('\n', 1)[0] if '\n' in self.text else ''
        elif key == 'C-c' and self.clear_works:
            self.text = ''
        elif key == 'Enter':
            if not self.enter_ok:
                return False
            self.submitted.append(self.text)
            if not self.restore:
                self.text = ''
            self.after_enter()
        return True


# §48 (user 2026-11-09:「任何场景都不用 claude 输入框打字」): the checked paste transaction these tests
# exercised is gone; every backend now refuses to type into a Claude pane (test_claude_pane_is_never_typed_into).
RETIRED = pytest.mark.skip(reason='docs/design.md §48: fleet never types into a Claude input box (2026-11-09)')


@pytest.fixture(params=['cmux', 'wezterm', 'fleetmux'])
def editor_backend(request, tmp_path, monkeypatch):
    editor = Editor()
    module, cls = {
        'cmux': (cmux, cmux.CmuxSessionManager),
        'wezterm': (wezterm, wezterm.WezTermSessionManager),
        'fleetmux': (fleetmux, fleetmux.FleetmuxSessionManager),
    }[request.param]
    mgr = cls('editor-test', state_dir=tmp_path)
    monkeypatch.setattr(module.time, 'sleep', lambda _seconds: None)
    monkeypatch.setattr(mgr, '_meta', lambda: {'surfaces': {'p': {'cli_type': 'claude'}}})
    monkeypatch.setattr(mgr, 'capture_output', editor.screen)
    if request.param == 'cmux':
        monkeypatch.setattr(mgr, '_where', lambda _pane: 'ws')
        buffers = {}

        def run(*args):
            ok = True
            if args[0] == 'set-buffer':
                buffers[args[2]] = args[3]
            elif args[0] == 'paste-buffer':
                ok = editor.paste(buffers[args[2]])
            elif args[0] == 'send-key':
                ok = editor.key(args[-1])
            return SimpleNamespace(returncode=0 if ok else 1, stdout='', stderr='')
        monkeypatch.setattr(module, '_run', run)
    elif request.param == 'wezterm':
        def send(_pane, text, no_paste):
            key = {'\r': 'Enter', '\x03': 'C-c', '\x15': 'C-u'}.get(text)
            return editor.key(key) if key else editor.paste(text)
        monkeypatch.setattr(mgr, '_send', send)
    else:
        monkeypatch.setattr(mgr, 'owns', lambda _pane: True)

        def call(command, **kw):
            if command == 'pane.write':
                ok = editor.paste(base64.b64decode(kw['data']).decode())
            elif command == 'pane.key':
                ok = editor.key(kw['key'])
            else:
                raise AssertionError(command)
            if not ok:
                raise fleetmux.FleetmuxError('fake ' + command + ' failure')
            return {}
        monkeypatch.setattr(mgr, '_call', call)
    return mgr, editor


def test_claude_pane_is_never_typed_into(editor_backend):
    """§48: every backend refuses a Claude pane: no paste, no key, no Enter, the reason in last_error."""
    mgr, editor = editor_backend
    for text, bracketed in (('a validated request', True), ('/clear', False)):
        assert mgr.send_prompt_to_idle('p', text, bracketed=bracketed) is False
        assert '§48' in mgr.last_error
    assert editor.pastes == [] and editor.keys == [] and editor.submitted == []
    assert editor.text == 'earlier startup request\nlast line'


@RETIRED
def test_multiline_residue_is_replaced_before_enter(editor_backend):
    mgr, editor = editor_backend
    # r6's false reassurance: the key transport ACKs, but an earlier line remains.
    assert mgr.send_key('p', 'C-u')
    assert editor.text == 'earlier startup request'
    intended = 'new validated request, only this text'
    assert mgr.send_prompt_to_idle('p', intended, bracketed=True)
    assert editor.submitted == [intended]
    assert editor.text == ''


@RETIRED
def test_successful_clear_key_ack_is_not_proof(editor_backend):
    mgr, editor = editor_backend
    editor.clear_works = False
    assert not mgr.send_prompt_to_idle('p', 'new request', bracketed=True)
    assert not editor.pastes and not editor.submitted
    assert 'held' in mgr.last_error.lower()


@RETIRED
def test_corrupt_paste_is_removed_without_enter(editor_backend):
    mgr, editor = editor_backend
    editor.text = ''
    editor.corrupt_paste = True
    assert not mgr.send_prompt_to_idle('p', 'new request', bracketed=True)
    assert not editor.submitted
    assert editor.text == ''
    assert 'held' in mgr.last_error.lower()


@RETIRED
@pytest.mark.parametrize('condition', ['busy', 'unreadable'])
def test_unverifiable_editor_receives_no_keys_or_text(editor_backend, condition):
    mgr, editor = editor_backend
    if condition == 'busy':
        editor.busy = True
    else:
        editor.readable = False
    assert not mgr.send_prompt_to_idle('p', 'new request', bracketed=True)
    assert not editor.keys and not editor.pastes


@RETIRED
def test_blocked_or_unsubmitted_prompt_does_not_leave_a_draft(editor_backend):
    mgr, editor = editor_backend
    editor.text = ''
    editor.restore = True
    assert not mgr.send_prompt_to_idle('p', 'new request', bracketed=True)
    assert editor.submitted == ['new request']  # never retry an uncertain Enter
    assert editor.text == ''
    assert 'held' in mgr.last_error.lower()


@RETIRED
def test_failed_enter_removes_the_unsubmitted_text(editor_backend):
    mgr, editor = editor_backend
    editor.text = ''
    editor.enter_ok = False
    assert not mgr.send_prompt_to_idle('p', 'new request', bracketed=True)
    assert not editor.submitted and editor.text == ''


@RETIRED
def test_raw_claude_slash_command_also_checks_editor(editor_backend):
    mgr, editor = editor_backend
    assert mgr.send_prompt_to_idle('p', '/clear', bracketed=False)
    assert editor.submitted == ['/clear']


@RETIRED
def test_studio_373_plus_458_regression(editor_backend):
    mgr, editor = editor_backend
    editor.text = 'old startup ' + 'x' * 361
    intended = 'new request ' + 'y' * 446
    assert len(editor.text) == 373 and len(intended) == 458
    assert mgr.send_prompt_to_idle('p', intended, bracketed=True)
    assert editor.submitted == [intended]


@pytest.mark.parametrize('text', ['two\twords', 'CR\rLF', '\x1b[201~evil', 'trailing ',
                                 'new\nline ', 'zero\u200bwidth', '\x00', ''])
@RETIRED
def test_nonverifiable_text_is_never_typed(editor_backend, text):
    mgr, editor = editor_backend
    assert not mgr.send_prompt_to_idle('p', text, bracketed=True)
    assert not editor.keys and not editor.pastes


@RETIRED
def test_multiline_payload_is_verified_after_clearing_residue(editor_backend):
    mgr, editor = editor_backend
    assert mgr.send_prompt_to_idle('p', 'first\nsecond', bracketed=True)
    assert editor.submitted == ['first\nsecond'] and editor.text == ''
    assert editor.keys == ['C-c', 'Enter']


@RETIRED
def test_entire_multiline_draft_is_cleared_with_one_key(editor_backend):
    mgr, editor = editor_backend
    editor.text = 'line one\nline two\nline three'
    assert mgr.send_prompt_to_idle('p', 'new request', bracketed=True)
    assert editor.submitted == ['new request'] and editor.text == ''
    assert editor.keys == ['C-c', 'Enter']


@RETIRED
def test_wrapping_is_verified_across_all_body_rows(editor_backend, monkeypatch):
    mgr, editor = editor_backend
    editor.text, editor.width = '', 30
    intended = 'word ' * 10 + 'end'
    screen = editor.screen
    monkeypatch.setattr(mgr, 'capture_output', lambda *a, **kw: screen().replace(
        intended, intended[:27] + '\n  ' + intended[27:]))
    assert mgr.send_prompt_to_idle('p', intended, bracketed=True)
    assert editor.submitted == [intended] and editor.keys == ['Enter']


@RETIRED
@pytest.mark.parametrize('replacement', ['[Pasted text #1 +3 lines] extra', 'truncated…', 'unwanted prefix request'])
def test_opaque_or_mismatched_paste_cleans_up_and_holds(editor_backend, replacement):
    mgr, editor = editor_backend
    editor.text = ''
    editor.after_paste = lambda: setattr(editor, 'text', replacement)
    assert not mgr.send_prompt_to_idle('p', 'request', bracketed=True)
    assert not editor.submitted and editor.text == ''


@RETIRED
def test_sole_paste_placeholder_is_verified_after_empty_check(editor_backend, monkeypatch):
    mgr, editor = editor_backend
    screen = editor.screen
    intended = 'first\nsecond'
    monkeypatch.setattr(mgr, 'capture_output', lambda *a, **kw: screen().replace(
        'first\n  second', '[Pasted text #1 +1 lines]'))
    assert mgr.send_prompt_to_idle('p', intended, bracketed=True)
    assert editor.submitted == [intended] and editor.text == ''
    assert editor.keys == ['C-c', 'Enter']


@RETIRED
def test_paste_that_starts_work_is_not_interrupted_or_submitted(editor_backend):
    mgr, editor = editor_backend
    editor.text = ''
    editor.after_paste = lambda: setattr(editor, 'busy', True)
    assert not mgr.send_prompt_to_idle('p', 'request', bracketed=True)
    assert not editor.keys
    assert 'cleanup unconfirmed' in mgr.last_error


@RETIRED
def test_concurrent_sender_cannot_append_between_verification_and_enter(editor_backend):
    mgr, editor = editor_backend
    editor.text = ''
    nested = []
    other_manager = copy.copy(mgr)
    editor.after_paste = lambda: nested.append(other_manager.send_prompt_to_idle('p', 'second', bracketed=True))
    assert mgr.send_prompt_to_idle('p', 'first', bracketed=True)
    assert nested == [False]
    assert editor.pastes == ['first'] and editor.submitted == ['first']


@RETIRED
def test_input_must_be_seen_twice_before_enter(editor_backend, monkeypatch):
    mgr, editor = editor_backend
    editor.text = ''
    real_screen = editor.screen
    count = 0

    def capture(*args, **kw):
        nonlocal count
        if editor.pastes:
            count += 1
            if count == 2:
                editor.text = 'late residue' + editor.text
        return real_screen(*args, **kw)
    monkeypatch.setattr(mgr, 'capture_output', capture)
    assert not mgr.send_prompt_to_idle('p', 'request', bracketed=True)
    assert not editor.submitted and editor.text == ''


def test_nonclaude_backend_semantics_stay_raw(editor_backend, monkeypatch):
    mgr, editor = editor_backend
    editor.text = 'shell prefix '
    monkeypatch.setattr(mgr, '_meta', lambda: {'surfaces': {'p': {'cli_type': 'pi'}}})
    assert mgr.send_prompt_to_idle('p', 'literal', bracketed=False)
    assert editor.submitted == ['shell prefix literal']
    assert editor.keys == ['Enter']


@RETIRED
def test_partial_write_with_error_is_cleaned_and_never_submitted(editor_backend):
    mgr, editor = editor_backend
    editor.text = ''
    editor.paste_ok = False
    assert not mgr.send_prompt_to_idle('p', 'request', bracketed=True)
    assert not editor.submitted and editor.text == ''
    assert 'paste failed' in mgr.last_error


@RETIRED
def test_empty_editor_after_enter_can_be_busy_without_interrupting_it(editor_backend, monkeypatch):
    mgr, editor = editor_backend
    editor.text = ''
    editor.after_enter = lambda: setattr(editor, 'busy', True)
    screen = editor.screen
    monkeypatch.setattr(mgr, 'capture_output', lambda *a, **kw: screen().replace(
        '? for shortcuts', 'esc to interrupt' if editor.busy else '? for shortcuts'))
    assert mgr.send_prompt_to_idle('p', 'request', bracketed=True)
    assert editor.submitted == ['request'] and editor.keys == ['Enter']


@pytest.mark.parametrize('change', [
    lambda s: s + '$ shell prompt\n',
    lambda s: s.replace('? for shortcuts', 'Press Ctrl-C again to exit'),
    lambda s: s.replace('? for shortcuts', 'Confirm this operation?'),
    lambda s: '\n'.join(s.splitlines()[2:]),  # missing upper frame
    lambda s: s.replace('❯ ', '> '),
])
def test_unknown_or_partial_screen_is_not_an_empty_editor(change):
    from fleetmux.claude_input import visible_editor
    editor = Editor()
    editor.text = ''
    assert visible_editor(change(editor.screen())) is None


def test_nbsp_ansi_and_session_title_are_chrome_not_prompt_text():
    from fleetmux.claude_input import visible_editor
    rule = '─' * 80
    screen = ('⏺ READY\n' + '─' * 50 + ' f123-worker-1 ' + '─' * 17 + '\n'
              '\x1b[32m❯\xa0exact input\x1b[0m\n' + rule + '\n  ? for shortcuts\n')
    view = visible_editor(screen)
    assert view.text == 'exact input' and view.exact and view.idle


@pytest.mark.parametrize('suffix', [' · ← 1 agent', ' · ← 2 agents', ' · ← for agents'])
def test_footer_agent_suffix_and_right_aligned_status(suffix):
    from fleetmux.claude_input import visible_editor
    editor = Editor()
    editor.text = ''
    screen = editor.screen().replace('? for shortcuts',
        '⏵⏵ bypass permissions on (shift+tab to cycle)' + suffix + '    ● high · /effort')
    view = visible_editor(screen)
    assert view is not None and view.idle and view.text == ''


def test_slash_menu_continuations_require_a_slash_draft():
    from fleetmux.claude_input import visible_editor
    editor = Editor()
    editor.text = '/clear'
    screen = editor.screen().replace('  ? for shortcuts',
        '  /clear    Clear conversation history\n    and free up context\n'
        '  /context    Visualize context\n    as a colored grid\n'
        '  /exit    Exit')
    view = visible_editor(screen)
    assert view is not None and view.idle and view.text == '/clear'
    assert visible_editor(screen.replace('❯ /clear', '❯ request')) is None
    assert visible_editor(screen + '$ shell prompt\n') is None
