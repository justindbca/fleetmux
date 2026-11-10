"""Watchdog five states for transport v2 (docs/design.md §24).

Real private Redis + real hook entry points (the `mixed` Codex supervisor + Claude workers fleet);
fake cmux screens / ps. Time is passed explicitly to Watchdog.tick, never slept.
"""
import json
import os
from pathlib import Path
import time

import pytest
import yaml

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_v2_codex_claude import answer_turn, decision, dispatch_one, drive, mixed, submit, typed  # noqa: F401
import hook_sim
from cmux_fleet import runtime_v2, subfleet, watchdog
from cmux_fleet import fleet_config

DIALOG_RM = ('Bash command\n  rm -rf "$OUT/"*\n\nDangerous rm on possibly-empty variable\n'
             'Do you want to proceed?\n❯ 1. Yes\n  2. No, and tell Claude what to do differently (esc)')
DIALOG_UNKNOWN = ('Bash command\n  git push --force origin main\n\nDo you want to proceed?\n❯ 1. Yes\n  2. No')
DIALOG_RM_LITERAL = ('Bash command\n  rm -rf build/tmp\n\nDangerous rm on possibly-empty variable\n'
                     'Do you want to proceed?\n❯ 1. Yes\n  2. No')
TRUST = 'Do you trust the files in this folder?\n\n{}\n\n❯ 1. Yes, proceed\n  2. No, exit'
DIALOG_TRUST = TRUST.format('/tmp/run/workspace/worker-1')
# Claude Code 2.1.280 trust dialog, captured verbatim from a live PTY (120x40) on 2026-10-12. The path
# wraps at the dialog width; "No, exit" is preselected.
CLAUDE_TRUST_LIVE = "u\n────────────────────────────────────────────────────────────────────────────────\n Accessing workspace:\n\n /private/tmp/claude-501/-Users-USER--cmux-fleet-dev-runs-fleet-20261013-0252\n 33-a4957e88-workspace-worker-6/24b6f3ae-4cf5-4090-92f6-7f39a32c99d9/scratchpad\n /trust-probe\n\n Quick safety check: Is this a project you created or one you trust? (Like your\n own code, a well-known open source project, or work from your team). If not,\n take a moment to review what's in this folder first.\n\n Claude Code'll be able to read, edit, and execute files here.\n\n Security guide\n\n ❯ No, exit\n   Yes, I trust this folder\n\n Enter to confirm · Esc to cancel\n"
CLAUDE_TRUST_LIVE_CWD = ('/private/tmp/claude-501/-Users-USER--cmux-fleet-dev-runs-fleet-20261013-025233-a4957e88-'
                         'workspace-worker-6/24b6f3ae-4cf5-4090-92f6-7f39a32c99d9/scratchpad/trust-probe')


def claude_trust(path, width=79):
    """The live layout above for another directory, wrapped the same way."""
    rows = [' ' + path[i:i + width - 1] for i in range(0, len(path), width - 1)]
    head, tail = CLAUDE_TRUST_LIVE.split(' /private/tmp/claude-501/', 1)
    return head + '\n'.join(rows).lstrip('\n')[0:] + tail[tail.index('\n\n'):]


READ = 'Read({})\n\nRead outside the working directories?\n❯ 1. Yes\n  2. No'
WORKING = '⏺ Reading files…\n\n> '


@pytest.fixture
def wd(mixed):  # noqa: F811
    f = mixed
    f.cfg.update(stall_timeout=600, stall_grace=300, stall_escapes=3, watch_interval=0)
    f.mgr.screens, f.mgr.stats = {}, {}
    f.mgr.capture_output = lambda pane, lines=2000: f.mgr.screens.get(pane, WORKING)
    f.mgr.process_states = lambda pane: f.mgr.stats.get(pane, ['Ss', 'S+'])
    f.mgr.owns = lambda pane: pane in f.mgr.alive  # this fleet's cmux registry
    return f


def cwd(f, slot):
    from cmux_fleet.fleet import agent_cwd
    return agent_cwd(f.cfg, slot)


def audits(f, event=None):
    rows = [fields for _, fields in f.r.xrange(f.v2.bus.audit_key)]
    return [r for r in rows if event is None or r.get('event') == event]


def transitions(f, slot):
    return [(json.loads(r['detail'])['from'], json.loads(r['detail'])['to'])
            for r in audits(f, 'agent.state') if r.get('slot') == slot]


def keys(f, slot):
    pane = f.agent(slot)['pane']
    return [k for p, k in f.mgr.keys if p == pane]


def screens(f):
    folder = f.run_dir / 'watchdog'
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


# ------------------------------------------------------------------ pure parts
BASE = dict(active=True, in_flight=True, age=0, stall_timeout=600, prev='working', since=0, now=1000,
            grace=300, escapes=0, max_escapes=3, screen_same=False, proc_frozen=0)


@pytest.mark.parametrize('change,state', [
    (dict(active=False, in_flight=False, age=10 ** 6), 'idle'),           # E1: idle is never dead
    ({}, 'working'),
    (dict(age=599), 'working'),
    (dict(age=600), 'stalled'),
    (dict(stall_timeout=0, age=10 ** 6), 'working'),                      # stall ladder off
    (dict(prev='stalled', since=900, age=700), 'stalled'),
    (dict(prev='stalled', since=900, moved=True), 'working'),
    (dict(prev='stalled', since=700, age=900), 'frozen'),                 # grace over
    (dict(prev='stalled', since=900, escapes=3, screen_same=True), 'frozen'),
    (dict(prev='stalled', since=900, escapes=3, screen_same=False), 'stalled'),
    (dict(proc_frozen=1), 'working'),
    (dict(proc_frozen=2), 'frozen'),                                      # ps 'U' twice
    (dict(prompt={'name': 'x'}, prompt_hits=0), 'prompting'),
    (dict(prompt={'name': 'x'}, prompt_hits=3), 'stalled'),               # same dialog 3x
    (dict(unknown='do you want to proceed?'), 'prompting'),
    (dict(in_flight=False), 'working'),                                   # starting, READY deadline rules
    (dict(in_flight=False, prompt={'name': 'trust'}), 'prompting'),
])
def test_classify_five_states(change, state):
    assert watchdog.classify(**dict(BASE, **change)) == state
    assert state in watchdog.STATES


def test_default_prompt_table_matches_the_three_known_dialogs_only():
    table = watchdog.parse_table(fleet_config.block('watchdog_prompts'))
    assert [p['name'] for p in table['prompts']] == ['read_outside', 'trust_folder', 'trust_folder_legacy']
    # Deletes are the user's call (2026-10-14): a dangerous-rm dialog is never whitelisted, only an unknown marker.
    assert watchdog.match_prompt(table, DIALOG_RM) == (None, 'dangerous rm')
    assert watchdog.match_prompt(table, DIALOG_RM_LITERAL) == (None, 'dangerous rm')
    assert 'rm_in_cwd' not in watchdog.VERIFY
    assert watchdog.match_prompt(table, 'Read file\n\nRead outside the working directories?\n❯ 1. Yes')[0]['name'] == 'read_outside'
    assert watchdog.match_prompt(table, DIALOG_TRUST)[0]['name'] == 'trust_folder_legacy'
    assert watchdog.match_prompt(table, 'Trust this folder?\n› 1. Yes')[0]['name'] == 'trust_folder_legacy'
    live = watchdog.match_prompt(table, CLAUDE_TRUST_LIVE)[0]
    assert live['name'] == 'trust_folder' and live['keys'] == ['Down', 'Enter']   # Enter alone = "No, exit"
    # Cursor already on Yes (after Down): neither entry claims it, nothing is pressed again.
    moved = CLAUDE_TRUST_LIVE.replace(' ❯ No, exit\n   Yes, I trust', '   No, exit\n ❯ Yes, I trust')
    assert moved != CLAUDE_TRUST_LIVE and watchdog.match_prompt(table, moved) == (None, None)
    assert watchdog.match_prompt(table, DIALOG_UNKNOWN) == (None, 'do you want to proceed?')
    assert watchdog.match_prompt(table, WORKING) == (None, None)
    # Only the bottom of the screen counts: an answered dialog in scrollback is not a dialog.
    assert watchdog.match_prompt(table, DIALOG_RM + '\n' + '\n'.join(['output'] * 40)) == (None, None)


