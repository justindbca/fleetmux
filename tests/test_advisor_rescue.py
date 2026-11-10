"""Advisor mid-task rescue (advisor plan §3.2 / §4.2 / §6 / §8): consult -> respawn | reassign | give_up.

The advisor process is always a fake runner: no test starts `claude`."""
import json
from pathlib import Path

import pytest

from test_advisor import Runner, advisor_on, afx, item, report, send_decision, supervisor_texts, v2adv2, worker_text  # noqa: F401
from test_fleet import decision, fx, turn  # noqa: F401
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from cmux_fleet import advisor, claude_hook, fleet, handoff

FINDING = '两轮都把 Reddit 抓成 403；产出只有标题没有正文'


def consult(worker='worker-1', task='M0', finding=FINDING, want='补能抓 Reddit 的工具'):
    return {'worker': worker, 'task': task, 'finding': finding, 'want': want}


def rescue_decision(*items, dispatch=None):
    return decision(dispatch=dispatch or [], consult=list(items))


def worked(f, name='worker-1', task='M0', secret='worker private chatter SECRET'):
    """Dispatch task M0 to `name`, deliver it (pre-dispatch advice: unchanged runtime) and report back."""
    send_decision(f, [dict(item(name, 'Research the semis rotation and write ' + name + '/notes.md'), id=task)])
    f.pump()
    f.handle(dict(turn(name, 'Done:\n- titles only ' + secret + '\nEvidence:\n- x\nNot done: bodies\nBlocked: none',
                       int(f.agent(name)['generation'])), msg_id=f.agent(name)['msg_id']))


def notes_and_inbox(f):
    return '\n'.join(supervisor_texts(f) + f.r.lrange(fleet.ADVICE_NOTES, 0, -1))


# ------------------------------------------------------------------ pure: parsing and validation
@pytest.mark.parametrize('bad, match', [
    ({'worker': 'worker-9', 'task': 'M0', 'finding': 'x'}, 'consult.worker'),
    ({'worker': 'worker-1', 'task': 'zero', 'finding': 'x'}, 'consult.task'),
    ({'worker': 'worker-1', 'task': 'M0', 'finding': '  '}, 'consult.finding'),
    ({'worker': 'worker-1', 'task': 'M0'}, 'consult.finding'),
    ({'worker': 'worker-1', 'task': 'M0', 'finding': 'x', 'want': 3}, 'consult.want'),
])
def test_parse_decision_validates_consult_items(bad, match):
    with pytest.raises(ValueError, match=match):
        fleet.parse_decision(rescue_decision(bad), ['worker-1'], False)
    with pytest.raises(ValueError, match='consult must be a list'):
        fleet.parse_decision(decision(dispatch=[], consult={'worker': 'worker-1'}), ['worker-1'], False)
    with pytest.raises(ValueError, match='one item per task'):
        fleet.parse_decision(rescue_decision(consult(), consult()), ['worker-1'], False)


def test_running_with_consult_needs_no_dispatch():
    with pytest.raises(ValueError, match='RUNNING must dispatch'):
        fleet.parse_decision(decision(dispatch=[]), ['worker-1'], False)
    out = fleet.parse_decision(rescue_decision(consult()), ['worker-1'], False)
    assert out['consult'][0]['task'] == 'M0' and out['dispatch'] == []
    assert fleet.parse_decision(decision(), ['worker-1'], False)['consult'] == []  # absent = empty


def test_rescue_actions_and_relay_cap():
    assert advisor.rescue_actions(0, ['worker-2']) == ['respawn', 'reassign', 'give_up']
    assert advisor.rescue_actions(1, []) == ['respawn', 'give_up']
    assert advisor.rescue_actions(2, ['worker-2']) == ['reassign', 'give_up']  # third time: no respawn
    assert advisor.rescue_actions(2, []) == ['give_up']


