"""The pause cause: ``Fleet.pause(reason, kind=None, **ref)`` writes ``fx:service.cause`` (docs/design.md section 11).

The daemon's pause review (section 24) reads the cause of a root pause from a structured field, not from the free
``reason`` text (which a later supervisor reply overwrites). ``cause`` is a JSON object {kind, slot?, message_id?,
generation?, subfleet_run?, brake_kind?}; a pause without a kind writes no cause and drops an older one; ``resume``
clears ``cause`` and ``review``. Every pause call site of fleet.py names its kind.

Private test Redis, fake cmux, run directories under tmp_path; no real ~/.fleetmux.
"""
import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_service_escalate import svc  # noqa: F401  (fixture)
from test_subfleet_blocked_scope import REASON, child, private_yaml, service_blocked, stuck_text
from fleetmux import fleet, runtime_v2


def cause(f):
    raw = f.r.hget(fleet.SERVICE, 'cause') or ''
    return json.loads(raw) if raw else None


def status_of(f):
    buf = io.StringIO()
    with redirect_stdout(buf):
        fleet.status(f.cfg)
    return json.loads(buf.getvalue())


# ------------------------------------------------------------------ the interface
def test_pause_writes_kind_and_ref_and_resume_clears_cause_and_review(svc):
    f = svc
    f.pause('worker-1: held', kind='slot_hold', slot='worker-1', message_id='m-1', generation=2)
    assert f.paused() and f.r.hget(fleet.SERVICE, 'reason') == 'worker-1: held'
    assert cause(f) == {'kind': 'slot_hold', 'slot': 'worker-1', 'message_id': 'm-1', 'generation': 2}
    f.r.hset(fleet.SERVICE, 'review', json.dumps({'verdict': 'kept'}))
    f.resume()
    assert not f.paused() and cause(f) is None and not f.r.hget(fleet.SERVICE, 'review')


def test_pause_without_kind_writes_no_cause_and_drops_an_older_one(svc):
    f = svc
    f.pause('first', kind='operator')
    assert cause(f) == {'kind': 'operator'}
    f.pause('an old caller without a kind')
    assert f.paused() and cause(f) is None
    assert f.r.hget(fleet.SERVICE, 'reason') == 'an old caller without a kind'


def test_pause_refuses_an_unknown_ref_key(svc):
    with pytest.raises(TypeError):
        svc.pause('x', kind='operator', colour='red')


def test_empty_ref_values_are_left_out(svc):
    f = svc
    f.pause('x', kind='wall', brake_kind='wall', slot='')
    assert cause(f) == {'kind': 'wall', 'brake_kind': 'wall'}


# ------------------------------------------------------------------ the call sites
def test_operator_pause_is_operator(svc):
    f = svc
    runtime_v2.post_event(f.v2.bus, dict(kind='pause', request_id='op-pause', reason='market closed'), 'op-pause')
    assert f.v2.step(10)
    assert f.paused() and cause(f) == {'kind': 'operator'}


def test_three_bad_supervisor_replies_are_supervisor_fault(svc):
    f = svc
    for _ in range(3):
        f.on_service_supervisor('not json', invalid='not a decision')
    assert f.paused()
    assert cause(f) == {'kind': 'supervisor_fault', 'slot': 'supervisor',
                        'generation': int(f.agent('supervisor').get('generation') or 0)}


def test_general_blocked_is_supervisor_blocked(svc):
    f = svc
    service_blocked(f, '[Operator instruction] stop routing to worker-2')
    assert f.paused() and cause(f) == {'kind': 'supervisor_blocked'}


def test_blocked_on_a_child_notice_with_the_switch_off_is_subfleet(svc, tmp_path, monkeypatch):
    f = svc
    private_yaml(tmp_path, monkeypatch, 'daemon_watch:\n  subfleet_blocked_marks_child: false\n')
    d = child(f)
    service_blocked(f, stuck_text('M7', d))
    assert f.paused() and cause(f) == {'kind': 'subfleet', 'subfleet_run': str(d)}


def test_blocked_on_a_child_notice_at_a_fleet_with_a_parent_is_subfleet(svc):
    f = svc
    d = child(f)
    f.cfg['parent_config'] = str(Path(f.run_dir).parent / 'parent' / 'config.json')
    service_blocked(f, stuck_text('M7', d))
    assert f.paused() and cause(f) == {'kind': 'subfleet', 'subfleet_run': str(d)}


def test_blocked_on_a_notice_about_a_foreign_run_is_supervisor_blocked(svc):
    f = svc
    service_blocked(f, stuck_text('M9', Path(f.run_dir).parent / 'elsewhere' / 'M9'))
    assert f.paused() and cause(f) == {'kind': 'supervisor_blocked'}


def test_root_blocked_on_a_child_notice_writes_no_pause_and_no_cause(svc):
    f = svc
    d = child(f)
    service_blocked(f, stuck_text('M7', d), reason=REASON)
    assert not f.paused() and cause(f) is None


def test_brake_budget_and_wall_carry_their_kind(svc):
    f = svc
    f.brake_trip('budget', 'budget line reached: usd 5.00 of 5.00')
    assert cause(f) == {'kind': 'budget', 'brake_kind': 'budget'}
    f.resume()
    f.brake_trip('wall', 'worker-1 hit a 5h wall ("limit reached")', agent='worker-1')
    assert cause(f) == {'kind': 'wall', 'brake_kind': 'wall', 'slot': 'worker-1'}


def test_v1_service_finish_keeps_no_cause(svc):
    """Section 24 pause review does not cover transport v1: its finish pause has no cause (falls back to text)."""
    f = svc
    v2 = f.v2
    f.pause('earlier', kind='operator')
    f.resume()
    try:
        f.v2 = None
        f.finish('blocked', 'worker-1 startup timed out before READY')
    finally:
        f.v2 = v2
    assert f.paused() and cause(f) is None


# ------------------------------------------------------------------ status
def test_status_shows_cause_and_review_only_when_set(svc):
    f = svc
    out = status_of(f)
    assert 'pause_cause' not in out and 'pause_review' not in out
    f.pause('worker-1: held', kind='slot_hold', slot='worker-1')
    f.r.hset(fleet.SERVICE, 'review', json.dumps({'verdict': 'kept', 'kind': 'slot_hold', 'why': 'still held'}))
    out = status_of(f)
    assert out['pause_cause'] == {'kind': 'slot_hold', 'slot': 'worker-1'}
    assert out['pause_review'] == {'verdict': 'kept', 'kind': 'slot_hold', 'why': 'still held'}
    f.resume()
    out = status_of(f)
    assert 'pause_cause' not in out and 'pause_review' not in out
