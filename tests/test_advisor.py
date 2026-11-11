"""Advisor pre-dispatch path (advisor plan §3.1 / §8 / §13): pure validation, v1 and v2 daemon wiring.

The advisor process is always a fake runner: no test starts `claude`."""
import json
import sys
import time
from pathlib import Path

import pytest

from test_fleet import FakeMgr, FakeRedis, decision, fx, turn  # noqa: F401
from test_kit import make_kit
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from fleetmux import advisor, control_view, fleet, kit as kitmod, profile_launch, profile_runtime, runtime_v2

MATRIX = {'research_web': {'web-research': {'sonnet': {'verdict': 'enough'}, 'opus': {'verdict': 'overkill'},
                                            'haiku': {'verdict': 'untested'}}},
          'fleet': {'review': {'sonnet': {'verdict': 'not_enough'}, 'haiku': {'verdict': 'untested'}}}}
CARDS = {m: object() for m in ('fable', 'opus', 'sonnet', 'haiku')}


def envelope(**sheet):
    sheet.setdefault('reason', '能力表 research_web/web-research：sonnet=enough')
    return {'type': 'result', 'is_error': False, 'result': '```json\n' + json.dumps(sheet) + '\n```',
            'usage': {'input_tokens': 900, 'output_tokens': 60, 'cache_read_input_tokens': 21000},
            'total_cost_usd': 0.14}


def check(raw, cli='claude', whitelist=None, current=('sonnet', 'medium'), models=('fable', 'opus', 'sonnet', 'haiku')):
    return advisor.validate(raw, cli=cli, models=list(models), current=current,
                            catalog=['research_web', 'us_options', 'fleet'], whitelist=whitelist,
                            matrix=MATRIX, cards=CARDS)


# ------------------------------------------------------------------ pure: sheet validation
@pytest.mark.parametrize('raw, match', [
    ({'model': 'gpt-5', 'domains': [], 'reason': 'x'}, 'model must be one of'),
    ({'model': 'sonnet', 'effort': 'ultra', 'domains': [], 'reason': 'x'}, 'effort must be one of'),
    ({'model': 'sonnet', 'domains': ['nope'], 'reason': 'x'}, 'not in the domain table'),
    ({'model': 'sonnet', 'domains': 'research_web', 'reason': 'x'}, 'list of distinct'),
    ({'model': 'sonnet', 'domains': ['fleet', 'fleet'], 'reason': 'x'}, 'list of distinct'),
    ({'model': 'sonnet', 'domains': []}, 'reason must cite'),
    ({'model': 'sonnet', 'domains': [], 'reason': 'x', 'capability': 'research_web/coding'}, 'not a row'),
    ({'model': 'haiku', 'domains': [], 'reason': 'x', 'capability': 'research_web/web-research'}, 'untested cell'),
    ({'model': 'opus', 'domains': [], 'reason': 'x', 'capability': 'research_web/web-research'}, 'verdict overkill'),
])
def test_sheet_validation_failure_branches(raw, match):
    with pytest.raises(advisor.AdviceError, match=match):
        check(raw)


def test_card_whitelist_limits_domains():
    with pytest.raises(advisor.AdviceError, match='kit.domains'):
        check({'model': 'sonnet', 'domains': ['us_options'], 'reason': 'x'}, whitelist=('research_web',))
    assert check({'model': 'sonnet', 'domains': ['research_web'], 'reason': 'x'}, whitelist=('research_web',))['domains'] == ['research_web']


@pytest.mark.parametrize('cli', ['pi', 'codex'])
def test_pi_and_codex_slots_refuse_domains_and_ignore_effort(cli):
    with pytest.raises(advisor.AdviceError, match='takes no domains'):
        check({'model': 'm', 'domains': ['fleet'], 'reason': 'x'}, cli=cli, models=['m'], current=('m', ''))
    sheet = check({'model': 'm', 'effort': 'high', 'domains': [], 'reason': 'x'}, cli=cli, models=['m'], current=('m', ''))
    assert sheet['effort'] == '' and sheet['effort_ignored'] == 'high'


def test_untested_cell_allowed_only_when_every_tested_cell_is_not_enough():
    sheet = check({'model': 'haiku', 'domains': [], 'reason': 'x', 'capability': 'fleet/review'})
    assert sheet['capability_gap'] is True and sheet['capability'] == 'fleet/review'
    assert 'capability_gap' not in check({'model': 'sonnet', 'domains': [], 'reason': 'x',
                                          'capability': 'research_web/web-research'})


def test_model_change_needs_a_launch_card():
    with pytest.raises(advisor.AdviceError, match='no registry card'):
        advisor.validate({'model': 'opus', 'domains': [], 'reason': 'x'}, cli='claude', models=['opus', 'sonnet'],
                         current=('sonnet', 'medium'), catalog=[], whitelist=None, matrix={}, cards={'sonnet': 1})


def test_decide_fingerprint_hotload_and_fewer_domains():
    cur = ('sonnet', 'medium')
    assert advisor.decide(cur, ('opus', 'medium'), [], []) == 'respawn'
    assert advisor.decide(cur, ('sonnet', 'high'), ['fleet'], ['fleet']) == 'respawn'  # effort is in the fingerprint
    assert advisor.decide(cur, cur, ['fleet'], ['fleet', 'research_web']) == 'hotload'
    assert advisor.decide(cur, cur, ['fleet', 'research_web'], ['fleet']) == 'none'    # fewer domains never act
    assert advisor.decide(cur, cur, [], []) == 'none'


def test_consult_retries_once_then_gives_up():
    calls = []

    def runner(payload):
        calls.append(payload)
        raise TimeoutError('90 s')
    sheet, env, errors = advisor.consult({'x': 1}, runner, lambda raw: raw)
    assert sheet is None and len(calls) == advisor.ATTEMPTS == 2 and len(errors) == 2
    outputs = iter([{'result': 'no json here'}, envelope(model='sonnet', domains=[])])
    sheet, env, errors = advisor.consult({}, lambda p: next(outputs), check)
    assert sheet['model'] == 'sonnet' and len(errors) == 1


