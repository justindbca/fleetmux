"""Jobs on a service fleet: `fleetmux task <spec>` runs in its own task-mode sub-fleet (docs/design.md §58).

The program, not the supervisor, takes one idle worker slot, makes it the supervisor of a task sub-fleet that gets
the job's spec as its task, publishes the sub-fleet's outcome under the job id, and gives the slot back. The
supervisor is only told; its decision JSON cannot dispatch, answer or skip a job. No idle slot: the job waits in
the queue. Only private test Redis, fake cmux, fake CLIs (fixtures of test_service_split / test_service_escalate).
"""
import json
import time

import pytest

from test_escalate import ok_check  # noqa: F401  (fixture)
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_service_escalate import audits, escalations, result, svc  # noqa: F401
from test_lifecycle_close import notes, table  # noqa: F401  (fixtures)
from test_runtime_v2 import fx  # noqa: F401  (fixture)
from test_service_split import (answer_open_supervisor_note, bad, capture_child_specs, finish_child, items,  # noqa: F401
                                queued_to, replay_svc, ready_again, sf_files, split_tree, sub_runs)
from test_subfleet import given, hook, no_launch, step, tree  # noqa: F401
from test_v2_codex_claude import answer_turn, typed
from fleetmux import fleet, fleet_config, lifecycle_close, remote, runtime_v2, subfleet

# §58 model chains: these tests are about jobs, not models; tests/test_job_model_chain.py covers the chains
pytestmark = pytest.mark.usefixtures('job_chains_as_before')

GOAL = 'Survey the five vendor APIs and write a comparison report with a recommendation'


def submit(f, spec=None, t=None, cid='cmd-job'):
    """`fleetmux task <spec>` against the fleet: the event on its bus, the daemon steps, the reply."""
    spec = {'goal': GOAL} if spec is None else spec
    runtime_v2.post_event(f.v2.bus, {'kind': 'task', 'request_id': cid, 'spec': spec}, cid)
    if t is None:
        assert f.v2.step(10)
        f.pump()
    else:
        step(t)
    return runtime_v2.wait_reply(f.v2.bus, cid, timeout=2)


def jobs(f):
    return f.job_state()


def job_rec(f, jid):
    """The §44 split record that carries job ``jid`` (its M<n> key and the record)."""
    return next(((k, v) for k, v in escalations(f).items() if v.get('job') == jid), (None, None))


def supervisor_texts(f, needle):
    return [m['body']['text'] for m in queued_to(f, 'supervisor', needle)]


def start_job(t, spec=None, cid='cmd-job'):
    """Tree: submit, the tick that takes a slot, the tick that spawns the sub-fleet. Returns (job id, M<n>)."""
    f = t.f
    answer = submit(f, spec, t=t, cid=cid)
    assert answer['status'] == 'accepted', answer
    jid = answer['job_id']
    f.tick()
    step(t)
    f.tick()
    step(t)
    key, rec = job_rec(f, jid)
    assert rec and rec['status'] == 'delegated', escalations(f)
    return jid, key


# ------------------------------------------------------------------ entry
def test_job_returns_id_reserves_slot_and_tells_supervisor_once(svc, ok_check, monkeypatch):
    f = svc
    monkeypatch.setattr(subfleet, 'default_launch', no_launch)
    answer = submit(f)
    assert answer['status'] == 'accepted' and answer['job_id'].startswith(fleet.JOB_PREFIX)
    jid = answer['job_id']
    assert answer['task_id'] == jid
    # registered at once, never routed: not in the request queue, no routing prompt, nothing to a worker
    assert f.r.hget(fleet.REQ + jid, 'status') == 'job_queued'
    assert jid not in f.r.lrange(fleet.REQQ, 0, -1)
    assert jobs(f)[jid]['state'] == 'queued' and jobs(f)[jid]['goal'] == GOAL
    assert remote.result(f.r, jid)[0] == remote.PENDING
    f.tick()
    req = f.r.hgetall(fleet.REQ + jid)
    assert req['status'] == 'dispatched' and req['worker'] == 'worker-1'
    key, rec = job_rec(f, jid)
    assert key == 'M0' and rec['slot'] == 'worker-1' and rec['request'] == jid
    assert rec['proposal'] == [] and rec['status'] == 'approved' and rec.get('message_id') == ''
    assert jobs(f)[jid]['state'] == 'starting' and jobs(f)[jid]['slot'] == 'worker-1' and jobs(f)[jid]['task'] == 'M0'
    (path,) = sf_files(f)
    sf = fleet.load(path)
    defaults = fleet_config.job_subfleet()
    assert sf['split'] == jid and sf['slot'] == 'worker-1' and GOAL in sf['goal']
    assert sf['workers'] == defaults['workers'] and sf['hours'] == defaults['hours'] and sf['lifecycle'] is None
    assert not sf['proposal']
    # the supervisor: one notice, bound to the job (kind followup), no routing duty
    notes = queued_to(f, 'supervisor', jid)
    assert len(notes) == 1 and notes[0]['kind'] == 'followup' and notes[0]['request_id'] == jid
    assert 'do not dispatch, answer or skip' in notes[0]['body']['text']
    assert not supervisor_texts(f, 'needs routing')
    f.pump()
    assert f.agent('worker-1')['state'] == 'idle'
    assert not [x for x in typed(f, 'worker-1') if 'Payload' in x]


