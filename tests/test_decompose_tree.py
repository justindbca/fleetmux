"""Decomposition tree, pure functions (docs/design.md §62): apply / drop, the shape checks, the longest chain, the
widest parallel step and the program's leaf judgement."""
import pytest

from fleetmux import decompose

PARAMS = dict(decompose.DEFAULTS, split={'k': 2, 'margin': None, 'floor': 2},
              cost={'cold_start': 3, 'per_decision': 2, 'merge': 2, 'supervisor': 1, 'spawn': 0.3})
CTX = {'root': 'root', 'prefix': 'M', 'params': PARAMS}


def tree0():
    return decompose.new_tree('root', 'add one pre-send check to both accounts')


def sample():
    """fable.md §6.1 with parent: M0 -> M1 -> M3 is the longest chain (45 min); M2 waits for D1 (M0), then runs
    beside M1."""
    return [
        {'decision': {'id': 'D1', 'q': 'check() signature', 'owner': 'M0', 'status': 'open'}},
        {'decision': {'id': 'D2', 'q': 'cancel or just not send', 'owner': 'supervisor', 'status': 'pending_user',
                      'answer': 'just not send'}},
        {'id': 'M0', 'goal': 'skeleton + contract tests', 'cut': 'decision', 'est': {'minutes': 10},
         'owns': ['gate/check.py', 'tests/test_check_contract.py'], 'decides': ['D1'], 'accept': 'contract tests red'},
        {'id': 'M1', 'goal': 'implement the check', 'cut': 'decision', 'est': {'minutes': 25},
         'owns': ['gate/check.py'], 'consumes': ['D1', 'D2'], 'after': ['M0'], 'accept': 'contract tests green'},
        {'id': 'M2', 'goal': 'wire both send paths', 'cut': 'decision', 'est': {'minutes': 15},
         'owns': ['engine/send.py'], 'consumes': ['D1'], 'accept': 'tests/test_send.py green'},
        {'id': 'M3', 'goal': 'integrate', 'cut': 'serial', 'est': {'minutes': 10}, 'depends_on': ['M1', 'M2'],
         'serial_reason': 'integration', 'accept': 'merged, module tests green'},
    ]


def node(tree, nid):
    return next(n for n in tree['nodes'] if n['id'] == nid)


def test_apply_adds_and_changes_nodes_and_drop_removes_them_without_touching_the_input():
    t0 = tree0()
    t1 = decompose.apply(t0, sample(), [], CTX)
    assert [n['id'] for n in t0['nodes']] == ['root']
    assert {n['id'] for n in t1['nodes']} == {'root', 'M0', 'M1', 'M2', 'M3'}
    assert node(t1, 'M1')['parent'] == 'root' and node(t1, 'M1')['state'] == 'planned'
    t2 = decompose.apply(t1, [{'id': 'M2', 'est': {'minutes': 20}}], [], CTX)
    assert node(t2, 'M2')['est']['minutes'] == 20 and node(t2, 'M2')['owns'] == ['engine/send.py']
    t3 = decompose.apply(t2, [{'id': 'M3', 'depends_on': ['M1'], 'serial_reason': 'integration'}], ['M2'], CTX)
    assert 'M2' not in {n['id'] for n in t3['nodes']}


def test_parallel_siblings_may_not_own_the_same_file_unless_ordered():
    items = [{'id': 'M0', 'goal': 'a', 'cut': 'function', 'est': {'minutes': 5}, 'owns': ['pkg/a.py'], 'accept': 'x'},
             {'id': 'M1', 'goal': 'b', 'cut': 'function', 'est': {'minutes': 5}, 'owns': ['pkg/'], 'accept': 'y'}]
    with pytest.raises(ValueError, match='M0 and M1 both own'):
        decompose.apply(tree0(), items, [], CTX)  # a directory owns the files below it
    items[1]['after'] = ['M0']
    decompose.apply(tree0(), items, [], CTX)


@pytest.mark.parametrize('change, error', [
    ({'decision': {'id': 'D5', 'q': 'q', 'status': 'open'}}, 'decision D5 needs an owner'),
    ({'id': 'M2', 'decides': ['D1']}, 'D1 is decided by M0 and M2'),
    ({'id': 'M2', 'consumes': ['D9']}, 'M2 consumes D9, which is not a decision in the tree'),
    ({'decision': {'id': 'D3', 'q': 'q', 'owner': 'supervisor', 'status': 'decided'}}, 'D3 is decided: give its answer'),
])
def test_decision_ownership_is_checked(change, error):
    t = decompose.apply(tree0(), sample(), [], CTX)
    with pytest.raises(ValueError, match=error):
        decompose.apply(t, [change], [], CTX)


