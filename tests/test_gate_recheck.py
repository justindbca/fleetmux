"""`fleetmux task --recheck <id>`: run only the dev_task delivery gate again, on the files as they are now.

Blocked by the gate -> files fixed -> recheck passes: the run's own task completes (the next tick closes it like any
completed dev_task); a job's sub-fleet result turns completed and the job's result is published again as ok. No work
is dispatched. A running task or job, and a task blocked by anything but a failed gate, are refused.
Private test Redis, fake cmux and fake agents; a temporary git repository.
"""
import json
import sys
from pathlib import Path

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import fx, receive, settle  # noqa: F401  (fixture)
from test_lifecycle_close import (as_task, deliver_spec, git, make_reviewer, notes, repo, result_json,  # noqa: F401
                                  submit, table)
from test_service_escalate import audits, result  # noqa: F401
from test_service_split import split_tree  # noqa: F401  (fixture)
from test_service_job import capture_child_specs, start_job, submit as submit_job
from test_subfleet import adopt_by_hook, step, stop_child_like_daemon
from fleetmux import fleet, lifecycle_close, lifecycle_recheck, runtime_v2, subfleet

REVIEW = 'review/verdict.json'


def write(path, verdict='PASS'):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'round': 1, 'verdict': verdict, 'issues': []}))


def recheck(f, ident, cid='recheck-1'):
    """The CLI half (compute from files) then the daemon half (the event on the fleet's bus)."""
    ev = lifecycle_recheck.compute(f.cfg, ident, f.r)
    runtime_v2.post_event(f.v2.bus, dict(ev, request_id=cid), cid)
    assert f.v2.step(10)
    return runtime_v2.wait_reply(f.v2.bus, cid, timeout=2)


def dispatched(f):
    return [x for x in audits(f, 'queued') if x.get('slot') != 'supervisor']


# ------------------------------------------------------------------ the run's own task
def blocked_own_task(fx, repo, tmp_path):
    f = as_task(fx, 'dev_task')
    make_reviewer(f, 'worker-2')
    spec = dict(deliver_spec(repo, tmp_path), review_file=REVIEW)
    write(f.workspace() / 'worker-1' / 'notes.md')  # the COMPLETED artifact
    submit(f, {'goal': 'ship', 'deliver': spec, 'artifacts': [{'path': 'worker-1/notes.md'}]})
    f.pump(); env = receive(f, 'supervisor')
    settle(f, 'supervisor', env, {'status': 'COMPLETED', 'completion': 100, 'dispatch': [], 'reason': 'done'})
    f.tick()
    assert f.task()['status'] == 'blocked' and 'no review was dispatched and no verdict was found' in f.task()['reason']
    assert result_json(f)['status'] == 'blocked'
    return f


def test_own_task_blocked_then_fixed_then_recheck_completes_and_closes(fx, table, notes, repo, tmp_path):
    f = blocked_own_task(fx, repo, tmp_path)
    before = len(dispatched(f))
    write(f.workspace() / 'worker-2' / REVIEW)  # the reviewer wrote its verdict after all
    answer = recheck(f, f.task()['id'])
    assert answer['status'] == 'passed', answer
    assert f.task()['status'] == 'completed' and f.task()['reason'] == lifecycle_recheck.RECHECKED
    assert git(repo, 'rev-parse', 'main') == git(repo, 'rev-parse', 'feat/x')
    assert len(Path(tmp_path / 'queue' / 'deploy-queue.jsonl').read_text().splitlines()) == 1
    f.tick()  # closed like any completed dev_task; result.json says completed
    assert f.lifecycle_close['closed_by'] == 'auto'
    assert result_json(f)['status'] == 'completed'
    assert len(dispatched(f)) == before  # nothing was dispatched


def test_own_task_recheck_still_failing_stays_blocked_with_the_new_reason(fx, table, notes, repo, tmp_path):
    f = blocked_own_task(fx, repo, tmp_path)
    write(f.workspace() / 'worker-2' / REVIEW, 'FAIL')
    answer = recheck(f, f.task()['id'])
    assert answer['status'] == 'blocked' and 'review verdict is FAIL' in answer['problem']
    assert f.task()['status'] == 'blocked' and 'review verdict is FAIL' in f.task()['reason']
    f.tick()
    assert result_json(f)['status'] == 'blocked' and not f.lifecycle_close


def test_recheck_of_a_running_task_is_refused(fx, table, repo, tmp_path):
    f = as_task(fx, 'dev_task')
    submit(f, {'goal': 'ship', 'deliver': dict(deliver_spec(repo, tmp_path), review_file=REVIEW)})
    with pytest.raises(ValueError, match='is running: only a task the delivery gate blocked can be rechecked'):
        lifecycle_recheck.compute(f.cfg, f.task()['id'], f.r)
    cid = 'forged-1'  # the daemon checks again: a recomputed record for a running task is refused there too
    runtime_v2.post_event(f.v2.bus, {'kind': 'recheck', 'id': f.task()['id'], 'delivery': {'ok': True},
                                     'request_id': cid}, cid)
    assert f.v2.step(10)
    answer = runtime_v2.wait_reply(f.v2.bus, cid, timeout=2)
    assert answer['status'] == 'rejected' and 'is running' in answer['reason']
    assert f.task()['status'] == 'running'