def test_advisor_process_flags_and_fixed_cwd(tmp_path, monkeypatch):
    seen = {}

    def run(argv, **kw):
        seen.update(argv=argv, **kw)
        return type('Done', (), {'returncode': 0, 'stdout': json.dumps(envelope(model='sonnet', domains=[])), 'stderr': ''})
    monkeypatch.setattr(advisor.subprocess, 'run', run)
    out = advisor.run_claude({'goal': 'g'}, cfg={'claude': '/bin/claude'}, run_dir=tmp_path)
    argv = seen['argv']
    assert argv[:6] == ['/bin/claude', '-p', '--model', 'fable', '--effort', 'high']
    assert argv[argv.index('--setting-sources') + 1] == ''
    assert '--strict-mcp-config' in argv and '--disable-slash-commands' in argv
    assert json.loads(argv[argv.index('--settings') + 1]) == {'autoMemoryEnabled': False}
    assert argv[argv.index('--output-format') + 1] == 'json' and '--bare' not in argv
    assert seen['cwd'] == str(tmp_path / 'advisor') and seen['timeout'] == 90
    assert (tmp_path / 'advisor.md').read_text() == advisor.PROMPT.read_text()
    assert advisor.usage_fields(out)['total_cost_usd'] == 0.14


def test_launch_extras_are_encoded_only_when_given_and_drift_covers_them():
    from fleetmux.agent_registry import AgentRegistry
    reg = AgentRegistry()
    plain = profile_launch.resolve_launch(reg.get('claude-sonnet'), reg, {})
    assert 'extras' not in plain  # Phase 5 encodings stay byte-identical
    high = profile_launch.resolve_launch(reg.get('claude-sonnet'), reg, {}, extras={'effort': 'high'})
    assert high['extras'] == {'effort': 'high'} and high['overrides']['worker_model'] == 'sonnet'
    frozen = profile_launch.encode_launch(high)
    assert profile_runtime.launch_drift(frozen, high) is None
    assert profile_runtime.launch_drift(frozen, dict(high, extras={'effort': 'low'})) == 'extras'
    assert profile_runtime.launch_drift(frozen, dict(high, role_text='changed')) == 'role_text'


def test_registry_kit_domains_whitelist(tmp_path):
    from fleetmux.agent_registry import AgentRegistry, AgentRegistryError
    card = {'id': 'claude-x', 'display_name': 'X', 'cli': 'claude', 'capabilities': ['planning'],
            'kit': {'domains': ['research_web']}}
    (tmp_path / 'x.json').write_text(json.dumps(card))
    assert AgentRegistry(tmp_path).get('claude-x').kit_domains == ('research_web',)
    assert AgentRegistry().get('claude-sonnet').kit_domains is None  # default: no restriction
    (tmp_path / 'x.json').write_text(json.dumps(dict(card, kit={'domains': 'research_web'})))
    with pytest.raises(AgentRegistryError, match='kit'):
        AgentRegistry(tmp_path)


def test_parse_decision_accepts_text_advise_note_only():
    ok = decision(dispatch=[{'to': 'worker-1', 'instruction': 'Write worker-1/a.md please', 'advise_note': 'web'}])
    assert fleet.parse_decision(ok, ['worker-1'], False)['dispatch'][0]['advise_note'] == 'web'
    bad = decision(dispatch=[{'to': 'worker-1', 'instruction': 'Write worker-1/a.md please', 'advise_note': 3}])
    with pytest.raises(ValueError, match='advise_note'):
        fleet.parse_decision(bad, ['worker-1'], False)


def test_capability_matrix_file_is_well_formed():
    matrix, error = advisor.load_matrix()
    assert error is None and matrix['research_web']['web-research']['sonnet']['verdict'] == 'enough'
    assert matrix['research_web']['web-research']['opus']['verdict'] == 'overkill'
    for domain in matrix.values():
        for row in domain.values():
            assert all(cell['verdict'] in advisor.VERDICTS for cell in row.values())


def test_control_line_shows_advice():
    record = {'event': 'control.state', 'snapshot': {
        'run_id': 'r', 'socket': {'available': True}, 'daemon': {'paused': False, 'heartbeat_at': 1},
        'slots': {'worker-1': {'label': 'worker-1', 'state': 'idle', 'context': {'ratio': None},
                               'queue_depth': 0, 'current_request': None, 'elapsed_s': None, 'deadline_at': None,
                               'advice': 'sonnet/medium/research_web'}}}}
    assert 'adv=sonnet/medium/research_web' in control_view._render(record, 400)


# ------------------------------------------------------------------ v1 daemon (fake Redis / cmux)
def advisor_on(run, workers):
    """The switch lives in the roster (advisor plan §2.1): <run>/roster.yaml with advisor: on."""
    from fleetmux import roster
    roster.write_advisor(Path(run) / 'roster.yaml', True, workers)


class Runner:
    def __init__(self, *sheets):
        self.sheets, self.calls = list(sheets), []

    def __call__(self, payload):
        self.calls.append(payload)
        item = self.sheets.pop(0) if len(self.sheets) > 1 else self.sheets[0]
        if isinstance(item, Exception):
            raise item
        return envelope(**item)


@pytest.fixture
def afx(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet, 'CLAUDE_JSON', tmp_path / 'claude.json')
    kit = make_kit(tmp_path / 'kit')
    run = tmp_path / 'run'
    (run / 'workspace').mkdir(parents=True)
    cfg = {'run': str(run), 'redis_socket': '/tmp/cmux-fleet-test.sock', 'python': sys.executable,
           'workers': ['worker-1', 'worker-2'], 'handoff_ratio': .6, 'handoff_timeout': 900, 'window': 95000,
           'codex_home': str(tmp_path / 'codex-home'), 'worker_cli': 'claude', 'worker_kit': str(kit),
           'claude': '/bin/claude', 'claude_window': 200000, 'worker_effort': 'medium',
           'token': 'abcd1234'}
    (run / 'config.json').write_text(json.dumps(cfg))
    advisor_on(run, cfg['workers'])
    f = fleet.Fleet(run / 'config.json', FakeRedis(), FakeMgr(), sessions=tmp_path / 'sessions')
    f.advisor_runner = Runner({'model': 'sonnet', 'effort': 'medium', 'domains': []})
    monkeypatch.setattr(f, 'preflight_profile', lambda launch: None)
    monkeypatch.setattr(fleet, 'connect', lambda cfg: f.r)
    f.start_all()
    for name in f.names:
        f.handle(turn(name, 'READY'))
    f.handle({'kind': 'task', 'request_id': 'r1', 'spec': {'goal': 'research the sector rotation'}})
    return f


