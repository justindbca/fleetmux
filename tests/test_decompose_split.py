"""Split errors go back to the tree (docs/design.md §62), switch on: no long-running nudge to the worker; a block on the
longest chain past est x overrun_ratio is reported to the supervisor once; [blocked] rulings carry a cause and the
block's returns are counted, with one cause line at returns_alert; a worker's split request carries dT / dC."""
import json
import time

from fleetmux import decompose
from test_decompose_fleet import answer_turn, hand_in_tree, node, reply, tree
from test_long_split import supervisor_texts
from test_message_bus_v2 import private_redis_v2  # noqa: F401
from test_v2_codex_claude import mixed, typed  # noqa: F401

M0 = {'to': 'worker-1', 'id': 'M0', 'instruction': 'Write the skeleton and the contract tests.'}


def dispatch_m0(f):
    hand_in_tree(f)
    answer_turn(f, 'supervisor', reply(dispatch=[M0]))
    f.pump()


def age(f, nid, minutes, state=None):
    t = tree(f)
    n = decompose.by_id(t)[nid]
    n['actual']['started'] = time.time() - minutes * 60
    if state:
        n['state'] = state
    f.save_tree(t)


def overruns(f):
    return [x for x in supervisor_texts(f) if x.startswith('[overrun]')]


def test_on_a_long_busy_worker_is_never_asked_to_split(mixed, decompose_on):
    f = mixed
    dispatch_m0(f)
    f.setf('worker-1', state='busy', busy_since=time.time() - 3 * 3600)
    assert f.split_due('worker-1', f.agent('worker-1')) is None


def test_on_a_chain_block_past_its_overrun_ratio_is_reported_to_the_supervisor_once(mixed, decompose_on):
    f = mixed
    dispatch_m0(f)
    age(f, 'M0', 40)  # est 10 x overrun_ratio 5 = 50 min: not yet
    f.tree_overrun_tick()
    assert not overruns(f)
    age(f, 'M0', 55)
    f.tree_overrun_tick()
    f.tree_overrun_tick()
    note, = overruns(f)
    assert note.startswith('[overrun] M0 is on the longest chain and has run 55 min against est 10 (x5.5 > 5)')
    assert 'merge' in note and 'split' in note and node(tree(f), 'M0')['actual']['overrun_noted'] is True


def test_on_a_block_off_the_chain_is_not_reported(mixed, decompose_on):
    f = mixed
    dispatch_m0(f)
    t = tree(f)
    decompose.by_id(t)['M0']['state'] = 'reported'
    f.save_tree(t)
    age(f, 'M2', 500, state='running')  # M2 has 10 min slack: off the chain
    f.tree_overrun_tick()
    assert not overruns(f)


BLOCKED = 'Done: the skeleton\nEvidence: worker-1/notes.md\nNot done: the tests\nBlocked: which signature should check() take?'


def blocked_round(f, cause, minutes_out=2):
    t = tree(f)
    decompose.by_id(t)['M0']['actual']['out_since'] = time.time() - minutes_out * 60
    decompose.by_id(t)['M0']['state'], decompose.by_id(t)['M0']['to'] = 'running', 'worker-1'
    f.save_tree(t)
    answer_turn(f, 'worker-1', BLOCKED)
    f.pump()
    answer_turn(f, 'supervisor', reply(escalations=[{'task': 'blocked:worker-1', 'decision': 'approve',
                                                     'note': 'take check(order) -> bool', 'cause': cause}]))
    f.pump()


def test_on_a_cause_tagged_ruling_counts_a_return_of_the_block(mixed, decompose_on):
    f = mixed
    dispatch_m0(f)
    blocked_round(f, 'shared_decision')
    actual = node(tree(f), 'M0')['actual']
    assert actual['returns'] == 1 and actual['causes'] == {'shared_decision': 1}
    assert 1.9 <= actual['session_minutes'] <= 2.5  # dispatch to report, per dispatch
    assert not f.r.lrange(decompose.TREE_NOTES, 0, -1)


def test_on_returns_alert_adds_one_cause_line_for_the_supervisor(mixed, decompose_on):
    f = mixed
    dispatch_m0(f)
    for cause in ('shared_decision', 'file_overlap', 'shared_decision', 'scope'):
        blocked_round(f, cause)
    seen = f.r.lrange(decompose.TREE_NOTES, 0, -1) + supervisor_texts(f) + typed(f, 'supervisor')
    notes = [line for x in seen for line in x.splitlines() if 'came back' in line]
    assert notes == ['[tree] M0 came back 3 times: shared_decision 2, file_overlap 1. Consider merging it with the '
                     'block it waits on, extracting the decision it keeps asking about, or splitting it in the tree.']
    assert node(tree(f), 'M0')['actual']['returns'] == 4
