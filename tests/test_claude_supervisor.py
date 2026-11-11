"""Transport v2 with an official Claude Code supervisor (route 2.0b, design.md §5 / §11 / §19).

Real private Redis + the real hook entry points (Claude Stop, Claude UserPromptSubmit, CLI
`claude-prompt` verb); fake cmux and fake CLIs. Workers are official Claude TUIs or Pi bridge
slots driven through bridge events. This proves the daemon/hook/bus wiring, not a real TUI.
"""
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import sys
import threading
import time
import uuid

import pytest
import yaml

import hook_sim
from test_fleet import FakeMgr, traffic
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from fleetmux import cli_pairs, cmux_manager, fleet, pi_adapter, runtime_v2
from fleetmux import fleet_config

WORKERS = ['worker-1', 'worker-2']


# ------------------------------------------------------------------ gate
@pytest.mark.parametrize('worker,mode', [('pi', 'task'), ('pi', 'service'), ('claude', 'task'), ('claude', 'service')])
def test_claude_supervisor_pairs_run_only_under_smoke(worker, mode):
    cfg = dict(transport_version=2, supervisor_cli='claude', worker_cli=worker, mode=mode,
               allow_development_v2=True)
    # Real smokes passed (cli_pairs.yaml verified_by): the pairs run without smoke now.
    row = cli_pairs.load_pairs().lookup('claude', worker, mode)
    assert row['status'] == 'verified' and row['adapter_missing'] == [] and 'smoke_pair.sh' in row['verified_by']
    assert runtime_v2.validate_config(cfg) == 2
    assert runtime_v2.validate_config(dict(cfg, lifecycle='smoke')) == 2
    with pytest.raises(ValueError, match='allow-development-v2'):  # smoke never relaxes the dev opt-in
        runtime_v2.validate_config(dict(cfg, lifecycle='smoke', allow_development_v2=False))


@pytest.mark.parametrize('worker,mode', [('codex', 'task'), ('codex', 'service')])   # claude/service verified 2026-11-03
def test_other_claude_supervisor_pairs_still_refused_even_for_smoke(worker, mode):
    assert runtime_v2.v2_cli_problem(dict(supervisor_cli='claude', worker_cli=worker, mode=mode, lifecycle='smoke'))


def test_start_accepts_claude_supervisor_cli_and_model(monkeypatch):
    seen = {}
    monkeypatch.setattr(fleet, 'start', lambda args: seen.update(vars(args)))
    fleet.main(['start', '--supervisor-cli', 'claude', '--supervisor-model', 'claude-fable-5-1',
                '--worker-cli', 'pi', '--allow-development-v2'])
    assert seen['supervisor_cli'] == 'claude' and seen['supervisor_model'] == 'claude-fable-5-1'
    fleet.main(['start', '--supervisor-cli', 'claude'])
    # §52: an unset flag stays None in the parser; start fills it (discovered default, else DEFAULTS --
    # tests/test_discover.py::test_without_a_discovered_pair_start_is_unchanged)
    assert seen['supervisor_model'] is None


def test_start_refuses_unverified_claude_supervisor_before_provisioning(monkeypatch, tmp_path):
    table = fleet_config.block('cli_pairs')
    for item in table['pairs']:
        if (item['supervisor_cli'], item['worker_cli']) == ('claude', 'pi'):
            item.update(status='unverified', verified_by=None)
    path = tmp_path / 'cli_pairs.yaml'
    path.write_text(yaml.safe_dump(table))
    monkeypatch.setattr(cli_pairs, 'DEFAULT_PATH', path)
    monkeypatch.setattr(cmux_manager, 'cmux_available',
                        lambda: pytest.fail('transport rejection must precede cmux/Redis provisioning'))
    with pytest.raises(SystemExit, match='lifecycle smoke'):
        fleet.main(['start', '--lifecycle', 'dev_task', '--supervisor-cli', 'claude', '--worker-cli', 'pi',
                    '--allow-development-v2'])