def send_decision(f, items):
    f.on_supervisor(decision(dispatch=items))


def item(to='worker-1', text='Research the semis rotation and write worker-1/notes.md', note=''):
    out = {'to': to, 'instruction': text}
    if note:
        out['advise_note'] = note
    return out


def report(f, name, generation=0):
    """A v1 Claude turn answering the message delivered last (bound by its msg id)."""
    f.handle(dict(turn(name, 'Done:\n- a\nEvidence:\n- b\nNot done: none\nBlocked: none', generation),
                  msg_id=f.agent(name)['msg_id']))


def supervisor_texts(f):
    pane = f.agent('supervisor')['pane']
    return [t for p, t in f.mgr.sent if p == pane] + list(f.r.l.get('fx:inbox:supervisor', []))


def worker_text(f, name):
    return f.agent(name)['last_msg']


def test_every_dispatch_is_advised_with_supervisor_material_only(afx):
    f = afx
    f.handle(turn('worker-1', 'earlier private worker chatter SECRET'))  # a reply the advisor must never see
    send_decision(f, [item(note='needs web research, no code'),
                      item('worker-2', 'Summarize the advisor plan into worker-2/sum.md')])
    f.pump()
    assert len(f.advisor_runner.calls) == 2
    first = f.advisor_runner.calls[0]
    assert first['advise_note'] == 'needs web research, no code' and first['goal'] == 'research the sector rotation'
    assert first['slot']['cli'] == 'claude' and first['slot']['model'] == 'sonnet'
    assert {d['name'] for d in first['domains']} == {'research_web', 'us_options', 'fleet'}
    assert 'SECRET' not in json.dumps(f.advisor_runner.calls, ensure_ascii=False)
    assert all(f.agent(w)['state'] == 'busy' for w in f.workers)
    ticket = f.agent('worker-1')['advice_ticket']
    audit = f.r.hgetall(fleet.ADVICE + ticket)
    assert audit['effect'] == 'none' and audit['status'] == 'ok'
    assert audit['total_cost_usd'] == '0.14' and audit['cache_read_input_tokens'] == '21000'


def test_notices_and_corrections_are_not_advised(afx):
    f = afx
    f.queue('worker-1', '[Supervisor instruction ' + f.task()['id'] + '] [contract] IF-1@1 ready, carry on please')
    f.pump()
    assert f.advisor_runner.calls == [] and f.agent('worker-1')['state'] == 'busy'


def test_fingerprint_change_respawns_without_double_opt_in(afx):
    f = afx
    assert f.cfg['allow_profile_live_switch'] is False and f.task().get('apply_profiles_live') in (None, '0', 'false')
    f.advisor_runner = Runner({'model': 'opus', 'effort': 'high', 'domains': ['research_web']})
    cwd = fleet.agent_cwd(f.cfg, 'worker-1')
    (cwd / '.claude' / 'skills' / 'stale-skill').mkdir(parents=True)
    old = f.agent('worker-1')['pane']
    send_decision(f, [item()])
    f.pump()
    info = f.agent('worker-1')
    assert info['state'] == 'starting' and info['generation'] == '1' and info['label'] == 'worker-1.1'
    assert old in f.mgr.killed and f.mgr.tabbed[-1] == (info['pane'], old)
    launch = json.loads(info['runtime_launch'])
    assert launch['profile_id'] == 'claude-opus' and launch['extras'] == {'effort': 'high'}
    # new generation: cleared then linked, CLAUDE.md = scope + core + domain rules, no "Read AGENTS.md first"
    assert sorted(p.name for p in (cwd / '.claude' / 'skills').iterdir()) == ['web-fetch-fallback']
    claude_md = (cwd / 'CLAUDE.md').read_text()
    assert 'Never use yfinance' in claude_md and 'RULES OF research_web' in claude_md
    assert 'Read AGENTS.md' not in Path(f.mgr.spawned[-1]['prompt_file']).read_text()
    assert json.loads(info['installed_domains']) == ['research_web']
    snapshot = fleet.load(f.run_dir / 'runtime' / 'worker-1-g1.json')
    argv = fleet.claude_argv(f.path, snapshot, 'worker-1', 1, 'READY?')
    assert argv[argv.index('--model') + 1] == 'opus' and argv[argv.index('--effort') + 1] == 'high'
    assert argv[argv.index('--setting-sources') + 1] == 'project' and '--strict-mcp-config' in argv
    settings = json.loads(Path(argv[argv.index('--settings') + 1]).read_text())
    assert settings['autoMemoryEnabled'] is False and settings['hooks']['Stop']
    assert f.r.llen('fx:inbox:worker-1') == 1  # queued work waits for READY
    f.handle(turn('worker-1', 'READY', 1))
    f.pump()
    assert len(f.advisor_runner.calls) == 1  # the same message is never advised twice
    assert f.agent('worker-1')['state'] == 'busy' and 'semis rotation' in worker_text(f, 'worker-1')
    assert 'RULES OF research_web' not in worker_text(f, 'worker-1')  # respawn puts rules in CLAUDE.md, not the text


def test_only_domain_change_hotloads_without_respawn(afx):
    f = afx
    f.advisor_runner = Runner({'model': 'sonnet', 'effort': 'medium', 'domains': ['research_web']})
    gen = f.agent('worker-1')['generation']
    send_decision(f, [item(note='web')])
    f.pump()
    info = f.agent('worker-1')
    assert info['generation'] == gen and info['state'] == 'busy'
    text = worker_text(f, 'worker-1')
    assert text.startswith('## Domain rules: research_web') and 'RULES OF research_web' in text
    assert '[本任务领域：research_web；已链入 skill：web-fetch-fallback' in text
    assert text.index('RULES OF research_web') < text.index('semis rotation')
    cwd = fleet.agent_cwd(f.cfg, 'worker-1')
    assert (cwd / '.claude' / 'skills' / 'web-fetch-fallback').is_symlink()
    assert (cwd / '.claude' / 'agents' / 'web-research-specialist.md').is_symlink()
    assert json.loads(info['installed_domains']) == ['research_web']
    assert f.r.hgetall(fleet.ADVICE + info['advice_ticket'])['effect'] == 'hotload'
    # the supervisor sees the advice with its next business message, never as a lone message
    assert f.r.llen(fleet.ADVICE_NOTES) == 1
    report(f, 'worker-1')
    sup = supervisor_texts(f)[-1]
    assert f.r.llen(fleet.ADVICE_NOTES) == 0
    assert '[advisor] worker-1 → sonnet/medium, domains: [research_web]' in sup and 'hotload' in sup


