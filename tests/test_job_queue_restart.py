"""Jobs survive a root fleet restart (docs/design.md section 58 "Restart").

A service root that is stopped (or dies) with open jobs is continued by the next start: queued and starting jobs
are still queued, a running job's sub-fleet is suspended and handed to a fresh slot of the same root (the same
sub/M<n>), and an operator's stop of one job stays a cancel. Private Redis, fake cmux, in-process daemons only.
"""
import json
import time
from pathlib import Path

import pytest

from test_escalate import ok_check  # noqa: F401  (fixture)
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_resume import kill_redis
from test_service_escalate import escalations, result, svc  # noqa: F401
from test_service_job import job_rec, start_job, submit
from test_service_split import split_tree, sub_runs  # noqa: F401
from test_subfleet import hook, step
from test_task_root_job import task_tree  # noqa: F401
from fleetmux import fleet, join, lifecycle_close, runtime_v2, subfleet

# section 58 model chains: these tests are about restarts, not models (as tests/test_service_job.py)
pytestmark = pytest.mark.usefixtures('job_chains_as_before')


def jobs(f):
    return f.job_state()


def mirror(f):
    path = f.run_dir / 'jobs.json'  # fleet.JOBS_FILE
    return json.loads(path.read_text()) if path.exists() else None


def queue_jobs(t, n, start=0):
    """Submit ``n`` jobs (they stay queued while no slot is free)."""
    out = []
    for i in range(start, start + n):
        answer = submit(t.f, {'goal': 'job number %d' % i}, t=t, cid='cmd-q%d' % i)
        assert answer['status'] == 'accepted', answer
        out.append(answer['job_id'])
    return out


def continue_root(t, kill=False, dead_sessions=False, keep_lock=None):
    """The root daemon is gone (stopped or crashed) and `start` continues the same run: Redis (reloaded from its AOF
    when ``kill``), a new Fleet with ``resume_requested`` and RuntimeV2.resume_run, as `daemon --resume` does."""
    old = t.f
    old.v2.close()
    old.stopped = True
    cfg = fleet.load(old.path)
    if kill:
        kill_redis(cfg)
        fleet.start_redis(cfg, tries=300)
    (old.run_dir / 'result.json').exists() and (old.run_dir / 'result.json').rename(old.run_dir / 'result.stopped.json')
    mgr = t.world.manager(cfg)
    if dead_sessions:
        mgr.tmux_session_alive = lambda pane: False  # a reboot / stop ended the fleet's tmux sessions
    again = fleet.Fleet(old.path, fleet.connect(cfg), mgr, sessions=t.sessions)
    t.fleets.remove(old)
    t.fleets.insert(0, again)
    t.f = again
    again.resume_requested = True
    again.v2.resume_run()
    return again


def all_ready(t):
    f = t.f
    for slot in f.names:
        if f.agent(slot).get('state') == 'starting':
            assert hook(t, f, slot, 'READY', prompt='startup')['status'] == 'ready', slot
    step(t)


def explicit_stop(t):
    """The root's explicit-stop tail as RuntimeV2.run runs it (no process exit, Redis stays for the test)."""
    f = t.f
    runtime_v2.post_event(f.v2.bus, {'kind': 'stop', 'request_id': 'stop-root'}, 'stop-root')
    for _ in range(5):
        if f.explicit_stop:
            break
        f.v2.step(10)
    assert f.explicit_stop
    f.tick()
    assert subfleet.stop_children(f, wait=0)
    f.write_result()
    if not (f.run_dir / 'result.json').exists():
        fleet.save_result(f.run_dir, {}, 'stopped', 'explicit fleet stop')
    getattr(f, 'job_stopped', lambda: None)()  # the stop tail's last mirror write


