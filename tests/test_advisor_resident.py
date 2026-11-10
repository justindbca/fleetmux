"""Resident advisor tab (docs/design.md §27, resident advisor): v2 task mode, real private Redis, fake cmux.

No test starts `claude`: the advisor tab's turns are fired through the real Stop-hook entry point
(test_v2_codex_claude.answer_turn), exactly like a Claude worker's."""
import json
import sys
import time
import uuid

import pytest

from test_advisor import advisor_on
from test_kit import make_kit
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from cmux_fleet import advisor, claude_hook, fleet, runtime_v2

SHEET = {'model': 'opus', 'effort': 'high', 'domains': ['research_web'],
         'reason': '能力表 research_web/web-research：联网取材'}
SECRET_REPORT = 'Done:\n- notes WORKER-SECRET-TEXT\nEvidence:\n- worker-1/notes.md\nNot done: none\nBlocked: none'


def resident(r, tmp_path, monkeypatch, workers, on=True, mode='task', **extra):
    from test_v2_codex_claude import answer_turn
    r.flushdb()
    kit = make_kit(tmp_path / 'kit')
    run = tmp_path / ('run-' + uuid.uuid4().hex)
    (run / 'workspace').mkdir(parents=True)
    sessions = tmp_path / 'codex-home' / 'sessions'
    cfg = dict(run=str(run), redis_socket=r.connection_pool.connection_kwargs['path'], python=sys.executable,
               workers=list(workers), mode=mode, supervisor_cli='codex', worker_cli='claude',
               codex_home=str(tmp_path / 'codex-home'), transport_version=2, allow_development_v2=True,
               stall_timeout=0, v2_lease_ms=300000, max_busy=0, worker_kit=str(kit),
               claude='/bin/claude', token='feedbeef', **extra)
    path = run / 'config.json'
    fleet.save(path, cfg)
    if on:
        advisor_on(run, workers)
    f = fleet.Fleet(path, r, fleet_mgr(), sessions=sessions)
    f.test_sessions, f.test_claude = sessions, tmp_path / 'claude-projects'
    f.test_threads = {s: str(uuid.uuid4()) for s in ['supervisor', 'advisor'] + list(workers)}
    monkeypatch.setattr(fleet, 'trust_claude_dir', lambda cwd: None)
    try:
        f.start_all()
        r.set(f.v2.prefix + ':boot', 'running')
        assert answer_turn(f, 'supervisor', 'READY', startup=True)['status'] == 'ready'
        yield f
    finally:
        f.v2.close()


def fleet_mgr():
    from test_fleet import FakeMgr
    return FakeMgr()


@pytest.fixture
def res(private_redis_v2, tmp_path, monkeypatch):  # noqa: F811
    yield from resident(private_redis_v2, tmp_path, monkeypatch, ['worker-1'])


@pytest.fixture
def res2(private_redis_v2, tmp_path, monkeypatch):  # noqa: F811
    yield from resident(private_redis_v2, tmp_path, monkeypatch, ['worker-1', 'worker-2'])


def dispatch(f, *items):
    from test_v2_codex_claude import answer_turn
    body = {'status': 'RUNNING', 'completion': 5, 'reason': 'go', 'dispatch': [
        {'to': to, 'instruction': text} for to, text in items]}
    assert answer_turn(f, 'supervisor', json.dumps(body))['status'] == 'committed'


def ready_advisor(f, generation=None):
    from test_v2_codex_claude import answer_turn
    assert answer_turn(f, 'advisor', 'READY', startup=True, generation=generation)['status'] == 'ready'


def advisor_answers(f, text):
    from test_v2_codex_claude import answer_turn
    return answer_turn(f, 'advisor', text)


def consultations(f):
    from test_v2_codex_claude import typed
    return [t for t in typed(f, 'advisor') if t.startswith('[Advisor ')]


def logs(f, kind):
    return [r for _, r in f.v2.bus.r.xrange(f.v2.bus.audit_key) if r.get('event') == kind]