def test_fewer_domains_do_nothing(afx):
    f = afx
    f.setf('worker-1', installed_domains=json.dumps(['research_web', 'us_options']))
    f.advisor_runner = Runner({'model': 'sonnet', 'effort': 'medium', 'domains': ['research_web']})
    send_decision(f, [item()])
    f.pump()
    info = f.agent('worker-1')
    assert info['generation'] == '0' and info['state'] == 'busy'
    assert '## Domain rules' not in worker_text(f, 'worker-1')
    assert json.loads(info['installed_domains']) == ['research_web', 'us_options']
    assert f.r.hgetall(fleet.ADVICE + info['advice_ticket'])['effect'] == 'none'


def test_advisor_unavailable_falls_back_without_blocking(afx):
    f = afx
    f.advisor_runner = Runner(TimeoutError('advisor timed out after 90 s'))
    send_decision(f, [item()])
    f.pump()
    assert len(f.advisor_runner.calls) == 2  # one retry
    info = f.agent('worker-1')
    assert info['state'] == 'busy' and info['generation'] == '0' and f.task()['status'] == 'running'
    audit = f.r.hgetall(fleet.ADVICE + info['advice_ticket'])
    assert audit['status'] == 'unavailable' and json.loads(audit['output'])['domains'] == []
    assert any('[advisor unavailable] 按缺省派出 worker-1' in x for x in f.r.lrange(fleet.ADVICE_NOTES, 0, -1))


def test_unavailable_reuses_previous_sheet_of_the_same_task(afx):
    f = afx
    f.advisor_runner = Runner({'model': 'sonnet', 'effort': 'medium', 'domains': ['research_web']})
    send_decision(f, [item()])
    f.pump()
    report(f, 'worker-1')
    f.advisor_runner = Runner(ValueError('not json'))
    send_decision(f, [item(text='Research the memory rotation and write worker-1/more.md')])
    f.pump()
    audit = f.r.hgetall(fleet.ADVICE + f.agent('worker-1')['advice_ticket'])
    assert audit['status'] == 'unavailable' and json.loads(audit['output'])['domains'] == ['research_web']


def test_invalid_sheet_counts_as_advisor_failure(afx):
    f = afx
    f.advisor_runner = Runner({'model': 'gpt-6', 'domains': []})
    send_decision(f, [item()])
    f.pump()
    audit = f.r.hgetall(fleet.ADVICE + f.agent('worker-1')['advice_ticket'])
    assert audit['status'] == 'unavailable' and 'model must be one of' in audit['errors']
    assert f.agent('worker-1')['state'] == 'busy'


def test_launch_drift_blocks_delivery(afx):
    f = afx
    f.advisor_runner = Runner({'model': 'opus', 'effort': 'high', 'domains': []})
    send_decision(f, [item()])
    f.pump()
    f.handle(turn('worker-1', 'READY', 1))
    f.pump()
    report(f, 'worker-1', 1)
    launch = json.loads(f.agent('worker-1')['runtime_launch'])
    launch['extras']['effort'] = 'high'
    launch['extras']['tampered'] = True  # frozen launch no longer matches what the sheet resolves to
    f.setf('worker-1', runtime_launch=profile_launch.encode_launch(launch))
    send_decision(f, [item(text='Research the memory rotation and write worker-1/more.md')])
    f.pump()
    assert f.task()['status'] == 'blocked' and 'extras changed since spawn' in f.task()['reason']


def test_codex_slot_never_gets_domains(fx):
    f = fx
    f.cfg.update(codex_model='gpt-6.1-sol')
    assert f.set_advisor(True, 'user') is None
    f.advisor_runner = Runner({'model': 'gpt-6.1-sol', 'domains': ['research_web']})
    f.on_supervisor(decision())
    f.pump()
    audit = f.r.hgetall(fleet.ADVICE + f.agent('worker-1')['advice_ticket'])
    assert audit['status'] == 'unavailable' and 'takes no domains' in audit['errors']
    assert f.agent('worker-1')['state'] == 'busy' and not (fleet.agent_cwd(f.cfg, 'worker-1') / '.claude').exists()


def test_without_advisor_nothing_changes(fx, tmp_path, monkeypatch):
    f = fx
    f.advisor_runner = Runner(AssertionError('advisor must not be consulted'))
    f.on_supervisor(decision())
    f.pump()
    assert f.advisor_runner.calls == [] and f.agent('worker-1')['state'] == 'busy'
    assert f.r.hgetall(fleet.ADVICE_SEQ) == {} and 'advice_ticket' not in f.agent('worker-1')
    cfg = dict(f.cfg, worker_cli='claude', claude='/bin/claude', worker_effort='high')
    argv = fleet.claude_argv(f.path, cfg, 'worker-1', 0, 'p')
    assert argv[argv.index('--effort') + 1] == 'high' and '--setting-sources' not in argv and '--strict-mcp-config' not in argv
    assert 'autoMemoryEnabled' not in Path(argv[argv.index('--settings') + 1]).read_text()


def test_bad_kit_refuses_the_fleet(tmp_path):
    kit = make_kit(tmp_path / 'kit')
    (kit / 'domains' / 'fleet' / 'RULES.md').unlink()
    run = tmp_path / 'run'
    (run / 'workspace').mkdir(parents=True)
    cfg = {'run': str(run), 'redis_socket': '/tmp/x.sock', 'python': sys.executable, 'workers': ['worker-1'],
           'worker_cli': 'claude', 'worker_kit': str(kit)}
    (run / 'config.json').write_text(json.dumps(cfg))
    fleet.Fleet(run / 'config.json', FakeRedis(), FakeMgr())  # advisor off: the kit is not the advisor's business
    advisor_on(run, cfg['workers'])
    with pytest.raises(kitmod.KitError, match='rules file missing'):
        fleet.Fleet(run / 'config.json', FakeRedis(), FakeMgr())