def test_job_spec_is_validated_and_overrides_the_config_block(svc, ok_check):
    f = svc
    f.cfg['limits'] = {'child_workers_max': 3}  # a job has no default cap; a root limit still applies
    for spec, why in (({}, 'goal'), ({'goal': GOAL, 'colour': 'red'}, 'colour'),
                      ({'goal': GOAL, 'workers': 0}, 'workers'), ({'goal': GOAL, 'hours': -1}, 'hours'),
                      ({'goal': GOAL, 'worker_profiles': 'claude-opus'}, 'worker_profiles'),
                      ({'goal': GOAL, 'workers': 2, 'worker_profiles': ['claude-opus']}, 'worker_profiles'),
                      ({'goal': GOAL, 'workers': 9}, 'child_workers_max'),
                      ({'goal': GOAL, 'lifecycle': 'service'}, 'lifecycle'),
                      ({'goal': GOAL, 'supervisor_profile': 'no-such-card'}, 'no-such-card')):
        answer = submit(f, spec, cid='cmd-' + str(abs(hash(json.dumps(spec))) % 10**8))
        assert answer['status'] == 'rejected' and why in answer['reason'], (spec, answer)
    assert not jobs(f)
    answer = submit(f, {'goal': GOAL, 'workers': 1, 'hours': 2, 'artifacts': [{'path': 'worker-1/report.md'}]})
    assert answer['status'] == 'accepted', answer
    f.tick()
    (path,) = sf_files(f)
    sf = fleet.load(path)
    assert sf['workers'] == 1 and sf['hours'] == 2 and sf['artifacts'] == [{'path': 'worker-1/report.md'}]


def test_config_block_defaults():
    got = fleet_config.job_subfleet()
    assert got == {'workers': 2, 'supervisor_profile': None, 'worker_profiles': [], 'hours': 4,
                   'supervisor': None, 'worker': None}  # §58 model chains: null = the discovery chains
    assert 'job_subfleet' in fleet_config.BLOCKS and 'job_subfleet' in fleet_config.USER_BLOCKS


def test_job_split_needs_no_proposal_supervisor_split_still_does(svc, ok_check):
    item = {'to': 'worker-1', 'request_id': 'r1', 'instruction': 'Run the big job as a sub-fleet.',
            'split': {'reason': 'too big for one worker'}}
    with pytest.raises(ValueError, match='proposal'):
        fleet.supervisor_split_check(item)
    f = svc
    jid = submit(f)['job_id']
    f.tick()
    _, rec = job_rec(f, jid)
    assert rec['proposal'] == [] and rec['status'] == 'approved'


def test_request_to_a_slot_a_job_holds_is_refused(svc, ok_check):
    f = svc
    jid = submit(f)['job_id']
    f.tick()
    with pytest.raises(ValueError, match=jid):
        f.request({'id': 'r9', 'to': 'worker-1', 'payload': {'q': 'x'}, 'reply': {'required': ['answer']}})
    # a request for a free worker is unchanged
    assert f.request({'id': 'r8', 'to': 'worker-2', 'payload': {'q': 'x'}, 'reply': {'required': ['answer']}}) == 'r8'


# ------------------------------------------------------------------ guard
def test_supervisor_json_cannot_dispatch_answer_or_skip_a_job(svc, ok_check):
    f = svc
    first = submit(f)['job_id']
    f.tick()
    key, _ = job_rec(f, first)
    ok = json.dumps({'status': 'RUNNING', 'reason': 'nothing to route', 'dispatch': [], 'skip': []})
    assert answer_turn(f, 'supervisor', ok)['status'] == 'committed' and bad(f) == '0'
    bodies = (
        lambda jid: {'dispatch': [{'to': 'worker-2', 'request_id': jid, 'instruction': 'Do the job yourself, all of it.'}]},
        lambda jid: {'answer': [{'request_id': jid, 'result': {'reply': 'done'}}]},
        lambda jid: {'skip': [{'request_id': jid, 'reason': 'not needed'}]},
        lambda jid: {'escalations': [{'task': key, 'decision': 'reject', 'note': 'do it yourself'}]},
        lambda jid: {'dispatch': [{'to': 'worker-1', 'instruction': 'Answer the question of this request.'}]},
    )
    for n, make in enumerate(bodies):
        jid = submit(f, cid='cmd-guard-' + str(n))['job_id']  # its notice is the delivery the reply answers
        body = dict({'status': 'RUNNING', 'reason': 'try', 'dispatch': [], 'skip': []}, **make(jid))
        assert answer_turn(f, 'supervisor', json.dumps(body))['status'] == 'committed'
        assert bad(f) == '1', body
        f.pump()
        assert 'is a job' in typed(f, 'supervisor')[-1] or first in typed(f, 'supervisor')[-1], typed(f, 'supervisor')[-1]
        assert answer_turn(f, 'supervisor', ok)['status'] == 'committed' and bad(f) == '0'
        assert f.r.hget(fleet.REQ + jid, 'status') == 'job_queued' and result(f, jid) is None
    assert f.r.hget(fleet.REQ + first, 'status') == 'dispatched' and result(f, first) is None
    assert escalations(f)[key]['status'] == 'approved'
    with pytest.raises(ValueError, match='is a job'):
        f.job_guard({'dispatch': [{'to': 'worker-2', 'request_id': first, 'instruction': 'Do the whole job.'}],
                     'skip': [], 'answer': [], 'escalations': []})


