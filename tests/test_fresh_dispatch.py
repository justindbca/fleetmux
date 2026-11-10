"""Fresh dispatch (docs/design.md §37): `dispatch[].fresh` gives an idle worker a clean next generation
before the instruction (no handoff note, no relay text); the worker board shows context use and the last
task; a reused worker over fresh_hint_ratio earns the supervisor one hint line. In-memory Redis, fake cmux."""
import json
from types import SimpleNamespace

import pytest

from test_fleet import (THREADS, SVC_THREADS, answer, append_rollout, cfx, decision, fx, last_to,  # noqa: F401
                        received, req, sfx, submit, turn, write_rollout)
from cmux_fleet import fleet, fleet_config

NEW_THREAD = '55555555-5555-5555-5555-555555555555'
OTHER = 'Write worker-1/other.md about the unrelated billing export, at least 50 chars'
DONE = json.dumps({'done': ['report written'], 'evidence': ['worker-1/report.md'], 'not_done': '', 'blocked': ''})


def worked(f, used=40000):
    """worker-1 handled one dispatch and reported: idle, delivered=1, `used` tokens of a 95k window."""
    f.pump()
    f.handle(turn('supervisor', decision()))
    f.pump()
    append_rollout(f.sessions, THREADS['worker-1'], fleet.agent_cwd(f.cfg, 'worker-1'), [  # this turn's usage
        {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
            'last_token_usage': {'total_tokens': used}, 'model_context_window': 95000}}}])
    f.handle(turn('worker-1', DONE))
    f.pump()  # the report goes to the supervisor
    assert f.agent('worker-1')['state'] == 'idle' and f.agent('worker-1')['delivered'] == '1'


def logs(f, kind):
    return [json.loads(x) for x in f.r.l.get('fx:log', []) if json.loads(x)['kind'] == kind]


def fresh(to='worker-1', instruction=OTHER, value=True):
    return decision(dispatch=[{'to': to, 'instruction': instruction, 'fresh': value}])


def test_fresh_to_an_idle_worker_rotates_clean_then_delivers(fx):
    worked(fx)
    old = fx.agent('worker-1')['pane']
    fx.handle(turn('supervisor', fresh()))
    fx.pump()
    info = fx.agent('worker-1')
    assert info['generation'] == '1' and info['label'] == 'worker-1.1' and info['state'] == 'starting'
    assert old in fx.mgr.killed and fx.r.llen('fx:inbox:worker-1') == 1  # the instruction waits for READY
    handoffs = fx.run_dir / 'handoffs'
    assert not list(handoffs.glob('worker-1-g0*.md'))  # no handoff note
    assert list(handoffs.glob('worker-1-g0*.terminal.txt'))  # the old screen is kept
    prompt = open(fx.mgr.spawned[-1]['prompt_file']).read()
    assert 'worker-1.1' in prompt and 'READY' in prompt
    assert 'handoff' not in prompt.lower() and 'taking over' not in prompt and 'took over' not in prompt
    hist = [json.loads(x) for x in (fx.run_dir / 'history.jsonl').read_text().splitlines()]
    assert hist[-1]['label'] == 'worker-1' and hist[-1]['source'] == 'fresh' and not hist[-1]['handoff']
    audit = logs(fx, 'dispatch.fresh')[-1]
    assert audit['agent'] == 'worker-1' and audit['from_generation'] == 0 and audit['generation'] == 1
    assert audit['dropped_tokens'] == 40000
    fx.handle({'kind': 'turn', 'agent': 'worker-1', 'generation': 1, 'thread': NEW_THREAD, 'text': 'READY'})
    fx.pump()
    assert 'worker-1/other.md' in last_to(fx, 'worker-1') and fx.agent('worker-1')['state'] == 'busy'
    assert not received(fx.mgr, old)[-1].count('other.md')  # never reached the old session
    assert fx.agent('worker-1')['generation'] == '1'  # one rotation only


