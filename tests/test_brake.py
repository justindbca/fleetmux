"""Brake (budget card per quota pool + wall table) and the caller-frozen acceptance contract
(artifacts_frozen + not_contains / json_keys / cmd_exit). In-memory Redis + fake cmux on the v1
path, plus real-Redis v2 checks (the transport new fleets must use)."""
import json
import os
import sys
import time
from pathlib import Path

import pytest

from test_fleet import THREADS, FakeMgr, FakeRedis, decision, turn, write_rollout
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import fx, receive, req, session, settle, event  # noqa: F401  (fx: real-Redis v2 fleet)
from fleetmux import brake, discover, fleet, runtime_v2
from fleetmux import fleet_config

REAL_FABLE_WALL = ("⎿  You've reached your Fable limit. Run /usage-credits to continue "
                   "or switch models with /model.")
REAL_CODEX_WALL = ("■ You've hit your usage limit. To continue using Codex and get access to "
                   "GPT-5.3-Codex, start a free trial of Plus today, or try again at Sep 27th, 2026.")
# Third-party tool errors a worker sees or reports (EF review r1 P1-01): never a CLI wall.
TOOL_ERRORS = ('⎿  Error: 429 Too Many Requests (reddit)', 'WebFetch: rate limited by host, retrying in 30s',
               'Tavily: all keys exhausted; quota exceeded for today',
               '{"status":"BLOCKED","reason":"reddit API rate limited; need human"}')
THREADS = dict(THREADS, **{'worker-2': '33333333-3333-3333-3333-333333333333'})


@pytest.fixture(autouse=True)
def isolated_claude_trust(tmp_path, monkeypatch):
    # Fake CLI spawns still exercise trust_claude_dir; never write the operator's config.
    monkeypatch.setattr(fleet, 'CLAUDE_JSON', tmp_path / 'claude.json')


@pytest.fixture(autouse=True)
def pools_file(tmp_path, monkeypatch):
    """The operator's private fleet.yaml with an accounts block (format: the comment in config/fleet.yaml)."""
    path = tmp_path / 'fleet.yaml'
    path.write_text('accounts:\n'
                    '  - {id: pool-a, aliases: [old-a], meter: usd_equiv}\n'
                    '  - {id: pool-b, meter: week_pct}\n'
                    '  - {id: pool-c, meter: usd_equiv, caps: {day_usd: 25, night_usd: 5}}\n')
    monkeypatch.setenv(fleet_config.USER_ENV, str(path))
    return path


@pytest.fixture(autouse=True)
def claude_only_chain(tmp_path, monkeypatch):
    """A discovery cache with only the official claude CLI: the wall chains are worker opus > sonnet and supervisor
    fable > opus (fleet.yaml#discovery), so these tests never depend on the host's pi / opencode installs.
    Cross-CLI chain switches: tests/test_wall_chain.py."""
    path = tmp_path / 'discovered.yaml'
    path.write_text(json.dumps({'version': 1, 'gateways': {}, 'clis': {'claude': {
        'installed': True, 'version_ok': True, 'logged_in': True, 'models': ['fable', 'haiku', 'opus', 'sonnet']}}}))
    monkeypatch.setenv(discover.CACHE_ENV, str(path))


def make(tmp_path, spec=None, workers=('worker-1',), **cfg_extra):
    """A v1 fleet with every agent idle and (optionally) one task submitted."""
    run = tmp_path / 'run'
    (run / 'workspace').mkdir(parents=True)
    cfg = dict({'run': str(run), 'redis_socket': '/tmp/fleetmux-test.sock', 'python': sys.executable,
                'workers': list(workers), 'handoff_ratio': .6, 'handoff_timeout': 900, 'window': 95000,
                'codex_home': str(tmp_path / 'codex-home'), 'quota_file': str(tmp_path / 'quota.json')}, **cfg_extra)
    (run / 'config.json').write_text(json.dumps(cfg))
    sessions = tmp_path / 'sessions'
    f = fleet.Fleet(run / 'config.json', FakeRedis(), FakeMgr(), sessions=sessions)
    f.start_all()
    for name in f.names:
        write_rollout(sessions, THREADS[name], fleet.agent_cwd(cfg, name))
        f.handle(turn(name, 'READY'))
    if spec is not None:
        submit(f, spec)
    return f


def submit(f, spec, rid='t1'):
    f.handle({'kind': 'task', 'request_id': rid, 'spec': spec})
    return json.loads(f.r.get('fx:reply:' + rid))


def control(f, kind, rid, **extra):
    f.handle(dict({'kind': kind, 'request_id': rid}, **extra))
    return json.loads(f.r.get('fx:reply:' + rid))


def quota(tmp_path, **pools):
    now = time.time()
    (tmp_path / 'quota.json').write_text(json.dumps({'at': now, 'pools': {
        k: dict(v, at=now) for k, v in pools.items()}}))


# Claude Code 2.1.316 usage reminders (11-09 channels fleet: tripped the brake twice in 40 minutes, §48.4)
USAGE_NOTICES = ("You've used 78% of your weekly limit · resets 3am (America/Toronto) · /usage-credits to keep using…",
                 "You've used 84% of your weekly limit · resets 3am (America/Toronto) · /usage-credits to keep using…")


# ------------------------------------------------------------------ wall table (pure)
@pytest.mark.parametrize('notice', USAGE_NOTICES)
def test_percent_usage_reminder_is_not_a_wall(notice):
    """§48.4: "You've used N% of your ... limit" is a reminder, not a wall (account or pool); a narrow pane
    that wraps it before "/usage-credits" is not a wall either."""
    assert brake.classify_wall(notice) is None
    assert brake.classify_wall('⎿  ' + notice) is None
    head, tail = notice.split(' · /usage-credits')
    assert brake.classify_wall(head + ' ·\n/usage-credits' + tail) is None
    assert brake.screen_wall('working on the report\n' + notice + '\n❯ ') is None


def test_spent_pool_and_account_limits_still_trip_next_to_a_usage_reminder():
    for wall, scope in ((REAL_FABLE_WALL, 'model_pool'), ("You've reached your Fable limit", 'model_pool'),
                        ("You've hit your weekly limit · resets 3am", 'account'), ('weekly limit reached', 'account')):
        got = brake.classify_wall(USAGE_NOTICES[0] + '\n' + wall)
        assert got and got['scope'] == scope, (wall, got)


def test_wall_table_classifies_real_wall_texts_and_ignores_work_screens():
    assert brake.classify_wall(REAL_FABLE_WALL)['scope'] == 'model_pool'
    assert brake.classify_wall(REAL_CODEX_WALL)['scope'] == 'account'
    assert brake.classify_wall("1. Set up usage credits and continue\n2. Switch model")['scope'] == 'model_pool'
    # E18: a weekly-limit screen that also offers /usage-credits is account-level, not a model pool.
    assert brake.classify_wall("You've hit your weekly limit · resets Oct 14\nRun /usage-credits")['scope'] == 'account'
    assert brake.classify_wall("You've reached your session limit")['scope'] == 'account'
    assert brake.classify_wall('Claude AI usage limit reached|1760000000')['scope'] == 'account'
    # EF review r1 P1-01 (spec change): generic tool errors are not CLI walls, only the CLIs' own wording is.
    for clean in ('正在分析 cu 的库存与基差', 'Read 20 files, no limit issues found', '设置 position limit 为 5 手',
                  'the API rate limit is 5/s', 'HTTP 429: Too Many Requests', *TOOL_ERRORS, '', None):
        assert brake.classify_wall(clean) is None