def test_dispatch_to_a_slot_a_job_holds_is_rejected(svc, ok_check):
    f = svc
    jid = submit(f)['job_id']
    f.tick()
    with pytest.raises(ValueError, match=jid):
        f.job_guard({'dispatch': [{'to': 'worker-1', 'request_id': 'r1', 'instruction': 'Answer the question.'}],
                     'skip': [], 'answer': [], 'escalations': []})


# ------------------------------------------------------------------ no idle slot: the job waits
def test_no_idle_slot_job_waits_in_queue(svc, ok_check, monkeypatch):
    f = svc
    monkeypatch.setattr(subfleet, 'process_requests', lambda f: None)  # spawns stay pending: slots stay held
    first = submit(f)['job_id']
    f.tick()
    assert jobs(f)[first]['slot'] == 'worker-1'
    f.setf('worker-2', state='busy')
    second = submit(f, cid='cmd-job-2')['job_id']
    f.tick()
    row = jobs(f)[second]
    assert row['state'] == 'queued' and row['slot'] == '' and row['waiting']
    assert f.r.hget(fleet.REQ + second, 'status') == 'job_queued'
    assert not supervisor_texts(f, 'needs routing') and result(f, second) is None
    f.tick()  # still nothing free: still queued, never handed to the supervisor or a worker
    assert jobs(f)[second]['state'] == 'queued'
    f.setf('worker-2', state='idle')
    f.tick()
    assert jobs(f)[second]['slot'] == 'worker-2' and jobs(f)[second]['task'] == 'M1'
    assert f.r.hget(fleet.REQ + second, 'worker') == 'worker-2'


def test_job_is_never_timed_out_or_replayed_as_a_request(svc, ok_check):
    f = svc
    f.setf('worker-1', state='busy')
    f.setf('worker-2', state='busy')  # no slot: the job stays queued through all of this
    jid = submit(f)['job_id']
    answer_open_supervisor_note(f)
    f.r.hset(fleet.REQ + jid, mapping={'created': time.time() - 10 * 3600})  # far past request_timeout 3600
    f.expire_requests(time.time())
    f.v2.tick()
    assert f.r.hget(fleet.REQ + jid, 'status') == 'job_queued' and result(f, jid) is None
    assert jid not in f.r.lrange(fleet.REQQ, 0, -1)
    f.setf('worker-1', state='idle')
    f.setf('worker-2', state='idle')
    replay_svc(f)  # the request replay of `start --resume`
    assert not supervisor_texts(f, 'needs routing') and not [x for x in typed(f, 'worker-1') if jid in x]
    assert f.r.hget(fleet.REQ + jid, 'status') == 'job_queued' and jobs(f)[jid]['state'] == 'queued'


def test_status_lists_open_jobs(svc, ok_check, capsys):
    f = svc
    jid = submit(f)['job_id']
    f.tick()
    fleet.status(f.cfg)
    out = json.loads(capsys.readouterr().out)
    (row,) = out['jobs']
    assert row['id'] == jid and row['state'] == 'starting' and row['slot'] == 'worker-1' and row['subfleet'] == 'M0'


def test_queued_job_wakes_a_reclaimed_worker_and_stops_reclaim(fx, table, notes):  # noqa: F811
    f = fx
    f.cfg['lifecycle'] = 'service'
    now = time.time()
    lifecycle_close.tick(f, now=now + 31 * 60)
    assert f.agent('worker-1')['state'] == 'reclaimed'
    f.mgr.dock_control = lambda pane: True  # real CmuxSessionManager call made by respawn
    f.save_jobs({'job-0000000001': {'goal': GOAL, 'state': 'queued', 'created': now, 'settings': {}}})
    lifecycle_close.tick(f, now=now + 32 * 60)
    assert f.agent('worker-1')['state'] == 'starting' and f.agent('worker-1')['generation'] == '1'
    f.setf('worker-1', state='idle')
    lifecycle_close.tick(f, now=now + 120 * 60)  # idle long past the reclaim time: kept while the job waits
    assert f.agent('worker-1')['state'] == 'idle'


