"""No review gate anywhere: delivery runs only the tests, nothing appends or waits for a review, and old
specs / tables / rosters that still name the removed review keys load and run as plain tasks.

Private test Redis, fake cmux and fake Pi agents (test_runtime_v2 fixture); a temporary git repository.
"""
import json
import sys
from pathlib import Path

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import fx, receive, settle  # noqa: F401  (fixture)
from test_lifecycle_close import (as_task, complete, deliver_spec, git, notes, repo, result_json,  # noqa: F401
                                  stop_tail, submit, table)
from fleetmux import fleet, fleet_config, lifecycle, lifecycle_deliver, planner, roster
from fleetmux.agent_registry import AgentRegistry

AGENTS = Path(fleet.__file__).resolve().parent / 'agents'
MERGE_READ = ('Merge read: when a feature block is done, the supervisor (Fable) reads the whole diff once before '
              'merging, fixes plain errors and does a light refactor in place (or sends one list back to the author '
              'once). No second read, no PASS/FAIL, no verdict file, nothing in the program waits for it.')


def plain_spec(repo, tmp_path, **extra):
    return dict(deliver_spec(repo, tmp_path), **extra)


def old_spec(repo, tmp_path):
    """A deliver spec written before the review removal."""
    return dict(deliver_spec(repo, tmp_path), review_file='reviewer/verdict.json')


def failing_verdict(ws, name='reviewer/verdict.json', rnd=3):
    path = Path(ws) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'round': rnd, 'verdict': 'FAIL',
                                'issues': [{'id': 'P0-01', 'severity': 'P0', 'title': 'open'}]}))


@pytest.fixture
def old_table(tmp_path, monkeypatch):
    """A mode table written before the review removal: review_step on dev_task, the review_loop mode, the
    review_pass done_when."""
    path = tmp_path / 'old_modes.yaml'
    text = fleet_config.SHIPPED.read_text()
    text = text.replace('      done_when: merged_to_deploy_branch_and_enqueued\n',
                        '      done_when: merged_to_deploy_branch_and_enqueued\n'
                        '      review_step: {must_pass: true, max_rounds: 3, on_cap: stop_and_report}\n', 1)
    text = text.replace('    smoke:\n', '    review_loop:\n      base: task\n      max_rounds: 3\n'
                        '      on_pass: stop_and_close\n      on_cap: stop_and_report\n    smoke:\n', 1)
    assert 'review_step' in text and 'review_loop' in text
    path.write_text(text)
    monkeypatch.setattr(lifecycle, 'DEFAULT_PATH', path)
    return path


# ------------------------------------------------------------------ §2 item 1: the gate runs only test_cmd
def test_deliver_spec_needs_no_review_file():
    spec = lifecycle_deliver.validate_deliver({'repo': '/r', 'feature_branch': 'f', 'deploy_branch': 'main',
                                               'test_cmd': ['true'], 'queue': '/q.jsonl'})
    assert 'review_file' not in spec


def test_gate_reads_no_verdict_and_runs_only_the_tests(tmp_path, repo):
    spec = plain_spec(repo, tmp_path)
    ws = tmp_path / 'ws'
    ws.mkdir()
    rec = lifecycle_deliver.deliver(spec, ws)
    assert rec['ok'], rec.get('problem')
    assert rec['steps'] == ['fast_forward_check', 'tests', 'merged', 'enqueued'] and 'review' not in rec
    assert git(repo, 'rev-parse', 'main') == rec['sha']


def test_gate_still_fails_on_the_tests(tmp_path, repo):
    spec = plain_spec(repo, tmp_path, test_cmd=[sys.executable, '-c', 'raise SystemExit(2)'])
    rec = lifecycle_deliver.deliver(spec, tmp_path)
    assert not rec['ok'] and 'tests failed (exit 2)' in rec['problem']


def test_old_spec_with_review_file_and_a_fail_verdict_still_delivers(tmp_path, repo):
    """§2 item 6: an old deliver spec keeps its review_file; it is read and ignored, never looked up."""
    spec = old_spec(repo, tmp_path)
    ws = tmp_path / 'ws'
    failing_verdict(ws)
    assert lifecycle_deliver.validate_deliver(spec)['review_file'] == spec['review_file']
    rec = lifecycle_deliver.deliver(spec, ws)
    assert rec['ok'], rec.get('problem')
    assert 'review' not in rec and 'review' not in rec['steps']


def test_dev_task_with_an_old_review_file_delivers_and_closes(fx, table, notes, repo, tmp_path):
    f = as_task(fx, 'dev_task')
    complete(f, {'goal': 'ship', 'deliver': old_spec(repo, tmp_path)})  # no verdict anywhere
    f.tick()
    assert f.task()['status'] == 'completed' and f.lifecycle_close['closed_by'] == 'auto'
    stop_tail(f)
    out = result_json(f)
    assert out['delivery']['ok'] and out['delivery']['steps'] == ['fast_forward_check', 'tests', 'merged', 'enqueued']