# ------------------------------------------------------------------ launch
def hooks_of(argv):
    return json.loads(Path(argv[argv.index('--settings') + 1]).read_text())['hooks']


def test_claude_argv_supervisor_model_and_prompt_hook_only_on_supervisor(tmp_path):
    run = tmp_path / 'run'
    cfg = dict(run=str(run), python=sys.executable, workers=WORKERS, supervisor_cli='claude', worker_cli='claude',
               supervisor_model='claude-fable-5-1', worker_model='sonnet', transport_version=2,
               claude_supervisor_hook_delivery=True)
    for name in ['supervisor'] + WORKERS:
        fleet.agent_cwd(cfg, name).mkdir(parents=True)
    sup = fleet.claude_argv(run / 'config.json', cfg, 'supervisor', 3, 'hi')
    assert sup[sup.index('--model') + 1] == 'claude-fable-5-1' and '--name' not in sup
    hooks = hooks_of(sup)
    prompt_hook = hooks['UserPromptSubmit'][0]['hooks'][0]
    assert 'claude-prompt' in prompt_hook['command'] and prompt_hook['command'].endswith('supervisor 3')
    assert prompt_hook['timeout'] == fleet.PROMPT_HOOK_TIMEOUT > fleet.PROMPT_WAIT
    assert 'claude-stop' in hooks['Stop'][0]['hooks'][0]['command']
    worker = fleet.claude_argv(run / 'config.json', cfg, 'worker-1', 0, 'hi')
    # Claude workers are on the Stop-hook channel: waiting Stop hook + asyncRewake fallback + StopFailure,
    # plus the mid-turn handoff PostToolUse hook (design §34); the UserPromptSubmit hook stays supervisor-only.
    assert 'claude-posttool' in hooks['PostToolUse'][0]['hooks'][0]['command']
    assert worker[worker.index('--model') + 1] == 'sonnet' and set(hooks_of(worker)) == {'Stop', 'StopFailure', 'PostToolUse'}
    assert [bool(h.get('asyncRewake')) for h in hooks_of(worker)['Stop'][0]['hooks']] == [False, True]
    assert '--append-system-prompt' in worker and '--append-system-prompt' in sup
    assert 'You remain the supervisor' in sup[sup.index('--append-system-prompt') + 1]
    # A §19 adopted pane keeps the parent's hooks; v1 runs have no operator queue.
    assert not fleet.prompt_hook_slot(dict(cfg, adopt={'pane': 'p'}), 'supervisor', 0)
    assert not fleet.prompt_hook_slot(dict(cfg, transport_version=1), 'supervisor', 0)
    assert not fleet.prompt_hook_slot(dict(cfg, supervisor_cli='pi'), 'supervisor', 0)


# ------------------------------------------------------------------ live wiring
def pi_session(f, slot):
    gen = int(f.agent(slot)['generation'])
    transcript = pi_adapter.session_dir(f.cfg, slot, gen) / 'session.jsonl'
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.touch()
    return dict(agent=slot, generation=gen, run_id=f.v2.bus.run_id,
                thread='session-' + slot + '-g' + str(gen), transcript=str(transcript))


def event(f, ev):
    identity = 'e-' + uuid.uuid4().hex
    runtime_v2.post_event(f.v2.bus, ev, identity)
    assert f.v2.step(10)


def make_fleet(r, tmp_path, worker_cli, mode):
    r.flushdb()
    run = tmp_path / ('run-' + uuid.uuid4().hex)
    (run / 'workspace').mkdir(parents=True)
    cfg = dict(run=str(run), redis_socket=r.connection_pool.connection_kwargs['path'], python=sys.executable,
               workers=list(WORKERS), mode=mode, supervisor_cli='claude', worker_cli=worker_cli,
               supervisor_model='claude-fable-5-1', transport_version=2, allow_development_v2=True,
               claude_supervisor_hook_delivery=True,
               lifecycle='smoke', stall_timeout=0, v2_lease_ms=300000, max_busy=0)
    if worker_cli == 'pi':
        cfg.update(pi='pi', pi_model='local/test', pi_home=str(run / 'pi-home'), allow_unsandboxed_pi=True)
    path = run / 'config.json'
    fleet.save(path, cfg)
    f = fleet.Fleet(path, r, FakeMgr())
    f.test_claude = tmp_path / ('claude-projects-' + uuid.uuid4().hex)
    f.test_threads = {'supervisor': str(uuid.uuid4())}
    f.test_threads.update({w: str(uuid.uuid4()) for w in WORKERS})
    return f