def test_validate_rescue_branches():
    def slot(target, raw):
        return {'model': raw['model'], 'effort': 'medium', 'domains': [], 'reason': raw['reason'], 'slot': target}
    v = lambda raw, actions=('respawn', 'reassign', 'give_up'), c=('worker-2',): advisor.validate_rescue(  # noqa: E731
        raw, actions=list(actions), check_slot=slot, candidates=list(c))
    with pytest.raises(advisor.AdviceError, match='action must be one of'):
        v({'action': 'respawn', 'model': 'opus', 'reason': 'x'}, actions=('reassign', 'give_up'))
    with pytest.raises(advisor.AdviceError, match='reason'):
        v({'action': 'give_up'})
    with pytest.raises(advisor.AdviceError, match='idle slots'):
        v({'action': 'reassign', 'to': 'worker-7', 'model': 'opus', 'reason': 'x'})
    with pytest.raises(advisor.AdviceError, match='handoff_hint'):
        v({'action': 'respawn', 'model': 'opus', 'reason': 'x', 'handoff_hint': 1})
    assert v({'action': 'give_up', 'reason': ' needs a human '}) == {'action': 'give_up', 'reason': 'needs a human'}
    out = v({'action': 'reassign', 'model': 'opus', 'reason': 'x', 'handoff_hint': 'dropped'})
    assert out['to'] == 'worker-2' and out['slot'] == 'worker-2' and 'handoff_hint' not in out
    out = v({'action': 'respawn', 'model': 'opus', 'reason': 'x', 'handoff_hint': 'check links'})
    assert out['slot'] is None and out['handoff_hint'] == 'check links' and 'to' not in out


def test_handoff_hint_survives_the_model_merge():
    base = handoff.build_skeleton('h1', 'claude', 'goal', next_lines=['skeleton step'])
    merged = handoff.merge_partial(base, '## Done (facts)\n- a\n## In progress\nnone\n## Next steps (in order)\n1. model step\n')
    out = handoff.add_next_step(merged, 'HINT: check the 7 links first')
    assert 'model step' in out and '- HINT: check the 7 links first' in out and 'skeleton step' not in out
    assert out.index('HINT') < out.index('## Needs a human decision') and handoff.validate.__name__
    assert 'Context is almost full' not in handoff.RESCUE_PROMPT and '{path}' in handoff.RESCUE_PROMPT


# ------------------------------------------------------------------ v1 daemon: consult intake
def test_consult_is_queued_and_rescue_input_has_supervisor_material_only(afx):
    f = afx
    worked(f)
    f.advisor_runner = Runner({'action': 'give_up', 'reason': 'finding 说要人工登录 Reddit'})
    f.on_supervisor(rescue_decision(consult()))
    assert f.task()['bad_replies'] == '0' and len(f.r.lrange(fleet.RESCUE, 0, -1)) == 1
    f.pump()
    payload = f.advisor_runner.calls[-1]
    assert payload['kind'] == 'rescue' and payload['task'] == 'M0' and payload['finding'] == FINDING
    assert payload['want'] == '补能抓 Reddit 的工具' and payload['goal'] == 'research the sector rotation'
    assert payload['instruction'] == 'Research the semis rotation and write worker-1/notes.md'
    assert payload['slot']['name'] == 'worker-1' and payload['slot']['model'] == 'sonnet'
    assert payload['previous_sheet']['model'] == 'sonnet' and payload['relays'] == 0
    assert payload['actions'] == ['respawn', 'reassign', 'give_up']
    assert [c['name'] for c in payload['reassign_candidates']] == ['worker-2']
    assert 'SECRET' not in json.dumps(f.advisor_runner.calls, ensure_ascii=False)  # boundary 6
    assert 'titles only' not in json.dumps(payload, ensure_ascii=False)
    assert f.r.lrange(fleet.RESCUE, 0, -1) == []


def test_consult_must_name_a_task_of_that_worker(afx):
    f = afx
    worked(f)
    for bad in (consult(task='M7'), consult(worker='worker-2')):
        f.on_supervisor(rescue_decision(bad))
        assert 'is not a task of' in supervisor_texts(f)[-1]
    f.r.hset(fleet.TASK, {'bad_replies': 0})  # three strikes would block the task
    f.on_supervisor(rescue_decision(consult(), dispatch=[item()]))
    assert 'also dispatched in this reply' in supervisor_texts(f)[-1]
    assert f.r.lrange(fleet.RESCUE, 0, -1) == []


