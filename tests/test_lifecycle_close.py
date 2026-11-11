"""Lifecycle closing (D2/D3): done gates, auto stop + close, hold -> stop_keep_resumable, service idling.

Fake cmux and fake Pi agents on a private Redis (tests/test_runtime_v2.py fixture); merge / test /
enqueue run against a temporary git repository. No real notification leaves the machine: a notifier is
injected (and without one, no channel command is configured)."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import event, fx, receive, req, session, settle  # noqa: F401  (fx fixture)
from fleetmux import fleet, lifecycle, lifecycle_close, lifecycle_deliver, lifecycle_hold, subfleet
from fleetmux import fleet_config


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def table(tmp_path, monkeypatch):
    """A private copy of the mode table that the daemon reads (hot reload is exercised on it)."""
    path = tmp_path / 'fleet_modes.yaml'
    path.write_text(fleet_config.SHIPPED.read_text())  # blocks format; edits keep the shipped wording
    monkeypatch.setattr(lifecycle, 'DEFAULT_PATH', path)
    return path


def edit_table(path, old, new):
    stat = path.stat()
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new, 1))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))  # distinct mtime even on coarse clocks


class Sent(list):
    pass


@pytest.fixture
def notes(monkeypatch):
    out = Sent()
    out.closers = []
    monkeypatch.setattr(lifecycle_close, 'NOTIFIER', lambda rec: out.append(rec) or 'test')
    monkeypatch.setattr(lifecycle_close, 'SPAWN_CLOSER', out.closers.append)
    return out


def as_task(f, name, initiator=None):
    f.service = False
    f.cfg.update(mode='task', lifecycle=name, initiator=initiator or {'kind': 'claude', 'id': 'origin-session'})
    return f


def submit(f, spec):
    event(f, dict(kind='task', request_id='task-' + str(time.time_ns()), spec=spec))
    return f.task()


def complete(f, spec):
    """The supervisor's real COMPLETED decision through the v2 settle path."""
    submit(f, dict(spec, artifacts=[{'path': 'reviewer/verdict.json'}]))  # COMPLETED needs a verified artifact
    assert f.task()['status'] == 'running'
    f.pump(); env = receive(f, 'supervisor')
    settle(f, 'supervisor', env, {'status': 'COMPLETED', 'completion': 100, 'dispatch': [], 'reason': 'verified'})
    assert f.task()['status'] == 'completed'


def stop_tail(f):
    """The daemon loop ends (stopping set by the lifecycle); RuntimeV2.run's explicit-stop tail retires
    every runtime, writes result.json, then lifecycle_close.finalize."""
    assert f.stopping and f.explicit_stop
    f.v2.close()
    f.v2.reader = f.v2.bus.blocking_client(1000)
    f.run()


def result_json(f):
    return json.loads((f.run_dir / 'result.json').read_text())


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """Temporary repo: main (deploy) checked out in a separate worktree is NOT allowed, so the repo's own
    worktree is on the feature branch and main is only a ref."""
    r = tmp_path / 'repo'
    r.mkdir()
    git(r, 'init', '-q', '-b', 'main')
    git(r, 'config', 'user.email', 't@example.org'); git(r, 'config', 'user.name', 't')
    (r / 'a.txt').write_text('base\n')
    git(r, 'add', '.'); git(r, 'commit', '-qm', 'base')
    git(r, 'checkout', '-qb', 'feat/x')
    (r / 'a.txt').write_text('feature\n')
    git(r, 'commit', '-qam', 'feature')
    return r


def deliver_spec(repo, tmp_path, test_cmd=None):
    return {'repo': str(repo), 'feature_branch': 'feat/x', 'deploy_branch': 'main',
            'test_cmd': test_cmd or [sys.executable, '-c', 'print("12 passed")'],
            'queue': str(tmp_path / 'queue' / 'deploy-queue.jsonl'), 'review_file': 'reviewer/verdict.json'}


def write_verdict(f, verdict='PASS', reviewer='opus', rnd=1, issues=(), name='reviewer/verdict.json'):
    path = f.workspace() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'round': rnd, 'verdict': verdict, 'reviewer': reviewer, 'issues': list(issues)}))


# ------------------------------------------------------------------ dev_task: gate, auto stop, close
def test_dev_task_completed_merges_tests_enqueues_then_stops_closes_and_notifies(fx, table, notes, repo, tmp_path):
    f = as_task(fx, 'dev_task')
    spec = deliver_spec(repo, tmp_path)
    write_verdict(f)
    complete(f, {'goal': 'ship feature x', 'deliver': spec})
    sha = git(repo, 'rev-parse', 'feat/x')
    f.tick()
    assert git(repo, 'rev-parse', 'main') == sha  # fast-forwarded by trusted Python
    queue = [json.loads(x) for x in Path(spec['queue']).read_text().splitlines()]
    assert queue == [dict(queue[0], status='pending_approval', sha=sha, deploy_branch='main', feature_branch='feat/x')]
    assert f.lifecycle_close['closed_by'] == 'auto'
    panes = [f.agent(s)['pane'] for s in f.names]
    stop_tail(f)
    assert set(panes) <= set(f.mgr.killed)  # every runtime retired before anything is closed
    out = result_json(f)
    assert out['status'] == 'completed' and out['mode'] == 'dev_task' and out['closed_by'] == 'auto'
    assert out['closed_at'] >= out['at'] and out['workspace'] == 'closing' and 'resumable' not in out
    assert out['delivery']['ok'] and out['delivery']['steps'] == ['review', 'fast_forward_check', 'tests', 'merged', 'enqueued']
    assert [n['channel'] for n in notes] == ['tg', 'parent']
    assert '已入队待批' in notes[0]['text'] and '已上线' not in notes[0]['text']
    assert notes[1]['initiator'] == {'kind': 'claude', 'id': 'origin-session'}
    assert len((f.run_dir / 'notifications.jsonl').read_text().splitlines()) == 2
    lifecycle_close.after_daemon(f)
    assert notes.closers == [str(f.path)]  # the detached workspace closer is launched after the daemon


def test_dev_task_gate_runs_once_and_is_idempotent(fx, table, notes, repo, tmp_path):
    spec = deliver_spec(repo, tmp_path)
    (tmp_path / 'ws').mkdir()
    (tmp_path / 'ws' / 'reviewer').mkdir()
    (tmp_path / 'ws' / 'reviewer' / 'verdict.json').write_text(json.dumps({'verdict': 'PASS', 'reviewer': 'opus'}))
    first = lifecycle_deliver.deliver(spec, tmp_path / 'ws')
    second = lifecycle_deliver.deliver(spec, tmp_path / 'ws')
    assert first['ok'] and second['ok']
    assert len(Path(spec['queue']).read_text().splitlines()) == 1  # queued once


@pytest.mark.parametrize('case,expect', [
    ('tests_fail', 'tests failed'),
    ('review_fail', 'review verdict is FAIL'),
    ('wrong_reviewer', 'the mode table asks for opus'),
    ('p1_left', '1 P1'),
    ('not_ff', 'not a fast-forward'),
    ('deploy_checked_out', 'is checked out in'),
    ('dirty', 'uncommitted changes'),
])
def test_dev_task_gate_refuses_and_never_moves_the_deploy_branch(tmp_path, repo, case, expect):
    spec = deliver_spec(repo, tmp_path)
    ws = tmp_path / 'ws'
    verdict = {'verdict': 'PASS', 'reviewer': 'opus', 'issues': []}
    if case == 'tests_fail':
        spec['test_cmd'] = [sys.executable, '-c', 'raise SystemExit(1)']
    elif case == 'review_fail':
        verdict['verdict'] = 'FAIL'
    elif case == 'wrong_reviewer':
        verdict['reviewer'] = 'sonnet'
    elif case == 'p1_left':
        verdict['issues'] = [{'id': 'X-1', 'severity': 'P1', 'title': 'open'}]
    elif case == 'not_ff':
        git(repo, 'checkout', '-q', 'main'); (repo / 'b.txt').write_text('x'); git(repo, 'add', '.')
        git(repo, 'commit', '-qm', 'diverge'); git(repo, 'checkout', '-q', 'feat/x')
    elif case == 'deploy_checked_out':
        git(repo, 'worktree', 'add', '-q', str(tmp_path / 'deploy-wt'), 'main')
    elif case == 'dirty':
        (repo / 'a.txt').write_text('uncommitted\n')
    (ws / 'reviewer').mkdir(parents=True)
    (ws / 'reviewer' / 'verdict.json').write_text(json.dumps(verdict))
    before = git(repo, 'rev-parse', 'main')
    rec = lifecycle_deliver.deliver(spec, ws)
    assert not rec['ok'] and expect in rec['problem']
    assert git(repo, 'rev-parse', 'main') == before and not Path(spec['queue']).exists()


def test_dev_task_failed_gate_turns_blocked_and_holds_instead_of_closing(fx, table, notes, repo, tmp_path):
    f = as_task(fx, 'dev_task')
    write_verdict(f)
    complete(f, {'goal': 'ship', 'deliver': deliver_spec(repo, tmp_path, [sys.executable, '-c', 'raise SystemExit(3)'])})
    f.tick()
    assert f.task()['status'] == 'blocked' and 'tests failed (exit 3)' in f.task()['reason']
    out = result_json(f)
    assert out['status'] == 'blocked' and 'closed_by' not in out  # result.json carries the gate verdict
    assert not f.stopping and f.lifecycle_close is None  # held: the run stays as it is
    assert [n['event'] for n in notes] == ['held', 'held'] and '保留现场 12 小时' in notes[0]['text']
    f.tick()
    assert len(notes) == 2  # the hold is announced once


def test_dev_task_submit_requires_a_deliver_spec(fx, table):
    f = as_task(fx, 'dev_task')
    event(f, dict(kind='task', request_id='no-deliver', spec={'goal': 'ship'}))
    answer = json.loads(f.r.get('fx:reply:no-deliver'))
    assert answer['status'] == 'rejected' and 'needs "deliver"' in answer['reason']


def typed(f, text, rid):
    """Operator text typed into the supervisor pane (pi input hook / Claude UserPromptSubmit -> operator_input)."""
    event(f, dict(session(f, 'supervisor'), kind='operator_input', text=text, request_id=rid))
    return json.loads(f.r.get('fx:reply:' + rid))


