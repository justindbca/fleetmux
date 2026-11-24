"""Leaf dispatch (docs/design.md §44 "Leaf blocks"): the supervisor marks a block not worth splitting further with
``"leaf": true``; the long-running nudge never asks that worker to split it, and every other dispatch is unchanged."""
import json

import pytest

from test_contracts import frozen_state, item
from test_long_split import flowing, probe_round, splittable, supervisor_texts  # noqa: F401
from test_v2_codex_claude import answer_turn, mixed, submit  # noqa: F401
from test_message_bus_v2 import private_redis_v2  # noqa: F401
from fleetmux import contracts, fleet


def leaf_decision(leaf):
    return json.dumps({'status': 'RUNNING', 'completion': 10, 'reason': 'one block',
                       'dispatch': [dict({'to': 'worker-1', 'instruction': 'Write worker-1/part.md with the findings.'},
                                         **({'leaf': leaf} if leaf is not None else {}))]})


def dispatch_leaf(f, leaf):
    submit(f)
    answer_turn(f, 'supervisor', leaf_decision(leaf))
    f.pump()


@pytest.mark.parametrize('bad', ['yes', 1, None])
def test_a_non_boolean_leaf_is_rejected(bad):
    one = {'to': 'worker-1', 'instruction': 'Write worker-1/a.md please', 'leaf': bad}
    with pytest.raises(ValueError, match='dispatch.leaf must be true or false'):
        fleet.parse_decision(json.dumps({'status': 'RUNNING', 'completion': 1, 'dispatch': [one]}), ['worker-1'], False)
    ok = dict(one, leaf=True)
    assert fleet.parse_decision(json.dumps({'status': 'RUNNING', 'completion': 1, 'dispatch': [ok]}),
                                ['worker-1'], False)['dispatch'][0]['leaf'] is True


def test_a_flowing_leaf_is_never_nudged_to_split(mixed, splittable):
    f = mixed
    dispatch_leaf(f, True)
    assert json.loads(f.r.get(fleet.ITEMS))['M0']['leaf'] is True
    flowing(f, 'worker-1')
    probe_round(f)
    info = f.agent('worker-1')
    assert not info.get('split_send_pending') and not info.get('split_nudged')
    assert 'not splittable: M0 is a leaf' in info['split_last']
    assert splittable == []  # refused before the sub-fleet limits are even asked
    assert not [x for x in supervisor_texts(f) if '[long-running]' in x]


def test_a_plain_dispatch_still_gets_the_nudge(mixed, splittable):
    f = mixed
    dispatch_leaf(f, False)
    assert 'leaf' not in json.loads(f.r.get(fleet.ITEMS))['M0']
    flowing(f, 'worker-1')
    probe_round(f)
    assert f.agent('worker-1')['split_nudged'] == 'M0'


def test_a_contract_row_keeps_leaf_and_a_redispatch_without_it_clears_it():
    state = frozen_state()
    contracts.dispatch(state, [item('M0', 'worker-1', provides=['IF-M0-src'], leaf=True)], lambda x: x)
    assert state['tasks']['M0']['leaf'] is True
    contracts.dispatch(state, [item('M0', 'worker-1', provides=['IF-M0-src'])], lambda x: x)
    assert state['tasks']['M0']['leaf'] is False


def test_every_task_supervisor_prompt_explains_leaf():
    agents = fleet.HERE / 'agents'
    for name in ('supervisor.md', 'supervisor_v2.md', 'contracts_supervisor.md'):
        assert '"leaf": true' in (agents / name).read_text(), name
