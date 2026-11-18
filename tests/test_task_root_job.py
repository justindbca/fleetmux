"""Jobs on a task root (docs/design.md §19 "Task root", §58): while a root task fleet runs its own task, `fleetmux
task` is a job, run by the program in its own task-mode sub-fleet as on a service root. The root's own COMPLETED is
held (no strike) while a job is open, and the task completes when the last job ends. The supervisor cannot route a
job. Only private test Redis, fake cmux, fake CLIs (fixtures of test_subfleet / test_service_split).
"""
import json

import pytest

from test_escalate import ok_check  # noqa: F401  (fixture)
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_service_escalate import escalations, result  # noqa: F401
from test_service_job import GOAL, job_rec, jobs, submit
from test_service_split import finish_child, queued_to
from test_subfleet import given, hook, no_launch, start_tree, step, tree  # noqa: F401  (tree: fixture)
from fleetmux import fleet, remote, runtime_v2, subfleet

ART = [{'path': 'report.md', 'contains': ['Conclusion']}]


@pytest.fixture
def task_tree(tmp_path, monkeypatch):
    """A root task fleet (v2, two Claude workers) whose own task runs; its supervisor holds the task prompt."""
    monkeypatch.setattr(subfleet, 'default_launch', no_launch)
    monkeypatch.setattr(fleet.Fleet, 'preflight_profile', lambda self, launch, cli_change_ok=False: None)
    yield from start_tree(tmp_path, monkeypatch, max_attempts=2)


def say(t, body):
    """The root supervisor answers its current delivery with ``body`` (a task decision)."""
    f = t.f
    step(t)
    out = hook(t, f, 'supervisor', json.dumps(body), prompt=given(t, f, 'supervisor')[-1])
    step(t)
    return out


def done(**extra):
    return dict({'status': 'COMPLETED', 'completion': 100, 'dispatch': [], 'reason': 'own report verified',
                 'artifacts': ART}, **extra)


def start_job(t, spec=None, cid='cmd-job'):
    f = t.f
    answer = submit(f, spec, t=t, cid=cid)
    assert answer['status'] == 'accepted' and answer['job_id'].startswith('job-'), answer
    jid = answer['job_id']
    for _ in range(2):
        f.tick()
        step(t)
    key, rec = job_rec(f, jid)
    assert rec and rec['status'] == 'delegated', escalations(f)
    return jid, key


def own_report(f):
    (f.workspace() / 'report.md').write_text('Conclusion: the root task did its own part')


# ------------------------------------------------------------------ entry
def test_task_root_takes_a_task_as_a_job_while_its_task_runs(task_tree, monkeypatch, capsys):
    t, f = task_tree, task_tree.f
    own = f.task()['id']
    jid, key = start_job(t)
    assert f.task()['id'] == own and f.task()['status'] == 'running'  # the root's own task is untouched
    cfg = fleet.load(f.run_dir / 'sub' / key / 'config.json')
    assert cfg['mode'] == 'task' and cfg['task_id'] == key and cfg['adopt']['slot'] == 'worker-1'
    assert jobs(f)[jid]['state'] == 'running' and f.agent('worker-1')['state'] == 'delegated'
    assert remote.result(f.r, jid)[0] == remote.PENDING
    fleet.status(f.cfg)
    assert [r['id'] for r in json.loads(capsys.readouterr().out)['jobs']] == [jid]  # join.job_view reads these
    notes = queued_to(f, 'supervisor', '[job ' + jid + '] accepted')
    assert len(notes) == 1 and 'Continue the current task' in notes[0]['body']['text']
    finish_child(t, key, 'completed', monkeypatch)
    got = result(f, jid)
    assert got['status'] == 'ok' and got['result']['subfleet'] == key and jid not in jobs(f)
    assert f.agent('worker-1')['state'] == 'idle' and f.task()['status'] == 'running'