def test_lifecycle_run_refuses_typed_text_once_its_task_ended(fx, table, tmp_path):
    """Design §5 task mode makes typed text a new goal-only task when nothing runs. In a lifecycle run the
    task's end is the run's end (hold / close), so typed text after it is out of bounds and is refused as
    such (10-30 deepseek 97c82305: a ruling typed after dev_task went blocked became a task submission and
    was refused for lacking deliver; the ruling never reached the supervisor)."""
    f = as_task(fx, 'dev_task')
    submit(f, {'goal': 'ship', 'deliver': deliver_spec(tmp_path, tmp_path)})
    task_id = f.task()['id']
    f.finish('blocked', 'needs a human decision')
    answer = typed(f, '授权第一种，请继续③④', 'ruling')
    assert answer['status'] == 'rejected'
    assert task_id in answer['reason'] and 'blocked' in answer['reason'] and 'task -' in answer['reason']
    assert 'needs "deliver"' not in answer['reason']
    assert f.task()['id'] == task_id and f.task()['status'] == 'blocked'  # nothing was submitted


def test_spec_only_lifecycle_takes_no_task_from_typed_text(fx, table):
    """dev_task / review_loop tasks exist only as a spec (deliver / review_file): typed text is refused
    up front with the way in, not after a failed validation; smoke keeps the typed-goal path."""
    f = as_task(fx, 'dev_task')
    answer = typed(f, 'ship it', 'typed')
    assert answer['status'] == 'rejected' and 'task -' in answer['reason'] and not f.task()
    f = as_task(fx, 'smoke')
    answer = typed(f, 'have worker-1 reply OK', 'typed-smoke')
    assert answer['status'] == 'accepted' and f.task()['goal'] == 'have worker-1 reply OK'


# ------------------------------------------------------------------ hold -> stop_keep_resumable -> resume
def test_blocked_hold_then_stop_keep_resumable_and_resume_reopens(fx, table, notes, monkeypatch):
    f = as_task(fx, 'program')
    submit(f, {'goal': 'phase 1', 'hours': 2})
    f.finish('blocked', 'needs a human decision')
    f.tick()
    held_at = result_json(f)['at']
    lifecycle_close.tick(f, now=held_at + 11.9 * 3600)
    assert not f.stopping  # inside hold_hours: everything stays
    assert all(p in f.mgr.alive for p in (f.agent(s)['pane'] for s in f.names))
    lifecycle_close.tick(f, now=held_at + 12 * 3600 + 1)
    assert f.lifecycle_close == dict(f.lifecycle_close, closed_by='timeout', resumable=True)
    stop_tail(f)
    out = result_json(f)
    assert out['status'] == 'blocked' and out['resumable'] is True and out['closed_by'] == 'timeout'
    assert 'start --resume' in notes[-1]['text']
    assert fleet.resume_target(str(f.run_dir)) == f.run_dir  # not "already finished"
    fields = lifecycle_hold.reopen(f.r, f.task())
    assert fields['status'] == 'running' and f.task()['status'] == 'running'


def test_reopen_of_budget_exhausted_needs_extend_hours(fx, table):
    f = as_task(fx, 'dev_task')
    task = {'id': 't1', 'status': 'budget_exhausted', 'deadline': str(time.time() - 5)}
    with pytest.raises(ValueError, match='--extend-hours'):
        lifecycle_hold.reopen(f.r, task)
    fields = lifecycle_hold.reopen(f.r, task, extend_hours=2)
    assert fields['deadline'] > time.time() + 7000


def test_explicit_stop_result_stays_not_resumable(fx, table, notes):
    f = as_task(fx, 'program')
    submit(f, {'goal': 'x'})
    event(f, {'kind': 'stop', 'request_id': 'stop-user'})
    stop_tail(f)
    out = result_json(f)
    assert out['closed_by'] == 'user' and out['status'] == 'cancelled' and 'resumable' not in out
    with pytest.raises(SystemExit, match='already finished'):
        fleet.resume_target(str(f.run_dir))


def test_hold_hours_edit_takes_effect_on_the_next_daemon_tick(fx, table, notes):
    f = as_task(fx, 'program')
    submit(f, {'goal': 'x'})
    f.finish('budget_exhausted', 'time budget exhausted')
    f.tick()
    at = result_json(f)['at']
    lifecycle_close.tick(f, now=at + 2 * 3600)
    assert not f.stopping  # 12 h hold
    edit_table(table, 'on_budget_exhausted: {hold_hours: 12', 'on_budget_exhausted: {hold_hours: 1')
    lifecycle_close.tick(f, now=at + 2 * 3600)  # same daemon, next tick, no restart
    assert f.lifecycle_close['closed_by'] == 'timeout' and f.lifecycle_close['resumable']


# ------------------------------------------------------------------ smoke / scheduled / review_loop
def test_smoke_failure_closes_at_once(fx, table, notes):
    f = as_task(fx, 'smoke')
    submit(f, {'goal': 'probe the pair'})
    f.finish('blocked', 'worker-1 startup timed out before READY')
    f.tick()
    assert f.lifecycle_close['closed_by'] == 'auto'
    panes = [f.agent(s)['pane'] for s in f.names]
    stop_tail(f)
    assert set(panes) <= set(f.mgr.killed) and not (set(panes) & f.mgr.alive)
    assert result_json(f)['closed_by'] == 'auto' and result_json(f)['status'] == 'blocked'


def test_scheduled_hard_timeout_ends_and_closes(fx, table, notes):
    f = as_task(fx, 'scheduled')
    submit(f, {'goal': 'sector rotation 09:00'})
    limit = lifecycle.mode_policy('scheduled', 'hard_timeout_min')  # the yaml's value, not a copy of it
    t0 = time.time()
    lifecycle_close.tick(f, now=t0)
    lifecycle_close.tick(f, now=t0 + (limit - 1) * 60)
    assert f.task()['status'] == 'running'
    lifecycle_close.tick(f, now=t0 + limit * 60 + 1)
    assert f.task()['status'] == 'budget_exhausted' and 'hard_timeout_min ' + str(limit) in f.task()['reason']
    assert f.lifecycle_close['closed_by'] == 'timeout'


def test_review_loop_cap_ends_with_outstanding_list(fx, table, notes):
    f = as_task(fx, 'review_loop')
    submit(f, {'goal': 'fix review findings', 'review_file': 'reviewer/verdict.json'})
    issues = [{'id': 'P1-02', 'severity': 'P1', 'title': 'race in finish()'},
              {'id': 'P2-01', 'severity': 'P2', 'title': 'naming', 'status': 'fixed'}]
    write_verdict(f, 'FAIL', rnd=2, issues=issues)
    f.tick()
    assert f.task()['status'] == 'running'  # round 2 < max_rounds 3
    write_verdict(f, 'FAIL', rnd=3, issues=issues)
    f.tick()
    assert f.task()['status'] == 'blocked' and 'max_rounds 3' in f.task()['reason']
    listing = json.loads((f.run_dir / 'outstanding.json').read_text())
    assert [i['id'] for i in listing['issues']] == ['P1-02']
    assert 'P1-02' in (f.run_dir / 'outstanding.md').read_text()
    assert f.lifecycle_close['closed_by'] == 'auto' and f.lifecycle_close['report'].endswith('outstanding.json')
    stop_tail(f)
    assert result_json(f)['outstanding'].endswith('outstanding.json')


def test_review_loop_completed_needs_a_clean_pass(fx, table, notes):
    f = as_task(fx, 'review_loop')
    write_verdict(f, 'PASS', reviewer='whoever')
    complete(f, {'goal': 'fix', 'review_file': 'reviewer/verdict.json'})
    f.tick()
    assert f.lifecycle_close['closed_by'] == 'auto' and f.task()['status'] == 'completed'


# ------------------------------------------------------------------ service: reclaim, respawn, idle stop
def test_service_idle_worker_reclaimed_supervisor_kept_and_respawned_on_demand(fx, table, notes):
    f = fx
    f.cfg['lifecycle'] = 'service'
    old = f.agent('worker-1')['pane']
    now = time.time()
    lifecycle_close.tick(f, now=now + 29 * 60)
    assert f.agent('worker-1')['state'] == 'idle'
    lifecycle_close.tick(f, now=now + 31 * 60)
    assert f.agent('worker-1')['state'] == 'reclaimed' and old in f.mgr.killed and not f.agent('worker-1')['pane']
    assert f.agent('supervisor')['pane'] in f.mgr.alive  # the supervisor always stays
    f.v2.tick()  # the liveness check must not hold the fleet for the cleared pane
    assert not f.paused()
    f.mgr.dock_control = lambda pane: True  # real CmuxSessionManager call made by respawn
    req(f, 'r9', 'worker-1')
    f.pump()
    lifecycle_close.tick(f)
    assert f.agent('worker-1')['state'] == 'starting' and f.agent('worker-1')['pane'] not in ('', old)
    assert f.agent('worker-1')['generation'] == '1'


def test_start_flag_overrides_the_table_reclaim_for_this_run_only(fx, table, notes):
    """start --worker-idle-reclaim-min off keeps experts resident while the shared table says 30."""
    f = fx
    f.cfg['lifecycle'] = 'service'
    f.cfg['worker_idle_reclaim_min'] = None  # off
    lifecycle_close.tick(f, now=time.time() + 10 * 3600)
    assert f.agent('worker-1')['state'] == 'idle'
    f.cfg['worker_idle_reclaim_min'] = 120.0
    lifecycle_close.tick(f, now=time.time() + 60 * 60)
    assert f.agent('worker-1')['state'] == 'idle'
    lifecycle_close.tick(f, now=time.time() + 121 * 60)
    assert f.agent('worker-1')['state'] == 'reclaimed'


def test_reclaim_flag_parses_minutes_or_off():
    from fleetmux import fleet
    assert fleet.reclaim_minutes('off') == 'off' and fleet.reclaim_minutes('45') == 45.0
    for bad in ('0', '-5', 'never', ''):
        with pytest.raises(argparse.ArgumentTypeError):
            fleet.reclaim_minutes(bad)


