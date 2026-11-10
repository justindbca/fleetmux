"""Watchdog dialog recognition on real saved screens (docs/design.md §24, 2026-11-09 fix).

tests/fixtures/watchdog_screens/ holds every prompt screen the watchdog saved on this machine up to 2026-11-09
(`~/.cmux-fleet-*/runs/*/watchdog/*prompt*.txt`, copied verbatim except the user name in home paths -> USER), named <source>-<slot>-<g>-<ms>-<real|false>:
`real` = a dialog really was up, `false` = the marker was the agent's own text above the CLI's input box.
Plus `live-claude-2.1.316-trust-real` (captured 2026-11-09 in a private tmux) and two `synthetic-*` screens
built from real ones (see test_synthetic_* below).
"""
import time
from pathlib import Path

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_v2_codex_claude import dispatch_one, mixed  # noqa: F401
from test_watchdog import audits, keys, screens, wd  # noqa: F401
from cmux_fleet import fleet_config, watchdog

FIXTURES = Path(__file__).parent / 'fixtures' / 'watchdog_screens'
TABLE = watchdog.parse_table(fleet_config.block('watchdog_prompts'))

# file -> (worker CLI, expected match, expected dialog class); match is (whitelist name or None, marker or None)
CASES = {
    # Claude Code (2026-10-13 build): Claude's own "Dangerous rm" confirmation, the deletion case of §24.
    'claude-hook-worker-1-g0-1791900614604-real.txt': ('claude', (None, 'dangerous rm'), 'delete'),
    'claude-hook-worker-2-g0-1791902391402-real.txt': ('claude', (None, 'dangerous rm'), 'delete'),
    'claude-hook-worker-2-g1-1791914450382-real.txt': ('claude', (None, 'dangerous rm'), 'delete'),
    # Claude Code (2026-10-28): "LSP plugin recommendation" (rust-analyzer), a real non-delete dialog.
    **{name: ('claude', (None, '❯ 1. yes'), 'other') for name in (
        'fleetmux-e2e-iface-1-g0-1793152989369-real.txt', 'fleetmux-e2e-iface-1-g0-1793153000970-real.txt',
        'fleetmux-e2e-iface-1-g0-1793153012195-real.txt', 'fleetmux-layout-iface-1-g0-1793153006389-real.txt',
        'fleetmux-layout-iface-1-g0-1793153017567-real.txt', 'fleetmux-proto-1-g0-1793152997456-real.txt',
        'fleetmux-proto-1-g0-1793153008124-real.txt', 'fleetmux-proto-1-g0-1793153025698-real.txt',
        'fleetmux-term-iface-1-g0-1793152821324-real.txt', 'fleetmux-term-iface-1-g0-1793152832983-real.txt',
        'fleetmux-term-iface-1-g0-1793152844215-real.txt')},
    # Claude Code 2.1.316 trust dialog, captured live 2026-11-09 (whitelisted, answered Down + Enter).
    'live-claude-2.1.316-trust-real.txt': ('claude', ('trust_folder', None), 'other'),
    # 2026-11-09 04:10 (the incident): approve-1's final report names "dangerous rm" above Claude's input box.
    'channels-approve-1-g0-1794168610498-false.txt': ('claude', (None, None), None),
    # pi workers whose tool output shows another Claude's trust / try-it dialog (driven over ssh) ...
    'acceptance42-pi-build-1-g0-1793651338940-false.txt': ('pi', (None, None), None),   # matched trust_folder
    'acceptance42-pi-build-1-g0-1793651401796-false.txt': ('pi', (None, None), None),
    # ... or the worker writing test fixtures that quote "❯ 1. Yes, I trust this folder".
    'layout-finish-pi-build-1-g1-1793390884952-false.txt': ('pi', (None, None), None),
    'layout-finish-pi-build-1-g1-1793390902021-false.txt': ('pi', (None, None), None),
    'layout-finish-pi-build-1-g1-1793390912193-false.txt': ('pi', (None, None), None),
    'layout-finish-pi-impl-1-g13-1793477517335-false.txt': ('pi', (None, None), None),
    'layout-finish-pi-impl-1-g13-1793477522378-false.txt': ('pi', (None, None), None),
    # A pi extension confirm dialog, laid out as pi-coding-agent's extension-selector.js draws it (it replaces
    # the editor and sits between two borders). Synthesised from source: no live pi dialog was ever saved.
    'synthetic-pi-confirm-dialog-real.txt': ('pi', (None, 'dangerous rm'), 'delete'),
    # The incident's text above, and a real Dangerous rm dialog (claude-hook layout) where the input box was.
    'synthetic-claude-text-then-rm-dialog-real.txt': ('claude', (None, 'dangerous rm'), 'delete'),
}


