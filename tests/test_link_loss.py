"""§35: a lost bridge link restarts the slot's session; a daemon hold is not a lost link.

Plan-level tests on the same fixture as the mid-turn tests (no sockets)."""
import time
from types import SimpleNamespace

import pytest

from fleetmux import fleet, runtime_v2, watchdog
from test_midturn_v2 import SLOT, order_plan
from test_runtime_v2_planning import plan  # noqa: F401


def fault(rt, reason):
    rt.handle({'kind': 'bridge_fault', 'agent': SLOT, 'generation': 0, 'run_id': rt.bus.run_id,
               'thread': 'session-' + SLOT, 'reason': reason})


@pytest.mark.parametrize('reason,link', [
    ('daemon holds execution; stop confirmation required', False),
    ('settlement unconfirmed/held; no execution replay', False),
    ('recovered PEL may have executed; replay forbidden', False),
    ('delivery collided with manual input; slot held', False),
    ('opencode hold: opencode turn unattributable', False),
    ('lease expired; delivery held', True),
    ('Error 61 connecting to unix socket. Connection refused.', True),
    ('', True)])
def test_link_fault_classification(reason, link):
    assert runtime_v2.link_fault(reason) is link


def test_link_fault_holds_only_this_slot_and_marks_it_for_restart(plan):
    f, rt, source, env = order_plan(plan)
    fault(rt, 'Error 61 connecting to unix socket')
    info = f.agent(SLOT)
    assert info['state'] == 'held' and info['link_lost'] == '1'
    assert not f.paused()  # the other slots keep working
    assert f.r.hget(rt.bus.msg_key('m-test'), 'uncertain_reason').startswith(runtime_v2.LINK_LOST)


def test_fault_echoing_a_daemon_hold_neither_rehold_nor_pauses(plan):
    f, rt, source, env = order_plan(plan)
    rt.hold(SLOT, 'blocked: reviewer: exactly one structured candidate is required', pause=False)
    fault(rt, 'daemon holds execution; stop confirmation required')
    info = f.agent(SLOT)
    assert info['state'] == 'held' and not info.get('link_lost')
    assert not f.paused()
    assert f.r.hget(rt.bus.msg_key('m-test'), 'uncertain_reason').startswith('blocked: reviewer')


def test_fault_echoing_a_hold_on_an_unheld_slot_keeps_the_old_hold(plan):
    f, rt, source, env = order_plan(plan)
    fault(rt, 'settlement unconfirmed/held; no execution replay')
    assert f.agent(SLOT)['state'] == 'held' and not f.agent(SLOT).get('link_lost') and f.paused()


def test_heartbeat_judges_only_a_bridge_that_did_beat(plan):
    f, rt, source, env = order_plan(plan)
    f.setf(SLOT, runtime_ready=1)
    key = runtime_v2.heartbeat_key(rt.bus, SLOT)
    rt.heartbeat_check(SLOT, f.agent(SLOT))  # older bridge: never beat, never judged
    assert f.agent(SLOT)['state'] == 'busy' and not f.agent(SLOT).get('bridge_hb_g')
    source.set(key, '0')
    rt.heartbeat_check(SLOT, f.agent(SLOT))
    assert f.agent(SLOT)['bridge_hb_g'] == '0' and f.agent(SLOT)['state'] == 'busy'
    source.set(key, '1')  # a successor's beat does not count for this generation
    rt.heartbeat_check(SLOT, f.agent(SLOT))
    assert f.agent(SLOT)['state'] == 'held' and f.agent(SLOT)['link_lost'] == '1'


def test_heartbeat_loss_on_an_idle_slot_holds_without_a_delivery(plan):
    f, rt, source, env = order_plan(plan)
    source.hset(rt.bus.lease_key(SLOT), mapping={'status': 'idle', 'current_delivery': '', 'message_id': ''})
    f.setf(SLOT, state='idle', runtime_ready=1, bridge_hb_g=0)
    rt.heartbeat_check(SLOT, f.agent(SLOT))
    assert f.agent(SLOT)['state'] == 'held' and f.agent(SLOT)['link_lost'] == '1' and not f.paused()


def tick(f, rt, monkeypatch):
    monkeypatch.setattr(rt, 'transaction', lambda identity, action: action())
    for name in ('resume_fallback', 'renew_tui', 'render_cards', 'stopfail_notify'):
        monkeypatch.setattr(rt, name, lambda *a, **k: None)
    monkeypatch.setattr(rt, 'clear_tick', lambda now: None)
    rt.watchdog = SimpleNamespace(tick=lambda now: None)
    f.next_liveness = time.time() + 3600
    rt.tick()


@pytest.mark.parametrize('deadline_passed', [False, True])
def test_lease_lapse_before_the_deadline_is_a_lost_link_not_a_timeout(plan, monkeypatch, deadline_passed):
    f, rt, source, env = order_plan(plan)
    past = int(time.time() * 1000) - 1000
    source.hset(rt.bus.lease_key(SLOT), mapping={'lease_until': past})
    if deadline_passed:
        source.hset(rt.bus.msg_key('m-test'), mapping={'deadline_at': past})
    tick(f, rt, monkeypatch)
    info = f.agent(SLOT)
    assert info['state'] == 'held'
    reason = f.r.hget(rt.bus.msg_key('m-test'), 'uncertain_reason')
    if deadline_passed:
        assert reason.startswith('lease/delivery deadline expired') and not info.get('link_lost')
    else:
        assert reason == runtime_v2.LINK_LOST + 'lease not renewed by the bridge' and info['link_lost'] == '1'


