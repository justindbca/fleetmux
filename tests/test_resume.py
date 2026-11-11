"""start --resume (docs/design.md §17): reattach an unfinished v2 run, replay lost work once.

Task cases: Codex supervisor + official Claude workers through the real hook entry points,
on a project-style private Redis with AOF that is SIGKILLed and restarted from its directory.
Service cases: all-Pi with the bridge simulated by events (as tests/test_runtime_v2.py).
Fake cmux, fake CLIs: this proves the reconcile wiring, not a real TUI.
"""
import fcntl
import json
import os
import re
from pathlib import Path
import shutil
import signal
import sys
import time
import uuid

import pytest

import hook_sim
from test_fleet import FakeMgr
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import event, fx, receive, result, session  # noqa: F401  (fx fixture)
from test_fleet import codex_rows, write_rollout
from test_v2_codex_claude import drive, typed
from fleetmux import cmux_manager, fleet, redis_runtime, runtime_v2


class ResumeMgr(FakeMgr):
    """FakeMgr plus the two calls resume makes; `no_cli` panes are open but show only a shell."""
    def __init__(self):
        super().__init__()
        self.no_cli, self.docked = set(), []

    def has_cli_process(self, pane, names=('codex',)):
        return pane in self.alive and pane not in self.no_cli

    def tab_panes(self):
        return {}

    def dock_control(self, pane):
        self.docked.append(pane)
        return True


# ------------------------------------------------------------------ task: kill daemon + Redis
@pytest.fixture
def managed(tmp_path):
    binary = shutil.which('redis-server')
    if binary is None:
        pytest.skip('redis-server unavailable')
    root = Path('/tmp/cfr-' + uuid.uuid4().hex[:8])  # short: macOS Unix socket path limit
    rcfg = dict(redis_runtime.prepare(root, uuid.uuid4().hex[:8]), redis_server=binary, transport_version=2)
    clients = [fleet.start_redis(rcfg)]
    try:
        yield rcfg, clients
    finally:
        for c in clients:
            c.close(); c.connection_pool.disconnect()
        pid_file = Path(rcfg['redis_runtime_dir']) / 'redis.pid'
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass
        time.sleep(.2)
        shutil.rmtree(root, ignore_errors=True)


def kill_redis(rcfg):
    """Crash the private Redis (no SHUTDOWN): only what AOF fsynced survives."""
    time.sleep(1.2)  # appendfsync everysec
    pid = int((Path(rcfg['redis_runtime_dir']) / 'redis.pid').read_text())
    os.kill(pid, signal.SIGKILL)
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(.05)
    pytest.fail('redis did not die')


@pytest.fixture
def task_run(managed, tmp_path, monkeypatch):
    rcfg, clients = managed
    monkeypatch.setattr(fleet, 'trust_claude_dir', lambda cwd: None)  # never touch ~/.claude.json
    run = tmp_path / 'run'
    (run / 'workspace').mkdir(parents=True)
    cfg = dict(rcfg, run=str(run), python=sys.executable, workers=['worker-1', 'worker-2'], mode='task',
               supervisor_cli='codex', worker_cli='claude', codex_home=str(tmp_path / 'codex-home'),
               allow_development_v2=True, stall_timeout=0, v2_lease_ms=300000, max_busy=0, max_attempts=2)
    fleet.save(run / 'config.json', cfg)
    state = dict(rcfg=rcfg, clients=clients, path=run / 'config.json', mgr=ResumeMgr(),
                 sessions=tmp_path / 'codex-home' / 'sessions', claude=tmp_path / 'claude-projects',
                 threads={s: str(uuid.uuid4()) for s in ['supervisor', 'worker-1', 'worker-2']})
    f = new_fleet(state, clients[0])
    f.start_all()
    f.r.set(f.v2.prefix + ':boot', 'running')
    for slot in f.names:
        assert answer_turn(f, slot, 'READY', startup=True)['status'] == 'ready'
    state['f'] = f
    yield state
    state['f'].v2.close()


def new_fleet(state, r):
    f = fleet.Fleet(state['path'], r, state['mgr'], sessions=state['sessions'])
    f.test_sessions, f.test_claude, f.test_threads = state['sessions'], state['claude'], state['threads']
    return f


