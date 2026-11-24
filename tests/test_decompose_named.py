"""A named sub-fleet's blocks carry its own prefix (docs/design.md §62): with the tree on, ``foo.0`` is a block id of
sub-fleet ``foo`` and the tree checks it; with the tree off the dispatch id check and its message are the baseline."""
import json

import pytest

from fleetmux import decompose, fleet
from test_decompose_tree import PARAMS

BLOCK = {'id': 'foo.0', 'goal': 'collect the numbers', 'cut': 'function', 'est': {'minutes': 5},
         'accept': 'numbers.md written', 'owns': ['worker-1/numbers.md']}


def named_ctx():
    ctx = {'root': 'foo', 'prefix': 'foo.', 'params': PARAMS}
    tree = decompose.judge(decompose.apply(decompose.new_tree('foo', 'named job'), [BLOCK], [], ctx), PARAMS)
    tree['accepted'] = tree['rev'] = 1
    return dict(ctx, tree=tree, running=[])


def reply(nid):
    return json.dumps({'status': 'RUNNING', 'completion': 5, 'dispatch': [
        {'to': 'worker-1', 'id': nid, 'instruction': 'Collect the numbers into worker-1/numbers.md.'}]})


def test_on_a_named_sub_fleet_dispatches_its_own_prefixed_block():
    d = fleet.parse_decision(reply('foo.0'), ['worker-1'], False, named_ctx())
    assert d['dispatch'][0]['id'] == 'foo.0'


def test_on_an_id_of_another_level_is_refused_by_the_tree():
    with pytest.raises(ValueError, match='dispatch id M0 is not in the tree'):
        fleet.parse_decision(reply('M0'), ['worker-1'], False, named_ctx())


def test_on_a_non_text_id_gets_the_tree_message():
    with pytest.raises(ValueError, match='every dispatch item needs the id of a block'):
        fleet.parse_decision(json.dumps({'status': 'RUNNING', 'completion': 5, 'dispatch': [
            {'to': 'worker-1', 'id': 5, 'instruction': 'Collect the numbers into worker-1/numbers.md.'}]}),
            ['worker-1'], False, named_ctx())


def test_off_the_same_id_gets_the_baseline_message():
    with pytest.raises(ValueError) as exc:
        fleet.parse_decision(reply('foo.0'), ['worker-1'], False)
    assert str(exc.value) == 'every dispatch item needs an id (M<n>, e.g. M0)'