def first_generation_through_the_advisor(f, instruction='Research the semis rotation into worker-1/notes.md'):
    """worker-1 held -> dispatch -> advisor READY -> consultation -> sheet -> worker-1 released and READY."""
    from test_v2_codex_claude import answer_turn, submit
    submit(f)
    dispatch(f, ('worker-1', instruction))
    f.pump()
    ready_advisor(f)
    f.pump()  # the queued consultation reaches the advisor tab
    assert advisor_answers(f, json.dumps(SHEET))['status'] == 'committed'
    f.pump()  # worker-1 reads the sheet and is released
    assert f.agent('worker-1')['launch_ready'] == '1'
    assert answer_turn(f, 'worker-1', 'READY', startup=True)['status'] == 'ready'
    f.pump()


# ------------------------------------------------------------------ 1. explicit tab, fixed argv, first generation waits
def test_advisor_tab_sits_in_the_supervisor_pane_with_fixed_argv(res):
    f = res
    info = f.agent('advisor')
    assert f.names == ['supervisor', 'worker-1', 'advisor'] and f.workers == ['worker-1']
    assert info['role'] == 'advisor' and info['label'] == 'advisor' and info['state'] == 'starting'
    sup = f.agent('supervisor')['pane']
    assert (info['pane'], sup) in f.mgr.tabbed  # a tab of the supervisor's pane, not of the worker column
    assert info['pane'] not in f.mgr.arranged[1]
    spawned = [k for k in f.mgr.spawned if k['metadata']['type'] == 'advisor']
    assert len(spawned) == 1 and spawned[0]['metadata']['cli_type'] == 'claude'
    assert spawned[0]['start_directory'] == str(f.run_dir / 'advisor')
    snapshot = f.run_dir / 'runtime' / 'advisor-g0.json'
    cfg = fleet.load(snapshot)
    argv = fleet.claude_argv(snapshot, cfg, 'advisor', 0, 'READY please')
    assert argv[argv.index('--model') + 1] == 'fable' and argv[argv.index('--effort') + 1] == 'high'
    assert argv[argv.index('--setting-sources') + 1] == '' and '--strict-mcp-config' in argv
    assert '--disable-slash-commands' in argv and argv[argv.index('--tools') + 1] == 'Read'
    assert argv[argv.index('--system-prompt-file') + 1] == str(f.run_dir / 'advisor.md')  # role prompt (r2 P2-b)
    assert '-p' not in argv and '--output-format' not in argv and '--dangerously-skip-permissions' not in argv
    assert argv[argv.index('--append-system-prompt') + 1] == claude_hook.DECLARATION
    settings = json.loads(open(argv[argv.index('--settings') + 1]).read())
    assert settings['autoMemoryEnabled'] is False and 'claude-stop' in json.dumps(settings['hooks']['Stop'])
    assert argv[-2:] == ['--', 'READY please']
    assert advisor.RESIDENT_NOTE.strip() in (f.run_dir / 'advisor.md').read_text()
    # its own runtime is fixed, whatever --worker-cli says (r2 P2-c); never a dispatch target
    assert fleet.agent_cli({'worker_cli': 'pi'}, 'advisor') == 'claude'
    assert runtime_v2.tui_slot({'transport_version': 2, 'worker_cli': 'pi'}, 'advisor')
    assert 'advisor' not in f.dispatch_targets()
    assert f.agent('worker-1')['advice_hold'] == '1'  # the first worker generation is held for its sheet


