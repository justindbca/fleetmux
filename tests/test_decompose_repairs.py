"""Two repairs of the tree (docs/design.md §62), switch on: an approved split whose sub-fleet cannot start (the
request goes back to the supervisor) or returns failed is rolled back in the tree (its pieces go, the block can be
dispatched again); any ruling on a block restarts its session-minute clock, with or without a cause."""
import json
import time

from fleetmux import decompose, fleet, subfleet
from test_decompose_escalate import PROPOSAL, cheap, escalate, splits  # noqa: F401
from test_decompose_fleet import answer_turn, node, reply, tree
from test_decompose_split import dispatch_m0
from test_long_split import supervisor_texts
from test_message_bus_v2 import private_redis_v2  # noqa: F401
from test_v2_codex_claude import mixed, typed  # noqa: F401


def approved(f):
    dispatch_m0(f)
    cheap(f)
    escalate(f, delta_t=4, delta_c=0)
    answer_turn(f, 'supervisor', reply(escalations=[{'task': 'M0', 'decision': 'approve', 'note': ''}]))
    f.pump()
    t = tree(f)
    assert node(t, 'M0')['state'] == 'delegated' and decompose.children(t, 'M0') == ['M0.0', 'M0.1']


def test_on_a_refused_spawn_rolls_the_split_back_and_a_new_approve_writes_it_again(mixed, decompose_on, splits):
    f = mixed
    approved(f)
    rec = f.escalation_state()['M0']
    assert rec['status'] == 'approved'
    fleet.save(subfleet.answer_path(f.run_dir, rec['rid']), {'status': 'refused', 'reason': 'no room'})
    f.escalation_tick()
    assert f.escalation_state()['M0']['status'] == 'escalated'
    t = tree(f)
    m0 = node(t, 'M0')
    assert not decompose.children(t, 'M0') and m0['state'] == 'running' and m0['to'] == 'worker-1'
    assert 'delegated_at' not in m0
    assert t['splits'][-1]['rolled_back'].startswith('sub-fleet could not start: no room')
    f.pump()
    r = answer_turn(f, 'supervisor', reply(escalations=[{'task': 'M0', 'decision': 'approve', 'note': ''}]))
    f.pump()
    t = tree(f)
    assert len(t['splits']) == 2, (r, supervisor_texts(f)[-3:])
    assert node(t, 'M0')['state'] == 'delegated' and decompose.children(t, 'M0') == ['M0.0', 'M0.1'], (r, supervisor_texts(f)[-3:])


def test_on_a_failed_sub_fleet_rolls_the_split_back_and_the_block_can_go_again(mixed, decompose_on, splits,
                                                                                tmp_path):
    f = mixed
    approved(f)
    child_run = tmp_path / 'child'
    child_run.mkdir()
    child = decompose.new_tree('M0', 'sub')
    child['nodes'] += [{'id': 'M0.0', 'parent': 'M0', 'state': 'reported',
                        'actual': dict(decompose.blank_actual(), minutes=6, session_minutes=6)},
                       {'id': 'M0.1', 'parent': 'M0', 'state': 'running',
                        'actual': dict(decompose.blank_actual(), minutes=4, session_minutes=3)}]
    (child_run / 'tree.json').write_text(json.dumps(child))
    f.tree_collected({'task_id': 'M0'}, child_run, False)
    t = tree(f)
    m0 = node(t, 'M0')
    assert not decompose.children(t, 'M0') and m0['state'] == 'planned' and not m0.get('to')
    assert m0['actual']['outcome'] == 'failed' and m0['actual']['session_minutes'] >= 9
    assert t['splits'][-1]['rolled_back'] == 'sub-fleet failed' and t['splits'][-1]['session_minutes'] == 9
    assert 'M0' in decompose.dispatchable(t)
    f.save_escalations({})
    f.queue('supervisor', '[Sub-fleet M0 failed] Reply with the JSON format from AGENTS.md.')  # what collect sends
    f.pump()
    assert answer_turn(f, 'supervisor', reply(dispatch=[{'to': 'worker-2', 'id': 'M0', 'instruction':
                                                         'Write the skeleton and the contract tests.'}]))['status'] \
        == 'committed'
    f.pump()
    assert node(tree(f), 'M0')['state'] == 'running' and node(tree(f), 'M0')['to'] == 'worker-2'


def test_on_a_ruling_without_cause_restarts_the_session_clock(mixed, decompose_on, splits):
    f = mixed
    dispatch_m0(f)
    escalate(f, delta_t=20, delta_c=3)  # the report with the escalate stops the clock, the block stays running
    t = tree(f)
    assert node(t, 'M0')['state'] == 'running' and not node(t, 'M0')['actual'].get('out_since')
    before = time.time()
    answer_turn(f, 'supervisor', reply(escalations=[{'task': 'M0', 'decision': 'reject', 'note': 'do it yourself'}]))
    f.pump()
    actual = node(tree(f), 'M0')['actual']
    assert actual['out_since'] >= before and actual['returns'] == 0
    assert any('rejected splitting M0' in x for x in typed(f, 'worker-1'))
    assert not [x for x in supervisor_texts(f) if 'refused' in x and 'M0' in x and 'cause' in x]