# ------------------------------------------------------------------ scenario 1 / goal 1
def test_scenario1_stop_then_start_keeps_queued_and_starting_jobs(split_tree):
    t = split_tree
    ids = queue_jobs(t, 3)
    t.f.tick()  # two idle workers: two jobs starting, the third waits
    before = {j: (x['state'], x['tries'], x['created']) for j, x in jobs(t.f).items()}
    assert sorted(s for s, _, _ in before.values()) == ['queued', 'starting', 'starting']
    explicit_stop(t)
    f = continue_root(t)
    assert set(jobs(f)) == set(ids)
    assert all(jobs(f)[j]['tries'] == before[j][1] for j in ids)  # a stop is not a failed start
    assert [jobs(f)[j]['created'] for j in ids] == [before[j][2] for j in ids]
    assert not f.paused() and not join._stopping(f.r)  # visible to live_root again
    all_ready(t)
    for _ in range(3):
        f.tick()
        step(t)
    assert sum(jobs(f)[j]['state'] in ('starting', 'running') for j in ids) == 2
    assert all(result(f, j) is None for j in ids)


def test_stop_and_resume_do_not_count_as_job_tries(split_tree):
    t = split_tree
    (jid,) = queue_jobs(t, 1)
    t.f.tick()
    assert jobs(t.f)[jid]['state'] == 'starting'
    explicit_stop(t)
    f = continue_root(t)
    assert jobs(f)[jid]['tries'] == 0 and jobs(f)[jid]['state'] in ('queued', 'starting', 'suspended')


def test_no_job_starts_while_the_root_stops(split_tree):
    t = split_tree
    f = t.f
    f.stopping = True
    (jid,) = queue_jobs(t, 1)
    f.job_tick()
    assert jobs(f)[jid]['state'] == 'queued'
    f.stopping = False


# ------------------------------------------------------------------ scenario 3 / goal 2
def test_scenario3_running_job_is_suspended_by_root_stop_and_resumes(split_tree):
    t = split_tree
    jid, key = start_job(t)
    child = t.f.run_dir / 'sub' / key
    explicit_stop(t)
    assert result(t.f, jid) is None  # not published failed
    assert jobs(t.f)[jid]['state'] == 'suspended' and jobs(t.f)[jid]['run'] == str(child)
    assert subfleet.record(child)['state'] == 'suspended'
    f = continue_root(t)
    all_ready(t)
    for _ in range(4):
        f.tick()
        step(t)
    rec = subfleet.record(child)
    assert rec['state'] == 'running' and rec.get('resumed') == 1
    assert jobs(f)[jid]['state'] == 'running' and jobs(f)[jid]['task'] == key
    assert sub_runs(f) == [key]  # the same sub-fleet, no second one
    adopt = fleet.load(child / 'config.json')['adopt']
    assert adopt['readopt'] is True and adopt['slot'] == rec['slot']
    assert adopt['generation'] == int(f.agent(rec['slot'])['generation'])
    assert f.agent(rec['slot'])['state'] == 'delegated'
    assert result(f, jid) is None


def test_scenario2_reboot_shape_keeps_the_queued_job_and_redelegates_the_running_one(split_tree):
    t = split_tree
    jid, key = start_job(t)
    (queued,) = queue_jobs(t, 1, start=5)
    slot = jobs(t.f)[jid]['slot']
    child_cfg = fleet.load(t.f.run_dir / 'sub' / key / 'config.json')
    for x in list(t.fleets[1:]):  # the child daemon dies with the machine
        x.v2.close()
        x.stopped = True
    subfleet.shutdown_redis(child_cfg)
    f = continue_root(t, kill=True, dead_sessions=True)  # no stop, no result.json: AOF reload + resume
    assert queued in jobs(f) and jobs(f)[queued]['state'] == 'queued'
    if f.agent(slot).get('state') == 'delegated':  # liveness reaches the slot whose tmux session ended
        f.v2.tab_missing(slot, f.agent(slot))
    all_ready(t)
    for _ in range(4):
        f.tick()
        step(t)
    assert result(f, jid) is None, result(f, jid)
    assert jobs(f)[jid]['task'] == key and jobs(f)[jid]['state'] in ('starting', 'running')
    assert [d.name for d in subfleet.job_children(f.run_dir, jid)] == [key]  # the same sub-fleet, handed on