def test_wall_action_takes_the_next_chain_model_and_stops_at_the_bottom():
    # 2026-11-11: the role's chain (fleet.yaml#discovery) replaced same_cli_downgrade as the wall decision
    pool = [{'cli': 'claude', 'model': 'opus', 'account': 'claude', 'scope': 'model_pool'}]
    assert brake.wall_action('claude', 'opus', [('claude', 'sonnet')], pool) == {
        'action': 'switch', 'cli': 'claude', 'from': 'opus', 'to_cli': 'claude', 'to': 'sonnet', 'skipped': []}
    assert brake.wall_action('claude', 'sonnet', [], pool)['action'] == 'stop'  # bottom of the chain
    account = [dict(pool[0], scope='account')]
    stop = brake.wall_action('claude', 'opus', [('claude', 'sonnet')], account)
    assert stop['action'] == 'stop' and stop['skipped'] == ['claude:sonnet (account wall)'] and 'reset' in stop['advice']


# ------------------------------------------------------------------ budget card (pure)
@pytest.mark.parametrize('raw, error', [
    ({'pool': 'pool-b', 'max_usd': 10}, 'max_usd applies only to a pool with meter usd_equiv'),
    ({'max_usd': 10}, 'max_usd applies only to a pool with meter usd_equiv'),
    ({'pool': 'pool-a', 'max_week_pct': 80}, 'max_week_pct applies only to a pool with meter week_pct'),
    ({'pool': 'pool-b', 'max_week_pct': 120}, 'at most 100'),
    ({'pool': 'other'}, 'is not an account id or alias'),
    ({'max_calls': 0}, 'max_calls must be a positive integer'),
    ({'max_calls': True}, 'max_calls must be a positive integer'),
    ({'max_rounds': 2.5}, 'max_rounds must be a positive integer'),
    ({'max_hours': float('inf')}, 'max_hours must be a positive number'),
    ({'max_tokens': 5}, 'budget may only have'),
    ({}, 'non-empty JSON object'),
    ([1], 'non-empty JSON object')])
def test_budget_card_rejects_wrong_pool_lines_and_bad_numbers(raw, error):
    with pytest.raises(ValueError, match=error):
        brake.validate_budget(raw)


def test_budget_card_counters_and_quota_readings():
    assert brake.validate_budget(None) is None
    card = brake.validate_budget({'pool': 'pool-a', 'max_usd': 40, 'max_calls': 3})
    now = time.time()
    q = {'pools': {'pool-a': {'cash_used_usd': 141.5, 'at': now}}}
    state = {'budget': json.dumps(card), 'calls': '3', 'rounds': '1', 'started': str(now - 7200), 'usd_base': '100'}
    values = brake.counters(state, q, now)
    assert values['usd'] == 41.5 and values['calls'] == 3 and values['hours'] == 2.0
    assert [t['limit'] for t in brake.trips(card, values)] == ['max_calls', 'max_usd']
    stale = {'pools': {'pool-a': {'cash_used_usd': 999, 'at': now - 7200}}}
    assert brake.counters(state, stale, now)['usd'] is None  # unknown never trips
    assert brake.trips(card, dict(values, usd=None, calls=2)) == []
    assert brake.pool_reading({}, 'pool-b') is None
    assert brake.parse_raise(['max_calls=10'], card)['max_calls'] == 10
    with pytest.raises(ValueError, match='LIMIT=VALUE'):
        brake.parse_raise(['pool=pool-b'], card)


# ------------------------------------------------------------------ brake in the daemon (v1 path)
def test_max_calls_pauses_notifies_and_resume_needs_a_raise(tmp_path):
    marker = tmp_path / 'notified.txt'
    notify = [sys.executable, '-c', 'import sys; open(%r, "w").write(sys.argv[1])' % str(marker)]
    f = make(tmp_path, {'goal': 'write a report', 'budget': {'max_calls': 2}}, brake_notify=notify)
    assert 'Budget card' in f.r.lrange('fx:inbox:supervisor', 0, 0)[0]
    f.pump()  # call 1: supervisor
    f.handle(turn('supervisor', decision()))
    f.pump()  # call 2: worker-1
    assert f.agent('worker-1')['state'] == 'busy'
    f.tick()
    assert f.paused() and f.task()['status'] == 'running'  # paused, resumable, not finished
    assert f.r.hget('fx:service', 'reason').startswith('brake: budget line reached: max_calls 2/2')
    record = json.loads((f.run_dir / 'brake.json').read_text())
    assert record['kind'] == 'budget' and record['counters']['calls'] == 2 and record['budget'] == {'max_calls': 2}
    for _ in range(50):
        if marker.exists():
            break
        time.sleep(0.05)
    assert marker.read_text().startswith('[fleetmux brake] budget line reached')
    f.handle(turn('worker-1', 'Done: worker-1/report.md'))
    f.pump()
    assert f.agent('supervisor')['state'] == 'idle' and f.r.llen('fx:inbox:supervisor') == 1  # nothing delivered
    answer = control(f, 'resume', 'c1')
    assert answer['status'] == 'rejected' and 'max_calls 2/2' in answer['reason'] and f.paused()
    assert control(f, 'resume', 'c2', **{'raise': ['pool=pool-b']})['status'] == 'rejected'
    assert control(f, 'resume', 'c3', **{'raise': ['max_calls=10']})['status'] == 'accepted'
    assert not f.paused() and f.brake_budget() == {'max_calls': 10}
    f.pump()
    assert f.agent('supervisor')['state'] == 'busy'


def test_max_rounds_counts_accepted_decisions_only(tmp_path):
    f = make(tmp_path, {'goal': 'write a report', 'budget': {'max_rounds': 2}})
    f.pump()
    f.handle(turn('supervisor', 'not json'))  # a rejected reply is not a round
    f.tick()
    assert not f.paused() and f.r.hget(fleet.BRAKE, 'rounds') == '0'
    f.pump()
    f.handle(turn('supervisor', decision()))
    f.tick()
    assert not f.paused()
    f.pump(); f.handle(turn('worker-1', 'Done: a'))
    f.pump(); f.handle(turn('supervisor', decision()))
    f.tick()
    assert f.paused() and 'max_rounds 2/2' in f.r.hget('fx:service', 'reason')