# ------------------------------------------------------------------ the sub-fleet (two real v2 fleets)
def test_job_spawns_a_task_mode_child_with_the_job_goal(split_tree, monkeypatch):
    t, f = split_tree, split_tree.f
    specs = capture_child_specs(monkeypatch)
    jid, key = start_job(t)
    cfg = fleet.load(f.run_dir / 'sub' / key / 'config.json')
    assert cfg['mode'] == 'task' and cfg['task_id'] == key and cfg['lifecycle'] is None
    assert cfg['adopt']['slot'] == 'worker-1'
    assert specs and GOAL in specs[-1]['goal'] and specs[-1]['hours'] == fleet_config.job_subfleet()['hours']
    assert 'Approved split' not in specs[-1]['goal']
    assert result(f, jid) is None and remote.result(f.r, jid)[0] == remote.PENDING  # no early "delegated" answer
    assert jobs(f)[jid]['state'] == 'running' and f.agent('worker-1')['state'] == 'delegated'
    rows = fleet.job_rows(f.r)
    assert rows == [dict(rows[0], id=jid, state='running', slot='worker-1', subfleet=key)]


def test_job_completed_publishes_result_and_frees_the_slot(split_tree, monkeypatch):
    t = split_tree
    jid, key = start_job(t)
    finish_child(t, key, 'completed', monkeypatch)
    f = t.f
    code, raw = remote.result(f.r, jid)
    assert code == remote.FOUND
    got = json.loads(raw)
    assert got['status'] == 'ok' and got['id'] == jid
    assert got['result']['outcome'] == 'completed' and got['result']['subfleet'] == key
    assert got['result']['workspace'].endswith(key + '/workspace') and 'test end: completed' in got['result']['reply']
    assert jid not in jobs(f) and key not in escalations(f)
    assert f.agent('worker-1')['state'] == 'idle'
    notes = queued_to(f, 'supervisor', '[Sub-fleet ' + key)
    assert len(notes) == 1 and notes[0]['kind'] == 'followup' and jid in notes[0]['body']['text']
    assert fleet.job_rows(f.r) == []


def test_job_blocked_result_says_blocked(split_tree, monkeypatch):
    t = split_tree
    jid, key = start_job(t)
    finish_child(t, key, 'blocked', monkeypatch)
    got = result(t.f, jid)
    assert got['status'] == 'blocked' and got['result']['outcome'] == 'blocked' and 'blocked' in got['error']


def test_spawn_refused_puts_the_job_back_in_the_queue(split_tree):
    t, f = split_tree, split_tree.f
    jid = submit(f, t=t)['job_id']
    f.tick()
    step(t)
    key, rec = job_rec(f, jid)
    assert rec['status'] == 'approved'
    f.setf('worker-1', state='busy')  # no longer idle when the spawn runs
    f.tick()
    step(t)
    assert jobs(f)[jid]['state'] == 'queued' and jobs(f)[jid]['tries'] == 1 and 'could not start' in jobs(f)[jid]['waiting']
    assert job_rec(f, jid) == (None, None) and f.r.hget(fleet.REQ + jid, 'status') == 'job_queued'
    assert not supervisor_texts(f, 'could not start') and not sub_runs(f)


def test_failed_spawns_fail_the_job_and_never_block_the_next_one(split_tree, monkeypatch):
    t, f = split_tree, split_tree.f
    monkeypatch.setattr(subfleet, 'default_launch',
                        lambda path, cfg, mgr: type('R', (), {'success': False, 'pane_id': None, 'error': 'boom'})())
    jid = submit(f, t=t)['job_id']
    seen = set()
    for _ in range(3 * fleet.JOB_TRIES):
        f.tick()
        step(t)
        seen.add(jobs(f).get(jid, {}).get('task') or '')
        if jid not in jobs(f):
            break
    got = result(f, jid)
    assert got['status'] == 'failed' and 'could not start' in got['error'] and jid not in jobs(f)
    assert len(seen - {''}) == fleet.JOB_TRIES  # a new number each time: a used one is never given out again
    assert not supervisor_texts(f, 'could not start')
    monkeypatch.setattr(subfleet, 'default_launch', no_launch)
    for slot in f.workers:
        f.setf(slot, state='idle')
    second = submit(f, t=t, cid='cmd-job-2')['job_id']
    f.tick()
    step(t)
    assert jobs(f)[second]['state'] == 'starting' and jobs(f)[second]['task'] not in seen