def test_fresh_rotation_works_for_a_claude_worker(cfx):
    cfx.pump()
    cfx.handle(turn('supervisor', decision()))
    cfx.pump()
    cfx.handle(dict(turn('worker-1', DONE), msg_id=cfx.agent('worker-1')['msg_id']))
    cfx.pump()
    cfx.handle(turn('supervisor', fresh()))
    cfx.pump()
    info = cfx.agent('worker-1')
    assert info['generation'] == '1' and info['label'] == 'worker-1.1' and info['state'] == 'starting'
    assert not list((cfx.run_dir / 'handoffs').glob('worker-1-g0*.md'))
    assert cfx.r.llen('fx:inbox:worker-1') == 1


def test_fresh_to_a_busy_worker_is_refused_with_the_reason(fx):
    fx.pump()
    fx.handle(turn('supervisor', decision()))
    fx.pump()
    assert fx.agent('worker-1')['state'] == 'busy'
    fx.handle(turn('supervisor', fresh()))
    back = fx.r.l['fx:inbox:supervisor'][0]
    assert 'fresh' in back and 'worker-1' in back and 'busy' in back
    assert fx.r.llen('fx:inbox:worker-1') == 0 and fx.agent('worker-1')['generation'] == '0'
    assert fx.task()['bad_replies'] == '1'


def test_fresh_to_a_worker_with_queued_work_is_refused(fx):
    worked(fx)
    fx.queue('worker-1', '[Supervisor instruction r1] Earlier queued instruction for worker-1')
    fx.handle(turn('supervisor', fresh()))
    back = fx.r.l['fx:inbox:supervisor'][0]
    assert 'fresh' in back and 'queued' in back
    assert fx.r.llen('fx:inbox:worker-1') == 1 and fx.agent('worker-1')['generation'] == '0'


def test_fresh_to_a_new_session_skips_the_reset(fx):
    fx.pump()
    fx.handle(turn('supervisor', fresh()))
    fx.pump()
    info = fx.agent('worker-1')
    assert info['generation'] == '0' and info['state'] == 'busy' and 'other.md' in last_to(fx, 'worker-1')
    assert not logs(fx, 'dispatch.fresh')


def test_fresh_spawn_failure_keeps_the_old_session_and_tells_the_supervisor(fx):
    worked(fx)
    old = fx.agent('worker-1')['pane']
    real = fx.mgr.spawn_interactive_cli
    fx.mgr.spawn_interactive_cli = lambda **kw: SimpleNamespace(success=False, pane_id=None, error='no cmux')
    fx.handle(turn('supervisor', fresh()))
    fx.pump()
    fx.mgr.spawn_interactive_cli = real
    info = fx.agent('worker-1')
    assert info['generation'] == '0' and info['label'] == 'worker-1' and info['pane'] == old
    assert old not in fx.mgr.killed
    fx.pump()  # the instruction is not lost: it goes to the existing session
    assert 'worker-1/other.md' in last_to(fx, 'worker-1')
    fx.handle(turn('worker-1', DONE))
    fx.pump()
    assert '[Fresh failed] worker-1' in last_to(fx, 'supervisor')


def test_reuse_over_the_hint_ratio_delivers_and_adds_one_hint(fx):
    worked(fx, used=40000)  # 42% of 95k, over the default 0.30
    fx.handle(turn('supervisor', decision(dispatch=[{'to': 'worker-1', 'instruction': OTHER}])))
    fx.pump()
    assert fx.agent('worker-1')['generation'] == '0' and 'other.md' in last_to(fx, 'worker-1')
    fx.handle(turn('worker-1', DONE))
    fx.pump()
    text = last_to(fx, 'supervisor')
    assert text.count('[Hint]') == 1
    assert ('[Hint] worker-1 was at 42% context when it got this dispatch; if the work is unrelated to its '
            'last task, add "fresh": true next time.') in text