def test_service_idle_stop_hours_null_by_default_then_hot_enabled(fx, table, notes):
    f = fx
    f.cfg['lifecycle'] = 'service'
    edit_table(table, 'worker_idle_reclaim_min: 30', 'worker_idle_reclaim_min: null')
    now = time.time()
    lifecycle_close.tick(f, now=now)
    lifecycle_close.tick(f, now=now + 100 * 3600)
    assert not f.stopping
    edit_table(table, 'idle_stop_hours: null', 'idle_stop_hours: 2')
    lifecycle_close.tick(f, now=now + 100 * 3600)
    assert f.lifecycle_close['closed_by'] == 'timeout'
    stop_tail(f)
    out = result_json(f)
    assert out['status'] == 'stopped' and 'idle_stop_hours' in out['reason'] and out['closed_by'] == 'timeout'


def test_reclaimed_slot_counts_as_retired_on_stop(fx, table, notes):
    f = fx
    f.cfg['lifecycle'] = 'service'
    lifecycle_close.tick(f, now=time.time() + 3600)
    assert f.agent('worker-1')['state'] == 'reclaimed'
    event(f, {'kind': 'stop', 'request_id': 'stop-svc'})
    stop_tail(f)
    assert result_json(f)['closed_by'] == 'user' and result_json(f)['workspace'] == 'closing'


# ------------------------------------------------------------------ scope: legacy runs, sub-fleets
def test_runs_without_lifecycle_are_untouched(fx, table, notes):
    f = fx
    f.service = False; f.cfg['mode'] = 'task'
    submit(f, {'goal': 'x'})
    f.finish('blocked', 'y')
    lifecycle_close.tick(f, now=time.time() + 100 * 3600)
    assert not f.stopping and not notes


def test_sub_fleet_is_closed_by_its_parent_not_by_itself(fx, table, notes):
    f = as_task(fx, 'dev_task')
    f.cfg['parent_config'] = '/parent/config.json'
    submit(f, {'goal': 'child work without deliver spec'})  # its parent checks the deliverable
    assert f.task()['status'] == 'running'
    f.finish('completed', 'done')
    lifecycle_close.tick(f, now=time.time() + 100 * 3600)
    assert not f.stopping and not notes


@pytest.mark.parametrize('parent,child', [('service', 'dev_task'), ('program', 'dev_task'), ('dev_task', 'dev_task'),
                                          (None, None)])
def test_child_config_lifecycle_comes_from_child_mode(parent, child, table):
    assert subfleet.child_lifecycle({'lifecycle': parent}) == child


# ------------------------------------------------------------------ workspace closer + ownership
class Registry:
    def __init__(self, meta, ok=True):
        self.meta, self.ok, self.closed = meta, ok, False
        self.meta_path = Path('/nonexistent/registry.json')

    def _meta(self):
        return self.meta

    def close_all(self):
        self.closed = True
        return self.ok

    def _surfaces(self, ws):
        return {} if self.ok or ws is None else {'s1': {}}

    def session_exists(self):  # cmux's definition, over this fake's surfaces
        return any(self._surfaces(self.meta.get(k)) for k in ('workspace', 'control_workspace'))


def test_closer_only_closes_a_registry_owned_by_this_run(tmp_path):
    run = tmp_path / 'run'
    run.mkdir()
    fleet.save(run / 'config.json', {'run': str(run)})
    fleet.save(run / 'result.json', {'status': 'completed'})
    mine = Registry({'run': str(run), 'surfaces': {'s1': {'output_file': str(run / 'prompts' / 'w.log')}}})
    assert lifecycle_close.close_workspace(run / 'config.json', mgr=mine) == 'closed' and mine.closed
    other = Registry({'run': str(tmp_path / 'other'), 'surfaces': {}})
    assert lifecycle_close.close_workspace(run / 'config.json', mgr=other).startswith('kept: cmux registry belongs')
    assert not other.closed
    mixed = Registry({'surfaces': {'s1': {'output_file': str(tmp_path / 'other' / 'w.log')}}})
    assert 'another run' in lifecycle_close.close_workspace(run / 'config.json', mgr=mixed) and not mixed.closed
    failing = Registry({'run': str(run), 'surfaces': {}, 'workspace': 'ws-1'}, ok=False)
    lifecycle_close_wait = lifecycle_close.all_gone
    lifecycle_close.all_gone = lambda mgr, meta: lifecycle_close_wait(mgr, meta, wait=0)
    try:
        assert lifecycle_close.close_workspace(run / 'config.json', mgr=failing).startswith('close_failed')
    finally:
        lifecycle_close.all_gone = lifecycle_close_wait
    lingering = Registry({'run': str(run), 'surfaces': {}, 'workspace': 'ws-1'}, ok=False)
    lingering._surfaces = lambda ws: {}  # close_all timed out but nothing of ours is live: closed
    assert lifecycle_close.close_workspace(run / 'config.json', mgr=lingering) == 'closed'
    assert fleet.load(run / 'result.json')['workspace'] == 'closed'


def test_stop_unconfirmed_keeps_workspace_and_alarms(fx, table, notes):
    f = as_task(fx, 'smoke')
    submit(f, {'goal': 'probe'})
    f.finish('blocked', 'x')
    f.tick()
    f.mgr.kill_pane = lambda pane: False  # cmux cannot confirm the close
    stop_tail(f)
    assert not f.close_workspace_pending and notes[-1]['event'] == 'stop_unconfirmed'
    assert 'closed_by' not in result_json(f)


# ------------------------------------------------------------------ notifier channels without injection
def test_without_channel_commands_nothing_leaves_the_machine(fx, table, monkeypatch, tmp_path):
    for env in (lifecycle_close.TG_ENV, lifecycle_close.PARENT_ENV):
        monkeypatch.delenv(env, raising=False)
    f = as_task(fx, 'smoke', {'kind': 'launchd', 'id': 'com.example.sector'})
    f.cfg['cmux_state_dir'] = str(tmp_path / 'state')
    recs = lifecycle_close.notify(f, 'closed', 'hello', lifecycle.get_mode('smoke'))
    assert [r['status'] for r in recs] == ['no_channel', 'mailbox']
    box = tmp_path / 'state' / 'mailbox' / 'launchd-com.example.sector.jsonl'
    assert json.loads(box.read_text())['text'] == 'hello'


def test_channel_command_gets_the_text(fx, table, monkeypatch, tmp_path):
    out = tmp_path / 'tg.txt'
    monkeypatch.setenv(lifecycle_close.TG_ENV, sys.executable + ' -c "import sys; open(%r, \'w\').write(sys.stdin.read())"' % str(out))
    f = as_task(fx, 'smoke')
    f.cfg['cmux_state_dir'] = str(tmp_path / 'state')
    recs = lifecycle_close.notify(f, 'closed', '收尾完成', lifecycle.get_mode('smoke'))
    assert recs[0]['status'] == 'sent' and out.read_text() == '收尾完成'


@pytest.mark.parametrize('value,env,expect', [
    ('claude:sess-1', {}, {'kind': 'claude', 'id': 'sess-1'}),
    (None, {'FLEETMUX_INITIATOR': 'program:reviewer'}, {'kind': 'program', 'id': 'reviewer'}),
    (None, {'XPC_SERVICE_NAME': 'com.example.sector.0900'}, {'kind': 'launchd', 'id': 'com.example.sector.0900'}),
    (None, {'XPC_SERVICE_NAME': '0'}, None),
    (None, {'XPC_SERVICE_NAME': 'application.com.cmux.123'}, None),
])
def test_initiator_recorded_at_start(value, env, expect):
    assert lifecycle_close.initiator_for_start(value, env) == expect


def test_bad_initiator_is_refused():
    with pytest.raises(ValueError, match='KIND:ID'):
        lifecycle_close.parse_initiator('nobody')


def test_stop_keep_resumable_run_is_picked_up_again_by_resume(fx, table, notes):
    f = as_task(fx, 'dev_task')
    f.mgr.has_cli_process = lambda pane, names=(): pane in f.mgr.alive
    f.mgr.dock_control = lambda pane: True
    submit(f, {'goal': 'x', 'hours': 4, 'deliver': {
        'repo': '/r', 'feature_branch': 'f', 'deploy_branch': 'm', 'test_cmd': ['true'], 'queue': '/q',
        'review_file': 'v.json'}})
    f.finish('blocked', 'needs a human')
    f.tick()
    lifecycle_close.tick(f, now=result_json(f)['at'] + 13 * 3600)
    stop_tail(f)
    assert result_json(f)['resumable'] and not (set(f.mgr.alive) - {'history-pane'})
    # start --resume: reopen (fleet.resume does this before launching the daemon), then the daemon's resume_run
    lifecycle_hold.reopen(f.r, f.task())
    (f.run_dir / 'result.json').unlink()
    again = fleet.Fleet(f.path, f.r, f.mgr)
    again.resume_requested = True
    again.v2.resume_run()
    try:
        assert again.task()['status'] == 'running' and not again.paused()
        assert all(again.agent(s)['state'] == 'starting' and again.agent(s)['generation'] == '1' for s in again.names)
        assert again.lifecycle_close is None and not again.stopping
    finally:
        again.v2.close()


class NoShutdown:
    """The fixture's Redis must survive resume()'s shutdown on the auto fallback."""
    def __init__(self, r):
        self.r, self.shut = r, False

    def __getattr__(self, name):
        return getattr(self.r, name)

    def shutdown(self):
        self.shut = True


def test_resume_of_budget_exhausted_resumable_run(fx, table, notes, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from fleetmux import cmux_manager
    # never reach the real cmux registry or the operator's latest.json
    launched = []
    monkeypatch.setattr(fleet, 'manager_for', lambda cfg: SimpleNamespace(
        spawn_window=lambda *a, **k: launched.append(a) or SimpleNamespace(success=True)))
    monkeypatch.setattr(fleet, 'LATEST', tmp_path / 'latest.json')
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: True)
    f = as_task(fx, 'program')
    fleet.save(f.path, dict(fleet.load(f.path), mode='task', lifecycle='program'))  # what start writes
    submit(f, {'goal': 'x', 'hours': 1})
    f.finish('budget_exhausted', 'time budget exhausted')
    f.r.hset(fleet.TASK, mapping={'deadline': time.time() - 1})
    f.tick()
    lifecycle_close.tick(f, now=result_json(f)['at'] + 13 * 3600)
    stop_tail(f)
    assert result_json(f)['resumable']
    client = NoShutdown(f.r)
    monkeypatch.setattr(fleet, 'connect', lambda cfg: client)
    args = SimpleNamespace(resume=str(f.run_dir), extend_hours=None, max_attempts=None, roster=None)
    with pytest.raises(SystemExit, match='--extend-hours'):
        fleet.resume(f.run_dir, args)
    args.resume = 'auto'
    assert fleet.resume(f.run_dir, args) is False and client.shut  # auto: a new run instead
    assert f.task()['status'] == 'budget_exhausted' and (f.run_dir / 'result.json').exists() and not launched
    args.resume, args.extend_hours = str(f.run_dir), 3
    assert fleet.resume(f.run_dir, args) is True and len(launched) == 1
    assert f.task()['status'] == 'running' and float(f.task()['deadline']) > time.time() + 3 * 3600 - 60
    assert not (f.run_dir / 'result.json').exists()
    assert fleet.load(tmp_path / 'latest.json')['run'] == str(f.run_dir)