def test_budget_max_busy_only_tightens_concurrency(tmp_path):
    f = make(tmp_path, {'goal': 'two parts', 'budget': {'max_busy': 1}}, workers=('worker-1', 'worker-2'))
    f.pump()
    both = [{'to': 'worker-1', 'instruction': 'Write worker-1/a.md with part A'},
            {'to': 'worker-2', 'instruction': 'Write worker-2/b.md with part B'}]
    f.handle(turn('supervisor', decision(dispatch=both)))
    f.pump()
    assert [f.agent(w)['state'] for w in ('worker-1', 'worker-2')] == ['busy', 'idle']


def test_usd_equiv_pool_watches_equivalent_usd_and_unknown_never_trips(tmp_path):
    f = make(tmp_path, {'goal': 'x', 'budget': {'pool': 'pool-a', 'max_usd': 40}})
    f.tick()
    assert not f.paused() and f.r.hget(fleet.BRAKE, 'usd_base') == ''  # no quota.json yet: unknown
    quota(tmp_path, **{'pool-a': {'cash_used_usd': 100.0}})
    f.tick()  # first known reading is the baseline
    assert not f.paused() and float(f.r.hget(fleet.BRAKE, 'usd_base')) == 100.0
    quota(tmp_path, **{'pool-a': {'cash_used_usd': 139.0}})
    f.tick()
    assert not f.paused()
    quota(tmp_path, **{'pool-a': {'cash_used_usd': 140.5}})
    f.tick()
    assert f.paused() and 'max_usd 40.5/40' in f.r.hget('fx:service', 'reason')


def test_week_pct_pool_watches_weekly_quota_percent(tmp_path):
    quota(tmp_path, **{'pool-b': {'week_used_pct': 70}})
    f = make(tmp_path, {'goal': 'x', 'budget': {'pool': 'pool-b', 'max_week_pct': 85}})
    f.tick()
    assert not f.paused()
    quota(tmp_path, **{'pool-b': {'week_used_pct': 86}})
    f.tick()
    assert f.paused() and 'max_week_pct 86.0/85' in f.r.hget('fx:service', 'reason')


def test_task_rejects_bad_budget_and_service_budget_lives_in_config(tmp_path):
    f = make(tmp_path)
    assert 'max_usd applies only' in submit(f, {'goal': 'x', 'budget': {'pool': 'pool-b', 'max_usd': 5}})['reason']
    with pytest.raises(ValueError, match='config budget is for service mode'):
        fleet.Fleet(_cfg(tmp_path, budget={'max_calls': 1}), FakeRedis(), FakeMgr())
    with pytest.raises(ValueError, match='brake_notify must be an argv list'):
        fleet.Fleet(_cfg(tmp_path, brake_notify='rm -rf /'), FakeRedis(), FakeMgr())
    with pytest.raises(ValueError, match='max_calls must be a positive integer'):
        fleet.Fleet(_cfg(tmp_path, mode='service', budget={'max_calls': -1}), FakeRedis(), FakeMgr())


def _cfg(tmp_path, **extra):
    path = tmp_path / ('cfg-' + str(len(list(tmp_path.glob('cfg-*')))) + '.json')
    path.write_text(json.dumps(dict({'run': str(tmp_path / 'run'), 'redis_socket': '/tmp/x.sock',
                                     'python': sys.executable, 'workers': ['worker-1']}, **extra)))
    return path


def test_model_pool_wall_on_a_turn_pauses_records_downgrade_and_keeps_the_message(tmp_path):
    f = make(tmp_path, {'goal': 'x'}, worker_cli='claude', worker_model='fable')
    f.pump()
    f.handle(turn('supervisor', decision()))
    f.pump()
    sent = f.agent('worker-1')['last_msg']
    f.setf('worker-1', msg_id='m1')
    f.handle(dict(turn('worker-1', REAL_FABLE_WALL), msg_id='m1'))
    assert f.paused() and f.r.hget('fx:service', 'reason').startswith('brake: worker-1 hit a model_pool wall')
    wall = json.loads(f.agent('worker-1')['wall'])
    assert wall['action'] == 'switch' and wall['to'] == 'opus'  # fable is not on the worker chain: its head
    assert f.r.lrange('fx:inbox:worker-1', 0, -1) == [sent]  # redelivered after resume
    assert not f.r.llen('fx:inbox:supervisor')  # the wall text never becomes a report
    assert json.loads((f.run_dir / 'brake.json').read_text())['decision']['to'] == 'opus'
    assert control(f, 'resume', 'c1')['status'] == 'accepted'  # a wall pause resumes plainly
    assert not f.paused() and f.r.hget(fleet.BRAKE, 'tripped') == ''


def test_long_report_mentioning_limits_is_not_a_wall(tmp_path):
    f = make(tmp_path, {'goal': 'x'})
    f.pump(); f.handle(turn('supervisor', decision())); f.pump()
    report = 'Done: worker-1/report.md\n' + 'The vendor said "rate limited" and 429 Too Many Requests. ' * 10
    f.handle(turn('worker-1', report))
    assert not f.paused() and '[worker-1 report]' in f.r.lrange('fx:inbox:supervisor', 0, -1)[-1]


def test_account_wall_on_a_stalled_screen_stops(tmp_path):
    f = make(tmp_path, {'goal': 'x'}, stall_timeout=60, worker_cli='claude')  # claude account: the chain is spent
    f.pump(); f.handle(turn('supervisor', decision())); f.pump()
    f.mgr.capture_to = lambda pane, path: Path(path).write_text('working...\n' + CLAUDE_ACCOUNT_WALL + '\n> ')
    stale = time.time() - 3600
    f.setf('worker-1', busy_since=stale)
    path = write_rollout(f.sessions, THREADS['worker-1'], fleet.agent_cwd(f.cfg, 'worker-1'))
    os.utime(path, (stale, stale))
    f.check_stalls(time.time())
    assert f.paused()
    wall = json.loads(f.agent('worker-1')['wall'])
    assert wall['scope'] == 'account' and wall['action'] == 'stop'
    assert json.loads((f.run_dir / 'brake.json').read_text())['source'] == 'screen'


def test_next_task_starts_a_fresh_card_and_clears_a_brake_pause(tmp_path):
    f = make(tmp_path, {'goal': 'x', 'budget': {'max_calls': 1}})
    f.pump(); f.tick()
    assert f.paused()
    f.handle({'kind': 'stop', 'request_id': 's1'})
    f.stopping = False
    assert submit(f, {'goal': 'y'}, 't2')['status'] == 'accepted'
    assert not f.paused() and f.brake_budget() is None and f.r.hget(fleet.BRAKE, 'calls') == '0'


def test_operator_pause_is_not_cleared_by_a_new_task(tmp_path):
    f = make(tmp_path)
    control(f, 'pause', 'p1')
    submit(f, {'goal': 'y'})
    assert f.paused()


# ------------------------------------------------------------------ frozen acceptance contract
def art(path='worker-1/verdict.md', **extra):
    return dict({'path': path, 'contains': ['判决'], 'not_contains': ['TODO'],
                 'json_keys': ['verdict', 'p0']}, **extra)


