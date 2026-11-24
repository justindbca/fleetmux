"""Held requeue (design.md section 24 "Held requeue of an interrupted request"): a service worker whose pure request
turn was interrupted (Escape / lease expiry / failed renewal) and held as "execution stop unconfirmed" is replaced by
the daemon with its next generation (kill and verify), and the request goes to it once more within max_attempts.
Only that slot is held; the fleet is never paused for it. Giving up (budget used up, the old session not proven
stopped, a delete dialog, a tab that is gone) is exactly today's behaviour: failed published, paused, escalated.

Incident: sub-fleet memsub, pi worker mem-3, a memory-extraction request; Escape after 153 s of silence; the turn ended
aborted; the slot was held, memsub paused, its daemon could not recover it (pi has no hold exit) and reported upwards.

Private Redis (test_message_bus_v2.private_redis_v2), fake cmux (test_fleet.FakeMgr), tmp_path run directories.
Tests marked WIRING need the runtime_v2.py wiring round (settle / held_watch_tick call held_requeue); they are red
until then.
"""
import json
import uuid

import pytest

from fleetmux import awake, fleet, held_requeue, runtime_v2
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import event, fx, receive, session  # noqa: F401  (fixture)

REASON = 'aborted/error/unfinished settlement; execution stop unconfirmed'


def audit_rows(f):
    return [x for _, x in f.r.xrange(f.v2.bus.audit_key)]


def events(f, name):
    return [x for x in audit_rows(f) if x.get('event') == name]


def result(f, rid):
    raw = f.r.get(fleet.RESULT + rid)
    return json.loads(raw) if raw else None


def ask(f, rid='q1', to='worker-1', **extra):
    """A pure question routed to ``to`` (the incident's memory extraction: timeout_s 900, one answer)."""
    spec = dict(id=rid, payload={'message': 'extract the memories of this note'}, to=to, timeout_s=900,
                reply={'required': ['reply'], 'allowed': ['reply']}, **extra)
    event(f, dict(kind='request', request_id='cmd-' + rid, spec=spec))
    f.pump()
    env = receive(f, to)
    assert env and f.r.hget(fleet.REQ + rid, 'status') == 'dispatched'
    return env


def interrupted(f, env, slot='worker-1', **flags):
    """What pi sends after the watchdog's Escape: stopReason aborted -> failed and aborted, no tool running."""
    ev = dict(session(f, slot), kind='settled', delivery=env, candidates=[], failed=True, aborted=True,
              unfinished=False)
    ev.update(flags)
    event(f, ev)


def held_for_requeue(f, env, slot='worker-1', cause='aborted'):
    """The hold the wiring makes for an eligible request (no public result, no pause) plus the requeue mark."""
    f.v2.transaction('t-' + uuid.uuid4().hex, lambda: f.v2.hold(slot, REASON, public_status=None, pause=False))
    held_requeue.mark(f, slot, f.v2.bus.message(env['message_id']), cause)
    assert f.agent(slot)['state'] == 'held' and not f.paused()


def knobs(**over):
    return dict(held_requeue.REQUEUE_WATCH, **over)


def due_now(f, slot='worker-1', ago=60):
    mark = json.loads(f.agent(slot)['requeue_due'])
    mark['at'] = awake.now() - ago
    f.setf(slot, requeue_due=json.dumps(mark))


def ready(f, slot):
    meta = session(f, slot)
    event(f, dict(meta, kind='bridge_session', request_id='register-' + slot + '-' + str(meta['generation'])))
    f.v2.bus.register_slot(slot, meta['generation'], meta['thread'])
    event(f, dict(meta, kind='startup', text='READY', failed=False, aborted=False))
    event(f, dict(meta, kind='availability', idle=True))


def answer(f, slot, env, text='ok'):
    event(f, dict(session(f, slot), kind='settled', delivery=env, failed=False, aborted=False, unfinished=False,
                  candidates=[{'tool': 'fleet_result', 'value': {'reply': text}}]))