def test_first_generation_waits_for_the_advisor_ready_then_gets_its_sheet(res):
    from test_v2_codex_claude import submit, typed
    f = res
    submit(f)
    dispatch(f, ('worker-1', 'Research the semis rotation into worker-1/notes.md'))
    f.pump()
    held = f.agent('worker-1')
    assert held['advice_hold'] == '1' and held['launch_ready'] == '0'
    pending = json.loads(held['advice_pending'])
    ticket = f.r.hgetall(fleet.ADVICE + pending['ticket'])
    assert ticket['status'] == 'pending' and ticket['phase'] == 'queued'
    notes = f.r.lrange(fleet.ADVICE_NOTES, 0, -1)
    assert any(n.startswith('[advisor] waiting: worker-1') and '顾问未就绪' in n for n in notes)  # M1, once
    f.setf('worker-1', startup_deadline=time.time() - 1)
    f.v2.tick()
    f.pump()
    assert f.agent('worker-1')['advice_hold'] == '1' and f.task()['status'] == 'running'
    ready_advisor(f)
    f.pump()
    text, = consultations(f)
    assert text.startswith('[Advisor consultation ' + pending['ticket'] + ']')
    payload = json.loads(text.split('\n', 1)[1])
    assert payload['kind'] == 'dispatch' and 'semis rotation' in payload['instruction']
    assert f.agent('advisor')['state'] == 'busy' and f.agent('worker-1')['advice_hold'] == '1'
    to_supervisor = len(typed(f, 'supervisor'))
    assert advisor_answers(f, '好的：\n```json\n' + json.dumps(SHEET) + '\n```')['status'] == 'committed'
    assert f.agent('advisor')['state'] == 'idle'
    f.pump()
    info = f.agent('worker-1')
    assert info['advice_hold'] == '0' and info['launch_ready'] == '1' and info['advice_pending'] == ''
    snap = fleet.load(f.run_dir / 'runtime' / 'worker-1-g0.json')
    assert snap['worker_model'] == 'opus' and snap['worker_effort'] == 'high'
    audit = f.r.hgetall(fleet.ADVICE + pending['ticket'])
    assert audit['status'] == 'ok' and audit['effect'] == 'launch' and json.loads(audit['output'])['model'] == 'opus'
    # the advisor's answer is never a report: nothing reached the supervisor as one, task state untouched
    assert f.task()['status'] == 'running' and not f.r.lrange('fx:replies:advisor', 0, -1)
    assert len(typed(f, 'supervisor')) == to_supervisor


# ------------------------------------------------------------------ 2. sheet validation: one correction, then default
def test_bad_sheet_gets_one_correction_then_the_default_without_blocking(res):
    from test_v2_codex_claude import answer_turn, typed
    f = res
    first_generation_through_the_advisor(f)
    answer_turn(f, 'worker-1', SECRET_REPORT)
    f.pump()
    dispatch(f, ('worker-1', 'Summarize worker-1/notes.md into worker-1/summary.md'))
    f.pump()
    assert consultations(f)[-1].startswith('[Advisor consultation ')
    assert advisor_answers(f, 'I think sonnet is fine.')['status'] == 'committed'  # no JSON at all
    f.pump()
    correction = consultations(f)[-1]
    assert correction.startswith('[Advisor correction ') and 'Summarize worker-1/notes.md' in correction
    assert f.agent('worker-1')['state'] == 'idle' and f.v2.depth('worker-1') == 1  # still queued, not delivered
    assert advisor_answers(f, json.dumps({'model': 'gpt-9', 'domains': [], 'reason': 'x'}))['status'] == 'committed'
    f.pump()
    info = f.agent('worker-1')
    assert info['state'] == 'busy' and info['generation'] == '0'  # default: the running runtime, no respawn
    assert 'Summarize worker-1/notes.md' in typed(f, 'worker-1')[-1]
    audit = f.r.hgetall(fleet.ADVICE + info['advice_ticket'])
    assert audit['status'] == 'unavailable' and len(json.loads(audit['errors'])) == 2
    assert f.task()['status'] == 'running' and not f.paused()  # never counted as blocked
    assert len([t for t in consultations(f) if t.startswith('[Advisor correction ')]) == 1


def test_corrected_sheet_is_applied(res):
    from test_v2_codex_claude import answer_turn
    f = res
    first_generation_through_the_advisor(f)
    answer_turn(f, 'worker-1', SECRET_REPORT)
    f.pump()
    dispatch(f, ('worker-1', 'Screen the option chain of NVDA into worker-1/chain.md'))
    f.pump()
    advisor_answers(f, json.dumps({'model': 'opus', 'effort': 'ultra', 'domains': [], 'reason': 'x'}))
    f.pump()
    assert 'effort must be one of' in consultations(f)[-1]
    advisor_answers(f, json.dumps(dict(SHEET, domains=['research_web', 'us_options'])))
    f.pump()
    info = f.agent('worker-1')
    assert info['state'] == 'busy' and json.loads(info['installed_domains']) == ['research_web', 'us_options']


