"""Held watchdog backstop (design.md §24): a slot held, or a run paused, for too long with no progress is acted on by
the daemon itself, never by an outside session.

- The turn is proven stopped (the StopFailure's ``turnend`` mark, or an API-error end in the transcript) -> the
  daemon settles it like a StopFailure event that never arrived and lifts the pause the hold itself set.
- An adopted sub-fleet supervisor held with its own pause -> the daemon lifts that pause and the existing
  ``adopted_held_tick`` takes it back in its session.
- Otherwise, or when the pause is not the hold's own (operator, brake): escalate only (parent supervisor / root
  channels), once per ``held_realert_s``; audit ``held_watch.*``.
- A delete confirmation dialog (or a delete dialog on the held pane's screen) is escalated only, never recovered.
- Thresholds: ``fleetmux/config/fleet.yaml#daemon_watch`` (``held_after_s`` / ``held_realert_s``).

Stubbed: fake cmux world / HookMgr, private Redis under /tmp, in-process daemons; no real ~/.fleetmux.
"""
import json
import time

import pytest

from fleetmux import awake, fleet, fleet_config, pi_adapter, runtime_v2
from test_supervisor_hold import (adopted_supervisor_working, audit_rows, backoff_due, failure_raw, no_outside_command,
                                  pending_texts, queued_texts, start_rewake)
from test_subfleet import step, tree  # noqa: F401  (fixture)
from test_claude_hook_delivery import hooked, handed  # noqa: F401  (fixture)
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)

FROZEN = 'frozen (3 Escape presses left the screen unchanged); adopted pane belongs to the parent fleet, not killed'


def events(f, name):
    return [x for x in audit_rows(f) if x.get('event') == name]


def stuck(f, slot, seconds=400):
    """Fast-forward the held clock (awake seconds) instead of waiting for held_after_s."""
    f.v2.tick()  # the watch stamps held_since on its first look
    assert f.agent(slot).get('held_since'), 'the held watch did not stamp the hold'
    f.setf(slot, held_since=awake.now() - seconds)


def drop_stop_failure_events(monkeypatch):
    """The hook loses its event (what every supervisor StopFailure did before the fix)."""
    real = runtime_v2.post_event

    def post(bus, ev, *a, **kw):
        if ev.get('kind') == 'stop_failure':
            return None
        return real(bus, ev, *a, **kw)
    monkeypatch.setattr(runtime_v2, 'post_event', post)
    return real


# ------------------------------------------------------------------ R3 + R5: the incident when no event arrives
def test_frozen_adopted_supervisor_is_recovered_by_the_daemon_after_the_threshold(tree, monkeypatch):
    """10:25 path of the incident: no stop_failure event, the watchdog froze the adopted supervisor and held it with
    a pause. After held_after_s with no progress the daemon recovers it on its own and the message is handed over
    again; nobody runs resume."""
    t = tree
    cf, rec, thread = adopted_supervisor_working(t, monkeypatch)
    raw = failure_raw(t, thread)
    real = drop_stop_failure_events(monkeypatch)
    assert fleet.claude_stop_failure(t.f.path, 'worker-1', 0, raw)
    monkeypatch.setattr(runtime_v2, 'post_event', real)
    th, box = start_rewake(t, cf, raw, monkeypatch)
    step(t)
    cf.v2.transaction('watch-frozen-test', lambda: cf.v2.hold('supervisor', FROZEN))
    assert cf.agent('supervisor')['state'] == 'held' and cf.paused()
    cf.tick()
    assert cf.paused(), 'acted before held_after_s'
    stuck(cf, 'supervisor')
    for _ in range(3):
        cf.tick()
        step(t)
    assert not cf.paused(), 'the daemon did not lift the pause its own hold set'
    assert events(cf, 'held_watch.recovered') or events(cf, 'held_watch.resumed_owned_pause')
    if cf.agent('supervisor').get('stopfail_at'):
        backoff_due(t, cf)
    th.join(20)
    assert not th.is_alive() and 'Research five sources' in box.get('text', ''), box
    no_outside_command(cf)


def test_held_watch_recovers_a_worker_when_the_turnend_mark_proves_the_turn_stopped(hooked):
    f = hooked
    got, env = handed(f)
    gen = int(f.agent('worker-1')['generation'])
    f.v2.transaction('t-hold', lambda: f.v2.hold('worker-1', 'execution state unconfirmed (test)'))
    assert f.paused()
    f.r.set(f.v2.hook_keys('worker-1', gen)['turnend'], str(time.time()))  # the StopFailure hook ran
    stuck(f, 'worker-1')
    f.v2.tick()
    assert events(f, 'held_watch.recovered') and events(f, 'stop_failure.retry')
    assert any(x.startswith(runtime_v2.STOP_FAILURE_RESUME) for x in pending_texts(f, 'worker-1'))
    assert not f.paused()


def test_held_watch_never_lifts_a_pause_someone_else_set(hooked):
    f = hooked
    handed(f)
    gen = int(f.agent('worker-1')['generation'])
    f.v2.transaction('t-hold', lambda: f.v2.hold('worker-1', 'execution state unconfirmed (test)'))
    time.sleep(0.01)
    f.pause('operator: hold everything')  # a later pause by the operator replaces the hold's own
    f.r.set(f.v2.hook_keys('worker-1', gen)['turnend'], str(time.time()))
    stuck(f, 'worker-1')
    f.v2.tick()
    assert f.paused(), 'an operator pause was lifted'
    assert events(f, 'held_watch.escalated')