# ------------------------------------------ A42-LINUX-RESTORE-FAILURE-04: a run with no task at all
def pre_ready_placeholder(f):
    """The 199 shape: the run failed before its first READY, was closed from the fleetmux sidebar and is
    ``resumable`` — with no task ever submitted (no id, no goal, no deadline, no artifacts)."""
    f.finish('blocked', 'worker-1: startup failed before READY')
    f.tick()
    # exactly the 199 close: the fleetmux sidebar keeps the run resumable (closed_by user, closed_via sidebar)
    lifecycle_close.request_close(f, 'user', 'fleetmux 侧栏「关闭整个 fleet」', resumable=True,
                                  via='fleetmux-sidebar')
    stop_tail(f)
    out = result_json(f)
    assert out['resumable'] is True and out['task_id'] == '' and out['reason'].endswith('startup failed before READY')
    return out


def test_reopen_refuses_the_pre_ready_placeholder_and_writes_no_task(fx, table):
    """The placeholder is never given a task: no status=running, no invented deadline, no budget verdict."""
    f = as_task(fx, 'smoke')
    out = pre_ready_placeholder(f)
    task = f.task()
    assert 'id' not in task and 'deadline' not in task, task
    assert lifecycle_hold.no_task(task, out) is True
    with pytest.raises(ValueError, match='never submitted a task'):
        lifecycle_hold.reopen(f.r, task, result=out)
    assert f.task() == task, 'reopen wrote a task the run never had: %r' % (f.task(),)


def test_no_task_is_strict_about_damaged_and_real_records(fx, table):
    """Only the fully evidenced placeholder is 'no task': a damaged or real record is never read as one."""
    placeholder = {'status': 'blocked', 'reason': 'worker-1: startup failed before READY', 'task_id': '',
                   'goal': '', 'artifacts': [], 'resumable': True}
    assert lifecycle_hold.no_task({'status': 'blocked'}, placeholder) is True
    # a goal, an artifact, a task id or a deadline anywhere means a real (or damaged) task, never a placeholder
    for result in ({**placeholder, 'goal': 'x'}, {**placeholder, 'task_id': 't1'},
                   {**placeholder, 'artifacts': [{'path': 'a'}]}, {**placeholder, 'reason': 'blocked by a human'}):
        assert lifecycle_hold.no_task({'status': 'blocked'}, result) is False, result
    for task in ({'status': 'blocked', 'id': 't1'}, {'status': 'blocked', 'goal': 'x'},
                 {'status': 'blocked', 'deadline': '1'}, {'status': 'running'}, {'status': 'blocked', 'lifecycle_spec': '{}'}):
        assert lifecycle_hold.no_task(task, placeholder) is False, task


def test_reopen_fails_closed_on_a_damaged_record(fx, table):
    """A record that is not the placeholder and has no usable id/deadline is refused, not reopened."""
    f = as_task(fx, 'dev_task')
    for task in ({'id': 't1', 'status': 'blocked'}, {'status': 'blocked', 'deadline': str(time.time() - 5)}):
        with pytest.raises(ValueError, match='task record is incomplete'):
            lifecycle_hold.reopen(f.r, task)
        assert not f.task(), 'a damaged record was written as a running task: %r' % (f.task(),)
    # a real expired task keeps its explicit budget gate and its extension
    with pytest.raises(ValueError, match='--extend-hours'):
        lifecycle_hold.reopen(f.r, {'id': 't1', 'status': 'budget_exhausted', 'deadline': str(time.time() - 5)})


def test_resume_of_the_placeholder_starts_the_initialization_and_keeps_the_evidence(fx, table, notes, monkeypatch, tmp_path):
    """start --resume of the placeholder launches the daemon (the run's own initialization), not a fake task."""
    from types import SimpleNamespace
    from fleetmux import cmux_manager
    launched = []
    monkeypatch.setattr(fleet, 'manager_for', lambda cfg: SimpleNamespace(
        spawn_window=lambda *a, **k: launched.append(a) or SimpleNamespace(success=True)))
    monkeypatch.setattr(fleet, 'LATEST', tmp_path / 'latest.json')
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: True)
    f = as_task(fx, 'smoke')
    fleet.save(f.path, dict(fleet.load(f.path), mode='task', lifecycle='smoke'))
    out = pre_ready_placeholder(f)
    client = NoShutdown(f.r)
    monkeypatch.setattr(fleet, 'connect', lambda cfg: client)
    args = SimpleNamespace(resume=str(f.run_dir), extend_hours=None, max_attempts=None, roster=None)
    assert fleet.resume(f.run_dir, args) is True and len(launched) == 1
    assert f.task().get('id', '') == '' and 'deadline' not in f.task(), f.task()
    assert not client.shut, 'the placeholder resume shut Redis down'
    assert result_json(f) == out, 'the failure evidence (result.json) was dropped instead of kept'


def test_resume_run_of_the_placeholder_takes_the_common_path_and_clears_it(fx, table):
    """A run that never submitted a task resumes exactly like a healthy empty run: intent gate, retire/reattach/
    respawn, unpause — and only then is the strict placeholder cleared (blocked TASK + closed result, evidence
    kept) so the run waits for a task again and a real task can be submitted."""
    f = as_task(fx, 'smoke')
    f.mgr.has_cli_process = lambda pane, names=(): pane in f.mgr.alive
    f.mgr.dock_control = lambda pane: True
    fleet.save(f.path, dict(fleet.load(f.path), mode='task', lifecycle='smoke'))  # what start writes
    out = pre_ready_placeholder(f)
    again = fleet.Fleet(f.path, f.r, f.mgr)
    assert not again.service, 'the test needs a task-mode run'
    again.r.hset(fleet.TASK, mapping={'status': 'blocked', 'reason': 'worker-1: startup failed before READY'})
    again.r.hset(fleet.SERVICE, mapping={'paused': 1, 'reason': 'same-run daemon restart recovered held'})
    again.resume_requested = True
    again.v2.resume_run()
    try:
        assert not again.paused(), 'the no-task resume left the run paused (it skipped the common f.resume())'
        assert again.task() == {}, 'the blocked placeholder TASK was left behind: %r' % (again.task(),)
        assert not (f.run_dir / 'result.json').exists(), 'the closed result was left behind'
        assert json.loads((f.run_dir / 'result.closed.json').read_text()) == out, 'the failure evidence was lost'
        assert again.history_pane, 'the no-task resume did not lay the page out again (no history page)'
        assert all(again.agent(s)['pane'] for s in again.names), [(s, again.agent(s)['state']) for s in again.names]
        assert again.r.llen(again.v2.bus.pending_key('supervisor')) == 0, 'a task was briefed with no id'
        assert not again.running(), 'the cleared run still reports a running task'
        # back to normal: a real task can be submitted, with its own id and a real deadline (no fake identity)
        again.submit({'goal': 'next', 'hours': 1})
        task = again.task()
        assert task['id'].startswith('task-') and float(task['deadline']) > time.time() + 3000, task
    finally:
        again.v2.close()


def test_resume_run_holds_an_ambiguous_intent_before_the_no_task_cleanup(fx, table):
    """A legitimate no-task run still passes the intent safety gate: an ambiguous intent holds it, nothing is
    respawned and the placeholder/evidence are left for the human."""
    f = as_task(fx, 'smoke')
    fleet.save(f.path, dict(fleet.load(f.path), mode='task', lifecycle='smoke'))
    out = pre_ready_placeholder(f)
    again = fleet.Fleet(f.path, f.r, f.mgr)
    again.r.hset(fleet.TASK, mapping={'status': 'blocked', 'reason': 'worker-1: startup failed before READY'})
    again.r.hset(again.v2.prefix + ':intent:ambiguous', mapping={'phase': 'applying', 'plan': '{"commands":[]}'})
    before = {s: (again.agent(s)['state'], again.agent(s)['pane']) for s in again.names}
    again.resume_requested = True
    again.v2.resume_run()
    try:
        assert again.paused(), 'an ambiguous intent did not hold the no-task resume'
        assert {s: (again.agent(s)['state'], again.agent(s)['pane']) for s in again.names} == before, \
            'the no-task resume retired or respawned slots before the intent gate'
        assert again.task().get('status') == 'blocked', 'the placeholder was cleared despite the held intent'
        assert result_json(f) == out and not (f.run_dir / 'result.closed.json').exists()
    finally:
        again.v2.close()


def test_resume_run_of_a_healthy_run_without_a_task_keeps_the_original_flow(fx, table):
    """A task-mode run that simply has no task yet resumes normally: no cleanup is invented, nothing is written,
    and the run waits for the operator's task."""
    f = as_task(fx, 'smoke')
    fleet.save(f.path, dict(fleet.load(f.path), mode='task', lifecycle='smoke'))
    assert f.task() == {} and not (f.run_dir / 'result.json').exists()
    again = fleet.Fleet(f.path, f.r, f.mgr)
    again.mgr.dock_control = lambda pane: True
    again.r.hset(fleet.SERVICE, mapping={'paused': 1, 'reason': 'same-run daemon restart recovered held'})
    again.resume_requested = True
    again.v2.resume_run()
    try:
        assert not again.paused()
        assert again.task() == {}, 'a task was invented for a run that has none: %r' % (again.task(),)
        assert not (f.run_dir / 'result.closed.json').exists(), 'cleanup ran without a closed result'
        assert all(again.agent(s)['pane'] for s in again.names)
        again.submit({'goal': 'first', 'hours': 1})
        assert again.task()['id'].startswith('task-') and float(again.task()['deadline']) > time.time()
    finally:
        again.v2.close()


