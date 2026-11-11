"""Real private redis-server REQUIRED. Orchestrator runs these outside the socket-restricted sandbox.

No real cmux/Claude and no production Redis. Real bus Lua, event hooks, PEL, intents and resume.
"""
import json
import time
import uuid

import pytest
import redis

from test_clear_after_result import IDLE, SLOT, idle_twice
from test_message_bus_v2 import bus, private_redis_v2, received, register  # noqa: F401
from test_v2_codex_claude import answer_turn, typed
from test_v2_codex_service import request, route, serve, service  # noqa: F401
from fleetmux import clear_after_result as clear, fleet, lifecycle_hold, runtime_v2
from fleetmux.message_bus import BusError

# §48 (user 2026-11-09:「任何场景都不用 claude 输入框打字」): these tests exercised a channel that typed into a
# Claude input box; it is refused at start now (tests/test_no_claude_typing.py covers the refusal).
RETIRED = pytest.mark.skip(reason='docs/design.md §48: fleet never types into a Claude input box (2026-11-09)')



@pytest.fixture
def clearing(service, monkeypatch):
    f = service
    f.cfg.update(clear_after_result=True, clear_timeout=20, lifecycle='service')
    fleet.save(f.path, f.cfg)
    monkeypatch.setattr(f.mgr, 'capture_output', lambda *a, **kw: IDLE, raising=False)
    monkeypatch.setattr(f.mgr, 'has_cli_process', lambda *a, **kw: True, raising=False)
    monkeypatch.setattr(f.mgr, 'dock_control', lambda *a, **kw: True, raising=False)
    return f


def allow_exit(monkeypatch):
    # Hook entry points run in the pytest process. Simulate that CLI hook subprocess exiting.
    monkeypatch.setattr(clear, 'hook_exited', lambda pid: bool(pid))


def cleared(f, *, session=None, generation=None):
    session = session or str(uuid.uuid4())
    generation = int(f.agent(SLOT)['generation']) if generation is None else generation
    path = f.test_claude / SLOT / (session + '.jsonl')
    assert fleet.claude_clear(f.path, SLOT, generation, json.dumps({
        'source': 'clear', 'session_id': session, 'transcript_path': str(path)}))
    assert f.v2.step(10)
    f.test_threads[SLOT] = session
    return session


def inflight(f, rid='r1'):
    request(f, rid)
    assert answer_turn(f, 'supervisor', route(rid))['status'] == 'committed'
    f.pump()
    lease = f.r.hgetall(f.v2.bus.lease_key(SLOT))
    return f.v2.bus.message(lease['message_id'])


def failure(f, error='safety classifier refusal', **extra):
    raw = dict(session_id=f.test_threads[SLOT], transcript_path=f.agent(SLOT)['transcript'], error=error)
    raw.update(extra)
    assert fleet.claude_stop_failure(f.path, SLOT, int(f.agent(SLOT)['generation']), json.dumps(raw))
    assert f.v2.step(10)