@pytest.mark.parametrize('spec, error', [
    ({'goal': 'x', 'contract': {'artifacts_frozen': True}}, "non-empty artifacts"),
    ({'goal': 'x', 'contract': {'artifacts_frozen': True}, 'artifacts': []}, "non-empty artifacts"),
    ({'goal': 'x', 'contract': {'artifacts_frozen': 'yes'}, 'artifacts': [art()]}, 'artifacts_frozen'),
    ({'goal': 'x', 'contract': {'interfaces': True}, 'artifacts': [art()]}, 'artifacts_frozen'),
    ({'goal': 'x', 'contract': {'artifacts_frozen': True}, 'artifacts': [art(json_keys=[])]}, 'json_keys'),
    ({'goal': 'x', 'contract': {'artifacts_frozen': True}, 'artifacts': [art(cmd_exit='make test')]}, 'cmd_exit')])
def test_frozen_contract_spec_is_validated(tmp_path, spec, error):
    answer = submit(make(tmp_path), spec)
    assert answer['status'] == 'rejected' and error in answer['reason']


def test_frozen_artifacts_ignore_and_audit_supervisor_additions_and_use_extended_checks(tmp_path):
    f = make(tmp_path, {'goal': 'review', 'contract': {'artifacts_frozen': True}, 'artifacts': [art()]})
    assert 'frozen by the caller' in f.r.lrange('fx:inbox:supervisor', 0, 0)[0]
    out = f.workspace() / 'worker-1/verdict.md'
    out.parent.mkdir(parents=True, exist_ok=True)
    extra = [{'path': 'worker-1/easy.md'}]
    f.pump()
    f.handle(turn('supervisor', decision(artifacts=extra)))
    assert json.loads(f.task()['artifacts']) == [art()]
    logged = [json.loads(x) for x in f.r.lrange('fx:log', 0, -1)]
    assert any(x['kind'] == 'contract.append_ignored' and x['dropped'] == extra for x in logged)
    f.pump(); f.handle(turn('worker-1', 'Done'))
    steps = [('判决：PASS\nTODO fix\n{"verdict": "PASS", "p0": 0}', 'forbidden text'),
             ('判决：PASS\n{"verdict": "PASS"}', 'missing JSON key p0'),
             ('判决：PASS, no machine line', 'no JSON object')]
    for text, why in steps:
        f.r.hset('fx:task', mapping={'bounces': 0})  # three false completions block; each step is one
        out.write_text(text)
        f.pump()
        f.handle(turn('supervisor', decision('COMPLETED', 100, [])))
        assert f.task()['status'] == 'running', text
        assert why in f.r.lrange('fx:inbox:supervisor', 0, -1)[-1]
    f.r.hset('fx:task', mapping={'bounces': 0})
    out.write_text('# 复核\n判决：PASS\n```json\n{"verdict": "PASS", "p0": 0, "p1": 0}\n```')
    f.pump()
    f.handle(turn('supervisor', decision('COMPLETED', 100, [], artifacts=extra)))
    assert f.task()['status'] == 'completed'
    f.write_result()
    result = json.loads((f.run_dir / 'result.json').read_text())
    assert [a['path'] for a in result['artifacts']] == ['worker-1/verdict.md'] and result['artifacts'][0]['problems'] == []


def test_frozen_artifact_cmd_exit_runs_the_callers_command(tmp_path):
    check = {'path': 'checks/tests.txt', 'cmd_exit': [sys.executable, '-c', 'print("7 passed")'],
             'contains': ['passed'], 'not_contains': ['failed']}
    f = make(tmp_path, {'goal': 'x', 'contract': {'artifacts_frozen': True}, 'artifacts': [check]})
    f.pump()
    f.handle(turn('supervisor', decision('COMPLETED', 100, [])))
    assert f.task()['status'] == 'completed'
    assert (f.workspace() / 'checks/tests.txt').read_text().strip() == '7 passed'


def test_unfrozen_task_keeps_old_artifact_rules(tmp_path):
    """Old tasks: supervisor additions still merge; json_keys / not_contains stay ignored."""
    f = make(tmp_path, {'goal': 'x', 'artifacts': [art()]})
    (f.workspace() / 'worker-1').mkdir(parents=True, exist_ok=True)
    (f.workspace() / 'worker-1/verdict.md').write_text('判决 TODO, no JSON')
    (f.workspace() / 'worker-1/easy.md').write_text('ok')
    f.pump()
    f.handle(turn('supervisor', decision(artifacts=[{'path': 'worker-1/easy.md'}])))
    assert len(json.loads(f.task()['artifacts'])) == 2
    f.pump(); f.handle(turn('worker-1', 'Done'))
    f.pump(); f.handle(turn('supervisor', decision('COMPLETED', 100, [])))
    assert f.task()['status'] == 'completed'


def test_json_key_failures_reads_whole_file_or_final_object():
    assert fleet.json_key_failures('{"a": 1, "b": 2}', ['a', 'b']) == []
    assert fleet.json_key_failures('text {"x": 1} more\n{"a": 1}', ['a']) == []
    assert fleet.json_key_failures('[1, 2]', ['a']) == ['no JSON object (whole file or final line)']
    assert fleet.json_key_failures('{"a": 1}\ntrailing words', ['a']) == ['no JSON object (whole file or final line)']


# ------------------------------------------------------------------ v2 transport (real Redis)
def test_v2_task_budget_pauses_in_a_transaction_and_resume_raises(fx):
    f = fx
    f.service = False; f.cfg['mode'] = 'task'
    event(f, dict(kind='task', request_id='new-task', spec={
        'goal': 'write an evidence report', 'budget': {'max_calls': 1, 'max_busy': 1}}))
    f.pump(); env = receive(f, 'supervisor')
    assert f.r.hget(fleet.BRAKE, 'calls') == '1'
    f.tick()
    assert f.paused() and f.task()['status'] == 'running'
    audit = [x['event'] for _, x in f.r.xrange(f.v2.bus.audit_key)]
    assert 'brake.tripped' in audit and (f.run_dir / 'brake.json').exists()
    settle(f, 'supervisor', env, {'status': 'RUNNING', 'completion': 20, 'reason': 'go', 'dispatch': [
        {'to': 'worker-1', 'instruction': 'Write worker-1/report.md with Conclusion evidence.'}]})
    assert f.r.hget(fleet.BRAKE, 'rounds') == '1'
    f.pump()
    assert f.agent('worker-1')['state'] == 'idle' and f.v2.depth('worker-1') == 1  # held by the pause
    event(f, dict(kind='resume', request_id='res-1'))
    assert f.paused()
    event(f, dict(kind='resume', request_id='res-2', **{'raise': ['max_calls=5']}))
    assert not f.paused() and f.brake_budget()['max_calls'] == 5
    f.pump()
    assert f.agent('worker-1')['state'] == 'busy'