def test_never_twice_starting_job_spawned_before_its_delegated_answer(split_tree):
    t = split_tree
    (jid,) = queue_jobs(t, 1)
    f = t.f
    f.tick()  # approved: the spawn request is written
    subfleet.process_requests(f)  # the child is spawned, its answer not yet read
    (child,) = [d for d in subfleet.children_dirs(f.run_dir)]
    explicit_stop(t)
    assert jobs(t.f)[jid]['state'] == 'suspended' and jobs(t.f)[jid]['run'] == str(child)
    f = continue_root(t)
    all_ready(t)
    for _ in range(4):
        f.tick()
        step(t)
    assert sub_runs(f) == [child.name]  # never a second sub-fleet for the job
    assert subfleet.record(child)['state'] == 'running'


def test_never_twice_child_daemon_still_alive_after_root_crash(split_tree, monkeypatch):
    """Map 4.4 window 2: the root crashes, the delegated session is gone, but the job child's daemon still holds its
    lock. The resume asks that child to suspend and never relaunches or re-delegates it while it lives."""
    t = split_tree
    jid, key = start_job(t)
    child = t.f.run_dir / 'sub' / key
    live = {str(child)}
    real = fleet.daemon_running
    monkeypatch.setattr(fleet, 'daemon_running', lambda run: str(run) in live or real(run))
    launched = []
    monkeypatch.setattr(subfleet, 'resume_launch', lambda path, cfg, mgr: launched.append(path)
                        or __import__('types').SimpleNamespace(success=True, error=None))
    f = continue_root(t, dead_sessions=True)
    rec = subfleet.record(child)
    assert rec['state'] == 'stopping' and rec['outcome'] == 'suspended'
    assert not launched
    all_ready(t)
    f.tick()
    step(t)
    assert result(f, jid) is None and sub_runs(f) == [key] and not launched
    live.clear()  # the child daemon exits after its suspend
    for _ in range(4):
        f.tick()
        step(t)
    assert subfleet.record(child)['state'] in ('suspended', 'running')
    assert result(f, jid) is None and sub_runs(f) == [key]


def test_never_twice_a_final_child_is_published_not_suspended(split_tree):
    t = split_tree
    jid, key = start_job(t)
    child = t.f.run_dir / 'sub' / key
    fleet.save(child / 'result.json', {'status': 'completed', 'reason': 'done before the stop', 'task_id': key})
    explicit_stop(t)
    assert result(t.f, jid)['status'] == 'ok' and jid not in jobs(t.f)


def test_redelegated_job_keeps_its_remaining_hours(split_tree, monkeypatch):
    t = split_tree
    jid, key = start_job(t)
    child = t.f.run_dir / 'sub' / key
    ccfg = fleet.load(child / 'config.json')
    r = fleet.connect(ccfg)
    before = time.time() + 7200  # the child task as its daemon recorded it (no child daemon runs here)
    r.hset(fleet.TASK, mapping={'id': key, 'status': 'running', 'deadline': before})
    r.close()
    explicit_stop(t)
    f = t.f
    jobs_ = f.job_state()
    jobs_[jid]['suspended_at'] -= 3600  # suspended an hour ago
    f.save_jobs(jobs_)
    f = continue_root(t)
    all_ready(t)
    for _ in range(4):
        f.tick()
        step(t)
    r = fleet.connect(ccfg)
    after = float(r.hget(fleet.TASK, 'deadline'))
    r.close()
    assert after - before >= 3600 - 5


