"""Transport v2 with a Codex supervisor and official Claude Code workers (task mode).

Real private Redis + the real hook entry points (Codex notify, Claude Stop); fake cmux
and fake CLIs. This proves the daemon/hook/bus wiring, not a real Codex/Claude TUI.
"""
import json
from pathlib import Path
import sys
import threading
import time
import uuid

import test_fleet

import pytest

import hook_sim
from test_fleet import FakeMgr, codex_rows, write_rollout
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from fleetmux import cmux_manager, codex_queue, fleet, runtime_v2, transport_inventory

REAL_ENQUEUE = codex_queue.enqueue  # captured at import, before conftest installs its fake

WORKERS = ['worker-' + str(i) for i in range(1, 7)]


# ------------------------------------------------------------------ gate matrix
CLIS = ('pi', 'codex', 'claude')
# Real smokes (scripts/smoke_pair.sh, 2026-10-12) verified the codex+pi, codex+claude/service and
# Claude-supervisor rows; codex+codex/task stays unverified (smoke-only).
ALLOWED = {('pi', 'pi', 'task'), ('pi', 'pi', 'service'), ('codex', 'claude', 'task'),
           ('codex', 'pi', 'task'), ('codex', 'pi', 'service'), ('codex', 'claude', 'service'),
           ('claude', 'pi', 'task'), ('claude', 'pi', 'service'), ('claude', 'claude', 'task'),
           ('claude', 'claude', 'service')}   # 2026-11-03 real smoke (Opus 5.5 supervisor)
SMOKE_ONLY = {('codex', 'codex', 'task')}
# pi supervisor + official Claude workers (2026-10-13 default): smoke-only until its real smoke flips
# the row to verified (scripts/smoke_pair.sh pi claude <mode>); the test follows the shipped status.
PI_CLAUDE = {('pi', 'claude', 'task'), ('pi', 'claude', 'service')}


@pytest.mark.parametrize('mode', ['task', 'service'])
@pytest.mark.parametrize('worker', CLIS)
@pytest.mark.parametrize('supervisor', CLIS)
def test_v2_cli_matrix_allows_only_validated_pairs_with_reason(supervisor, worker, mode):
    cfg = dict(transport_version=2, supervisor_cli=supervisor, worker_cli=worker, mode=mode,
               allow_development_v2=True)
    if (supervisor, worker, mode) in PI_CLAUDE:
        from fleetmux import cli_pairs
        row = cli_pairs.load_pairs().lookup(supervisor, worker, mode)
        assert row and row['status'] in ('verified', 'unverified') and not row['adapter_missing']
        if row['status'] == 'unverified':
            with pytest.raises(ValueError, match='--lifecycle smoke'):
                runtime_v2.validate_config(cfg)
            cfg = dict(cfg, lifecycle='smoke')
        else:
            assert 'smoke_pair.sh pi claude ' + mode in row['verified_by'] and 'PASS' in row['verified_by']
        assert runtime_v2.validate_config(cfg) == 2
        with pytest.raises(ValueError, match='allow-development-v2'):
            runtime_v2.validate_config(dict(cfg, allow_development_v2=False))
        return
    if (supervisor, worker, mode) in ALLOWED:
        assert runtime_v2.validate_config(cfg) == 2
        with pytest.raises(ValueError, match='allow-development-v2'):
            runtime_v2.validate_config(dict(cfg, allow_development_v2=False))
        return
    with pytest.raises(ValueError) as exc:
        runtime_v2.validate_config(cfg)
    reason = str(exc.value)
    if (supervisor, worker, mode) in SMOKE_ONLY:
        assert 'Codex workers are unvalidated' in reason and '--lifecycle smoke' in reason
        assert runtime_v2.validate_config(dict(cfg, lifecycle='smoke')) == 2
    elif supervisor == 'claude':
        assert 'no Claude supervisor adapter' in reason
    elif (supervisor, worker) == ('codex', 'codex'):
        assert 'Codex workers are unvalidated' in reason
    else:
        assert 'mixed Pi/' in reason and 'unvalidated' in reason


def no_provisioning(monkeypatch):
    def forbidden():
        pytest.fail('transport rejection must precede cmux/Redis provisioning')
    monkeypatch.setattr(cmux_manager, 'cmux_available', forbidden)


@pytest.mark.parametrize('argv,match', [
    (['--mode', 'service', '--lifecycle', 'dev_task'], 'runs on --mode task, not --mode service'),
    ([], 'allow-development-v2'),
])
def test_start_rejects_codex_claude_before_provisioning(monkeypatch, argv, match):
    no_provisioning(monkeypatch)
    extra = [] if match == 'allow-development-v2' else ['--allow-development-v2']
    with pytest.raises(SystemExit, match=match):
        fleet.main(['start', '--supervisor-cli', 'codex', '--worker-cli', 'claude'] + argv + extra)