def answer_turn(f, slot, text, *, startup=False, prompt=None):
    """Fire the slot's real hook (Codex notify / Claude Stop) for a finished turn of its current
    generation. Each generation has its own session UUID (Codex rollout lookup needs a UUID)."""
    gen = int(f.agent(slot)['generation'])
    thread = f.test_threads.setdefault((slot, gen), str(uuid.uuid4())) if gen else f.test_threads[slot]
    prompt = prompt if prompt is not None else ('startup prompt' if startup else typed(f, slot)[-1])
    if slot == 'supervisor':
        turn_id, rows = codex_rows(thread, prompt, text)  # the queued delivery's own turn, or a typed one
        write_rollout(f.test_sessions, thread, fleet.agent_cwd(f.cfg, slot), rows=rows)
        payload = json.dumps({'type': 'agent-turn-complete', 'thread-id': thread, 'turn-id': turn_id,
                              'input-messages': [prompt], 'last-assistant-message': text})
        return drive(f, lambda: fleet.notify(f.path, slot, str(gen), payload, f.test_sessions))
    transcript = f.test_claude / slot / (thread + '.jsonl')
    transcript.parent.mkdir(parents=True, exist_ok=True)
    rows = [hook_sim.user_row(prompt, hook_sim.was_handed(slot, prompt)),
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}}]
    transcript.write_text('\n'.join(json.dumps(x) for x in rows) + '\n')
    raw = json.dumps({'session_id': thread, 'transcript_path': str(transcript), 'last_assistant_message': text})
    return drive(f, lambda: hook_sim.claude_stop(f.path, slot, str(gen), raw))


REPORT = 'Done:\n- wrote {0}\nEvidence:\n- {0}\nNot done: none\nBlocked: none'


def half_done(state):
    """worker-1 finished a.md and reported; worker-2 is still working on b.md."""
    f = state['f']
    runtime_v2.post_event(f.v2.bus, dict(kind='task', request_id='new-task', spec={
        'goal': 'write two parts', 'artifacts': [{'path': 'worker-1/a.md'}, {'path': 'worker-2/b.md'}]}), 'new-task')
    assert f.v2.step(10)
    f.pump()
    plan = {'status': 'RUNNING', 'completion': 10, 'reason': 'split',
            'dispatch': [{'to': 'worker-1', 'instruction': 'Write worker-1/a.md with part A.'},
                         {'to': 'worker-2', 'instruction': 'Write worker-2/b.md with part B.'}]}
    assert answer_turn(f, 'supervisor', json.dumps(plan))['status'] == 'committed'
    f.pump()
    (f.workspace() / 'worker-1').mkdir(exist_ok=True)
    (f.workspace() / 'worker-1' / 'a.md').write_text('part A')
    assert answer_turn(f, 'worker-1', REPORT.format('worker-1/a.md'))['status'] == 'committed'
    f.pump()  # the report is now in flight to the supervisor
    assert f.v2.bus.slot_state('worker-2')['status'] == 'received'
    assert f.v2.bus.slot_state('supervisor')['status'] == 'received'


def crash_and_resume(state, **cfg):
    """Daemon and Redis die; start --resume restarts Redis from its directory and reconciles."""
    state['f'].v2.close()
    kill_redis(state['rcfg'])
    r = fleet.start_redis(state['rcfg'], tries=300)
    state['clients'].append(r)
    if cfg:
        fleet.save(state['path'], dict(fleet.load(state['path']), **cfg))
    f = new_fleet(state, r)
    f.resume_requested = True
    f.v2.resume_run()
    state['f'] = f
    return f