def test_give_up_tells_the_supervisor(afx):
    f = afx
    worked(f)
    f.advisor_runner = Runner({'action': 'give_up', 'reason': 'finding 是验证码，需要人'})
    f.on_supervisor(rescue_decision(consult()))
    f.pump()
    assert '[advisor] give_up M0: finding 是验证码，需要人' in supervisor_texts(f)[-1]
    assert 'BLOCKED' in supervisor_texts(f)[-1]
    info = f.agent('worker-1')
    assert info['generation'] == '0' and info['state'] == 'idle' and f.task()['status'] == 'running'
    audits = [f.r.hgetall(k) for k in f.r.h if k.startswith(fleet.ADVICE + 'adv-')]
    assert any(a.get('kind') == 'rescue' and a.get('action') == 'give_up' for a in audits)


def test_advisor_unavailable_on_rescue_takes_no_action(afx):
    f = afx
    worked(f)
    f.advisor_runner = Runner(TimeoutError('advisor timed out after 90 s'))
    old = f.agent('worker-1')['pane']
    f.on_supervisor(rescue_decision(consult()))
    f.pump()
    assert len(f.advisor_runner.calls) == 2  # one retry, then nothing
    sup = supervisor_texts(f)[-1]
    assert sup.startswith('[advisor unavailable] consult M0 (worker-1)') and '未做任何动作' in sup
    assert '按缺省派出' not in sup  # never the pre-dispatch fallback
    info = f.agent('worker-1')
    assert info['generation'] == '0' and info['state'] == 'idle' and info['pane'] == old and old not in f.mgr.killed
    assert f.r.llen('fx:inbox:worker-1') == 0 and f.task()['status'] == 'running'


def test_invalid_rescue_sheet_counts_as_unavailable(afx):
    f = afx
    worked(f)
    f.advisor_runner = Runner({'action': 'respawn', 'model': 'gpt-6', 'domains': []})
    f.on_supervisor(rescue_decision(consult()))
    f.pump()
    assert supervisor_texts(f)[-1].startswith('[advisor unavailable] consult M0')
    assert f.agent('worker-1')['generation'] == '0'


def test_advisor_off_ignores_consult_with_a_note(afx):
    f = afx
    worked(f)
    assert f.set_advisor(False, 'user', write=True) is None  # the roster switch (advisor plan §2.1)
    calls = len(f.advisor_runner.calls)
    f.advisor_runner = Runner(AssertionError('advisor must not be consulted'))
    f.on_supervisor(rescue_decision(consult()))
    f.pump()
    assert f.advisor_runner.calls == [] and calls == 1 and f.r.lrange(fleet.RESCUE, 0, -1) == []
    offs = [t for t in supervisor_texts(f) if '[advisor off]' in t]
    assert len(offs) == 1 and offs[0].startswith('[advisor off] consult M0 (worker-1) 未执行')  # one note, not two
    assert 'advisor: "on"' in offs[0] and f.task()['bad_replies'] == '0'


def test_consult_queued_while_on_then_off_before_idle_gets_the_off_note(afx):
    f = afx
    worked(f)
    f.advisor_runner = Runner(AssertionError('advisor must not be consulted'))
    f.setf('worker-1', state='busy')  # not idle yet: the consult waits in the queue
    f.on_supervisor(rescue_decision(consult()))
    assert len(f.r.lrange(fleet.RESCUE, 0, -1)) == 1
    assert f.set_advisor(False, 'user', write=True) is None
    f.setf('worker-1', state='idle')
    f.pump()
    assert f.advisor_runner.calls == [] and f.r.lrange(fleet.RESCUE, 0, -1) == []
    offs = [t for t in supervisor_texts(f) if '[advisor off]' in t]
    assert len(offs) == 1 and offs[0].startswith('[advisor off] consult M0 (worker-1) 未执行')


def test_without_advisor_consult_parses_and_is_answered(fx):
    f = fx  # plain fleet, no advisor key at all: consult is still a valid key, answered with the off note
    f.on_supervisor(decision(dispatch=[{'to': 'worker-1', 'id': 'M0',
                                        'instruction': 'Write worker-1/report.md with a Conclusion section'}]))
    f.r.l.setdefault('fx:inbox:supervisor', []).clear()
    f.on_supervisor(rescue_decision(consult()))
    assert any(t.startswith('[advisor off] consult M0') for t in supervisor_texts(f))
    assert f.r.hgetall(fleet.ADVICE_SEQ) == {}