@pytest.fixture
def no_trust(monkeypatch):
    monkeypatch.setattr(fleet, 'trust_claude_dir', lambda cwd: None)  # never touch ~/.claude.json


def boot(f):
    f.start_all()
    f.r.set(f.v2.prefix + ':boot', 'running')
    for slot in f.names:
        if fleet.is_pi(f.cfg, slot):
            meta = pi_session(f, slot)
            event(f, dict(meta, kind='bridge_session', request_id='register-' + slot))
            f.v2.bus.register_slot(slot, 0, meta['thread'])
            event(f, dict(meta, kind='startup', request_id='ready-' + slot, text='READY', failed=False, aborted=False))
            event(f, dict(meta, kind='availability', idle=True))
        else:
            assert stop_hook(f, slot, 'READY', prompt=spawn_prompt(f, slot))['status'] == 'ready'
    return f


@pytest.fixture(params=['claude', 'pi'])
def task_fleet(request, private_redis_v2, tmp_path, no_trust):  # noqa: F811
    f = boot(make_fleet(private_redis_v2, tmp_path, request.param, 'task'))
    yield f
    f.v2.close()


@pytest.fixture
def service_fleet(private_redis_v2, tmp_path, no_trust):  # noqa: F811
    f = boot(make_fleet(private_redis_v2, tmp_path, 'pi', 'service'))
    yield f
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
    """What the slot's current generation was given: typed into its pane, or handed over by its Stop hook."""
    pane = f.agent(slot)['pane']
    return [text for p, text in f.mgr.sent if p == pane] + hook_sim.handed_to(f, slot)


def spawn_prompt(f, slot):
    gen = f.agent(slot)['generation']
    return (f.run_dir / 'prompts' / (slot + '-g' + gen + '.md')).read_text()


def stop_hook(f, slot, text, *, prompt=None, generation=None):
    """Fire the real Claude Stop hook of this slot for a finished turn."""
    gen = int(f.agent(slot)['generation']) if generation is None else generation
    prompt = prompt if prompt is not None else typed(f, slot)[-1]
    thread = f.test_threads[slot]
    transcript = f.test_claude / slot / (thread + '.jsonl')
    transcript.parent.mkdir(parents=True, exist_ok=True)
    rows = [hook_sim.user_row(prompt, hook_sim.was_handed(slot, prompt)),
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}}]
    transcript.write_text('\n'.join(json.dumps(x) for x in rows) + '\n')
    raw = json.dumps({'session_id': thread, 'transcript_path': str(transcript), 'last_assistant_message': text})
    return drive(f, lambda: hook_sim.claude_stop(f.path, slot, str(gen), raw))


def prompt_hook(f, text, *, generation=None, cli=False, wait=5, step=True):
    """Fire the real UserPromptSubmit hook of the supervisor; returns (block reason, stdout)."""
    gen = int(f.agent('supervisor')['generation']) if generation is None else generation
    raw = json.dumps({'session_id': f.test_threads['supervisor'], 'prompt': text,
                      'hook_event_name': 'UserPromptSubmit'})
    if cli:
        out = io.StringIO()
        stdin = sys.stdin
        sys.stdin = io.StringIO(raw)
        try:
            with redirect_stdout(out):
                run = lambda: fleet.main(['claude-prompt', str(f.path), 'supervisor', str(gen)])
                drive(f, run) if step else run()
        finally:
            sys.stdin = stdin
        printed = out.getvalue()
        return (json.loads(printed)['reason'] if printed else None), printed
    call = lambda: fleet.claude_prompt(f.path, 'supervisor', str(gen), raw, wait=wait)
    return (drive(f, call) if step else call()), None