def test_aof_restart_resumes_task_redoes_only_the_lost_dispatch_and_writes_result(task_run):
    state = task_run
    half_done(state)
    mgr = state['mgr']
    old_sup, old_w2 = state['f'].agent('supervisor')['pane'], state['f'].agent('worker-2')['pane']
    mgr.alive.discard(old_w2)  # worker-2's tab is gone
    w1_typed = len(typed(state['f'], 'worker-1'))
    f = crash_and_resume(state)
    assert f.task()['status'] == 'running'  # survived SIGKILL through the AOF
    assert f.agent('supervisor')['label'] == 'supervisor-1.1' and old_sup in mgr.killed
    assert f.agent('worker-2')['label'] == 'worker-2.1' and f.agent('worker-1')['generation'] == '0'
    assert f.resolve('worker-2') == 'worker-2'  # the old name still routes to the slot
    assert not f.paused()
    for slot in ('supervisor', 'worker-2'):
        assert answer_turn(f, slot, 'READY', startup=True)['status'] == 'ready'
    f.pump()
    brief = typed(f, 'supervisor')[-1]
    assert brief.startswith('[Resumed task ')
    assert 'verified on disk: ["worker-1/a.md"]' in brief and 'worker-2/b.md (artifact not written' in brief
    assert 'Write worker-1/a.md' in brief and 'wrote worker-1/a.md' in brief  # completed + report excerpt
    assert 'worker-2.1 (attempt 2 of 2): [M1] Write worker-2/b.md' in brief  # §44 auto id
    assert len(typed(f, 'worker-1')) == w1_typed  # verified work is not redone
    again, = typed(f, 'worker-2')  # the new generation's pane got the lost dispatch exactly once
    assert 'Write worker-2/b.md' in again and '[Supervisor instruction' not in again
    assert answer_turn(f, 'supervisor', json.dumps({'status': 'RUNNING', 'completion': 50,
                                                    'dispatch': []}))['status'] == 'committed'
    (f.workspace() / 'worker-2').mkdir(exist_ok=True)
    (f.workspace() / 'worker-2' / 'b.md').write_text('part B')
    assert answer_turn(f, 'worker-2', REPORT.format('worker-2/b.md'))['status'] == 'committed'
    f.pump()
    assert typed(f, 'supervisor')[-1].startswith('[worker-2.1 report (took over from worker-2)]')
    assert answer_turn(f, 'supervisor', json.dumps({'status': 'COMPLETED', 'completion': 100,
                                                    'reason': 'both parts'}))['status'] == 'committed'
    assert f.task()['status'] == 'completed'
    f.tick()
    out = json.loads((f.run_dir / 'result.json').read_text())
    assert out['status'] == 'completed' and out['task_id'] == f.task()['id']
    assert [(a['path'], a['problems']) for a in out['artifacts']] == [('worker-1/a.md', []), ('worker-2/b.md', [])]


def test_resume_reattaches_idle_hook_worker_and_replaces_in_flight_one(task_run):
    state = task_run
    half_done(state)
    mgr = state['mgr']
    w1, w2 = state['f'].agent('worker-1')['pane'], state['f'].agent('worker-2')['pane']
    spawned = len(mgr.spawned)
    f = crash_and_resume(state)
    # Idle Claude worker: reattached as it is; its Stop hook keeps waiting (an Escape would kill it).
    assert f.agent('worker-1')['pane'] == w1 and f.agent('worker-1')['generation'] == '0'
    assert w1 not in mgr.killed and (w1, 'Escape') not in mgr.keys
    # In-flight Claude worker: aborting its turn with Escape would also kill the waiting hook, so the
    # hook channel replaces it with the next generation instead (RuntimeV2.reattachable).
    assert w2 in mgr.killed and (w2, 'Escape') not in mgr.keys
    assert f.agent('worker-2')['generation'] == '1' and f.agent('worker-2')['state'] == 'starting'
    assert len(mgr.spawned) == spawned + 2  # the supervisor's and worker-2's next generations
    assert answer_turn(f, 'worker-2', 'READY', startup=True)['status'] == 'ready'
    f.pump()  # the lost dispatch is redone through worker-2.1's Stop hook, never typed
    assert [t for t in typed(f, 'worker-2') if 'Write worker-2/b.md' in t].__len__() == 1
    assert not [t for p, t in mgr.sent if p in (w2, f.agent('worker-2')['pane'])]
    assert answer_turn(f, 'worker-2', REPORT.format('worker-2/b.md'))['status'] == 'committed'


def test_attempt_cap_blocks_with_the_lost_dispatch_named(task_run):
    state = task_run
    half_done(state)
    state['mgr'].alive.discard(state['f'].agent('worker-2')['pane'])
    f = crash_and_resume(state)
    for slot in ('supervisor', 'worker-2'):
        answer_turn(f, slot, 'READY', startup=True)
    f.pump()  # attempt 2 of worker-2's dispatch is in flight again
    assert f.v2.bus.slot_state('worker-2')['status'] == 'received'
    f = crash_and_resume(state)
    task = f.task()
    assert task['status'] == 'blocked'
    # The label carries the generation: each resume of an in-flight hook worker starts a new one.
    assert re.search(r'worker-2\.\d+', task['reason']) and 'max-attempts 2' in task['reason'] \
        and 'worker-2/b.md' in task['reason']
    f.tick()
    assert json.loads((f.run_dir / 'result.json').read_text())['status'] == 'blocked'