# ------------------------------------------------------------------ v1 daemon: respawn worker-1 -> worker-1.1
def respawn_sheet(**extra):
    return dict({'action': 'respawn', 'model': 'opus', 'effort': 'high', 'domains': ['research_web'],
                 'handoff_hint': '先核对已抓到的 7 条链接再补正文',
                 'reason': 'finding Reddit 403 = 工具缺口；能力表 research_web/web-research'}, **extra)


def run_respawn(f, gen=0):
    """Consult -> pump (respawn starts the handoff) -> old generation writes its draft -> successor."""
    f.on_supervisor(rescue_decision(consult()))
    f.pump()
    info = f.agent('worker-1')
    assert info['state'] == 'handoff'
    # Claude workers take deliveries from their Stop-hook mailbox (claude_hook), never the pane.
    box = claude_hook.keys('fx', 'worker-1', int(info['generation']))['mailbox']
    prompt = json.loads(f.r.l[box][-1])['text']
    assert 'new configuration' in prompt and 'Context is almost full' not in prompt
    Path(info['handoff_draft']).write_text('## Done (facts)\n- 7 links in worker-1/notes.md\n## In progress\nnone\n'
                                           '## Next steps (in order)\n1. fetch the bodies\n')
    import os
    os.utime(info['handoff_draft'], ns=(int(info['handoff_mtime']) + 10**9,) * 2)
    f.handle(dict(turn('worker-1', 'handoff written', gen), msg_id=info['msg_id']))
    return info


def test_respawn_hands_off_to_the_next_generation_and_redelivers_after_ready(afx):
    f = afx
    worked(f)
    cwd = fleet.agent_cwd(f.cfg, 'worker-1')
    (cwd / '.claude' / 'skills' / 'stale-skill').mkdir(parents=True)
    old = f.agent('worker-1')['pane']
    f.advisor_runner = Runner(respawn_sheet())
    run_respawn(f)
    info = f.agent('worker-1')
    assert info['generation'] == '1' and info['label'] == 'worker-1.1' and info['state'] == 'starting'
    assert old in f.mgr.killed and f.mgr.tabbed[-1] == (info['pane'], old)
    launch = json.loads(info['runtime_launch'])
    assert launch['profile_id'] == 'claude-opus' and launch['extras'] == {'effort': 'high'}
    snapshot = fleet.load(f.run_dir / 'runtime' / 'worker-1-g1.json')
    argv = fleet.claude_argv(f.path, snapshot, 'worker-1', 1, 'READY?')
    assert argv[argv.index('--model') + 1] == 'opus' and argv[argv.index('--effort') + 1] == 'high'
    # new generation: clean kit (stale skill gone), new domain rules in CLAUDE.md
    assert sorted(p.name for p in (cwd / '.claude' / 'skills').iterdir()) == ['web-fetch-fallback']
    assert 'RULES OF research_web' in (cwd / 'CLAUDE.md').read_text()
    assert json.loads(info['installed_domains']) == ['research_web']
    # the handoff note: the model's three sections merged, plus the advisor's hint in Next steps
    note = sorted((f.run_dir / 'handoffs').glob('worker-1-g0-*.md'))[-1].read_text()
    assert '7 links in worker-1/notes.md' in note and '- 先核对已抓到的 7 条链接再补正文' in note
    assert handoff.validate.__name__ and 'fetch the bodies' in note
    takeover = Path(f.mgr.spawned[-1]['prompt_file']).read_text()
    assert 'worker-1.1' in takeover and 'handoff note' in takeover and 'READY' in takeover
    assert f.r.llen('fx:inbox:worker-1') == 1  # original instruction waits for READY
    f.handle(turn('worker-1', 'READY', 1))
    f.pump()
    text = worker_text(f, 'worker-1')
    assert 'Research the semis rotation and write worker-1/notes.md' in text and '[M0]' in text
    assert '上一代已做：见交接单；本代领域：research_web' in text
    assert f.agent('worker-1')['state'] == 'busy'
    assert len(f.advisor_runner.calls) == 1  # only the rescue: the re-delivery is not advised again
    report(f, 'worker-1', 1)
    sup = supervisor_texts(f)[-1]
    assert '[advisor] respawn worker-1 → worker-1.1 done' in sup and 'respawn）' in sup
    assert f.r.hget(fleet.RESCUE_RELAYS, f.task()['id'] + '|M0|worker-1') == '1'