def test_start_admits_codex_claude_task_to_provisioning(monkeypatch):
    class Reached(Exception):
        pass

    def reached():
        raise Reached
    monkeypatch.setattr(cmux_manager, 'cmux_available', reached)
    with pytest.raises(Reached):
        fleet.main(['start', '--lifecycle', 'dev_task', '--supervisor-cli', 'codex', '--worker-cli', 'claude', '--workers', '6',
                    '--allow-development-v2'])


def test_inventory_reports_mixed_support_and_reason():
    ok = {'supervisor_cli': 'codex', 'worker_cli': 'claude', 'mode': 'task'}
    assert transport_inventory.v2_cli_problem(ok) is None
    assert transport_inventory.v2_cli_problem(dict(ok, mode='service')) is None   # verified by real smoke
    assert 'Codex workers are unvalidated' in transport_inventory.v2_cli_problem(dict(ok, worker_cli='codex'))
    assert transport_inventory.v2_cli_problem(dict(ok, worker_cli='codex', lifecycle='smoke')) is None


# ------------------------------------------------------------------ live wiring
@pytest.fixture
def mixed(private_redis_v2, tmp_path):  # noqa: F811
    r = private_redis_v2
    r.flushdb()
    run = tmp_path / ('run-' + uuid.uuid4().hex)
    (run / 'workspace').mkdir(parents=True)
    sessions = tmp_path / 'codex-home' / 'sessions'
    # A real start records the resolved claude binary (cfg['claude']); the profile preflight checks
    # it. A stub that is never run keeps these tests off the host's PATH (no claude installed: fine).
    claude_bin = tmp_path / 'bin' / 'claude'
    claude_bin.parent.mkdir()
    claude_bin.write_text('#!/bin/sh\nexit 1\n')
    claude_bin.chmod(0o755)
    cfg = dict(run=str(run), redis_socket=r.connection_pool.connection_kwargs['path'],
               python=sys.executable, workers=list(WORKERS), mode='task', supervisor_cli='codex',
               worker_cli='claude', claude=str(claude_bin), codex_home=str(tmp_path / 'codex-home'), transport_version=2,
               allow_development_v2=True, stall_timeout=0, v2_lease_ms=300000, max_busy=0)
    path = run / 'config.json'
    fleet.save(path, cfg)
    claude_home = tmp_path / 'claude-projects'
    f = fleet.Fleet(path, r, FakeMgr(), sessions=sessions)
    f.test_sessions, f.test_claude = sessions, claude_home
    f.test_threads = {'supervisor': str(uuid.uuid4())}
    f.test_threads.update({w: str(uuid.uuid4()) for w in WORKERS})
    import fleetmux.fleet as fleet_mod
    real_trust = fleet_mod.trust_claude_dir
    fleet_mod.trust_claude_dir = lambda cwd: None  # never touch the operator's ~/.claude.json
    try:
        f.start_all()
        r.set(f.v2.prefix + ':boot', 'running')
        for slot in f.names:
            assert answer_turn(f, slot, 'READY', startup=True)['status'] == 'ready'
        yield f
    finally:
        fleet_mod.trust_claude_dir = real_trust
        f.v2.close()


def drive(f, fn):
    """Run a hook (it blocks on the daemon reply) while the daemon steps."""
    box = {}
    thread = threading.Thread(target=lambda: box.setdefault('value', fn()))
    thread.start()
    deadline = time.time() + 15
    while thread.is_alive() and time.time() < deadline:
        f.v2.step(20)
    thread.join(2)
    assert not thread.is_alive()
    return box['value']


def typed(f, slot):
    """What the slot's current generation was given: queued for a Codex pane (codex queue), pasted for a
    non-hook pane (test_fleet.received), or handed over by a Claude worker's Stop hook."""
    return test_fleet.received(f.mgr, f.agent(slot)['pane']) + hook_sim.handed_to(f, slot)


