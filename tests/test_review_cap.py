"""Fleet reviews are capped and narrowed (no-typing batch, step 3b).

Incident 2026-10-13: the codex-notype review ran to round 16. Now (1) dev_task's review_step has
max_rounds 3 + on_cap stop_and_report: a FAIL verdict at round 3 stops the rounds and hands the open
P0/P1 to the user in the result; (2) every fleet worker gets the fleet copy of the review rules
(REVIEWER.md) and is told to follow it for review instructions; (3) the task supervisor prompts carry
the narrowing principle (go live quickly, round 2+ only reruns round-1 tests, P2 not written).
"""
import json
from pathlib import Path

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import fx  # noqa: F401  (fixture)
from test_lifecycle_close import (as_task, deliver_spec, edit_table, notes, repo, result_json, stop_tail,  # noqa: F401
                                  submit, table, write_verdict)
from cmux_fleet import fleet, lifecycle

AGENTS = Path(fleet.__file__).resolve().parent / 'agents'
ISSUES = [{'id': 'P1-07', 'severity': 'P1', 'title': 'late settle race'},
          {'id': 'P2-03', 'severity': 'P2', 'title': 'log wording'}]


def test_dev_task_review_step_is_capped_in_the_mode_table():
    review = lifecycle.mode_policy('dev_task', 'review_step')
    assert review['max_rounds'] == 3 and review['on_cap'] == 'stop_and_report'
    assert review['reviewer'] == 'opus' and review['must_pass'] is True  # unchanged


def test_dev_task_review_cap_stops_the_rounds_and_reports_the_open_issues(fx, table, notes, repo, tmp_path):  # noqa: F811
    f = as_task(fx, 'dev_task')
    submit(f, {'goal': 'ship feature x', 'deliver': deliver_spec(repo, tmp_path)})
    write_verdict(f, 'FAIL', rnd=2, issues=ISSUES)
    f.tick()
    assert f.task()['status'] == 'running'  # round 2 < 3: the next round may run
    write_verdict(f, 'FAIL', rnd=3, issues=ISSUES)
    f.tick()
    task = f.task()
    assert task['status'] == 'blocked' and 'dev_task review: round 3 of max_rounds 3' in task['reason']
    listing = json.loads((f.run_dir / 'outstanding.json').read_text())
    assert [i['id'] for i in listing['issues']] == ['P1-07', 'P2-03'] and listing['max_rounds'] == 3  # open P1 + deferred P2
    assert (f.run_dir / 'outstanding.md').read_text().startswith('# 挂账清单：dev_task review 第 3 轮')
    # stopped at once (not the 12 h blocked hold): the open issues go to the user with the result
    assert f.lifecycle_close['closed_by'] == 'auto' and f.lifecycle_close['report'].endswith('outstanding.json')
    stop_tail(f)
    assert result_json(f)['outstanding'].endswith('outstanding.json')


def test_dev_task_without_a_cap_keeps_reviewing(fx, table, notes, repo, tmp_path):  # noqa: F811
    edit_table(table, 'max_rounds: 3, on_cap: stop_and_report}', '}')
    f = as_task(fx, 'dev_task')
    submit(f, {'goal': 'ship feature x', 'deliver': deliver_spec(repo, tmp_path)})
    write_verdict(f, 'FAIL', rnd=5, issues=ISSUES)
    f.tick()
    assert f.task()['status'] == 'running' and not (f.run_dir / 'outstanding.json').exists()


def test_dev_task_pass_at_the_cap_round_is_not_capped(fx, table, notes, repo, tmp_path):  # noqa: F811
    f = as_task(fx, 'dev_task')
    submit(f, {'goal': 'ship feature x', 'deliver': deliver_spec(repo, tmp_path)})
    write_verdict(f, 'PASS', rnd=3)
    f.tick()
    assert f.task()['status'] == 'running' and not (f.run_dir / 'outstanding.json').exists()


def test_every_worker_gets_the_fleet_review_rules_and_the_supervisor_does_not(fx):  # noqa: F811
    rules = (AGENTS / 'reviewer.md').read_text()
    for w in fx.workers:
        cwd = fleet.agent_cwd(fx.cfg, w)
        assert (cwd / 'REVIEWER.md').read_text() == rules
        assert 'read REVIEWER.md in your current directory' in (cwd / 'AGENTS.md').read_text()
    sup = fleet.agent_cwd(fx.cfg, 'supervisor')
    assert not (sup / 'REVIEWER.md').exists() and 'REVIEWER.md' not in (sup / 'AGENTS.md').read_text()


def test_task_supervisor_prompts_carry_the_review_narrowing_rule():
    for name in ('supervisor.md', 'supervisor_v2.md'):
        text = (AGENTS / name).read_text()
        row = next(line for line in text.splitlines() if line.startswith('| Dispatch a review'))
        for must in ('go live quickly', 'from round 2 only run the failing tests', '非阻断观察 ×N，未展开',
                     'at most 3 rounds', 'do not start round 4', 'same as reviewer.md R4',
                     'without a failing test or reproduction command does not count'):
            assert must in row, (name, must)