def test_reuse_under_the_hint_ratio_gives_no_hint(fx):
    worked(fx, used=20000)  # 21%
    fx.handle(turn('supervisor', decision(dispatch=[{'to': 'worker-1', 'instruction': OTHER}])))
    fx.pump()
    fx.handle(turn('worker-1', DONE))
    fx.pump()
    assert '[Hint]' not in last_to(fx, 'supervisor')


def test_hint_ratio_comes_from_fleet_yaml(fx, tmp_path, monkeypatch):
    user = tmp_path / 'private.yaml'
    user.write_text('fresh_dispatch:\n  fresh_hint_ratio: 0.5\n')
    monkeypatch.setenv(fleet_config.USER_ENV, str(user))
    assert fleet_config.fresh_hint_ratio() == 0.5
    worked(fx, used=40000)  # 42% < 50%
    fx.handle(turn('supervisor', decision(dispatch=[{'to': 'worker-1', 'instruction': OTHER}])))
    fx.pump()
    fx.handle(turn('worker-1', DONE))
    fx.pump()
    assert '[Hint]' not in last_to(fx, 'supervisor')


@pytest.mark.parametrize('bad', [0, 1.5, 'x', True])
def test_hint_ratio_must_be_a_fraction(tmp_path, monkeypatch, bad):
    user = tmp_path / 'private.yaml'
    user.write_text(json.dumps({'fresh_dispatch': {'fresh_hint_ratio': bad}}))
    monkeypatch.setenv(fleet_config.USER_ENV, str(user))
    with pytest.raises(ValueError, match='fresh_hint_ratio'):
        fleet_config.fresh_hint_ratio()


def test_shipped_hint_ratio_default():
    assert fleet_config.fresh_hint_ratio() == 0.3


def test_task_report_carries_the_worker_board_with_context_and_last_task(fx):
    worked(fx, used=40000)
    text = last_to(fx, 'supervisor')
    assert '[worker-1 report]' in text and 'Worker status now' in text
    line = [x for x in text.splitlines() if x.startswith('- worker-1: ')][0]
    assert 'context used 42% (40k tokens)' in line
    assert 'last task: [M0] Write worker-1/report.md with a Conclusion section' in line  # §44 auto id
    assert '[Supervisor instruction' not in line
    assert text.rstrip().endswith('Reply with the JSON format from AGENTS.md.')


def test_service_board_shows_context_and_last_task(sfx):
    write_rollout(sfx.sessions, SVC_THREADS['exit'], fleet.agent_cwd(sfx.cfg, 'exit'), used=40000)
    submit(sfx, req('r20', to='exit'))
    sfx.pump()
    answer(sfx, 'exit', '{"verdict": 1, "param": 2}')
    submit(sfx, req('r21'))
    sfx.pump()
    text = last_to(sfx, 'supervisor')
    line = [x for x in text.splitlines() if x.startswith('- exit: ')][0]
    assert line.startswith('- exit: idle, role: You judge exits.')
    assert 'context used 42% (40k tokens)' in line and 'last task: [Request r20] Handle this request' in line
    assert '- reentry: idle' in text and 'reentry: idle,' not in text  # no data: nothing added


def test_service_fresh_to_a_busy_worker_is_refused(sfx):
    submit(sfx, req('r30', to='exit'))
    sfx.pump()
    assert sfx.agent('exit')['state'] == 'busy'
    submit(sfx, req('r31'))
    sfx.handle(turn('supervisor', json.dumps({'status': 'RUNNING', 'skip': [], 'dispatch': [
        {'to': 'exit', 'request_id': 'r31', 'instruction': 'Judge the exit of this new fill', 'fresh': True}]})))
    back = sfx.r.l['fx:inbox:supervisor'][-1]  # v1 queues a correction last
    assert 'fresh' in back and 'busy' in back
    assert sfx.r.hget('fx:req:r31', 'status') == 'routing'