def answer_turn(f, slot, text, *, startup=False, prompt=None, generation=None):
    """Fire the real hook entry point of this slot's CLI for a finished turn."""
    gen = int(f.agent(slot)['generation']) if generation is None else generation
    thread = f.test_threads[slot] if gen == 0 else f.test_threads[slot] + '-g' + str(gen)
    prompt = prompt if prompt is not None else (typed(f, slot)[-1] if typed(f, slot) and not startup else 'startup prompt')
    if slot == 'supervisor':
        turn_id, rows = codex_rows(thread, prompt, text)  # the queued delivery's own turn, or a typed one
        write_rollout(f.test_sessions, thread, fleet.agent_cwd(f.cfg, slot), rows=rows)
        payload = json.dumps({'type': 'agent-turn-complete', 'thread-id': thread, 'turn-id': turn_id,
                              'input-messages': [prompt], 'last-assistant-message': text})
        return drive(f, lambda: fleet.notify(f.path, slot, str(gen), payload, f.test_sessions))
    transcript = f.test_claude / slot / (thread + '.jsonl')
    transcript.parent.mkdir(parents=True, exist_ok=True)
    rows = [hook_sim.user_row(prompt, hook_sim.was_handed(slot, prompt)),
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}}]
    transcript.write_text('\n'.join(json.dumps(x) for x in rows) + '\n')
    raw = json.dumps({'session_id': thread, 'transcript_path': str(transcript), 'last_assistant_message': text})
    return drive(f, lambda: hook_sim.claude_stop(f.path, slot, str(gen), raw))


def decision(workers):
    return json.dumps({'status': 'RUNNING', 'completion': 10, 'reason': 'split the work',
                       'dispatch': [{'to': w, 'instruction': 'Write ' + w + '/part.md with the findings.'}
                                    for w in workers]})


def submit(f):
    runtime_v2.post_event(f.v2.bus, dict(kind='task', request_id='new-task', spec={'goal': 'build M0 parts'}), 'new-task')
    assert f.v2.step(10)
    f.pump()


def no_message_files(f):
    return not list(f.run_dir.rglob('.inbox')) and not list(f.run_dir.rglob('msg-*.md'))


def test_startup_ready_binds_sessions_via_hooks(mixed):
    f = mixed
    for slot in f.names:
        info = f.agent(slot)
        assert info['state'] == 'idle' and info['runtime_ready'] == '1'
        assert f.v2.bus.slot_state(slot)['session_id'] == f.test_threads[slot]
    assert f.agent('supervisor')['transcript'] == ''          # Codex usage stays rollout-based
    assert f.agent('worker-1')['transcript'].endswith('.jsonl')
    sup_role = (fleet.agent_cwd(f.cfg, 'supervisor') / 'AGENTS.md').read_text()
    worker_role = (fleet.agent_cwd(f.cfg, 'worker-1') / 'CLAUDE.md').read_text()
    assert 'fleet_decision' not in sup_role and 'fleet_result' not in worker_role  # original AGENTS.md formats


def test_codex_supervisor_dispatches_six_claude_workers_through_v2_bus(mixed):
    f = mixed
    submit(f)
    body, = typed(f, 'supervisor')
    lease = f.v2.bus.slot_state('supervisor')
    assert lease['status'] == 'received' and lease['consumer'] == 'tui-supervisor-g0'
    assert 'build M0 parts' in body and 'Reply with the JSON format from AGENTS.md.' in body
    for transport_id in (lease['message_id'], lease['current_delivery'], 'fleet_decision', '.inbox'):
        assert transport_id not in body
    assert answer_turn(f, 'supervisor', decision(WORKERS))['status'] == 'committed'
    f.pump()  # max_busy=0: all six idle workers receive in one pass
    for slot in WORKERS:
        text, = typed(f, slot)
        assert 'Write ' + slot + '/part.md' in text and 'fleet_result' not in text
        assert f.agent(slot)['state'] == 'busy'
        assert f.v2.bus.slot_state(slot)['status'] == 'received'
    assert sum(f.agent(w)['state'] == 'busy' for w in WORKERS) == 6
    # Nothing is pasted at all: Codex takes its deliveries from its own queue (codex queue) and the
    # Claude workers from their Stop hook (hook channel).
    modes = dict(getattr(f.mgr, 'bracketed', []))  # set by the first paste only
    assert f.agent('supervisor')['pane'] not in modes
    assert not any(f.agent(w)['pane'] in modes for w in WORKERS)
    assert no_message_files(f)


def test_claude_stop_reply_settles_into_v2_and_reports_to_supervisor(mixed):
    f = mixed
    submit(f)
    answer_turn(f, 'supervisor', decision(['worker-1']))
    f.pump()
    out = f.workspace() / 'worker-1' / 'part.md'
    out.parent.mkdir(parents=True, exist_ok=True); out.write_text('findings')
    report = 'Done:\n- wrote part.md\nEvidence:\n- worker-1/part.md 8 bytes\nNot done: none\nBlocked: none'
    env = f.v2.bus.slot_state('worker-1')
    assert answer_turn(f, 'worker-1', report)['status'] == 'committed'
    msg = f.v2.bus.message(env['message_id'])
    assert msg['status'] == 'committed' and msg['outcome'] == 'completed'
    assert msg['result']['done'] == ['wrote part.md'] and msg['result']['evidence'] == ['worker-1/part.md 8 bytes']
    assert f.v2.bus.slot_state('worker-1')['pel_count'] == 0 and f.agent('worker-1')['state'] == 'idle'
    f.pump()
    followup = typed(f, 'supervisor')[-1]
    assert followup.startswith('[worker-1 report]') and 'wrote part.md' in followup
    assert no_message_files(f)


