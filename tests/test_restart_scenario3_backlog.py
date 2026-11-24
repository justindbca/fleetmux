"""Scenario three (docs/design.md section 60): while the root supervisor was held, errors of other slots, sub-fleets
and applets and messages for the supervisor piled up. The next supervisor, whether it comes from a new generation
in the same run (any path: regen, frozen, fresh session after refusals, handoff) or from a new run after the
operator's stop / start, gets the backlog as reconcile items. Every generation change reconciles; an empty result
sends nothing, and an error a closed ledger already ruled is not listed again unless it happened again.
Private test Redis, fake cmux, per-test fleet home."""
import inspect
import json
import time

import pytest

from restart_kit import all_ready, items, kill_extra, ledger, ledgers, new_run, operator_stop, reconcile_notices, tick
from test_service_attach import root  # noqa: F401  (fixture)
from test_named_subfleet_chain import named_chain_as_before  # noqa: F401  (fixture)
from test_subfleet import given, hook, step, svc  # noqa: F401
from fleetmux import awake, fleet, lifecycle_close, restart_state, watchdog

pytestmark = pytest.mark.usefixtures('named_chain_as_before')


@pytest.fixture
def rt(root, monkeypatch):
    sent = []
    monkeypatch.setattr(lifecycle_close, 'NOTIFIER', lambda rec: sent.append(rec) or 'sent')
    root.sent = sent
    yield root
    kill_extra(root)


def pile_up(f):
    """What the held supervisor never heard: a held worker, an error of a sub-fleet, a failed notice."""
    f.setf('worker-2', state='held', held_since=str(awake.now()), held_at=str(time.time()),
           regen_reason='API error: overloaded')
    f.log('subfleet.stuck', level='error', slot='M3', reason='sub-fleet M3 waits for a human since 40 min')
    f.log('notify.failed', level='error', reason='tg leg: no route')


def next_generation(t, source='regen'):
    """A new supervisor generation of this run (every path ends in Fleet.spawn), then its READY."""
    f = t.f
    f.v2.retire('supervisor', 'test: the old generation is retired as a rotation does (confirm_retired)')
    gen = int(f.agent('supervisor')['generation'])
    f.setf('supervisor', generation=gen + 1, label='supervisor.' + str(gen + 1), state='starting')
    f.spawn('supervisor', 'You are the next supervisor (' + source + '). Reply READY.')
    assert hook(t, f, 'supervisor', 'READY', prompt='startup')['status'] == 'ready'
    tick(t)


def rule_all(t, verdict='recovered', reason='seen'):
    f = t.f
    led = ledger(f)
    restart_state.decide(f, [{'item': i['id'], 'verdict': verdict, 'reason': reason}
                             for i in led['items'] if i['state'] == 'open'], by='operator')


def test_every_supervisor_generation_path_goes_through_the_two_hooks():
    for fn, call in ((watchdog.Watchdog.rotate, 'f.spawn('), (fleet.Fleet.fresh_rotation, 'self.spawn('),
                     (fleet.Fleet.finish_rotation, 'self.spawn('), (fleet.Fleet.wall_rotate, 'self.spawn(')):
        assert call in inspect.getsource(fn), fn.__qualname__
    assert 'restart_state.before_supervisor(self)' in inspect.getsource(fleet.Fleet.spawn)


def test_scenario3_new_generation_in_the_same_run_gets_the_backlog(rt):
    f = rt.f
    tick(rt)  # the first generation's READY is recorded: later generations are reconciled
    pile_up(f)
    next_generation(rt)
    led = ledger(f)
    assert led and led['mode'] == 'generation'
    kinds = {(i['kind'], i['key']) for i in led['items']}
    assert ('slot', 'worker-2') in kinds and ('audit', 'subfleet.stuck:M3') in kinds
    assert ('audit', 'notify.failed:tg leg: no route') in kinds
    note = reconcile_notices(f)[-1]
    assert 'worker-2' in note and 'M3' in note and '"reconcile"' in note