# ------------------------------------------------------------------ parameters
def test_defaults_and_each_key_validated_on_its_own():
    k, problem = held_requeue.knobs({})
    assert problem is None
    assert k['held_requeue'] is True and k['held_requeue_after_s'] == 30 and k['held_requeue_max'] == 3
    assert k['held_requeue_window_s'] == 3600
    assert tuple(k['held_requeue_causes']) == ('aborted', 'lease_expired', 'renew_failed')
    k, problem = held_requeue.knobs({'held_requeue': 'yes', 'held_requeue_max': 0, 'held_requeue_after_s': 5,
                                     'held_requeue_causes': ['aborted', 'nonsense']})
    assert k['held_requeue'] is True and k['held_requeue_max'] == 3 and k['held_requeue_after_s'] == 5
    assert tuple(k['held_requeue_causes']) == ('aborted', 'lease_expired', 'renew_failed')
    for key in ('held_requeue', 'held_requeue_max', 'held_requeue_causes'):
        assert key in problem
    k, problem = held_requeue.knobs({'held_requeue_causes': ['aborted']})
    assert problem is None and tuple(k['held_requeue_causes']) == ('aborted',)


def test_settle_cause_names_the_interruption():
    assert held_requeue.settle_cause({'failed': True, 'aborted': True, 'unfinished': False}) == 'aborted'
    assert held_requeue.settle_cause({'failed': True, 'aborted': False, 'unfinished': False}) == 'failed'
    assert held_requeue.settle_cause({'failed': True, 'aborted': True, 'unfinished': True}) == 'unfinished'
    assert held_requeue.settle_cause({}) == 'unconfirmed'


# ------------------------------------------------------------------ the rule
def test_a_pure_service_request_interrupted_is_eligible(fx):
    f = fx
    env = ask(f)
    msg = f.v2.bus.message(env['message_id'])
    assert held_requeue.eligible(f, 'worker-1', msg, 'aborted', knobs()) == (True, '')
    for cause in ('lease_expired', 'renew_failed'):
        assert held_requeue.eligible(f, 'worker-1', msg, cause, knobs())[0]


@pytest.mark.parametrize('case, want', [
    ('task_mode', 'task mode'),
    ('supervisor', 'not a worker'),
    ('cause_failed', 'not in held_requeue_causes'),
    ('cause_unfinished', 'not in held_requeue_causes'),
    ('tool_running', 'a tool was still running'),
    ('replay_false', 'replay: false'),
    ('operator', 'operator request'),
    ('applet', 'applet lease'),
    ('midturn', 'mid-turn'),
    ('other_worker', 'not dispatched to this slot'),
    ('attempts_used', 'attempts used'),
    ('switch_off', 'held_requeue is off'),
])
def test_what_is_not_eligible_stays_held_and_reported(fx, case, want):
    f = fx
    env = ask(f, replay=False) if case == 'replay_false' else ask(f)
    msg = dict(f.v2.bus.message(env['message_id']))
    slot, cause, k, ev = 'worker-1', 'aborted', knobs(), None
    if case == 'task_mode':
        f.service = False
    elif case == 'supervisor':
        slot = 'supervisor'
    elif case == 'cause_failed':
        cause = 'failed'
    elif case == 'cause_unfinished':
        cause = 'unfinished'
    elif case == 'tool_running':
        ev = {'failed': True, 'aborted': True, 'unfinished': True}
    elif case == 'operator':
        f.r.hset(fleet.REQ + 'q1', 'operator', '1')
    elif case == 'applet':
        f.r.hset('fx:agent:worker-1', 'applet_lease', 'L1')
    elif case == 'midturn':
        msg['midturn_id'] = 'mt-1'
    elif case == 'other_worker':
        f.r.hset(fleet.REQ + 'q1', 'worker', 'worker-2')
    elif case == 'attempts_used':
        f.r.hset(fleet.REQ + 'q1', 'replay', str(f.cfg['max_attempts']))
    elif case == 'switch_off':
        k = knobs(held_requeue=False)
    ok, why = held_requeue.eligible(f, slot, msg, cause, k, ev=ev)
    assert not ok and want in why, why


def test_tmux_view_tab_cannot_prove_the_cli_stopped(fx):
    """fleetmux / cmux kill_pane on a tmux *view* tab ends only its client, never the session (backends/fleetmux.py
    kill_pane docstring): no proof that the CLI stopped, so the slot is not requeued. An owner tab ends the session."""
    class Mgr:
        def __init__(self, role):
            self.role = role

        def tmux_view_info(self, pane):
            return {'tmux_socket': '/tmp/x', 'tmux_session': 's'} if self.role else None

        def _tmux_of(self, pane):
            return {'tmux_role': self.role} if self.role else {}
    assert held_requeue.retire_provable(Mgr(None), 'p1') == (True, '')
    assert held_requeue.retire_provable(Mgr('owner'), 'p1') == (True, '')
    ok, why = held_requeue.retire_provable(Mgr('view'), 'p1')
    assert not ok and 'tmux view' in why