def test_duplicate_manual_and_old_generation_turns_are_rejected(mixed):
    f = mixed
    submit(f)
    answer_turn(f, 'supervisor', decision(['worker-1']))
    f.pump()
    report = 'Done:\n- x\nEvidence:\n- worker-1/x.md\nNot done: none\nBlocked: none'
    # A manual turn the operator typed in the worker tab cannot settle the delivery.
    assert answer_turn(f, 'worker-1', report, prompt='just chatting')['status'] == 'ignored'
    assert f.v2.bus.slot_state('worker-1')['status'] == 'received'
    # A retired generation's late hook is stale.
    assert answer_turn(f, 'worker-1', report, generation=5)['status'] == 'stale'
    assert answer_turn(f, 'worker-1', report)['status'] == 'committed'
    # The same Stop fired twice: nothing is received any more.
    assert answer_turn(f, 'worker-1', report, prompt=typed(f, 'worker-1')[-1])['status'] == 'ignored'
    assert f.r.llen(f.v2.bus.pending_key('supervisor')) == 1  # exactly one report follow-up


def dispatch_one(f):
    submit(f)
    answer_turn(f, 'supervisor', decision(['worker-1']))
    f.pump()


def test_unparseable_worker_reply_gets_corrections_then_blocks(mixed):
    f = mixed
    dispatch_one(f)
    for attempt in range(1, runtime_v2.TUI_REPORT_RETRIES + 1):
        env = f.v2.bus.slot_state('worker-1')
        answer_turn(f, 'worker-1', '## Done\nI wrote it. Anything else?')
        assert f.v2.bus.message(env['message_id'])['outcome'] == 'failed'  # never a fake success
        assert f.task()['status'] == 'running' and not f.paused()
        assert f.agent('worker-2')['state'] == 'idle'
        f.pump()  # correction goes through the normal delivery path: new delivery + fence
        again = f.v2.bus.slot_state('worker-1')
        assert again['status'] == 'received' and again['current_delivery'] != env['current_delivery']
        assert int(again['fence']) > int(env['fence']) and again['generation'] == env['generation']
        assert 'Done: / Evidence: / Not done: / Blocked:' in typed(f, 'worker-1')[-1]
        assert f.r.llen(f.v2.bus.pending_key('supervisor')) == 0  # no bogus report
    answer_turn(f, 'worker-1', 'Still prose.')
    assert f.task()['status'] == 'blocked'


def test_new_task_is_refused_plainly_while_the_supervisor_is_held_from_the_last_one(mixed):
    # 2026-10-28 run fleet-20261028-112659: BLOCKED held the in-flight pi supervisor; the next task was
    # "accepted" but the held supervisor could never take it, and nothing said so.
    f = mixed
    submit(f)  # the task is in flight at the supervisor
    f.v2.finish('blocked', 'worker-1: exactly one structured candidate is required')
    assert f.agent('supervisor')['state'] == 'held'
    runtime_v2.post_event(f.v2.bus, dict(kind='task', request_id='next-task', spec={'goal': 'continue the run'}),
                          'next-task')
    assert f.v2.step(10)
    answer = runtime_v2.wait_reply(f.v2.bus, 'next-task', timeout=5)
    assert answer['status'] == 'rejected' and 'supervisor is held' in answer['reason']
    assert 'exactly one structured candidate' in answer['reason'] and 'new run' in answer['reason']
    assert f.task()['status'] == 'blocked' and f.r.llen(f.v2.bus.pending_key('supervisor')) == 0


def test_corrected_worker_report_settles_and_resets_retries(mixed):
    f = mixed
    dispatch_one(f)
    answer_turn(f, 'worker-1', 'I finished, it is in part.md.')
    f.pump()
    report = 'Done:\n- wrote part.md\nEvidence:\n- worker-1/part.md\nNot done: none\nBlocked: none'
    assert answer_turn(f, 'worker-1', report)['status'] == 'committed'
    assert f.task()['status'] == 'running' and f.agent('worker-1')['report_retries'] == '0'
    f.pump()
    assert typed(f, 'supervisor')[-1].startswith('[worker-1 report]')


def test_max_busy_caps_only_when_set(mixed):
    f = mixed
    f.cfg['max_busy'] = 2
    submit(f)
    answer_turn(f, 'supervisor', decision(WORKERS))
    f.pump()
    assert sum(bool(typed(f, w)) for w in WORKERS) == 2