def decision(workers, status='RUNNING', **extra):
    return json.dumps(dict({'status': status, 'completion': 10, 'reason': 'split the work',
                            'dispatch': [{'to': w, 'instruction': 'Write ' + w + '/part.md with the findings.'}
                                         for w in workers]}, **extra))


def submit(f, goal='build the M0 parts'):
    runtime_v2.post_event(f.v2.bus, dict(kind='task', request_id='new-task', spec={'goal': goal}), 'new-task')
    assert f.v2.step(10)
    f.pump()


def no_message_files(f):
    return not list(f.run_dir.rglob('.inbox')) and not list(f.run_dir.rglob('msg-*.md'))


def report_back(f, slot):
    """worker-1 finishes its dispatched part through its own CLI's settlement path."""
    out = f.workspace() / slot / 'part.md'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text('findings')
    if fleet.is_pi(f.cfg, slot):
        env = f.v2.bus.receive(slot, 'bridge-' + slot + '-g0', generation=0, session_id=f.agent(slot)['thread'])
        value = {'done': ['wrote part.md'], 'evidence': [slot + '/part.md 8 bytes'], 'not_done': '', 'blocked': ''}
        event(f, dict(pi_session(f, slot), kind='settled', delivery=env, failed=False, aborted=False,
                      unfinished=False, candidates=[{'tool': 'fleet_result', 'value': value}]))
        event(f, dict(pi_session(f, slot), kind='availability', idle=True))
    else:
        report = 'Done:\n- wrote part.md\nEvidence:\n- ' + slot + '/part.md 8 bytes\nNot done: none\nBlocked: none'
        assert stop_hook(f, slot, report)['status'] == 'committed'
    f.pump()


def test_ready_gate_binds_claude_supervisor_session(task_fleet):
    f = task_fleet
    info = f.agent('supervisor')
    assert info['state'] == 'idle' and info['runtime_ready'] == '1'
    assert info['transcript'].endswith('.jsonl')
    assert f.v2.bus.slot_state('supervisor')['session_id'] == f.test_threads['supervisor']
    role = (fleet.agent_cwd(f.cfg, 'supervisor') / 'CLAUDE.md').read_text()
    assert 'fleet_decision' not in role  # the original AGENTS.md decision JSON format


def test_non_ready_startup_blocks_the_task(private_redis_v2, tmp_path, no_trust):  # noqa: F811
    f = make_fleet(private_redis_v2, tmp_path, 'claude', 'task')
    f.start_all()
    f.r.set(f.v2.prefix + ':boot', 'running')
    try:
        assert stop_hook(f, 'supervisor', 'Hello! How can I help?', prompt=spawn_prompt(f, 'supervisor'))['status'] == 'rejected'
        assert f.agent('supervisor')['state'] == 'starting'  # one correction first (acceptance 10/13 P1-3)
        assert stop_hook(f, 'supervisor', 'Hello again', prompt=spawn_prompt(f, 'supervisor'))['status'] == 'rejected'
        assert f.agent('supervisor')['state'] == 'dead'
    finally:
        f.v2.close()


def test_decision_json_via_stop_hook_dispatches_and_completes(task_fleet):
    f = task_fleet
    submit(f)
    body, = typed(f, 'supervisor')
    lease = f.v2.bus.slot_state('supervisor')
    assert lease['status'] == 'received' and lease['consumer'] == 'tui-supervisor-g0'
    assert 'build the M0 parts' in body and 'Reply with the JSON format from AGENTS.md.' in body
    assert 'fleet_decision' not in body and lease['message_id'] not in body
    assert not [text for pane, text in f.mgr.sent if pane == f.agent('supervisor')['pane']]
    assert hook_sim.handed_to(f, 'supervisor') == [body]
    assert stop_hook(f, 'supervisor', 'Plan below.\n' + decision(['worker-1']))['status'] == 'committed'
    f.pump()
    assert f.agent('worker-1')['state'] == 'busy'
    report_back(f, 'worker-1')
    followup = typed(f, 'supervisor')[-1]
    assert followup.startswith('[worker-1 report]') and 'wrote part.md' in followup
    done = decision([], status='COMPLETED', completion=100, artifacts=[{'path': 'worker-1/part.md', 'contains': ['findings']}])
    assert stop_hook(f, 'supervisor', done)['status'] == 'committed'
    assert f.task()['status'] == 'completed'
    assert no_message_files(f)