def test_held_actor_waits_past_60_seconds_and_rereads_its_snapshot(tmp_path, monkeypatch):
    r = FakeRedis()
    run = tmp_path / 'run'
    (run / 'workspace' / 'worker-1').mkdir(parents=True)
    snap = run / 'runtime' / 'worker-1-g0.json'
    base = {'run': str(run), 'python': sys.executable, 'workers': ['worker-1'], 'worker_cli': 'claude',
            'claude': '/bin/claude', 'advisor': True, 'worker_model': 'sonnet', 'redis_socket': '/tmp/x.sock'}
    fleet.save(snap, base)
    r.hset('fx:agent:worker-1', {'generation': 0, 'state': 'starting', 'launch_ready': 0, 'advice_hold': 1})
    clock = {'t': 0.0, 'polls': 0}

    def sleep(_):
        clock['t'] += 30  # every poll jumps 30 s: a 60 s limit would have fired long before release
        clock['polls'] += 1
        if clock['polls'] == 10:
            fleet.save(snap, dict(base, worker_model='opus', worker_effort='high'))
            r.hset('fx:agent:worker-1', {'launch_ready': 1, 'advice_hold': 0})
    monkeypatch.setattr(fleet.time, 'monotonic', lambda: clock['t'])
    monkeypatch.setattr(fleet.time, 'sleep', sleep)
    monkeypatch.setattr(fleet, 'connect', lambda cfg: r)
    execd = {}
    monkeypatch.setattr(fleet.os, 'execvp', lambda a0, argv: execd.update(argv=argv))
    monkeypatch.setattr(fleet.os, 'chdir', lambda d: None)
    fleet.actor(snap, 'worker-1', 'READY?', 0)
    argv = execd['argv']
    assert clock['t'] >= 300 and argv[argv.index('--model') + 1] == 'opus' and argv[argv.index('--effort') + 1] == 'high'


# ------------------------------------------------------------------ v2 daemon (real private Redis, fake cmux)
@pytest.fixture
def v2adv(private_redis_v2, tmp_path, monkeypatch):  # noqa: F811
    yield from v2_advised(private_redis_v2, tmp_path, monkeypatch, ['worker-1'])


@pytest.fixture
def v2adv2(private_redis_v2, tmp_path, monkeypatch):  # noqa: F811
    yield from v2_advised(private_redis_v2, tmp_path, monkeypatch, ['worker-1', 'worker-2'])


def v2_advised(r, tmp_path, monkeypatch, workers, on=True, **extra):
    import uuid
    from test_v2_codex_claude import answer_turn
    r.flushdb()
    kit = make_kit(tmp_path / 'kit')
    run = tmp_path / ('run-' + uuid.uuid4().hex)
    (run / 'workspace').mkdir(parents=True)
    sessions = tmp_path / 'codex-home' / 'sessions'
    cfg = dict(run=str(run), redis_socket=r.connection_pool.connection_kwargs['path'], python=sys.executable,
               workers=list(workers), mode='task', supervisor_cli='codex', worker_cli='claude',
               codex_home=str(tmp_path / 'codex-home'), transport_version=2, allow_development_v2=True,
               stall_timeout=0, v2_lease_ms=300000, max_busy=0, worker_kit=str(kit),
               claude='/bin/claude', token='feedbeef', **extra)
    path = run / 'config.json'
    fleet.save(path, cfg)
    if on:
        advisor_on(run, workers)
    f = fleet.Fleet(path, r, FakeMgr(), sessions=sessions)
    f.test_sessions, f.test_claude = sessions, tmp_path / 'claude-projects'
    f.test_threads = {'supervisor': str(uuid.uuid4())}
    f.test_threads.update({w: str(uuid.uuid4()) for w in workers})
    f.advisor_runner = Runner({'model': 'opus', 'effort': 'high', 'domains': ['research_web']})
    monkeypatch.setattr(fleet, 'trust_claude_dir', lambda cwd: None)
    try:
        f.start_all()
        r.set(f.v2.prefix + ':boot', 'running')
        assert answer_turn(f, 'supervisor', 'READY', startup=True)['status'] == 'ready'
        yield f
    finally:
        f.v2.close()


def test_v2_first_spawn_is_held_until_the_first_sheet(v2adv):
    from test_v2_codex_claude import answer_turn, submit, typed
    f = v2adv
    info = f.agent('worker-1')
    assert info['state'] == 'starting' and info['launch_ready'] == '0' and info['advice_hold'] == '1'
    first_snapshot = fleet.load(f.run_dir / 'runtime' / 'worker-1-g0.json')
    assert 'worker_effort' not in first_snapshot
    f.setf('worker-1', startup_deadline=time.time() - 1)
    f.v2.tick()
    assert f.agent('worker-1')['state'] == 'starting' and f.task().get('status') != 'blocked'  # hold is not a startup timeout
    submit(f)
    assert answer_turn(f, 'supervisor', json.dumps({'status': 'RUNNING', 'completion': 5, 'reason': 'go', 'dispatch': [
        {'to': 'worker-1', 'instruction': 'Research the semis rotation into worker-1/notes.md', 'advise_note': 'web',
         'id': 'M0'}]}))['status'] == 'committed'
    f.pump()  # release: the advisor decides the first launch
    assert len(f.advisor_runner.calls) == 1 and f.advisor_runner.calls[0]['advise_note'] == 'web'
    info = f.agent('worker-1')
    assert info['launch_ready'] == '1' and info['advice_hold'] == '0' and float(info['startup_deadline']) > time.time()
    snap = fleet.load(f.run_dir / 'runtime' / 'worker-1-g0.json')
    assert snap['worker_model'] == 'opus' and snap['worker_effort'] == 'high'
    cwd = fleet.agent_cwd(f.cfg, 'worker-1')
    assert 'RULES OF research_web' in (cwd / 'CLAUDE.md').read_text()
    assert (cwd / '.claude' / 'skills' / 'web-fetch-fallback').is_symlink()
    assert not typed(f, 'worker-1')  # nothing typed before READY
    assert answer_turn(f, 'worker-1', 'READY', startup=True)['status'] == 'ready'
    f.pump()
    text, = typed(f, 'worker-1')
    assert 'semis rotation' in text and 'RULES OF' not in text
    assert len(f.advisor_runner.calls) == 1 and f.agent('worker-1')['generation'] == '0'