# ------------------------------------------------------------------ the step (module, wiring-free)
def test_requeue_waits_for_after_s_then_regenerates_and_replays_once(fx):
    f = fx
    old_pane = f.agent('worker-1')['pane']
    env = ask(f)
    held_for_requeue(f, env)
    assert held_requeue.step(f.v2, 'worker-1', knobs()) == 'waiting'
    assert f.agent('worker-1')['generation'] == '0'
    due_now(f)
    assert held_requeue.step(f.v2, 'worker-1', knobs()) == 'started'
    w = f.agent('worker-1')
    assert w['generation'] == '1' and old_pane in f.mgr.killed and w['pane'] != old_pane
    msg = f.v2.bus.message(env['message_id'])
    assert msg['status'] == 'committed' and msg.get('outcome') == 'cancelled' and msg.get('stop_confirmed') == '1'
    assert f.r.hget(fleet.REQ + 'q1', 'replay') == '2' and result(f, 'q1') is None
    assert not f.paused() and not w.get('requeue_due') and not w.get('held_since')
    assert events(f, 'held_requeue.started') and events(f, 'held_requeue.replayed')
    assert not events(f, 'held_watch.escalated')
    rows = [x for x in audit_rows(f) if x.get('event') == 'result']
    assert not rows, 'nothing published before the replay settles'
    ready(f, 'worker-1')
    f.pump()
    again = receive(f, 'worker-1')
    assert again and 'extract the memories' in f.v2.bus.message(again['message_id'])['body']['text']
    answer(f, 'worker-1', again, 'three memories')
    assert result(f, 'q1')['status'] == 'ok'


def test_hold_exit_first_means_no_regeneration(fx):
    f = fx
    env = ask(f)
    held_for_requeue(f, env)
    f.setf('worker-1', state='idle')  # e.g. a Claude hook slot's turn_ended -> release_held
    due_now(f)
    assert held_requeue.step(f.v2, 'worker-1', knobs()) == 'released'
    assert f.agent('worker-1')['generation'] == '0' and not f.mgr.killed


def test_the_old_session_not_proven_stopped_gives_up_to_todays_behaviour(fx):
    f = fx
    env = ask(f)
    held_for_requeue(f, env)
    due_now(f)
    f.mgr.kill_pane = lambda pane: False
    assert held_requeue.step(f.v2, 'worker-1', knobs()) == 'gave_up'
    assert result(f, 'q1')['status'] == 'failed' and f.paused()
    esc = events(f, 'held_watch.escalated')
    assert esc and json.loads(esc[-1]['detail'])['reason'] == 'not_recovered'
    assert events(f, 'held_requeue.gave_up')


def test_budget_used_up_gives_up(fx):
    f = fx
    env = ask(f)
    held_for_requeue(f, env)
    due_now(f)
    now = awake.now()
    f.setf('worker-1', requeue_log=json.dumps([now - 10, now - 20, now - 30]))
    assert held_requeue.step(f.v2, 'worker-1', knobs()) == 'gave_up'
    assert result(f, 'q1')['status'] == 'failed' and f.paused() and f.agent('worker-1')['generation'] == '0'
    assert 'budget' in events(f, 'held_requeue.gave_up')[-1]['error']


def test_an_operator_pause_waits_without_spending_a_count(fx):
    f = fx
    env = ask(f)
    held_for_requeue(f, env)
    due_now(f)
    f.pause('paused by operator')
    assert held_requeue.step(f.v2, 'worker-1', knobs()) == 'waiting'
    assert not json.loads(f.agent('worker-1').get('requeue_log') or '[]') and f.agent('worker-1')['generation'] == '0'
    f.resume()
    assert held_requeue.step(f.v2, 'worker-1', knobs()) == 'started'


