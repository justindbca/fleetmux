"""The two accounts of a split (docs/design.md §62): dT = minutes the whole task finishes earlier on the longest chain
(0 off it), dC = extra session minutes (cold starts, decisions to agree, the merge; a sub-fleet adds the spawn and its
supervisor for the sub-chain); split only when dT >= k x dC and dT >= margin. A tree split (split_from) is checked with
the program's own account; the dispatch width counts the blocks out and the new ones."""
import copy

import pytest

from fleetmux import decompose
from test_decompose_tree import CTX, PARAMS, sample, tree0


def base():
    return decompose.judge(decompose.apply(tree0(), sample(), [], CTX), PARAMS)


def halves(minutes, serial=False):
    return [{'id': 'p0', 'est': {'minutes': minutes}}, {'id': 'p1', 'est': {'minutes': minutes},
                                                         'after': ['p0'] if serial else []}]


def test_a_block_off_the_longest_chain_saves_nothing():
    acc = decompose.split_account(base(), 'M2', halves(7.5), PARAMS)
    assert acc['dT'] == 0 and acc['split'] is False and 'off the longest chain' in acc['reason']


def test_a_chain_block_split_in_two_pays_when_dt_reaches_k_times_dc():
    acc = decompose.split_account(base(), 'M1', halves(12.5), PARAMS)
    # M0 10 + 12.5 + merge 2 = 24.5 < M2's 25: the chain becomes M0 -> M2 -> M3, 35 min (was 45)
    assert acc == {'dT': 10, 'dC': 5, 'split': True, 'reason': 'dT 10 >= k x dC = 2 x 5'}


def test_each_new_decision_costs_its_agreement():
    acc = decompose.split_account(base(), 'M1', halves(12.5), PARAMS, new_decisions=1)
    assert acc['dC'] == 7 and acc['split'] is False and acc['reason'] == 'dT 10 < k x dC = 2 x 7'


def test_a_sub_fleet_adds_the_spawn_every_cold_start_and_its_supervisor():
    acc = decompose.split_account(base(), 'M1', halves(12.5), PARAMS, subfleet=True)
    # spawn 0.3 + 2 cold starts x 3 + merge 2 + supervisor 1 x sub-chain (12.5 + merge 2) = 22.8
    assert acc['dT'] == 10 and acc['dC'] == 22.8 and acc['split'] is False


def test_margin_and_a_serial_cut_stop_a_split():
    params = copy.deepcopy(PARAMS)
    params['split']['margin'] = 11
    assert decompose.split_account(base(), 'M1', halves(12.5), params)['reason'] == 'dT 10 < margin 11'
    assert decompose.split_account(base(), 'M1', halves(12.5, serial=True), PARAMS)['dT'] == 0


def split_reply(d_t=12, d_c=2, minutes=12.5):
    piece = {'cut': 'decision', 'est': {'minutes': minutes}, 'consumes': ['D1', 'D2'], 'after': ['M0'],
             'accept': 'its tests green', 'split_from': {'id': 'M1', 'dT': d_t, 'dC': d_c}}
    return [dict(piece, id='M4', goal='size check', owns=['gate/size.py']),
            dict(piece, id='M5', goal='price check', owns=['gate/price.py']),
            {'id': 'M3', 'depends_on': ['M4', 'M5', 'M2']}]


def test_a_tree_split_that_pays_is_recorded_with_both_accounts():
    t = decompose.apply(base(), split_reply(), ['M1'], CTX)
    assert t['splits'] == [{'of': 'M1', 'pieces': ['M4', 'M5'], 'dT': 10, 'dC': 5, 'claimed': {'dT': 12, 'dC': 2},
                            'by': 'supervisor', 'est': {'minutes': 25}}]  # est: what the split replaced (review)


def test_a_tree_split_the_program_does_not_find_worth_it_is_refused():
    with pytest.raises(ValueError, match=r'split of M1 refused: dT 0 < k x dC = 2 x 5 \(the program.s account; '
                                         r'yours: dT 12, dC 2\)'):
        decompose.apply(base(), split_reply(minutes=24), ['M1'], CTX)


def test_the_width_counts_blocks_out_and_new_ones_but_not_a_correction():
    t = base()
    t['width'] = 1
    nodes = decompose.by_id(t)
    nodes['M0']['state'], nodes['M1']['state'] = 'reported', 'running'  # M2 is ready (D1's owner reported)
    with pytest.raises(ValueError, match="would run 2 blocks at once but the tree's max parallel width is 1"):
        decompose.dispatch_errors(t, [{'id': 'M2', 'to': 'worker-2'}], ['M1'])
    decompose.dispatch_errors(t, [{'id': 'M0', 'to': 'worker-2'}], ['M1'])  # a correction starts nothing new


def test_the_leaf_bound_is_the_two_halves_account():
    t = base()
    m1 = decompose.by_id(t)['M1']
    assert m1['bound'] == {'dT': 10, 'dC': 5}
    acc = decompose.split_account(t, 'M1', halves(12.5), PARAMS)
    assert (acc['dT'], acc['dC']) == (m1['bound']['dT'], m1['bound']['dC'])