def test_v2_domain_hotload_rewrites_the_bus_body(v2adv):
    from test_v2_codex_claude import answer_turn, submit, typed
    f = v2adv
    submit(f)
    answer_turn(f, 'supervisor', json.dumps({'status': 'RUNNING', 'completion': 5, 'reason': 'go', 'dispatch': [
        {'to': 'worker-1', 'instruction': 'Research the semis rotation into worker-1/notes.md'}]}))
    f.pump()
    answer_turn(f, 'worker-1', 'READY', startup=True)
    f.pump()
    answer_turn(f, 'worker-1', 'Done:\n- notes\nEvidence:\n- worker-1/notes.md\nNot done: none\nBlocked: none')
    f.pump()
    f.advisor_runner = Runner({'model': 'opus', 'effort': 'high', 'domains': ['research_web', 'us_options'],
                               'reason': '美股选链 + 查社区，一个领域不够；能力表 research_web/web-research'})
    sup = typed(f, 'supervisor')[-1]
    assert '[advisor] worker-1 → opus/high, domains: [research_web]' in sup  # first sheet, carried on the report
    answer_turn(f, 'supervisor', json.dumps({'status': 'RUNNING', 'completion': 50, 'reason': 'go', 'dispatch': [
        {'to': 'worker-1', 'instruction': 'Screen the option chain of NVDA into worker-1/chain.md'}]}))
    f.pump()
    text = typed(f, 'worker-1')[-1]
    assert text.startswith('## Domain rules: us_options') and 'RULES OF research_web' not in text
    assert 'option chain of NVDA' in text and f.agent('worker-1')['generation'] == '0'
    assert json.loads(f.agent('worker-1')['installed_domains']) == ['research_web', 'us_options']
    assert (fleet.agent_cwd(f.cfg, 'worker-1') / '.claude' / 'skills' / 'option-tier-screen').is_symlink()


# ------------------------------------------------------------------ start --advisor (sugar: writes the roster)
def fake_start(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from fleetmux import cmux_manager
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: True)
    monkeypatch.setattr(fleet.shutil, 'which', lambda name: '/bin/' + Path(str(name)).name)
    mgr = SimpleNamespace(session_exists=lambda: False, meta_path=tmp_path / 'cmux' / 'meta.json',
                          spawn_window=lambda *a, **k: SimpleNamespace(success=True, error=None))
    monkeypatch.setattr(fleet, 'manager_for', lambda titles: mgr)
    monkeypatch.setattr(fleet.redis_runtime, 'prepare', lambda root, token: {'redis_socket': str(tmp_path / 's.sock')})
    monkeypatch.setattr(fleet, 'start_redis', lambda cfg: None)
    monkeypatch.setattr(fleet, 'prepare_codex_home', lambda home, profiles: None)
    monkeypatch.setattr(fleet, 'LATEST', tmp_path / 'latest.json')
    return ['start', '--lifecycle', 'dev_task', '--supervisor-cli', 'codex', '--worker-cli', 'claude', '--allow-development-v2',
            '--runs-dir', str(tmp_path / 'runs'), '--codex-home', str(tmp_path / 'codex-home')]


def test_start_advisor_flag_only_writes_the_roster(monkeypatch, tmp_path):
    from fleetmux import roster
    argv = fake_start(monkeypatch, tmp_path)
    kit = make_kit(tmp_path / 'kit')
    fleet.main(argv + ['--advisor', '--worker-kit', str(kit), '--workers', '2'])
    latest = fleet.load(tmp_path / 'latest.json')
    assert 'advisor' not in latest and 'advisor' not in fleet.load(latest['config'])  # never in config.json
    raw = (Path(latest['run']) / 'roster.yaml').read_bytes()
    assert roster.advisor(raw) is True and roster.parse(raw, Path(latest['run']))[0] == ['worker-1', 'worker-2']
    own = tmp_path / 'team.yaml'
    own.write_text('# my team\nworkers:\n  - name: exit-1\n')
    fleet.main(argv + ['--advisor', '--roster', str(own)])
    assert own.read_text() == '# my team\nworkers:\n  - name: exit-1\nadvisor: on\n'  # edited in place, comment kept
    fleet.main(argv)
    assert not (Path(fleet.load(tmp_path / 'latest.json')['run']) / 'roster.yaml').exists()  # not passed: no roster


def test_start_advisor_refuses_bad_kit_before_provisioning(monkeypatch, tmp_path):
    argv = fake_start(monkeypatch, tmp_path)
    from fleetmux import cmux_manager
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: pytest.fail('bad kit must be refused first'))
    kit = make_kit(tmp_path / 'kit')
    (kit / 'domains' / 'fleet' / 'RULES.md').unlink()
    with pytest.raises(SystemExit, match='--worker-kit rejected'):
        fleet.main(argv + ['--advisor', '--worker-kit', str(kit)])