def test_scenario3_held_supervisor_then_operator_restart_hands_the_backlog_to_the_new_run(rt):
    f = rt.f
    tick(rt)
    pile_up(f)
    f.setf('supervisor', state='held', held_since=str(awake.now()), held_at=str(time.time()), regen_state='exhausted',
           regen_reason='regen budget used up')
    f.queue('supervisor', '[sub-fleet held] sub-fleet M3 slot ops-1 is held: dialog on its screen')
    step(rt)  # a held supervisor takes nothing: the message stays undelivered
    operator_stop(rt)
    nf = new_run(rt)
    tick(rt, 2)
    found = {i['kind']: i for i in items(nf)}
    assert found['supervisor_message']['class'] == 'lost' and 'dialog on its screen' in found['supervisor_message'][
        'payload']
    assert any(i['kind'] == 'slot' and i['key'] == 'supervisor' for i in items(nf))
    note = reconcile_notices(nf)[-1]
    assert '[Reconcile L1]' in note and 'supervisor_message' in note


def test_empty_reconcile_sends_nothing_and_a_ruled_error_is_not_listed_again_unless_it_recurs(rt):
    f = rt.f
    tick(rt)
    next_generation(rt, 'handoff')  # a plain handoff with nothing to report
    assert ledgers(f) == [] and reconcile_notices(f) == []
    pile_up(f)
    next_generation(rt)
    rule_all(rt)
    assert ledger(f) is None and ledgers(f)[-1]['closed']
    assert f.agent('worker-2')['state'] == 'held'  # the same hold goes on, it did not happen again
    next_generation(rt, 'handoff')
    assert ledger(f) is None  # ruled and not happened again: not listed
    time.sleep(0.01)
    f.log('subfleet.stuck', level='error', slot='M3', reason='sub-fleet M3 waits for a human since 40 min')
    next_generation(rt, 'handoff')
    again = ledger(f)
    assert again and [i['key'] for i in again['items']] == ['subfleet.stuck:M3']  # it happened again: listed again


def test_audit_errors_of_two_slots_are_two_items(rt):
    f = rt.f
    tick(rt)
    f.log('subfleet.stuck', level='error', slot='M3', reason='sub-fleet M3 waits for a human')
    f.log('subfleet.stuck', level='error', slot='M5', reason='sub-fleet M5 waits for a human')
    next_generation(rt)
    keys = {i['key'] for i in items(f, 'audit')}
    assert {'subfleet.stuck:M3', 'subfleet.stuck:M5'} <= keys


def test_timer_snapshots_between_reconciles_keep_the_whole_backlog_for_a_generation(rt):
    f = rt.f
    tick(rt)
    pile_up(f)
    restart_state.write(f, 'timer')
    restart_state.write(f, 'timer')  # the second timer snapshot still carries the first one's errors
    next_generation(rt)
    keys = {i['key'] for i in items(f, 'audit')}
    assert 'subfleet.stuck:M3' in keys and 'notify.failed:tg leg: no route' in keys


def test_timer_snapshots_before_an_operator_stop_keep_the_whole_backlog_for_the_new_run(rt):
    f = rt.f
    tick(rt)
    pile_up(f)
    restart_state.write(f, 'timer')
    restart_state.write(f, 'timer')
    operator_stop(rt)
    nf = new_run(rt)
    tick(rt, 2)
    keys = {i['key'] for i in items(nf, 'audit')}
    assert 'subfleet.stuck:M3' in keys and 'notify.failed:tg leg: no route' in keys


def test_a_supervisor_message_the_bus_committed_is_not_lost_an_expired_or_cancelled_one_is(rt):
    f = rt.f
    S = {'supervisor': {'m-done': {'text': 'taken'}, 'm-exp': {'text': 'expired before offer'},
                        'm-cxl': {'text': 'offered, then the generation was retired'}}}
    f.r.hset(f.v2.bus.msg_key('m-done'), mapping={'status': 'committed', 'outcome': 'completed'})
    f.r.hset(f.v2.bus.msg_key('m-exp'), mapping={'status': 'committed', 'outcome': 'expired'})
    f.r.hset(f.v2.bus.msg_key('m-cxl'), mapping={'status': 'committed', 'outcome': 'cancelled'})
    got = {i['key']: i for i in restart_state.inflight_items(f, S, {}, False) if i['kind'] == 'supervisor_message'}
    assert 'm-done' not in got and got['m-exp']['class'] == 'lost' and 'expired' in got['m-exp']['evidence']
    assert got['m-cxl']['class'] == 'lost' and 'cancelled' in got['m-cxl']['evidence']