def test_failed_typing_holds_slot_without_success(mixed):
    f = mixed
    f.mgr.alive.discard(f.agent('supervisor')['pane'])
    submit(f)
    assert f.agent('supervisor')['state'] == 'held' and f.paused()
    assert f.v2.bus.slot_state('supervisor')['status'] == 'uncertain'


def test_startup_must_be_ready(private_redis_v2, tmp_path):  # noqa: F811
    r = private_redis_v2
    r.flushdb()
    run = tmp_path / 'run'
    (run / 'workspace').mkdir(parents=True)
    cfg = dict(run=str(run), redis_socket=r.connection_pool.connection_kwargs['path'], python=sys.executable,
               workers=['worker-1'], mode='task', supervisor_cli='codex', worker_cli='claude',
               codex_home=str(tmp_path / 'ch'), transport_version=2, allow_development_v2=True, stall_timeout=0)
    fleet.save(run / 'config.json', cfg)
    f = fleet.Fleet(run / 'config.json', r, FakeMgr(), sessions=tmp_path / 'ch' / 'sessions')
    import fleetmux.fleet as fleet_mod
    real, fleet_mod.trust_claude_dir = fleet_mod.trust_claude_dir, (lambda cwd: None)
    try:
        f.start_all()
        f.test_sessions, f.test_claude = tmp_path / 'ch' / 'sessions', tmp_path / 'cp'
        f.test_threads = {'supervisor': str(uuid.uuid4()), 'worker-1': str(uuid.uuid4())}
        assert answer_turn(f, 'worker-1', 'Hello, how can I help?', startup=True)['status'] == 'rejected'
        assert f.agent('worker-1')['state'] == 'starting'  # one correction first (acceptance 10/13 P1-3)
        assert answer_turn(f, 'worker-1', 'Hello again', startup=True)['status'] == 'rejected'
        assert f.agent('worker-1')['state'] == 'dead'
    finally:
        fleet_mod.trust_claude_dir = real
        f.v2.close()


def test_renewal_extends_received_tui_lease(mixed):
    f = mixed
    submit(f)
    before = int(f.v2.bus.slot_state('supervisor')['lease_until'])
    f.v2.renewed.clear()
    time.sleep(.01)
    f.v2.renew_tui()
    assert int(f.v2.bus.slot_state('supervisor')['lease_until']) >= before
    assert any(fields.get('event') == 'renewed' for _, fields in f.r.xrange(f.v2.bus.audit_key))


def test_v2_claude_stop_hook_outlives_ready_wait(tmp_path):
    def timeout(cfg, generation):
        argv = fleet.claude_argv('cfg.json', cfg, 'worker-1', generation, 'hi')
        return json.loads(Path(argv[argv.index('--settings') + 1]).read_text())['hooks']['Stop'][0]['hooks'][0]['timeout']
    cfg = {'python': 'py', 'run': str(tmp_path), 'workers': ['worker-1'], 'transport_version': 2,
           'profile_ready_timeout': 180, 'worker_cli': 'claude'}
    fleet.agent_cwd(cfg, 'worker-1').mkdir(parents=True)
    # Stop-hook channel: the hook waits there for the next delivery (claude_hook_timeout, default one day).
    assert timeout(cfg, 0) == 86400
    assert timeout(dict(cfg, claude_hook_timeout=900), 1) == 900
    # Fallback (claude_hook_delivery: false): the READY wait budget as before, v1 unchanged.
    typed_path = dict(cfg, claude_hook_delivery=False)
    assert timeout(typed_path, 2) == 210
    del typed_path['transport_version']
    assert timeout(typed_path, 3) == 30


def test_codex_turn_matches_rollout_cwd_through_symlinked_run_dir(tmp_path):
    real = tmp_path / 'real'
    (real / 'agents' / 'supervisor').mkdir(parents=True)
    link = tmp_path / 'link'
    link.symlink_to(real)
    thread = str(uuid.uuid4())
    sessions = tmp_path / 'sessions'
    write_rollout(sessions, thread, real / 'agents' / 'supervisor', rows=[
        {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'u1', 'last_agent_message': 'READY'}}])
    data = {'thread-id': thread, 'turn-id': 'u1', 'last-assistant-message': 'READY'}
    assert fleet.completed_turn(fleet.agent_cwd({'run': str(link)}, 'supervisor'), data, sessions)