# ------------------------------------------------------------------ 3. §22 handoff: advisor -> advisor-1.1
def test_advisor_hands_off_with_a_skeleton_of_sheets_only(res, monkeypatch):
    from test_v2_codex_claude import answer_turn, typed
    f = res
    first_generation_through_the_advisor(f)
    answer_turn(f, 'worker-1', SECRET_REPORT)
    f.pump()
    old = f.agent('advisor')['pane']
    real = fleet.Fleet.usage
    monkeypatch.setattr(f, 'usage', lambda name: (700000, 1000000) if name == 'advisor' else real(f, name))
    dispatch(f, ('worker-1', 'Summarize worker-1/notes.md into worker-1/summary.md'))
    f.pump()  # the next delivery to the advisor finds it over the §22 ratio: next generation first
    info = f.agent('advisor')
    assert info['generation'] == '1' and info['label'] == 'advisor-1.1' and info['state'] == 'starting'
    # old tab retired first (never two advisor sessions), successor in the supervisor's pane (astra r1 P0-01)
    assert old in f.mgr.killed and (info['pane'], f.agent('supervisor')['pane']) in f.mgr.tabbed
    assert not logs(f, 'rotate.impossible')
    note = next((f.run_dir / 'handoffs').glob('advisor-g0-*.md')).read_text()
    first_ticket = json.loads(f.r.hget(fleet.ADVICE + f.r.lrange(fleet.ADVICE_RECENT, 0, -1)[0], 'output'))['ticket']
    assert first_ticket in note and '联网取材' in note
    assert 'WORKER-SECRET-TEXT' not in note and 'Evidence' not in note  # no worker conversation (zero memory)
    assert 'advisor' not in f.r.hgetall(fleet.NAMES).values()  # never routable
    monkeypatch.setattr(f, 'usage', lambda name: real(f, name))
    ready_advisor(f, generation=1)
    f.pump()
    assert consultations(f)[-1].startswith('[Advisor consultation ')  # the queued one goes to the successor
    advisor_answers(f, json.dumps(dict(SHEET, domains=['research_web'])))
    f.pump()
    assert f.agent('worker-1')['state'] == 'busy' and 'Summarize' in typed(f, 'worker-1')[-1]


# ------------------------------------------------------------------ 4. faults: default for this one, next generation in place
def test_closed_advisor_tab_falls_back_and_restarts_in_place(res):
    from test_v2_codex_claude import answer_turn, typed
    f = res
    first_generation_through_the_advisor(f)
    answer_turn(f, 'worker-1', SECRET_REPORT)
    f.pump()
    dispatch(f, ('worker-1', 'Summarize worker-1/notes.md into worker-1/summary.md'))
    f.pump()  # the consultation is with the advisor now
    pane = f.agent('advisor')['pane']
    f.mgr.alive.discard(pane)  # the user closed the advisor tab
    f.next_liveness = 0
    f.v2.tick()
    assert f.agent('advisor')['state'] == 'held' and not f.paused()  # never pauses the fleet (§27)
    assert f.agent('worker-1')['state'] == 'idle'  # no other slot held
    f.pump()
    assert f.agent('worker-1')['state'] == 'busy' and 'Summarize' in typed(f, 'worker-1')[-1]  # default, not blocked
    assert any(n.startswith('[advisor unavailable]') for n in f.r.lrange(fleet.ADVICE_NOTES, 0, -1))
    f.setf('advisor', restart_at=0)
    f.advisor_tick()
    info = f.agent('advisor')
    assert info['generation'] == '1' and info['label'] == 'advisor-1.1' and info['state'] == 'starting'
    assert (info['pane'], f.agent('supervisor')['pane']) in f.mgr.tabbed
    assert f.task()['status'] == 'running'
    ready_advisor(f, generation=1)
    assert f.agent('advisor')['state'] == 'idle'


def test_frozen_advisor_is_rotated_by_the_watchdog_without_blocking(res):
    from test_v2_codex_claude import answer_turn
    f = res
    first_generation_through_the_advisor(f)
    answer_turn(f, 'worker-1', SECRET_REPORT)
    f.pump()
    dispatch(f, ('worker-1', 'Summarize worker-1/notes.md into worker-1/summary.md'))
    f.pump()
    old = f.agent('advisor')['pane']
    assert f.v2.watchdog.rotate('advisor', 'no activity', 'screen')
    info = f.agent('advisor')
    assert info['label'] == 'advisor-1.1' and old in f.mgr.killed and f.task()['status'] == 'running'
    f.pump()
    assert f.agent('worker-1')['state'] == 'busy'  # the cancelled consultation fell back at once


