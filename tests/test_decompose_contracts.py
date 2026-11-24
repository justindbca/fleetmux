"""Contract tasks with the tree on (docs/design.md §62, map §3): the contract gates stay (P0 / P1 / readiness), the
P0 reply also hands in the tree, and a contract dispatch takes edits / depends_on / serial_reason from its tree block
when it leaves them out; written and different from the tree, it is refused."""
import pytest

from fleetmux import decompose
from test_contracts import THREADS, cf, inbox, item, p0_reply, report, submit, sup, write  # noqa: F401

TREE = [{'id': 'M0', 'goal': 'fetch sources', 'cut': 'decision', 'est': {'minutes': 10},
         'owns': ['worker-1/sources.md'], 'accept': 'sources.md has the Sources table'},
        {'id': 'M1', 'goal': 'compute metrics', 'cut': 'decision', 'est': {'minutes': 10},
         'owns': ['worker-2/metrics.json'], 'accept': 'metrics.json has symbol and iv_rank'},
        {'id': 'M2', 'goal': 'write the report', 'cut': 'function', 'est': {'minutes': 15},
         'owns': ['worker-3/report.md'], 'accept': 'report.md cites both'}]


def to_parallel_with_tree(f):
    submit(f, contracts=True)
    sup(f, p0_reply())  # the contract P0 without a tree: the tree's first-reply rule refuses it
    assert any('your first reply hands in the tree only' in x for x in inbox(f, 'supervisor'))
    sup(f, p0_reply(tree=TREE))
    assert f.contract_state()['phase'] == 'skeleton' and f.tree_state()['accepted'] == 1
    write(f, 'worker-1/skeleton.md', 'IF-M0-src@1 · M0 → M2 · PASS · ok\nIF-M1-metrics@1 · M1 → M2 · PASS · ok\n')
    report(f, 'worker-1')
    assert f.contract_state()['phase'] == 'parallel'
    f.r.l.clear()


def test_a_contract_dispatch_takes_its_edits_from_the_tree(cf, decompose_on):
    to_parallel_with_tree(cf)
    sup(cf, {'status': 'RUNNING', 'completion': 10, 'dispatch': [item('M0', 'worker-1', provides=['IF-M0-src'])]})
    assert cf.task()['bad_replies'] == '0', inbox(cf, 'supervisor')
    assert cf.contract_state()['tasks']['M0']['edits'] == ['worker-1/sources.md']


def test_a_contract_dispatch_that_differs_from_the_tree_is_refused(cf, decompose_on):
    to_parallel_with_tree(cf)
    sup(cf, {'status': 'RUNNING', 'completion': 10,
             'dispatch': [item('M1', 'worker-2', provides=['IF-M1-metrics'], edits=['worker-2/other.json'])]})
    assert cf.task()['bad_replies'] == '1'
    assert any('M1 edits differ from the tree (owns): change the tree, not the dispatch' in x
               for x in inbox(cf, 'supervisor'))