def test_resume_run_refuses_a_damaged_record_before_any_side_effect(fx, table):
    """A damaged running record is refused before the respawn, with a clear message (never a bare KeyError)."""
    f = as_task(fx, 'smoke')
    fleet.save(f.path, dict(fleet.load(f.path), mode='task', lifecycle='smoke'))
    pre_ready_placeholder(f)
    broken = fleet.Fleet(f.path, f.r, f.mgr)
    broken.r.hset(fleet.TASK, mapping={'status': 'running', 'goal': 'x'})
    assert broken.task().get('goal') == 'x', broken.task()
    broken.resume_requested = True
    before = {s: (broken.agent(s)['state'], broken.agent(s)['pane']) for s in broken.names}
    with pytest.raises(SystemExit, match='task record is damaged'):
        broken.v2.resume_run()
    after = {s: (broken.agent(s)['state'], broken.agent(s)['pane']) for s in broken.names}
    assert after == before, 'the damaged record was retired or respawned before it was refused: %r -> %r' % (
        before, after)
    broken.v2.close()


def test_public_resume_refuses_a_damaged_running_task_without_side_effects(fx, table, notes, monkeypatch, tmp_path):
    """The public layer classifies before it deletes result.json or spawns: a running task with no id fails with
    a nonzero explicit error, no spawn_window, and the closed result stays where it is."""
    from types import SimpleNamespace
    from fleetmux import cmux_manager
    launched = []
    monkeypatch.setattr(fleet, 'manager_for', lambda cfg: SimpleNamespace(
        spawn_window=lambda *a, **k: launched.append(a) or SimpleNamespace(success=True)))
    monkeypatch.setattr(fleet, 'LATEST', tmp_path / 'latest.json')
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: True)
    f = as_task(fx, 'smoke')
    fleet.save(f.path, dict(fleet.load(f.path), mode='task', lifecycle='smoke'))
    out = pre_ready_placeholder(f)
    f.r.hset(fleet.TASK, mapping={'status': 'running', 'goal': 'x'})  # a running task with no id
    client = NoShutdown(f.r)
    monkeypatch.setattr(fleet, 'connect', lambda cfg: client)
    args = SimpleNamespace(resume=str(f.run_dir), extend_hours=None, max_attempts=None, roster=None)
    with pytest.raises(SystemExit, match='damaged'):
        fleet.resume(f.run_dir, args)
    assert launched == [], 'a damaged record was spawned as a daemon: %r' % (launched,)
    assert result_json(f) == out, 'the closed result was deleted before the record was refused'
    assert f.task()['goal'] == 'x', 'the damaged record was rewritten: %r' % (f.task(),)
    assert not client.shut, 'Redis was shut down by a refused resume'


def test_classify_needs_a_usable_deadline_and_an_actual_empty_artifact_list(fx, table):
    """A non-empty deadline string is not a usable deadline, and {} is not an empty artifact list: both fail
    closed instead of reading a damaged record as a real task / as 'never submitted'."""
    soon = str(time.time() + 3600)
    assert lifecycle_hold.classify({'id': 't1', 'deadline': soon}, {}) == 'task'
    for bad in ({'id': 't1', 'deadline': 'soon'}, {'id': 't1', 'deadline': '0'}, {'id': 't1', 'deadline': '-5'},
                {'id': 't1', 'deadline': 'nan'}, {'id': 't1', 'deadline': 'inf'}, {'id': 't1'}):
        with pytest.raises(ValueError, match='no usable deadline'):
            lifecycle_hold.classify(bad, {})
    with pytest.raises(ValueError, match='no id'):
        lifecycle_hold.classify({'status': 'running', 'deadline': soon}, {})
    placeholder = {'status': 'blocked', 'reason': 'worker-1: startup failed before READY', 'task_id': '',
                   'goal': '', 'artifacts': [], 'resumable': True}
    assert lifecycle_hold.no_task({'status': 'blocked', 'artifacts': '[]'}, placeholder) is True
    assert lifecycle_hold.no_task({'status': 'blocked'}, placeholder) is True
    for bad in ('{}', '"x"', '[1]', 'nonsense', {}, ['x'], 0):
        assert lifecycle_hold.no_task({'status': 'blocked', 'artifacts': bad}, placeholder) is False, bad
    assert lifecycle_hold.no_task({'status': 'blocked'}, {**placeholder, 'artifacts': {}}) is False


def test_classify_refuses_a_durable_result_that_contradicts_an_empty_task(fx, table):
    """TASK={} is not 'never submitted' when a readable result.json names a task: a goal, artifacts, a task id or a
    deadline there is a contradiction and fails closed (P-04-CLASSIFY); a healthy empty run stays legal."""
    blocked = {'resumable': True, 'status': 'blocked', 'reason': 'blocked by a human', 'task_id': '',
               'goal': '', 'artifacts': []}
    for extra, what in (({'goal': 'x'}, 'a goal'),
                        ({'artifacts': [{'path': 'a'}]}, 'artifacts'),
                        ({'task_id': 't9'}, 'task t9'),
                        ({'deadline': '1'}, 'a deadline')):
        with pytest.raises(ValueError, match='names ' + what + ' but no task record exists'):
            lifecycle_hold.classify({}, {**blocked, **extra})
    # a readable closed result that names no task and is not the strict placeholder is not guessed either
    with pytest.raises(ValueError, match='refusing to guess'):
        lifecycle_hold.classify({}, blocked)
    # a healthy run that never had a task and never wrote a result is still legal
    assert lifecycle_hold.classify({}, {}, present=False) == 'none'
    # an existing but unreadable result.json is never an empty one
    with pytest.raises(ValueError, match='exists but cannot be read'):
        lifecycle_hold.classify({}, {}, True, False)
    with pytest.raises(ValueError, match='exists but cannot be read'):
        lifecycle_hold.classify({'id': 't1', 'deadline': str(time.time() + 60)}, {}, True, False)


def test_public_resume_refuses_an_empty_task_contradicted_by_the_durable_result(fx, table, notes, monkeypatch,
                                                                                tmp_path):
    """The public layer refuses the contradiction before deleting result.json or spawning anything."""
    from types import SimpleNamespace
    from fleetmux import cmux_manager
    launched = []
    monkeypatch.setattr(fleet, 'manager_for', lambda cfg: SimpleNamespace(
        spawn_window=lambda *a, **k: launched.append(a) or SimpleNamespace(success=True)))
    monkeypatch.setattr(fleet, 'LATEST', tmp_path / 'latest.json')
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: True)
    f = as_task(fx, 'smoke')
    fleet.save(f.path, dict(fleet.load(f.path), mode='task', lifecycle='smoke'))
    pre_ready_placeholder(f)
    out = result_json(f)
    out.update(resumable=True, status='blocked', task_id='', goal='a real goal', artifacts=[{'path': 'w/r.md'}])
    (f.run_dir / 'result.json').write_text(json.dumps(out))
    f.r.delete(fleet.TASK)  # the durable result is all that is left
    client = NoShutdown(f.r)
    monkeypatch.setattr(fleet, 'connect', lambda cfg: client)
    args = SimpleNamespace(resume=str(f.run_dir), extend_hours=None, max_attempts=None, roster=None)
    with pytest.raises(SystemExit, match='no task record exists'):
        fleet.resume(f.run_dir, args)
    assert launched == [], 'the contradiction was spawned as a daemon: %r' % (launched,)
    assert result_json(f) == out, 'the durable result was deleted before the contradiction was refused'
    assert not client.shut, 'Redis was shut down by a refused resume'


def test_resume_run_refuses_an_empty_task_contradicted_by_the_durable_result(fx, table):
    """The daemon's second layer refuses the same contradiction before retiring or respawning any slot."""
    f = as_task(fx, 'smoke')
    fleet.save(f.path, dict(fleet.load(f.path), mode='task', lifecycle='smoke'))
    pre_ready_placeholder(f)
    out = result_json(f)
    out.update(resumable=True, status='blocked', task_id='', goal='a real goal', artifacts=[{'path': 'w/r.md'}])
    (f.run_dir / 'result.json').write_text(json.dumps(out))
    broken = fleet.Fleet(f.path, f.r, f.mgr)
    broken.r.delete(fleet.TASK)
    broken.resume_requested = True
    before = {s: (broken.agent(s)['state'], broken.agent(s)['pane']) for s in broken.names}
    with pytest.raises(SystemExit, match='no task record exists'):
        broken.v2.resume_run()
    after = {s: (broken.agent(s)['state'], broken.agent(s)['pane']) for s in broken.names}
    assert after == before, 'the contradiction retired or respawned slots: %r -> %r' % (before, after)
    assert result_json(f) == out
    broken.v2.close()


HEALTHY_CLOSE = Path(__file__).parent / 'fixtures' / 'restore04-199-healthy-closed-idle.json'


def healthy_closed_idle():
    """The 199 native 11/13 success path (read-only evidence copied verbatim into tests/fixtures): a healthy
    task-mode run that sat idle and was closed from the fleetmux sidebar, so it never submitted a task."""
    return json.loads(HEALTHY_CLOSE.read_text())['result']


def test_classify_accepts_the_199_healthy_closed_idle_shape_as_none(fx, table):
    """A complete, self-consistent sidebar close of an idle task-mode run is a legal 'none' even though its reason
    is not a startup failure; anything contradictory or malformed around it still fails closed."""
    result = healthy_closed_idle()
    assert result['task_id'] == '' and result['goal'] == '' and result['artifacts'] == []
    assert lifecycle_hold.closed_idle(result) is True
    assert lifecycle_hold.never_submitted({}, result) is True
    assert lifecycle_hold.classify({}, result) == 'none'
    for bad in ({'goal': 'x'}, {'task_id': 't1'}, {'artifacts': [{'path': 'a'}]}, {'artifacts': '{}'},
                {'deadline': '1'}, {'closed_via': 'user'}, {'closed_by': 'timeout'}, {'status': 'blocked'},
                {'base_mode': 'service'}, {'resumable': False}, {'closed_at': None}, {'at': 'soon'},
                {'reason': ''}):
        with pytest.raises(ValueError):
            lifecycle_hold.classify({}, {**result, **bad})
    # a closed result that is neither the strict placeholder nor this healthy shape is still refused
    with pytest.raises(ValueError, match='refusing to guess'):
        lifecycle_hold.classify({}, {'resumable': True, 'status': 'blocked', 'reason': 'x'})