# ------------------------------------------------------------------ contract task: held rows are advised on release
def test_contract_row_held_by_depends_on_is_advised_when_released(tmp_path):
    from test_contracts import WORKERS, THREADS as CT, item as citem, report as creport, sup, to_parallel, write
    from test_fleet import write_rollout
    run = tmp_path / 'run'
    (run / 'workspace').mkdir(parents=True)
    cfg = {'run': str(run), 'redis_socket': '/tmp/cmux-fleet-test.sock', 'python': sys.executable,
           'workers': list(WORKERS), 'handoff_ratio': .6, 'handoff_timeout': 900, 'window': 95000,
           'codex_home': str(tmp_path / 'codex-home'), 'codex_model': 'gpt-6.1-sol'}
    (run / 'config.json').write_text(json.dumps(cfg))
    advisor_on(run, cfg['workers'])
    sessions = tmp_path / 'sessions'
    f = fleet.Fleet(run / 'config.json', FakeRedis(), FakeMgr(), sessions=sessions)
    f.advisor_runner = Runner({'model': 'gpt-6.1-sol', 'domains': []})
    f.start_all()
    for name in f.names:
        write_rollout(sessions, CT[name], fleet.agent_cwd(cfg, name))
        f.handle({'kind': 'turn', 'agent': name, 'generation': 0, 'thread': CT[name], 'text': 'READY'})
    to_parallel(f)
    sup(f, {'status': 'RUNNING', 'completion': 10, 'dispatch': [
        citem('M0', 'worker-1', provides=['IF-M0-src']),
        citem('M9', 'worker-3', depends_on=['M0'], serial_reason='integration')]})
    assert f.contract_state()['tasks']['M9']['status'] == 'pending'
    assert len(f.r.lrange(fleet.ADVICE_PENDING + 'worker-3', 0, -1)) == 1  # registered while held
    write(f, 'worker-1/sources.md', '## Sources\n| url |\n| https://a |\n')
    creport(f, 'worker-1', 'Interfaces: IF-M0-src@1 provided: ready')
    assert f.contract_state()['tasks']['M9']['status'] == 'running'
    f.pump()
    asked = [c['instruction'] for c in f.advisor_runner.calls]
    assert 'Do task M9 and write its output file' in asked
    assert f.agent('worker-3')['advice_ticket'] and f.agent('worker-3')['state'] == 'busy'


def test_v2_fingerprint_change_respawns_on_the_real_bus(v2adv, monkeypatch):
    from test_v2_codex_claude import answer_turn, submit, typed
    f = v2adv
    monkeypatch.setattr(f, 'preflight_profile', lambda launch: None)
    submit(f)
    answer_turn(f, 'supervisor', json.dumps({'status': 'RUNNING', 'completion': 5, 'reason': 'go', 'dispatch': [
        {'to': 'worker-1', 'instruction': 'Research the semis rotation into worker-1/notes.md'}]}))
    f.pump()  # release with opus/high
    answer_turn(f, 'worker-1', 'READY', startup=True)
    f.pump()
    answer_turn(f, 'worker-1', 'Done:\n- notes\nEvidence:\n- worker-1/notes.md\nNot done: none\nBlocked: none')
    f.pump()
    old = f.agent('worker-1')['pane']
    f.advisor_runner = Runner({'model': 'sonnet', 'effort': 'medium', 'domains': [],
                               'reason': '能力表 research_web/web-research：sonnet=enough，降档'})
    answer_turn(f, 'supervisor', json.dumps({'status': 'RUNNING', 'completion': 50, 'reason': 'go', 'dispatch': [
        {'to': 'worker-1', 'instruction': 'Summarize worker-1/notes.md into worker-1/summary.md'}]}))
    f.pump()
    info = f.agent('worker-1')
    assert info['generation'] == '1' and info['label'] == 'worker-1.1' and info['state'] == 'starting'
    assert old in f.mgr.killed and json.loads(info['runtime_launch'])['profile_id'] == 'claude-sonnet'
    assert info['launch_ready'] == '1'
    assert len(typed(f, 'worker-1')) == 0  # the successor pane has nothing typed before READY
    assert answer_turn(f, 'worker-1', 'READY', startup=True, generation=1)['status'] == 'ready'
    assert f.v2.bus.slot_state('worker-1')['generation'] == '1'
    f.pump()
    text, = typed(f, 'worker-1')
    assert 'Summarize worker-1/notes.md' in text
    assert len(f.advisor_runner.calls) == 1 and f.agent('worker-1')['state'] == 'busy'
    snapshot = fleet.load(f.run_dir / 'runtime' / 'worker-1-g1.json')
    assert snapshot['worker_model'] == 'sonnet' and snapshot['worker_effort'] == 'medium'


def test_held_worker_does_not_count_as_busy_for_the_dispatch_rule(v2adv2):
    """P1-01: a worker held for its first sheet (nothing queued) must not suppress the
    'RUNNING must dispatch' correction, or the task hangs until its time budget runs out."""
    from test_v2_codex_claude import answer_turn, submit, typed
    f = v2adv2
    submit(f)
    answer_turn(f, 'supervisor', json.dumps({'status': 'RUNNING', 'completion': 5, 'reason': 'go', 'dispatch': [
        {'to': 'worker-1', 'instruction': 'Research the semis rotation into worker-1/notes.md'}]}))
    f.pump()
    answer_turn(f, 'worker-1', 'READY', startup=True)
    f.pump()
    answer_turn(f, 'worker-1', 'Done:\n- notes\nEvidence:\n- worker-1/notes.md\nNot done: none\nBlocked: none')
    f.pump()
    assert f.agent('worker-2')['state'] == 'starting' and f.agent('worker-2')['advice_hold'] == '1'
    assert not f.others_active()
    answer_turn(f, 'supervisor', json.dumps({'status': 'RUNNING', 'completion': 50, 'reason': 'wait', 'dispatch': []}))
    f.pump()
    # v2 turns the parse error into the generic correction (existing behavior); the count proves it fired
    assert typed(f, 'supervisor')[-1].startswith('Your reply is not a valid decision JSON')
    assert f.task()['bad_replies'] == '1'


def test_broken_card_file_is_advisor_unavailable_not_a_daemon_exit(afx, monkeypatch):
    """A bad card JSON must not escape pump (the v2 main loop does not catch it): fallback per §8."""
    f = afx
    from fleetmux.agent_registry import AgentRegistryError

    def broken(*args, **kwargs):
        raise AgentRegistryError('could not load agent card claude-opus.json: Expecting value')
    monkeypatch.setattr(fleet, 'AgentRegistry', broken)
    send_decision(f, [item()])
    f.pump()  # must not raise
    info = f.agent('worker-1')
    assert info['state'] == 'busy' and info['generation'] == '0' and f.advisor_runner.calls == []
    audit = f.r.hgetall(fleet.ADVICE + info['advice_ticket'])
    assert audit['status'] == 'unavailable' and 'agent registry unreadable' in audit['errors']
    assert any('[advisor unavailable]' in x for x in f.r.lrange(fleet.ADVICE_NOTES, 0, -1))