def test_startup_timeout_and_startup_failure_restart_instead_of_blocking(res):
    f = res
    f.setf('advisor', startup_deadline=time.time() - 1)
    f.v2.tick()
    assert f.agent('advisor')['state'] == 'dead' and f.task().get('status') != 'blocked' and not f.paused()
    f.setf('advisor', restart_at=0)
    f.advisor_tick()
    assert f.agent('advisor')['label'] == 'advisor-1.1' and f.agent('advisor')['state'] == 'starting'
    from test_v2_codex_claude import answer_turn
    answer_turn(f, 'advisor', 'not ready', startup=True)  # one READY nudge first
    answer_turn(f, 'advisor', 'still not ready', startup=True)
    assert f.agent('advisor')['state'] == 'dead' and f.task().get('status') != 'blocked'


def test_wall_answer_falls_back_and_never_pauses_the_fleet(res):
    from test_v2_codex_claude import answer_turn
    f = res
    first_generation_through_the_advisor(f)
    answer_turn(f, 'worker-1', SECRET_REPORT)
    f.pump()
    dispatch(f, ('worker-1', 'Summarize worker-1/notes.md into worker-1/summary.md'))
    f.pump()
    advisor_answers(f, "You've reached your session limit")
    assert not f.paused() and not f.r.hget(fleet.BRAKE, 'tripped')
    assert f.agent('advisor')['advisor_fault'].startswith('usage wall')
    f.pump()
    assert f.agent('worker-1')['state'] == 'busy'  # no correction for a wall: default at once
    assert not [t for t in consultations(f) if t.startswith('[Advisor correction ')]
    f.setf('advisor', restart_at=0)
    f.advisor_tick()
    assert f.agent('advisor')['label'] == 'advisor-1.1'
    assert f.v2.watchdog and f.wall_gate('advisor', "You've reached your session limit") is not None
    assert not f.paused()  # a wall on the advisor's screen is not the brake's either


def test_unanswered_first_consultation_releases_the_default_after_the_limit(res):
    f = res
    from test_v2_codex_claude import submit
    submit(f)
    dispatch(f, ('worker-1', 'Research the semis rotation into worker-1/notes.md'))
    f.pump()
    ticket = json.loads(f.agent('worker-1')['advice_pending'])['ticket']
    f.r.hset(fleet.ADVICE + ticket, mapping={'deadline': time.time() - 1})  # 180 s passed, advisor never READY
    f.pump()
    info = f.agent('worker-1')
    assert info['advice_hold'] == '0' and info['launch_ready'] == '1' and info['installed_domains'] == ''
    assert 'worker_model' not in fleet.load(f.run_dir / 'runtime' / 'worker-1-g0.json') or \
        fleet.load(f.run_dir / 'runtime' / 'worker-1-g0.json')['worker_model'] != 'opus'
    assert logs(f, 'advice.released_default')
    assert any(n.startswith('[advisor unavailable]') for n in f.r.lrange(fleet.ADVICE_NOTES, 0, -1))
    assert f.r.hget(f.v2.bus.msg_key(f.r.hget(fleet.ADVICE + ticket, 'mid')), 'outcome') == 'cancelled'
    assert f.v2.depth('advisor') == 0  # the stale consultation never reaches the advisor