def test_respawn_with_same_fingerprint_keeps_the_runtime_but_still_relays(afx):
    f = afx
    worked(f)
    f.advisor_runner = Runner(respawn_sheet(model='sonnet', effort='medium', handoff_hint=''))
    frozen = f.agent('worker-1').get('runtime_launch', '')
    run_respawn(f)
    info = f.agent('worker-1')
    assert info['label'] == 'worker-1.1' and info.get('runtime_launch', '') == frozen  # no launch change
    assert 'RULES OF research_web' in (fleet.agent_cwd(f.cfg, 'worker-1') / 'CLAUDE.md').read_text()


def test_relay_cap_third_consult_only_reassign_or_give_up(afx):
    f = afx
    worked(f)
    f.advisor_runner = Runner(respawn_sheet())
    for gen in (0, 1):
        run_respawn(f, gen)
        f.handle(turn('worker-1', 'READY', gen + 1))
        f.pump()
        report(f, 'worker-1', gen + 1)
    assert f.agent('worker-1')['label'] == 'worker-1.2'
    assert f.r.hget(fleet.RESCUE_RELAYS, f.task()['id'] + '|M0|worker-1') == '2'
    f.on_supervisor(rescue_decision(consult()))
    f.pump()  # the advisor insists on respawn: invalid twice -> unavailable, no third relay
    assert f.advisor_runner.calls[-1]['actions'] == ['reassign', 'give_up']
    assert supervisor_texts(f)[-1].startswith('[advisor unavailable] consult M0 (worker-1.2)')
    assert f.agent('worker-1')['generation'] == '2' and f.agent('worker-1')['state'] == 'idle'
    f.advisor_runner = Runner({'action': 'give_up', 'reason': '两次接力都没过；能力表无更高档'})
    f.on_supervisor(rescue_decision(consult()))
    f.pump()
    assert '[advisor] give_up M0' in supervisor_texts(f)[-1]


# ------------------------------------------------------------------ v1 daemon: reassign
def test_reassign_takes_the_task_back_and_the_supervisor_redispatches(afx):
    f = afx
    worked(f)
    f.advisor_runner = Runner({'action': 'reassign', 'to': 'worker-2', 'model': 'opus', 'effort': 'high',
                               'domains': ['research_web'], 'reason': '能力表 research_web/web-research；换槽'})
    old1, old2 = f.agent('worker-1')['pane'], f.agent('worker-2')['pane']
    f.on_supervisor(rescue_decision(consult()))
    f.pump()
    assert json.loads(f.r.get(fleet.ITEMS))['M0']['to'] == ''  # taken back from worker-1
    w1, w2 = f.agent('worker-1'), f.agent('worker-2')
    assert w1['generation'] == '0' and w1['pane'] == old1 and old1 not in f.mgr.killed  # no handoff for worker-1
    assert not list((f.run_dir / 'handoffs').glob('worker-1-*')) if (f.run_dir / 'handoffs').exists() else True
    assert w2['label'] == 'worker-2.1' and w2['state'] == 'starting' and old2 in f.mgr.killed
    assert json.loads(w2['runtime_launch'])['profile_id'] == 'claude-opus'
    assert not any('[advisor] M0 → worker-2' in t for t in supervisor_texts(f))  # only after READY
    f.handle(turn('worker-2', 'READY', 1))
    f.pump()
    sup = supervisor_texts(f)[-1]
    assert '[advisor] M0 → worker-2.1 (opus/high, domains: [research_web])' in sup and 'reassign' in sup
    assert '请重新 dispatch M0 给 worker-2.1' in sup
    assert f.agent('worker-2')['state'] == 'idle' and f.r.llen('fx:inbox:worker-2') == 0  # daemon does not dispatch
    # the supervisor re-dispatches: the reassign sheet holds, no second consultation
    calls = len(f.advisor_runner.calls)
    f.advisor_runner = Runner(AssertionError('no second consult for the reassigned task'))
    send_decision(f, [dict(item('worker-2.1', 'Research the semis rotation and write worker-2/notes.md'), id='M0')])
    f.pump()
    assert f.advisor_runner.calls == [] and calls == 1
    assert f.agent('worker-2')['state'] == 'busy' and f.agent('worker-2')['generation'] == '1'
    assert 'semis rotation' in worker_text(f, 'worker-2') and json.loads(f.r.get(fleet.ITEMS))['M0']['to'] == 'worker-2'


