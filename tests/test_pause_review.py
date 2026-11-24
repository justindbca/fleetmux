"""Pause review (design.md section 24 "Pause review"): a run paused for pause_review_after_s awake seconds is checked
by its daemon, one cause at a time. A cause that no longer holds (the sub-fleet recovered or is gone, the held slot
was released, the dialog is gone, the wall retry time passed) is lifted and audited with the reason and the evidence.
A supervisor fault that still holds is handed to supervisor regeneration. Budget lines, delete dialogs, security,
integrity, operator pauses and general BLOCKED replies are never lifted.

Incident: the root fleet stayed paused for 5 days after its supervisor replied BLOCKED to a stuck sub-fleet notice;
the queued job never started because job_tick returns while the run is paused.

Pause causes are written as the interface agreed with dev-1: ``fx:service.cause`` = JSON {kind, slot?, message_id?,
generation?, subfleet_run?, brake_kind?}. Without a cause the reason text is classified (fallback). Private Redis,
fake cmux, tmp_path. Tests marked WIRING need the runtime_v2.tick call (wiring round); red until then.
"""
import json
import time
import uuid

import pytest

from fleetmux import awake, fleet, pause_review, subfleet
from fleetmux.fleet import BRAKE, SERVICE
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import event, fx, receive, session  # noqa: F401  (fixture)

AGO = 4000


def audit_rows(f):
    return [x for _, x in f.r.xrange(f.v2.bus.audit_key)]


def events(f, name):
    return [x for x in audit_rows(f) if x.get('event') == name]


def paused(f, reason, ago=AGO, **cause):
    """A pause written as Fleet.pause(reason, kind=..., **ref) will write it (interface with dev-1)."""
    f.r.hset(SERVICE, mapping={'paused': 1, 'reason': reason, 'at': time.time() - ago})
    if cause:
        f.r.hset(SERVICE, 'cause', json.dumps(cause))
    else:
        f.r.hdel(SERVICE, 'cause')


def knobs(**over):
    return dict(pause_review.REVIEW_WATCH, **over)


def review(f):
    return json.loads(f.r.hget(SERVICE, 'review') or '{}')


# ------------------------------------------------------------------ parameters and pacing
def test_defaults_and_each_key_validated_on_its_own():
    k, problem = pause_review.knobs({})
    assert problem is None and k == {'pause_review': True, 'pause_review_after_s': 1800,
                                     'pause_review_every_s': 300, 'pause_review_wall_retry_s': 3600}
    k, problem = pause_review.knobs({'pause_review': 1, 'pause_review_after_s': -5, 'pause_review_every_s': 60})
    assert k['pause_review'] is True and k['pause_review_after_s'] == 1800 and k['pause_review_every_s'] == 60
    assert 'pause_review,' in problem + ',' and 'pause_review_after_s' in problem


def test_not_checked_before_after_s_then_paced_by_every_s(fx):
    f = fx
    paused(f, 'paused by operator', ago=100, kind='operator')
    assert pause_review.tick(f.v2, knobs()) is None and not review(f)
    paused(f, 'paused by operator', kind='operator')
    first = pause_review.tick(f.v2, knobs())
    assert first['action'] == 'keep' and review(f)['kind'] == 'operator'
    assert pause_review.tick(f.v2, knobs()) is None, 'checked again before pause_review_every_s'
    assert len(events(f, 'pause_review.kept')) == 1


def test_switch_off_checks_nothing(fx):
    f = fx
    paused(f, 'w: x', kind='slot_hold', slot='worker-1')
    assert pause_review.tick(f.v2, knobs(pause_review=False)) is None and f.paused()


