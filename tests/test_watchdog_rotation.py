"""Frozen -> next generation on a Pi fleet (service mode, bridge receive), including every failure branch.

Real private Redis + the Pi v2 fixture from test_runtime_v2; fake cmux (FakeMgr) with failure knobs.
Invariant checked throughout: the successor never gets work before the old session is confirmed
retired, a failed rotation never leaves two sessions, and cancelled work is never re-run.
"""
import time
from types import SimpleNamespace

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import event, fx, receive, req, result, session  # noqa: F401  (fixture)
from cmux_fleet import fleet

WORKING = 'pi> working…'


@pytest.fixture
def pi(fx):  # noqa: F811
    f = fx
    f.cfg.update(watch_interval=0)
    f.mgr.screens, f.mgr.stats = {}, {}
    f.mgr.capture_output = lambda pane, lines=2000: f.mgr.screens.get(pane, WORKING)
    f.mgr.process_states = lambda pane: f.mgr.stats.get(pane, ['Ss', 'S+'])
    f.mgr.owns = lambda pane: pane in f.mgr.alive
    req(f, 'r1', to='worker-1')
    f.pump()
    env = receive(f, 'worker-1')
    assert env and f.v2.bus.slot_state('worker-1')['status'] == 'received'
    f.old_pane, f.msg_id = f.agent('worker-1')['pane'], env['message_id']
    f.mgr.stats[f.old_pane] = ['Ss', 'U+']   # E5: stuck in the kernel; keys do nothing
    return f


def audits(f, event_name):
    return [x for _, x in f.r.xrange(f.v2.bus.audit_key) if x.get('event') == event_name]


def freeze(f, t=None):
    t = time.time() if t is None else t
    f.v2.watchdog.tick(t)
    f.v2.watchdog.tick(t + 1)   # second 'U' sighting -> frozen


def counts(f):
    return dict(killed=len(f.mgr.killed), spawned=len(getattr(f.mgr, 'spawned', [])),
                restarted=len(audits(f, 'watch.frozen_restarted')), failed=len(audits(f, 'watch.rotate_failed')),
                held=len(audits(f, 'execution.held')), notes=len(list((f.run_dir / 'handoffs').glob('*.md'))),
                keys=len(f.mgr.keys))


def assert_rolled_back(f):
    """Failure: only the frozen session is left, back in its slot, held, its work still uncertain."""
    w = f.agent('worker-1')
    assert w['pane'] == f.old_pane and w['generation'] == '0' and w['label'] == 'worker-1'
    assert w['state'] == 'held' and f.paused()
    assert f.r.hget(fleet.NAMES, 'worker-1.1') is None
    alive_worker_panes = {p for p in f.mgr.alive if p not in (f.agent(s)['pane'] for s in f.names if s != 'worker-1')}
    assert alive_worker_panes - {'history-pane'} == {f.old_pane}   # never two sessions of worker-1
    lease = f.v2.bus.slot_state('worker-1')
    assert lease['current_delivery'] and lease['status'] == 'uncertain'   # not cancelled, not re-run
    assert f.v2.bus.message(f.msg_id)['status'] == 'uncertain'
    assert audits(f, 'watch.frozen_restarted') == [] and audits(f, 'runtime.retired') == []
    assert f.r.hget(fleet.REQ + 'r1', 'status') == 'dispatched'          # no result invented for the caller
    assert not f.v2.bus.pending('worker-1')


def test_pi_frozen_rotation_succeeds_and_the_request_is_not_re_run(pi):
    f = pi
    freeze(f)
    w = f.agent('worker-1')
    assert f.old_pane in f.mgr.killed and w['generation'] == '1' and w['label'] == 'worker-1.1'
    assert w['state'] == 'starting' and w['launch_ready'] == '1' and (w['pane'], f.old_pane) in f.mgr.tabbed
    msg = f.v2.bus.message(f.msg_id)
    assert msg['outcome'] == 'cancelled' and str(msg['stop_confirmed']) == '1'
    assert not f.v2.bus.slot_state('worker-1').get('current_delivery')
    told = result(f, 'r1')                                            # caller told once, not re-dispatched
    assert told['status'] == 'stalled' and 'not re-run' in told['error'] and told['worker'] == 'worker-1.1'
    assert f.r.llen(fleet.RESULTS) == 1
    assert not f.v2.bus.pending('worker-1') and not f.paused()
    # The successor registers and starts like any new generation, and is offered nothing.
    meta = session(f, 'worker-1')
    event(f, dict(meta, kind='bridge_session', request_id='register-g1'))
    f.v2.bus.register_slot('worker-1', 1, meta['thread'])
    event(f, dict(meta, kind='startup', request_id='ready-g1', text='READY', failed=False, aborted=False))
    event(f, dict(meta, kind='availability', idle=True))
    assert f.agent('worker-1')['state'] == 'idle'
    f.pump()
    assert f.agent('worker-1')['state'] == 'idle' and not f.v2.bus.slot_state('worker-1').get('current_delivery')