def test_reassign_without_runtime_change_notifies_at_once(afx):
    f = afx
    worked(f)
    f.advisor_runner = Runner({'action': 'reassign', 'model': 'sonnet', 'effort': 'medium', 'domains': [],
                               'reason': 'finding 指向 worker-1 的工作目录坏了'})
    f.on_supervisor(rescue_decision(consult()))
    f.pump()
    assert '[advisor] M0 → worker-2 (sonnet/medium, domains: [])' in supervisor_texts(f)[-1]
    assert f.agent('worker-2')['generation'] == '0'


def test_reassign_is_not_offered_without_an_idle_slot(afx):
    f = afx
    send_decision(f, [item('worker-2', 'Summarize the advisor plan into worker-2/sum.md')])
    f.pump()
    assert f.agent('worker-2')['state'] == 'busy'
    worked(f)
    f.advisor_runner = Runner({'action': 'give_up', 'reason': 'x'})
    f.on_supervisor(rescue_decision(consult()))
    f.pump()
    assert f.advisor_runner.calls[-1]['actions'] == ['respawn', 'give_up']
    assert f.advisor_runner.calls[-1]['reassign_candidates'] == []


def test_contract_row_goes_back_to_running_on_reassign(tmp_path, monkeypatch):
    from test_contracts import WORKERS, THREADS as CT, item as citem, report as creport, sup, to_parallel
    from test_fleet import FakeMgr, FakeRedis, write_rollout
    import sys
    run = tmp_path / 'run'
    (run / 'workspace').mkdir(parents=True)
    cfg = {'run': str(run), 'redis_socket': '/tmp/cmux-fleet-test.sock', 'python': sys.executable,
           'workers': list(WORKERS), 'handoff_ratio': .6, 'handoff_timeout': 900, 'window': 95000,
           'codex_home': str(tmp_path / 'codex-home'), 'codex_model': 'gpt-6.1-sol'}
    (run / 'config.json').write_text(json.dumps(cfg))
    advisor_on(run, WORKERS)  # the roster switch (advisor plan §2.1), not a config key
    sessions = tmp_path / 'sessions'
    f = fleet.Fleet(run / 'config.json', FakeRedis(), FakeMgr(), sessions=sessions)
    f.advisor_runner = Runner({'model': 'gpt-6.1-sol', 'domains': []})
    f.start_all()
    for name in f.names:
        write_rollout(sessions, CT[name], fleet.agent_cwd(cfg, name))
        f.handle({'kind': 'turn', 'agent': name, 'generation': 0, 'thread': CT[name], 'text': 'READY'})
    to_parallel(f)
    sup(f, {'status': 'RUNNING', 'completion': 10, 'dispatch': [citem('M0', 'worker-1', provides=['IF-M0-src'])]})
    f.pump()
    creport(f, 'worker-1', 'Interfaces: none')
    row = f.contract_state()['tasks']['M0']
    assert row['status'] == 'failed' and row['attempts'] == 1
    f.advisor_runner = Runner({'action': 'reassign', 'to': 'worker-2', 'model': 'gpt-6.1-sol', 'domains': [],
                               'reason': 'finding：接口没写出；换槽'})
    sup(f, {'status': 'RUNNING', 'completion': 20, 'dispatch': [], 'consult': [consult()]})
    f.pump()
    row = f.contract_state()['tasks']['M0']
    assert row['status'] == 'running' and row['attempts'] == 2 and 'reassigned by the advisor' in row['reason']
    assert '[advisor] M0 → worker-2' in supervisor_texts(f)[-1]