# ------------------------------------------------------------------ P2-9: advisor respawns pass Fleet.card_gate
def test_advisor_respawn_waits_for_a_model_ticket_then_switches(afx):
    """fable max_concurrent 1: the first slot switches, the second waits queued (no re-advice) and
    switches once the first slot's fable generation is gone."""
    f = afx
    from fleetmux.agent_registry import AgentRegistry
    assert AgentRegistry().get('claude-fable').max_concurrent == 1
    f.advisor_runner = Runner({'model': 'fable', 'effort': 'high', 'domains': []})
    send_decision(f, [item(), item('worker-2', 'Summarize the advisor plan into worker-2/sum.md')])
    f.pump()
    one, two = f.agent('worker-1'), f.agent('worker-2')
    assert one['generation'] == '1' and json.loads(one['runtime_launch'])['profile_id'] == 'claude-fable'
    assert two['state'] == 'idle' and two['generation'] == '0' and two['profile_state'] == 'pending'
    assert 'max_concurrent 1 for model fable' in two['capacity_wait'] and f.r.llen('fx:inbox:worker-2') == 1
    f.pump()
    f.pump()
    from test_model_usage import logs
    assert len(logs(f, 'profile.capacity_wait')) == 1  # once per reason
    assert len(f.advisor_runner.calls) == 2 and f.agent('worker-2')['generation'] == '0'
    f.mgr.kill_pane(one['pane'])  # worker-1's fable generation is gone
    f.setf('worker-1', state='dead')
    f.pump()
    two = f.agent('worker-2')
    assert two['generation'] == '1' and two['state'] == 'starting' and two['capacity_wait'] == ''
    assert json.loads(two['runtime_launch'])['profile_id'] == 'claude-fable' and two['advice_wait'] == ''
    assert len(f.advisor_runner.calls) == 2  # the waiting sheet was reused, not re-advised
    f.handle(turn('worker-2', 'READY', 1))
    f.pump()
    assert f.agent('worker-2')['state'] == 'busy' and 'advisor plan' in worker_text(f, 'worker-2')


def test_advisor_respawn_to_a_card_for_another_host_blocks_the_task(afx, monkeypatch, tmp_path):
    import shutil
    from fleetmux import agent_registry
    reg = tmp_path / 'agents'
    shutil.copytree(Path(agent_registry.__file__).parent / 'agents', reg)  # role files
    (reg / 'models').mkdir()
    from fleetmux import fleet_config
    for key, card in fleet_config.block('profiles').items():
        (reg / (key + '.json')).write_text(json.dumps(card))
    for key, card in fleet_config.block('models').items():
        (reg / 'models' / (key + '.json')).write_text(json.dumps(dict(card, hosts=['studio']) if key == 'opus' else card))

    class Reg(agent_registry.AgentRegistry):
        def __init__(self, directory=None):
            super().__init__(reg)
    monkeypatch.setattr(fleet, 'AgentRegistry', Reg)
    f = afx
    f.cfg['host'] = 'macbook'
    f.advisor_runner = Runner({'model': 'opus', 'effort': 'high', 'domains': []})
    send_decision(f, [item()])
    f.pump()
    info = f.agent('worker-1')
    assert info['generation'] == '0' and info['profile_state'] == 'blocked' and 'studio' in info['profile_reason']
    assert f.task()['status'] == 'blocked' and f.mgr.killed == []  # no silent swap, old runtime kept


def test_v2_held_first_generation_keeps_holding_while_the_model_is_full(v2adv2):
    from test_v2_codex_claude import answer_turn, submit
    f = v2adv2
    f.advisor_runner = Runner({'model': 'fable', 'effort': 'high', 'domains': []})
    submit(f)
    assert answer_turn(f, 'supervisor', json.dumps({'status': 'RUNNING', 'completion': 5, 'reason': 'go', 'dispatch': [
        {'to': 'worker-1', 'instruction': 'Research the semis rotation into worker-1/notes.md', 'id': 'M0'},
        {'to': 'worker-2', 'instruction': 'Summarize the advisor plan into worker-2/sum.md', 'id': 'M1'}]}))['status'] == 'committed'
    f.pump()
    one, two = f.agent('worker-1'), f.agent('worker-2')
    assert one['launch_ready'] == '1' and json.loads(one['runtime_launch'])['profile_id'] == 'claude-fable'
    assert two['advice_hold'] == '1' and two['launch_ready'] == '0' and two['profile_state'] == 'pending'
    assert 'max_concurrent 1 for model fable' in two['capacity_wait']
    f.pump()
    f.v2.tick()
    assert f.agent('worker-2')['advice_hold'] == '1' and len(f.advisor_runner.calls) == 2  # held, not re-advised
    assert f.task().get('status') != 'blocked'
    f.mgr.kill_pane(one['pane'])
    f.setf('worker-1', state='dead')
    f.pump()
    two = f.agent('worker-2')
    assert two['advice_hold'] == '0' and two['launch_ready'] == '1' and two['capacity_wait'] == ''
    assert fleet.load(f.run_dir / 'runtime' / 'worker-2-g0.json')['worker_model'] == 'fable'
    assert len(f.advisor_runner.calls) == 2


def test_v2_named_subfleet_supervisor_slot_is_not_held(private_redis_v2, tmp_path, monkeypatch):  # noqa: F811
    """10-14 Studio: memx-ops / tgog-ops waited for a first sheet that never comes (a supervisor slot gets no
    delivery before its sub-fleet is up), and the sub-fleet waits for them READY: both stuck in starting."""
    gen = v2_advised(private_redis_v2, tmp_path, monkeypatch, ['memx-ops', 'worker-2'],
                     subfleets=[{'name': 'memx', 'supervisor_slot': 'memx-ops'}])
    f = next(gen)
    try:
        sub, plain = f.agent('memx-ops'), f.agent('worker-2')
        assert sub['launch_ready'] == '1' and sub.get('advice_hold', '0') == '0'
        assert plain['launch_ready'] == '0' and plain['advice_hold'] == '1'  # ordinary workers still wait
    finally:
        gen.close()
