"""Fixes from the real-cmux acceptance of 373d22c (docs/design.md §26).

Same two-fleet v2 world as test_subfleet / test_escalate: real private Redis, in-process daemons,
real ``claude_stop`` hooks, a fake cmux world.
"""
import json
from pathlib import Path

from test_contracts import cf, inbox, item, p0_reply, report, write  # noqa: F401  (fixture + helpers)
from test_escalate import escalate, ok_check, parallel_m0  # noqa: F401
from test_fleet import received  # noqa: E402
from test_subfleet import (adopt_by_hook, child_daemon, given, hook, no_launch, parent_hook, step,  # noqa: F401
                           stop_child_like_daemon, tree)
from cmux_fleet import runtime_v2, subfleet


# ------------------------------------------------------------------ P1-3: startup READY
def test_ready_reply_takes_the_last_line():
    assert runtime_v2.ready_reply("I've read AGENTS.md. No task has been sent yet.\n\nREADY")
    assert runtime_v2.ready_reply('**READY**\n') and runtime_v2.ready_reply('READY.')
    assert not runtime_v2.ready_reply('NOT READY') and not runtime_v2.ready_reply('Ready to go')
    assert not runtime_v2.ready_reply('READY\nI will start now') and not runtime_v2.ready_reply('')


def test_startup_not_ready_gets_one_correction_then_blocks(tree):
    t, f = tree, tree.f
    rec = subfleet.spawn_child(f, 'worker-1', 'M0', 'Research five sources and merge them', 2, launch=no_launch)
    cf = child_daemon(t, rec)
    # a chatty but well-ended reply settles
    assert hook(t, cf, 'worker-1', "I've read AGENTS.md. No task has been sent yet.\n\nREADY",
                prompt='startup')['status'] == 'ready'
    # anything else: one correction (through the Stop hook on the hook channel), the slot keeps starting
    answer = hook(t, cf, 'worker-2', 'Ready to go', prompt='startup')
    assert answer['status'] == 'rejected' and 'asked once more' in answer['reason']
    pane = cf.agent('worker-2')['pane']
    assert given(t, cf, 'worker-2')[-1] == runtime_v2.STARTUP_NUDGE
    assert runtime_v2.STARTUP_NUDGE not in [x for p, x in t.world.sent if p == pane]  # never typed
    assert cf.agent('worker-2')['state'] == 'starting' and cf.task().get('status') != 'blocked'
    # the corrected turn settles normally
    assert hook(t, cf, 'worker-2', 'READY', prompt=runtime_v2.STARTUP_NUDGE)['status'] == 'ready'
    assert cf.agent('worker-2')['state'] == 'idle'


def test_second_bad_startup_reply_blocks(tree):
    t, f = tree, tree.f
    rec = subfleet.spawn_child(f, 'worker-1', 'M0', 'Research five sources and merge them', 1, launch=no_launch)
    cf = child_daemon(t, rec)
    assert hook(t, cf, 'worker-1', 'NOT READY', prompt='startup')['status'] == 'rejected'
    step(t)
    assert hook(t, cf, 'worker-1', 'still not', prompt=runtime_v2.STARTUP_NUDGE)['status'] == 'rejected'
    assert cf.agent('worker-1')['state'] == 'dead'
    assert cf.task()['status'] == 'blocked'


# ------------------------------------------------------------------ P1-4: sub-fleet attempts are capped
def old_attempts(run, task_id, n):
    for i in range(n):
        d = Path(run) / 'sub' / (task_id if i == 0 else task_id + '-attempt-' + str(i + 1))
        d.mkdir(parents=True)
        subfleet.save_record(d, {'task_id': task_id, 'slot': 'worker-1', 'state': 'failed', 'attempt': i + 1})


def test_escalate_after_max_attempts_fails_the_task(cf, ok_check):
    parallel_m0(cf)
    old_attempts(cf.run_dir, 'M0', 2)  # max_attempts defaults to 2
    report(cf, 'worker-1', escalate())
    row = cf.contract_state()['tasks']['M0']
    assert row['status'] == 'failed' and subfleet.EXHAUSTED in row['reason']
    up = inbox(cf, 'supervisor')[-1]
    assert 'max-attempts 2: sub-fleet M0 already started 2 time(s)' in up and 'marked failed' in up
    assert not inbox(cf, 'worker-1') and cf.escalation_state() == {}
    assert not (cf.run_dir / 'subfleet-requests').exists()  # no spawn was requested


