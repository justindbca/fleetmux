"""Scenario two (docs/design.md sections 58 and 60): queued and running jobs survive a root restart (the section 58
tests are tests/test_job_queue_restart*.py) and every job the restart touched is in the reconcile ledger; a job that
could not be carried is ruled by the supervisor, and its redo submits it again exactly once. Also: a root restart
while the job's slot rotates, and the model chain of a job handed on by resume_run. Private test Redis, fake cmux."""
import json

import pytest

from restart_kit import items, kill_extra, ledger, new_run, rdir, tick
from test_escalate import ok_check  # noqa: F401  (fixture)
from test_job_queue_restart import all_ready, continue_root, explicit_stop, jobs, queue_jobs
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_service_escalate import escalations, result, svc  # noqa: F401
from test_service_job import GOAL, job_rec, start_job, submit
from test_service_split import split_tree, sub_runs  # noqa: F401
from test_subfleet import step
from fleetmux import fleet, lifecycle_close, restart_state, subfleet

pytestmark = pytest.mark.usefixtures('job_chains_as_before')


@pytest.fixture
def jt(split_tree, monkeypatch):
    sent = []
    monkeypatch.setattr(lifecycle_close, 'NOTIFIER', lambda rec: sent.append(rec) or 'sent')
    split_tree.sent = sent
    yield split_tree
    kill_extra(split_tree)


def stop(t):
    """The explicit-stop tail: the snapshot first, then the section 58 stop (tests/test_job_queue_restart.py)."""
    restart_state.on_stop(t.f)
    explicit_stop(t)


def run_ticks(t, n=4):
    for _ in range(n):
        t.f.tick()
        step(t)


def test_continued_run_lists_the_resumed_job_and_keeps_the_queued_one(jt):
    t = jt
    jid, key = start_job(t)
    (queued,) = queue_jobs(t, 1, start=7)
    t.f.tick()
    stop(t)
    f = continue_root(t)
    all_ready(t)
    run_ticks(t)
    assert queued in jobs(f) and result(f, queued) is None
    it, = [i for i in items(f, 'job') if i['key'] == jid]
    assert it['class'] == 'stopped' and it['auto']['action'] == 'job resume'
    assert not [i for i in items(f, 'job') if i['key'] == queued]  # still open: nothing to rule
    assert [d.name for d in subfleet.job_children(f.run_dir, jid)] == [key]  # the same sub-fleet, never a second


def test_job_not_carried_to_a_new_run_is_errored_and_its_redo_submits_it_once(jt):
    t = jt
    jid, key = start_job(t)
    stop(t)
    old = t.f
    f = new_run(t, carry_from=[{'run': str(old.run_dir), 'reason': 'cannot resume: test'}])
    f.job_carry_in()  # the new run's first boot (RuntimeV2.run) before its first tick
    assert result(f, jid)['status'] == 'failed'
    tick(t)
    it, = [i for i in items(f, 'job') if i['key'] == jid]
    assert it['class'] == 'errored' and 'redo' in it['evidence']
    first = restart_state.decide(f, [{'item': it['id'], 'verdict': 'redo', 'reason': 'run it again'}], by='operator')
    assert first['applied'] == [it['id']]
    done, = [i for i in items(f, 'job') if i['key'] == jid]
    assert done['state'] == 'done' and done['replayed'] in jobs(f)
    again = restart_state.decide(f, [{'item': it['id'], 'verdict': 'redo', 'reason': 'again'}], by='operator')
    assert again['refused'] and len([j for j in jobs(f) if j != jid]) == 1  # never submitted twice


def test_redo_submits_the_job_again_with_its_own_settings(jt):
    t = jt
    jid, key = start_job(t, {'goal': GOAL, 'hours': 3, 'workers': 2, 'name': 'nightly docs'})
    stop(t)
    old = t.f
    f = new_run(t, carry_from=[{'run': str(old.run_dir), 'reason': 'cannot resume: test'}])
    f.job_carry_in()
    tick(t)
    it, = [i for i in items(f, 'job') if i['key'] == jid]
    assert it['submit']['hours'] == 3 and it['submit']['workers'] == 2
    restart_state.decide(f, [{'item': it['id'], 'verdict': 'redo', 'reason': 'run it again'}], by='operator')
    done, = [i for i in items(f, 'job') if i['key'] == jid]
    row = jobs(f)[done['replayed']]
    assert row['goal'] == GOAL and row['settings']['hours'] == 3 and row['settings']['workers'] == 2
    assert row['spawn'].get('name') == 'nightly docs'


def test_redo_spec_keeps_the_job_specs_own_model_chain_and_not_a_lower_layer():
    row = {'goal': 'g', 'settings': {'hours': 2, 'workers': 1, 'supervisor_profile': None, 'worker_profiles': [],
                                     'chains': {'supervisor': ['opus'], 'worker': ['sonnet']},
                                     'chain_source': {'supervisor': 'spec', 'worker': 'discovery',
                                                      'supervisor_profile': None, 'worker_profiles': None}},
           'spawn': {'name': 'n'}, 'budget': None, 'artifacts': None, 'lifecycle': None}
    spec = restart_state.submit_spec(row)
    assert spec['supervisor'] == ['opus'] and 'worker' not in spec
    assert spec == {'goal': 'g', 'name': 'n', 'hours': 2, 'supervisor': ['opus'], 'workers': 1}


def test_root_restart_while_the_job_slot_rotates_loses_nothing_and_starts_once(jt):
    t = jt
    f = t.f
    jid = submit(f, {'goal': GOAL, 'supervisor_profile': 'claude-opus'}, t=t)['job_id']
    f.tick()
    key, rec = job_rec(f, jid)
    assert rec['status'] == 'rotating'
    stop(t)
    f = continue_root(t)
    all_ready(t)
    run_ticks(t, 6)
    assert jid in jobs(f) and result(f, jid) is None  # not lost, not failed
    assert len(subfleet.job_children(f.run_dir, jid)) <= 1  # never two sub-fleets for one job
    run_ticks(t, 2)
    assert len(subfleet.job_children(f.run_dir, jid)) <= 1


def test_resume_run_hands_the_job_on_with_its_model_chain(jt):
    t = jt
    jid, key = start_job(t)
    before = dict(jobs(t.f)[jid].get('chain') or {})
    child_before = fleet.load(t.f.run_dir / 'sub' / key / 'config.json').get('model_chain')
    stop(t)
    f = continue_root(t)
    all_ready(t)
    run_ticks(t, 6)
    assert jobs(f)[jid]['task'] == key and jobs(f)[jid]['state'] in ('starting', 'running', 'suspended')
    assert (jobs(f)[jid].get('chain') or {}) == before  # the chain recorded at the first start is kept
    assert fleet.load(f.run_dir / 'sub' / key / 'config.json').get('model_chain') == child_before