# ------------------------------------------------------------------ 5. serial consultations in dispatch order
def test_simultaneous_dispatches_are_advised_one_by_one_in_order(res2):
    from test_v2_codex_claude import submit
    f = res2
    ready_advisor(f)
    submit(f)
    dispatch(f, ('worker-1', 'Research the semis rotation into worker-1/notes.md'),
             ('worker-2', 'Summarize the advisor plan into worker-2/sum.md'))
    f.pump()
    first, = consultations(f)
    assert 'semis rotation' in first and f.v2.depth('advisor') == 1  # the second waits in the advisor's queue
    assert any('前面还有 1 条会诊' in n for n in f.r.lrange(fleet.ADVICE_NOTES, 0, -1))
    advisor_answers(f, json.dumps(SHEET))
    f.pump()
    assert f.agent('worker-1')['launch_ready'] == '1' and f.agent('worker-2')['launch_ready'] == '0'
    f.pump()
    second = consultations(f)[-1]
    assert 'advisor plan' in second
    advisor_answers(f, json.dumps({'model': 'sonnet', 'effort': 'medium', 'domains': [], 'reason': '能力表：sonnet=enough'}))
    f.pump()
    assert f.agent('worker-2')['launch_ready'] == '1'
    assert fleet.load(f.run_dir / 'runtime' / 'worker-2-g0.json')['worker_model'] == 'sonnet'


# ------------------------------------------------------------------ switch off mid-run; rescue; mode boundaries
def test_advisor_off_falls_back_pending_work_and_retires_the_tab_when_idle(res2):
    from test_v2_codex_claude import submit
    f = res2
    ready_advisor(f)
    submit(f)
    dispatch(f, ('worker-1', 'Research the semis rotation into worker-1/notes.md'),
             ('worker-2', 'Summarize the advisor plan into worker-2/sum.md'))
    f.pump()  # worker-1's consultation is with the advisor, worker-2's queued behind it
    t2 = json.loads(f.agent('worker-2')['advice_pending'])['ticket']
    assert f.set_advisor(False, 'user') is None
    for w in ('worker-1', 'worker-2'):
        info = f.agent(w)
        assert info['advice_hold'] == '0' and info['launch_ready'] == '1' and info['advice_pending'] == ''
    assert f.r.hget(fleet.ADVICE + t2, 'status') == 'unavailable' and f.v2.depth('advisor') == 0
    f.advisor_tick()
    assert f.advisor_slot and f.agent('advisor')['state'] == 'busy'  # drains its current consultation first
    advisor_answers(f, json.dumps(SHEET))  # late: nobody waits for it any more
    assert logs(f, 'advice.late')
    f.advisor_tick()
    assert not f.advisor_slot and 'advisor' not in f.names and f.agent('advisor')['state'] == 'retired'
    assert f.mgr.get_pane_status(f.agent('advisor').get('pane') or 'none') is None
    assert f.set_advisor(True, 'user') is None
    f.advisor_tick()
    info = f.agent('advisor')
    assert f.advisor_slot and info['generation'] == '1' and info['state'] == 'starting'


def test_rescue_consult_goes_through_the_advisor_tab(res):
    from test_v2_codex_claude import answer_turn, typed
    f = res
    first_generation_through_the_advisor(f, 'M0: research the semis rotation into worker-1/notes.md')
    answer_turn(f, 'worker-1', SECRET_REPORT)
    f.pump()
    task = f.task()['id']
    f.r.set(fleet.ITEMS, json.dumps({'M0': {'to': 'worker-1', 'instruction': 'research the semis rotation'}}))
    body = {'status': 'RUNNING', 'completion': 30, 'reason': 'consult', 'dispatch': [],
            'consult': [{'worker': 'worker-1', 'task': 'M0', 'finding': 'Reddit 403', 'want': 'a browser'}]}
    answer_turn(f, 'supervisor', json.dumps(body))
    f.pump()
    pending = json.loads(f.r.get(fleet.RESCUE_PENDING))
    assert pending['req']['run_task'] == task
    f.pump()
    text = consultations(f)[-1]
    assert json.loads(text.split('\n', 1)[1])['kind'] == 'rescue' and 'Reddit 403' in text
    assert 'WORKER-SECRET-TEXT' not in text  # supervisor material only
    advisor_answers(f, json.dumps({'action': 'give_up', 'reason': 'finding 是登录墙，需要人'}))
    f.pump()
    assert f.r.get(fleet.RESCUE_PENDING) is None
    assert any('[advisor] give_up M0' in t for t in typed(f, 'supervisor'))


def test_one_shot_runs_have_no_advisor_tab(res):
    f = res
    assert f.advisor_resident()
    f.service = True
    assert f.advisor_resident()  # a service main fleet has the tab too (tests/test_advisor_resident_service.py)
    f.service = False
    f.advisor_runner = lambda payload: {}
    assert not f.advisor_resident()  # an injected one-shot runner answers synchronously instead