@pytest.mark.parametrize('bad,match', [
    ('prompts: [', 'not valid YAML'),
    ({'prompts': [{'name': 'x', 'match': ['a'], 'keys': ['rm -rf /']}]}, 'keys must be'),
    ({'prompts': [{'name': 'x', 'match': [], 'keys': ['Enter']}]}, 'match must not be empty'),
    ({'prompts': [{'name': 'x', 'match': ['a'], 'keys': ['Enter'], 'regex': '.*'}]}, 'exactly name'),
    ({'promts': []}, 'unknown prompt table keys'),
    ({'max_hits': 0}, 'max_hits'),
    ({'prompts': [{'name': 'x', 'match': ['a'], 'keys': ['Enter']}]}, 'verify must be'),   # text alone never authorises
    ({'prompts': [{'name': 'x', 'match': ['a'], 'keys': ['Enter'], 'verify': 'always'}]}, 'verify must be'),
])
def test_prompt_table_validation(bad, match):
    with pytest.raises(ValueError, match=match):
        watchdog.parse_table(bad)


def test_v2_config_validation_rejects_bad_watchdog_settings(tmp_path):
    cfg = dict(transport_version=2, supervisor_cli='codex', worker_cli='claude', mode='task', allow_development_v2=True)
    assert runtime_v2.validate_config(cfg) == 2
    with pytest.raises(ValueError, match='stall_grace'):
        runtime_v2.validate_config(dict(cfg, stall_grace=-1))
    bad = tmp_path / 'table.yaml'
    bad.write_text('prompts: [{name: x, match: [a], keys: [Delete], verify: own_cwd}]')
    with pytest.raises(ValueError, match='keys must be'):
        runtime_v2.validate_config(dict(cfg, prompt_table=str(bad)))


# ------------------------------------------------------------------ idle / working
def test_idle_slots_are_never_touched_however_long(wd):
    f = wd
    f.v2.watchdog.tick(time.time() + 10 ** 6)
    assert f.mgr.keys == [] and f.mgr.killed == [] and screens(f) == []
    assert all(f.agent(s)['state'] == 'idle' for s in f.names)


def test_busy_worker_with_fresh_transcript_is_working(wd):
    f = wd
    dispatch_one(f)
    f.v2.watchdog.tick(time.time())
    assert f.agent('worker-1')['watch_state'] == 'working'
    assert ('idle', 'working') in transitions(f, 'worker-1') and keys(f, 'worker-1') == []


# ------------------------------------------------------------------ prompting
def read_in_run(f, slot='worker-1'):
    """A whitelisted dialog whose scope is inside the run (worker-2's notes in the shared workspace)."""
    return READ.format(cwd(f, slot).parent / 'worker-2' / 'notes.md')


def test_whitelisted_dialog_is_answered_with_screen_saved(wd):
    f = wd
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = read_in_run(f)
    f.v2.watchdog.tick(time.time())
    assert keys(f, 'worker-1') == ['Enter']
    assert f.agent('worker-1')['watch_state'] == 'prompting' and f.agent('worker-1')['state'] == 'busy'
    saved, = screens(f)
    assert saved.startswith('worker-1-g0-') and saved.endswith('prompt-read_outside.txt')
    assert 'Read outside the working directories?' in (f.run_dir / 'watchdog' / saved).read_text()
    answered, = audits(f, 'watch.prompt_answered')
    assert answered['prompt'] == 'read_outside' and json.loads(answered['keys']) == ['Enter']
    assert answered['authorized'].endswith('/worker-2/notes.md')
    assert not f.paused() and f.v2.bus.slot_state('worker-1')['status'] == 'received'
    f.mgr.screens[f.agent('worker-1')['pane']] = WORKING  # the dialog went away: back to work
    f.v2.watchdog.tick(time.time())
    assert f.agent('worker-1')['watch_state'] == 'working'


def test_same_dialog_three_times_becomes_stalled(wd):
    f = wd
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = read_in_run(f)
    now = time.time()
    for i in range(3):
        f.v2.watchdog.tick(now + i)
    assert keys(f, 'worker-1') == ['Enter'] * 3
    f.v2.watchdog.tick(now + 3)
    assert f.agent('worker-1')['watch_state'] == 'stalled' and keys(f, 'worker-1')[-1] == 'Escape'


def test_unknown_dialog_waits_for_a_human_not_pressed(wd):
    # Plan F B-1: an unanswerable dialog no longer holds the slot (the result used to be thrown away after the
    # human answered); it is marked prompt_wait, dispatch pauses, the lease stays received.
    f = wd
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = DIALOG_UNKNOWN
    f.v2.watchdog.tick(time.time())
    assert keys(f, 'worker-1') == []
    waiting(f, 'worker-1')
    saved, = screens(f)
    assert saved.endswith('prompt-unknown.txt') and 'git push --force' in (f.run_dir / 'watchdog' / saved).read_text()
    assert audits(f, 'watch.prompt_unknown')[0]['marker'] == 'do you want to proceed?'


def test_trust_dialog_is_answered_while_starting(wd):
    f = wd
    f.setf('worker-2', state='starting')  # a new generation before READY: no delivery yet
    f.mgr.screens[f.agent('worker-2')['pane']] = TRUST.format(cwd(f, 'worker-2'))
    f.v2.watchdog.tick(time.time())
    assert keys(f, 'worker-2') == ['Enter'] and f.agent('worker-2')['watch_state'] == 'prompting'


def test_prompt_table_hot_reload_and_bad_edit_keeps_previous(wd, tmp_path):
    f = wd
    table = tmp_path / 'prompts.yaml'
    table.write_text('prompts: []\nunknown_markers: ["Do you want to proceed?"]\n')
    f.cfg['prompt_table'] = str(table)
    f.v2.watchdog = watchdog.Watchdog(f.v2)
    dispatch_one(f)
    pane = f.agent('worker-1')['pane']
    f.mgr.screens[pane] = 'Allow web fetch of ' + str(cwd(f, 'worker-1')) + '/page.html?\n❯ Yes'
    f.v2.watchdog.tick(time.time())
    assert keys(f, 'worker-1') == []                     # not in the table: nothing pressed
    table.write_text('prompts:\n  - {name: web_fetch, match: ["allow web fetch"], keys: [Enter], verify: paths_in_run}\n')
    f.v2.watchdog.tick(time.time())
    assert keys(f, 'worker-1') == ['Enter']               # edited table in force without restart
    assert json.loads(audits(f, 'watch.table_reloaded')[0]['prompts']) == ['web_fetch']
    table.write_text('prompts: [ {name: broken')
    f.v2.watchdog.tick(time.time())
    assert keys(f, 'worker-1') == ['Enter', 'Enter']      # previous table still in force
    assert audits(f, 'watch.table_rejected')


def test_capture_failures_raise_one_alarm_per_streak(wd):
    f = wd
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = ''
    now = time.time()
    for i in range(5):
        f.v2.watchdog.tick(now + i)
    alarm, = audits(f, 'watch.capture_fail')
    assert alarm['slot'] == 'worker-1' and alarm['level'] == 'error'
    assert f.agent('worker-1')['capture_fail'] == '5'