def test_resume_run_of_the_healthy_closed_idle_shape_clears_it_and_takes_a_task(fx, table):
    """The 199 success path resumes like a healthy empty run: unpaused, closed result cleared (kept as evidence),
    no task invented, and the operator can submit a real task afterwards."""
    f = as_task(fx, 'smoke')
    fleet.save(f.path, dict(fleet.load(f.path), mode='task', lifecycle='smoke'))
    result = healthy_closed_idle()
    (f.run_dir / 'result.json').write_text(json.dumps(result))
    again = fleet.Fleet(f.path, f.r, f.mgr)
    again.mgr.dock_control = lambda pane: True
    assert again.task() == {}
    again.r.hset(fleet.SERVICE, mapping={'paused': 1, 'reason': 'same-run daemon restart recovered held'})
    again.resume_requested = True
    again.v2.resume_run()
    try:
        assert not again.paused(), 'the healthy close left the run paused'
        assert again.task() == {}, 'a task was invented for the idle run: %r' % (again.task(),)
        assert not (f.run_dir / 'result.json').exists(), 'the closed result was left behind'
        assert json.loads((f.run_dir / 'result.closed.json').read_text()) == result, 'evidence lost'
        assert all(again.agent(s)['pane'] for s in again.names)
        again.submit({'goal': 'next', 'hours': 1})
        assert again.task()['id'].startswith('task-') and float(again.task()['deadline']) > time.time()
    finally:
        again.v2.close()


def test_public_resume_of_the_healthy_closed_idle_shape_starts_the_daemon(fx, table, notes, monkeypatch, tmp_path):
    """The public layer accepts the healthy close (nothing to reopen, no fake task) and keeps the evidence until
    the daemon clears it."""
    from types import SimpleNamespace
    from fleetmux import cmux_manager
    launched = []
    monkeypatch.setattr(fleet, 'manager_for', lambda cfg: SimpleNamespace(
        spawn_window=lambda *a, **k: launched.append(a) or SimpleNamespace(success=True)))
    monkeypatch.setattr(fleet, 'LATEST', tmp_path / 'latest.json')
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: True)
    f = as_task(fx, 'smoke')
    fleet.save(f.path, dict(fleet.load(f.path), mode='task', lifecycle='smoke'))
    result = healthy_closed_idle()
    (f.run_dir / 'result.json').write_text(json.dumps(result))
    client = NoShutdown(f.r)
    monkeypatch.setattr(fleet, 'connect', lambda cfg: client)
    args = SimpleNamespace(resume=str(f.run_dir), extend_hours=None, max_attempts=None, roster=None)
    assert fleet.resume(f.run_dir, args) is True and len(launched) == 1
    assert f.task() == {}, 'the healthy close was given a task: %r' % (f.task(),)
    assert result_json(f) == result, 'the evidence was deleted by the public layer'
    assert not client.shut


def test_public_resume_refuses_an_unreadable_closed_result(fx, table, notes, monkeypatch, tmp_path):
    """A result.json that exists but cannot be parsed is not an empty one: refuse before deleting or spawning."""
    from types import SimpleNamespace
    from fleetmux import cmux_manager
    launched = []
    monkeypatch.setattr(fleet, 'manager_for', lambda cfg: SimpleNamespace(
        spawn_window=lambda *a, **k: launched.append(a) or SimpleNamespace(success=True)))
    monkeypatch.setattr(fleet, 'LATEST', tmp_path / 'latest.json')
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: True)
    f = as_task(fx, 'smoke')
    fleet.save(f.path, dict(fleet.load(f.path), mode='task', lifecycle='smoke'))
    (f.run_dir / 'result.json').write_text('{not json')
    client = NoShutdown(f.r)
    monkeypatch.setattr(fleet, 'connect', lambda cfg: client)
    args = SimpleNamespace(resume=str(f.run_dir), extend_hours=None, max_attempts=None, roster=None)
    with pytest.raises(SystemExit, match='cannot be read'):
        fleet.resume(f.run_dir, args)
    assert launched == [] and not client.shut
    assert (f.run_dir / 'result.json').read_text() == '{not json'


def test_brief_refuses_a_task_without_an_id(fx, table):
    """The old KeyError ('id') cannot come back: brief() says what is wrong instead."""
    f = as_task(fx, 'smoke')
    with pytest.raises(ValueError, match='task record has no id'):
        f.v2.brief([], {})


# ------------------------------------------------------------------ S2: the sidebar's resumable close
def test_sidebar_close_then_resume_reopens_the_run(fx, table, notes):
    """A close from the fleetmux sidebar keeps the run resumable; start --resume picks it up again."""
    f = as_task(fx, 'dev_task')
    f.mgr.has_cli_process = lambda pane, names=(): pane in f.mgr.alive
    f.mgr.dock_control = lambda pane: True
    submit(f, {'goal': 'x', 'hours': 4, 'deliver': {
        'repo': '/r', 'feature_branch': 'f', 'deploy_branch': 'm', 'test_cmd': ['true'], 'queue': '/q',
        'review_file': 'v.json'}})
    lifecycle_close.request_close(f, 'user', 'fleetmux 侧栏「关闭整个 fleet」', resumable=True,
                                  via='fleetmux-sidebar')
    assert f.stopping and f.explicit_stop, 'request_close must drive the explicit-stop tail'
    stop_tail(f)
    out = result_json(f)
    assert out['resumable'] is True, out
    assert out['closed_by'] == 'user', out
    assert out['closed_via'] == 'fleetmux-sidebar', out
    assert fleet.resume_target(str(f.run_dir)) == f.run_dir.resolve(), \
        'a sidebar-closed run must pass start --resume validation: %r' % (out,)
    assert fleet.clear_resumable_result(f.run_dir, f.task()) is True, \
        'the sidebar-closed result.json must be dropped before the daemon starts: %r' % (out,)
    assert not (f.run_dir / 'result.json').exists(), 'a stale result.json still refuses start --resume'
    import inspect
    assert 'clear_resumable_result(' in inspect.getsource(fleet.resume), \
        'fleet.resume must call clear_resumable_result before it spawns the daemon'
    again = fleet.Fleet(f.path, f.r, f.mgr)
    again.resume_requested = True
    again.v2.resume_run()
    try:
        assert again.history_pane, 'the no-task resume did not run the start initialization (no history page)'
        assert all(again.agent(s)['pane'] for s in again.names), [(s, again.agent(s)['state']) for s in again.names]
    finally:
        again.v2.close()


# ------------------------------------------------------------------ S2b: the closer keeps the fleetmux server for ↺
class KeptRegistry:
    """A fleetmux manager whose server survives the close: panes die, kv gets the resume command."""

    def __init__(self, meta):
        self.meta = meta
        self.meta_path = Path('/nonexistent/registry.json')
        self.kv, self.writes = {}, []
        self.marked = None
        self.panes_closed = self.server_ended = False

    def _meta(self):
        return self.meta

    def _surfaces(self, ws):
        return {}

    def session_exists(self):
        return False

    def kv_get(self, key):
        return self.kv.get(key)

    def _kv_set(self, key, value):
        self.kv[key] = value
        self.writes.append((key, value))
        return True

    def close_panes(self):
        self.panes_closed = True
        return True

    def mark_closed(self, info):
        self.marked = info
        return self._kv_set('restorable', json.dumps({'v': 1, 'runs': [info]}, ensure_ascii=False))

    def close_all(self):
        self.server_ended = True
        return True

    def end_server(self):
        self.server_ended = True
        return True


SIDEBAR_CLOSED_RUN = 'fleet-20261030-010101-ab12cd34'


def sidebar_closed_run(tmp_path, monkeypatch, **result):
    """A run the fleetmux sidebar closed (resumable), its config.json, and the fake manager on it."""
    run = tmp_path / 'runs' / SIDEBAR_CLOSED_RUN
    run.mkdir(parents=True)
    monkeypatch.setenv('FLEETMUX_BIN', '/opt/fleetmux')
    monkeypatch.setenv('FLEETMUX_THEME', str(tmp_path / 'theme.toml'))
    monkeypatch.setenv('PATH', '/usr/bin:/bin')
    monkeypatch.delenv('FLEETMUX_CONFIG', raising=False)
    fleet.save(run / 'config.json', {
        'run': str(run), 'python': '/py/bin/python', 'backend': 'fleetmux', 'name': 'fmx-demo',
        'fleet_home': str(tmp_path / 'home'), 'cmux_state_dir': str(tmp_path / 'state'),
        'private_config': 'none'})
    fleet.save(run / 'result.json', dict({
        'status': 'running', 'goal': 'ship it', 'resumable': True, 'closed_by': 'user',
        'closed_via': 'fleetmux-sidebar', 'closed_at': 1790000000.5}, **result))
    mgr = KeptRegistry({'run': str(run), 'surfaces': {}})
    mgr.kv['sidebar'] = json.dumps({'v': 1, 'current': run.name, 'fleets': [
        {'id': run.name, 'title': 'fmx-demo', 'state': 'stopping',
         'line1': '正在关闭 · 监工+1 worker（1忙）', 'line2': '~/.cmux-fleet'},
        {'id': 'fleet-child-a', 'title': 'child', 'state': 'running', 'line1': '运行中', 'line2': '~'}]},
        ensure_ascii=False)
    mgr.kv['stop_request'] = json.dumps({'v': 1, 'state': 'accepted'})
    return run, mgr


