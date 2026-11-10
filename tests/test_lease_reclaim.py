"""An official-CLI lease that ran out only because the daemon itself did not run is reclaimed, not held (§24).

2026-11-10 fleet-20261110-001730-b05647bd: the Mac slept 05:15-05:32 (Clamshell Sleep, pmset). The daemon renews a
Claude / Codex slot's lease itself (renew_tui), so on wake every in-flight lease had run out while its work
deadline had not. expire() held integrator-1 (task mode: only that slot; service mode: hold + pause the whole fleet);
its turn later ended with the full report, release_held cancelled it and dropped the text, the supervisor was never
told and the fleet sat all idle for 1 h 40 min. Now:

- A: such a lease is reclaimed (tui slot, lease received by tui-<slot>-g<generation>, pane alive, message deadline
  not reached) in expire(), renew_tui() and settle(); audit `lease.reclaimed`. `lease_reclaim: off` keeps the hold.
  The watchdog sees the daemon gap (`last_tick`) and restarts the in-flight slots' stillness baseline, so the first
  tick after waking does not judge them stalled.
- B: in task mode every hold exit hands the turn's final text to the supervisor (`[<label> report] HELD-RELEASED`).

Real private Redis + real hook entry points (Codex supervisor + official Claude workers).
"""
import json
import time

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_v2_codex_claude import answer_turn, dispatch_one, mixed, typed  # noqa: F401  (fixture)
from test_v2_codex_service import route, service  # noqa: F401  (fixture)
from test_watchdog import keys, wd  # noqa: F401  (fixture)
from test_request_no_deadline import post, result
from test_link_loss import tick as plan_tick
from test_midturn_v2 import SLOT, order_plan
from test_runtime_v2_planning import plan  # noqa: F401  (fixture)
from cmux_fleet import fleet, runtime_v2

REPORT = 'Done:\n- wrote part.md\nEvidence:\n- worker-1/part.md 8 bytes\nNot done: none\nBlocked: none'


def audits(f, event):
    return [x for _, x in f.r.xrange(f.v2.bus.audit_key) if x.get('event') == event]


def expire_lease(f, slot, minutes=6):
    """What a lid-closed sleep leaves behind: the lease the daemon did not renew ran out minutes ago."""
    past = int(time.time() * 1000) - minutes * 60000
    f.r.hset(f.v2.bus.lease_key(slot), 'lease_until', past)
    return past


def told(f):
    """Everything the supervisor got (typed / queued for its Codex pane) or still has queued."""
    queued = [f.v2.bus.message(m)['body']['text'] for m in f.v2.bus.pending('supervisor')]
    return typed(f, 'supervisor') + queued


# ------------------------------------------------------------------ A: reclaim instead of hold
def test_task_worker_lease_run_out_during_sleep_is_reclaimed_and_its_report_counts(mixed):
    f = mixed
    dispatch_one(f)
    env = f.v2.bus.slot_state('worker-1')
    expire_lease(f, 'worker-1')
    f.v2.tick()
    lease = f.v2.bus.slot_state('worker-1')
    assert not audits(f, 'execution.held'), 'a lease the daemon did not renew is not a worker fault'
    assert lease['status'] == 'received' and int(lease['lease_until']) > int(time.time() * 1000)
    assert lease['current_delivery'] == env['current_delivery'] and f.agent('worker-1')['state'] == 'busy'
    rec, = audits(f, 'lease.reclaimed')
    assert rec['slot'] == 'worker-1' and json.loads(rec['detail'])['gap_s'] >= 359
    assert answer_turn(f, 'worker-1', REPORT)['status'] == 'committed'
    assert f.v2.bus.message(env['message_id'])['outcome'] == 'completed'
    f.pump()
    assert any(t.startswith('[worker-1 report]') for t in told(f)), told(f)
    assert not f.paused() and not audits(f, 'hold.released')


def test_service_lease_run_out_during_sleep_neither_pauses_nor_times_out(service):
    f = service
    post(f, 'r1', to='worker-1')
    expire_lease(f, 'worker-1')
    f.v2.tick()
    assert not f.paused() and result(f, 'r1') is None and not audits(f, 'execution.held')
    assert f.agent('worker-1')['state'] == 'busy'
    assert answer_turn(f, 'worker-1', '{"reply": "fills ok"}')['status'] == 'committed'
    assert result(f, 'r1')['status'] == 'ok' and not f.paused()


def test_service_supervisor_lease_run_out_during_sleep_is_reclaimed_too(service):
    f = service
    post(f, 'r1')                                      # no `to`: the (Codex) supervisor routes it
    expire_lease(f, 'supervisor')
    f.v2.tick()
    assert not f.paused() and not audits(f, 'execution.held') and result(f, 'r1') is None
    assert answer_turn(f, 'supervisor', route('r1'))['status'] == 'committed'
    f.pump()
    assert answer_turn(f, 'worker-1', '{"reply": "fills ok"}')['status'] == 'committed'
    assert result(f, 'r1')['status'] == 'ok' and not f.paused()


def test_stop_handled_before_the_first_tick_after_sleep_settles_normally(mixed):
    f = mixed
    dispatch_one(f)
    env = f.v2.bus.slot_state('worker-1')
    expire_lease(f, 'worker-1')
    # The Stop entered the event stream during the sleep; step() handles it before any tick().
    assert answer_turn(f, 'worker-1', REPORT)['status'] == 'committed'
    assert f.v2.bus.message(env['message_id'])['outcome'] == 'completed'
    assert not f.paused() and not audits(f, 'execution.held')
    assert audits(f, 'lease.reclaimed')