def test_job_with_supervisor_profile_rotates_then_runs(split_tree, monkeypatch):
    t, f = split_tree, split_tree.f
    jid = submit(f, {'goal': GOAL, 'supervisor_profile': 'claude-opus',
                     'worker_profiles': ['claude-haiku', 'claude-sonnet']}, t=t)['job_id']
    f.tick()  # a slot is taken and the approve records the rotation
    key, rec = job_rec(f, jid)
    assert rec['status'] == 'rotating' and jobs(f)[jid]['state'] == 'starting'
    f.tick()  # idle slot: the handoff turn starts
    assert f.agent('worker-1')['state'] == 'handoff'
    step(t)
    ask = given(t, f, 'worker-1')[-1]
    assert 'sub-fleet ' + key in ask
    assert hook(t, f, 'worker-1', 'handoff written', prompt=ask)['status'] in ('committed', 'handoff')
    step(t)
    info = f.agent('worker-1')
    assert info['generation'] == '1' and json.loads(info['runtime_launch'])['profile_id'] == 'claude-opus'
    assert hook(t, f, 'worker-1', 'READY', prompt='startup')['status'] == 'ready'
    f.tick()  # the spawn request is written
    f.tick()  # ... executed and accepted
    step(t)
    assert escalations(f)[key]['status'] == 'delegated' and jobs(f)[jid]['state'] == 'running'
    assert result(f, jid) is None
    ccfg = fleet.load(f.run_dir / 'sub' / key / 'config.json')
    assert ccfg['worker_profiles'] == {'worker-1': 'claude-haiku', 'worker-2': 'claude-sonnet'}
    finish_child(t, key, 'completed', monkeypatch)
    assert result(f, jid)['status'] == 'ok' and jid not in jobs(f)


def test_parent_stop_suspends_the_job_subfleet_without_a_result(split_tree):
    """section 58 "Restart": a stop of the root suspends a running job's sub-fleet (no result); the next start of the root
    hands it to a fresh slot (tests/test_job_queue_restart.py)."""
    t = split_tree
    jid, key = start_job(t)
    assert subfleet.stop_children(t.f, wait=0)
    assert result(t.f, jid) is None
    assert jobs(t.f)[jid]['state'] == 'suspended' and jobs(t.f)[jid]['task'] == key
    assert subfleet.record(t.f.run_dir / 'sub' / key)['state'] == 'suspended'


# ------------------------------------------------------------------ task mode (§19 "Task root": tests/test_task_root_job.py)
def test_task_on_a_task_mode_fleet_is_its_task_then_a_job(tree):
    f = tree.f
    first = runtime_v2.wait_reply(f.v2.bus, 'new-task', timeout=2)  # the fixture's own `task` event
    assert first['status'] == 'accepted' and first['task_id'].startswith('task-') and 'job_id' not in first
    assert f.task()['status'] == 'running' and not jobs(f)
    again = submit(f, {'goal': 'Write a one page summary of the notes'}, t=tree)
    assert again['status'] == 'accepted' and again['job_id'] in jobs(f) and f.task()['id'] == first['task_id']


# ------------------------------------------------------------------ a whole development fleet as a job
from pathlib import Path  # noqa: E402

from test_subfleet import adopt_by_hook, start_tree, stop_child_like_daemon  # noqa: E402
from fleetmux import applet_ops, lifecycle_deliver  # noqa: E402

DELIVER = {'repo': '/abs/repo', 'feature_branch': 'feat/x', 'deploy_branch': 'main', 'test_cmd': ['true'],
           'queue': '/abs/queue.json', 'review_file': 'review.json', 'via': 'queue'}  # review_file: an older spec's key, ignored


def team(tmp_path, extra=''):
    """A roster of two named workers (one with its own role file) and a roles directory with a supervisor role."""
    (tmp_path / 'team').mkdir(exist_ok=True)
    (tmp_path / 'team' / 'tester.md').write_text('# Tester role\nWrite the failing test first.\n')
    path = tmp_path / 'team' / 'roster.yaml'
    path.write_text('workers:\n  - {name: coder-1}\n  - {name: tester-1, role: tester.md}\n' + extra)
    roles = tmp_path / 'roles'
    roles.mkdir(exist_ok=True)
    (roles / 'supervisor.md').write_text('# Integrator supervisor\nMerge only after the review verdict PASS.\n')
    return path, roles