def test_three_invalid_decisions_get_two_corrections_then_block(task_fleet):
    f = task_fleet
    submit(f)
    for attempt in (1, 2):
        stop_hook(f, 'supervisor', 'I will ask worker-1 to do it.')
        assert f.task()['status'] == 'running'
        f.pump()
        assert 'not a valid decision JSON' in typed(f, 'supervisor')[-1]
    stop_hook(f, 'supervisor', 'Still prose.')
    assert f.task()['status'] == 'blocked' and 'three times' in f.task()['reason']


def test_valid_decision_resets_correction_count(task_fleet):
    f = task_fleet
    submit(f)
    stop_hook(f, 'supervisor', 'prose')
    f.pump()
    assert stop_hook(f, 'supervisor', decision(['worker-1']))['status'] == 'committed'
    assert f.r.hget(fleet.TASK, 'bad_replies') == '0'


def test_service_invalid_decisions_pause_after_three(service_fleet):
    f = service_fleet
    event(f, dict(kind='request', request_id='cmd-r1', spec={'id': 'r1', 'payload': {'message': 'hi'},
                                                             'reply': {'required': ['reply']}}))
    f.pump()
    for _ in range(3):
        stop_hook(f, 'supervisor', 'no json here')
        f.pump()
    assert f.paused()


def test_service_result_card_rendered_off_model_never_typed(service_fleet):
    """A Claude supervisor has no UI-only session entry: its cards go to cards/supervisor.log (A2 tui_cards)."""
    f = service_fleet
    spec = dict(id='r1', payload={'message': 'say OK'}, reply={'required': ['reply'], 'allowed': ['reply']})
    event(f, dict(kind='request', request_id='cmd-r1', spec=spec))
    f.pump()
    assert 'Reply with the service decision JSON from AGENTS.md.' in typed(f, 'supervisor')[-1]
    route = json.dumps({'status': 'RUNNING', 'skip': [],
                        'dispatch': [{'to': 'worker-1', 'request_id': 'r1', 'instruction': 'Reply OK in the reply field.'}]})
    assert stop_hook(f, 'supervisor', route)['status'] == 'committed'
    f.pump()
    env = f.v2.bus.receive('worker-1', 'bridge-worker-1-g0', generation=0, session_id=f.agent('worker-1')['thread'])
    sent = len(traffic(f.mgr))
    event(f, dict(pi_session(f, 'worker-1'), kind='settled', delivery=env, failed=False, aborted=False,
                  unfinished=False, candidates=[{'tool': 'fleet_result', 'value': {'reply': 'OK'}}]))
    assert json.loads(f.r.get(fleet.RESULT + 'r1'))['result'] == {'reply': 'OK'}
    f.v2.render_cards()
    assert 'worker-1：OK' in (f.run_dir / 'cards' / 'supervisor.log').read_text()
    assert 'worker-1：OK' in (f.run_dir / 'history.html').read_text()   # history page "Cards" view
    assert not (f.run_dir / 'cards' / 'worker-1.log').exists()          # Pi shows its own cards
    assert len(traffic(f.mgr)) == sent and not f.v2.depth('supervisor')     # never typed into Claude or queued
    assert not any('worker-1：OK' in text for text in typed(f, 'supervisor'))