def test_delete_dialog_on_the_screen_gives_up_and_presses_nothing(fx):
    f = fx
    env = ask(f)
    held_for_requeue(f, env)
    due_now(f)
    f.mgr.capture_output = lambda pane, lines=200: 'Delete 3 files?\n❯ 1. Yes\n  2. No\nEsc to cancel'
    assert held_requeue.step(f.v2, 'worker-1', knobs()) == 'gave_up'
    assert not f.mgr.keys and not f.mgr.killed and f.paused()


def test_request_expired_while_held_regenerates_without_replay(fx):
    f = fx
    env = ask(f)
    held_for_requeue(f, env)
    due_now(f)
    f.v2.publish('q1', 'timeout', worker='worker-1', error='no valid answer within 900 s')
    assert held_requeue.step(f.v2, 'worker-1', knobs()) == 'started'
    assert f.agent('worker-1')['generation'] == '1' and result(f, 'q1')['status'] == 'timeout'
    assert not events(f, 'held_requeue.replayed') and not f.paused()


def test_rotation_records_requeue_in_history_and_publishes_no_stalled(fx):
    f = fx
    env = ask(f)
    held_for_requeue(f, env)
    due_now(f)
    held_requeue.step(f.v2, 'worker-1', knobs())
    done = events(f, 'watch.frozen_restarted')
    assert done and 'held' in done[-1]['reason']
    assert result(f, 'q1') is None, 'a requeued request is never published stalled'
    rows = [json.loads(x) for x in (f.run_dir / 'history.jsonl').read_text().splitlines() if x.strip()]
    assert any(x.get('source') == 'requeue' and x.get('slot') == 'worker-1' for x in rows), rows


# ------------------------------------------------------------------ WIRING (runtime_v2.settle / held_watch_tick)
def test_wiring_interrupted_pure_request_holds_only_its_slot(fx):
    f = fx
    env = ask(f)
    interrupted(f, env)
    assert f.agent('worker-1')['state'] == 'held'
    assert not f.paused(), 'the fleet was paused for a re-dispatchable request (incident: memsub paused)'
    assert result(f, 'q1') is None and f.r.hget(fleet.REQ + 'q1', 'status') == 'dispatched'
    assert f.agent('worker-1').get('requeue_due')


def test_wiring_held_watch_requeues_and_the_answer_lands(fx):
    f = fx
    env = ask(f)
    interrupted(f, env)
    due_now(f)
    f.v2.tick()
    assert f.agent('worker-1')['generation'] == '1' and f.r.hget(fleet.REQ + 'q1', 'replay') == '2'
    ready(f, 'worker-1')
    f.pump()
    again = receive(f, 'worker-1')
    answer(f, 'worker-1', again)
    assert result(f, 'q1')['status'] == 'ok' and not f.paused()


def test_todays_behaviour_for_an_api_error_turn_not_in_the_default_causes(fx):
    """failed without aborted (an API / model error) is not in the default list: published failed, paused."""
    f = fx
    env = ask(f)
    interrupted(f, env, aborted=False)
    assert f.agent('worker-1')['state'] == 'held' and f.paused() and result(f, 'q1')['status'] == 'failed'


def test_todays_behaviour_when_a_tool_was_still_running(fx):
    f = fx
    env = ask(f)
    interrupted(f, env, unfinished=True)
    assert f.paused() and result(f, 'q1')['status'] == 'failed'


def test_shipped_keys_are_read_by_the_daemon_and_never_exported_to_pi():
    from fleetmux import pi_adapter
    k, problem = held_requeue.knobs()
    assert problem is None and k == dict(held_requeue.REQUEUE_WATCH)
    assert set(held_requeue.KEYS) <= set(pi_adapter.HELD_KEYS)
    assert pi_adapter.daemon_watch() == {'check_interval_s': 120, 'stale_after_s': 120, 'realert_after_s': 600}


def test_wiring_resume_answers_a_requeue_slot_and_runs_it_on_the_next_tick(fx):
    f = fx
    env = ask(f)
    interrupted(f, env)
    out = f.v2.resume_held()
    assert [x for x in out['regenerating'] if x['slot'] == 'worker-1' and x.get('requeue')], out
    assert not [x for x in out['held'] if x['slot'] == 'worker-1'], out
    f.v2.tick()
    assert f.agent('worker-1')['generation'] == '1' and f.r.hget(fleet.REQ + 'q1', 'replay') == '2'