@pytest.mark.parametrize('bad', ['yes', 1, None])
def test_decision_validators_reject_a_non_boolean_fresh(bad):
    item = {'to': 'worker-1', 'instruction': 'Write worker-1/a.md please', 'fresh': bad}
    with pytest.raises(ValueError, match='fresh'):
        fleet.parse_decision(json.dumps({'status': 'RUNNING', 'completion': 1, 'dispatch': [item]}), ['worker-1'], False)
    with pytest.raises(ValueError, match='fresh'):
        fleet.parse_service_decision(json.dumps({'status': 'RUNNING', 'skip': [],
                                                 'dispatch': [dict(item, request_id='r1')]}), ['worker-1'])
    ok = dict(item, fresh=True)
    assert fleet.parse_decision(json.dumps({'status': 'RUNNING', 'completion': 1, 'dispatch': [ok]}),
                                ['worker-1'], False)['dispatch'][0]['fresh'] is True


def test_every_supervisor_prompt_explains_fresh():
    agents = fleet.HERE / 'agents'
    for name in ('supervisor.md', 'supervisor_v2.md', 'service_supervisor.md', 'service_supervisor_v2.md',
                 'service_supervisor_tui_v2.md', 'contracts_supervisor.md'):
        text = (agents / name).read_text()
        assert '"fresh": true' in text, name


class V2Stub:
    """Only what fresh_rotation touches on a v2 runtime: the retirement confirmation and the audit."""
    def __init__(self):
        self.retired, self.audit = [], []

    def log(self, kind, data):
        self.audit.append(dict(data, kind=kind))

    def confirm_retired(self, slot):
        self.retired.append(slot)


def test_v2_fresh_rotation_starts_launch_held_and_opens_after_retirement(fx):
    worked(fx)
    old = fx.agent('worker-1')['pane']
    fx.v2 = V2Stub()
    try:
        fx.fresh_rotation('worker-1', 40000, 95000)
    finally:
        stub, fx.v2 = fx.v2, None
    info = fx.agent('worker-1')
    assert stub.retired == ['worker-1'] and old in fx.mgr.killed and (info['pane'], old) in fx.mgr.tabbed
    assert info['launch_ready'] == '1' and info['generation'] == '1' and info['label'] == 'worker-1.1'
    assert stub.audit[-1]['kind'] == 'dispatch.fresh' and stub.audit[-1]['dropped_tokens'] == 40000


def test_v2_fresh_rotation_that_cannot_take_the_tab_keeps_the_old_session(fx):
    worked(fx)
    old = fx.agent('worker-1')['pane']
    fx.mgr.tab_into = lambda pane, peer, before=True: False
    fx.v2 = V2Stub()
    try:
        fx.fresh_rotation('worker-1', 40000, 95000)
    finally:
        stub, fx.v2 = fx.v2, None
    info = fx.agent('worker-1')
    assert not stub.retired and old not in fx.mgr.killed and fx.mgr.killed  # only the successor was removed
    assert info['pane'] == old and info['generation'] == '0' and info['label'] == 'worker-1'
    assert '[Fresh failed] worker-1' in fx.r.l['fx:fresh:hints'][0]
    assert stub.audit[-1]['kind'] == 'dispatch.fresh_failed'


def test_a_fresh_mark_whose_instruction_was_cancelled_does_not_reset_later_work(fx):
    worked(fx)
    fx.handle(turn('supervisor', fresh()))
    assert fx.agent('worker-1')['fresh_pending'] == '1'
    fx.r.l['fx:inbox:worker-1'] = []  # cancelled before delivery (e.g. the task ended)
    fx.pump()
    fx.handle(turn('supervisor', decision(dispatch=[{'to': 'worker-1', 'instruction': OTHER}])))
    fx.pump()
    assert fx.agent('worker-1')['generation'] == '0' and fx.agent('worker-1')['state'] == 'busy'