INPUT_ROW = '\n❯\xa0\n'  # Claude's empty input row in the 2026-11-09 capture


def screen(name):
    return (FIXTURES / name).read_text(encoding='utf-8')


def name(match):
    entry, marker = match
    return (entry['name'] if entry else None, marker)


def test_every_fixture_is_classified():
    assert sorted(p.name for p in FIXTURES.iterdir()) == sorted(CASES)
    assert sum(n.endswith('-real.txt') for n in CASES) == 17 and sum(n.endswith('-false.txt') for n in CASES) == 8


@pytest.mark.parametrize('fixture', sorted(CASES))
def test_saved_screen_is_recognised_by_its_structure(fixture):
    cli, want, kind = CASES[fixture]
    got = watchdog.match_prompt(TABLE, screen(fixture), cli)
    assert name(got) == want
    if kind is not None:  # a dialog: the delete classification is unchanged (a delete stays the user's call)
        assert watchdog.dialog_class(TABLE, got[0], screen(fixture)) == kind


@pytest.mark.parametrize('fixture', sorted(n for n in CASES if not n.startswith('synthetic-')))
def test_without_a_known_cli_the_old_text_match_stands(fixture):
    # codex / opencode / dsh (no saved screens) and callers that pass no CLI keep the plain tail match.
    old = watchdog.match_prompt(TABLE, screen(fixture))
    assert old[0] is not None or old[1] is not None
    for cli in (None, 'codex', 'opencode', 'dsh'):
        assert watchdog.match_prompt(TABLE, screen(fixture), cli) == old


def test_text_typed_into_the_claude_input_box_is_not_a_dialog():
    # a nudge typed but not sent ("reply exactly READY") still shows Claude's own input box (Claude 2.1.316 draws
    # the prompt as "❯" + NO-BREAK SPACE)
    typed = screen('channels-approve-1-g0-1794168610498-false.txt').replace(INPUT_ROW, '\n❯\xa0reply exactly READY\n')
    assert '❯\xa0reply exactly READY' in typed
    assert watchdog.match_prompt(TABLE, typed, 'claude') == (None, None)


def test_claude_box_with_numbered_options_is_still_a_dialog():
    # anything dialog-like from the box down (numbered cursor, key hints) keeps the old match: never a miss
    fp = screen('channels-approve-1-g0-1794168610498-false.txt')
    for row in ('❯ 1. Yes', 'Esc to cancel · Tab to amend'):
        boxed = fp.replace(INPUT_ROW, '\n' + row + '\n')
        assert row in boxed and watchdog.match_prompt(TABLE, boxed, 'claude') == (None, 'dangerous rm')


def test_pi_dialog_without_its_key_hint_line_is_still_a_dialog():
    # only the "→" cursor is left of the pi dialog's markings: still a dialog
    dialog = screen('synthetic-pi-confirm-dialog-real.txt').replace(' ↑↓ navigate  enter select  esc cancel\n', '\n')
    assert '↑↓' not in dialog and watchdog.match_prompt(TABLE, dialog, 'pi') == (None, 'dangerous rm')


def test_worker_text_naming_dangerous_rm_does_not_pause_the_fleet(wd):
    """2026-11-09 04:10 regression: approve-1's report named "dangerous rm" above Claude's input box; the
    watchdog read it as an unknown dialog, marked prompt_wait and paused dispatch for about 20 minutes."""
    f = wd
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = screen('channels-approve-1-g0-1794168610498-false.txt')
    f.v2.watchdog.tick(time.time())
    assert f.agent('worker-1')['watch_state'] == 'working' and not f.agent('worker-1').get('prompt_wait')
    assert not f.paused() and keys(f, 'worker-1') == [] and screens(f) == []
    assert audits(f, 'watch.prompt_unknown') == [] and audits(f, 'execution.prompt_wait') == []


def test_a_real_rm_dialog_below_the_same_text_still_waits_for_the_user(wd):
    f = wd
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = screen('synthetic-claude-text-then-rm-dialog-real.txt')
    f.v2.watchdog.tick(time.time())
    assert f.agent('worker-1')['prompt_wait'] and f.paused() and keys(f, 'worker-1') == []
    unknown, = audits(f, 'watch.prompt_unknown')
    assert unknown['marker'] == 'dangerous rm' and unknown['dialog_class'] == 'delete'