def test_depends_on_needs_a_serial_reason_and_no_cycle():
    t = decompose.apply(tree0(), sample(), [], CTX)
    with pytest.raises(ValueError, match='depends_on needs serial_reason'):
        decompose.apply(t, [{'id': 'M2', 'depends_on': ['M3']}], [], CTX)
    with pytest.raises(ValueError, match='cycle: '):
        decompose.apply(t, [{'id': 'M0', 'after': ['M3']}], [], CTX)


def test_a_block_needs_an_estimate_and_an_acceptance_check():
    with pytest.raises(ValueError, match='M0 needs est.minutes'):
        decompose.apply(tree0(), [{'id': 'M0', 'goal': 'g', 'cut': 'function', 'accept': 'x'}], [], CTX)
    with pytest.raises(ValueError, match='M0 needs accept'):
        decompose.apply(tree0(), [{'id': 'M0', 'goal': 'g', 'cut': 'function', 'est': {'minutes': 3}}], [], CTX)


def test_a_block_below_floor_with_a_sibling_must_be_merged():
    one = {'id': 'M0', 'goal': 'g', 'cut': 'function', 'est': {'minutes': 1}, 'accept': 'x'}
    decompose.apply(tree0(), [one], [], CTX)  # alone: nothing to merge it into
    two = dict(one, id='M1', est={'minutes': 8}, owns=['b.py'])
    with pytest.raises(ValueError, match='M0 .est 1 min. is below floor 2 min: merge it into a sibling'):
        decompose.apply(tree0(), [dict(one, owns=['a.py']), two], [], CTX)


def test_program_fields_are_refused():
    with pytest.raises(ValueError, match='leaf is written by the program'):
        decompose.apply(tree0(), [dict(sample()[2], leaf=True)], [], CTX)
    with pytest.raises(ValueError, match='actual is written by the program'):
        decompose.apply(tree0(), [dict(sample()[2], actual={'minutes': 1})], [], CTX)


def test_ids_belong_to_this_fleets_level():
    with pytest.raises(ValueError, match='M3.1 is not an id of this level'):
        decompose.apply(tree0(), [dict(sample()[2], id='M3.1', decides=[])], [], CTX)
    sub = {'root': 'M1', 'prefix': 'M1.', 'params': PARAMS}
    t = decompose.new_tree('M1', 'sub task')
    decompose.apply(t, [dict(sample()[2], id='M1.2', decides=[])], [], sub)
    with pytest.raises(ValueError, match='M2 is not an id of this level'):
        decompose.apply(t, [dict(sample()[2], id='M2', decides=[])], [], sub)


def test_schedule_finds_the_longest_chain_and_the_width():
    t = decompose.apply(tree0(), sample(), [], CTX)
    s = decompose.schedule(t)
    assert s['chain'] == ['M0', 'M1', 'M3'] and s['minutes'] == 45
    assert s['width'] == 2
    assert s['slack']['M2'] == 10 and s['slack']['M1'] == 0


def test_judge_marks_off_chain_blocks_leaf_and_bounds_chain_blocks():
    t = decompose.judge(decompose.apply(tree0(), sample(), [], CTX), PARAMS)
    assert node(t, 'M2')['leaf'] is True and 'off the longest chain' in node(t, 'M2')['leaf_reason']
    assert node(t, 'M1')['critical'] is True and node(t, 'M2')['critical'] is False
    m1 = node(t, 'M1')  # halving 25 min + 2 merge: the chain becomes M2 -> M3 (35 min), saves 10 >= 2 x 5: may pay
    assert m1['leaf'] is False and m1['bound'] == {'dT': 10, 'dC': 5}
    m0 = node(t, 'M0')  # halving 10 min + 2 merge saves 3 min < 2 x 5: not worth it
    assert m0['leaf'] is True and m0['bound'] == {'dT': 3, 'dC': 5}
    assert t['chain'] == {'ids': ['M0', 'M1', 'M3'], 'minutes': 45} and t['width'] == 2


def test_the_supervisors_claim_decides_a_block_the_bound_cannot():
    items = sample()
    items[3] = dict(items[3], claim={'leaf': True, 'reason': 'one function, cannot be cut'})
    t = decompose.judge(decompose.apply(tree0(), items, [], CTX), PARAMS)
    assert node(t, 'M1')['leaf'] is True and node(t, 'M1')['leaf_reason'].startswith('supervisor: one function')


def test_accepted_message_lists_dispatchable_blocks_and_the_chain_blocks_that_may_pay():
    t = decompose.judge(decompose.apply(tree0(), sample(), [], CTX), PARAMS)
    t['rev'] = 1
    text = decompose.accepted_message(t)
    assert text.startswith('[tree] rev 1 accepted: longest chain M0 -> M1 -> M3 (45 min), max parallel width 2')
    assert 'dispatchable now: M0;' in text  # M2 consumes D1, still open with M0
    assert 'M1 (dT <= 10 min, dC >= 5 session min)' in text and 'M0 (' not in text.split('leaf=false')[-1]