def test_spawn_refused_once_attempts_are_used(tree):
    f = tree.f
    old_attempts(f.run_dir, 'M0', 1)
    rec = subfleet.spawn_child(f, 'worker-1', 'M0', 'Research five sources and merge them', 1, launch=no_launch)
    assert rec['attempt'] == 2 and subfleet.attempts_used(f.run_dir, 'M0') == 2
    reason = subfleet.check(f, 'worker-2', 'M0', 1, ready=False)  # also the cap, before any slot state
    assert reason.startswith('sub-fleet M0 is already running')
    subfleet.save_record(rec['run'], dict(subfleet.record(rec['run']), state='failed'))
    assert subfleet.check(f, 'worker-2', 'M0', 1, ready=False).startswith('max-attempts 2: sub-fleet M0 already')


# ------------------------------------------------------------------ P0: the child delivers inside its own workspace
WORK_REPORT = 'Done:\n- {done}\nEvidence:\n- {ev}\nNot done: none\nBlocked: none\n'


def test_contract_subfleet_writes_its_own_dir_and_the_parent_check_passes(tree, monkeypatch):
    """Acceptance P0 path: contract parent task, M0 provides worker-1/sources.md, worker-1 escalates,
    the child (whose workers may only write inside the child workspace) delivers there; the parent
    daemon copies it to the parent path, the parent's literal check passes, M0 is done."""
    t, f = tree, tree.f
    blocked = {'status': 'BLOCKED', 'completion': 0, 'reason': 'replaced by a contract task', 'dispatch': []}
    assert hook(t, f, 'supervisor', json.dumps(blocked))['status'] == 'committed'  # end the fixture's task
    step(t)
    assert f.task()['status'] == 'blocked'
    runtime_v2.post_event(f.v2.bus, dict(kind='task', request_id='ctask', spec={
        'goal': 'research report', 'contracts': True}), 'ctask')
    step(t)
    assert hook(t, f, 'supervisor', json.dumps(p0_reply()))['status'] == 'committed'
    step(t)
    (f.workspace() / 'worker-1').mkdir(exist_ok=True)
    (f.workspace() / 'worker-1' / 'skeleton.md').write_text(
        'IF-M0-src@1 · M0 → M2 · PASS · ok\nIF-M1-metrics@1 · M1 → M2 · PASS · ok\n')
    assert hook(t, f, 'worker-1', WORK_REPORT.format(done='skeleton', ev='worker-1/skeleton.md'))['status'] == 'committed'
    step(t)
    assert f.contract_state()['phase'] == 'parallel'
    assert hook(t, f, 'supervisor', json.dumps({'status': 'RUNNING', 'completion': 10, 'reason': 'go', 'dispatch': [
        item('M0', 'worker-1', provides=['IF-M0-src']), item('M1', 'worker-2', provides=['IF-M1-metrics'])]})
    )['status'] == 'committed'
    step(t)
    assert hook(t, f, 'worker-1', WORK_REPORT.format(done='read the goal', ev='five sources') + escalate()
                )['status'] == 'committed'
    step(t)
    assert f.contract_state()['tasks']['M0']['status'] == 'escalated'
    approve = {'status': 'RUNNING', 'completion': 20, 'reason': 'split', 'dispatch': [],
               'escalations': [{'task': 'M0', 'decision': 'approve'}]}
    assert hook(t, f, 'supervisor', json.dumps(approve))['status'] == 'committed'
    f.tick()
    rec = subfleet.record(f.run_dir / 'sub' / 'M0')
    parent_path = f.workspace() / 'worker-1' / 'sources.md'
    inside = Path(rec['run']) / 'workspace' / 'worker-1' / 'sources.md'
    assert rec['deliver'] == [[str(inside), str(parent_path)]] and rec['output'] == str(parent_path)
    cf, _ = adopt_by_hook(t, monkeypatch, rec)  # §48: the role change comes through the pane's hook, never typed
    step(t)
    goal = cf.task()['goal']
    assert str(inside) in goal and str(parent_path) not in goal and 'IF-M0-src@1' in goal
    # the child's worker writes inside its own directory (as its rules demand), the child completes
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text('## Sources\n| https://a |\n')
    cf.v2.finish('completed', 'merged')
    cf.write_result()
    f.tick()
    stop_child_like_daemon(cf)
    f.tick()
    assert subfleet.record(rec['run'])['state'] == 'returned'
    assert parent_path.read_text() == '## Sources\n| https://a |\n'
    state = f.contract_state()
    assert state['tasks']['M0']['status'] == 'done', state['tasks']['M0']
    assert state['interfaces']['IF-M0-src@1']['status'] == 'ready'
    msgs = [f.v2.bus.message(m)['body']['text'] for m in f.v2.bus.pending('supervisor')]
    assert any('[Sub-fleet M0 completed]' in x for x in msgs)