# ------------------------------------------------------------------ never lifted
@pytest.mark.parametrize('reason, cause', [
    ('paused by operator', {'kind': 'operator'}),
    ('brake: budget line reached: usd 10/10', {'kind': 'budget', 'brake_kind': 'budget'}),
    ('ambiguous effect intent held for inspection: e1', {'kind': 'integrity'}),
    ('worker-1 opencode model_lock: x', {'kind': 'opencode', 'slot': 'worker-1'}),
    ('canary tripped: c1', {'kind': 'security'}),
    ('a human must approve the license', {'kind': 'supervisor_blocked'}),
    ('something nobody classified', {}),
])
def test_never_lifted_automatically(fx, reason, cause):
    f = fx
    paused(f, reason, **cause)
    out = pause_review.tick(f.v2, knobs())
    assert out['action'] == 'keep' and f.paused()
    assert (events(f, 'pause_review.unknown') if not cause else events(f, 'pause_review.kept'))


def test_text_fallback_classifies_known_reasons(fx):
    f = fx
    for reason, kind in (('paused by operator', 'operator'),
                         ('brake: budget line reached: usd 10/10', 'budget'),
                         ('ambiguous intent requires inspection: fx:x', 'integrity'),
                         ('same-run daemon restart recovered held; inspect sessions before resuming', 'restart_recovery'),
                         ('canary tripped: c', 'security'), ('security limit: send over 3', 'security'),
                         ('supervisor gave no valid decision JSON three times in a row: x', 'supervisor_fault'),
                         ('held adopted supervisor not released after the resume: x', 'adopted'),
                         ('blocked: worker-2 startup timed out before READY', 'startup'),
                         ('worker-1: dialog trust not answered: x', 'dialog'),
                         ('worker-1: aborted/error/unfinished settlement; execution stop unconfirmed', 'slot_hold')):
        paused(f, reason)
        got = pause_review.classify(f)
        assert got['kind'] == kind and got['source'] == 'text', (reason, got)
        f.r.hset(SERVICE, 'review', '{}')
        out = pause_review.tick(f.v2, knobs())
        assert out['action'] == 'keep' and f.paused(), (reason, out)  # the reason text is never acted on


# ------------------------------------------------------------------ lifted when the cause is gone
def test_slot_hold_lifted_once_the_slot_is_released(fx):
    f = fx
    paused(f, 'worker-1: aborted/error/unfinished settlement; execution stop unconfirmed', kind='slot_hold',
           slot='worker-1', generation=0)
    f.setf('worker-1', state='held')
    assert pause_review.tick(f.v2, knobs())['action'] == 'keep' and f.paused()
    f.setf('worker-1', state='idle')
    f.r.hset(SERVICE, 'review', '{}')  # past every_s
    out = pause_review.tick(f.v2, knobs())
    assert out['action'] == 'lift' and not f.paused()
    row = events(f, 'pause_review.lifted')[-1]
    for key in ('paused_at', 'paused_s', 'reason', 'kind', 'check', 'evidence'):
        assert key in row, row


def test_slot_hold_lifted_when_its_generation_changed(fx):
    f = fx
    paused(f, 'worker-1: held', kind='slot_hold', slot='worker-1', generation=0)
    f.setf('worker-1', state='held', generation=1)
    assert pause_review.tick(f.v2, knobs())['action'] == 'lift'


def test_dialog_gone_is_lifted_a_delete_dialog_pause_never(fx):
    f = fx
    paused(f, 'worker-1: dialog trust not answered: x', kind='dialog', slot='worker-1')
    f.setf('worker-1', prompt_wait='d1', prompt_route='user', prompt_class='other')
    assert pause_review.tick(f.v2, knobs())['action'] == 'keep'
    f.setf('worker-1', prompt_wait='', prompt_class='')
    f.r.hset(SERVICE, 'review', '{}')
    assert pause_review.tick(f.v2, knobs())['action'] == 'lift' and not f.paused()
    paused(f, 'worker-1: dialog rm not answered: the screen shows a delete / move: the user decides', kind='dialog',
           slot='worker-1')
    assert pause_review.tick(f.v2, knobs())['action'] == 'keep' and f.paused()