@RETIRED
@pytest.mark.parametrize('reply,status', [('{"reply":"OK"}', 'ok'), ('{"wrong":1}', 'invalid')])
def test_result_clear_rebind_next_delivery_settles_in_new_session(clearing, monkeypatch, reply, status):
    f = clearing
    old = f.agent(SLOT)['thread']
    assert serve(f, 'r1', reply)['status'] == 'committed'
    assert json.loads(f.r.get(fleet.RESULT + 'r1'))['status'] == status
    assert f.agent(SLOT)['state'] == 'clearing'
    sent = len(f.mgr.sent)
    idle_twice(f.v2)  # pytest hook process is still alive: no paste
    assert len(f.mgr.sent) == sent
    request(f, 'r2')
    answer_turn(f, 'supervisor', route('r2'))
    f.pump()
    assert f.v2.depth(SLOT) == 1  # queued throughout clearing
    allow_exit(monkeypatch)
    idle_twice(f.v2)
    assert typed(f, SLOT)[-1] == '/clear'
    assert f.mgr.bracketed[-1] == (f.agent(SLOT)['pane'], False)
    assert f.r.xpending(f.v2.bus.inbox_key(SLOT), f.v2.bus.slot_group(SLOT))['pending'] == 0
    fence = int(f.r.hget(f.v2.bus.lease_key(SLOT), 'fence'))
    new = cleared(f)
    assert new != old and f.agent(SLOT)['generation'] == '0'
    assert f.agent(SLOT)['thread'] == f.r.hget(f.v2.bus.lease_key(SLOT), 'session_id') == new
    assert int(f.r.hget(f.v2.bus.lease_key(SLOT), 'fence')) == fence + 1
    assert f.agent(SLOT)['ctx_used'] == '0'
    f.pump()
    assert f.v2.depth(SLOT) == 1  # clear hook itself has not yet exited
    idle_twice(f.v2)
    f.pump()
    lease = f.r.hgetall(f.v2.bus.lease_key(SLOT))
    assert lease['session_id'] == new and lease['status'] == 'received'
    assert answer_turn(f, SLOT, '{"reply":"fresh context"}')['status'] == 'committed'
    assert json.loads(f.r.get(fleet.RESULT + 'r2'))['result']['reply'] == 'fresh context'
    assert f.agent(SLOT)['state'] == 'clearing'
    assert '/clear' not in typed(f, 'supervisor')


@RETIRED
def test_missing_clear_hook_deadline_holds_and_never_delivers(clearing, monkeypatch):
    f = clearing
    serve(f, 'r1')
    allow_exit(monkeypatch)
    idle_twice(f.v2)
    f.v2.clear_tick(float(f.agent(SLOT)['clear_deadline']) + 1)
    assert f.agent(SLOT)['state'] == 'held'
    sent = len(f.mgr.sent)
    request(f, 'r2')
    f.v2.deliver(SLOT)
    assert len(f.mgr.sent) == sent and f.paused()
    cleared(f)  # confirmed clear may repair the idle binding, but never lifts the hold's run pause
    idle_twice(f.v2)
    assert f.agent(SLOT)['state'] == 'idle' and f.paused()
    f.pump()
    assert len(f.mgr.sent) == sent


@RETIRED
def test_manual_clear_without_delivery_rebinds(clearing, monkeypatch):
    f = clearing
    allow_exit(monkeypatch)
    new = cleared(f)
    idle_twice(f.v2)
    assert f.agent(SLOT)['state'] == 'idle'
    assert f.r.hget(f.v2.bus.lease_key(SLOT), 'session_id') == new
    assert '/clear' not in typed(f, SLOT)
    assert serve(f, 'r1')['status'] == 'committed'


@RETIRED
def test_manual_clear_during_delivery_holds_without_settling(clearing):
    f = clearing
    env = inflight(f)
    old = env['session_id']
    cleared(f)
    assert f.agent(SLOT)['state'] == 'held'
    assert f.agent(SLOT)['clear_conflict'] == '1'
    assert f.r.hget(f.v2.bus.lease_key(SLOT), 'session_id') == old
    assert f.r.xpending(f.v2.bus.inbox_key(SLOT), f.v2.bus.slot_group(SLOT))['pending'] == 1
    assert f.r.get(fleet.RESULT + 'r1') is None
    assert answer_turn(f, SLOT, '{"reply":"wrong session"}')['status'] == 'ignored'
    assert f.r.get(fleet.RESULT + 'r1') is None


@RETIRED
def test_stop_failure_immediate_terminal_no_replay_then_clear(clearing, monkeypatch):
    f = clearing
    env = inflight(f)
    failure(f, error='API overload')
    result = json.loads(f.r.get(fleet.RESULT + 'r1'))
    assert result['status'] == 'failed' and result['error'] == 'API overload'
    assert f.v2.bus.message(env['message_id'])['outcome'] == 'failed'
    assert f.v2.depth(SLOT) == 0 and f.agent(SLOT)['state'] == 'clearing'
    failure(f, error='duplicate')
    assert json.loads(f.r.get(fleet.RESULT + 'r1')) == result
    allow_exit(monkeypatch)
    idle_twice(f.v2)
    cleared(f)
    idle_twice(f.v2)
    assert f.agent(SLOT)['state'] == 'idle'
    assert serve(f, 'r2')['status'] == 'committed'