def test_start_budget_flag_is_service_only_and_validated(capsys):
    with pytest.raises(SystemExit, match='--budget is the service budget card'):
        fleet.main(['start', '--budget', '{"max_calls": 1}'])
    with pytest.raises(SystemExit, match='--budget rejected: budget max_usd applies only to a pool with meter usd_equiv'):
        fleet.main(['start', '--mode', 'service', '--budget', '{"max_usd": 1}'])


def test_service_budget_card_comes_from_config(tmp_path):
    f = make(tmp_path, mode='service', budget={'max_calls': 1})
    f.tick()  # first tick starts the service card
    assert f.brake_budget() == {'max_calls': 1} and not f.paused()
    f.handle({'kind': 'request', 'request_id': 'q1', 'spec': {
        'id': 'r1', 'payload': {'m': 'hi'}, 'reply': {'required': ['reply']}}})
    f.pump()
    f.tick()
    assert f.paused() and 'max_calls 1/1' in f.r.hget('fx:service', 'reason')


def test_v2_stalled_screen_wall_pauses_before_the_slot_is_held(fx):
    """v2 never calls Fleet.on_stall: the wall scan runs in the brake transaction of Fleet.tick."""
    f = fx
    f.cfg['claude'] = '/nonexistent/claude'  # the chain's claude preflight never reads the host PATH
    f.service = False; f.cfg['mode'] = 'task'; f.cfg['stall_timeout'] = 60
    event(f, dict(kind='task', request_id='new-task', spec={'goal': 'write an evidence report'}))
    f.pump(); receive(f, 'supervisor')
    f.mgr.capture_to = lambda pane, path: Path(path).write_text('thinking...\n' + REAL_CODEX_WALL + '\n')
    stale = time.time() - 3600
    f.setf('supervisor', busy_since=stale)
    transcript = Path(f.agent('supervisor')['transcript'])
    os.utime(transcript, (stale, stale))
    f.tick()
    assert f.paused() and f.task()['status'] == 'running'
    assert json.loads(f.agent('supervisor')['wall'])['scope'] == 'account'
    audit = [x for _, x in f.r.xrange(f.v2.bus.audit_key) if x['event'] == 'brake.tripped']
    assert audit and json.loads((f.run_dir / 'brake.json').read_text())['source'] == 'screen'


# ------------------------------------------------------------------ E before F (watchdog wall gate)
TRUST_DIALOG = 'Do you trust the files in this folder?\n❯ 1. Yes, proceed\n  2. No, exit'


def test_screen_wall_reads_the_bottom_of_the_screen_and_beats_a_dialog():
    assert brake.screen_wall(REAL_FABLE_WALL + '\n' + TRUST_DIALOG)['scope'] == 'model_pool'
    assert brake.screen_wall('❯ 1. Yes, switch to Sonnet\n' + REAL_CODEX_WALL)['scope'] == 'account'
    assert brake.screen_wall(REAL_CODEX_WALL + '\n' + 'scrolled work line\n' * 30) is None  # old scrollback
    assert brake.screen_wall(TRUST_DIALOG) is None  # no wall: the watchdog's own table decides


def test_wall_gate_trips_before_the_watchdog_presses_anything(tmp_path):
    f = make(tmp_path, {'goal': 'x'}, worker_cli='claude', worker_model='fable')
    f.pump(); f.handle(turn('supervisor', decision())); f.pump()
    assert f.wall_gate('worker-1', 'busy...\n' + TRUST_DIALOG) is None and not f.paused()
    record = f.wall_gate('worker-1', 'busy...\n' + REAL_FABLE_WALL + '\n' + TRUST_DIALOG)
    assert record['kind'] == 'wall' and record['source'] == 'watch' and record['decision']['to'] == 'opus'
    assert f.paused() and f.r.hget('fx:service', 'reason').startswith('brake: worker-1 hit a model_pool wall')
    assert f.mgr.keys == [] and f.agent('worker-1')['state'] == 'busy'  # nothing pressed, slot not held
    assert f.wall_gate('worker-1', REAL_FABLE_WALL)['at'] == record['at']  # idempotent: no second trip
    assert sum(json.loads(x)['kind'] == 'brake.tripped' for x in f.r.lrange(fleet.LOG, 0, -1)) == 1
    assert json.loads((f.run_dir / 'brake.json').read_text())['source'] == 'watch'


def test_hold_release_never_resumes_a_brake_pause(tmp_path):
    """F's unknown-dialog hold rewrites the pause reason and its turn_ended release only frees the slot:
    the brake record survives both, dispatch stays paused, a plain resume stays refused."""
    f = make(tmp_path, {'goal': 'x', 'budget': {'max_calls': 2}})
    f.pump(); f.handle(turn('supervisor', decision())); f.pump()
    f.tick()
    assert f.paused() and json.loads(f.r.hget(fleet.BRAKE, 'tripped'))['kind'] == 'budget'
    f.pause('worker-1: unknown confirmation dialog on screen (do you want to proceed?); nothing pressed')
    f.setf('worker-1', state='idle', idle_confirmed=1)  # what RuntimeV2.release_held leaves behind
    f.handle(turn('supervisor', decision()))
    f.tick(); f.pump()
    assert f.paused() and f.agent('worker-1')['state'] == 'idle' and f.agent('supervisor')['state'] == 'idle'
    answer = control(f, 'resume', 'c1')
    assert answer['status'] == 'rejected' and 'max_calls 2/2' in answer['reason'] and f.paused()


def test_a_later_wall_trip_does_not_unlock_a_budget_pause(tmp_path):
    f = make(tmp_path, {'goal': 'x', 'budget': {'max_calls': 2}}, worker_cli='claude', worker_model='fable')
    f.pump(); f.handle(turn('supervisor', decision())); f.pump()
    f.tick()
    assert f.wall_gate('worker-1', REAL_FABLE_WALL)['kind'] == 'wall'  # overwrites the trip record
    assert control(f, 'resume', 'c1')['status'] == 'rejected' and f.paused()
    assert control(f, 'resume', 'c2', **{'raise': ['max_calls=9']})['status'] == 'accepted' and not f.paused()


def test_v2_wall_gate_trips_in_a_transaction_without_holding_the_slot(fx):
    f = fx
    f.cfg['claude'] = '/nonexistent/claude'  # the chain's claude preflight never reads the host PATH
    f.service = False; f.cfg['mode'] = 'task'
    event(f, dict(kind='task', request_id='new-task', spec={'goal': 'write an evidence report'}))
    f.pump(); receive(f, 'supervisor')
    record = f.wall_gate('supervisor', 'thinking...\n' + REAL_CODEX_WALL + '\n❯ 1. Yes')
    assert record['kind'] == 'wall' and record['wall']['scope'] == 'account'
    assert f.paused() and f.task()['status'] == 'running' and f.agent('supervisor')['state'] != 'held'
    assert f.r.hget(f.v2.bus.lease_key('supervisor'), 'status') == 'received' and f.mgr.keys == []
    audit = [x for _, x in f.r.xrange(f.v2.bus.audit_key) if x['event'] == 'brake.tripped']
    assert len(audit) == 1 and json.loads((f.run_dir / 'brake.json').read_text())['source'] == 'watch'


