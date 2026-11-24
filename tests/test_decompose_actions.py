"""The three ways to fix a split in the tree (docs/design.md §62): merge (two blocks that wait on each other become
one: merge_of, both dropped, the new block owns what they owned), extract (a question several blocks ask becomes a
decision they consume), split (split_from, checked with the program's account)."""
import pytest

from fleetmux import decompose
from test_decompose_account import base, split_reply
from test_decompose_tree import CTX


def node(t, nid):
    return decompose.by_id(t)[nid]


MERGED = {'id': 'M4', 'goal': 'implement and wire the check', 'cut': 'decision', 'est': {'minutes': 35},
          'owns': ['gate/check.py', 'engine/send.py'], 'consumes': ['D1', 'D2'], 'after': ['M0'],
          'accept': 'contract tests and send tests green', 'merge_of': ['M1', 'M2']}


def test_merge_two_blocks_into_one():
    t = decompose.apply(base(), [MERGED, {'id': 'M3', 'depends_on': ['M4']}], ['M1', 'M2'], CTX)
    assert {n['id'] for n in decompose.level_nodes(t)} == {'M0', 'M3', 'M4'}
    assert node(t, 'M4')['merge_of'] == ['M1', 'M2']
    assert decompose.schedule(t)['chain'] == ['M0', 'M4', 'M3']


def test_a_merge_keeps_what_the_merged_blocks_owned_and_drops_them():
    with pytest.raises(ValueError, match='a merge owns what the merged blocks owned; missing engine/send.py'):
        decompose.apply(base(), [dict(MERGED, owns=['gate/check.py']), {'id': 'M3', 'depends_on': ['M4']}],
                        ['M1', 'M2'], CTX)
    with pytest.raises(ValueError, match='merge_of M2 must be in tree_drop of this reply'):
        decompose.apply(base(), [MERGED, {'id': 'M3', 'depends_on': ['M4', 'M2']}], ['M1'], CTX)


def test_extract_a_decision_several_blocks_ask_about():
    change = [{'decision': {'id': 'D3', 'q': 'log failures where', 'owner': 'supervisor', 'status': 'decided',
                            'answer': 'gate/log.txt'}},
              {'id': 'M1', 'consumes': ['D1', 'D2', 'D3']}, {'id': 'M2', 'consumes': ['D1', 'D3']}]
    t = decompose.apply(base(), change, [], CTX)
    assert {d['id'] for d in t['decisions']} == {'D1', 'D2', 'D3'} and 'D3' in node(t, 'M2')['consumes']
    assert decompose.brief(t, 'M2').endswith('accepted by: tests/test_send.py green') and 'D3 = gate/log.txt' in \
        decompose.brief(t, 'M2')
    owned = [{'decision': {'id': 'D4', 'q': 'retry policy', 'owner': 'M0', 'status': 'open'}},
             {'id': 'M0', 'decides': ['D1', 'D4']}, {'id': 'M2', 'consumes': ['D1', 'D4']}]
    t = decompose.apply(base(), owned, [], CTX)
    assert decompose.waits(t, 'M2') == 'consumes D1, still open with M0'


def test_split_a_block_with_the_programs_account():
    t = decompose.apply(base(), split_reply(), ['M1'], CTX)
    assert {n['id'] for n in decompose.level_nodes(t)} == {'M0', 'M2', 'M3', 'M4', 'M5'}
    assert t['splits'][-1]['of'] == 'M1'
