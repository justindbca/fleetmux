"""A development task or job brings its tree (docs/design.md §62): with the decompose switch on, a task of a root
dev_task fleet and a job with ``lifecycle: dev_task`` are refused at submit when the spec has no ``tree``; the
message names the three steps (write the tree file, run ``fleetmux tree check``, give ``tree`` in the spec). Other
lifecycles are not held to it; with the switch off nothing changes. Only new submissions are checked: a job accepted
before the switch went on, a restarted run and a sub-fleet the program starts (a job's or a split's, which always
gets a seed) are never refused."""
import json

import pytest

from fleetmux import acp_server, decompose, runtime_v2, subfleet
from test_decompose_keep import crash_and_resume, task_run  # noqa: F401
from test_decompose_tree import sample
from test_escalate import ok_check  # noqa: F401
from test_message_bus_v2 import private_redis_v2  # noqa: F401
from test_resume import managed  # noqa: F401
from test_service_escalate import svc  # noqa: F401
from test_service_job import DELIVER, GOAL, job_rec, jobs, submit as job_submit, team
from test_service_split import split_tree  # noqa: F401
from test_subfleet import adopt_by_hook, step
from test_v2_codex_claude import mixed  # noqa: F401

pytestmark = pytest.mark.usefixtures('job_chains_as_before')


def tree_file(tmp_path):
    path = tmp_path / 'seed-tree.json'
    path.write_text(json.dumps({'nodes': [x for x in sample() if 'id' in x],
                                'decisions': [x['decision'] for x in sample() if 'decision' in x]}))
    return str(path)


def task_answer(f, spec, cid='t1'):
    runtime_v2.post_event(f.v2.bus, {'kind': 'task', 'request_id': cid, 'spec': spec}, cid)
    assert f.v2.step(10)
    f.pump()
    return runtime_v2.wait_reply(f.v2.bus, cid, timeout=2)


def dev_root(f):
    f.cfg['lifecycle'] = 'dev_task'
    return {'goal': 'implement the export command', 'deliver': DELIVER}


def refused_for_the_tree(answer):
    reason = answer.get('reason') or ''
    return (answer['status'] == 'rejected' and 'tree file' in reason and 'fleetmux tree check' in reason
            and '"tree"' in reason)


def dev_job(tmp_path, **extra):
    path, roles = team(tmp_path)
    return dict({'goal': GOAL, 'name': 'feature-x', 'roster': str(path), 'roles': str(roles),
                 'lifecycle': 'dev_task', 'deliver': DELIVER}, **extra)


# ------------------------------------------------------------------ tasks
def test_a_dev_task_task_without_a_tree_is_refused_with_the_three_steps(mixed, decompose_on):
    f = mixed
    answer = task_answer(f, dev_root(f))
    assert refused_for_the_tree(answer), answer
    assert not f.running()


def test_a_dev_task_task_with_a_tree_is_taken(mixed, decompose_on, tmp_path):
    f = mixed
    answer = task_answer(f, dict(dev_root(f), tree=tree_file(tmp_path)))
    assert answer['status'] == 'accepted', answer
    assert f.tree_state()['seed']['blocks']


def test_another_lifecycle_needs_no_tree(mixed, decompose_on):
    f = mixed
    assert f.cfg.get('lifecycle') != 'dev_task'
    assert task_answer(f, {'goal': 'add one pre-send check'})['status'] == 'accepted'


def test_switch_off_a_dev_task_task_without_a_tree_is_taken_as_before(mixed):
    f = mixed
    assert not decompose.enabled()
    answer = task_answer(f, dev_root(f))
    assert answer['status'] == 'accepted', answer
    assert f.tree_state() is None


def test_a_restarted_dev_task_run_is_not_refused(task_run, monkeypatch):  # noqa: F811
    state = task_run
    f = state['f']
    answer = task_answer(f, dev_root(f))  # taken while the switch is off
    assert answer['status'] == 'accepted', answer
    monkeypatch.setattr(decompose, 'enabled', lambda: True)
    f = crash_and_resume(state, lifecycle='dev_task')  # daemon and Redis killed, start --resume
    assert f.running() and f.task()['id'] == answer['task_id']


# ------------------------------------------------------------------ jobs
def test_a_dev_task_job_without_a_tree_is_refused(svc, ok_check, decompose_on, tmp_path):
    f = svc
    answer = job_submit(f, dev_job(tmp_path), cid='cmd-dev')
    assert refused_for_the_tree(answer), answer
    assert not jobs(f)


def test_a_dev_task_job_with_a_tree_and_other_jobs_are_taken(svc, ok_check, decompose_on, tmp_path):
    f = svc
    answer = job_submit(f, dev_job(tmp_path, tree=tree_file(tmp_path), by='orchestrator'), cid='cmd-tree')
    assert answer['status'] == 'accepted', answer
    assert jobs(f)[answer['job_id']]['by'] == 'orchestrator'
    assert job_submit(f, {'goal': GOAL}, cid='cmd-plain')['status'] == 'accepted'


def test_switch_off_a_dev_task_job_without_a_tree_is_taken_as_before(svc, ok_check, tmp_path):
    f = svc
    assert job_submit(f, dev_job(tmp_path), cid='cmd-off')['status'] == 'accepted'


def test_a_job_taken_before_the_switch_went_on_still_starts_its_sub_fleet(split_tree, tmp_path, monkeypatch):
    t, f = split_tree, split_tree.f
    answer = job_submit(f, dev_job(tmp_path), t=t, cid='cmd-queued')  # queued while off
    assert answer['status'] == 'accepted', answer
    jid = answer['job_id']
    monkeypatch.setattr(decompose, 'enabled', lambda: True)
    f.tick()
    step(t)
    f.tick()
    key, _ = job_rec(f, jid)
    assert key, jobs(f)[jid]
    rec = subfleet.record(f.run_dir / 'sub' / key)
    cf, _ = adopt_by_hook(t, monkeypatch, rec)  # the child takes its task: a program-started dev_task sub-fleet
    assert cf.cfg['lifecycle'] == 'dev_task' and cf.cfg.get('parent_config')
    assert cf.running() and cf.tree_state() is not None  # the program gave it a seed
    assert jobs(f)[jid]['state'] not in ('failed', 'done')


# ------------------------------------------------------------------ the ACP entry
def acp_text(**keys):
    return GOAL + '\n\n```json\n' + json.dumps(keys) + '\n```'


def test_acp_takes_tree_and_by_and_a_dev_task_job_without_a_tree_is_refused(svc, ok_check, decompose_on, tmp_path):
    f = svc
    assert {'tree', 'by'} <= set(acp_server.JOB_KEYS) and 'tree' in acp_server.allowed_keys()
    spec = acp_server.job_spec(acp_text(lifecycle='dev_task', deliver=DELIVER, tree=tree_file(tmp_path), by='acp'))
    assert spec['tree'] == tree_file(tmp_path) and spec['by'] == 'acp'
    assert job_submit(f, spec, cid='cmd-acp-tree')['status'] == 'accepted'
    answer = job_submit(f, acp_server.job_spec(acp_text(lifecycle='dev_task', deliver=DELIVER)), cid='cmd-acp-none')
    assert refused_for_the_tree(answer), answer