def test_claude_settlement_survives_megabytes_of_tool_output(mixed):
    f = mixed
    submit(f)
    answer_turn(f, 'supervisor', decision(['worker-1']))
    f.pump()
    prompt = typed(f, 'worker-1')[-1]
    thread = f.test_threads['worker-1']
    transcript = f.test_claude / 'worker-1' / (thread + '.jsonl')
    report = 'Done:\n- big\nEvidence:\n- worker-1/big.log\nNot done: none\nBlocked: none'
    rows = [hook_sim.user_row(prompt, hook_sim.was_handed('worker-1', prompt)),
            {'type': 'user', 'message': {'role': 'user', 'content': [
                {'type': 'tool_result', 'content': 'x' * 2_000_000}]}},
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': report}]}}]
    transcript.write_text('\n'.join(json.dumps(x) for x in rows) + '\n')
    raw = json.dumps({'session_id': thread, 'transcript_path': str(transcript), 'last_assistant_message': report})
    assert drive(f, lambda: hook_sim.claude_stop(f.path, 'worker-1', '0', raw))['status'] == 'committed'


def test_ignored_turn_is_audited(mixed):
    f = mixed
    answer_turn(f, 'worker-1', 'hello', prompt='manual chat')
    events = [fields for _, fields in f.r.xrange(f.v2.bus.audit_key)]
    assert any(e.get('event') == 'turn.ignored' and e.get('slot') == 'worker-1' for e in events)



def _queue_turn_without_client_id(f, queued_text, answer, turn=None):
    """A queue-triggered supervisor turn whose rollout UserMessage has no client_id (Codex consumed the
    row before `codex queue` returned), fired through the real notify entry point."""
    thread = f.agent('supervisor')['thread']
    turn = turn or 't-' + uuid.uuid4().hex
    rows = [{'timestamp': '2026-10-13T00:00:00.000Z', 'type': 'event_msg', 'payload': {'type': 'task_started',
             'turn_attribution': {'turn_id': turn, 'turn_trigger': 'queue', 'root_turn_id': turn}}},
            {'timestamp': '2026-10-13T00:00:00.001Z', 'type': 'event_msg', 'payload': {'type': 'item_completed',
             'turn_id': turn, 'item': {'type': 'UserMessage', 'client_id': '',
                                       'content': [{'type': 'text', 'text': queued_text}]}}},
            {'timestamp': '2026-10-13T00:00:00.002Z', 'type': 'event_msg', 'payload': {
             'type': 'task_complete', 'turn_id': turn, 'last_agent_message': answer}}]
    test_fleet.append_rollout(f.test_sessions, thread, fleet.agent_cwd(f.cfg, 'supervisor'), rows)
    payload = json.dumps({'type': 'agent-turn-complete', 'thread-id': thread, 'turn-id': turn,
                          'input-messages': [queued_text], 'last-assistant-message': answer})
    gen = f.agent('supervisor')['generation']
    return turn, drive(f, lambda: fleet.notify(f.path, 'supervisor', gen, payload, f.test_sessions))


def test_same_text_different_deliveries_each_complete_without_client_id_and_never_cross_bind(mixed):
    """F1, end to end on the public path, client_id lost every time: worker-1 sends the same report twice,
    so the supervisor gets two different deliveries with the identical body. Each is settled by its own
    token's turn, exactly once; the first delivery's turn (late duplicate) never settles the second."""
    import fleetmux.codex_queue as cq
    f = mixed
    one = json.loads(decision(['worker-1']))
    same = json.dumps(dict(one, dispatch=[dict(one['dispatch'][0], id='M0')]))  # §44: one explicit id keeps both bodies identical
    submit(f)
    rec = cq.record(f.agent('supervisor'))
    assert _queue_turn_without_client_id(f, rec['cq_text'], same)[1]['status'] == 'committed'
    f.pump()
    report = 'Done:\n- wrote worker-1/part.md\nEvidence:\n- worker-1/part.md\nNot done: none\nBlocked: none'
    texts = []
    for round_ in (1, 2):
        assert answer_turn(f, 'worker-1', report)['status'] == 'committed'
        f.pump()
        rec = cq.record(f.agent('supervisor'))
        assert rec['cq_state'] in ('queuing', 'queued') and '[worker-1 report]' in rec['cq_text']
        texts.append(rec)
        if round_ == 1:
            first_turn, out = _queue_turn_without_client_id(f, rec['cq_text'], same)
            assert out['status'] == 'committed', out
            assert cq.record(f.agent('supervisor'))['cq_turn'] == first_turn
            f.pump()
    one, two = texts
    bare = lambda t: t.rsplit('\n\n(fleet delivery ', 1)[0]  # noqa: E731
    assert bare(one['cq_text']) == bare(two['cq_text'])  # identical business body
    assert one['cq_delivery'] != two['cq_delivery'] and one['cq_token'] != two['cq_token']
    # A second run of the first delivery's item (new turn id, its exact text and token) must not settle the
    # second delivery, although the business body is identical.
    late = _queue_turn_without_client_id(f, one['cq_text'], decision(['worker-1']))[1]
    assert late is False or late['status'] in ('ignored', 'stale'), late
    assert cq.record(f.agent('supervisor'))['cq_state'] in ('queuing', 'queued')
    check = json.dumps({'status': 'RUNNING', 'completion': 50, 'reason': 'review',
                        'dispatch': [{'to': 'worker-2', 'instruction': 'Check worker-1/part.md'}]})
    second_turn, out = _queue_turn_without_client_id(f, two['cq_text'], check)
    assert out['status'] == 'committed', out
    now = cq.record(f.agent('supervisor'))
    assert now['cq_state'] == 'bound' and now['cq_turn'] == second_turn != first_turn
    committed = [e for _, e in f.r.xrange(f.v2.bus.audit_key) if e.get('event') == 'committed' and e.get('slot') == 'supervisor']
    ids = [e['delivery_id'] for e in committed]
    assert ids.count(one['cq_delivery']) == 1 and ids.count(two['cq_delivery']) == 1
    assert not [p for p, _ in f.mgr.sent if p == f.agent('supervisor')['pane']]  # never typed


def test_cli_rc0_without_the_queued_line_is_uncertain_not_failed(mixed):
    """R1-02: rc 0 but no "Queued message <id> for thread <t>" line: the item may be queued, so the outcome is
    unknown ('uncertain', which keeps a reattach candidate out), never a refusal ('failed')."""
    import subprocess
    f = mixed
    thread = str(uuid.uuid4())
    def answered(stdout, rc):
        return lambda argv, **kw: subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr='')
    for stdout, rc, kind in (('', 0, 'uncertain'), ('warning: something changed\n', 0, 'uncertain'),
                             ('error: no such thread\n', 1, 'failed')):
        try:
            REAL_ENQUEUE(f.cfg, 'supervisor', thread, 'body', run=answered(stdout, rc))
            raise AssertionError('no QueueError')
        except codex_queue.QueueError as exc:
            assert str(exc).startswith(kind + ':'), (stdout, rc, str(exc))
    ok = REAL_ENQUEUE(f.cfg, 'supervisor', thread, 'body',
                      run=answered('Queued message q-1 for thread ' + thread + '.\n', 0))
    assert ok == {'queued_id': 'q-1', 'thread': thread}
    assert codex_queue.unsettled({'cq_state': 'uncertain'}) and not codex_queue.unsettled({'cq_state': 'failed'})


