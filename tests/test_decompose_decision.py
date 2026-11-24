"""parse_decision with the decomposition tree (docs/design.md §62), switch on and off: the first reply hands in the
tree only (a valid tree is the reply's progress), dispatch ids are blocks of the tree, [blocked] rulings carry a
cause, decompose_tune rides only on COMPLETED. Off: the new keys are ignored and every old message stays."""
import json

import pytest

from fleetmux import decompose, fleet
from test_decompose_tree import PARAMS, sample

WORKERS = ['worker-1', 'worker-2']


def ctx(tree=None, accepted=False):
    tree = tree or decompose.new_tree('root', 'goal')
    if accepted:
        tree = decompose.judge(decompose.apply(tree, sample(), [], {'root': 'root', 'prefix': 'M', 'params': PARAMS}),
                               PARAMS)
        tree['accepted'] = tree['rev'] = 1
    return {'tree': tree, 'root': 'root', 'prefix': 'M', 'params': PARAMS, 'running': []}


def reply(**kw):
    return json.dumps(dict({'status': 'RUNNING', 'completion': 5, 'dispatch': []}, **kw))


def one(nid, to='worker-1'):
    return {'to': to, 'id': nid, 'instruction': 'Do block ' + nid + ' and report the result.'}


def test_on_the_first_reply_may_hand_in_the_tree_only_while_nobody_is_busy():
    d = fleet.parse_decision(reply(tree=sample()), WORKERS, False, ctx())
    assert {n['id'] for n in d['_tree']['nodes']} == {'root', 'M0', 'M1', 'M2', 'M3'}


def test_on_the_first_reply_may_not_dispatch_nor_skip_the_tree():
    with pytest.raises(ValueError, match='your first reply hands in the tree only'):
        fleet.parse_decision(reply(tree=sample(), dispatch=[one('M0')]), WORKERS, False, ctx())
    with pytest.raises(ValueError, match='your first reply hands in the tree only'):
        fleet.parse_decision(reply(dispatch=[one('M0')]), WORKERS, False, ctx())


def test_on_a_seeded_tree_is_not_accepted_until_the_supervisor_hands_it_in():
    seeded = decompose.apply(decompose.new_tree('root', 'goal'), sample(), [], {'root': 'root', 'prefix': 'M',
                                                                              'params': PARAMS})
    assert seeded['accepted'] == 0
    with pytest.raises(ValueError, match='your first reply hands in the tree only'):
        fleet.parse_decision(reply(dispatch=[one('M0')]), WORKERS, False, ctx(seeded))
    d = fleet.parse_decision(reply(tree=[{'id': 'M0', 'est': {'minutes': 12}}]), WORKERS, False, ctx(seeded))
    assert d['_tree'] is not None


@pytest.mark.parametrize('item, error', [
    ({'to': 'worker-1', 'instruction': 'Write worker-1/x.md with notes.'}, 'every dispatch item needs the id of a block'),
    (one('M7'), 'dispatch id M7 is not in the tree: add it to the tree first'),
    (one('M2'), 'M2 consumes D1, still open with M0; dispatch it when that is done'),
])
def test_on_a_dispatch_must_be_a_ready_block_of_the_tree(item, error):
    with pytest.raises(ValueError, match=error):
        fleet.parse_decision(reply(dispatch=[item]), WORKERS, False, ctx(accepted=True))
    assert fleet.parse_decision(reply(dispatch=[one('M0')]), WORKERS, False, ctx(accepted=True))['_tree'] is None


def test_on_a_tree_change_counts_as_progress_after_the_first_tree():
    d = fleet.parse_decision(reply(tree=[{'id': 'M4', 'goal': 'docs', 'cut': 'function', 'est': {'minutes': 5},
                                          'accept': 'docs updated', 'owns': ['docs/x.md']}]),
                             WORKERS, False, ctx(accepted=True))
    assert 'M4' in {n['id'] for n in d['_tree']['nodes']}
    with pytest.raises(ValueError, match='no worker is busy: RUNNING must dispatch'):
        fleet.parse_decision(reply(), WORKERS, False, ctx(accepted=True))


def test_on_a_blocked_ruling_needs_a_cause():
    esc = {'task': 'blocked:worker-1', 'decision': 'approve', 'note': 'go on'}
    with pytest.raises(ValueError, match='a .blocked. ruling needs a cause'):
        fleet.parse_decision(reply(escalations=[esc]), WORKERS, False, ctx(accepted=True))
    with pytest.raises(ValueError, match='cause must be one of'):
        fleet.parse_decision(reply(escalations=[dict(esc, cause='bad luck')]), WORKERS, False, ctx(accepted=True))
    fleet.parse_decision(reply(escalations=[dict(esc, cause='file_overlap')]), WORKERS, False, ctx(accepted=True))


def test_on_decompose_tune_rides_only_on_completed():
    tune = [{'param': 'split.k', 'value': 3, 'reason': 'splits cost more than they saved'}]
    with pytest.raises(ValueError, match='decompose_tune is for the closing COMPLETED reply only'):
        fleet.parse_decision(reply(decompose_tune=tune, dispatch=[one('M0')]), WORKERS, False, ctx(accepted=True))
    fleet.parse_decision(json.dumps({'status': 'COMPLETED', 'completion': 100, 'decompose_tune': tune}),
                         WORKERS, False, ctx(accepted=True))
    with pytest.raises(ValueError, match='decompose_tune: split.k has a bad value'):
        fleet.parse_decision(json.dumps({'status': 'COMPLETED', 'completion': 100,
                                         'decompose_tune': [dict(tune[0], value=0)]}), WORKERS, False, ctx(accepted=True))


def test_off_the_tree_keys_are_ignored_and_the_old_messages_stay():
    with pytest.raises(ValueError, match='no worker is busy: RUNNING must dispatch at least one instruction'):
        fleet.parse_decision(reply(tree=sample()), WORKERS, False)
    d = fleet.parse_decision(reply(dispatch=[{'to': 'worker-1', 'instruction': 'Write worker-1/x.md with notes.',
                                              'leaf': True}], tree_drop=['M9'],
                                   escalations=[{'task': 'blocked:worker-2', 'decision': 'approve'}],
                                   decompose_tune='anything'), WORKERS, False)
    assert d['dispatch'][0]['leaf'] is True and '_tree' not in d
