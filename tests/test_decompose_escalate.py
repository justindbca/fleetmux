"""A worker's split request with the tree on (docs/design.md §62): it carries delta_t / delta_c; the supervisor sees the
worker's numbers beside the program's; an approve passes the program's account for a sub-fleet (not: refused by the
program, no strike, the request stays); an approved split is written into the tree (the block leaf=false, its pieces
delegated) and the sub-fleet's actuals fill the block in when it returns. A job is never put to the account."""
import json

import pytest

from fleetmux import decompose, fleet, subfleet
from test_decompose_fleet import answer_turn, node, reply, tree
from test_decompose_split import dispatch_m0
from test_long_split import supervisor_texts
from test_message_bus_v2 import private_redis_v2  # noqa: F401
from test_v2_codex_claude import mixed, typed  # noqa: F401

PROPOSAL = [{'id': 'M0.0', 'instruction': 'Write the skeleton of check().', 'output': 'a.md'},
            {'id': 'M0.1', 'instruction': 'Write the contract tests of check().', 'output': 'b.md'}]
REPORT = 'Done: read the code\nEvidence: worker-1/notes.md\nNot done: everything else\nBlocked: none\n'


@pytest.fixture
def splits(monkeypatch):
    asked = []
    monkeypatch.setattr(subfleet, 'check', lambda *a, **k: None)
    monkeypatch.setattr(fleet.Fleet, 'supervisor_chain_pick', lambda self, slot, **kw: ({}, None, ''))
    monkeypatch.setattr(subfleet, 'request', lambda run_dir, **spec: asked.append(spec) or 'sf-test')
    return asked


def seen(f):
    return supervisor_texts(f) + typed(f, 'supervisor')


def escalate(f, **numbers):
    answer_turn(f, 'worker-1', REPORT + json.dumps(dict({'escalate': 'two independent halves', 'proposal': PROPOSAL,
                                                         'workers': 2}, **numbers)))
    f.pump()


def cheap(f):
    f.decompose_load()
    p = decompose.params_get(f.r)
    decompose.params_put(f.r, dict(p, split=dict(p['split'], margin=0), cost={k: 0 for k in p['cost']}))


def test_on_a_split_request_needs_delta_t_and_delta_c(mixed, decompose_on, splits):
    f = mixed
    dispatch_m0(f)
    escalate(f)
    note = [x for x in seen(f) if '[escalate] refused for' in x]
    assert note and 'delta_t (minutes the whole task finishes earlier) and delta_c (extra session minutes:' in note[0]
    assert 'M0' not in f.escalation_state()


def test_on_the_supervisor_sees_both_accounts(mixed, decompose_on, splits):
    f = mixed
    dispatch_m0(f)
    escalate(f, delta_t=20, delta_c=3)
    text = [x for x in seen(f) if 'asks to split M0' in x][0]
    assert 'Accounts: worker dT 20 / dC 3; program dT <= 3.2, dC 23 (sub-fleet, k=2): does not pay' in text
    assert f.escalation_state()['M0']['delta_t'] == 20


def test_on_an_approve_the_program_finds_not_worth_it_is_refused_without_a_strike(mixed, decompose_on, splits):
    f = mixed
    dispatch_m0(f)
    escalate(f, delta_t=20, delta_c=3)
    answer_turn(f, 'supervisor', reply(escalations=[{'task': 'M0', 'decision': 'approve', 'note': ''}]))
    refused = [x for x in supervisor_texts(f) if 'approve of M0 refused by the program' in x]
    assert refused and 'the split does not pay: dT 3.2 < k x dC = 2 x 23 (program; worker: dT 20, dC 3)' in refused[0]
    assert not splits and f.escalation_state()['M0']['status'] == 'escalated'
    assert int(f.r.hget(fleet.TASK, 'bad_replies') or 0) == 0
    assert node(tree(f), 'M0')['state'] == 'running' and not decompose.children(tree(f), 'M0')


def test_on_an_approved_split_lands_in_the_tree_and_the_return_fills_it_in(mixed, decompose_on, splits, tmp_path):
    f = mixed
    dispatch_m0(f)
    cheap(f)
    escalate(f, delta_t=4, delta_c=0)
    answer_turn(f, 'supervisor', reply(escalations=[{'task': 'M0', 'decision': 'approve', 'note': ''}]))
    assert splits and splits[0]['task_id'] == 'M0'
    t = tree(f)
    assert node(t, 'M0')['leaf'] is False and node(t, 'M0')['state'] == 'delegated'
    assert [(n['id'], n['state']) for n in t['nodes'] if n.get('parent') == 'M0'] == [('M0.0', 'delegated'),
                                                                                       ('M0.1', 'delegated')]
    assert t['splits'][-1]['by'] == 'worker' and t['splits'][-1]['claimed'] == {'dT': 4, 'dC': 0}
    child_run = tmp_path / 'child'
    child_run.mkdir()
    child = decompose.new_tree('M0', 'sub')
    child['nodes'] += [{'id': 'M0.0', 'parent': 'M0', 'state': 'reported',
                        'actual': dict(decompose.blank_actual(), minutes=6, session_minutes=6)},
                       {'id': 'M0.1', 'parent': 'M0', 'state': 'reported',
                        'actual': dict(decompose.blank_actual(), minutes=4, session_minutes=5)}]
    (child_run / 'tree.json').write_text(json.dumps(child))
    f.tree_collected({'task_id': 'M0'}, child_run, True)
    t = tree(f)
    m0 = node(t, 'M0')
    assert m0['state'] == 'reported' and m0['actual']['session_minutes'] >= 11 and m0['actual']['outcome'] == 'completed'
    assert node(t, 'M0.0')['state'] == 'reported' and node(t, 'M0.1')['actual']['session_minutes'] == 5