def test_job_fields_are_validated_at_entry(svc, ok_check, tmp_path):
    f = svc
    path, roles = team(tmp_path)
    nested, _ = team(tmp_path / 'n') if (tmp_path / 'n').mkdir() is None else (None, None)
    nested.write_text(nested.read_text() + 'subfleets:\n  - {name: inner, roster: roster.yaml, supervisor_slot: coder-1}\n')
    for spec, why in (({'goal': GOAL, 'roster': 'team/roster.yaml'}, 'absolute path'),
                      ({'goal': GOAL, 'roster': str(tmp_path / 'none.yaml')}, 'absolute path'),
                      ({'goal': GOAL, 'roster': str(path), 'workers': 2}, 'do not also give workers'),
                      ({'goal': GOAL, 'roster': str(nested)}, 'subfleets'),
                      ({'goal': GOAL, 'roles': str(tmp_path / 'nope')}, 'roles'),
                      ({'goal': GOAL, 'name': 'a\nb'}, 'name'),
                      ({'goal': GOAL, 'lifecycle': 'dev_task'}, 'dev_task needs "deliver"'),
                      ({'goal': GOAL, 'lifecycle': 'dev_task', 'deliver': dict(DELIVER, repo='rel')}, 'deliver.repo'),
                      ({'goal': GOAL, 'lifecycle': 'dev_task', 'deliver': {'repo': '/abs/repo'}}, 'deliver is missing'),
                      ({'goal': GOAL, 'deliver': DELIVER}, 'deliver needs lifecycle dev_task')):
        answer = submit(f, spec, cid='cmd-' + str(abs(hash(json.dumps(spec))) % 10**8))
        assert answer['status'] == 'rejected' and why in answer['reason'], (spec, answer)
    assert not jobs(f)
    answer = submit(f, {'goal': GOAL, 'name': 'feature  x', 'roster': str(path), 'roles': str(roles),
                        'lifecycle': 'dev_task', 'deliver': DELIVER}, cid='cmd-ok')
    assert answer['status'] == 'accepted', answer
    row = jobs(f)[answer['job_id']]
    assert row['settings']['workers'] == 2 and row['spawn'] == {
        'name': 'feature x', 'roster': str(path.resolve()), 'roles': str(roles.resolve()),
        'deliver': lifecycle_deliver.validate_deliver(DELIVER)}
    assert fleet.job_rows(f.r)[0]['name'] == 'feature x'
    # an old job spec that still names the removed review_loop lifecycle is accepted and runs as a plain task
    answer = submit(f, {'goal': GOAL, 'lifecycle': 'review_loop'}, cid='cmd-old-review-loop')
    assert answer['status'] == 'accepted', answer


def dev_job(t, tmp_path, monkeypatch, **extra):
    """A dev_task job with roster, roles and name started on worker-1; returns (job id, M<n>, child cfg, specs)."""
    path, roles = team(tmp_path)
    specs = capture_child_specs(monkeypatch)
    spec = dict({'goal': GOAL, 'name': 'feature-x', 'roster': str(path), 'roles': str(roles),
                 'lifecycle': 'dev_task', 'deliver': DELIVER}, **extra)
    jid, key = start_job(t, spec)
    return jid, key, fleet.load(t.f.run_dir / 'sub' / key / 'config.json'), specs


def test_job_child_carries_roster_roles_name_and_deliver(split_tree, tmp_path, monkeypatch):
    t, f = split_tree, split_tree.f
    jid, key, cfg, specs = dev_job(t, tmp_path, monkeypatch)
    run = f.run_dir / 'sub' / key
    assert cfg['job'] == jid and cfg['lifecycle'] == 'dev_task' and cfg['mode'] == 'task'
    assert cfg['workers'] == ['coder-1', 'tester-1'] and cfg['roster'] == str(run / 'roster.yaml')
    assert Path(cfg['worker_roles']['tester-1']).read_text().startswith('# Tester role')
    assert cfg['roles'] == str((tmp_path / 'roles').resolve())
    assert cfg['agents_title'].endswith('子 fleet feature-x · ' + key)
    assert subfleet.record(run)['title'] == cfg['agents_title']  # the sidebar card shows the job's name
    assert specs[-1]['deliver'] == lifecycle_deliver.validate_deliver(DELIVER)
    # the child: its task takes the deliver block like a root dev_task run, its agents get the roles
    cf, _ = adopt_by_hook(t, monkeypatch, subfleet.record(run))
    step(t)
    assert json.loads(cf.task()['lifecycle_spec'])['deliver']['feature_branch'] == 'feat/x'
    scope = (Path(cf.cfg['supervisor_cwd']) / 'AGENTS.md').read_text()
    assert 'Integrator supervisor' in scope
    assert 'Tester role' in (fleet.agent_cwd(cf.cfg, 'tester-1') / 'AGENTS.md').read_text()
    assert lifecycle_close.job_child(cf) and not lifecycle_close.managed(cf)


@pytest.mark.parametrize('gate, outcome', [({'ok': True, 'sha': 'abc123', 'steps': ['merged']}, 'ok'),
                                           ({'ok': False, 'problem': 'tests failed'}, 'blocked')])
def test_job_child_runs_the_dev_task_gate(split_tree, tmp_path, monkeypatch, gate, outcome):
    t, f = split_tree, split_tree.f
    jid, key, cfg, _ = dev_job(t, tmp_path, monkeypatch)
    cf, _ = adopt_by_hook(t, monkeypatch, subfleet.record(f.run_dir / 'sub' / key))
    step(t)
    calls = []

    def deliver(spec, workspace, **kw):
        calls.append((spec, str(workspace), kw))
        return dict(gate)
    monkeypatch.setattr(lifecycle_deliver, 'deliver', deliver)
    cf.v2.finish('completed', 'work merged')
    lifecycle_close.tick(cf)  # the same gate a root dev_task run passes (completion_problem)
    (spec, workspace, kw), = calls
    assert spec['repo'] == '/abs/repo' and workspace == str(cf.workspace()) and 'reviewer' not in kw
    res = fleet.load(cf.run_dir / 'result.json')
    assert res['status'] == ('completed' if gate['ok'] else 'blocked') and res['delivery']['ok'] is gate['ok']
    f.tick()
    stop_child_like_daemon(cf)
    f.tick()
    step(t)
    got = result(f, jid)
    assert got['status'] == outcome and got['result']['delivery'] == gate
    assert not cf.lifecycle_close  # closing stays with the parent