def test_advisor_off_v2_run_is_unchanged(private_redis_v2, tmp_path, monkeypatch):  # noqa: F811
    from test_v2_codex_claude import answer_turn, submit, typed
    gen = resident(private_redis_v2, tmp_path, monkeypatch, ['worker-1'], on=False)
    f = next(gen)
    try:
        assert f.names == ['supervisor', 'worker-1'] and not f.advisor_slot
        assert not [k for k in f.mgr.spawned if k['metadata']['type'] == 'advisor']
        f.advisor_tick()
        assert not f.agent('advisor')
        answer_turn(f, 'worker-1', 'READY', startup=True)
        submit(f)
        dispatch(f, ('worker-1', 'Research the semis rotation into worker-1/notes.md'))
        f.pump()
        assert f.agent('worker-1')['state'] == 'busy' and 'semis' in typed(f, 'worker-1')[-1]
        assert not f.r.keys(fleet.ADVICE + '*') and not f.r.lrange(fleet.ADVICE_NOTES, 0, -1)
    finally:
        gen.close()


def test_roster_rejects_a_worker_named_advisor():
    from cmux_fleet import roster
    with pytest.raises(ValueError, match='not be "supervisor" or "advisor"'):
        roster.parse(b'workers:\n  - name: advisor\n', None)


def test_resume_respawns_the_advisor_tab_in_its_place(res):
    f = res
    ready_advisor(f)
    old = f.agent('advisor')['pane']
    f.v2.close()
    f.mgr.has_cli_process = lambda pane, clis: False
    f.mgr.dock_control = lambda pane: None
    again = fleet.Fleet(f.path, f.r, f.mgr, sessions=f.test_sessions)
    try:
        assert again.advisor_slot and again.names == ['supervisor', 'worker-1', 'advisor']
        again.resume_requested = True
        again.v2.resume_run()
        info = again.agent('advisor')
        assert info['label'] == 'advisor-1.1' and info['state'] == 'starting' and info['launch_ready'] == '1'
        # one advisor fault branch for resume too (review astra r1 P0-01): old tab retired, new one by the supervisor
        assert old in f.mgr.killed and (info['pane'], again.agent('supervisor')['pane']) in f.mgr.tabbed
        prompt = (again.run_dir / 'prompts' / 'advisor-g1.md').read_text()
        assert 'AGENTS.md' not in prompt and 'READY' in prompt
        assert 'advisor-1.1' not in again.r.hgetall(fleet.NAMES)
    finally:
        again.v2.close()



# ------------------------------------------------------------------ review astra r1: P0-01 / P2-02 / P2-01
def over_threshold_handoff(f, monkeypatch):
    """worker-1 ran one sheet; the advisor is over its §22 ratio; worker-1's next dispatch is queued."""
    from test_v2_codex_claude import answer_turn
    first_generation_through_the_advisor(f)
    answer_turn(f, 'worker-1', SECRET_REPORT)
    f.pump()
    real = fleet.Fleet.usage
    monkeypatch.setattr(f, 'usage', lambda name: (700000, 1000000) if name == 'advisor' else real(f, name))
    dispatch(f, ('worker-1', 'Summarize worker-1/notes.md into worker-1/summary.md'))


def no_fleet_damage(f):
    assert f.task()['status'] == 'running' and not f.paused()
    assert all(f.agent(w)['state'] != 'held' for w in f.workers)