def test_a_message_offered_to_the_retired_generation_reaches_the_next_one_as_an_item(rt):
    f = rt.f
    tick(rt)
    mid = f.queue('supervisor', '[note] offered to the old generation, never answered')
    tick(rt)
    assert (f.v2.bus.slot_state('supervisor') or {}).get('message_id') == mid  # offered, not answered
    next_generation(rt)  # the retire cancels the open delivery
    got = [i for i in items(f, 'supervisor_message') if i['key'] == mid]
    assert got and 'never answered' in got[0]['payload']


def test_dispatched_request_and_the_supervisors_current_message_are_in_the_snapshot(rt):
    f = rt.f
    tick(rt)
    spec = json.dumps({'id': 'q-disp', 'payload': {'message': 'on a worker'}})
    f.r.hset(fleet.REQ + 'q-disp', mapping={'status': 'dispatched', 'worker': 'worker-1', 'spec': spec})
    mid = f.queue('supervisor', '[note] the supervisor holds this one now')
    tick(rt)
    assert mid not in f.v2.bus.pending('supervisor')  # offered: no longer queued
    got = restart_state.collect_inflight(f, 20000)
    assert 'q-disp' in got['requests'] and got['requests']['q-disp']['status'] == 'dispatched'
    assert mid in got['supervisor'] and 'holds this one now' in got['supervisor'][mid]['text']


def test_an_error_after_many_audit_entries_still_reaches_the_snapshot(rt):
    f = rt.f
    tick(rt)
    pipe = f.r.pipeline(transaction=False)
    for n in range(restart_state.AUDIT_PAGE + 100):  # more than one page of entries since the last reconcile
        pipe.xadd(f.v2.bus.audit_key, {'event': 'busy.event', 'n': str(n), 'level': 'info'})
    pipe.execute()
    f.log('subfleet.stuck', level='error', slot='M7', reason='late error after a long busy stretch')
    path = restart_state.write(f, 'timer')
    audit = json.loads(path.read_text())['kinds']['errors']['audit']
    assert [a['slot'] for a in audit if a['event'] == 'subfleet.stuck'] == ['M7']


def test_a_request_the_continued_run_routes_again_is_not_lost_and_never_redone(rt):
    from test_job_queue_restart import continue_root
    f = rt.f
    tick(rt)
    spec = json.dumps({'id': 'q-run', 'payload': {'message': 'on a worker when the root stopped'}})
    f.r.hset(fleet.REQ + 'q-run', mapping={'status': 'dispatched', 'worker': 'worker-1', 'spec': spec})
    operator_stop(rt)
    nf = continue_root(rt)  # the same run and Redis
    nf.r.hset(fleet.REQ + 'q-run', mapping={'status': 'routing', 'worker': ''})  # what replay_request writes
    nf.r.lrem(fleet.REQQ, 0, 'q-run')
    tick(rt, 2)
    assert not items(nf, 'request')  # this run still handles it: not listed, so no redo can run it twice


def test_answered_request_rows_do_not_crowd_a_live_one_out_of_the_snapshot(rt, monkeypatch):
    f = rt.f
    monkeypatch.setattr(restart_state, 'REQUEST_CAP', 5)
    for n in range(40):
        f.r.hset(fleet.REQ + 'q-old%d' % n, mapping={'status': 'answered', 'worker': 'worker-1', 'spec': '{}'})
    f.r.hset(fleet.REQ + 'q-live', mapping={'status': 'dispatched', 'worker': 'worker-2', 'spec': '{}'})
    assert 'q-live' in restart_state.collect_inflight(f, 20000)['requests']