def test_restart_recovery_lifted_when_nothing_is_held_or_uncertain(fx):
    f = fx
    paused(f, 'same-run daemon restart recovered held; inspect sessions before resuming', kind='restart_recovery')
    f.setf('worker-2', state='held')
    assert pause_review.tick(f.v2, knobs())['action'] == 'keep'
    f.setf('worker-2', state='idle')
    f.r.hset(SERVICE, 'review', '{}')
    assert pause_review.tick(f.v2, knobs())['action'] == 'lift' and not f.paused()


def test_startup_lifted_once_the_slot_is_ready(fx):
    f = fx
    paused(f, 'blocked: worker-2 startup timed out before READY', kind='startup', slot='worker-2')
    f.setf('worker-2', state='dead', runtime_ready=0)
    assert pause_review.tick(f.v2, knobs())['action'] == 'keep'
    f.setf('worker-2', state='idle', runtime_ready=1)
    f.r.hset(SERVICE, 'review', '{}')
    assert pause_review.tick(f.v2, knobs())['action'] == 'lift'


def test_subfleet_gone_or_recovered_is_lifted(fx, tmp_path, monkeypatch):
    f = fx
    child = tmp_path / 'sub' / 'memsub'
    paused(f, 'memsub is stuck', kind='subfleet', subfleet_run=str(child))
    out = pause_review.tick(f.v2, knobs())
    assert out['action'] == 'lift' and 'gone' in out['check']
    child.mkdir(parents=True)
    subfleet.save_record(child, {'state': 'running', 'task_id': 'memsub'})
    fleet.save(child / 'config.json', dict(f.cfg, run=str(child)))
    facts = {'paused': True, 'held': ['mem-3'], 'supervisor_held': False}
    monkeypatch.setattr(subfleet, 'child_progress', lambda cfg, rec: dict(facts))
    paused(f, 'memsub is stuck', kind='subfleet', subfleet_run=str(child))
    assert pause_review.tick(f.v2, knobs())['action'] == 'keep'
    facts.update(paused=False, held=[])
    f.r.hset(SERVICE, 'review', '{}')
    assert pause_review.tick(f.v2, knobs())['action'] == 'lift' and not f.paused()
    paused(f, 'memsub is stuck', kind='subfleet', subfleet_run=str(child))
    subfleet.save_record(child, {'state': 'down', 'task_id': 'memsub'})
    assert pause_review.tick(f.v2, knobs())['action'] == 'lift'


def test_nothing_is_lifted_past_a_budget_line(fx, monkeypatch):
    f = fx
    paused(f, 'worker-1: held', kind='slot_hold', slot='worker-1')
    f.r.hset(BRAKE, mapping={'tripped': json.dumps({'kind': 'budget'}), 'paused_by': ''})
    f.setf('worker-1', state='idle')  # the slot cause is gone, the brake is not
    out = pause_review.tick(f.v2, knobs())
    assert f.paused() and out['action'] == 'keep' and 'brake' in out['check']


# ------------------------------------------------------------------ wall
def test_wall_lifted_after_retry_s_at_most_three_times(fx):
    f = fx
    for n in range(4):
        f.setf('worker-1', wall=json.dumps({'scope': 'account', 'line': 'You have hit your usage limit'}))
        paused(f, 'brake: worker-1 hit a account wall ("You have hit your usage limit"); stop',
               kind='wall', slot='worker-1', brake_kind='wall')
        f.r.hset(BRAKE, mapping={'tripped': json.dumps({'kind': 'wall'}), 'paused_by': json.dumps(
            {'kind': 'wall', 'agent': 'worker-1', 'reason': f.r.hget(SERVICE, 'reason')})})
        out = pause_review.tick(f.v2, knobs())
        if n < 3:
            assert out['action'] == 'lift' and not f.paused() and not f.r.hget(BRAKE, 'tripped'), n
        else:
            assert out['action'] == 'keep' and f.paused() and 'tries' in out['check']


