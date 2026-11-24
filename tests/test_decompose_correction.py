"""A worker's report settles the block it is running by the tree (docs/design.md §62), switch on: not by the order of
fx:items keys. A correction of an earlier reported block (same id, same worker) is settled when its report comes,
so blocks after it can go. Several blocks out to one worker: a report settles the oldest dispatch whose clock runs
(deliveries reach a worker in order); a block whose report stopped its clock (Blocked / escalate) is skipped."""
import json
import time

from fleetmux import fleet
from test_decompose_fleet import answer_turn, hand_in_tree, node, reply, tree
from test_long_split import supervisor_texts
from test_message_bus_v2 import private_redis_v2  # noqa: F401
from test_v2_codex_claude import mixed, typed  # noqa: F401

DONE = 'Done: {}\nEvidence: ok\nNot done: none\nBlocked: none'
BLOCKED = 'Done: part of {}\nEvidence: ok\nNot done: the rest\nBlocked: which signature should check() take?'


def go(f, *items):
    out = answer_turn(f, 'supervisor', reply(dispatch=[{'to': 'worker-1', 'id': nid, 'instruction': text}
                                                       for nid, text in items]))
    f.pump()
    return out


def done(f, what, text=DONE):
    answer_turn(f, 'worker-1', text.format(what))
    f.pump()


def m0_and_m2_reported(f):
    hand_in_tree(f)
    go(f, ('M0', 'Write the skeleton and the contract tests.'))
    done(f, 'M0 skeleton')
    go(f, ('M2', 'Wire both send paths to the check.'))
    done(f, 'M2 wired')
    assert node(tree(f), 'M0')['state'] == 'reported' and node(tree(f), 'M2')['state'] == 'reported'


def test_on_a_correction_of_an_earlier_block_is_settled_by_its_report(mixed, decompose_on):
    f = mixed
    m0_and_m2_reported(f)
    go(f, ('M0', 'Fix: the contract test misses the empty-input case.'))  # a correction of reported M0
    assert node(tree(f), 'M0')['state'] == 'running'
    assert list(json.loads(f.r.get(fleet.ITEMS))) == ['M0', 'M2'] and f.report_item('worker-1') == 'M2'
    done(f, 'M0 fixed')
    m0 = node(tree(f), 'M0')
    assert m0['state'] == 'reported' and m0['actual']['dispatches'] == 2


def test_on_after_a_correction_the_block_after_it_can_go(mixed, decompose_on):
    f = mixed
    m0_and_m2_reported(f)
    go(f, ('M0', 'Fix: the contract test misses the empty-input case.'))
    done(f, 'M0 fixed')
    n = len(supervisor_texts(f))
    assert go(f, ('M1', 'Implement the check so the contract tests go green.'))['status'] == 'committed'
    assert 'M1' in json.loads(f.r.get(fleet.ITEMS))
    assert not [x for x in supervisor_texts(f)[n:] + typed(f, 'supervisor') if 'waits for M0' in x]


def test_on_two_blocks_out_to_one_worker_reports_settle_them_in_dispatch_order(mixed, decompose_on):
    f = mixed
    hand_in_tree(f)
    go(f, ('M0', 'Write the skeleton and the contract tests.'))
    done(f, 'M0 skeleton')
    go(f, ('M1', 'Implement the check.'), ('M2', 'Wire both send paths to the check.'))
    assert node(tree(f), 'M1')['state'] == node(tree(f), 'M2')['state'] == 'running'
    assert f.report_item('worker-1') == 'M2'  # fx:items' last key: not what the tree goes by
    done(f, 'M1 implemented')
    assert node(tree(f), 'M1')['state'] == 'reported' and node(tree(f), 'M2')['state'] == 'running'
    done(f, 'M2 wired')
    assert node(tree(f), 'M2')['state'] == 'reported'


def test_on_a_block_whose_report_stopped_its_clock_is_skipped(mixed, decompose_on):
    f = mixed
    hand_in_tree(f)
    go(f, ('M0', 'Write the skeleton and the contract tests.'))
    done(f, 'M0 skeleton')
    go(f, ('M1', 'Implement the check.'))
    done(f, 'M1', BLOCKED)  # waits for a ruling: running, clock stopped
    m1 = node(tree(f), 'M1')
    assert m1['state'] == 'running' and not m1['actual'].get('out_since')
    t = tree(f)
    nodes = {n['id']: n for n in t['nodes']}
    nodes['M2'].update(state='running', to='worker-1')  # as a dispatch of M2 to worker-1 leaves it
    now = time.time()  # dispatched after M1
    nodes['M2']['actual'].update(out_since=now, dispatched_at=now, started=now)
    f.save_tree(t)
    f.tree_reported('worker-1', 'M1', DONE.format('M2 wired'))
    assert node(tree(f), 'M2')['state'] == 'reported' and node(tree(f), 'M1')['state'] == 'running'