def test_job_child_has_no_review_cap(split_tree, tmp_path, monkeypatch):
    """An older job spec (deliver.review_file) with a round-3 FAIL verdict on disk: nothing reads it, the child
    keeps running."""
    t, f = split_tree, split_tree.f
    jid, key, cfg, _ = dev_job(t, tmp_path, monkeypatch)
    cf, _ = adopt_by_hook(t, monkeypatch, subfleet.record(f.run_dir / 'sub' / key))
    step(t)
    (cf.workspace() / 'review.json').write_text(json.dumps({'round': 3, 'verdict': 'FAIL', 'reviewer': 'opus',
                                                            'issues': [{'id': 'P1-1', 'severity': 'P1', 'title': 'x'}]}))
    lifecycle_close.tick(cf)
    assert cf.task()['status'] == 'running' and not (cf.run_dir / 'outstanding.json').exists()


# ------------------------------------------------------------------ no small fixed cap for jobs
@pytest.fixture
def wide_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(subfleet, 'default_launch', no_launch)
    monkeypatch.setattr(fleet.Fleet, 'preflight_profile', lambda self, launch, cli_change_ok=False: None)
    yield from start_tree(tmp_path, monkeypatch, mode='service', lifecycle='service', max_attempts=2,
                          workers=['worker-' + str(i) for i in range(1, 7)])


def run_jobs(t, n):
    ids = [submit(t.f, {'goal': GOAL + ' ' + str(i)}, t=t, cid='cmd-wide-' + str(i))['job_id'] for i in range(n)]
    for _ in range(8):  # each job: the tick that takes a slot, the tick that spawns (bus steps may lag a tick)
        t.f.tick()
        step(t)
        if all(jobs(t.f).get(j, {}).get('state') != 'starting' for j in ids):
            break
    return ids


def test_five_jobs_start_without_roster_limits(wide_tree):
    t, f = wide_tree, wide_tree.f
    ids = run_jobs(t, 5)
    assert [jobs(f)[j]['state'] for j in ids] == ['running'] * 5  # default max_children 3 / agents 12 do not apply
    assert len(subfleet.active_children(f.run_dir)) == 5


def test_roster_limits_cap_jobs(wide_tree, tmp_path):
    t, f = wide_tree, wide_tree.f
    path = tmp_path / 'root-roster.yaml'
    path.write_text('workers:\n' + ''.join('  - {name: worker-' + str(i) + '}\n' for i in range(1, 7))
                    + 'limits: {max_children: 2}\n')
    f.cfg['roster'] = str(path)
    ids = run_jobs(t, 3)
    assert [jobs(f)[j]['state'] for j in ids[:2]] == ['running'] * 2, [(jobs(f)[j]['waiting'], jobs(f)[j]['tries']) for j in ids]
    assert jobs(f)[ids[2]]['state'] == 'queued' and 'max_children 2' in jobs(f)[ids[2]]['waiting']
    assert not jobs(f)[ids[2]]['hot_slot']  # a tree-wide cap is not helped by another worker


def test_split_keeps_the_default_caps(svc):
    assert subfleet.limits(svc.cfg)['max_children'] == 3 and subfleet.limits(svc.cfg, job=True)['max_children'] is None


# ------------------------------------------------------------------ no idle worker: one is added
def test_no_idle_worker_adds_one_that_supervises_the_job_and_retires_after(split_tree, tmp_path):
    t, f = split_tree, split_tree.f
    path = tmp_path / 'root-roster.yaml'
    path.write_text('# root roster\nworkers:\n  - {name: worker-1}\n  - {name: worker-2}\n')
    f.cfg['roster'] = str(path)
    f.tick()  # the roster is followed from now on
    f.setf('worker-1', state='busy')
    f.setf('worker-2', state='busy')
    jid = submit(f, t=t)['job_id']
    f.tick()
    assert jobs(f)[jid]['hot_slot'] == 'worker-3' and 'added worker worker-3' in jobs(f)[jid]['waiting']
    assert '- {name: worker-3}' in path.read_text() and path.read_text().startswith('# root roster\n')
    f.tick()  # the roster reload starts it
    assert 'worker-3' in f.workers
    assert hook(t, f, 'worker-3', 'READY', prompt='startup')['status'] == 'ready'
    step(t)
    for _ in range(2):
        f.tick()
        step(t)
    key, rec = job_rec(f, jid)
    assert rec['slot'] == 'worker-3' and rec['status'] == 'delegated' and jobs(f)[jid]['state'] == 'running'
    assert f.agent('worker-3')['state'] == 'delegated'
    f.job_cancel(jid)  # the job ends (an operator cancel; a root stop would suspend it, section 58 "Restart")
    assert subfleet.stop_children(f, wait=0)  # its sub-fleet is collected
    assert result(f, jid)['status'] == 'failed'
    assert 'worker-3' not in path.read_text()
    f.setf('worker-1', state='idle')
    f.setf('worker-2', state='idle')
    for _ in range(3):
        for slot in f.workers:
            if f.agent(slot).get('state') == 'starting':
                assert hook(t, f, slot, 'READY', prompt='startup')['status'] == 'ready'
        f.tick()
        step(t)
    assert 'worker-3' not in f.workers and f.agent('worker-3')['state'] == 'retired'