def test_successor_cannot_take_work_before_the_old_session_is_retired(pi, monkeypatch):
    """Observe the successor at the moment the old pane is killed: launch-held, no lease, no READY."""
    f = pi
    seen = {}
    real_kill = f.mgr.kill_pane

    def kill(pane):
        if pane == f.old_pane:
            w = f.agent('worker-1')
            seen.update(state=w['state'], launch_ready=w['launch_ready'],
                        lease=f.v2.bus.slot_state('worker-1')['current_delivery'])
            with pytest.raises(Exception, match='busy'):   # the bus refuses a new registration
                f.v2.bus.register_slot('worker-1', 1, 'session-worker-1-g1')
        return real_kill(pane)
    monkeypatch.setattr(f.mgr, 'kill_pane', kill)
    freeze(f)
    assert seen == dict(state='starting', launch_ready='0', lease=seen['lease']) and seen['lease']
    assert audits(f, 'watch.frozen_restarted')


def test_old_session_that_does_not_exit_rolls_back_and_kills_the_successor(pi, monkeypatch):
    f = pi
    real_kill = f.mgr.kill_pane
    monkeypatch.setattr(f.mgr, 'kill_pane', lambda pane: False if pane == f.old_pane else real_kill(pane))
    freeze(f)
    failed, = audits(f, 'watch.rotate_failed')
    assert 'did not exit' in failed['reason'] and failed['successor_removed'] == 'true'
    assert failed['new_pane'] in f.mgr.killed and f.old_pane not in f.mgr.killed
    assert_rolled_back(f)


def test_old_session_still_alive_after_a_reported_kill_rolls_back(pi, monkeypatch):
    f = pi
    real_kill = f.mgr.kill_pane

    def kill(pane):
        if pane == f.old_pane:
            return True       # cmux said closed, but the surface is still there
        return real_kill(pane)
    monkeypatch.setattr(f.mgr, 'kill_pane', kill)
    freeze(f)
    assert 'did not exit' in audits(f, 'watch.rotate_failed')[0]['reason']
    assert_rolled_back(f)


@pytest.mark.parametrize('partial', [None, 'pane-partial'])
def test_successor_that_fails_to_start_rolls_back(pi, monkeypatch, partial):
    f = pi

    def broken(**kw):
        if partial:
            f.mgr.alive.add(partial)
        return SimpleNamespace(success=False, pane_id=partial, error='cmux new-surface failed')
    monkeypatch.setattr(f.mgr, 'spawn_interactive_cli', broken)
    freeze(f)
    failed, = audits(f, 'watch.rotate_failed')
    assert 'did not start' in failed['reason'] and 'cmux new-surface failed' in failed['reason']
    assert f.old_pane not in f.mgr.killed
    assert (partial in f.mgr.killed) if partial else f.mgr.killed == []
    assert_rolled_back(f)


def test_successor_that_cannot_take_the_tab_rolls_back(pi, monkeypatch):
    f = pi
    monkeypatch.setattr(f.mgr, 'tab_into', lambda pane, peer, before=True: False)
    freeze(f)
    assert 'could not take the old tab' in audits(f, 'watch.rotate_failed')[0]['reason']
    assert f.old_pane not in f.mgr.killed
    assert_rolled_back(f)


def test_neither_session_can_be_stopped_blocks_the_task_with_both_named(pi, monkeypatch):
    f = pi
    monkeypatch.setattr(f.mgr, 'kill_pane', lambda pane: False)
    freeze(f)
    failed, = audits(f, 'watch.rotate_failed')
    assert failed['successor_removed'] == 'false'
    w = f.agent('worker-1')
    assert w['state'] == 'held' and w['launch_ready'] == '0' and w['retired_pane'] == f.old_pane
    assert f.paused() and 'two sessions may hold slot worker-1' in f.r.hget(fleet.SERVICE, 'reason')  # service: blocked = paused
    assert f.v2.bus.slot_state('worker-1')['status'] == 'uncertain' and audits(f, 'runtime.retired') == []
    # The leftover successor's READY is refused: it can never pick up work.
    meta = session(f, 'worker-1')
    event(f, dict(meta, kind='startup', request_id='ready-late', text='READY', failed=False, aborted=False))
    assert f.agent('worker-1')['state'] == 'held' and f.agent('worker-1')['runtime_ready'] != '1'
    assert audits(f, 'startup.duplicate') or audits(f, 'event.stale_session')


@pytest.mark.parametrize('breakage', ['none', 'old_exit', 'spawn'])
def test_repeated_ticks_after_a_rotation_do_nothing_more(pi, monkeypatch, breakage):
    f = pi
    real_kill = f.mgr.kill_pane
    if breakage == 'old_exit':
        monkeypatch.setattr(f.mgr, 'kill_pane', lambda pane: False if pane == f.old_pane else real_kill(pane))
    if breakage == 'spawn':
        monkeypatch.setattr(f.mgr, 'spawn_interactive_cli',
                            lambda **kw: SimpleNamespace(success=False, pane_id=None, error='no'))
    t = time.time()
    freeze(f, t)
    before = counts(f)
    status = f.r.hget(fleet.REQ + 'r1', 'status'), f.r.llen(fleet.RESULTS)
    for i in range(2, 8):
        f.v2.watchdog.tick(t + i * 1000)
    assert counts(f) == before
    assert (f.r.hget(fleet.REQ + 'r1', 'status'), f.r.llen(fleet.RESULTS)) == status and not f.v2.bus.pending('worker-1')