def test_handoff_spawn_failure_is_an_advisor_fault_not_a_daemon_exit(res, monkeypatch):
    from types import SimpleNamespace
    from test_v2_codex_claude import typed
    f = res
    over_threshold_handoff(f, monkeypatch)
    real = f.mgr.spawn_interactive_cli

    def spawn(**kw):
        if kw['metadata']['type'] == 'advisor':
            return SimpleNamespace(success=False, pane_id=None, error='cmux spawn failed')
        return real(**kw)
    monkeypatch.setattr(f.mgr, 'spawn_interactive_cli', spawn)
    old = f.agent('advisor')['pane']
    f.pump()  # worker-1's consultation is queued, then the advisor's handoff fails: nothing raises
    info = f.agent('advisor')
    assert info['state'] == 'dead' and info['spawn_failed'] == '1' and old in f.mgr.killed
    assert 'cmux spawn failed' in info['advisor_fault'] and f.v2.depth('advisor') == 0
    no_fleet_damage(f)
    f.pump()  # the withdrawn consultation falls back: worker-1 gets its dispatch on its running runtime
    assert f.agent('worker-1')['state'] == 'busy' and 'Summarize' in typed(f, 'worker-1')[-1]
    assert any(n.startswith('[advisor unavailable]') for n in f.r.lrange(fleet.ADVICE_NOTES, 0, -1))
    no_fleet_damage(f)
    f.advisor_tick()
    assert f.agent('advisor')['state'] == 'dead'  # not before the 60 s gap
    monkeypatch.setattr(f.mgr, 'spawn_interactive_cli', real)
    f.setf('advisor', restart_at=0)
    f.advisor_tick()
    info = f.agent('advisor')
    assert info['state'] == 'starting' and info['spawn_failed'] == '0' and info['label'] == 'advisor-1.2'


def test_handoff_retire_and_placement_failures_never_block(res, monkeypatch):
    from test_v2_codex_claude import typed
    f = res
    over_threshold_handoff(f, monkeypatch)
    old = f.agent('advisor')['pane']
    monkeypatch.setattr(f.mgr, 'kill_pane', lambda pane: False)  # the old advisor tab will not close
    f.pump()
    info = f.agent('advisor')
    assert info['state'] == 'held' and info['pane'] == old and info['generation'] == '0'  # never two sessions
    assert 'did not close' in info['advisor_fault'] and f.v2.depth('advisor') == 0
    no_fleet_damage(f)
    f.pump()
    assert f.agent('worker-1')['state'] == 'busy' and 'Summarize' in typed(f, 'worker-1')[-1]
    monkeypatch.undo()  # the tab closes now; placing the new one fails
    monkeypatch.setattr(f.mgr, 'tab_into', lambda pane, peer, before=True: False)
    f.setf('advisor', restart_at=0)
    f.advisor_tick()
    info = f.agent('advisor')
    assert info['state'] == 'starting' and info['label'] == 'advisor-1.1' and old in f.mgr.killed
    assert logs(f, 'advisor.placement_failed')
    no_fleet_damage(f)


def test_rescue_correction_carries_the_current_idle_slots(res2):
    from test_v2_codex_claude import answer_turn, submit
    f = res2
    ready_advisor(f)
    submit(f)
    dispatch(f, ('worker-1', 'M0: research the semis rotation into worker-1/notes.md'))
    f.pump()
    advisor_answers(f, json.dumps(SHEET))
    f.pump()
    answer_turn(f, 'worker-1', 'READY', startup=True)
    f.pump()
    answer_turn(f, 'worker-1', SECRET_REPORT)
    f.pump()
    f.r.set(fleet.ITEMS, json.dumps({'M0': {'to': 'worker-1', 'instruction': 'research the semis rotation'}}))
    answer_turn(f, 'supervisor', json.dumps({'status': 'RUNNING', 'completion': 30, 'reason': 'consult', 'dispatch': [],
        'consult': [{'worker': 'worker-1', 'task': 'M0', 'finding': 'Reddit 403', 'want': 'a browser'}]}))
    f.pump()
    f.pump()
    asked = json.loads(consultations(f)[-1].split('\n', 1)[1])
    assert [c['name'] for c in asked['reassign_candidates']] == ['worker-2']
    f.setf('worker-2', state='busy')  # took other work while the advisor thought
    advisor_answers(f, json.dumps({'action': 'reassign', 'to': 'worker-2', 'model': 'sonnet', 'effort': 'high',
                                   'domains': [], 'reason': '能力表：sonnet=enough'}))
    f.pump()
    correction = consultations(f)[-1]
    assert correction.startswith('[Advisor correction ')
    again = json.loads(correction.split('\n', 2)[2])
    assert again['reassign_candidates'] == [] and 'reassign' not in again['actions']
    no_fleet_damage(f)