def test_sidebar_close_keeps_the_fleetmux_server_and_registers_the_resume_command(tmp_path, monkeypatch):
    run, mgr = sidebar_closed_run(tmp_path, monkeypatch)
    assert lifecycle_close.close_workspace(run / 'config.json', mgr=mgr) == 'closed (server kept for ↺)', \
        'a sidebar-closed fleetmux run must keep its server: %r' % (mgr.writes,)
    assert mgr.panes_closed, 'the closer must kill this run\'s panes (close_panes)'
    assert not mgr.server_ended, 'end_server must not run: the ↺ button needs the server: %r' % (mgr.writes,)
    assert mgr.marked == {
        'id': SIDEBAR_CLOSED_RUN, 'title': 'fmx-demo', 'closed_at': 1790000000,
        'argv': ['/py/bin/python', str(Path(fleet.__file__).resolve()), 'start', '--resume', str(run)],
        'cwd': str(run),
        'env': {'FLEETMUX_HOME': str(tmp_path / 'home'), 'FLEETMUX_STATE': str(tmp_path / 'state'),
                'FLEETMUX_BIN': '/opt/fleetmux',
                'FLEETMUX_THEME': str(tmp_path / 'theme.toml'), 'PATH': '/usr/bin:/bin'},
    }, 'kv restorable must carry the exact start --resume argv/cwd/env: %r' % (mgr.marked,)
    assert json.loads(mgr.kv['restorable']) == {'v': 1, 'runs': [mgr.marked]}, mgr.kv['restorable']
    assert mgr.kv['stop_request'] == '', 'the stale stop_request must be cleared: %r' % (mgr.kv['stop_request'],)
    card = json.loads(mgr.kv['sidebar'])['fleets'][0]
    assert (card['state'], card['line1']) == ('closed', '已关闭 · 可恢复'), card
    assert json.loads(mgr.kv['sidebar'])['fleets'][1]['state'] == 'running', 'other cards stay as they were'
    assert fleet.load(run / 'result.json')['workspace'] == 'closed (server kept for ↺)', \
        fleet.load(run / 'result.json')


def test_normal_close_still_ends_the_server(tmp_path, monkeypatch):
    run, mgr = sidebar_closed_run(tmp_path, monkeypatch, closed_via=None, resumable=False)
    assert lifecycle_close.close_workspace(run / 'config.json', mgr=mgr) == 'closed', mgr.writes
    assert mgr.server_ended, 'a normal close still stops the fleetmux server'
    assert 'restorable' not in mgr.kv, 'only a sidebar close registers a resume command: %r' % (mgr.writes,)



def test_close_outcome_names_the_bash_pages_kept_busy(tmp_path, monkeypatch):
    """Idle bash pages go with the fleet; the outcome line names the busy ones the close kept (user 2026-11-08)."""
    run, mgr = sidebar_closed_run(tmp_path, monkeypatch)
    mgr.kept_bash_pages = ['bash-2']
    assert lifecycle_close.close_workspace(run / 'config.json', mgr=mgr) == (
        'closed (server kept for ↺); bash tabs kept, a program runs: bash-2'), mgr.writes
    run, mgr = sidebar_closed_run(tmp_path / 'n', monkeypatch, closed_via=None, resumable=False)
    mgr.kept_bash_pages = ['bash-1', 'bash-3']
    assert lifecycle_close.close_workspace(run / 'config.json', mgr=mgr) == (
        'closed; bash tabs kept, a program runs: bash-1, bash-3'), mgr.writes


# ------------------------------------------------------------------ S2c ②: the closer writes the whole history
def older_resumable_run(tmp_path, name='fleet-older-timeout', closed_at=1789000000):
    """A lifecycle-timeout run of this home: the closer must keep it in kv restorable (§18.6)."""
    run = tmp_path / 'home' / 'runs' / name
    run.mkdir(parents=True)
    fleet.save(run / 'config.json', {
        'run': str(run), 'python': '/py/bin/python', 'backend': 'fleetmux', 'name': 'fmx-older',
        'fleet_home': str(tmp_path / 'home'), 'cmux_state_dir': str(tmp_path / 'state'),
        'private_config': 'none'})
    fleet.save(run / 'result.json', {'status': 'budget_exhausted', 'goal': 'old', 'resumable': True,
                                     'closed_by': 'timeout', 'closed_at': closed_at})
    return run


def test_closer_registers_the_whole_history_with_the_just_closed_run(tmp_path, monkeypatch):
    """§18.2 / §18.6: the closer writes the just-closed run plus every older resumable run (timeout too),
    keeps the cards it already had (a still-running one untouched) and appends the rest as grey cards."""
    run, mgr = sidebar_closed_run(tmp_path, monkeypatch)
    older = older_resumable_run(tmp_path)
    older_resumable_run(tmp_path, name='fleet-child-a', closed_at=1788000000)  # the live child card's run

    assert lifecycle_close.close_workspace(run / 'config.json', mgr=mgr) == 'closed (server kept for ↺)', mgr.writes

    runs = json.loads(mgr.kv['restorable'])['runs']
    assert [r['id'] for r in runs] == [SIDEBAR_CLOSED_RUN, older.name, 'fleet-child-a'], \
        'kv restorable must list the just-closed run first, then the older runs newest first (§18.2 / §18.6): %r' % (runs,)
    assert runs[0]['argv'][-1] == str(run), 'the first entry is this run\'s start --resume: %r' % (runs[0],)
    assert mgr.marked['id'] == SIDEBAR_CLOSED_RUN, mgr.marked

    data = json.loads(mgr.kv['sidebar'])
    cards = data['fleets']
    assert [c['id'] for c in cards] == [SIDEBAR_CLOSED_RUN, 'fleet-child-a', older.name], \
        'existing cards stay in place and the history is appended (§13.6-1 / §18.2): %r' % (cards,)
    assert (cards[0]['state'], cards[0]['line1']) == ('closed', '已关闭 · 可恢复'), cards
    assert cards[1]['state'] == 'running', \
        'a card that still runs is never regreyed, even when the history lists its run: %r' % (cards,)
    assert (cards[2]['state'], cards[2]['line1']) == ('closed', '到点停 · 可恢复'), cards
    assert data['current'] == SIDEBAR_CLOSED_RUN, 'current is the run just closed (§18.2): %r' % (data,)
    assert mgr.kv['stop_request'] == '', '§13.6-7: a stale stop_request must be cleared: %r' % (mgr.kv['stop_request'],)
    assert not mgr.server_ended, 'the ↺ button needs the server (§13.5): %r' % (mgr.writes,)


def test_closer_caps_the_merged_sidebar_at_sixteen_cards(tmp_path, monkeypatch):
    """§18.3 cap 16: the closer fills up to 16 and never drops or regreys a running card to make room."""
    run, mgr = sidebar_closed_run(tmp_path, monkeypatch)
    older = older_resumable_run(tmp_path, name='fleet-older-a', closed_at=1789000000)
    too_old = older_resumable_run(tmp_path, name='fleet-older-b', closed_at=1788000000)
    fleets = [{'id': SIDEBAR_CLOSED_RUN, 'title': 'fmx-demo', 'state': 'stopping',
               'line1': '正在关闭 · 监工+1 worker（1忙）', 'line2': '~/.cmux-fleet'}]
    fleets += [{'id': 'fleet-c%d' % i, 'title': 'c%d' % i, 'state': 'running',
                'line1': '运行中 · 1 worker', 'line2': '~'} for i in range(14)]
    mgr.kv['sidebar'] = json.dumps({'v': 1, 'current': SIDEBAR_CLOSED_RUN, 'fleets': fleets}, ensure_ascii=False)

    assert lifecycle_close.close_workspace(run / 'config.json', mgr=mgr) == 'closed (server kept for ↺)', mgr.writes

    data = json.loads(mgr.kv['sidebar'])
    cards = data['fleets']
    assert len(cards) == 16, 'kv sidebar may hold at most 16 cards (§18.3): %d %r' % (len(cards), cards)
    assert [c['id'] for c in cards[:2]] == [SIDEBAR_CLOSED_RUN, 'fleet-c0'], cards
    assert cards[0]['state'] == 'closed', cards
    assert all(c['state'] == 'running' for c in cards[1:15]), \
        'the cap must not drop or regrey a running card to make room: %r' % (cards,)
    assert (cards[15]['id'], cards[15]['line1']) == (older.name, '到点停 · 可恢复'), \
        'the newest history card fills the last slot (§18.2): %r' % (cards,)
    assert too_old.name not in [c['id'] for c in cards], 'the 17th card does not fit: %r' % (cards,)
    assert [r['id'] for r in json.loads(mgr.kv['restorable'])['runs']] == [SIDEBAR_CLOSED_RUN, older.name, too_old.name], \
        'kv restorable still lists the whole history even when the sidebar is full: %r' % (mgr.kv['restorable'],)


def test_closer_writes_the_history_cards_when_the_sidebar_was_never_set(tmp_path, monkeypatch):
    """§18.2: the closer writes this run's card and the rest of the history into kv sidebar even when the
    daemon never pushed one (a server that died before its first sidebar tick) - the window must still
    show the history instead of an unset kv sidebar."""
    run, mgr = sidebar_closed_run(tmp_path, monkeypatch)
    older = older_resumable_run(tmp_path)
    mgr.kv.pop('sidebar')  # no daemon push ever landed in kv

    assert lifecycle_close.close_workspace(run / 'config.json', mgr=mgr) == 'closed (server kept for ↺)', mgr.writes

    assert 'sidebar' in mgr.kv, 'the closer must write kv sidebar even when it was never set (§18.2): %r' % (mgr.writes,)
    data = json.loads(mgr.kv['sidebar'])
    cards = data['fleets']
    assert [c['id'] for c in cards] == [SIDEBAR_CLOSED_RUN, older.name], \
        'kv sidebar must list the just-closed run, then the older history run (§18.2 / §18.6): %r' % (cards,)
    assert (cards[0]['state'], cards[0]['line1']) == ('closed', '已关闭 · 可恢复'), cards
    assert (cards[1]['state'], cards[1]['line1']) == ('closed', '到点停 · 可恢复'), cards
    assert data['current'] == SIDEBAR_CLOSED_RUN, 'current is the run just closed (§18.2): %r' % (data,)
    assert [r['id'] for r in json.loads(mgr.kv['restorable'])['runs']] == [SIDEBAR_CLOSED_RUN, older.name], \
        'kv restorable lists the same history newest first (§18.3): %r' % (mgr.kv['restorable'],)
    assert not mgr.server_ended, 'the ↺ button needs the server (§13.5): %r' % (mgr.writes,)