def test_wall_waits_for_retry_s(fx):
    f = fx
    f.setf('worker-1', wall=json.dumps({'scope': 'account', 'line': 'limit'}))
    paused(f, 'brake: worker-1 hit a account wall ("limit"); stop', ago=2000, kind='wall', slot='worker-1',
           brake_kind='wall')
    assert pause_review.tick(f.v2, knobs())['action'] == 'keep'


# ------------------------------------------------------------------ supervisor fault -> regeneration
def test_supervisor_fault_is_handed_to_regeneration(fx):
    f = fx
    f.network_up = lambda *a, **k: True
    old = f.agent('supervisor')['pane']
    paused(f, 'supervisor gave no valid decision JSON three times in a row: x', kind='supervisor_fault',
           slot='supervisor', generation=0)
    out = pause_review.tick(f.v2, knobs())
    assert out['action'] == 'regen' and events(f, 'pause_review.handed_to_regen')
    sup = f.agent('supervisor')
    assert sup['state'] == 'held' and sup.get('regen_due')
    assert json.loads(sup['hold_pause'])['at'] == f.r.hget(SERVICE, 'at')
    for _ in range(2):
        f.v2.tick()  # the existing held watch -> supervisor_regen lifts the handed-over pause and rotates
    assert f.agent('supervisor')['generation'] == '1' and old in f.mgr.killed and not f.paused()


def test_supervisor_fault_with_regen_budget_used_up_is_kept(fx):
    f = fx
    now = awake.now()
    f.setf('supervisor', regen_log=json.dumps([now - 1, now - 2, now - 3]))
    paused(f, 'supervisor gave no valid decision JSON three times in a row: x', kind='supervisor_fault',
           slot='supervisor', generation=0)
    out = pause_review.tick(f.v2, knobs())
    assert out['action'] == 'keep' and 'budget' in out['check'] and f.agent('supervisor')['state'] != 'held'


def test_supervisor_fault_of_an_earlier_generation_is_lifted(fx):
    f = fx
    f.setf('supervisor', generation=2)
    paused(f, 'supervisor gave no valid decision JSON three times in a row: x', kind='supervisor_fault',
           slot='supervisor', generation=1)
    assert pause_review.tick(f.v2, knobs())['action'] == 'lift' and not f.paused()


# ------------------------------------------------------------------ WIRING (runtime_v2.tick) and jobs
def test_wiring_the_daemon_tick_reviews_a_paused_run(fx, monkeypatch):
    f = fx
    monkeypatch.setattr(pause_review, 'knobs', lambda block=None: (knobs(), None))
    paused(f, 'worker-1: held', kind='slot_hold', slot='worker-1', generation=0)
    f.v2.tick()
    assert not f.paused(), 'runtime_v2.tick does not call pause_review.tick yet'


def test_wiring_resume_clears_cause_and_review(fx):
    f = fx
    paused(f, 'paused by operator', kind='operator')
    pause_review.tick(f.v2, knobs())
    f.resume()
    svc = f.r.hgetall(SERVICE)
    assert not svc.get('cause') and not svc.get('review'), 'Fleet.resume must clear cause and review (dev-1)'


def test_security_pause_of_the_channels_carries_its_cause(fx):
    from fleetmux.channels import stop
    stop.pause_service(fx.r, 'security limit: send over 3', now=time.time() - AGO)
    got = pause_review.classify(fx)
    assert got['kind'] == 'security' and got['source'] == 'cause'
    assert pause_review.tick(fx.v2, knobs())['action'] == 'keep' and fx.paused()


def test_shipped_keys_are_read_by_the_daemon_and_never_exported_to_pi():
    from fleetmux import pi_adapter
    k, problem = pause_review.knobs()
    assert problem is None and k == pause_review.REVIEW_WATCH
    assert set(pause_review.KEYS) <= set(pi_adapter.HELD_KEYS)
    assert pi_adapter.daemon_watch() == {'check_interval_s': 120, 'stale_after_s': 120, 'realert_after_s': 600}