def test_a_task_root_with_no_task_running_takes_the_first_task_as_its_own(tree):
    t, f = tree, tree.f
    first = runtime_v2.wait_reply(f.v2.bus, 'new-task', timeout=2)  # the fixture's own first `task`
    assert first['status'] == 'accepted' and first['task_id'].startswith('task-') and 'job_id' not in first
    own_report(f)
    assert say(t, done())['status'] == 'committed' and f.task()['status'] == 'completed'  # no job: ends as before
    runtime_v2.post_event(f.v2.bus, {'kind': 'task', 'request_id': 'cmd-job', 'spec': {'goal': GOAL}, 'job': True},
                          'cmd-job')
    step(t)
    refused = runtime_v2.wait_reply(f.v2.bus, 'cmd-job', timeout=2)  # a join stub's task is never the root's own
    assert refused['status'] == 'rejected' and 'runs no task of its own' in refused['reason'] and not jobs(f)
    again = submit(f, {'goal': 'Write a one page summary of the notes'}, t=t, cid='cmd-own')
    assert again['status'] == 'accepted' and again['task_id'].startswith('task-') and 'job_id' not in again
    assert f.task()['status'] == 'running' and f.task()['id'] == again['task_id'] and not jobs(f)


# ------------------------------------------------------------------ closing
def test_root_completed_while_a_job_runs_is_held_then_closes_after_it(task_tree, monkeypatch):
    t, f = task_tree, task_tree.f
    own_report(f)
    jid, key = start_job(t)
    assert say(t, done())['status'] == 'committed'
    task = f.task()
    assert task['status'] == 'running' and task['bounces'] == '0' and task['bad_replies'] == '0'
    assert task['jobs_hold'] == 'own report verified'
    assert len(queued_to(f, 'supervisor', 'COMPLETED recorded')) == 1
    assert say(t, done(reason='still done'))['status'] == 'committed'  # again: no strike, no second notice
    assert f.task()['bounces'] == '0' and len(queued_to(f, 'supervisor', 'COMPLETED recorded')) == 1
    assert f.task()['status'] == 'running' and subfleet.record(f.run_dir / 'sub' / key)['state'] == 'running'
    finish_child(t, key, 'completed', monkeypatch)
    assert result(f, jid)['status'] == 'ok' and remote.result(f.r, jid)[0] == remote.FOUND  # `fleetmux result`
    task = f.task()
    assert task['status'] == 'completed' and task['reason'] == 'still done'


def test_the_hold_waits_for_own_work_still_out_when_the_last_job_ends(task_tree, monkeypatch):
    t, f = task_tree, task_tree.f
    own_report(f)
    jid, key = start_job(t)
    say(t, done())
    f.setf('worker-2', state='busy')  # own work out again (e.g. an approved split of its own)
    finish_child(t, key, 'completed', monkeypatch)
    assert jid not in jobs(f) and f.task()['status'] == 'running'
    f.setf('worker-2', state='idle')
    f.tick()
    assert f.task()['status'] == 'completed'


def test_a_queued_job_holds_the_root_completed_too(task_tree):
    t, f = task_tree, task_tree.f
    own_report(f)
    for w in f.workers:
        f.setf(w, state='busy')  # no idle worker: the job stays queued
    jid = submit(f, t=t)['job_id']
    f.tick()
    assert jobs(f)[jid]['state'] == 'queued'
    for w in f.workers:
        f.setf(w, state='idle')
    assert say(t, done())['status'] == 'committed'
    assert f.task()['status'] == 'running' and f.task()['jobs_hold']


def test_new_own_work_after_the_hold_lapses_it(task_tree):
    t, f = task_tree, task_tree.f
    own_report(f)
    start_job(t)
    say(t, done())
    assert f.task()['jobs_hold']
    say(t, {'status': 'RUNNING', 'completion': 80, 'reason': 'one more check',
            'dispatch': [{'to': 'worker-2', 'instruction': 'Check report.md once more.'}]})
    assert f.task()['jobs_hold'] == '' and f.task()['bad_replies'] == '0'


