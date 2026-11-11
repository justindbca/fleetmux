"""A laptop lid closed and opened: the daemon recovers the slots by itself (§35).

1. After a daemon gap the bridges' heartbeat keys have expired; the first tick must not call the link lost.
2. A startup that fails on the network (Wi-Fi not back yet) is retried as the next generation with backoff,
   not turned into a blocked run that only a human can resume; while the network is down nothing is spent."""
import time

import pytest

from fleetmux import fleet, runtime_v2
from test_link_loss import SLOT, order_plan
from test_runtime_v2_planning import plan  # noqa: F401
from test_message_bus_v2 import private_redis_v2  # noqa: F401
from test_runtime_v2 import audits, event, fx, session  # noqa: F401


def test_first_tick_after_a_gap_waits_for_the_next_beat(plan):
    f, rt, source, env = order_plan(plan)
    source.hset(rt.bus.lease_key(SLOT), mapping={'status': 'idle', 'current_delivery': '', 'message_id': ''})
    f.setf(SLOT, state='idle', runtime_ready=1, bridge_hb_g=0)  # beat once, key expired while asleep
    now = time.time()
    rt.last_tick = now - 900
    rt.daemon_gap(now)
    rt.heartbeat_check(SLOT, f.agent(SLOT))
    assert f.agent(SLOT)['state'] == 'idle' and not f.agent(SLOT).get('link_lost')
    rt.hb_grace_until = 0  # still silent after the grace: the link is lost
    rt.heartbeat_check(SLOT, f.agent(SLOT))
    assert f.agent(SLOT)['state'] == 'held' and f.agent(SLOT)['link_lost'] == '1'


def startup(f, slot, **kw):
    f.setf(slot, state='starting')
    event(f, dict(session(f, slot), kind='startup', request_id='ready-again-' + str(time.time()),
                  **dict(dict(text='', failed=True, aborted=False), **kw)))


def test_offline_startup_is_retried_not_blocked(fx, monkeypatch):
    f = fx
    startup(f, 'supervisor', error='fetch failed')
    info = f.agent('supervisor')
    assert info['state'] == 'dead' and not f.paused()
    assert info['startup_retries'] == '1' and float(info['startup_retry_at']) > time.time()
    assert audits(f, 'startup.offline')
    rotated = []
    monkeypatch.setattr(f, 'network_up', lambda: True)
    monkeypatch.setattr(f, 'wall_rotate', lambda name, launch, why: rotated.append((name, why)))
    f.startup_retry_tick()
    assert rotated == []  # backoff not due
    f.setf('supervisor', startup_retry_at=time.time() - 1)
    f.startup_retry_tick()
    f.startup_retry_tick()
    assert rotated == [('supervisor', 'retry after a network error at startup')]


@pytest.mark.parametrize('kw', [dict(error='model not found'), dict(error='fetch failed', aborted=True),
                                dict(text='hello', failed=False)])
def test_other_startup_failures_still_block(fx, kw):
    startup(fx, 'supervisor', **kw)
    if not kw.get('failed', True):  # one READY nudge first, as before
        startup(fx, 'supervisor', **kw)
    assert fx.agent('supervisor')['state'] == 'dead' and fx.paused()
    assert not fx.agent('supervisor').get('startup_retry_at')


def test_offline_retries_are_capped_then_block(fx):
    fx.setf('supervisor', startup_retries=fleet.Fleet.STARTUP_RETRY_MAX)
    startup(fx, 'supervisor', error='getaddrinfo ENOTFOUND api.openai.com')
    assert fx.paused()


def test_ready_resets_the_retry_count(fx):
    startup(fx, 'worker-1', error='socket hang up')
    assert fx.agent('worker-1')['startup_retries'] == '1'
    startup(fx, 'worker-1', text='READY', failed=False)
    assert fx.agent('worker-1')['state'] == 'idle' and fx.agent('worker-1')['startup_retries'] == '0'


@pytest.mark.parametrize('error,offline', [('fetch failed', True), ('read ECONNRESET', True),
                                           ('getaddrinfo EAI_AGAIN chatgpt.com', True),
                                           ('429 rate limit', False), ('', False)])
def test_offline_classification(error, offline):
    assert runtime_v2.startup_offline({'failed': True, 'aborted': False, 'error': error}) is offline


def test_dark_wakes_spend_no_attempts(fx, monkeypatch):
    f = fx
    startup(f, 'supervisor', error='fetch failed')
    rotated = []
    monkeypatch.setattr(f, 'network_up', lambda: False)
    monkeypatch.setattr(f, 'wall_rotate', lambda name, launch, why: rotated.append(name))
    for _ in range(fleet.Fleet.STARTUP_RETRY_MAX + 1):  # one due tick per dark wake, Wi-Fi still down
        f.setf('supervisor', startup_retry_at=time.time() - 1)
        f.startup_retry_tick()
    info = f.agent('supervisor')
    assert rotated == [] and not f.paused() and info['startup_retries'] == '1'
    assert float(info['startup_retry_at']) > time.time()  # still scheduled for when the network is back


def test_retry_starts_a_real_next_generation_that_goes_ready(fx, monkeypatch):
    f = fx
    startup(f, 'worker-1', error='fetch failed')
    monkeypatch.setattr(f, 'network_up', lambda: True)
    f.setf('worker-1', startup_retry_at=time.time() - 1)
    f.startup_retry_tick()
    info = f.agent('worker-1')
    assert info['generation'] == '1' and info['state'] == 'starting' and info['launch_ready'] == '1'
    md = session(f, 'worker-1')
    event(f, dict(md, kind='bridge_session', request_id='reg-g1'))
    f.v2.bus.register_slot('worker-1', 1, md['thread'])
    event(f, dict(md, kind='startup', text='READY', failed=False, aborted=False, request_id='ready-g1'))
    assert f.agent('worker-1')['state'] == 'idle' and f.agent('worker-1')['startup_retries'] == '0'
    assert not f.paused()


def test_link_restart_waits_for_the_network_without_counting(plan, monkeypatch):
    from fleetmux import watchdog
    f, rt, source, env = order_plan(plan)
    monkeypatch.setattr(rt, 'transaction', lambda identity, action: action())
    wd = watchdog.Watchdog(rt)
    calls = []
    wd.rotate = lambda slot, why, screen, reason=None: calls.append(slot) or True
    wd.capture = lambda slot, info, pane: 'screen'
    online = [False]
    monkeypatch.setattr(f, 'network_up', lambda: online[0])
    f.setf(SLOT, state='held', link_lost=1)
    for n in range(watchdog.LINK_MAX_RESTARTS + 2):  # a long offline wait never reaches the cap
        wd.link_restart(SLOT, f.agent(SLOT), time.time() + n)
    assert calls == [] and f.agent(SLOT)['link_lost'] == '1' and not f.agent(SLOT).get('link_restarts')
    assert f.agent(SLOT)['link_offline'] == '1' and not f.paused()
    online[0] = True
    wd.link_restart(SLOT, f.agent(SLOT), time.time())
    assert calls == [SLOT] and f.agent(SLOT)['link_restarts'].startswith('1:')
    assert f.agent(SLOT)['link_offline'] == '0'