def test_switch_off_stop_failure_fails_the_request_without_clear(service):
    f = service
    env = inflight(f)
    # Switch off: no /clear. The hook channel's StopFailure still ends the turn (2026-11-04, §24 service):
    # the request fails at once and only this slot is held (tests/test_service_stop_failure.py).
    raw = dict(session_id=f.test_threads[SLOT], transcript_path=f.agent(SLOT)['transcript'], error='refusal')
    assert fleet.claude_stop_failure(f.path, SLOT, int(f.agent(SLOT)['generation']), json.dumps(raw))
    assert f.v2.step(10)
    assert any(e.get('event') == 'hook.stop_failure' for _, e in f.r.xrange(f.v2.bus.audit_key))
    assert json.loads(f.r.get(fleet.RESULT + 'r1'))['status'] == 'failed'
    assert f.v2.bus.message(env['message_id'])['status'] == 'committed'
    assert f.agent(SLOT)['state'] == 'held' and not f.paused() and '/clear' not in typed(f, SLOT)


@RETIRED
def test_timeout_waits_for_valid_held_turn_exit_before_clear(clearing, monkeypatch):
    f = clearing
    f.cfg['request_timeout'] = 900  # explicit run deadline (none by default, design §43)
    env = inflight(f)
    f.r.hset(fleet.REQ + 'r1', 'created', 0)
    f.v2.tick()
    assert json.loads(f.r.get(fleet.RESULT + 'r1'))['status'] == 'timeout'
    assert f.agent(SLOT)['state'] == 'clearing'
    allow_exit(monkeypatch)
    idle_twice(f.v2)
    assert '/clear' not in typed(f, SLOT)
    assert f.r.hget(f.v2.bus.lease_key(SLOT), 'current_delivery') == env['delivery_id']
    assert answer_turn(f, SLOT, '{"reply":"late"}')['status'] == 'released'
    idle_twice(f.v2)
    assert typed(f, SLOT)[-1] == '/clear'
    cleared(f)
    idle_twice(f.v2)
    assert f.agent(SLOT)['state'] == 'idle' and not f.paused()  # a request timeout never paused the fleet (§43)
    assert json.loads(f.r.get(fleet.RESULT + 'r1'))['status'] == 'timeout'


@RETIRED
def test_clearing_defers_rotation_reclaim_and_watchdog(clearing):
    f = clearing
    serve(f, 'r1')
    pane = f.agent(SLOT)['pane']
    f.start_rotation(SLOT, 1000000, 1000000)
    lifecycle_hold.service_tick(f, {'worker_idle_reclaim_min': 0, 'idle_stop_hours': None}, time.time())
    f.v2.watchdog.tick(time.time() + 100000)
    assert f.agent(SLOT)['state'] == 'clearing' and pane not in f.mgr.killed
    assert not f.mgr.keys


@RETIRED
@pytest.mark.parametrize('phase', ['waiting', 'pasted', 'confirmed'])
def test_resume_unconfirmed_clear_replaces_generation_and_keeps_switch(clearing, monkeypatch, phase):
    f = clearing
    serve(f, 'r1')
    if phase == 'confirmed':
        cleared(f)
    else:
        f.setf(SLOT, clear_phase=phase)
    oldpane = f.agent(SLOT)['pane']
    assert not f.v2.reattachable(SLOT)
    f.v2.resume_run()
    assert f.cfg['clear_after_result'] and fleet.load(f.path)['clear_after_result']
    assert f.agent(SLOT)['generation'] == '1'
    assert f.agent(SLOT)['state'] == 'starting' and oldpane in f.mgr.killed
    assert f.agent(SLOT)['clear_required'] == '0'
    assert f.r.get(fleet.RESULT + 'r1') is not None  # completed request not replayed


