"""Delivery gate: a round-2 PASS that still lists a resolved P1 is not blocked (2026-11-10 P1c delivery).

The reviewer's later rounds keep earlier P0 / P1 in the issue list with status fixed / closed. The gate
used to count them by severity alone and blocked a clean PASS; only open P0 / P1 block now.
"""
import json

from cmux_fleet import lifecycle_deliver as d


def _verdict(tmp_path, verdict, issues, rnd=2, reviewer='opus'):
    path = tmp_path / 'verdict.json'
    path.write_text(json.dumps({'round': rnd, 'verdict': verdict, 'reviewer': reviewer, 'issues': issues}))
    data, problem = d.read_verdict(path)
    assert problem is None
    return data


def test_pass_with_resolved_p1_is_clean(tmp_path):
    for status in ('fixed', 'closed', 'Closed'):
        v = _verdict(tmp_path, 'PASS', [{'id': 'P1-1', 'severity': 'P1', 'title': 'x', 'status': status},
                                        {'id': 'P2-1', 'severity': 'P2', 'title': 'y', 'status': 'ledger'}])
        assert d.review_problem(v, 'opus') is None
        assert [i['id'] for i in d.outstanding(v)] == ['P2-1']


def test_pass_with_open_p0_p1_still_blocks(tmp_path):
    v = _verdict(tmp_path, 'PASS', [{'id': 'P1-1', 'severity': 'P1', 'title': 'x', 'status': 'closed'},
                                    {'id': 'P1-2', 'severity': 'P1', 'title': 'y'},
                                    {'id': 'P0-1', 'severity': 'P0', 'title': 'z', 'status': 'open'}])
    assert d.review_problem(v, 'opus') == 'review PASS still lists 1 P0 / 1 P1'


def test_fail_and_wrong_reviewer_unchanged(tmp_path):
    v = _verdict(tmp_path, 'FAIL', [{'id': 'P1-1', 'severity': 'P1', 'title': 'x', 'status': 'fixed'}])
    assert d.review_problem(v, 'opus') == 'review verdict is FAIL (round 2)'
    v = _verdict(tmp_path, 'PASS', [], reviewer='codex')
    assert 'the mode table asks for opus' in d.review_problem(v, 'opus')