def test_recheck_of_a_task_blocked_by_something_else_is_refused(fx, table, repo, tmp_path):
    f = as_task(fx, 'dev_task')
    submit(f, {'goal': 'ship', 'deliver': dict(deliver_spec(repo, tmp_path), review_file=REVIEW)})
    lifecycle_close.set_task_status(f, 'blocked', 'supervisor: needs a decision')
    with pytest.raises(ValueError, match='not by a failed delivery gate'):
        lifecycle_recheck.compute(f.cfg, f.task()['id'], f.r)


# ------------------------------------------------------------------ a job of a service fleet
def roster(tmp_path):
    path = tmp_path / 'team' / 'roster.yaml'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('workers:\n  - {name: coder-1}\n  - {name: reviewer-1}\n')
    return path


def blocked_job(t, tmp_path, monkeypatch, repo):
    f = t.f
    capture_child_specs(monkeypatch)
    spec = {'goal': 'ship feature x', 'name': 'feature-x', 'roster': str(roster(tmp_path)), 'lifecycle': 'dev_task',
            'deliver': dict(deliver_spec(repo, tmp_path), review_file=REVIEW)}
    jid, key = start_job(t, spec)
    child_run = f.run_dir / 'sub' / key
    cf, _ = adopt_by_hook(t, monkeypatch, subfleet.record(child_run))
    step(t)
    make_reviewer(cf, 'reviewer-1')  # it runs the claude-reviewer card (opus)
    cf.mark_review('reviewer-1')  # what the child supervisor's "review": true dispatch records
    doc = (fleet.agent_cwd(cf.cfg, 'reviewer-1') / 'REVIEWER.md').read_text()  # told where to write, by the program
    assert '`' + str(child_run / 'workspace' / 'reviewer-1' / REVIEW) + '`' in doc
    return jid, key, child_run, cf


def end_child(t, cf):
    f = t.f
    lifecycle_close.tick(cf)
    f.tick()
    stop_child_like_daemon(cf)
    f.tick()
    step(t)


@pytest.mark.usefixtures('job_chains_as_before')  # §58 model chains: the slot keeps its model
def test_job_blocked_then_fixed_then_recheck_publishes_ok(split_tree, tmp_path, monkeypatch, repo):
    t, f = split_tree, split_tree.f
    jid, key, child_run, cf = blocked_job(t, tmp_path, monkeypatch, repo)
    with pytest.raises(ValueError, match='is running'):  # the sub-fleet still runs: refused
        lifecycle_recheck.compute(f.cfg, jid, f.r)
    cf.v2.finish('completed', 'work done')
    end_child(t, cf)
    got = result(f, jid)
    assert got['status'] == 'blocked' and 'review verdict not written' in got['error'] and '(the review was dispatched to reviewer-1)' in got['error']
    assert got['result']['delivery']['spec']['review_file'] == REVIEW  # the gate record keeps what a recheck needs
    assert got['result']['delivery']['slots']['models']['reviewer-1'] == 'opus'
    before = len(dispatched(f))
    write(child_run / 'workspace' / 'reviewer-1' / REVIEW)  # fixed in the kept workspace
    answer = recheck(f, jid)
    assert answer['status'] == 'passed' and answer['published'] == 'ok', answer
    got = result(f, jid)
    assert got['status'] == 'ok' and got['result']['delivery']['ok']
    assert got['result']['delivery']['review']['slot'] == 'reviewer-1'
    assert git(repo, 'rev-parse', 'main') == git(repo, 'rev-parse', 'feat/x')
    child = fleet.load(child_run / 'result.json')
    assert child['status'] == 'completed' and child['reason'] == lifecycle_recheck.RECHECKED
    assert subfleet.record(child_run)['state'] == 'returned'
    assert len(dispatched(f)) == before  # nothing was dispatched
    with pytest.raises(ValueError, match='not blocked by a failed delivery gate'):  # done: a second recheck is refused
        lifecycle_recheck.compute(f.cfg, jid, f.r)


@pytest.mark.usefixtures('job_chains_as_before')  # §58 model chains: the slot keeps its model
def test_job_recheck_refused_while_the_job_is_open(split_tree, tmp_path, monkeypatch, repo):
    t, f = split_tree, split_tree.f
    jid, key, child_run, cf = blocked_job(t, tmp_path, monkeypatch, repo)
    cid = 'forged-job'
    runtime_v2.post_event(f.v2.bus, {'kind': 'recheck', 'id': jid, 'delivery': {'ok': True}, 'request_id': cid}, cid)
    step(t)
    answer = runtime_v2.wait_reply(f.v2.bus, cid, timeout=2)
    assert answer['status'] == 'rejected' and 'still open' in answer['reason']


def test_daemon_refuses_a_passing_record_whose_sha_is_not_the_feature_tip(fx, table, notes, repo, tmp_path):
    f = blocked_own_task(fx, repo, tmp_path)
    write(f.workspace() / 'worker-2' / REVIEW)
    ev = lifecycle_recheck.compute(f.cfg, f.task()['id'], f.r)
    (repo / 'a.txt').write_text('later\n')
    git(repo, 'commit', '-qam', 'later')  # the feature branch moved after the CLI ran the gate
    cid = 'stale-sha'
    runtime_v2.post_event(f.v2.bus, dict(ev, request_id=cid), cid)
    assert f.v2.step(10)
    answer = runtime_v2.wait_reply(f.v2.bus, cid, timeout=2)
    assert answer['status'] == 'blocked' and 'is not the tip of feat/x' in answer['problem']
    assert f.task()['status'] == 'blocked'


def test_cli_takes_recheck_without_a_spec():
    args = fleet.cli().parse_args(['task', '--recheck', 'job-abc', '--config', '/x/config.json'])
    assert args.recheck == 'job-abc' and args.spec is None