def test_respawn_abandons_an_unsettled_codex_delivery_with_an_audit(mixed):
    """P2-06: a respawn that does not go through retire/clear still abandons the slot's unsettled Codex
    delivery (audited queue.abandoned reason=respawn) before the new session's record is reset."""
    f = mixed
    submit(f)
    old = codex_queue.record(f.agent('supervisor'))
    assert old['cq_state'] in ('queuing', 'queued') and old['cq_token']
    f.spawn('supervisor', 'You are fleet agent supervisor.1. Read AGENTS.md first.')
    events = [e for _, e in f.r.xrange(f.v2.bus.audit_key) if e.get('event') == 'queue.abandoned']
    assert [e.get('reason') for e in events] == ['respawn'] and events[0].get('delivery') == old['cq_delivery']
    assert codex_queue.record(f.agent('supervisor'))['cq_state'] == ''  # the new session starts without one
    f.spawn('supervisor', 'You are fleet agent supervisor.2. Read AGENTS.md first.')  # nothing unsettled now
    assert len([e for _, e in f.r.xrange(f.v2.bus.audit_key) if e.get('event') == 'queue.abandoned']) == 1


def test_turn_end_uses_the_runtime_cli_of_the_generation_not_only_config(mixed, monkeypatch):
    """R1-01: the turn hook decides Codex vs Claude from the generation's runtime launch overrides, as delivery
    does (Fleet.runtime_cfg); a profile that switches a worker to codex gets the exact queue binding."""
    f = mixed
    seen = []
    real = codex_queue.is_codex
    def spy(cfg, name):
        if sys._getframe(1).f_code.co_name == 'tui_turn':  # only the turn hook's decision
            seen.append((name, cfg.get('worker_cli')))
        return real(cfg, name)
    monkeypatch.setattr(codex_queue, 'is_codex', spy)
    f.setf('worker-1', runtime_launch=json.dumps({'profile_id': 'p', 'overrides': {'worker_cli': 'codex'}}))
    dispatch_one(f)
    answer_turn(f, 'worker-1', 'Done:\n- x\nEvidence:\n- y\nNot done: none\nBlocked: none')
    assert seen and all(cli == 'codex' for slot, cli in seen if slot == 'worker-1'), seen
    assert ('worker-1', 'codex') in seen, seen