def test_frozen_task_rejects_a_supervisor_cmd_exit_and_runs_only_the_callers(tmp_path):
    caller, added = tmp_path / 'caller', tmp_path / 'added'
    run = lambda m: [sys.executable, '-c', 'open(%r, "a").write("x"); print("7 passed")' % str(m)]
    check = {'path': 'checks/tests.txt', 'cmd_exit': run(caller), 'contains': ['passed']}
    f = make(tmp_path, {'goal': 'x', 'contract': {'artifacts_frozen': True}, 'artifacts': [check]})
    f.pump()
    f.handle(turn('supervisor', decision('COMPLETED', 100, [], artifacts=[
        {'path': 'checks/extra.txt', 'cmd_exit': run(added)}])))
    assert 'was not declared by the caller' in f.r.lrange('fx:inbox:supervisor', 0, -1)[-1]
    assert not caller.exists() and not added.exists() and f.task()['status'] == 'running'
    f.pump()
    f.handle(turn('supervisor', decision('COMPLETED', 100, [], artifacts=[check])))
    assert f.task()['status'] == 'completed' and caller.read_text() == 'x' and not added.exists()


# ------------------------------------------------------------------ walls: startup, downgrade, requeue once
CLAUDE_ACCOUNT_WALL = 'Claude usage limit reached. Your limit will reset at 5pm (America/New_York).'


def busy_worker(f):
    f.pump(); f.handle(turn('supervisor', decision())); f.pump()
    f.setf('worker-1', msg_id='m1')
    return f.agent('worker-1')['last_msg']


def test_v1_model_pool_wall_runs_the_downgrade_and_lifts_its_own_pause(tmp_path):
    f = make(tmp_path, {'goal': 'x'}, worker_cli='claude', worker_model='fable')
    sent = busy_worker(f)
    f.handle(dict(turn('worker-1', REAL_FABLE_WALL), msg_id='m1'))
    f.handle(dict(turn('worker-1', REAL_FABLE_WALL), msg_id='m1'))  # duplicate hook: dropped, not requeued twice
    assert f.paused() and f.r.lrange('fx:inbox:worker-1', 0, -1) == [sent]
    assert json.loads(f.agent('worker-1')['wall_downgrade'])['to'] == 'opus'
    old = f.agent('worker-1')['pane']
    f.tick()  # idle boundary: next generation on the next chain model
    info = f.agent('worker-1')
    assert info['generation'] == '1' and info['label'] == 'worker-1.1' and info['state'] == 'starting'
    assert f.runtime_cfg('worker-1')['worker_model'] == 'opus' and old in f.mgr.killed
    assert f.paused() and f.r.lrange('fx:inbox:worker-1', 0, -1) == [sent]  # queue kept for the new generation
    f.handle(turn('worker-1', 'READY', generation=1))
    f.tick()
    assert not f.paused() and f.agent('worker-1')['wall_downgrade'] == ''
    assert any(json.loads(x)['kind'] == 'brake.downgraded' and json.loads(x)['resumed'] for x in f.r.lrange(fleet.LOG, 0, -1))
    f.pump()
    assert f.agent('worker-1')['state'] == 'busy' and f.r.llen('fx:inbox:worker-1') == 0


def test_downgrade_keeps_a_pause_that_is_not_the_walls_own(tmp_path):
    f = make(tmp_path, {'goal': 'x'}, worker_cli='claude', worker_model='fable')
    busy_worker(f)
    f.handle(dict(turn('worker-1', REAL_FABLE_WALL), msg_id='m1'))
    f.pause('worker-1: unknown confirmation dialog on screen; nothing pressed')  # F's hold rewrites the reason
    f.tick()
    f.handle(turn('worker-1', 'READY', generation=1))
    f.tick()
    assert f.agent('worker-1')['wall_downgrade'] == '' and f.paused()  # downgraded, still paused for the human


def test_supervisor_and_account_walls_stop_without_a_downgrade(tmp_path):
    f = make(tmp_path, {'goal': 'x'}, worker_cli='claude', worker_model='fable', supervisor_model='fable')
    busy_worker(f)
    f.handle(dict(turn('worker-1', CLAUDE_ACCOUNT_WALL), msg_id='m1'))
    assert f.paused() and json.loads(f.agent('worker-1')['wall'])['action'] == 'stop'
    assert not f.agent('worker-1').get('wall_downgrade')
    f.tick()
    assert f.agent('worker-1')['generation'] == '0' and f.paused()  # account wall: stays paused, nothing spawned
    # The supervisor's chain (fable > opus) sits on the Claude account worker-1 just found walled: stop.
    f.on_wall('supervisor', REAL_FABLE_WALL, 'turn')
    wall = json.loads(f.agent('supervisor')['wall'])
    assert wall['action'] == 'stop' and not f.agent('supervisor').get('wall_downgrade')
    assert wall['skipped'] == ['claude:fable (account wall)', 'claude:opus (account wall)']


def test_v1_codex_supervisor_wall_cannot_take_a_claude_supervisor(tmp_path):
    # The chain offers claude fable / opus, but a Claude supervisor needs transport v2: the preflight refuses both.
    f = make(tmp_path, {'goal': 'x'}, worker_cli='claude')
    f.on_wall('supervisor', REAL_FABLE_WALL, 'turn')
    assert json.loads(f.agent('supervisor')['wall_downgrade'])['to'] == 'fable'
    f.tick()
    assert json.loads(f.agent('supervisor')['wall'])['action'] == 'stop' and f.agent('supervisor')['wall_downgrade'] == ''
    assert f.agent('supervisor')['generation'] == '0' and f.paused()


def test_v1_startup_wall_is_walled_paused_and_retried_after_resume(tmp_path):
    f = make(tmp_path, {'goal': 'x'}, worker_cli='claude', worker_model='claude-haiku-4-5')
    f.setf('worker-1', state='starting')  # a new generation answering its startup prompt
    f.handle(turn('worker-1', CLAUDE_ACCOUNT_WALL))
    assert f.agent('worker-1')['state'] == 'walled' and f.paused() and f.task()['status'] == 'running'
    assert json.loads((f.run_dir / 'brake.json').read_text())['source'] == 'startup'
    f.tick()
    assert f.agent('worker-1')['generation'] == '0'  # paused: no retry yet
    assert control(f, 'resume', 'c1')['status'] == 'accepted'
    f.tick()
    assert f.agent('worker-1')['generation'] == '1' and f.agent('worker-1')['state'] == 'starting'
    f.handle(turn('worker-1', 'READY', generation=1))
    assert f.agent('worker-1')['state'] == 'idle'


def test_v1_startup_non_wall_failure_still_blocks(tmp_path):
    f = make(tmp_path, {'goal': 'x'})
    f.setf('worker-1', state='starting')
    f.handle(turn('worker-1', 'I could not find AGENTS.md'))
    assert f.task()['status'] == 'blocked' and not f.paused()