def test_max_attempts_override_on_resume_allows_another_replay(task_run):
    state = task_run
    half_done(state)
    state['mgr'].alive.discard(state['f'].agent('worker-2')['pane'])
    f = crash_and_resume(state)
    for slot in ('supervisor', 'worker-2'):
        answer_turn(f, slot, 'READY', startup=True)
    f.pump()
    f = crash_and_resume(state, max_attempts=3)
    assert f.task()['status'] == 'running'
    assert 'attempt 3 of 3' in f.v2.pending_text('supervisor')[0]


def test_plain_restart_without_resume_still_holds_and_never_replays(task_run):
    state = task_run
    half_done(state)
    state['f'].v2.close()
    f = new_fleet(state, state['clients'][0])
    spawned = len(state['mgr'].spawned)
    f.stopping = True
    f.run()  # resume_requested is False: the original recover() path
    assert f.paused() and f.agent('worker-2')['state'] == 'held'
    assert len(state['mgr'].spawned) == spawned
    assert f.v2.bus.slot_state('worker-2')['status'] == 'uncertain'
    state['f'] = f


# ------------------------------------------------------------------ service (all-Pi)
def ready(f, slot):
    md = session(f, slot)
    event(f, dict(md, kind='bridge_session', request_id='reg-' + uuid.uuid4().hex))
    f.v2.bus.register_slot(slot, int(f.agent(slot)['generation']), md['thread'])
    event(f, dict(md, kind='startup', request_id='rdy-' + uuid.uuid4().hex, text='READY', failed=False, aborted=False))
    event(f, dict(md, kind='availability', idle=True))


def settle_ok(f, slot, env):
    event(f, dict(session(f, slot), kind='settled', delivery=env, failed=False, aborted=False, unfinished=False,
                  candidates=[{'tool': 'fleet_result', 'value': {'reply': 'OK ' + env['request_id']}}]))
    event(f, dict(session(f, slot), kind='availability', idle=True))


def submit(f, rid, to, **extra):
    spec = dict(id=rid, to=to, payload={'message': 'job ' + rid}, reply={'required': ['reply']}, **extra)
    event(f, dict(kind='request', request_id='cmd-' + rid, spec=spec))


def resumed(f):
    f.v2.close()
    again = fleet.Fleet(f.path, f.r, f.mgr)
    again.resume_requested = True
    again.v2.resume_run()
    return again


def result_ids(f):
    return [json.loads(raw)['id'] for raw in f.r.lrange(fleet.RESULTS, 0, -1)]


@pytest.fixture
def service(fx):  # noqa: F811
    f = fx
    f.cfg['max_attempts'] = 2
    f.mgr.__class__, f.mgr.no_cli, f.mgr.docked = ResumeMgr, set(), []  # same panes, resume calls added
    submit(f, 'r1', 'worker-1'); f.pump(); settle_ok(f, 'worker-1', receive(f, 'worker-1'))
    submit(f, 'r2', 'worker-1'); f.pump(); receive(f, 'worker-1')   # in flight
    submit(f, 'r3', 'worker-1')                                     # queued behind r2
    submit(f, 'r4', 'worker-2', replay=False); f.pump(); receive(f, 'worker-2')
    assert result(f, 'r1')['status'] == 'ok'
    return f


def test_service_resume_requeues_only_requests_without_results(service):
    f = resumed(service)
    try:
        assert result_ids(f) == ['r1', 'r4']
        assert result(f, 'r4')['status'] == 'timeout' and 'not replayed on resume' in result(f, 'r4')['error']
        queued = [f.v2.bus.message(m)['request_id'] for m in f.v2.bus.pending('worker-1')]
        assert queued == ['r2', 'r3'] and not f.v2.bus.pending('worker-2')
        assert f.r.hget(fleet.REQ + 'r2', 'replay') == '2'
        assert all(f.agent(s)['generation'] == '1' for s in f.names)  # Pi never reattaches
        for slot in f.names:
            ready(f, slot)
        for rid in ('r2', 'r3'):
            f.pump()
            env = receive(f, 'worker-1')
            assert env['request_id'] == rid
            settle_ok(f, 'worker-1', env)
        assert sorted(result_ids(f)) == ['r1', 'r2', 'r3', 'r4']  # one result per id
        assert result(f, 'r2')['result'] == {'reply': 'OK r2'}
    finally:
        f.v2.close()