def _raising_after(f, monkeypatch, during):
    """A `codex queue` stand-in that runs `during(record)` while the CLI is 'running', then times out."""
    def enqueue(cfg, name, thread, text, **kw):
        during(codex_queue.record(f.agent(name)))
        raise codex_queue.QueueError('uncertain: codex queue gave no answer: timed out after 30 seconds')
    monkeypatch.setattr(codex_queue, 'enqueue', enqueue)


def test_cli_error_after_a_fast_bind_neither_overwrites_bound_nor_holds_the_slot(mixed, monkeypatch):
    """R1-04 race (D2 shape): the queued item's turn is proven (bound) while `codex queue` still hangs; the late
    timeout must not turn 'bound' back into 'uncertain' or hold a delivery the rollout already proved."""
    f = mixed
    _raising_after(f, monkeypatch, lambda rec: codex_queue.mark_bound(f.r, 'supervisor', 't-fast', rec['cq_token']))
    submit(f)
    rec = codex_queue.record(f.agent('supervisor'))
    assert rec['cq_state'] == 'bound' and rec['cq_turn'] == 't-fast'
    assert f.agent('supervisor')['state'] != 'held' and not f.paused()
    events = [e.get('event') for _, e in f.r.xrange(f.v2.bus.audit_key)]
    assert 'queue.late_cli_error' in events and 'queue.uncertain' not in events


def test_cli_error_after_clear_keeps_abandoned(mixed, monkeypatch):
    f = mixed
    _raising_after(f, monkeypatch, lambda rec: codex_queue.clear(f, 'supervisor', 'operator stop'))
    submit(f)
    assert codex_queue.record(f.agent('supervisor'))['cq_state'] == 'abandoned'  # not revived as 'uncertain'
    assert f.agent('supervisor')['state'] == 'held'  # the delivery itself is still unproven: fail closed


def test_mark_bound_never_revives_an_abandoned_or_superseded_record(mixed):
    f = mixed
    submit(f)
    first = codex_queue.record(f.agent('supervisor'))
    codex_queue.clear(f, 'supervisor', 'test')
    assert codex_queue.mark_bound(f.r, 'supervisor', 't-1', first['cq_token']) is False
    assert codex_queue.record(f.agent('supervisor'))['cq_state'] == 'abandoned'
    got = codex_queue.deliver(f, 'supervisor', 'd-again', 'same body')
    assert codex_queue.mark_bound(f.r, 'supervisor', 't-2', first['cq_token']) is False  # old token, new record
    assert codex_queue.record(f.agent('supervisor'))['cq_state'] == 'queued'
    assert codex_queue.mark_bound(f.r, 'supervisor', 't-3', got['token']) is True
    assert codex_queue.record(f.agent('supervisor'))['cq_turn'] == 't-3'


def test_waiting_for_a_report_already_queued_for_the_supervisor_is_a_valid_decision(mixed):
    """Real E2E (worker-3 rehearsal A, w3e2e-fa): both workers finished quickly; while the supervisor answers
    worker-1's report, worker-2's report is already queued for it. "RUNNING, no dispatch, waiting" is then a
    correct decision (another supervisor turn is certain), not 'no worker is busy'."""
    f = mixed
    submit(f)
    assert answer_turn(f, 'supervisor', decision(['worker-1', 'worker-2']))['status'] == 'committed'
    f.pump()
    report = 'Done:\n- wrote {0}/part.md\nEvidence:\n- {0}/part.md\nNot done: none\nBlocked: none'
    assert answer_turn(f, 'worker-1', report.format('worker-1'))['status'] == 'committed'
    f.pump()
    assert '[worker-1 report]' in codex_queue.record(f.agent('supervisor'))['cq_text']
    assert answer_turn(f, 'worker-2', report.format('worker-2'))['status'] == 'committed'
    f.pump()
    assert f.inbox_depth('supervisor') == 1  # worker-2's report waits behind the supervisor's current turn
    assert all(f.agent(w)['state'] == 'idle' for w in WORKERS)
    wait = json.dumps({'status': 'RUNNING', 'completion': 50, 'reason': "waiting for worker-2's report", 'dispatch': []})
    assert answer_turn(f, 'supervisor', wait)['status'] == 'committed'
    events = [e for _, e in f.r.xrange(f.v2.bus.audit_key)]
    assert not [e for e in events if e.get('event') == 'candidate.invalid'], 'the wait decision was rejected'
    assert int(f.r.hget(fleet.TASK, 'bad_replies') or 0) == 0
    f.pump()
    assert '[worker-2 report]' in codex_queue.record(f.agent('supervisor'))['cq_text']  # delivered next, once