def test_wiring_a_slot_hold_pause_carries_its_cause_and_is_lifted_once_released(fx):
    f = fx
    spec = dict(id='q9', payload={'message': 'x'}, to='worker-1', replay=False,
                reply={'required': ['reply'], 'allowed': ['reply']})
    event(f, dict(kind='request', request_id='cmd-q9', spec=spec))
    f.pump()
    env = receive(f, 'worker-1')
    event(f, dict(session(f, 'worker-1'), kind='settled', delivery=env, candidates=[], failed=True, aborted=True,
                  unfinished=False))
    assert f.paused()
    got = pause_review.classify(f)
    assert got['source'] == 'cause' and got['kind'] == 'slot_hold' and got['ref']['slot'] == 'worker-1'
    f.r.hset(SERVICE, 'at', time.time() - AGO)
    f.setf('worker-1', state='idle', hold_pause='')
    assert pause_review.tick(f.v2, knobs())['action'] == 'lift' and not f.paused()


# ------------------------------------------------------------------ P0-01 (review round 1, ruling 29): a later pause
# of a liftable kind (a worker hold, a dialog) never replaces the cause of a run that is already paused.
def first_pause(f, which):
    from fleetmux.channels import stop
    if which == 'operator':
        f.pause('paused by operator', kind='operator')
    elif which == 'security':
        stop.pause_service(f.r, 'canary tripped: c1')
    else:
        f.pause('a human must approve the license', kind='supervisor_blocked')


def later_hold(f, slot='worker-1'):
    f.v2.transaction('t-' + uuid.uuid4().hex, lambda: f.v2.hold(
        slot, 'lease/delivery deadline expired; execution stop unconfirmed'))


def cause_of(f):
    return json.loads(f.r.hget(SERVICE, 'cause') or 'null')


@pytest.mark.parametrize('which', ['operator', 'security', 'supervisor_blocked'])
def test_never_lifted_pause_survives_a_later_worker_hold(fx, which):
    """reviewer-1 r1 repro: the hold's own pause used to overwrite the cause with slot_hold, then got lifted."""
    f = fx
    first_pause(f, which)
    later_hold(f)
    assert f.paused() and cause_of(f)['kind'] == which
    f.r.hset(SERVICE, 'at', time.time() - AGO)
    f.setf('worker-1', state='idle')
    out = pause_review.tick(f.v2, knobs())
    assert f.paused() and out['action'] == 'keep' and out['kind'] == which, out


@pytest.mark.parametrize('which', ['operator', 'security', 'supervisor_blocked'])
def test_never_lifted_pause_survives_a_later_dialog(fx, which):
    f = fx
    first_pause(f, which)
    f.v2.prompt_wait('worker-1', 'd1', 'dialog trust not answered: x', '/tmp/screen.txt', kind='other')
    assert f.paused() and cause_of(f)['kind'] == which
    f.r.hset(SERVICE, 'at', time.time() - AGO)
    f.setf('worker-1', prompt_wait='', prompt_class='', prompt_pause='')
    out = pause_review.tick(f.v2, knobs())
    assert f.paused() and out['action'] == 'keep' and out['kind'] == which, out


def test_slot_hold_then_operator_pause_becomes_operator_and_is_kept(fx):
    f = fx
    later_hold(f)
    assert cause_of(f)['kind'] == 'slot_hold'
    f.pause('paused by operator', kind='operator')
    assert cause_of(f) == {'kind': 'operator'}
    f.r.hset(SERVICE, 'at', time.time() - AGO)
    f.setf('worker-1', state='idle')
    out = pause_review.tick(f.v2, knobs())
    assert f.paused() and out['action'] == 'keep' and out['kind'] == 'operator', out


def test_a_later_slot_hold_on_a_pause_without_cause_writes_no_cause_and_is_kept(fx):
    """An empty cause is unknown (never lifted); a later liftable pause must not make it liftable."""
    f = fx
    f.pause('an old caller without a kind')
    later_hold(f)
    assert f.paused() and not f.r.hget(SERVICE, 'cause')
    f.r.hset(SERVICE, 'at', time.time() - AGO)
    f.setf('worker-1', state='idle')
    out = pause_review.tick(f.v2, knobs())
    assert f.paused() and out['action'] == 'keep', out