def test_service_attempt_cap_publishes_timeout_with_reason(service):
    f = resumed(service)
    f = resumed(f)
    try:
        for rid in ('r2', 'r3'):
            assert result(f, rid)['status'] == 'timeout'
            assert 'max-attempts 2' in result(f, rid)['error']
        assert sorted(result_ids(f)) == ['r1', 'r2', 'r3', 'r4']
    finally:
        f.v2.close()


def test_replay_must_be_boolean():
    with pytest.raises(ValueError, match='replay'):
        fleet.validate_request({'id': 'x', 'payload': {}, 'reply': {'required': ['a']}, 'replay': 'no'}, [])


# ------------------------------------------------------------------ start --resume entry
def finished_run(tmp_path):
    run = tmp_path / 'run'
    run.mkdir()
    fleet.save(run / 'config.json', {'run': str(run), 'transport_version': 2})
    fleet.save(run / 'result.json', {'status': 'completed'})
    return run


def no_cmux(monkeypatch):
    def forbidden():
        pytest.fail('must not reach cmux provisioning')
    monkeypatch.setattr(cmux_manager, 'cmux_available', forbidden)


def test_explicit_resume_of_finished_run_is_refused(tmp_path, monkeypatch):
    no_cmux(monkeypatch)
    run = finished_run(tmp_path)
    with pytest.raises(SystemExit, match='already finished'):
        fleet.main(['start', '--resume', str(run)])


def test_auto_resume_of_finished_run_starts_a_new_run(tmp_path, monkeypatch):
    class Reached(Exception):
        pass

    def reached():
        raise Reached
    run = finished_run(tmp_path)
    monkeypatch.setattr(fleet, 'LATEST', tmp_path / 'latest.json')
    fleet.save(fleet.LATEST, {'run': str(run), 'config': str(run / 'config.json')})
    monkeypatch.setattr(cmux_manager, 'cmux_available', reached)
    with pytest.raises(Reached):  # the normal new-start path
        fleet.main(['start', '--resume', 'auto', '--lifecycle', 'dev_task', '--supervisor-cli', 'codex', '--worker-cli', 'claude',
                    '--allow-development-v2'])


def test_resume_with_live_daemon_does_nothing(tmp_path, monkeypatch, capsys):
    no_cmux(monkeypatch)
    run = tmp_path / 'run'
    run.mkdir()
    fleet.save(run / 'config.json', {'run': str(run), 'transport_version': 2, 'supervisor_cli': 'codex',
                                     'worker_cli': 'claude', 'mode': 'task', 'allow_development_v2': True})
    with (run / 'daemon.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fleet.main(['start', '--resume', str(run)])
    assert 'live daemon' in capsys.readouterr().out


def test_daemon_run_takes_resume_branch_only_when_requested(task_run, monkeypatch):
    state = task_run
    state['f'].v2.close()
    f = new_fleet(state, state['clients'][0])
    calls = []
    monkeypatch.setattr(runtime_v2.RuntimeV2, 'resume_run', lambda self: calls.append('resume'))
    monkeypatch.setattr(runtime_v2.RuntimeV2, 'recover', lambda self: calls.append('recover'))
    f.stopping = True
    f.resume_requested = True
    f.run()
    assert calls == ['resume']
    state['f'] = new_fleet(state, state['clients'][0])


def test_lost_page_is_rebuilt_with_next_generations(task_run):
    state = task_run
    half_done(state)
    state['mgr'].alive.clear()  # cmux restarted: no tab of this run is left
    f = crash_and_resume(state)
    assert [f.label(s) for s in f.names] == ['supervisor-1.1', 'worker-1.1', 'worker-2.1']
    sup, workers, _ = state['mgr'].arranged
    assert sup == f.agent('supervisor')['pane'] and workers == [f.agent(w)['pane'] for w in f.workers]
    for slot in f.names:
        assert answer_turn(f, slot, 'READY', startup=True)['status'] == 'ready'
    f.pump()
    assert not typed(f, 'worker-1') and len(typed(f, 'worker-2')) == 1  # only the lost dispatch reruns


def test_explicit_stop_writes_result_so_resume_refuses(service):
    f = service
    f.v2.close()
    again = fleet.Fleet(f.path, f.r, f.mgr)
    runtime_v2.post_event(again.v2.bus, {'kind': 'stop', 'request_id': 'stop-1'}, 'stop-1')
    again.run()
    out = json.loads((again.run_dir / 'result.json').read_text())
    assert out['status'] == 'stopped' and out['reason'] == 'explicit fleet stop'
    with pytest.raises(SystemExit, match='already finished'):
        fleet.resume_target(str(again.run_dir))