def test_deliverable_on_another_child_worker_counts_and_worker_1_wins(tree):
    t, f = tree, tree.f
    out = f.workspace() / 'worker-1' / 'm0.md'
    rec = subfleet.spawn_child(f, 'worker-1', 'M0', 'Research five sources and merge them', 2,
                               output=str(out), launch=no_launch)
    child_ws = Path(rec['run']) / 'workspace'
    (child_ws / 'worker-2').mkdir(parents=True, exist_ok=True)
    (child_ws / 'worker-2' / 'm0.md').write_text('from worker-2')  # the child put the final piece on worker-2
    cfg = {'run': rec['run'], 'workers': ['worker-1', 'worker-2']}
    subfleet.deliver_back(f, rec, cfg)
    assert out.read_text() == 'from worker-2'
    (child_ws / 'worker-1').mkdir(exist_ok=True)
    (child_ws / 'worker-1' / 'm0.md').write_text('from worker-1')  # the declared place wins
    subfleet.deliver_back(f, rec, cfg)
    assert out.read_text() == 'from worker-1'


def test_child_that_did_not_complete_copies_nothing(tree):
    t, f = tree, tree.f
    out = f.workspace() / 'worker-1' / 'm0.md'
    rec = subfleet.spawn_child(f, 'worker-1', 'M0', 'Research five sources and merge them', 1,
                               output=str(out), launch=no_launch)
    cf = child_daemon(t, rec)
    inside = Path(rec['deliver'][0][0])
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text('half done')
    cf.v2.finish('blocked', 'gave up')
    cf.write_result()
    f.tick()
    stop_child_like_daemon(cf)
    f.tick()
    assert subfleet.record(rec['run'])['state'] == 'failed' and not out.exists()


# ------------------------------------------------------------------ P2: a garbled Interfaces line is named
def test_written_file_without_the_interfaces_line_tells_the_worker(cf):
    parallel_m0(cf)
    write(cf, 'worker-1/sources.md', '## Sources\n| https://a |\n')
    report(cf, 'worker-1', 'IF-M0-src@1: provided: ready\n')  # the acceptance slip: no `Interfaces:` prefix
    row = cf.contract_state()['tasks']['M0']
    assert row['status'] == 'failed' and 'no line `Interfaces: IF-M0-src@1 provided: ready`' in row['reason']
    hint = inbox(cf, 'worker-1')[-1]
    assert 'did not announce IF-M0-src@1' in hint and 'Interfaces: IF-M0-src@1 provided: ready' in hint
    assert 'was asked for that line (no re-dispatch needed)' in inbox(cf, 'supervisor')[-1]
    # the corrected report settles M0 (the hint was queued under M0)
    report(cf, 'worker-1', 'Interfaces: IF-M0-src@1 provided: ready\n')
    assert cf.contract_state()['tasks']['M0']['status'] == 'done'


def test_a_failing_copy_still_returns_the_pane_and_fails_the_subfleet(tree):
    t, f = tree, tree.f
    out = f.workspace() / 'worker-1' / 'm0.md'
    rec = subfleet.spawn_child(f, 'worker-1', 'M0', 'Research five sources and merge them', 1,
                               output=str(out), launch=no_launch)
    cf = child_daemon(t, rec)
    inside = Path(rec['deliver'][0][0])
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text('merged')
    out.mkdir(parents=True)  # the parent path cannot take a file: copyfile raises IsADirectoryError
    cf.v2.finish('completed', 'merged')
    cf.write_result()
    f.tick()
    stop_child_like_daemon(cf)
    f.tick()
    rec = subfleet.record(rec['run'])
    assert rec['state'] == 'failed' and 'missing or empty' in rec['reason']
    # the pane still comes back (hook-channel worker: as its next generation, never typed into)
    assert f.agent('worker-1')['state'] == 'starting' and f.agent('worker-1')['generation'] == '1'
    assert f.agent('worker-1')['pane'] != rec['pane'] and rec['pane'] not in t.world.alive