def test_prepared_rebind_replays_atomically_once(clearing, monkeypatch):
    f = clearing
    old = f.agent(SLOT)['thread']
    fence = int(f.r.hget(f.v2.bus.lease_key(SLOT), 'fence'))
    apply = f.v2.apply
    def crash(intent):
        raise RuntimeError('injected before apply')
    monkeypatch.setattr(f.v2, 'apply', crash)
    with pytest.raises(RuntimeError, match='injected'):
        cleared(f)
    assert f.agent(SLOT)['thread'] == f.r.hget(f.v2.bus.lease_key(SLOT), 'session_id') == old
    assert f.r.xpending(f.v2.events, f.v2.group)['pending'] == 1
    monkeypatch.setattr(f.v2, 'apply', apply)
    assert f.v2.replay_intents()
    assert f.r.xpending(f.v2.events, f.v2.group)['pending'] == 0
    new = f.agent(SLOT)['thread']
    assert new != old and f.r.hget(f.v2.bus.lease_key(SLOT), 'session_id') == new
    assert int(f.r.hget(f.v2.bus.lease_key(SLOT), 'fence')) == fence + 1
    assert f.v2.replay_intents()
    assert int(f.r.hget(f.v2.bus.lease_key(SLOT), 'fence')) == fence + 1
    assert not f.v2.step(10)  # replay already ACKed the original event; no duplicate was posted


def test_rebind_apply_rechecks_pel_before_any_write(clearing, monkeypatch):
    f = clearing
    old = f.agent(SLOT)['thread']
    apply = f.v2.apply
    def race(intent):
        bus = f.v2.bus
        f.r.xadd(bus.inbox_key(SLOT), {'extra': 'unexpected'})
        f.r.xreadgroup(bus.slot_group(SLOT), 'other', {bus.inbox_key(SLOT): '>'}, count=1)
        return apply(intent)
    monkeypatch.setattr(f.v2, 'apply', race)
    with pytest.raises(redis.ResponseError, match='undrained_pel'):
        cleared(f)
    assert f.agent(SLOT)['thread'] == f.r.hget(f.v2.bus.lease_key(SLOT), 'session_id') == old
    assert not f.v2.replay_intents()  # applying uncertainty is not silently replayed
    assert f.paused()


def test_bus_same_generation_rebind_bumps_fence_and_audits(bus):
    register(bus)
    fence = int(bus.slot_state(SLOT)['fence'])
    with pytest.raises(BusError, match='old_generation'):
        bus.register_slot(SLOT, 0, 'session-1')  # ordinary registration stays strict
    assert bus.rebind_slot(SLOT, 0, 'session-0', 'session-1', fence) == fence + 1
    assert bus.slot_state(SLOT)['session_id'] == 'session-1'
    assert bus.audit_tail(1)[0][1]['event'] == 'slot_rebound'
    with pytest.raises(BusError, match='rebind_identity'):
        bus.rebind_slot(SLOT, 0, 'session-0', 'session-2', fence)


@pytest.mark.parametrize('residual_pel', [False, True])
def test_bus_rebind_refuses_current_delivery_or_undrained_pel(bus, residual_pel):
    env = received(bus)
    if residual_pel:
        bus.r.hset(bus.lease_key(SLOT), mapping={'current_delivery': '', 'status': 'idle'})
    before = bus.r.hgetall(bus.lease_key(SLOT))
    with pytest.raises(BusError, match='undrained_pel' if residual_pel else 'rebind_identity'):
        bus.rebind_slot(SLOT, 0, 'session-0', 'session-1', int(before['fence']))
    assert bus.r.hgetall(bus.lease_key(SLOT)) == before
    assert bus.message(env['message_id'])['status'] == 'received'