def test_child_resume_with_readopt_adopts_the_new_session(split_tree):
    t = split_tree
    jid, key = start_job(t)
    child = t.f.run_dir / 'sub' / key
    explicit_stop(t)
    f = continue_root(t)
    all_ready(t)
    for _ in range(4):
        f.tick()
        step(t)
    rec = subfleet.record(child)
    ccfg = fleet.load(child / 'config.json')
    cf = fleet.Fleet(child / 'config.json', fleet.connect(ccfg), t.world.manager(ccfg), sessions=t.sessions)
    t.fleets.append(cf)
    cf.resume_requested = True
    cf.v2.resume_run()
    sup = cf.agent('supervisor')
    assert sup['pane'] == ccfg['adopt']['pane'] == rec['pane'] and sup['generation'] == '0'
    assert sup['state'] == 'starting'
    assert not fleet.load(child / 'config.json')['adopt'].get('readopt')  # done once


# ------------------------------------------------------------------ goal 2: an operator's stop is a cancel
def test_operator_stop_of_running_job_is_a_cancel_and_is_not_resumed(split_tree):
    t = split_tree
    jid, key = start_job(t)
    answer = t.f.job_cancel(jid)
    assert answer['status'] == 'stopping'
    assert jobs(t.f)[jid]['cancel']['by'] == 'operator'
    explicit_stop(t)
    got = result(t.f, jid)
    assert got['status'] == 'failed' and got['result']['outcome'] == 'cancelled'
    f = continue_root(t)
    all_ready(t)
    f.tick()
    assert jid not in jobs(f) and subfleet.record(t.f.run_dir / 'sub' / key)['state'] != 'running'


def test_operator_stop_then_root_restart_before_collect_stays_cancelled(split_tree):
    t = split_tree
    jid, key = start_job(t)
    t.f.job_cancel(jid)  # the child got a plain stop; the root dies before it collects
    child = t.f.run_dir / 'sub' / key
    fleet.save(child / 'result.json', {'status': 'cancelled', 'reason': 'explicit fleet stop', 'task_id': key})
    for x in list(t.fleets[1:]):
        x.v2.close()
        x.stopped = True
    f = continue_root(t, dead_sessions=True)
    all_ready(t)
    for _ in range(3):
        f.tick()
        step(t)
    got = result(f, jid)
    assert got and got['result']['outcome'] == 'cancelled' and jid not in jobs(f)
    assert sub_runs(f) == [key]


def test_cancel_written_while_the_root_is_down_wins_over_redelegation(split_tree):
    t = split_tree
    jid, key = start_job(t)
    explicit_stop(t)
    assert jobs(t.f)[jid]['state'] == 'suspended'
    fleet.save(t.f.run_dir / fleet.JOB_CANCELS / (jid + '.json'), {'by': 'operator', 'at': time.time()})
    f = continue_root(t)
    got = result(f, jid)
    assert got['status'] == 'failed' and got['result']['outcome'] == 'cancelled' and jid not in jobs(f)
    assert not (f.run_dir / fleet.JOB_CANCELS / (jid + '.json')).exists()


# ------------------------------------------------------------------ goal 6: stop cancels a job with no sub-fleet
def test_cancel_of_a_queued_job_publishes_a_result(svc, ok_check):
    f = svc
    f.stopping = True  # keep it queued
    jid = submit(f)['job_id']
    f.stopping = False
    answer = f.job_cancel(jid)
    assert answer['status'] == 'cancelled'
    got = result(f, jid)
    assert got['status'] == 'failed' and got['result']['outcome'] == 'cancelled' and jid not in jobs(f)
    assert json.loads((f.run_dir / fleet.JOB_RESULTS / (jid + '.json')).read_text())['status'] == 'failed'


def test_cancel_of_a_starting_job_never_requeues_it(split_tree):
    t = split_tree
    (jid,) = queue_jobs(t, 1)
    t.f.tick()
    assert jobs(t.f)[jid]['state'] == 'starting'
    answer = t.f.job_cancel(jid)
    assert answer['status'] == 'cancelled'
    assert jid not in jobs(t.f) and result(t.f, jid)['result']['outcome'] == 'cancelled'
    assert not [k for k, v in escalations(t.f).items() if v.get('job') == jid]
    for _ in range(2):
        t.f.tick()
        step(t)
    assert jid not in jobs(t.f) and not sub_runs(t.f)