def test_job_hot_add_max_caps_added_workers(split_tree, tmp_path):
    t, f = split_tree, split_tree.f
    path = tmp_path / 'root-roster.yaml'
    path.write_text('workers:\n  - {name: worker-1}\n  - {name: worker-2}\nlimits: {job_hot_add_max: 0}\n')
    f.cfg['roster'] = str(path)
    f.tick()
    f.setf('worker-1', state='busy')
    f.setf('worker-2', state='busy')
    jid = submit(f, t=t)['job_id']
    f.tick()
    assert jobs(f)[jid]['state'] == 'queued' and not jobs(f)[jid]['hot_slot']
    assert 'job_hot_add_max 0' in jobs(f)[jid]['waiting'] and 'worker-3' not in path.read_text()


def test_remove_worker_row_takes_out_only_that_row(tmp_path):
    path = tmp_path / 'r.yaml'
    path.write_text('# keep\nworkers:\n  - {name: worker-1}\n  - {name: worker-3}\n')
    assert applet_ops.remove_worker_row(path, 'worker-3') and path.read_text() == '# keep\nworkers:\n  - {name: worker-1}\n'
    assert not applet_ops.remove_worker_row(path, 'worker-3')


def test_job_subfleets_are_listed_in_submit_order(tmp_path):
    """Two jobs submitted one after the other: M1's spawn ran 1 s before M0's, the sidebar still lists M0 first."""
    for key, created, submitted in (('M0', 101.0, 50.0), ('M1', 100.0, 51.0)):
        (tmp_path / 'sub' / key).mkdir(parents=True)
        subfleet.save_record(tmp_path / 'sub' / key, {'task_id': key, 'created': created, 'submitted': submitted})
    assert [d.name for d in subfleet.children_dirs(tmp_path)] == ['M0', 'M1']


def test_job_record_carries_the_submit_time(wide_tree):
    t, f = wide_tree, wide_tree.f
    ids = run_jobs(t, 2)
    recs = [subfleet.record(d) for d in subfleet.children_dirs(f.run_dir)]
    assert [r['job'] for r in recs] == ids and [r['submitted'] for r in recs] == [jobs(f)[j]['created'] for j in ids]


def test_rotating_job_slot_keeps_the_job_fields(split_tree, tmp_path, monkeypatch):
    """A job with a supervisor card rotates first; the replayed spawn still carries roster, roles and deliver."""
    t, f = split_tree, split_tree.f
    path, roles = team(tmp_path)
    specs = capture_child_specs(monkeypatch)
    jid = submit(f, {'goal': GOAL, 'supervisor_profile': 'claude-opus', 'name': 'feature-x', 'roster': str(path),
                     'roles': str(roles), 'lifecycle': 'dev_task', 'deliver': DELIVER}, t=t)['job_id']
    f.tick()
    key, rec = job_rec(f, jid)
    assert rec['status'] == 'rotating' and rec['spec']['job']['roster'] == str(path.resolve())
    f.tick()
    step(t)
    ask = given(t, f, 'worker-1')[-1]
    assert hook(t, f, 'worker-1', 'handoff written', prompt=ask)['status'] in ('committed', 'handoff')
    step(t)
    assert hook(t, f, 'worker-1', 'READY', prompt='startup')['status'] == 'ready'
    f.tick()
    f.tick()
    step(t)
    assert escalations(f)[key]['status'] == 'delegated'
    cfg = fleet.load(f.run_dir / 'sub' / key / 'config.json')
    assert cfg['job'] == jid and cfg['workers'] == ['coder-1', 'tester-1'] and cfg['lifecycle'] == 'dev_task'
    assert cfg['roles'] == str(roles.resolve()) and specs[-1]['deliver']['repo'] == '/abs/repo'


def test_job_workers_have_no_default_cap(svc, ok_check):
    f = svc
    answer = submit(f, {'goal': GOAL, 'workers': 5})
    assert answer['status'] == 'accepted', answer
    assert jobs(f)[answer['job_id']]['settings']['workers'] == 5