# ------------------------------------------------------------------ stalled / frozen
def test_stall_sends_escape_without_holding_and_activity_resumes(wd):
    f = wd
    dispatch_one(f)
    t = time.time() + 700
    f.v2.watchdog.tick(t)
    assert f.agent('worker-1')['watch_state'] == 'stalled' and keys(f, 'worker-1') == ['Escape']
    assert f.agent('worker-1')['state'] == 'busy' and not f.paused()  # no hold: E16 stall != dead
    f.v2.watchdog.tick(t + 10)
    assert keys(f, 'worker-1') == ['Escape']   # spaced: grace/escapes = 100 s
    transcript = f.agent('worker-1')['transcript']
    os.utime(transcript, (t + 20, t + 20))     # the agent wrote again
    f.v2.watchdog.tick(t + 30)
    assert f.agent('worker-1')['watch_state'] == 'working' and f.agent('worker-1')['stall_escapes'] == '0'
    assert transitions(f, 'worker-1')[-2:] == [('idle', 'stalled'), ('stalled', 'working')]


def frozen_checks(f, old_pane, msg_id):
    """A frozen official Claude worker resumes its own session in place and gets its work again (§43; the
    fresh-successor branch -- no transcript, resumed again, attempts used up -- is in test_frozen_resume.py)."""
    assert old_pane in f.mgr.killed
    assert f.agent('worker-1')['generation'] == '1' and f.agent('worker-1')['label'] == 'worker-1.1'
    assert f.agent('worker-1')['state'] == 'starting' and f.agent('worker-1')['pane'] != old_pane
    assert (f.agent('worker-1')['pane'], old_pane) in f.mgr.tabbed       # next generation in place
    assert f.agent('worker-1')['resume_generation'] == '1'               # claude --resume <its session>
    msg = f.v2.bus.message(msg_id)
    assert msg['status'] == 'committed' and msg['outcome'] == 'cancelled' and str(msg['stop_confirmed']) == '1'
    assert not f.v2.bus.slot_state('worker-1').get('current_delivery')
    assert not f.paused() and f.task()['status'] == 'running'
    assert f.v2.depth('supervisor') == 0 and f.v2.depth('worker-1') == 1   # no FROZEN report: the work goes again
    restarted, = audits(f, 'watch.frozen_restarted')
    assert restarted['prev'] == 'worker-1' and restarted['label'] == 'worker-1.1' and restarted['resumed']
    assert any(n.endswith('-frozen.txt') for n in screens(f))
    # The new generation starts normally.
    assert answer_turn(f, 'worker-1', 'READY', startup=True)['status'] == 'ready'
    assert f.agent('worker-1')['state'] == 'idle' and f.agent('worker-1')['runtime_ready'] == '1'


def test_stalled_past_grace_is_frozen_killed_and_restarted(wd):
    f = wd
    dispatch_one(f)
    old, msg_id = f.agent('worker-1')['pane'], f.v2.bus.slot_state('worker-1')['message_id']
    t = time.time() + 700
    f.v2.watchdog.tick(t)
    f.mgr.screens[old] = WORKING + ' changing'      # screen moves, transcript does not
    f.v2.watchdog.tick(t + 150)
    assert old not in f.mgr.killed
    f.v2.watchdog.tick(t + 301)
    assert ('stalled', 'frozen') in transitions(f, 'worker-1')
    frozen_checks(f, old, msg_id)


def test_escapes_that_change_nothing_mean_frozen_before_grace(wd):
    f = wd
    dispatch_one(f)
    old, msg_id = f.agent('worker-1')['pane'], f.v2.bus.slot_state('worker-1')['message_id']
    t = time.time() + 700
    for dt in (0, 100, 200):
        f.v2.watchdog.tick(t + dt)
    assert keys(f, 'worker-1') == ['Escape'] * 3 and old not in f.mgr.killed
    f.v2.watchdog.tick(t + 210)   # < grace (300), but three Escapes left the same screen
    assert 'unchanged' in json.loads(audits(f, 'agent.state')[-1]['detail'])['reason']
    frozen_checks(f, old, msg_id)


def test_uninterruptible_process_twice_is_frozen(wd):
    f = wd
    dispatch_one(f)
    old, msg_id = f.agent('worker-1')['pane'], f.v2.bus.slot_state('worker-1')['message_id']
    f.mgr.stats[old] = ['Ss', 'U+']   # E5: autofs read, keys do nothing
    f.v2.watchdog.tick(time.time())
    assert old not in f.mgr.killed
    f.v2.watchdog.tick(time.time() + 1)
    assert 'uninterruptible' in audits(f, 'watch.frozen_restarted')[0]['reason']
    frozen_checks(f, old, msg_id)


def test_frozen_while_paused_is_held_not_restarted(wd):
    f = wd
    dispatch_one(f)
    old = f.agent('worker-1')['pane']
    f.pause('market closed')
    f.mgr.stats[old] = ['U']
    f.v2.watchdog.tick(time.time()); f.v2.watchdog.tick(time.time() + 1)
    assert f.mgr.killed == [] and f.agent('worker-1')['state'] == 'held'
    assert f.agent('worker-1')['generation'] == '0'


def test_adopted_pane_is_never_killed(wd, monkeypatch):
    f = wd
    dispatch_one(f)
    old = f.agent('worker-1')['pane']
    monkeypatch.setattr(subfleet, 'adopted', lambda cfg, slot, generation: slot == 'worker-1')
    f.mgr.stats[old] = ['U']
    f.v2.watchdog.tick(time.time()); f.v2.watchdog.tick(time.time() + 1)
    assert f.mgr.killed == [] and f.agent('worker-1')['state'] == 'held'


def test_frozen_supervisor_gets_its_message_back_with_a_note(wd):
    f = wd
    from test_v2_codex_claude import submit
    submit(f)
    old = f.agent('supervisor')['pane']
    original = typed(f, 'supervisor')[0]
    f.mgr.stats[old] = ['U']
    f.v2.watchdog.tick(time.time()); f.v2.watchdog.tick(time.time() + 1)
    assert old in f.mgr.killed and f.agent('supervisor')['generation'] == '1'
    again = f.v2.pending_text('supervisor')[0]
    assert original.split('\n')[0] in again and 'froze while handling this message' in again


def test_held_and_dead_slots_are_left_alone(wd):
    f = wd
    dispatch_one(f)
    f.v2.transaction('t-hold', lambda: f.v2.hold('worker-1', 'operator inspection'))
    f.mgr.stats[f.agent('worker-1')['pane']] = ['U']
    f.v2.watchdog.tick(time.time() + 10 ** 5); f.v2.watchdog.tick(time.time() + 10 ** 5 + 1)
    assert f.mgr.killed == [] and keys(f, 'worker-1') == []


# ------------------------------------------------------------------ v2 hold exit
REPORT = 'Done:\n- x\nEvidence:\n- worker-1/x.md\nNot done: none\nBlocked: none'


def test_blocked_task_hold_is_released_when_the_turn_ends(wd):
    f = wd
    dispatch_one(f)
    msg_id = f.v2.bus.slot_state('worker-1')['message_id']
    f.v2.finish('blocked', 'supervisor gave up')   # in-flight worker is held as uncertain (E16 ①)
    assert f.agent('worker-1')['state'] == 'held' and f.v2.bus.slot_state('worker-1')['status'] == 'uncertain'
    assert answer_turn(f, 'worker-1', REPORT)['status'] == 'released'
    assert f.agent('worker-1')['state'] == 'idle'
    assert f.v2.bus.slot_state('worker-1')['status'] == 'idle' and f.v2.bus.slot_state('worker-1')['pel_count'] == 0
    msg = f.v2.bus.message(msg_id)
    assert msg['outcome'] == 'cancelled' and str(msg['stop_confirmed']) == '1'     # result NOT applied
    assert f.r.llen(f.v2.bus.pending_key('supervisor')) == 0               # no report forwarded
    released, = audits(f, 'hold.released')
    assert released['slot'] == 'worker-1' and released['message_id'] == msg_id
    # The same Stop fired twice: nothing is held any more.
    assert answer_turn(f, 'worker-1', REPORT, prompt=typed(f, 'worker-1')[-1])['status'] == 'ignored'