def test_job_cancel_event_through_the_task_cli(svc, ok_check):
    f = svc
    f.stopping = True
    jid = submit(f)['job_id']
    f.stopping = False
    runtime_v2.post_event(f.v2.bus, {'kind': 'job_cancel', 'request_id': 'cancel-1', 'job': jid}, 'cancel-1')
    assert f.v2.step(10)
    answer = runtime_v2.wait_reply(f.v2.bus, 'cancel-1', timeout=2)
    assert answer['status'] == 'cancelled' and jid not in jobs(f)


# ------------------------------------------------------------------ mirror, restore, goal 5
def test_jobs_json_mirror_follows_fx_jobs(svc, ok_check):
    f = svc
    f.stopping = True
    jid = submit(f)['job_id']
    assert set(mirror(f)) == {jid}
    f.job_cancel(jid)
    assert mirror(f) == {}


def test_row_in_mirror_missing_from_reloaded_redis_is_restored(split_tree):
    t = split_tree
    (jid,) = queue_jobs(t, 1)
    rows = mirror(t.f)
    t.f.r.delete(fleet.JOBS, fleet.REQ + jid)  # the AOF lost the last second
    fleet.save(t.f.run_dir / fleet.JOBS_FILE, rows)
    f = continue_root(t)
    assert jobs(f)[jid]['state'] == 'queued' and f.r.hget(fleet.REQ + jid, 'status') == 'job_queued'


def test_row_with_a_result_on_disk_is_never_restored(split_tree):
    """Map condition (a): a result past RESULT_TTL lives only in job-results/<id>.json; the row never runs again."""
    t = split_tree
    (jid,) = queue_jobs(t, 1)
    rows = mirror(t.f)
    t.f.job_cancel(jid)
    t.f.r.delete(fleet.RESULT + jid)  # expired from Redis
    fleet.save(t.f.run_dir / fleet.JOBS_FILE, rows)
    f = continue_root(t)
    assert jid not in jobs(f)


def test_job_that_cannot_be_carried_is_failed_with_reason_and_notified_once(split_tree, tmp_path, monkeypatch):
    t = split_tree
    sent = []
    monkeypatch.setattr(lifecycle_close, 'NOTIFIER', lambda rec: sent.append(rec) or 'sent')
    roster = tmp_path / 'job-roster.yaml'
    roster.write_text('workers:\n  - name: worker-1\n')
    answer = submit(t.f, {'goal': 'needs its roster', 'roster': str(roster)}, t=t, cid='cmd-roster')
    jid = answer['job_id']
    t.f.stopping = True
    explicit_stop(t)
    roster.unlink()
    f = continue_root(t)
    all_ready(t)
    for _ in range(3):
        f.tick()
        step(t)
    got = result(f, jid)
    assert got['status'] == 'failed' and got['result']['outcome'] == 'not_carried' and 'roster' in got['error']
    assert len([s for s in sent if jid in s['text'] and s['channel'] == 'tg']) == 1
    f.job_notify_failed(jid, 'again')  # never twice
    assert len([s for s in sent if jid in s['text'] and s['channel'] == 'tg']) == 1


def test_resumed_root_is_visible_to_live_root_again(split_tree):
    t = split_tree
    queue_jobs(t, 1)
    explicit_stop(t)
    assert join._stopping(t.f.r)
    f = continue_root(t)
    assert not join._stopping(f.r)


# ------------------------------------------------------------------ task root: unchanged
def test_task_root_stop_still_fails_unstarted_jobs(task_tree):
    t = task_tree
    f = t.f
    f.stopping = True
    answer = submit(f, {'goal': 'a job beside the task'}, t=t, cid='cmd-tr')
    jid = answer['job_id']
    f.stopping = False
    f.v2.finish('cancelled', 'explicit fleet stop; runtime retirement required')
    f.job_tick()
    got = result(f, jid)
    assert got['status'] == 'failed' and 'before the job started' in got['error']
    assert json.loads((f.run_dir / fleet.JOB_RESULTS / (jid + '.json')).read_text())['status'] == 'failed'