def test_root_task_end_fails_its_queued_job(task_tree):
    t, f = task_tree, task_tree.f
    for w in f.workers:
        f.setf(w, state='busy')
    jid = submit(f, t=t)['job_id']
    f.tick()
    f.v2.finish('cancelled', 'stopped by operator')
    f.tick()
    got = result(f, jid)
    assert got['status'] == 'failed' and 'before the job started' in got['error'] and jid not in jobs(f)


def test_the_root_task_ending_stops_and_fails_a_running_job(task_tree):
    t, f = task_tree, task_tree.f
    jid, key = start_job(t)
    assert subfleet.stop_children(f, wait=0)
    got = result(f, jid)
    assert got['status'] == 'failed' and key in got['error'] and jid not in jobs(f)


# ------------------------------------------------------------------ guard
@pytest.mark.parametrize('body, words', [
    ({'status': 'RUNNING', 'completion': 50, 'reason': 'r',
      'dispatch': [{'to': 'worker-1', 'instruction': 'Check report.md and write check.md with findings.'}]}, 'supervises the sub-fleet of job'),
    ({'status': 'RUNNING', 'completion': 50, 'reason': 'r',
      'dispatch': [{'to': 'worker-2', 'instruction': 'Check report.md and write check.md with findings.', 'id': 'KEY'}]}, 'is the sub-fleet of job'),
    (dict(done(), withdraw=['worker-1']), 'withdraw worker-1'),
])
def test_root_supervisor_cannot_route_a_job(task_tree, body, words):
    t, f = task_tree, task_tree.f
    own_report(f)
    jid, key = start_job(t)
    body = json.loads(json.dumps(body).replace('KEY', key))
    say(t, body)
    assert f.task()['bad_replies'] == '1' and f.task()['status'] == 'running'
    refusal = queued_to(f, 'supervisor', 'not a valid decision JSON')
    assert refusal and words in refusal[-1]['body']['text'] and jid in refusal[-1]['body']['text']
    assert jobs(f)[jid]['state'] == 'running'


# ------------------------------------------------------------------ the CLI flag a join stub sends
def test_task_cli_job_flag_puts_job_on_the_event(task_tree, monkeypatch, capsys):
    import io
    import sys
    sent = []
    monkeypatch.setattr(fleet, 'request', lambda cfg, event, wait=30: sent.append(event) or {'status': 'accepted'})
    for argv in (['task', '--config', str(task_tree.f.path), '--job', '-'], ['task', '--config', str(task_tree.f.path), '-']):
        monkeypatch.setattr(sys, 'stdin', io.StringIO(json.dumps({'goal': GOAL})))
        fleet.main(argv)
    assert sent[0] == {'kind': 'task', 'spec': {'goal': GOAL}, 'job': True}
    assert sent[1] == {'kind': 'task', 'spec': {'goal': GOAL}}


# ------------------------------------------------------------------ a lifecycle root closes after its last job
@pytest.fixture
def program_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(subfleet, 'default_launch', no_launch)
    monkeypatch.setattr(fleet.Fleet, 'preflight_profile', lambda self, launch, cli_change_ok=False: None)
    yield from start_tree(tmp_path, monkeypatch, max_attempts=2, lifecycle='program')


def test_a_lifecycle_root_closes_through_its_lifecycle_after_the_last_job(program_tree, monkeypatch):
    t, f = program_tree, program_tree.f
    own_report(f)
    jid, key = start_job(t)
    say(t, done())
    f.tick()
    assert f.task()['status'] == 'running' and not f.lifecycle_close  # held: the run stays up for the job
    finish_child(t, key, 'completed', monkeypatch)
    f.tick()
    assert f.task()['status'] == 'completed' and f.lifecycle_close and f.lifecycle_close['closed_by'] == 'auto'