def link_watchdog(f, rt, monkeypatch):
    monkeypatch.setattr(rt, 'transaction', lambda identity, action: action())
    wd = watchdog.Watchdog(rt)
    calls = []
    wd.rotate = lambda slot, why, screen, reason=None: calls.append((slot, why)) or True
    wd.capture = lambda slot, info, pane: 'screen'
    return wd, calls


def test_watchdog_restarts_a_lost_link_then_stops_at_the_cap(plan, monkeypatch):
    f, rt, source, env = order_plan(plan)
    wd, calls = link_watchdog(f, rt, monkeypatch)
    now = time.time()
    for n in range(watchdog.LINK_MAX_RESTARTS):
        f.setf(SLOT, state='held', link_lost=1)
        wd.link_restart(SLOT, f.agent(SLOT), now + n)
        assert calls[-1] == (SLOT, 'bridge link lost') and f.agent(SLOT)['link_lost'] == '0'
    f.setf(SLOT, state='held', link_lost=1)
    wd.link_restart(SLOT, f.agent(SLOT), now + 10)
    assert len(calls) == watchdog.LINK_MAX_RESTARTS
    assert f.agent(SLOT)['link_lost'] == 'capped' and f.paused()  # a human looks at Redis / the bridge


def test_watchdog_restart_count_resets_after_the_window_and_waits_while_paused(plan, monkeypatch):
    f, rt, source, env = order_plan(plan)
    wd, calls = link_watchdog(f, rt, monkeypatch)
    now = time.time()
    f.setf(SLOT, state='held', link_lost=1, link_restarts=str(watchdog.LINK_MAX_RESTARTS) + ':' + str(now - 7200))
    f.pause('operator')
    wd.link_restart(SLOT, f.agent(SLOT), now)
    assert not calls and f.agent(SLOT)['link_lost'] == '1'
    f.r.hset(fleet.SERVICE, mapping={'paused': 0, 'reason': ''})
    assert not f.paused()
    wd.link_restart(SLOT, f.agent(SLOT), now)
    assert calls and f.agent(SLOT)['link_restarts'].startswith('1:')


def test_failed_restart_is_not_retried_every_tick(plan, monkeypatch):
    f, rt, source, env = order_plan(plan)
    wd, calls = link_watchdog(f, rt, monkeypatch)
    wd.rotate = lambda *a, **k: calls.append(a) and False
    f.setf(SLOT, state='held', link_lost=1)
    wd.link_restart(SLOT, f.agent(SLOT), time.time())
    assert f.agent(SLOT)['link_lost'] == 'failed'


def test_busy_bridge_slot_is_not_called_lost_right_after_a_sleep(plan, monkeypatch):
    f, rt, source, env = order_plan(plan)
    now = time.time()
    # slept 15 min: the bridge (asleep too) could not renew; the work deadline is still far away
    source.hset(rt.bus.lease_key(SLOT), mapping={'lease_until': int((now - 600) * 1000)})
    rt.last_tick = now - 900
    tick(f, rt, monkeypatch)
    assert rt.hb_grace_until > now
    assert f.agent(SLOT)['state'] == 'busy' and not f.agent(SLOT).get('link_lost')
    rt.hb_grace_until = 0.0                                       # the grace passed and the bridge never renewed
    tick(f, rt, monkeypatch)
    assert f.agent(SLOT)['state'] == 'held' and f.agent(SLOT)['link_lost'] == '1'


def test_slow_ticks_do_not_keep_the_heartbeat_check_off(plan, monkeypatch):
    f, rt, source, env = order_plan(plan)
    source.hset(rt.bus.lease_key(SLOT), mapping={'status': 'idle', 'current_delivery': '', 'message_id': ''})
    f.setf(SLOT, state='idle', runtime_ready=1, bridge_hb_g=0)   # the bridge beat once; its key is gone (bridge dead)
    t0 = time.time()
    rt.last_tick = t0
    for n in range(1, 4):                                         # a loaded daemon: one tick every 25 s
        now = t0 + 25 * n
        monkeypatch.setattr(time, 'time', lambda now=now: now)
        rt.daemon_gap(now)
        rt.heartbeat_check(SLOT, f.agent(SLOT))
    assert f.agent(SLOT).get('link_lost') == '1'


def test_a_real_sleep_during_slow_ticks_still_gets_the_wait(plan, monkeypatch):
    f, rt, source, env = order_plan(plan)
    now = time.time()
    rt.last_tick, rt.gap_before = now - 900, True                # slow ticks before, then 15 min asleep
    source.hset(rt.bus.lease_key(SLOT), mapping={'lease_until': int((now - 600) * 1000)})
    tick(f, rt, monkeypatch)
    assert rt.hb_grace_until > now
    assert f.agent(SLOT)['state'] == 'busy' and not f.agent(SLOT).get('link_lost')