# ------------------------------------------------------------------ operator input (design.md §5)
def test_operator_text_without_task_submits_a_task_and_is_blocked_from_the_turn(task_fleet):
    f = task_fleet
    reason, printed = prompt_hook(f, '让 worker-1 写一份 OK 文件', cli=True)
    task_id = f.task()['id']
    assert f.task()['status'] == 'running' and '让 worker-1 写一份 OK 文件' in f.task()['goal']
    assert json.loads(printed) == {'decision': 'block', 'reason': reason}
    assert '已排队' in reason and task_id in reason
    f.pump()
    body = typed(f, 'supervisor')[-1]
    assert '让 worker-1 写一份 OK 文件' in body
    assert body in hook_sim.handed_to(f, 'supervisor')  # Stop feedback bypasses UserPromptSubmit
    assert stop_hook(f, 'supervisor', decision(['worker-1']))['status'] == 'committed'


def test_operator_text_during_task_is_queued_as_operator_instruction(task_fleet):
    f = task_fleet
    submit(f)
    stop_hook(f, 'supervisor', decision(['worker-1']))
    reason, _ = prompt_hook(f, 'also check the README')
    assert '已排队' in reason
    f.pump()
    body = typed(f, 'supervisor')[-1]
    assert body.startswith('[Operator instruction] also check the README')
    assert 'decision JSON' in body and 'fleet_decision' not in body


def test_service_operator_text_becomes_ui_request(service_fleet):
    f = service_fleet
    reason, _ = prompt_hook(f, '让 worker-1 回复 OK')
    rid = reason[reason.index('ui-'):reason.index('）')]
    req = f.r.hgetall(fleet.REQ + rid)
    assert req['operator'] == '1' and json.loads(req['spec'])['payload'] == {'message': '让 worker-1 回复 OK'}
    f.pump()
    assert '让 worker-1 回复 OK' in typed(f, 'supervisor')[-1]


def test_pass_paths_print_nothing(task_fleet):
    f = task_fleet
    assert prompt_hook(f, '/compact', cli=True) == (None, '')
    assert prompt_hook(f, '   ', cli=True) == (None, '')
    submit(f)
    reason, printed = prompt_hook(f, typed(f, 'supervisor')[-1], cli=True)
    assert '已排队' in reason and json.loads(printed)['decision'] == 'block'


def test_image_stale_generation_and_unready_input_are_refused(private_redis_v2, tmp_path, no_trust):  # noqa: F811
    f = make_fleet(private_redis_v2, tmp_path, 'claude', 'task')
    f.start_all()
    f.r.set(f.v2.prefix + ':boot', 'running')
    try:
        # Before READY: the spawn prompt passes, typed operator text does not.
        assert prompt_hook(f, spawn_prompt(f, 'supervisor'))[0] is None
        assert '尚未 READY' in prompt_hook(f, 'start now')[0]
        stop_hook(f, 'supervisor', 'READY', prompt=spawn_prompt(f, 'supervisor'))
        assert '图片' in prompt_hook(f, 'look at [Image #1] please')[0]
        assert '已退役' in prompt_hook(f, 'hello', generation=4)[0]
        assert not f.task()
    finally:
        f.v2.close()


def test_unconfirmed_queue_never_claims_success(task_fleet):
    f = task_fleet
    reason, _ = prompt_hook(f, 'do the thing', wait=0.2, step=False)  # daemon never steps
    assert '未确认' in reason and '已排队' not in reason


def test_operator_input_still_rejects_wrong_slot_and_session(task_fleet):
    f = task_fleet
    base = dict(kind='operator_input', run_id=f.v2.bus.run_id, generation=0, text='x')
    for bad in (dict(agent='worker-1', thread=f.test_threads['worker-1']),
                dict(agent='supervisor', thread='other-session')):
        cid = 'ui-' + uuid.uuid4().hex
        runtime_v2.post_event(f.v2.bus, dict(base, request_id=cid, **bad), cid)
        assert f.v2.step(10)
        assert runtime_v2.wait_reply(f.v2.bus, cid, timeout=1)['status'] == 'rejected'
    assert not f.task()


def test_manual_turn_that_slipped_through_is_not_settled(task_fleet):
    f = task_fleet
    submit(f)
    assert stop_hook(f, 'supervisor', decision(['worker-1']), prompt='just chatting')['status'] == 'ignored'
    assert f.v2.bus.slot_state('supervisor')['status'] == 'received'
