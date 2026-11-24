"""The tree survives every path that removes or rewrites an fx:items row, a refused split, a real Redis restart and a
handoff (docs/design.md §62): fx:items keeps who holds a task id now; fx:tree keeps the plan and the actuals."""
import json

import pytest

from fleetmux import advisor, decompose, fleet, runtime_v2, subfleet
from test_decompose_escalate import escalate, splits  # noqa: F401
from test_decompose_fleet import answer_turn, reply, sample, tree
from test_decompose_split import dispatch_m0
from test_escalate import ok_check  # noqa: F401
from test_message_bus_v2 import private_redis_v2  # noqa: F401
from test_resume import answer_turn as resume_answer, crash_and_resume, managed, task_run  # noqa: F401
from test_service_escalate import audits, request, svc  # noqa: F401
from test_service_split import answer_split, ask_split, asked, items, opened
from test_v2_codex_claude import mixed  # noqa: F401

KEPT = json.dumps(decompose.new_tree('root', 'a tree that must stay'))


@pytest.mark.usefixtures('job_chains_as_before')
@pytest.mark.parametrize('decision', ['reject', 'revise'])
def test_w1_a_p7_split_answered_reject_or_revise_leaves_the_tree(svc, ok_check, decision):
    f = svc
    f.r.set(decompose.TREE, KEPT)
    asked(f)
    key, _ = opened(f)
    answer_split(f, key, decision, note='answer it yourself')
    assert key not in items(f) and f.r.get(decompose.TREE) == KEPT


@pytest.mark.usefixtures('job_chains_as_before')
def test_w3_a_p7_split_the_program_refuses_rolls_back_fx_items_and_leaves_the_tree(svc, ok_check, monkeypatch):
    f = svc
    f.r.set(decompose.TREE, KEPT)
    request(f)
    monkeypatch.setattr(subfleet, 'check', lambda *a, **k: 'max_children 3: this fleet already runs M1, M2, M3')
    ask_split(f)  # written as M<n> first, refused by the limits: the row is rolled back
    assert [a for a in audits(f, 'split.refused')] and not items(f)
    assert f.r.get(decompose.TREE) == KEPT


def test_w8_an_advisor_reassign_clears_the_owner_and_leaves_the_tree(mixed, decompose_on):
    f = mixed
    dispatch_m0(f)
    before = f.r.get(decompose.TREE)
    sheet = {'to': 'worker-2', 'model': 'm', 'effort': '', 'domains': [], 'reason': 'stuck', 'ticket': 'tk-1'}
    opt = {'cli': 'claude', 'current': advisor.fingerprint('claude', 'm', ''), 'cards': {}}
    f.rescue_reassign('worker-1', 'M0', sheet, opt, None, None)
    assert json.loads(f.r.get(fleet.ITEMS))['M0']['to'] == '' and f.r.get(decompose.TREE) == before


@pytest.mark.parametrize('decision', ['reject', 'revise'])
def test_a_task_split_answered_reject_or_revise_keeps_the_block(mixed, decompose_on, splits, decision):  # noqa: F811
    f = mixed
    dispatch_m0(f)
    escalate(f, delta_t=1, delta_c=1)
    answer_turn(f, 'supervisor', reply(escalations=[{'task': 'M0', 'decision': decision, 'note': 'do it yourself',
                                                     'cause': 'scope'}]))
    t = tree(f)
    m0 = decompose.by_id(t)['M0']
    assert m0['state'] == 'running' and not decompose.children(t, 'M0') and m0['actual']['returns'] == 1
    assert {n['id'] for n in t['nodes']} == {'root', 'M0', 'M1', 'M2', 'M3'}


def test_a_real_redis_restart_keeps_the_tree_and_tree_json_in_step(task_run, decompose_on):  # noqa: F811
    state = task_run
    f = state['f']
    runtime_v2.post_event(f.v2.bus, dict(kind='task', request_id='dt', spec={'goal': 'a decomposed task'}), 'dt')
    assert f.v2.step(10)
    f.pump()
    assert resume_answer(f, 'supervisor', reply(tree=sample()))['status'] == 'committed'
    kept = f.tree_state()
    assert kept['accepted'] == 1
    f = crash_and_resume(state)  # daemon and Redis killed; Redis restarts from its AOF
    assert f.tree_state() == kept
    f.sync_tree_files()
    assert json.loads((f.run_dir / 'tree.json').read_text()) == f.tree_state()


def test_a_handoff_rotation_keeps_the_tree(mixed, decompose_on):
    f = mixed
    dispatch_m0(f)
    before = f.r.get(decompose.TREE)
    answer_turn(f, 'worker-1', 'Done: skeleton\nEvidence: worker-1/x\nNot done: none\nBlocked: none')
    f.start_rotation('worker-1', 90000, 100000)
    f.pump()
    assert answer_turn(f, 'worker-1', 'handoff written')['status'] == 'committed'
    f.v2.step(10)
    assert f.agent('worker-1')['label'] == 'worker-1.1'
    after = json.loads(f.r.get(decompose.TREE))
    assert {n['id'] for n in after['nodes']} == {n['id'] for n in json.loads(before)['nodes']}
    assert decompose.by_id(after)['M0']['state'] == 'reported'  # its report, not the handoff, settled it