def test_old_review_step_in_the_table_never_caps_or_blocks(fx, old_table, notes, repo, tmp_path):
    """§2 item 1: no "review rounds used up" block, even with an old table and a round-3 FAIL verdict on disk."""
    assert lifecycle.get_mode('dev_task')['name'] == 'dev_task'
    f = as_task(fx, 'dev_task')
    submit(f, {'goal': 'ship', 'deliver': old_spec(repo, tmp_path)})
    failing_verdict(f.workspace())
    f.tick()
    assert f.task()['status'] == 'running' and not (f.run_dir / 'outstanding.json').exists()


def test_shipped_table_has_no_review_step_and_no_review_loop():
    text = fleet_config.SHIPPED.read_text()
    assert 'review_step' not in text and 'review_pass' not in text and 'review_loop' not in text
    assert 'review_loop' not in lifecycle.MODE_NAMES


# ------------------------------------------------------------------ §2 item 6: old review_loop tasks
def test_old_table_with_a_review_loop_block_loads(old_table):
    table = lifecycle.load_modes()
    assert 'review_loop' not in table['modes']
    assert lifecycle.get_mode('dev_task')['base'] == 'task'


def test_review_loop_resolves_as_a_plain_task_mode(table):
    policy = lifecycle.get_mode('review_loop')
    assert policy['base'] == 'task' and policy['name'] == 'review_loop'
    assert not {'max_rounds', 'on_cap', 'review_step'} & set(policy)


def test_new_start_does_not_offer_review_loop(table):
    with pytest.raises(ValueError, match='unknown --lifecycle'):
        lifecycle.resolve_start('review_loop')


def test_old_review_loop_task_starts_and_closes_as_a_plain_task(fx, table, notes):
    f = as_task(fx, 'review_loop')
    failing_verdict(f.workspace())  # an old FAIL verdict is never read
    complete(f, {'goal': 'fix the findings', 'review_file': 'reviewer/verdict.json'})
    f.tick()
    assert f.task()['status'] == 'completed' and f.lifecycle_close['closed_by'] == 'auto'


def test_old_review_loop_task_on_an_old_table_starts(fx, old_table, notes):
    f = as_task(fx, 'review_loop')
    task = submit(f, {'goal': 'fix the findings', 'review_file': 'reviewer/verdict.json'})
    assert task['status'] == 'running'


def test_old_job_spec_keys_are_read_and_ignored(fx):
    out = fx.job_spawn_fields({'lifecycle': 'review_loop', 'review_file': 'reviewer/verdict.json'})
    assert 'review_file' not in out


def test_old_dispatch_review_flag_is_accepted_and_ignored(fx, table):
    f = as_task(fx, 'smoke')
    submit(f, {'goal': 'look', 'artifacts': []})
    f.pump(); env = receive(f, 'supervisor')
    settle(f, 'supervisor', env, {'status': 'RUNNING', 'completion': 50, 'reason': 'next', 'dispatch': [
        {'to': 'worker-2', 'instruction': 'Read the feature branch and report what you find', 'review': True}]})
    assert 'review_slot' not in f.task()
    assert not (fleet.agent_cwd(f.cfg, 'worker-2') / 'REVIEWER.md').exists()


def test_old_roster_with_a_reviewer_slot_loads(tmp_path):
    path = tmp_path / 'roster.yaml'
    path.write_text('workers:\n  - {name: dev-1}\n  - {name: review-1, role: ../reviewer.md}\n'
                    '  - {name: reviewer-1, profile: claude-reviewer}\n')
    workers, roles, profiles = roster.load_entries(str(path))
    assert workers == ['dev-1', 'review-1', 'reviewer-1'] and 'review-1' not in roles
    assert AgentRegistry().get('claude-reviewer').role_file == 'worker.md'


# ------------------------------------------------------------------ §2 item 2: no auto-appended review
def test_code_change_plan_has_no_review_subtask():
    plan = planner.build_dispatch_plan('Implement the retry flag in the queue module', AgentRegistry())
    assert [s.id for s in plan.subtasks] == ['implementation']
    assert all(s.depends_on == () for s in plan.subtasks)


def test_an_explicit_review_request_is_still_a_review_job():
    plan = planner.build_dispatch_plan('Review the queue module for races', AgentRegistry())
    assert [s.id for s in plan.subtasks] == ['review']


# ------------------------------------------------------------------ §2 item 3: no review text for workers
def test_workers_get_no_review_instructions(fx):
    for w in fx.workers:
        cwd = fleet.agent_cwd(fx.cfg, w)
        assert not (cwd / 'REVIEWER.md').exists()
        assert 'REVIEWER.md' not in (cwd / 'AGENTS.md').read_text()
    assert not (AGENTS / 'reviewer.md').exists()
    assert not hasattr(fleet, 'REVIEW_POINTER') and not hasattr(fleet, 'VERDICT_SECTION')


# ------------------------------------------------------------------ §2 item 7: merge read in the supervisor prompts
def test_supervisor_prompts_carry_the_merge_read_line_and_no_review_dispatch():
    for name in ('supervisor.md', 'supervisor_v2.md'):
        text = (AGENTS / name).read_text()
        assert MERGE_READ in text, name
        assert 'Dispatch a review' not in text and '"review": true' not in text, name