def test_default_usd_equiv_cash_cap_day_and_night(tmp_path):
    assert brake.effective({'pool': 'pool-a'}) == {'pool': 'pool-a', 'max_usd': 40}
    assert brake.effective({'pool': 'pool-a'}, night=True)['max_usd'] == 10
    assert brake.effective({'pool': 'pool-a', 'max_usd': 5}, night=True)['max_usd'] == 5
    assert brake.effective({'pool': 'pool-b'}) == {'pool': 'pool-b'}
    f = make(tmp_path, {'goal': 'x', 'budget': {'pool': 'pool-a'}})
    f.tick()
    assert not f.paused()  # no quota.json: unknown, never trips
    quota(tmp_path, **{'pool-a': {'cash_used_usd': 100}}); f.tick()
    quota(tmp_path, **{'pool-a': {'cash_used_usd': 111}}); f.r.hset('fx:night', mapping={'brake_card': 'night'}); f.tick()
    assert f.paused() and 'max_usd 11.0/10' in f.r.hget('fx:service', 'reason')
    assert json.loads((f.run_dir / 'brake.json').read_text())['effective_budget']['max_usd'] == 10


# ---- v2 (real Redis): Pi model error event, startup wall
PI_WALL = '429 Too Many Requests: usage limit reached for this account'


def test_v2_pi_model_error_wall_commits_walled_and_requeues_once(fx):
    f = fx
    f.cfg['claude'] = '/nonexistent/claude'  # the chain's claude preflight never reads the host PATH
    req(f, 'r1'); f.pump(); slot = 'worker-1'
    env = receive(f, slot)
    event(f, dict(session(f, slot), kind='candidate', delivery=env, candidate={'tool': 'model', 'error': PI_WALL}))
    ident = event(f, dict(session(f, slot), kind='settled', delivery=env, candidates=[], failed=True, aborted=False,
                          unfinished=False))
    msg = f.v2.bus.message(env['message_id'])
    assert msg['status'] == 'committed' and msg['outcome'] == 'walled'
    assert f.agent(slot)['state'] == 'idle' and f.paused()  # not an unconfirmed hold: the turn ended
    assert f.v2.depth(slot) == 1
    event(f, dict(session(f, slot), kind='settled', delivery=env, candidates=[], failed=True, aborted=False,
                  unfinished=False))  # replay: settle.late
    assert f.v2.depth(slot) == 1
    f.tick()
    assert json.loads((f.run_dir / 'brake.json').read_text())['source'] == 'turn'
    audit = [x['event'] for _, x in f.r.xrange(f.v2.bus.audit_key)]
    assert 'brake.wall_requeued' in audit and 'execution.held' not in audit and ident


def test_v2_pi_failure_without_a_wall_still_holds(fx):
    f = fx
    req(f, 'r1'); f.pump(); slot = 'worker-1'
    env = receive(f, slot)
    event(f, dict(session(f, slot), kind='candidate', delivery=env, candidate={'tool': 'model', 'error': 'socket hang up'}))
    event(f, dict(session(f, slot), kind='settled', delivery=env, candidates=[], failed=True, aborted=False, unfinished=False))
    assert f.agent(slot)['state'] == 'held'


def test_v2_startup_wall_is_walled_not_blocked(fx):
    f = fx
    f.cfg['claude'] = '/nonexistent/claude'  # the chain's claude preflight never reads the host PATH
    f.service = False; f.cfg['mode'] = 'task'
    event(f, dict(kind='task', request_id='new-task', spec={'goal': 'write an evidence report'}))
    f.setf('worker-2', state='starting', launch_ready=1)
    event(f, dict(session(f, 'worker-2'), kind='startup', request_id='ready-again', text='', failed=True,
                  aborted=False, error=PI_WALL))
    assert f.agent('worker-2')['state'] == 'walled' and f.paused() and f.task()['status'] == 'running'
    f.tick()
    assert json.loads((f.run_dir / 'brake.json').read_text())['source'] == 'startup'


# ---- v2 integration: Codex supervisor + official Claude workers, real hook entry points
from test_v2_codex_claude import mixed, answer_turn, typed  # noqa: E402,F401
from test_v2_codex_claude import submit as mixed_submit, decision as mixed_decision  # noqa: E402


def test_v2_claude_pool_wall_short_reply_downgrades_once_and_resumes(mixed):
    f = mixed
    f.cfg['worker_model'] = 'fable'
    mixed_submit(f)
    answer_turn(f, 'supervisor', mixed_decision(['worker-1']))
    f.pump()
    instruction = typed(f, 'worker-1')[-1]
    env = f.v2.bus.slot_state('worker-1')
    assert answer_turn(f, 'worker-1', REAL_FABLE_WALL)['status'] == 'committed'  # short reply, no stall wait
    msg = f.v2.bus.message(env['message_id'])
    assert msg['outcome'] == 'walled' and f.paused() and f.agent('worker-1')['state'] == 'idle'
    assert answer_turn(f, 'worker-1', REAL_FABLE_WALL, prompt=instruction)['status'] == 'ignored'  # late duplicate
    assert f.v2.depth('worker-1') == 1 and not any('[worker-1 report]' in t for t in typed(f, 'supervisor'))
    old = f.agent('worker-1')['pane']
    f.tick()
    assert f.agent('worker-1')['generation'] == '1' and old in f.mgr.killed
    assert f.runtime_cfg('worker-1')['worker_model'] == 'opus'
    assert '"profile": "opus"' in json.dumps(f.mgr.spawned[-1], default=str)
    assert answer_turn(f, 'worker-1', 'READY', startup=True)['status'] == 'ready'
    f.tick()
    assert not f.paused()
    f.pump()
    resent = typed(f, 'worker-1')
    assert len(resent) == 1 and resent[0].startswith('[Resumed after a usage wall]')
    assert 'Write worker-1/part.md' in resent[0]
    report = 'Done:\n- wrote part.md\nEvidence:\n- worker-1/part.md\nNot done: none\nBlocked: none'
    assert answer_turn(f, 'worker-1', report)['status'] == 'committed'
    f.pump()
    reports = [t for t in typed(f, 'supervisor') if t.startswith('[worker-1')]
    assert len(reports) == 1  # one report for the instruction: the wall turn never became one


def test_v2_claude_account_wall_stays_paused_and_hold_release_cannot_resume(mixed):
    f = mixed
    mixed_submit(f)
    answer_turn(f, 'supervisor', mixed_decision(['worker-1', 'worker-2']))
    f.pump()
    assert answer_turn(f, 'worker-1', CLAUDE_ACCOUNT_WALL)['status'] == 'committed'
    assert f.paused() and json.loads(f.agent('worker-1')['wall'])['action'] == 'stop'
    f.tick()
    assert f.agent('worker-1')['generation'] == '0' and f.paused()  # no downgrade for an account wall
    # F's unknown-dialog hold on worker-2, then its turn_ended release: the slot frees, the pause stays.
    f.v2.transaction('hold-test', lambda: f.v2.hold('worker-2', 'unknown confirmation dialog'))
    f.setf('worker-2', state='idle', idle_confirmed=1)
    f.tick(); f.pump()
    assert f.paused() and len(typed(f, 'worker-1')) == 1
    assert f.wall_gate('worker-2', 'Do you trust the files in this folder?\n❯ 1. Yes') is None


