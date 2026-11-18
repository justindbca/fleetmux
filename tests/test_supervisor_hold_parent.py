"""R3b: the parent fleet watches each sub-fleet for progress (design.md §24, second line of defence).

A stuck sub-fleet once went unnoticed. The parent daemon reads every running
sub-fleet's own Redis directly (no help from the sub-fleet's daemon): its daemon heartbeat, the last time its audit
stream moved past bookkeeping rows, whether it is paused, whether its supervisor is held, and its task state. No
progress past ``fleet.yaml#daemon_watch.subfleet_stuck_after_s`` -> ``[sub-fleet stuck]`` to the parent's
supervisor and, at a managed root, the user channel (``lifecycle_close.notify``). This works when the sub-fleet's
daemon itself is dead or hung.

Stubbed: fake cmux world, private Redis under /tmp, in-process daemons; no real ~/.fleetmux.
"""
import time

from fleetmux import fleet_config, lifecycle_close, pi_adapter, runtime_v2, subfleet
from test_supervisor_hold import adopted_supervisor_working, queued_texts
from test_supervisor_hold_watch import FROZEN
from test_subfleet import step, tree  # noqa: F401  (fixture)


def parent_look(t):
    """One parent daemon tick, with the per-child check interval made due."""
    t.f.subfleet_watch_at = {}
    t.f.tick()
    step(t)


def stuck_notes(t):
    return [x for x in queued_texts(t.f, 'supervisor') if x.startswith('[sub-fleet stuck]')]


def test_parent_reports_a_subfleet_whose_daemon_stopped(tree, monkeypatch):
    """The sub-fleet's daemon stopped beating (dead or hung; its lock is still held, so the existing collect never
    fires). Its own watchdog cannot report anything: the parent does."""
    t = tree
    cf, rec, thread = adopted_supervisor_working(t, monkeypatch)
    cf.stopped = True  # the child daemon runs no more steps or ticks
    cf.r.set('fx:daemon:beat', str(time.time() - 1000))
    assert subfleet._fleet().daemon_running(rec['run']), 'the hung child still holds its lock'
    parent_look(t)
    notes = stuck_notes(t)
    assert len(notes) == 1, queued_texts(t.f, 'supervisor')
    assert 'M0' in notes[0] and 'daemon' in notes[0] and 'beat' in notes[0]


def test_parent_reports_a_paused_subfleet_with_a_held_supervisor(tree, monkeypatch):
    """The incident's shape: the child's daemon is alive (it beats) but its supervisor is held and it is paused."""
    t = tree
    cf, rec, thread = adopted_supervisor_working(t, monkeypatch)
    cf.v2.transaction('watch-frozen-test', lambda: cf.v2.hold('supervisor', FROZEN))
    cf.stopped = True  # keep the child's own R3 out of it: this is the parent's view only
    cf.r.set('fx:daemon:beat', str(time.time()))
    parent_look(t)
    assert stuck_notes(t) == [], 'reported before subfleet_stuck_after_s'
    cf.r.hset('fx:service', 'at', str(time.time() - 1000))  # paused for 1000 s
    parent_look(t)
    notes = stuck_notes(t)
    assert len(notes) == 1 and 'paused' in notes[0] and 'held' in notes[0] and 'supervisor' in notes[0], notes


def test_parent_stays_quiet_while_the_subfleet_progresses_and_reports_once_per_realert(tree, monkeypatch):
    t = tree
    cf, rec, thread = adopted_supervisor_working(t, monkeypatch)
    cf.r.set('fx:daemon:beat', str(time.time()))
    parent_look(t)
    assert stuck_notes(t) == []
    cf.pause('operator: hold')
    cf.r.hset('fx:service', 'at', str(time.time() - 1000))
    cf.r.set('fx:daemon:beat', str(time.time()))
    parent_look(t)
    assert len(stuck_notes(t)) == 1
    parent_look(t)
    assert len(stuck_notes(t)) == 1, 'reported again before held_realert_s'
    subfleet.save_record(rec['run'], dict(subfleet.record(rec['run']), stuck_alert_at=str(time.time() - 4000)))
    parent_look(t)
    assert len(stuck_notes(t)) == 2
    cf.resume()  # progress again: the episode ends
    cf.r.set('fx:daemon:beat', str(time.time()))
    parent_look(t)
    assert not subfleet.record(rec['run']).get('stuck_alert_at')


def test_root_parent_also_pushes_to_the_user_channel(tree, monkeypatch):
    t = tree
    cf, rec, thread = adopted_supervisor_working(t, monkeypatch)
    sent = []
    monkeypatch.setattr(lifecycle_close, 'managed', lambda f: f is t.f)
    monkeypatch.setattr(lifecycle_close, 'notify', lambda f, event, text, policy=None: sent.append((f, event, text)))
    cf.stopped = True
    cf.r.set('fx:daemon:beat', str(time.time() - 1000))
    parent_look(t)
    assert [(f, e) for f, e, _ in sent] == [(t.f, 'subfleet_stuck')], sent
    assert '[sub-fleet stuck]' in sent[0][2]


def test_subfleet_thresholds_come_from_daemon_watch(tmp_path, monkeypatch):
    shipped = fleet_config.block('daemon_watch')
    assert shipped['subfleet_stuck_after_s'] == 600 and shipped['subfleet_check_s'] == 60
    knobs, problem = runtime_v2.held_watch_knobs()
    assert knobs['subfleet_stuck_after_s'] == 600 and knobs['subfleet_check_s'] == 60 and problem is None
    private = tmp_path / 'fleet.yaml'
    private.write_text('daemon_watch:\n  subfleet_stuck_after_s: 30\n')
    monkeypatch.setenv(fleet_config.USER_ENV, str(private))
    knobs, problem = runtime_v2.held_watch_knobs()
    assert knobs['subfleet_stuck_after_s'] == 30 and knobs['subfleet_check_s'] == 60 and problem is None
    assert pi_adapter.daemon_watch() == {'check_interval_s': 120, 'stale_after_s': 120, 'realert_after_s': 600}