def test_held_watch_without_proof_only_escalates_once_per_realert(hooked):
    f = hooked
    handed(f)
    f.v2.transaction('t-hold', lambda: f.v2.hold('worker-1', 'execution state unconfirmed (test)'))
    stuck(f, 'worker-1')
    f.v2.tick()
    f.pump()
    assert f.paused() and f.agent('worker-1')['state'] == 'held'
    assert not events(f, 'stop_failure.retry')
    assert len(events(f, 'held_watch.escalated')) == 1
    assert any('HELD' in x and 'worker-1' in x for x in queued_texts(f, 'supervisor'))
    f.v2.tick()
    assert len(events(f, 'held_watch.escalated')) == 1, 'escalated again before held_realert_s'
    f.setf('worker-1', held_alert_at=awake.now() - 4000)
    f.v2.tick()
    assert len(events(f, 'held_watch.escalated')) == 2


def test_a_stop_failure_backoff_is_not_stuck(hooked):
    from test_stop_failure_retry import fail
    f = hooked
    handed(f)
    fail(f)
    assert f.agent('worker-1')['state'] == 'held' and float(f.agent('worker-1')['stopfail_retry_at']) > time.time()
    stuck(f, 'worker-1')
    f.v2.tick()
    assert not events(f, 'held_watch.escalated') and not events(f, 'held_watch.recovered')


# ------------------------------------------------------------------ R4: delete confirmation dialogs
def test_delete_dialog_wait_is_escalated_only(hooked):
    f = hooked
    handed(f)
    scope = f.v2.bus.slot_state('worker-1')['current_delivery']
    f.v2.transaction('t-prompt', lambda: f.v2.prompt_wait('worker-1', scope, 'Dangerous rm', 'screen.txt', kind='delete'))
    assert f.agent('worker-1').get('prompt_class') == 'delete' and f.paused()
    keys = list(f.mgr.keys) if hasattr(f.mgr, 'keys') else None
    f.v2.tick()
    f.setf('worker-1', prompt_wait_at=time.time() - 400)
    f.r.hset(fleet.SERVICE, 'at', time.time() - 400)
    f.v2.tick()
    assert f.paused() and f.agent('worker-1').get('prompt_wait') == scope
    esc = events(f, 'held_watch.escalated')
    assert esc and 'delete' in (esc[-1].get('detail') or '')
    if keys is not None:
        assert list(f.mgr.keys) == keys, 'a key was pressed on a delete dialog'


def test_held_adopted_supervisor_with_a_delete_dialog_on_screen_is_not_taken_back(tree, monkeypatch):
    t = tree
    cf, rec, thread = adopted_supervisor_working(t, monkeypatch)
    raw = failure_raw(t, thread)
    real = drop_stop_failure_events(monkeypatch)
    assert fleet.claude_stop_failure(t.f.path, 'worker-1', 0, raw)
    monkeypatch.setattr(runtime_v2, 'post_event', real)
    cf.r.delete(cf.v2.hook_keys('supervisor', 0)['turnend'])  # no proof either way: only the screen speaks
    screen = ('Bash command\n  rm -rf build/\n  Remove build output\nDo you want to proceed?\n'
              '❯ 1. Yes\n  2. No, and tell Claude what to do differently (esc)\n')
    monkeypatch.setattr(type(cf.mgr), 'capture_output', lambda self, pane, lines=200: screen, raising=False)
    cf.v2.transaction('watch-frozen-test', lambda: cf.v2.hold('supervisor', FROZEN))
    keys_before = list(t.world.keys)
    stuck(cf, 'supervisor')
    for _ in range(3):
        cf.tick()
        step(t)
    assert cf.paused() and cf.agent('supervisor')['state'] == 'held'
    assert t.world.keys == keys_before, 'Escape on a delete dialog answers it for the user'
    esc = events(cf, 'held_watch.escalated')
    assert esc and 'delete' in (esc[-1].get('detail') or '')


# ------------------------------------------------------------------ thresholds: fleet.yaml#daemon_watch
def test_thresholds_come_from_fleet_yaml_daemon_watch(tmp_path, monkeypatch):
    shipped = fleet_config.block('daemon_watch')
    assert shipped['held_after_s'] == 300 and shipped['held_realert_s'] == 1800
    private = tmp_path / 'fleet.yaml'
    private.write_text('daemon_watch:\n  held_after_s: 60\n')
    monkeypatch.setenv(fleet_config.USER_ENV, str(private))
    knobs, problem = runtime_v2.held_watch_knobs()
    assert (knobs['held_after_s'], knobs['held_realert_s']) == (60, 1800) and problem is None
    assert pi_adapter.daemon_watch() == {'check_interval_s': 120, 'stale_after_s': 120, 'realert_after_s': 600}
    private.write_text('daemon_watch:\n  held_after_s: -5\n')
    knobs, problem = runtime_v2.held_watch_knobs()
    assert knobs == runtime_v2.HELD_WATCH and problem
    private.write_text('daemon_watch:\n  stale_after_s: 300\n  held_after_s: 90\n  bogus_s: 1\n')
    with pytest.raises(ValueError):
        pi_adapter.daemon_watch()