def stop_with(f, slot, rows, *, thread=None, text=REPORT, background=None):
    """The real Stop hook of `slot`'s current generation over a hand-written transcript."""
    gen = str(f.agent(slot)['generation'])
    thread = thread or f.agent(slot)['thread']
    transcript = f.test_claude / slot / (thread + '.jsonl')
    transcript.parent.mkdir(parents=True, exist_ok=True)
    rows = list(rows) + [{'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}}]
    transcript.write_text('\n'.join(json.dumps(x) for x in rows) + '\n')
    payload = {'session_id': thread, 'transcript_path': str(transcript), 'last_assistant_message': text}
    if background is not None:
        payload['background_tasks'] = background
    return drive(f, lambda: hook_sim.claude_stop(f.path, slot, gen, json.dumps(payload)))


def released_not_applied(f, slot, msg_id):
    assert f.agent(slot)['state'] == 'idle' and f.v2.bus.slot_state(slot)['status'] == 'idle'
    msg = f.v2.bus.message(msg_id)
    assert msg['outcome'] == 'cancelled' and str(msg['stop_confirmed']) == '1'     # result NOT applied
    assert f.r.llen(f.v2.bus.pending_key('supervisor')) == 0                      # no report forwarded


def test_hold_exit_is_any_turn_end_of_the_held_session_and_generation(wd):
    # Plan E-1 (2026-10-14): the held delivery's own turn need not be the one that ends; Claude runs one turn
    # at a time, so any Stop of the same session and generation proves the held execution stopped.
    f = wd
    dispatch_one(f)
    msg_id = f.v2.bus.slot_state('worker-1')['message_id']
    f.v2.finish('blocked', 'supervisor gave up')
    assert answer_turn(f, 'worker-1', REPORT, generation=4)['status'] == 'stale'
    other = stop_with(f, 'worker-1', [hook_sim.user_row('just chatting', False)], thread='another-session')
    assert other['status'] == 'ignored' and 'another generation/session' in other['reason']
    assert f.agent('worker-1')['state'] == 'held' and f.v2.bus.slot_state('worker-1')['status'] == 'uncertain'
    assert answer_turn(f, 'worker-1', REPORT, prompt='just chatting')['status'] == 'released'
    released_not_applied(f, 'worker-1', msg_id)


def test_hold_exit_survives_later_notifications_and_typed_lines(wd):
    # 15bdd30e worker-2: background-task notifications and operator lines pushed the delivery out of the
    # last prompts; every later Stop was ignored and the slot stayed held for 2 h 27 min.
    f = wd
    dispatch_one(f)
    msg_id = f.v2.bus.slot_state('worker-1')['message_id']
    f.v2.finish('blocked', 'supervisor gave up')
    note = {'type': 'user', 'origin': {'kind': 'task-notification'},
            'message': {'role': 'user', 'content': '<task-notification> bbx5c6eem done</task-notification>'}}
    rows = [hook_sim.user_row(typed(f, 'worker-1')[-1], True), note, note, note,
            hook_sim.user_row('operator: status?', False), hook_sim.user_row('operator: go on', False),
            hook_sim.user_row('operator: and?', False)]
    assert stop_with(f, 'worker-1', rows)['status'] == 'released'
    released_not_applied(f, 'worker-1', msg_id)


def test_hold_exit_also_counts_the_hook_keepalive_turn(wd):
    from cmux_fleet import claude_hook, fleet
    f = wd
    dispatch_one(f)
    msg_id = f.v2.bus.slot_state('worker-1')['message_id']
    f.v2.finish('blocked', 'supervisor gave up')
    k = fleet.hook_keys(f.cfg, f.r, 'worker-1', 0)
    f.r.set(k['bind'], json.dumps({'kind': 'keepalive', 'generation': 0, 'session_id': f.agent('worker-1')['thread']}))
    assert stop_with(f, 'worker-1', [hook_sim.user_row(claude_hook.KEEPALIVE, True)], text='IDLE')['status'] == 'released'
    released_not_applied(f, 'worker-1', msg_id)
    assert f.r.get(k['bind']) is None or json.loads(f.r.get(k['bind'])).get('kind') != 'keepalive'


def test_stop_deferred_by_background_tasks_is_audited_and_releases_nothing(wd):
    f = wd
    dispatch_one(f)
    f.v2.finish('blocked', 'supervisor gave up')
    assert stop_with(f, 'worker-1', [hook_sim.user_row('x', False)], background=[{'id': 'b1'}, {'id': 'b2'}]) is False
    deferred, = audits(f, 'turn.deferred')
    assert deferred['slot'] == 'worker-1' and deferred['tasks'] == '2' and deferred['generation'] == '0'
    assert [a for a in audits(f) if a.get('slot') == 'worker-1' and a['event'] in ('turn_ended', 'settled', 'hold.released')] == []
    assert f.agent('worker-1')['state'] == 'held'


def test_hook_mailbox_delivery_left_at_hold_is_dropped_after_release_never_run(wd):
    # The hold came before the worker's hook took the delivery: after the release the hook re-validates the
    # entry against the (now idle) lease and drops it, so it never runs; the next delivery goes through.
    f = wd
    submit(f)
    answer_turn(f, 'supervisor', decision(['worker-1']))
    hook_sim._armed.clear()               # no hook waiting: the delivery stays in the mailbox
    f.pump()
    assert hook_sim.handed_to(f, 'worker-1') == []
    msg_id = f.v2.bus.slot_state('worker-1')['message_id']
    f.v2.finish('blocked', 'supervisor gave up')
    assert stop_with(f, 'worker-1', [hook_sim.user_row('operator: anything?', False)])['status'] == 'released'
    released_not_applied(f, 'worker-1', msg_id)
    dropped = [a for a in audits(f, 'hook.dropped') if a.get('message_id') == msg_id]
    assert dropped and hook_sim.handed_to(f, 'worker-1') == []   # dropped as stale, never handed to Claude
    assert len([a for a in audits(f, 'tui.hooked') if a.get('message_id') == msg_id]) == 1


def test_forged_release_event_is_rejected(wd):
    f = wd
    dispatch_one(f)  # delivered, not held
    env = {k: f.v2.bus.message(f.v2.bus.slot_state('worker-1')['message_id'])[k] for k in runtime_v2.IDENTITY}
    runtime_v2.post_event(f.v2.bus, dict(kind='turn_ended', agent='worker-1', generation=0, run_id=f.v2.bus.run_id,
                                         thread=f.agent('worker-1')['thread'], request_id='h-forged', delivery=env),
                          'h-forged')
    assert f.v2.step(10)
    assert audits(f, 'hold.release_rejected')[0]['error'] == 'slot is not held'
    assert f.v2.bus.slot_state('worker-1')['status'] == 'received' and f.agent('worker-1')['state'] == 'busy'


# ------------------------------------------------------------------ dialog text is not an authorisation
def waiting(f, slot, state='busy', scope=None):
    """Plan F B-1: the dialog waits for a human. Nothing pressed, slot state and lease untouched (no hold),
    the slot marked prompt_wait with its scope, dispatch paused, one execution.prompt_wait audit."""
    info = f.agent(slot)
    lease = f.v2.bus.slot_state(slot)
    assert keys(f, slot) == [] and info['state'] == state and f.paused()
    assert info['prompt_wait'] == (scope or lease['current_delivery']) and info['prompt_route'] == 'user'
    if state == 'busy':
        assert lease['status'] == 'received'
    assert len(audits(f, 'execution.prompt_wait')) == 1 and audits(f, 'execution.held') == []


def waits_unpressed(f, slot, prompt, reason, state='busy', scope=None):
    waiting(f, slot, state, scope)
    unverified, = audits(f, 'watch.prompt_unverified')
    assert unverified['prompt'] == prompt and reason in unverified['reason']
    assert any(n.endswith('prompt-' + prompt + '.txt') for n in screens(f))
    assert audits(f, 'watch.prompt_answered') == []


def waits_for_the_user(f, slot, marker):
    """An unknown dialog: screen saved, slot marked for the user (fleet paused), not a single key pressed."""
    waiting(f, slot)
    assert audits(f, 'watch.prompt_unknown')[0]['marker'] == marker
    assert any(n.endswith('prompt-unknown.txt') for n in screens(f))
    assert audits(f, 'watch.prompt_answered') == [] and audits(f, 'watch.prompt_unverified') == []


def test_dangerous_rm_on_a_variable_waits_for_a_human(wd):
    f = wd
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = DIALOG_RM   # rm -rf "$OUT/"*: OUT may be empty
    f.v2.watchdog.tick(time.time())
    waits_for_the_user(f, 'worker-1', 'dangerous rm')
    assert f.v2.bus.slot_state('worker-1')['status'] == 'received'
    f.v2.watchdog.tick(time.time() + 1)                         # waiting: the next tick presses nothing either
    assert keys(f, 'worker-1') == []


@pytest.mark.parametrize('target', ['build/tmp', '../worker-2/out', '/Users', '~/Documents'])
def test_dangerous_rm_waits_for_the_user_wherever_its_target_is(wd, target):
    # build/tmp (a literal target inside the slot directory) used to be answered; deletes are now always
    # confirmed by the user (2026-10-14).
    f = wd
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = DIALOG_RM_LITERAL.replace('build/tmp', target)
    f.v2.watchdog.tick(time.time())
    waits_for_the_user(f, 'worker-1', 'dangerous rm')
    assert f.v2.bus.slot_state('worker-1')['status'] == 'received'
    f.v2.watchdog.tick(time.time() + 1)                         # waiting: the next tick presses nothing either
    assert keys(f, 'worker-1') == []


def test_no_permission_hook_answers_delete_dialogs(tmp_path):
    """Nothing in the fleet auto-answers a delete confirmation: no PermissionRequest hook is installed."""
    from cmux_fleet import fleet
    for hook in (True, False):
        cfg = dict(run=str(tmp_path / ('run-' + str(hook))), workers=['worker-1'], supervisor_cli='claude',
                   worker_cli='claude', python='python3', claude_hook_delivery=hook)
        for slot in ('supervisor', 'worker-1'):
            fleet.agent_cwd(cfg, slot).mkdir(parents=True)
            argv = fleet.claude_argv(tmp_path / 'config.json', cfg, slot, 0, 'prompt')
            settings = json.loads(Path(argv[argv.index('--settings') + 1]).read_text())
            assert 'PermissionRequest' not in settings['hooks'] and 'PreToolUse' not in settings['hooks']


def test_read_outside_inside_the_run_workspace_is_answered(wd):
    f = wd
    dispatch_one(f)
    shared = cwd(f, 'worker-1').parent / 'worker-2' / 'notes.md'   # <run>/workspace is granted (--add-dir)
    f.mgr.screens[f.agent('worker-1')['pane']] = READ.format(shared)
    f.v2.watchdog.tick(time.time())
    assert keys(f, 'worker-1') == ['Enter'] and f.agent('worker-1')['state'] == 'busy'
    assert audits(f, 'watch.prompt_answered')[0]['prompt'] == 'read_outside'


@pytest.mark.parametrize('screen,reason', [
    (READ.format('/Users/someone/.ssh/id_ed25519'), 'outside the granted area'),
    (READ.format('/Users/user/projects/fleetmux/cmux_fleet/fleet.py'), 'outside the granted area'),
    ('Read outside the working directories?\n❯ 1. Yes', 'no path'),   # scope not on the screen
])
def test_read_outside_the_run_or_without_a_path_goes_to_the_supervisor(wd, screen, reason):
    # Plan F' 8.3: not a delete, so the supervisor rules on it (step 2 sent it to the user).
    f = wd
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = screen
    f.v2.watchdog.tick(time.time())
    escalated(f, 'worker-1', 'read_outside', ['Enter'])
    unverified, = audits(f, 'watch.prompt_unverified')
    assert reason in unverified['reason'] and unverified['dialog_class'] == 'other'


@pytest.mark.parametrize('screen,name,reason', [
    (DIALOG_TRUST, 'trust_folder_legacy', 'outside the granted area'),   # some other folder than the slot's own
    ('Trust this folder?\n› 1. Yes', 'trust_folder_legacy', 'no path'),
    (CLAUDE_TRUST_LIVE, 'trust_folder', 'outside the granted area'),     # the live capture names another directory
])
def test_trust_of_a_foreign_or_unnamed_folder_waits_for_a_human(wd, screen, name, reason):
    f = wd
    f.setf('worker-2', state='starting')
    f.mgr.screens[f.agent('worker-2')['pane']] = screen
    f.v2.watchdog.tick(time.time())
    waits_unpressed(f, 'worker-2', name, reason, state='starting', scope='startup-g0')
    assert f.v2.bus.slot_state('worker-2')['status'] == 'idle'   # no lease touched before READY


def test_dialog_on_a_pane_outside_this_fleet_registry_is_not_answered(wd):
    f = wd
    dispatch_one(f)
    pane = f.agent('worker-1')['pane']
    f.mgr.screens[pane] = read_in_run(f)
    f.mgr.owns = lambda p: False
    f.v2.watchdog.tick(time.time())
    waits_unpressed(f, 'worker-1', 'read_outside', 'registry')


# ------------------------------------------------------------------ plan F: a dialog waits for a human, not a hold
def mark_rm(f, slot='worker-1'):
    dispatch_one(f)
    f.mgr.screens[f.agent(slot)['pane']] = DIALOG_RM
    f.v2.watchdog.tick(time.time())
    waiting(f, slot)
    return f.agent(slot)['prompt_wait']


def move_transcript(f, slot):
    path = Path(f.agent(slot)['transcript'])
    later = float(f.agent(slot)['prompt_wait_at']) + 5
    os.utime(path, (later, later))


def test_prompt_wait_presses_nothing_while_the_dialog_stays(wd):
    f = wd
    mark_rm(f)
    now = time.time()
    for i in range(30):                                    # well past stall_timeout (600 s) and stall_grace
        f.v2.watchdog.tick(now + 60 * i)
    assert keys(f, 'worker-1') == [] and audits(f, 'watch.escape') == []
    assert ('prompting', 'stalled') not in transitions(f, 'worker-1') and f.mgr.killed == []
    assert len(audits(f, 'execution.prompt_wait')) == 1
    f.mgr.screens[f.agent('worker-1')['pane']] = WORKING   # the dialog scrolled out, nobody answered yet
    f.v2.watchdog.tick(now + 3600)
    assert keys(f, 'worker-1') == [] and f.agent('worker-1')['prompt_wait']


def test_prompt_wait_clears_when_the_transcript_moves(wd):
    f = wd
    mark_rm(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = WORKING
    move_transcript(f, 'worker-1')                          # the human answered, the worker went on
    f.v2.watchdog.tick(time.time() + 10)
    assert f.agent('worker-1')['prompt_wait'] == '' and f.agent('worker-1')['watch_state'] == 'working'
    assert audits(f, 'watch.prompt_cleared')[-1]['reason'] == 'activity'
    f.mgr.screens[f.agent('worker-1')['pane']] = DIALOG_RM  # the model opens another dialog: wait again
    f.v2.watchdog.tick(time.time() + 20)
    assert len(audits(f, 'execution.prompt_wait')) == 2 and keys(f, 'worker-1') == []


def test_answered_dialog_turn_settles_normally_and_the_report_reaches_the_supervisor(wd):
    f = wd
    mark_rm(f)
    msg_id = f.v2.bus.slot_state('worker-1')['message_id']
    f.mgr.screens[f.agent('worker-1')['pane']] = WORKING
    move_transcript(f, 'worker-1')
    f.v2.watchdog.tick(time.time() + 10)
    assert answer_turn(f, 'worker-1', REPORT)['status'] == 'committed'
    msg = f.v2.bus.message(msg_id)
    assert msg['status'] == 'committed' and msg['outcome'] == 'completed'
    assert audits(f, 'hold.released') == [] and audits(f, 'execution.held') == []
    assert any(t.startswith('[worker-1 report]') for t in f.v2.pending_text('supervisor'))
    f.resume(); f.pump()
    assert typed(f, 'supervisor')[-1].startswith('[worker-1 report]')


def test_answered_dialog_turn_settles_even_before_the_watchdog_sees_activity(wd):
    f = wd
    mark_rm(f)
    assert answer_turn(f, 'worker-1', REPORT)['status'] == 'committed'   # B-4: the lease stayed received
    f.v2.watchdog.tick(time.time() + 10)
    assert f.agent('worker-1')['prompt_wait'] == '' and audits(f, 'watch.prompt_cleared')[-1]['reason'] == 'inactive'


def test_resume_without_answering_keeps_the_slot_waiting(wd):
    f = wd
    mark_rm(f)
    f.resume()
    f.v2.watchdog.tick(time.time() + 10)
    assert keys(f, 'worker-1') == [] and f.agent('worker-1')['prompt_wait'] and not f.paused()
    assert len(audits(f, 'execution.prompt_wait')) == 1


def test_prompt_wait_is_dropped_when_the_delivery_is_gone(wd):
    f = wd
    mark_rm(f)
    f.v2.finish('blocked', 'supervisor gave up')            # the delivery is held now: hands off
    f.v2.watchdog.tick(time.time() + 10)
    assert f.agent('worker-1')['state'] == 'held' and f.agent('worker-1')['prompt_wait'] == ''
    assert audits(f, 'watch.prompt_cleared')[-1]['reason'] == 'hands_off' and keys(f, 'worker-1') == []


def test_prompt_wait_of_another_delivery_is_cleared_and_checked_afresh(wd):
    f = wd
    mark_rm(f)
    f.setf('worker-1', prompt_wait='d-some-older-delivery')
    f.mgr.screens[f.agent('worker-1')['pane']] = WORKING
    f.v2.watchdog.tick(time.time() + 10)
    assert f.agent('worker-1')['prompt_wait'] == '' and audits(f, 'watch.prompt_cleared')[-1]['reason'] == 'scope_changed'


def test_starting_slot_trust_dialog_waits_for_a_human_and_ready_registers(wd):
    f = wd
    f.setf('worker-2', state='starting', startup_deadline=time.time() + 180)
    f.mgr.screens[f.agent('worker-2')['pane']] = DIALOG_TRUST       # a folder that is not the slot's own
    f.v2.watchdog.tick(time.time())
    waiting(f, 'worker-2', state='starting', scope='startup-g0')
    f.setf('worker-2', startup_deadline=time.time() - 1)           # past profile_ready_timeout
    f.v2.tick()
    assert f.agent('worker-2')['state'] == 'starting' and f.task().get('status') != 'blocked'
    assert keys(f, 'worker-2') == []
    f.mgr.screens[f.agent('worker-2')['pane']] = WORKING
    assert answer_turn(f, 'worker-2', 'READY', startup=True)['status'] == 'ready'
    assert f.agent('worker-2')['state'] == 'idle' and f.agent('worker-2')['runtime_ready'] == '1'
    f.v2.watchdog.tick(time.time() + 10)
    assert f.agent('worker-2')['prompt_wait'] == ''


def test_unmarked_starting_slot_past_its_deadline_is_still_dead(wd):
    f = wd
    submit(f)
    f.setf('worker-2', state='starting', startup_deadline=time.time() - 1)
    f.v2.tick()
    assert f.agent('worker-2')['state'] == 'dead' and f.task()['status'] == 'blocked'


# ------------------------------------------------------------------ plan F' 8.3: non-delete dialog -> supervisor
READ_FAR = READ.format('/Users/someone/notes/today.md')
UNKNOWN_EDIT = 'Do you want to make this edit to report.md?\n❯ 1. Yes\n  2. No'
DIALOG_KEY = 'dialog:worker-1:g0'


def escalations(f):
    return json.loads(f.r.get('fx:escalations') or '{}')


def escalated(f, slot, prompt, keys_approve, scope=None, state='busy'):
    """Handed to the supervisor: no key, no pause, slot / lease untouched, one escalation and one [dialog] message."""
    info = f.agent(slot)
    assert keys(f, slot) == [] and not f.paused() and info['state'] == state
    assert info['prompt_wait'] == (scope or f.v2.bus.slot_state(slot)['current_delivery'])
    assert info['prompt_route'] == 'supervisor'
    if state == 'busy':
        assert f.v2.bus.slot_state(slot)['status'] == 'received'
    rec = escalations(f)['dialog:' + slot + ':g' + info['generation']]
    assert rec['kind'] == 'dialog' and rec['prompt'] == prompt and rec['keys_approve'] == keys_approve
    assert rec['status'] == 'escalated' and len(rec['digest']) == 16 and rec['keys_reject'] == ['Escape']
    text = f.v2.bus.message(rec['message_id'])['body']['text']
    assert text.startswith('[dialog] ' + f.label(slot)) and '"task": "dialog:' + slot in text
    assert audits(f, 'watch.prompt_escalated') and audits(f, 'execution.held') == []
    assert audits(f, 'execution.prompt_wait') == []
    return rec


def rule(f, decision, key=DIALOG_KEY, status='RUNNING', extra=None):
    """The supervisor receives the queued [dialog] message and answers it."""
    f.pump()
    body = dict({'status': status, 'completion': 10, 'reason': 'ruling', 'dispatch': [],
                 'escalations': [{'task': key, 'decision': decision}] if decision else []}, **(extra or {}))
    return answer_turn(f, 'supervisor', json.dumps(body))


def test_non_delete_dialog_waits_for_the_supervisor_and_presses_nothing(wd):
    f = wd
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = READ_FAR
    f.v2.watchdog.tick(time.time())
    rec = escalated(f, 'worker-1', 'read_outside', ['Enter'])
    assert 'Read outside the working directories?' in f.v2.bus.message(rec['message_id'])['body']['text']
    now = time.time()
    for i in range(30):                                     # past stall_timeout: still nothing pressed
        f.v2.watchdog.tick(now + 60 * i)
    assert keys(f, 'worker-1') == [] and audits(f, 'watch.escape') == []


@pytest.mark.parametrize('decision,pressed', [('approve', ['Enter']), ('reject', ['Escape'])])
def test_supervisor_ruling_is_pressed_after_a_fresh_check_and_the_turn_settles(wd, decision, pressed):
    f = wd
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = READ_FAR
    f.v2.watchdog.tick(time.time())
    assert rule(f, decision)['status'] == 'committed'
    assert escalations(f)[DIALOG_KEY]['status'] == 'answered' and keys(f, 'worker-1') == []   # no key in the transaction
    f.v2.watchdog.tick(time.time() + 1)
    assert keys(f, 'worker-1') == pressed
    answered, = audits(f, 'watch.prompt_answered')
    assert answered['authorized'] == 'supervisor:' + decision and answered['task'] == DIALOG_KEY
    assert DIALOG_KEY not in escalations(f) and f.agent('worker-1')['prompt_wait'] == ''
    assert f.agent('worker-1')['watch_state'] == 'working'
    f.mgr.screens[f.agent('worker-1')['pane']] = WORKING   # the dialog closed on the key
    f.v2.watchdog.tick(time.time() + 2)
    assert keys(f, 'worker-1') == pressed                  # pressed once, never again
    assert answer_turn(f, 'worker-1', REPORT)['status'] == 'committed'
    f.pump()
    assert typed(f, 'supervisor')[-1].startswith('[worker-1 report]')


def test_dialog_gone_or_changed_before_the_press_is_not_pressed(wd):
    f = wd
    dispatch_one(f)
    pane = f.agent('worker-1')['pane']
    f.mgr.screens[pane] = READ_FAR
    f.v2.watchdog.tick(time.time())
    rule(f, 'approve')
    f.mgr.screens[pane] = WORKING                           # answered in the pane meanwhile / the agent moved on
    f.v2.watchdog.tick(time.time() + 1)
    assert keys(f, 'worker-1') == [] and DIALOG_KEY not in escalations(f)
    assert audits(f, 'dialog.stale')[-1]['reason'] == 'dialog_gone' and f.agent('worker-1')['prompt_wait'] == ''


def test_another_dialog_before_the_press_is_routed_afresh(wd):
    f = wd
    dispatch_one(f)
    pane = f.agent('worker-1')['pane']
    f.mgr.screens[pane] = READ_FAR
    f.v2.watchdog.tick(time.time())
    old = escalations(f)[DIALOG_KEY]['message_id']
    rule(f, 'approve')
    f.mgr.screens[pane] = READ.format('/Users/someone/other/file.md')   # another read: new digest
    f.v2.watchdog.tick(time.time() + 1)
    assert keys(f, 'worker-1') == [] and audits(f, 'dialog.stale')[-1]['reason'] == 'dialog_changed'
    rec = escalations(f)[DIALOG_KEY]
    assert rec['status'] == 'escalated' and rec['message_id'] != old   # a new question for the supervisor


def test_identity_change_before_the_press_is_not_pressed(wd):
    f = wd
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = READ_FAR
    f.v2.watchdog.tick(time.time())
    rule(f, 'approve')
    f.setf('worker-1', thread='another-session')
    f.v2.watchdog.tick(time.time() + 1)
    assert keys(f, 'worker-1') == [] and audits(f, 'dialog.stale')[-1]['reason'] == 'identity'
    assert DIALOG_KEY not in escalations(f)


def test_a_delete_appearing_before_the_press_goes_to_the_user(wd):
    f = wd
    dispatch_one(f)
    pane = f.agent('worker-1')['pane']
    f.mgr.screens[pane] = READ_FAR
    f.v2.watchdog.tick(time.time())
    rule(f, 'approve')
    f.mgr.screens[pane] = 'Bash command\n  rm -rf notes\n' + READ_FAR    # the tail now shows a delete
    f.v2.watchdog.tick(time.time() + 1)
    f.v2.watchdog.tick(time.time() + 2)
    assert keys(f, 'worker-1') == [] and f.paused() and f.agent('worker-1')['prompt_route'] == 'user'
    assert json.loads(audits(f, 'execution.prompt_wait')[-1]['detail'])['dialog_class'] == 'delete'


def test_reclassified_delete_on_the_same_screen_goes_to_the_user(wd, tmp_path):
    f = wd
    table = tmp_path / 'prompts.yaml'
    table.write_text(yaml.safe_dump(fleet_config.block('watchdog_prompts')))
    f.cfg['prompt_table'] = str(table)
    f.v2.watchdog = watchdog.Watchdog(f.v2)
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = READ_FAR
    f.v2.watchdog.tick(time.time())
    rule(f, 'approve')
    table.write_text(yaml.safe_dump(fleet_config.block('watchdog_prompts')).replace('delete_markers: []', 'delete_markers: ["today"]'))
    f.v2.watchdog.tick(time.time() + 1)
    assert keys(f, 'worker-1') == [] and f.paused() and audits(f, 'dialog.reclassified_delete')
    assert f.agent('worker-1')['prompt_route'] == 'user' and DIALOG_KEY not in escalations(f)


def test_unknown_dialog_without_a_visible_yes_cursor_has_no_key_and_goes_to_the_user(wd):
    f = wd
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = 'Do you want to make this edit to report.md?\n  1. Yes\n❯ 2. No'
    f.v2.watchdog.tick(time.time())
    escalated(f, 'worker-1', 'unknown:do you want to make this edit', None)
    rule(f, 'approve')
    f.v2.watchdog.tick(time.time() + 1)
    assert keys(f, 'worker-1') == [] and f.paused() and audits(f, 'dialog.to_human')[-1]['reason'] == 'no_safe_key'
    assert any('no safe key' in t for t in f.v2.pending_text('supervisor'))


def test_unknown_dialog_with_the_cursor_on_yes_is_pressed_enter_on_approve(wd):
    f = wd
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = UNKNOWN_EDIT
    f.v2.watchdog.tick(time.time())
    escalated(f, 'worker-1', 'unknown:do you want to make this edit', ['Enter'])
    rule(f, 'approve')
    f.v2.watchdog.tick(time.time() + 1)
    assert keys(f, 'worker-1') == ['Enter']


def test_supervisor_timeout_counts_from_delivery_and_hands_the_dialog_to_the_user(wd):
    f = wd
    f.cfg['stall_timeout'] = 0   # the hours skipped below would otherwise stall / rotate the supervisor itself
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = READ_FAR
    now = time.time()
    f.v2.watchdog.tick(now)
    f.v2.watchdog.tick(now + 3600)                         # still queued for the supervisor: no clock yet
    assert not f.paused() and escalations(f)[DIALOG_KEY]['delivered_at'] == 0
    f.pump()                                                # the supervisor receives the [dialog] message
    f.v2.watchdog.tick(now + 3601)
    assert escalations(f)[DIALOG_KEY]['delivered_at'] == now + 3601
    f.v2.watchdog.tick(now + 3601 + 599)
    assert not f.paused()
    f.v2.watchdog.tick(now + 3601 + 600)
    assert f.paused() and keys(f, 'worker-1') == [] and f.agent('worker-1')['prompt_route'] == 'user'
    assert audits(f, 'dialog.to_human')[-1]['reason'] == 'supervisor_timeout' and DIALOG_KEY not in escalations(f)


def test_running_reply_that_ignores_the_dialog_is_sent_back_once_without_a_strike(wd):
    f = wd
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = READ_FAR
    f.v2.watchdog.tick(time.time())
    rule(f, None)                                           # RUNNING, nothing answered
    assert f.task()['bad_replies'] == '0' and escalations(f)[DIALOG_KEY]['nudged'] == 1
    f.pump()
    assert 'answer ' + DIALOG_KEY + ' first' in typed(f, 'supervisor')[-1]
    answer_turn(f, 'supervisor', json.dumps({'status': 'RUNNING', 'completion': 10, 'reason': 'x', 'dispatch': []}))
    assert f.paused() and f.agent('worker-1')['prompt_route'] == 'user' and DIALOG_KEY not in escalations(f)
    assert audits(f, 'dialog.to_human')[-1]['reason'] == 'supervisor_unanswered' and keys(f, 'worker-1') == []


def test_supervisor_own_dialog_goes_to_the_user(wd):
    f = wd
    dispatch_one(f)
    f.v2.queue('supervisor', 'something to think about')
    f.pump()
    f.mgr.screens[f.agent('supervisor')['pane']] = READ_FAR
    f.v2.watchdog.tick(time.time())
    assert f.paused() and f.agent('supervisor')['prompt_route'] == 'user' and escalations(f) == {}


def test_worker_dialog_goes_to_the_user_while_the_supervisor_is_held(wd):
    f = wd
    dispatch_one(f)
    f.setf('supervisor', state='held')
    f.mgr.screens[f.agent('worker-1')['pane']] = READ_FAR
    f.v2.watchdog.tick(time.time())
    assert f.paused() and f.agent('worker-1')['prompt_route'] == 'user' and DIALOG_KEY not in escalations(f)
    assert audits(f, 'dialog.to_human')[-1]['reason'] == 'supervisor_unavailable'


def test_dialog_record_cleared_by_a_new_task_falls_back_to_the_user(wd):
    f = wd
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = READ_FAR
    f.v2.watchdog.tick(time.time())
    f.r.delete('fx:escalations')
    f.v2.watchdog.tick(time.time() + 1)
    assert f.paused() and f.agent('worker-1')['prompt_route'] == 'user' and keys(f, 'worker-1') == []


def test_starting_slot_foreign_trust_dialog_is_ruled_by_the_supervisor_then_ready(wd):
    f = wd
    submit(f)
    f.setf('worker-2', state='starting', startup_deadline=time.time() + 180)
    f.mgr.screens[f.agent('worker-2')['pane']] = claude_trust('/Users/someone/elsewhere')
    f.v2.watchdog.tick(time.time())
    escalated(f, 'worker-2', 'trust_folder', ['Down', 'Enter'], scope='startup-g0', state='starting')
    f.setf('worker-2', startup_deadline=time.time() - 1)
    f.v2.tick()
    assert f.agent('worker-2')['state'] == 'starting'                  # waiting is not a dead start
    rule(f, 'approve', key='dialog:worker-2:g0')
    f.v2.watchdog.tick(time.time() + 1)
    assert keys(f, 'worker-2') == ['Down', 'Enter']
    f.mgr.screens[f.agent('worker-2')['pane']] = WORKING
    assert answer_turn(f, 'worker-2', 'READY', startup=True)['status'] == 'ready'
    assert f.agent('worker-2')['state'] == 'idle'


def test_stale_answer_to_a_gone_dialog_is_not_a_bad_reply(wd):
    f = wd
    dispatch_one(f)
    f.v2.queue('supervisor', '[worker-2 note] something to answer')
    assert rule(f, 'approve', key='dialog:worker-2:g0')['status'] == 'committed'
    assert f.task()['bad_replies'] == '0'


def test_dialog_answer_must_be_approve_or_reject(wd):
    f = wd
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = READ_FAR
    f.v2.watchdog.tick(time.time())
    rule(f, 'revise')
    assert f.task()['bad_replies'] == '1' and escalations(f)[DIALOG_KEY]['status'] == 'escalated'


def test_dialog_escalation_timeout_is_validated():
    base = dict(transport_version=2, allow_development_v2=True, supervisor_cli='codex', worker_cli='claude',
                mode='task', workers=['worker-1'])
    for bad in (59, -1, 'x'):
        with pytest.raises(ValueError, match='dialog_escalation_timeout'):
            runtime_v2.validate_config(dict(base, dialog_escalation_timeout=bad))
    runtime_v2.validate_config(dict(base, dialog_escalation_timeout=60))


# ------------------------------------------------------------------ plan F' 8.1: delete classifier
@pytest.mark.parametrize('screen', [
    DIALOG_RM, DIALOG_RM_LITERAL, 'Bash command\n  rm -rf "$X"\nDo you want to proceed?\n❯ 1. Yes',
    'Bash command\n  unlink /tmp/a\nDo you want to proceed?', 'Bash command\n  git clean -fd\nDo you want to proceed?',
    'Bash command\n  psql -c "DROP TABLE users"\nDo you want to proceed?', '确认删除这些文件？\n❯ 1. Yes',
    'Bash command\n  mv build/out.txt /tmp/elsewhere\nDo you want to proceed?',
    'Rename src/a.py to src/b.py?\n❯ 1. Yes', 'Bash command\n  git reset --hard HEAD~1\nDo you want to proceed?',
    'Bash command\n  git branch -D old\nDo you want to proceed?', 'Deleting 3 files. Do you want to proceed?',
    '把旧目录移动到归档？\n❯ 1. Yes',
])
def test_delete_classifier_is_strict(screen):
    table = watchdog.parse_table(fleet_config.block('watchdog_prompts'))
    assert watchdog.dialog_class(table, None, screen) == 'delete'


@pytest.mark.parametrize('screen', [
    READ.format('/Users/someone/notes.md'), DIALOG_TRUST, CLAUDE_TRUST_LIVE,
    'Do you want to make this edit to report.md?\n❯ 1. Yes\n  2. No',
])
def test_non_delete_dialogs_are_other(screen):
    table = watchdog.parse_table(fleet_config.block('watchdog_prompts'))
    assert watchdog.dialog_class(table, None, screen) == 'other'


def test_unreadable_screen_is_not_classified():
    table = watchdog.parse_table({})
    assert watchdog.dialog_class(table, None, '') is None and watchdog.dialog_class(table, None, '\n  \n') is None
    assert watchdog.dialog_class(table, {'name': 'dangerous_rm'}, '') == 'delete'


def test_delete_markers_only_add_and_bad_ones_are_refused():
    table = watchdog.parse_table({'delete_markers': ['obliterate']})
    assert watchdog.dialog_class(table, None, 'Obliterate the cache?') == 'delete'
    assert watchdog.dialog_class(table, None, 'rm -rf x') == 'delete'          # built-ins stay in force
    for bad in (['', 'x'], 'rm', [3]):
        with pytest.raises(ValueError, match='delete_markers'):
            watchdog.parse_table({'delete_markers': bad})


def test_whitelisted_dialog_showing_a_delete_is_never_pressed(wd):
    f = wd
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = 'Bash command\n  rm -rf build\n' + read_in_run(f)
    f.v2.watchdog.tick(time.time())
    waits_unpressed(f, 'worker-1', 'read_outside', 'delete')
    assert audits(f, 'watch.prompt_unverified')[0]['dialog_class'] == 'delete'


def test_live_claude_trust_dialog_wrapped_path_is_read_whole():
    table = watchdog.parse_table(fleet_config.block('watchdog_prompts'))
    entry = watchdog.match_prompt(table, CLAUDE_TRUST_LIVE)[0]
    assert watchdog.authorize(entry, CLAUDE_TRUST_LIVE, 25, CLAUDE_TRUST_LIVE_CWD, '/nowhere')[0] is True
    assert watchdog.authorize(entry, CLAUDE_TRUST_LIVE, 25, CLAUDE_TRUST_LIVE_CWD + '-other', '/nowhere')[0] is False
    assert watchdog.authorize(entry, CLAUDE_TRUST_LIVE, 25, CLAUDE_TRUST_LIVE_CWD.rsplit('/', 1)[0], '/nowhere')[0] is False


def test_claude_trust_dialog_for_the_slot_own_directory_is_answered_down_enter(wd):
    f = wd
    f.setf('worker-2', state='starting')
    f.mgr.screens[f.agent('worker-2')['pane']] = claude_trust(str(cwd(f, 'worker-2')))
    f.v2.watchdog.tick(time.time())
    assert keys(f, 'worker-2') == ['Down', 'Enter'] and f.agent('worker-2')['state'] == 'starting'
    assert audits(f, 'watch.prompt_answered')[0]['prompt'] == 'trust_folder'


# ------------------------------------------------------------------ E x F: wall gate first (§23 / §24)
def test_wall_on_screen_trips_the_brake_and_the_watchdog_presses_nothing(wd):
    f = wd
    dispatch_one(f)
    pane = f.agent('worker-1')['pane']
    f.mgr.screens[pane] = ("⎿  You've reached your Fable limit. Run /usage-credits to continue "
                           "or switch models with /model.\n" + DIALOG_RM_LITERAL)
    f.v2.watchdog.tick(time.time())
    assert keys(f, 'worker-1') == [] and audits(f, 'watch.prompt_answered') == []
    assert f.paused() and f.agent('worker-1')['state'] != 'held'
    assert any(r.get('event') == 'brake.tripped' for r in audits(f)) or f.r.hget('fx:brake', 'tripped')