def test_after_resume_a_new_slot_hold_writes_its_cause_and_is_lifted_once_released(fx):
    f = fx
    f.pause('paused by operator', kind='operator')
    f.resume()
    later_hold(f)
    assert cause_of(f)['kind'] == 'slot_hold' and cause_of(f)['slot'] == 'worker-1'
    f.r.hset(SERVICE, 'at', time.time() - AGO)
    f.setf('worker-1', state='idle')
    out = pause_review.tick(f.v2, knobs())
    assert out['action'] == 'lift' and not f.paused(), out


def test_liftable_and_never_lifted_kinds_are_one_table():
    assert set(pause_review.LIFTABLE) | set(pause_review.NEVER) >= set(pause_review.KINDS)
    assert not set(pause_review.LIFTABLE) & set(pause_review.NEVER)
    assert set(pause_review.LIFTABLE) == {'supervisor_fault', 'subfleet', 'wall', 'slot_hold', 'dialog',
                                          'restart_recovery', 'startup', 'adopted'}


# ------------------------------------------------------------------ ruling 30: another slot's own pause reason blocks
# a slot_hold / dialog / restart_recovery lift (a hold that pauses on its own, or a dialog waiting for the user).
def worker1_paused_then_released(f):
    later_hold(f, 'worker-1')
    assert cause_of(f)['slot'] == 'worker-1'


def review_after(f):
    f.r.hset(SERVICE, 'at', time.time() - AGO)
    f.r.hset(SERVICE, 'review', '{}')
    return pause_review.tick(f.v2, knobs())


def test_a_second_pausing_hold_blocks_the_lift_and_names_the_slot(fx):
    f = fx
    worker1_paused_then_released(f)
    later_hold(f, 'worker-2')  # held while paused: alone it would have paused the fleet
    f.setf('worker-1', state='idle')
    out = review_after(f)
    assert out['action'] == 'keep' and f.paused() and 'worker-2' in out['check'], out
    row = events(f, 'pause_review.kept')[-1]
    assert 'worker-2' in row['check'] and 'worker-2' in row['evidence'], row
    f.setf('worker-2', state='idle')
    out = review_after(f)
    assert out['action'] == 'lift' and not f.paused(), out


@pytest.mark.parametrize('how', ['request_timeout', 'requeue_wait'])
def test_a_slot_only_hold_does_not_block_the_lift(fx, how):
    f = fx
    worker1_paused_then_released(f)
    f.v2.transaction('t-' + uuid.uuid4().hex, lambda: f.v2.hold(
        'worker-2', 'request timeout; execution stop unconfirmed', pause=False))  # section 43: only this slot
    if how == 'requeue_wait':
        f.setf('worker-2', requeue_due=json.dumps({'at': awake.now(), 'message_id': '', 'cause': 'aborted'}))
    f.setf('worker-1', state='idle')
    out = review_after(f)
    assert out['action'] == 'lift' and not f.paused(), out


def test_another_slot_waiting_on_a_user_dialog_blocks_the_lift(fx):
    f = fx
    worker1_paused_then_released(f)
    f.setf('worker-2', prompt_wait='d2', prompt_route='user', prompt_class='other')
    f.setf('worker-1', state='idle')
    out = review_after(f)
    assert out['action'] == 'keep' and f.paused() and 'worker-2' in out['check'], out


def test_restart_recovery_is_blocked_by_a_user_dialog(fx):
    f = fx
    paused(f, 'same-run daemon restart recovered held; inspect sessions before resuming', kind='restart_recovery')
    f.setf('worker-2', prompt_wait='d2', prompt_route='user', prompt_class='other')
    out = pause_review.tick(f.v2, knobs())
    assert out['action'] == 'keep' and 'worker-2' in out['check'], out