# ------------------------------------------------------------------ v2 (real private Redis, fake cmux)
def v2_worked(f, slot='worker-1'):
    from test_v2_codex_claude import answer_turn, submit
    submit(f)
    answer_turn(f, 'supervisor', json.dumps({'status': 'RUNNING', 'completion': 5, 'reason': 'go', 'dispatch': [
        {'to': slot, 'id': 'M0', 'instruction': 'Research the semis rotation into ' + slot + '/notes.md'}]}))
    f.pump()  # release the held first generation with the first sheet (opus/high/research_web)
    answer_turn(f, slot, 'READY', startup=True)
    f.pump()
    answer_turn(f, slot, 'Done:\n- titles SECRET\nEvidence:\n- ' + slot + '/notes.md\nNot done: bodies\nBlocked: none')
    f.pump()


def test_v2_consult_respawn_on_the_real_bus(v2adv2, monkeypatch):
    from test_v2_codex_claude import answer_turn, typed
    f = v2adv2
    monkeypatch.setattr(f, 'preflight_profile', lambda launch: None)
    v2_worked(f)
    f.advisor_runner = Runner(respawn_sheet(model='sonnet', effort='medium'))
    old = f.agent('worker-1')['pane']
    assert answer_turn(f, 'supervisor', json.dumps({'status': 'RUNNING', 'completion': 30, 'reason': 'consult',
                                                    'dispatch': [], 'consult': [consult()]}))['status'] == 'committed'
    assert f.task().get('bad_replies') in (None, '0')
    f.pump()  # rescue -> respawn: the handoff request is offered first
    assert f.agent('worker-1')['state'] in ('busy', 'handoff')
    prompt = typed(f, 'worker-1')[-1]
    assert 'new configuration' in prompt
    payload = f.advisor_runner.calls[-1]
    assert payload['kind'] == 'rescue' and 'SECRET' not in json.dumps(payload, ensure_ascii=False)
    assert answer_turn(f, 'worker-1', 'handoff written')['status'] == 'committed'
    f.v2.step(10)
    info = f.agent('worker-1')
    assert info['generation'] == '1' and info['label'] == 'worker-1.1' and old in f.mgr.killed
    assert json.loads(info['runtime_launch'])['profile_id'] == 'claude-sonnet'
    assert answer_turn(f, 'worker-1', 'READY', startup=True, generation=1)['status'] == 'ready'
    f.pump()
    text = typed(f, 'worker-1')[-1]
    assert 'Research the semis rotation into worker-1/notes.md' in text and '本代领域：research_web' in text
    assert len(f.advisor_runner.calls) == 1  # the rescue only: no re-consult on re-delivery


@pytest.mark.parametrize('name', ['supervisor.md', 'supervisor_v2.md'])
def test_supervisor_prompts_carry_the_judgement_section(name):
    text = (Path(fleet.HERE) / 'agents' / name).read_text()
    assert '### Judging a report when the fleet advisor is on' in text and 'the worker never judges itself' in text
    assert '"consult": [{"worker": "worker-1", "task": "M0", "finding"' in text
    assert 'first one corrective dispatch; if the second result still fails, `consult`' in text
    for message in ('[advisor] respawn worker-1 → worker-1.1 done', '[advisor] give_up M0', '[advisor unavailable] consult M0',
                    '[advisor off] consult M0', 'dispatch M0 to worker-2 yourself'):
        assert message in text
    assert '`dispatch[].advise_note`' in text
    assert '- `advisor`: optional `"on"` / `"off"` (default off' in text and 'send `"advisor": "on"` and the consult again' in text