# ------------------------------------------------------------------ fix-m4-2 (brief-m4 §9 defect A): close the subtree's cards
def sub_record(parent_run, dirname, state, **extra):
    """A sub-fleet run dir ``<parent>/sub/<dirname>`` with its ``subfleet.json`` record and ``config.json``."""
    d = Path(parent_run) / 'sub' / dirname
    d.mkdir(parents=True)
    (d / 'subfleet.json').write_text(json.dumps(dict({'task_id': dirname, 'state': state, 'attempt': 1,
                                                       'slot': 'worker-1', 'workers': 1}, **extra)))
    fleet.save(d / 'config.json', {'run': str(d), 'backend': 'fleetmux', 'parent_config': str(parent_run / 'config.json')})
    return d


def card(cid, state, line1, **extra):
    return dict({'id': cid, 'title': cid, 'state': state, 'line1': line1, 'line2': '由 worker-1 委派'}, **extra)


def tree_closed_run(tmp_path, monkeypatch, **result):
    """A sidebar-closed run whose kv sidebar still shows its sub-fleet tree as live (the 07:1x Mac acceptance)."""
    run, mgr = sidebar_closed_run(tmp_path, monkeypatch, **result)
    sub_record(run, 'svc', 'running', named=True, name='svc', mode='service')
    inner = sub_record(run / 'sub' / 'svc', 'M0', 'running')
    assert inner.is_dir()
    sub_record(run, 'memx', 'running', named=True, name='memx', mode='service')
    sub_record(run, 'tgog', 'reclaimed', outcome='down', named=True, name='tgog', mode='service')
    sub_record(run, 'ops', 'running')
    mgr.kv['sidebar'] = json.dumps({'v': 1, 'current': run.name, 'fleets': [
        {'id': run.name, 'title': 'fmx-demo', 'state': 'stopping', 'page': '',
         'line1': '正在关闭 · 监工+3 worker（1忙）', 'line2': '~/.cmux-fleet'},
        card('svc', 'running', '运行中 · 1 worker', page='svc'),
        card('M0', 'stopping', '运行中 · 1 worker', page='M0'),
        card('memx', 'paused', '运行中 · 1 worker', page='memx'),
        card('tgog', 'reclaimed', '已收回 · 结局 down', page='tgog', line2='只读，可看记录'),
        card('ops', 'running', '运行中 · 1 worker', page='ops'),
        # not in this run's sub/ tree: another fleet's card, and an id that merely starts like a child's
        {'id': 'fleet-child-a', 'title': 'child', 'state': 'running', 'line1': '运行中', 'line2': '~'},
        {'id': 'svc-2', 'title': 'other', 'state': 'running', 'line1': '运行中 · 2 worker', 'line2': '~'},
    ]}, ensure_ascii=False)
    return run, mgr


def test_sidebar_close_takes_this_runs_subfleet_cards_out_of_running(tmp_path, monkeypatch):
    """§9 defect A: after a sidebar close the sub-fleet cards of this run's tree (running / paused / stopping,
    any depth) read 已收回 · 随根关闭 / 只读，可看记录 with a grey dot (state reclaimed, no ×, no ↺); a card already
    reclaimed keeps its text; cards outside the tree - by identity, not by an id prefix - stay as they were."""
    run, mgr = tree_closed_run(tmp_path, monkeypatch)
    assert lifecycle_close.close_workspace(run / 'config.json', mgr=mgr) == 'closed (server kept for ↺)', mgr.writes
    cards = {c['id']: c for c in json.loads(mgr.kv['sidebar'])['fleets']}
    assert (cards[run.name]['state'], cards[run.name]['line1']) == ('closed', '已关闭 · 可恢复'), cards[run.name]
    for cid in ('svc', 'M0', 'memx', 'ops'):
        c = cards[cid]
        assert (c['state'], c['line1'], c['line2']) == ('reclaimed', '已收回 · 随根关闭', '只读，可看记录'), \
            'a sub-fleet card of the closed tree must not stay running: %r' % (c,)
        assert c['page'] == cid, 'the card keeps its page (click still switches to the read-only page): %r' % (c,)
    assert (cards['tgog']['state'], cards['tgog']['line1']) == ('reclaimed', '已收回 · 结局 down'), \
        'an already reclaimed card keeps its own outcome text: %r' % (cards['tgog'],)
    assert (cards['fleet-child-a']['state'], cards['fleet-child-a']['line1']) == ('running', '运行中'), cards
    assert (cards['svc-2']['state'], cards['svc-2']['line1']) == ('running', '运行中 · 2 worker'), \
        'a card whose id only looks like a child id is not part of the tree: %r' % (cards['svc-2'],)


def test_sidebar_close_leaves_no_running_title_for_any_card_of_the_tree(tmp_path, monkeypatch):
    """§9 defect A: the title bar shows the current card's title + line1 (sidebar.rs draw_topbar,
    overlay::topbar_for), so no card of the closed tree may still say 运行中, whichever page is current."""
    run, mgr = tree_closed_run(tmp_path, monkeypatch)
    lifecycle_close.close_workspace(run / 'config.json', mgr=mgr)
    data = json.loads(mgr.kv['sidebar'])
    tree_ids = {run.name, 'svc', 'M0', 'memx', 'tgog', 'ops'}
    for c in data['fleets']:
        if c['id'] in tree_ids:
            assert '运行中' not in c['line1'] and c['state'] not in lifecycle_close.LIVE_CARD_STATES, \
                'the title bar would still say running for %r' % (c,)
    assert data['current'] == run.name, data


def test_resumed_daemon_push_brings_the_subfleet_cards_back_to_their_real_state(tmp_path, monkeypatch):
    """§9 defect A: the grey sub-fleet cards are only the closer's picture; after ↺ the root daemon's first push
    rebuilds every tree card from the sub-fleet records (a running child reads 运行中 again)."""
    from types import SimpleNamespace
    from fleetmux import fleetmux_sidebar
    run, mgr = tree_closed_run(tmp_path, monkeypatch)
    lifecycle_close.close_workspace(run / 'config.json', mgr=mgr)
    assert json.loads(mgr.kv['sidebar'])['fleets'][1]['state'] == 'reclaimed'
    cfg = fleet.load(run / 'config.json')
    f = SimpleNamespace(cfg=cfg, workers=['worker-1'], stopping=False, service=False, mgr=mgr,
                        agent=lambda name: {'state': 'idle'}, task=lambda: {}, paused=lambda: False)
    fleetmux_sidebar.tick(f, now=100.0)
    cards = {c['id']: c for c in json.loads(mgr.kv['sidebar'])['fleets']}
    assert cards[run.name]['state'] == 'running', cards[run.name]
    for cid in ('svc', 'M0', 'memx', 'ops'):
        assert (cards[cid]['state'], cards[cid]['line1']) == ('running', '运行中 · 1 worker'), cards[cid]
    assert (cards['tgog']['state'], cards['tgog']['line1']) == ('reclaimed', '已收回 · 结局 down'), cards['tgog']


# ------------------------------------------------------------------ close_all keeps the server for a busy bash page
# (2026-11-08): no daemon pushes kv sidebar any more, so the closer itself must take the run's cards out of the live
# states, or the closed-fleet reaper (§13.4) never lets the server go once the bash page is closed too.
def _tree_states(mgr, run):
    cards = {c['id']: c for c in json.loads(mgr.kv['sidebar'])['fleets']}
    return cards, {cid: cards[cid]['state'] for cid in (run.name, 'svc', 'M0', 'memx', 'tgog', 'ops')}


def test_close_all_keeping_a_busy_bash_page_stops_the_cards_of_a_plain_stop(tmp_path, monkeypatch):
    """``fleet.sh stop`` (closed_by user, not resumable): the root card turns ``stopped`` / ``已停止`` (grey, no ×,
    no ↺); the tree's live sub-fleet cards turn reclaimed as on the sidebar path; other fleets' cards stay."""
    run, mgr = tree_closed_run(tmp_path, monkeypatch, closed_via=None, resumable=False)
    mgr.kept_bash_pages = ['bash-2']
    assert lifecycle_close.close_workspace(run / 'config.json', mgr=mgr) == (
        'closed; bash tabs kept, a program runs: bash-2'), mgr.writes
    cards, states = _tree_states(mgr, run)
    assert not set(states.values()) & set(lifecycle_close.LIVE_CARD_STATES), states
    root = cards[run.name]
    assert (root['state'], root['line1']) == ('stopped', '已停止'), root
    assert 'restorable' not in mgr.kv, 'a plain stop is not a ↺ history entry: %r' % (mgr.writes,)
    for cid in ('svc', 'M0', 'memx', 'ops'):
        assert (cards[cid]['state'], cards[cid]['line1']) == ('reclaimed', '已收回 · 随根关闭'), cards[cid]
    assert cards['fleet-child-a']['state'] == 'running', cards['fleet-child-a']


def test_close_all_keeping_a_busy_bash_page_marks_a_timeout_run_closed_for_resume(tmp_path, monkeypatch):
    """A lifecycle timeout close (closed_by timeout, resumable) is a ↺ history entry (§18.6): the root card turns
    ``closed`` / ``到点停 · 可恢复`` and kv restorable gets its resume command, as the sidebar path does."""
    run, mgr = tree_closed_run(tmp_path, monkeypatch, closed_via=None, closed_by='timeout', resumable=True)
    mgr.kept_bash_pages = ['bash-2']
    assert lifecycle_close.close_workspace(run / 'config.json', mgr=mgr) == (
        'closed; bash tabs kept, a program runs: bash-2'), mgr.writes
    cards, states = _tree_states(mgr, run)
    assert not set(states.values()) & set(lifecycle_close.LIVE_CARD_STATES), states
    assert (cards[run.name]['state'], cards[run.name]['line1']) == ('closed', '到点停 · 可恢复'), cards[run.name]
    runs = json.loads(mgr.kv['restorable'])['runs']
    assert runs and runs[0]['id'] == run.name and runs[0]['argv'][-2:] == ['--resume', str(run)], runs


def test_close_all_that_ends_the_server_leaves_the_sidebar_alone(tmp_path, monkeypatch):
    """No bash page kept: the server is gone with the close, so nothing is written to kv sidebar."""
    run, mgr = tree_closed_run(tmp_path, monkeypatch, closed_via=None, resumable=False)
    before = mgr.kv['sidebar']
    assert lifecycle_close.close_workspace(run / 'config.json', mgr=mgr) == 'closed', mgr.writes
    assert mgr.kv['sidebar'] == before and not mgr.writes, mgr.writes