def test_tool_errors_on_a_worker_or_supervisor_screen_do_not_pause(tmp_path):
    f = make(tmp_path, {'goal': 'x'}, worker_cli='claude', worker_model='fable')
    f.pump(); f.handle(turn('supervisor', decision())); f.pump()
    for text in TOOL_ERRORS:  # last lines of the screen, where the gate looks
        for slot in ('worker-1', 'supervisor'):  # a worker report pasted into the supervisor pane
            assert f.wall_gate(slot, 'working...\n' + text + '\n> ') is None and not f.paused()
    assert f.wall_gate('supervisor', 'thinking...\n' + REAL_CODEX_WALL)['wall']['scope'] == 'account'
    assert f.paused()  # the CLI's own wording still trips


def test_v2_short_reply_with_tool_errors_is_a_report_not_a_wall(mixed):
    f = mixed
    mixed_submit(f)
    answer_turn(f, 'supervisor', mixed_decision(['worker-1', 'worker-2']))
    f.pump()
    env = f.v2.bus.slot_state('worker-1')
    assert answer_turn(f, 'worker-1', '\n'.join(TOOL_ERRORS))['status'] == 'committed'  # ≤400 chars
    assert f.v2.bus.message(env['message_id'])['outcome'] != 'walled' and not f.paused()
    assert not f.agent('worker-1').get('wall')
    assert answer_turn(f, 'worker-2', CLAUDE_ACCOUNT_WALL)['status'] == 'committed'
    assert f.paused() and json.loads(f.agent('worker-2')['wall'])['action'] == 'stop'


def test_v2_claude_startup_wall_then_resume_retries_the_same_launch(mixed):
    f = mixed
    mixed_submit(f)
    assert f.wall_rotate('worker-2', None, 'test rotation')
    assert answer_turn(f, 'worker-2', CLAUDE_ACCOUNT_WALL, startup=True)['status'] == 'rejected'
    assert f.agent('worker-2')['state'] == 'walled' and f.paused() and f.task()['status'] == 'running'
    f.tick()
    assert f.agent('worker-2')['generation'] == '1'
    runtime_v2.post_event(f.v2.bus, dict(kind='resume', request_id='res-1'), 'res-1')
    assert f.v2.step(10) and not f.paused()
    f.tick()
    assert f.agent('worker-2')['generation'] == '2' and f.agent('worker-2')['state'] == 'starting'
    assert answer_turn(f, 'worker-2', 'READY', startup=True)['status'] == 'ready'


def test_account_wall_at_the_downgraded_startup_is_retried_after_resume(tmp_path):
    f = make(tmp_path, {'goal': 'x'}, worker_cli='claude', worker_model='fable')
    busy_worker(f)
    f.handle(dict(turn('worker-1', REAL_FABLE_WALL), msg_id='m1'))
    f.tick()  # chain: generation 1 starting on opus
    f.handle(turn('worker-1', CLAUDE_ACCOUNT_WALL, generation=1))
    assert f.agent('worker-1')['state'] == 'walled' and f.agent('worker-1')['wall_downgrade'] == ''
    f.tick()
    assert f.agent('worker-1')['generation'] == '1'
    assert control(f, 'resume', 'c1')['status'] == 'accepted'
    f.tick()
    assert f.agent('worker-1')['generation'] == '2' and f.runtime_cfg('worker-1')['worker_model'] == 'opus'


def test_screen_trip_then_turn_trip_still_lifts_the_walls_own_pause(tmp_path):
    f = make(tmp_path, {'goal': 'x'}, worker_cli='claude', worker_model='fable')
    busy_worker(f)
    assert f.wall_gate('worker-1', 'thinking\n⎿  You\'ve reached your Fable limit · resets 6pm')  # F screen first
    f.handle(dict(turn('worker-1', REAL_FABLE_WALL), msg_id='m1'))  # then the turn text (another line)
    f.tick()
    f.handle(turn('worker-1', 'READY', generation=1))
    f.tick()
    assert not f.paused() and f.agent('worker-1')['generation'] == '1'


def test_pending_downgrade_never_undoes_a_profile_swap(tmp_path):
    f = make(tmp_path, {'goal': 'x'}, worker_cli='claude', worker_model='fable')
    busy_worker(f)
    f.handle(dict(turn('worker-1', REAL_FABLE_WALL), msg_id='m1'))
    f.setf('worker-1', runtime_launch=json.dumps({'profile_id': 'flashnext-coder', 'role_text': '',
                                                  'overrides': {'worker_cli': 'pi', 'pi_model': 'omlx/flashnext'}}))
    f.tick()  # B's night swap got there first
    assert f.agent('worker-1')['generation'] == '0' and f.agent('worker-1')['wall_downgrade'] == ''
    assert any(json.loads(x)['kind'] == 'brake.downgrade_superseded' for x in f.r.lrange(fleet.LOG, 0, -1))


def test_pool_names_come_from_the_pools_file(pools_file, tmp_path, monkeypatch):
    assert brake.validate_budget({'pool': 'old-a', 'max_usd': 3}) == {'pool': 'old-a', 'max_usd': 3}  # alias
    now = time.time()
    assert brake.pool_reading({'pools': {'pool-a': {'cash_used_usd': 7, 'at': now}}}, 'old-a', now) == 7.0
    assert brake.effective({'pool': 'pool-c'}) == {'pool': 'pool-c', 'max_usd': 25}  # the pool's own caps
    assert brake.effective({'pool': 'pool-c'}, night=True)['max_usd'] == 5
    monkeypatch.setenv(fleet_config.USER_ENV, str(tmp_path / 'missing.yaml'))
    with pytest.raises(ValueError, match='is not an account id or alias'):
        brake.validate_budget({'pool': 'pool-a'})
    assert brake.validate_budget({'max_calls': 2}) == {'max_calls': 2}  # no pool: no pools file needed
    dup = tmp_path / 'dup.yaml'
    dup.write_text('accounts:\n  - {id: a, aliases: [b]}\n  - {id: b}\n')  # an explicit file: blocks format
    with pytest.raises(ValueError, match='used twice'):
        brake.pool_accounts(dup)



def test_codex_usage_limit_wording_is_an_account_wall():
    """2026-11-09 the Astra supervisor (pi / openai-codex) stopped on this exact line; the chain must see it."""
    for text in ('Codex error: The usage limit has been reached', 'The usage limit has been reached',
                 'You have hit your ChatGPT usage limit (plus plan). Try again in ~42 min.'):
        got = brake.classify_wall(text)
        assert got and got['scope'] == 'account', text