def test_renew_tui_reclaims_instead_of_holding(mixed):
    f = mixed
    dispatch_one(f)
    expire_lease(f, 'worker-1')
    f.v2.renewed.clear()
    f.v2.renew_tui()
    assert f.v2.bus.slot_state('worker-1')['status'] == 'received'
    assert not audits(f, 'execution.held') and not f.paused() and audits(f, 'lease.reclaimed')


# ------------------------------------------------------------------ A: what still holds
def test_deadline_passed_still_holds(mixed):
    f = mixed
    dispatch_one(f)
    env = f.v2.bus.slot_state('worker-1')
    expire_lease(f, 'worker-1')
    f.r.hset(f.v2.bus.msg_key(env['message_id']), 'deadline_at', int(time.time() * 1000) - 1000)
    f.v2.tick()
    assert f.agent('worker-1')['state'] == 'held' and not audits(f, 'lease.reclaimed')
    assert f.v2.bus.message(env['message_id'])['status'] == 'uncertain'


def test_missing_pane_is_not_reclaimed(mixed):
    f = mixed
    dispatch_one(f)
    f.mgr.alive.discard(f.agent('worker-1')['pane'])
    expire_lease(f, 'worker-1')
    f.v2.tick()
    assert not audits(f, 'lease.reclaimed')
    assert f.v2.bus.slot_state('worker-1')['status'] != 'received'


def test_pi_bridge_lease_lapse_is_still_a_lost_link(plan, monkeypatch):  # noqa: F811
    f, rt, source, env = order_plan(plan)
    f.cfg['lease_reclaim'] = 'on'
    source.hset(rt.bus.lease_key(SLOT), mapping={'lease_until': int(time.time() * 1000) - 1000})
    plan_tick(f, rt, monkeypatch)
    assert f.agent(SLOT)['state'] == 'held' and f.agent(SLOT)['link_lost'] == '1'


def test_lease_reclaim_off_keeps_the_hold(mixed):
    f = mixed
    f.cfg['lease_reclaim'] = 'off'
    dispatch_one(f)
    expire_lease(f, 'worker-1')
    f.v2.tick()
    assert f.agent('worker-1')['state'] == 'held' and not f.paused() and not audits(f, 'lease.reclaimed')
    held, = audits(f, 'execution.held')
    assert 'daemon not running' in held['error']


def test_lease_reclaim_value_is_validated():
    base = dict(transport_version=1)
    for good in ('on', 'off'):
        runtime_v2.validate_config(dict(base, lease_reclaim=good))
    with pytest.raises(ValueError, match='lease_reclaim'):
        runtime_v2.validate_config(dict(base, lease_reclaim=True))
    assert fleet.cli().parse_args(['start']).lease_reclaim is None
    assert fleet.cli().parse_args(['start', '--lease-reclaim', 'off']).lease_reclaim == 'off'


# ------------------------------------------------------------------ A: the watchdog after a daemon gap
def test_first_tick_after_a_daemon_gap_does_not_judge_in_flight_slots_stalled(wd):  # noqa: F811
    f = wd
    f.cfg.update(stall_timeout=2400, stall_window=600, stall_cpu_pct=5)
    f.mgr.process_cpu = lambda pane: 10.0                    # CPU still, screen still, transcript still
    dispatch_one(f)
    t0 = time.time()
    f.v2.watchdog.tick(t0 - 900)                             # the last check before the sleep
    assert f.agent('worker-1')['watch_state'] == 'working'
    f.v2.last_tick = t0 - 900                                # the daemon did not run for 15 minutes
    f.v2.tick()
    info = f.agent('worker-1')
    assert info['watch_state'] == 'working' and keys(f, 'worker-1') == [], 'the sleep is not the worker being still'
    assert float(info['act_floor']) >= t0 - 1 and float(info['quiet_since']) >= t0 - 1
    gap, = audits(f, 'daemon.gap')
    assert json.loads(gap['detail'])['gap_s'] >= 899 and 'worker-1' in json.loads(gap['detail'])['slots']
    now = time.time()
    f.v2.watchdog.tick(now + 300)
    assert f.agent('worker-1')['watch_state'] == 'working'
    f.v2.watchdog.tick(now + 601)                            # still nothing moved for a whole window after waking
    assert f.agent('worker-1')['watch_state'] == 'stalled' and keys(f, 'worker-1') == ['Escape']


# ------------------------------------------------------------------ B: a hold exit tells the supervisor
def test_task_hold_exit_hands_the_turn_text_to_the_supervisor(mixed):
    f = mixed
    dispatch_one(f)
    f.v2.transaction('test-hold', lambda: f.v2.hold('worker-1', 'x', pause=False))
    paused = f.paused()
    text = 'HEAD-MARK ' + 'x' * 5000 + '\n' + REPORT + '\nTAIL-MARK'
    assert answer_turn(f, 'worker-1', text)['status'] == 'released'
    assert f.paused() == paused and f.agent('worker-1')['state'] == 'idle'
    f.pump()
    notes = [t for t in told(f) if 'HELD-RELEASED' in t]
    assert len(notes) == 1, told(f)
    note = notes[0]
    assert note.startswith('[worker-1 report]') and '(x)' in note
    assert 'TAIL-MARK' in note and 'HEAD-MARK' not in note
    rel, = audits(f, 'hold.released')
    assert json.loads(rel['detail'])['notified'] is True


def test_service_hold_exit_queues_nothing_for_the_supervisor(service):
    f = service
    post(f, 'r1', to='worker-1')
    f.v2.transaction('test-hold', lambda: f.v2.hold('worker-1', 'x', pause=False))
    assert answer_turn(f, 'worker-1', '{"reply": "late"}')['status'] == 'released'
    assert not [t for t in told(f) if 'HELD-RELEASED' in t]
    rel, = audits(f, 'hold.released')
    assert json.loads(rel['detail']).get('notified') is False