def test_v2_reassign_to_a_held_slot_starts_it_with_the_sheet_on_redispatch(v2adv2):
    from test_v2_codex_claude import answer_turn, typed
    f = v2adv2
    v2_worked(f)
    assert f.agent('worker-2')['advice_hold'] == '1'  # never dispatched: still held for its first sheet
    f.advisor_runner = Runner({'action': 'reassign', 'to': 'worker-2', 'model': 'sonnet', 'effort': 'high',
                               'domains': ['us_options'], 'reason': '能力表；换槽'})
    answer_turn(f, 'supervisor', json.dumps({'status': 'RUNNING', 'completion': 30, 'reason': 'consult',
                                             'dispatch': [], 'consult': [consult()]}))
    f.pump()
    assert '[advisor] M0 → worker-2 (sonnet/high, domains: [us_options])' in typed(f, 'supervisor')[-1]
    assert json.loads(f.r.get(fleet.ITEMS))['M0']['to'] == ''
    f.advisor_runner = Runner(AssertionError('the reassign sheet holds: no second consult'))
    answer_turn(f, 'supervisor', json.dumps({'status': 'RUNNING', 'completion': 40, 'reason': 'redispatch', 'dispatch': [
        {'to': 'worker-2', 'id': 'M0', 'instruction': 'Research the semis rotation into worker-2/notes.md'}]}))
    f.pump()  # release_hold with the preset sheet
    assert f.advisor_runner.calls == []
    snap = fleet.load(f.run_dir / 'runtime' / 'worker-2-g0.json')
    assert snap['worker_model'] == 'sonnet' and snap['worker_effort'] == 'high'
    assert 'RULES OF us_options' in (fleet.agent_cwd(f.cfg, 'worker-2') / 'CLAUDE.md').read_text()
    assert answer_turn(f, 'worker-2', 'READY', startup=True)['status'] == 'ready'
    f.pump()
    assert 'semis rotation into worker-2/notes.md' in typed(f, 'worker-2')[-1]


def test_respawn_preflight_failure_blocks_without_a_silent_switch(afx, monkeypatch):
    f = afx
    worked(f)

    def broken(launch):
        raise ValueError('profile CLI binary not found: claude')
    monkeypatch.setattr(f, 'preflight_profile', broken)
    f.advisor_runner = Runner(respawn_sheet())
    f.on_supervisor(rescue_decision(consult()))
    f.pump()
    assert f.task()['status'] == 'blocked' and 'advisor respawn failed' in f.task()['reason']
    info = f.agent('worker-1')
    assert info['generation'] == '0' and info['state'] != 'handoff' and f.r.llen('fx:inbox:worker-1') == 0


def test_consult_waits_for_the_worker_to_be_idle(afx):
    f = afx
    worked(f)
    f.on_supervisor(rescue_decision(consult()))
    f.setf('worker-1', state='busy')
    f.advisor_runner = Runner(AssertionError('not before the idle boundary'))
    f.pump()
    assert f.advisor_runner.calls == [] and len(f.r.lrange(fleet.RESCUE, 0, -1)) == 1
    f.setf('worker-1', state='idle')
    f.advisor_runner = Runner({'action': 'give_up', 'reason': 'x'})
    f.pump()
    assert len(f.advisor_runner.calls) == 1 and f.r.lrange(fleet.RESCUE, 0, -1) == []


def test_deferred_switch_on_reassign_still_tells_the_supervisor(afx, monkeypatch):
    f = afx
    worked(f)
    f.advisor_runner = Runner({'action': 'reassign', 'to': 'worker-2', 'model': 'opus', 'effort': 'high',
                               'domains': [], 'reason': '换槽'})

    def deferred(name, launch):  # e.g. a codex slot whose native subagents still run (switch_profile returns)
        f.setf(name, profile_state='pending', profile_reason='waiting for native subagents to finish')
    monkeypatch.setattr(f, 'switch_profile', deferred)
    f.on_supervisor(rescue_decision(consult()))
    f.pump()
    assert f.agent('worker-2')['state'] == 'idle' and not f.agent('worker-2').get('rescue_note')
    assert '[advisor] M0 → worker-2 (opus/high, domains: [])' in supervisor_texts(f)[-1]


def test_new_task_clears_rescue_leftovers(afx):
    f = afx
    worked(f)
    f.r.rpush(fleet.RESCUE, json.dumps({'slot': 'worker-1', 'task': 'M0', 'run_task': f.task()['id']}))
    f.setf('worker-2', rescue_note='[advisor] M0 → worker-2 old', rescue_launch='{"x":1}', advice_preset='{"task":"M0"}')
    f.finish('blocked', 'operator stop')
    f.handle({'kind': 'task', 'request_id': 'r2', 'spec': {'goal': 'another task'}})
    assert f.r.lrange(fleet.RESCUE, 0, -1) == []
    info = f.agent('worker-2')
    assert all(not info.get(k) for k in fleet.RESCUE_FIELDS)
    f.pump()
    assert not any('old' in t for t in supervisor_texts(f) if t.startswith('[advisor] M0'))
