"""Supervisor regeneration (design.md §24 "Supervisor regeneration"): a root fleet's supervisor that is held and does not
recover is replaced by the daemon with its next generation, the same way for a pi and a Claude supervisor.

Incident shape: a root service fleet's pi supervisor got `fetch failed` / `Retry failed after 3 attempts` on a
followup notice; its turn ended failed, the slot was held (execution stop unconfirmed) and stayed held for days: the
held watchdog could not prove the stop (pi has no hook channel) and reported to the held supervisor itself. A Claude
supervisor whose turn ended with `API Error: Connection refused` was retried in the same session forever.

Now: the old session is ended (kill and verify, screen kept in history), the next generation `supervisor-1.<N+1>`
starts launch-held, and the unsettled message goes to it once with a "previous generation did not finish" note.

Stubbed: fake cmux, private Redis under /tmp, in-process daemon; tests/conftest.py pins FLEETMUX_HOME to tmp_path.
"""
import json
import time

import pytest

from fleetmux import awake, fleet, runtime_v2
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import event, fx, receive, req, session  # noqa: F401  (fixture)
from test_claude_supervisor_hook import operator, supervisor  # noqa: F401  (fixture)
from test_claude_hook_delivery import delivered


def audit_rows(f):
    return [x for _, x in f.r.xrange(f.v2.bus.audit_key)]


def events(f, name):
    return [x for x in audit_rows(f) if x.get('event') == name]


def pending_texts(f, slot):
    return [f.v2.bus.message(m)['body'].get('text') or '' for m in f.v2.bus.pending(slot)]


def queued_texts(f, slot):
    out = []
    for x in events(f, 'queued'):
        if x.get('slot') == slot:
            try:
                out.append(json.loads(x.get('detail') or '{}').get('text') or '')
            except ValueError:
                pass
    return out


def online(f, up=True):
    f.network_up = lambda *a, **k: up


# ------------------------------------------------------------------ pi supervisor (the incident)
def pi_held(f, error='fetch failed\nRetry failed after 3 attempts', notice='[sub-fleet report] M3 finished its run.'):
    """A followup notice delivered to the pi supervisor; its turn ends failed (model error); the slot is held."""
    f.v2.queue('supervisor', notice, kind='followup')
    f.pump()
    env = receive(f, 'supervisor')
    assert env and f.v2.bus.message(env['message_id'])['kind'] == 'followup'
    if error:
        event(f, dict(session(f, 'supervisor'), kind='candidate', delivery=env,
                      candidate={'tool': 'model', 'error': error}))
    event(f, dict(session(f, 'supervisor'), kind='settled', delivery=env, candidates=[],
                  failed=True, aborted=False, unfinished=False))
    assert f.agent('supervisor')['state'] == 'held'
    return env


def ready(f, slot='supervisor'):
    meta = session(f, slot)
    event(f, dict(meta, kind='bridge_session', request_id='register-' + slot + '-' + str(meta['generation'])))
    f.v2.bus.register_slot(slot, meta['generation'], meta['thread'])
    event(f, dict(meta, kind='startup', text='READY', failed=False, aborted=False))
    event(f, dict(meta, kind='availability', idle=True))


def assert_regenerated(f, old_pane, env, marker):
    sup = f.agent('supervisor')
    assert sup['generation'] == '1' and sup['label'] == 'supervisor-1.1', sup
    assert old_pane in f.mgr.killed and sup['pane'] != old_pane
    msg = f.v2.bus.message(env['message_id'])
    assert msg['status'] == 'committed' and msg.get('outcome') == 'cancelled' and msg.get('stop_confirmed') == '1'
    head = pending_texts(f, 'supervisor')
    assert len(head) == 1 and marker in head[0] and 'Previous generation did not finish' in head[0], head
    assert events(f, 'supervisor_regen.done')
    assert not any('HELD' in x for x in queued_texts(f, 'supervisor')), 'told the held supervisor about itself'


def test_pi_root_supervisor_held_without_stop_proof_is_regenerated(fx):
    f = fx
    online(f)
    old_pane = f.agent('supervisor')['pane']
    env = pi_held(f)
    assert f.paused()
    for _ in range(2):
        f.v2.tick()
    assert_regenerated(f, old_pane, env, 'M3 finished its run')
    assert not f.paused(), 'the pause the hold itself set stays'
    assert f.agent('supervisor')['state'] == 'starting'
    ready(f)
    assert f.agent('supervisor')['state'] == 'idle'
    f.pump()
    got = receive(f, 'supervisor')
    assert got and 'M3 finished its run' in f.v2.bus.message(got['message_id'])['body']['text']


def test_pi_root_supervisor_held_without_error_text_is_regenerated_after_held_after_s(fx):
    f = fx
    online(f)
    old_pane = f.agent('supervisor')['pane']
    env = pi_held(f, error=None)
    f.v2.tick()  # the held watch stamps the hold
    assert f.agent('supervisor')['generation'] == '0', 'regenerated before held_after_s'
    f.setf('supervisor', held_since=awake.now() - 400)
    f.v2.tick()
    assert_regenerated(f, old_pane, env, 'M3 finished its run')


# ------------------------------------------------------------------ Claude supervisor ended by an API error
def claude_failure(f, text='API Error: Connection refused (ECONNREFUSED)'):
    thread = f.test_threads['supervisor']
    fleet.claude_stop_failure(f.path, 'supervisor', 0, json.dumps(
        {'session_id': thread, 'error': 'server_error', 'last_assistant_message': text}))
    for _ in range(5):
        f.v2.step(20)


@pytest.mark.parametrize('supervisor', ['task'], indirect=True)
def test_claude_root_supervisor_api_error_turn_is_regenerated_after_retries(supervisor):
    f = supervisor
    f.cfg['max_attempts'] = 1  # the same-session retry is used up at the first failure
    online(f)
    assert '已排队' in operator(f, 'marker one')
    delivered(f, f.ready_hook)
    before = f.v2.bus.slot_state('supervisor')
    old_pane = f.agent('supervisor')['pane']
    claude_failure(f)
    sup = f.agent('supervisor')
    assert sup['state'] == 'held' and sup.get('stopfail_at')
    for _ in range(2):
        f.v2.tick()
    sup = f.agent('supervisor')
    assert sup['generation'] == '1' and sup['label'] == 'supervisor-1.1', sup
    assert old_pane in f.mgr.killed
    assert f.v2.bus.message(before['message_id'])['status'] == 'committed'  # settled failed by api_error_retry
    head = pending_texts(f, 'supervisor')
    assert len(head) == 1 and 'marker one' in head[0] and 'Previous generation did not finish' in head[0], head
    copy = f.v2.bus.message(f.v2.bus.pending('supervisor')[0])
    assert int(copy['deadline_at']) == runtime_v2.NO_DEADLINE_MS, 'a long network wait could expire the copy'
    assert not sup.get('stopfail_at') and events(f, 'supervisor_regen.done')
    assert not f.paused()


# ------------------------------------------------------------------ helpers for the guards
def regenerated(f):
    return f.agent('supervisor')['generation'] != '0'


def control(f, kind, rid, **extra):
    event(f, dict(kind=kind, request_id=rid, **extra))
    return json.loads(f.r.get('fx:reply:' + rid))


def ticks(f, n=2):
    for _ in range(n):
        f.v2.tick()


# ------------------------------------------------------------------ (4) pauses regeneration never lifts
@pytest.mark.parametrize('who', ['operator', 'brake', 'wall', 'blocked'])
def test_regen_never_lifts_a_pause_someone_else_set(fx, who):
    f = fx
    online(f)
    pi_held(f)
    time.sleep(0.01)
    if who == 'operator':
        f.pause('paused by operator')
    elif who == 'blocked':
        f.v2.finish('blocked', 'supervisor replied BLOCKED')  # service: a pause
    else:
        f.r.hset(fleet.BRAKE, mapping={'tripped': '1'} if who == 'brake' else {'paused_by': 'wall'})
        f.pause('brake: ' + who)
    ticks(f)
    assert not regenerated(f) and f.paused()
    assert not events(f, 'supervisor_regen.started')
    assert 'paused' in f.agent('supervisor').get('regen_state', '')
    assert not f.agent('supervisor').get('regen_log'), 'a refused regeneration spent a count'


# ------------------------------------------------------------------ (5) not stuck / hands off
def test_user_dialog_wait_is_escalated_never_regenerated(fx):
    """A supervisor waiting on a user dialog (prompt_wait, route user) is escalated only. (On a held slot the watchdog
    drops the marker as hands-off; there the screen check below is the guard.)"""
    f = fx
    online(f)
    f.v2.queue('supervisor', '[sub-fleet report] M3 finished its run.', kind='followup')
    f.pump()
    env = receive(f, 'supervisor')
    f.setf('supervisor', prompt_wait=env['delivery_id'], prompt_route='user', prompt_wait_at=time.time() - 400,
           prompt_class='delete')
    ticks(f, 3)
    assert not regenerated(f) and not f.mgr.killed and not events(f, 'supervisor_regen.started')


@pytest.mark.parametrize('screen', ['Bash command\n  rm -rf build/\nDo you want to proceed?\n❯ 1. Yes\n  2. No',
                                    'Edit file\nDo you want to make this edit to a.py?\n❯ 1. Yes\n  2. No'])
def test_dialog_on_held_supervisor_screen_is_escalated_never_regenerated(fx, screen):
    f = fx
    online(f)
    f.mgr.capture_output = lambda pane, lines=2000: screen
    pi_held(f)
    ticks(f, 3)
    assert not regenerated(f) and not f.mgr.killed
    assert events(f, 'held_watch.escalated')


def test_session_record_moved_since_the_hold_is_not_regenerated(fx):
    f = fx
    online(f)
    pi_held(f, error=None)
    f.v2.tick()
    f.setf('supervisor', held_since=awake.now() - 400)
    f.last_activity = lambda info: time.time() + 100  # the session wrote after the hold: a new episode
    f.v2.tick()
    assert not regenerated(f)


def test_api_error_end_whose_session_then_moves_is_not_regenerated(fx):
    """regen_due skips held_after_s only while the session stays still: offline first (no regeneration, no count),
    then the session writes again -> a new episode, regen_due cleared, nothing regenerated when the network is back."""
    f = fx
    online(f, False)
    pi_held(f)
    ticks(f)
    assert not regenerated(f) and f.agent('supervisor')['regen_state'] == 'waiting_network'
    f.last_activity = lambda info: time.time() + 100
    online(f)
    f.setf('supervisor', regen_next_at=awake.now() - 1)
    ticks(f, 3)
    assert not regenerated(f) and not f.agent('supervisor').get('regen_due')
    assert not events(f, 'supervisor_regen.started')


IDLE_SCREEN = 'tests/fixtures/watchdog_screens/channels-approve-1-g0-1794168610498-false.txt'


def test_idle_input_box_with_a_delete_word_above_is_regenerated(fx):
    """The screen ends in the CLI's own input box; 'delete ... rm' in the scrollback above is history, not a dialog."""
    from pathlib import Path
    f = fx
    online(f)
    screen = (Path(__file__).resolve().parent.parent / IDLE_SCREEN).read_text(encoding='utf-8')
    f.mgr.capture_output = lambda pane, lines=2000: screen
    old_pane = f.agent('supervisor')['pane']
    env = pi_held(f)
    ticks(f)
    assert_regenerated(f, old_pane, env, 'M3 finished its run')


def test_stop_failure_backoff_ahead_is_not_regenerated(fx):
    f = fx
    online(f)
    pi_held(f)
    f.setf('supervisor', stopfail_at=time.time(), stopfail_retry_at=awake.deadline(300))
    ticks(f, 3)
    assert not regenerated(f)


def test_regen_backoff_ahead_then_a_second_regeneration(fx):
    f = fx
    online(f)
    pi_held(f)
    ticks(f)
    assert regenerated(f)
    ready(f)
    f.pump()
    env = receive(f, 'supervisor')
    event(f, dict(session(f, 'supervisor'), kind='candidate', delivery=env, candidate={'tool': 'model', 'error': 'fetch failed'}))
    event(f, dict(session(f, 'supervisor'), kind='settled', delivery=env, candidates=[], failed=True, aborted=False,
                  unfinished=False))
    ticks(f, 3)
    assert f.agent('supervisor')['generation'] == '1', 'regenerated again inside its backoff'
    f.setf('supervisor', regen_next_at=awake.now() - 1)
    ticks(f)
    assert f.agent('supervisor')['generation'] == '2'
    assert len(json.loads(f.agent('supervisor')['regen_log'])) == 2


@pytest.mark.parametrize('mark', [dict(link_lost='1'), dict(handoff_id='h-1', handoff_deadline=time.time() + 3600),
                                  dict(fresh_pending='1'),
                                  dict(stopfail_fresh='1')])
def test_other_owners_keep_the_slot(fx, mark):
    """§35 link restart, a handoff in progress, a model refusal's fresh session (§37) own the slot: no regen."""
    f = fx
    online(f)
    pi_held(f)
    f.setf('supervisor', **mark)
    ticks(f, 3)
    assert not regenerated(f) and not f.mgr.killed
    assert 'guarded' in f.agent('supervisor').get('regen_state', '')


def test_link_capped_supervisor_is_taken_over_by_regen(fx):
    f = fx
    online(f)
    pi_held(f)
    f.setf('supervisor', link_lost='capped')
    ticks(f)
    assert regenerated(f) and f.agent('supervisor').get('link_lost') == '0'


@pytest.mark.parametrize('how', ['gone', 'not_owned'])
def test_missing_or_unowned_supervisor_pane_is_escalated_only(fx, how):
    f = fx
    online(f)
    pi_held(f)
    pane = f.agent('supervisor')['pane']
    if how == 'gone':
        f.mgr.alive.discard(pane)
    else:
        f.mgr.owns = lambda p: p != pane
    ticks(f, 3)
    assert not regenerated(f) and pane not in f.mgr.killed
    assert events(f, 'held_watch.escalated')


def test_worker_api_error_is_unchanged(fx):
    f = fx
    online(f)
    req(f, 'r1', to='worker-1')
    f.pump()
    env = receive(f, 'worker-1')
    event(f, dict(session(f, 'worker-1'), kind='candidate', delivery=env, candidate={'tool': 'model', 'error': 'fetch failed'}))
    event(f, dict(session(f, 'worker-1'), kind='settled', delivery=env, candidates=[], failed=True, aborted=False,
                  unfinished=False))
    assert f.agent('worker-1')['state'] == 'held' and not f.agent('worker-1').get('api_error')
    f.v2.tick()
    f.setf('worker-1', held_since=awake.now() - 400)
    ticks(f)
    assert f.agent('worker-1')['generation'] == '0' and not events(f, 'supervisor_regen.started')


def test_advisor_and_workers_are_outside_regen(fx):
    from fleetmux import advisor
    assert not fx.v2.regen_on(advisor.SLOT, {}) and not fx.v2.regen_on('worker-1', {})
    assert fx.v2.regen_on('supervisor', fx.agent('supervisor'))


# ------------------------------------------------------------------ mechanism
def test_offline_probe_waits_without_spending_a_regen(fx):
    f = fx
    online(f, False)
    pi_held(f)
    ticks(f, 3)
    sup = f.agent('supervisor')
    assert not regenerated(f) and sup['regen_state'] == 'waiting_network' and not sup.get('regen_log')
    assert len(events(f, 'supervisor_regen.waits_network')) == 1
    online(f)
    f.setf('supervisor', regen_next_at=awake.now() - 1)
    ticks(f)
    assert regenerated(f)


def test_a_long_offline_wait_is_escalated_without_spending_a_regen(fx):
    """A probe that keeps failing must not hold the supervisor silently: past held_after_s the held watch escalates
    (reason names the network wait), still with no count spent and nothing queued to the supervisor."""
    f = fx
    online(f, False)
    pi_held(f)
    ticks(f)
    assert f.agent('supervisor')['regen_state'] == 'waiting_network' and not events(f, 'held_watch.escalated')
    f.setf('supervisor', held_since=awake.now() - 400)
    ticks(f)
    rows = events(f, 'held_watch.escalated')
    assert rows and 'network' in json.dumps(rows[-1]), rows
    assert not regenerated(f) and not f.agent('supervisor').get('regen_log')
    assert not any('HELD' in x for x in queued_texts(f, 'supervisor'))


def test_delete_dialog_on_a_regenerable_supervisor_blocks_the_recovery_too(fx):
    """A delete dialog on the held supervisor's screen: nothing is settled or re-sent (held_recover never runs), as
    for any held slot; escalated only."""
    f = fx
    online(f)
    f.mgr.capture_output = lambda pane, lines=2000: 'Bash command\n  rm -rf build/\nDo you want to proceed?\n❯ 1. Yes\n  2. No'
    calls = []
    f.v2.held_recover = lambda slot, info: calls.append(slot)
    pi_held(f)  # an API-error end: due at once
    ticks(f, 3)
    assert not calls, 'recovered (re-sent) under a delete dialog'
    assert not regenerated(f) and events(f, 'held_watch.escalated')


def test_probe_off_and_route_closed(fx, monkeypatch):
    f = fx
    online(f, False)
    assert f.v2.regen_online('supervisor', {'api_error': json.dumps({'text': 'fetch failed'})}, 'off')
    assert not f.v2.regen_online('supervisor', {'api_error': json.dumps({'text': 'fetch failed'})}, 'auto')
    assert f.v2.regen_online('supervisor', {}, 'auto'), 'no network evidence: the regeneration is the retry'
    assert not f.v2.regen_online('supervisor', {}, 'tcp')
    monkeypatch.setattr(runtime_v2.session_env, 'route_closed', lambda *a, **k: True)
    online(f)
    assert not f.v2.regen_online('supervisor', {}, 'auto')


def used_up(f):
    pi_held(f)
    now = awake.now()
    f.setf('supervisor', regen_log=json.dumps([now - 30, now - 20, now - 10]))
    ticks(f, 3)
    assert not regenerated(f) and f.agent('supervisor')['regen_state'] == 'exhausted'
    assert events(f, 'supervisor_regen.exhausted')
    assert not any('supervisor-1' in x and 'HELD' in x for x in queued_texts(f, 'supervisor'))


def notices(f):
    path = f.run_dir / 'notifications.jsonl'
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def test_used_up_budget_without_channels_is_recorded_never_queued_to_the_supervisor(fx):
    f = fx
    online(f)
    used_up(f)
    assert [n['status'] for n in notices(f) if n['event'] == 'supervisor_held'] == ['no_channel']


def test_used_up_budget_goes_out_through_the_channels(fx, monkeypatch):
    f = fx
    online(f)
    sent = []
    from fleetmux.channels import actor
    monkeypatch.setattr(actor, 'send_direct', lambda cfg, item, **kw: sent.append(item) or {'status': 'sent'})
    f.cfg['channels'] = [{'type': 'telegram', 'account': 'main'}]
    used_up(f)
    assert sent and sent[0]['event'] == 'daemon_down' and 'regeneration budget is used up' in sent[0]['text']
    assert [n['status'] for n in notices(f) if n['event'] == 'supervisor_held'] == ['sent']


def test_used_up_budget_of_a_managed_root_uses_lifecycle_notify(fx):
    f = fx
    online(f)
    got = []
    f.notifier = lambda rec: got.append(rec) or 'sent'
    f.cfg['lifecycle'] = 'no-such-mode'
    used_up(f)
    assert any(r['event'] == 'slot_held' and 'regeneration budget' in r['text'] for r in got)


def test_rotation_failure_rolls_back_and_counts(fx):
    f = fx
    online(f)
    pi_held(f)
    old, real = f.agent('supervisor')['pane'], f.mgr.kill_pane
    f.mgr.kill_pane = lambda pane: False if pane == old else real(pane)  # the old session does not exit
    ticks(f)
    sup = f.agent('supervisor')
    assert sup['generation'] == '0' and sup['state'] == 'held' and sup['regen_state'] == 'failed'
    assert len(json.loads(sup['regen_log'])) == 1 and events(f, 'watch.rotate_failed')
    lease = f.v2.bus.slot_state('supervisor')
    assert lease['status'] == 'uncertain', 'the held delivery was cancelled without a confirmed stop'


def test_regen_successor_without_ready_is_held_and_retried_not_blocked(fx):
    f = fx
    online(f)
    pi_held(f)
    ticks(f)
    assert regenerated(f) and f.agent('supervisor')['state'] == 'starting'
    f.setf('supervisor', startup_deadline=awake.now() - 1)
    f.v2.tick()
    assert f.agent('supervisor')['state'] in ('held', 'starting') and not f.paused()
    assert events(f, 'supervisor_regen.not_ready')
    f.setf('supervisor', regen_next_at=awake.now() - 1)
    ticks(f)
    assert f.agent('supervisor')['generation'] == '2'


def test_regen_requeue_carries_no_bus_deadline(fx):
    f = fx
    online(f)
    pi_held(f)
    ticks(f)
    mid, = f.v2.bus.pending('supervisor')
    assert int(f.v2.bus.message(mid)['deadline_at']) == runtime_v2.NO_DEADLINE_MS


def test_request_bound_routing_prompt_published_at_hold_time_is_not_sent_again(fx):
    f = fx
    online(f)
    req(f, 'r9', to=None)
    f.pump()
    env = receive(f, 'supervisor')
    assert f.v2.bus.message(env['message_id'])['request_id'] == 'r9'
    event(f, dict(session(f, 'supervisor'), kind='candidate', delivery=env, candidate={'tool': 'model', 'error': 'fetch failed'}))
    event(f, dict(session(f, 'supervisor'), kind='settled', delivery=env, candidates=[], failed=True, aborted=False,
                  unfinished=False))
    assert json.loads(f.r.get(fleet.RESULT + 'r9'))['status'] == 'failed'
    ticks(f)
    assert regenerated(f) and f.v2.bus.pending('supervisor') == []
    assert events(f, 'supervisor_regen.request_settled')


def test_request_bound_routing_prompt_still_open_is_routed_by_the_new_generation(fx):
    f = fx
    online(f)
    req(f, 'r8', to=None)
    f.pump()
    env = receive(f, 'supervisor')
    f.v2.transaction('lease-test', lambda: f.v2.hold('supervisor', 'lease/delivery deadline expired; execution stop unconfirmed'))
    f.v2.tick()
    f.setf('supervisor', held_since=awake.now() - 400)
    ticks(f)
    assert regenerated(f) and f.r.hgetall(fleet.REQ + 'r8')
    mid, = f.v2.bus.pending('supervisor')
    msg = f.v2.bus.message(mid)
    assert msg['request_id'] == 'r8' and 'Previous generation did not finish' in msg['body']['text']


def test_supervisor_regen_off_keeps_todays_behaviour(fx, tmp_path, monkeypatch):
    from fleetmux import fleet_config
    private = tmp_path / 'fleet.yaml'
    private.write_text('daemon_watch:\n  supervisor_regen: false\n')
    monkeypatch.setenv(fleet_config.USER_ENV, str(private))
    f = fx
    f.v2.held_knobs_cache = None
    online(f)
    pi_held(f)
    ticks(f, 3)
    assert not regenerated(f) and not events(f, 'supervisor_regen.started')


# ------------------------------------------------------------------ fleetmux resume
def test_resume_regenerates_an_unproven_pi_supervisor_at_once(fx):
    f = fx
    online(f)
    pi_held(f, error=None)
    answer = control(f, 'resume', 'res-1')
    assert answer['status'] == 'accepted' and [x['label'] for x in answer['regenerating']] == ['supervisor-1']
    ticks(f)
    assert regenerated(f)


def test_resume_refuses_when_nothing_can_be_released_with_reason_and_next_step(fx):
    f = fx
    online(f)
    pi_held(f, error=None)
    now = awake.now()
    f.setf('supervisor', regen_log=json.dumps([now - 30, now - 20, now - 10]))
    f.resume()
    answer = control(f, 'resume', 'res-2')
    assert answer['status'] == 'rejected' and answer['held'][0]['slot'] == 'supervisor'
    assert 'budget is used up' in answer['held'][0]['next'] and answer['reason']


def test_resume_lists_a_held_worker_with_its_next_step_and_never_regenerates_it(fx):
    f = fx
    req(f, 'r1', to='worker-1')
    f.pump()
    receive(f, 'worker-1')
    f.v2.transaction('w-hold', lambda: f.v2.hold('worker-1', 'lease expired; execution stop unconfirmed'))
    answer = control(f, 'resume', 'res-3')
    assert answer['status'] == 'accepted'  # the hold's pause was lifted: the call changed something
    assert answer['held'][0]['slot'] == 'worker-1' and answer['held'][0]['next']
    ticks(f)
    assert f.agent('worker-1')['generation'] == '0'


def test_resume_leaves_a_worker_stop_failure_backoff_unchanged(fx):
    """R1: a worker held by a StopFailure backoff is only reported (reason + next step); its retry time stays."""
    f = fx
    retry = awake.deadline(300)
    f.setf('worker-1', state='held', stopfail_at=time.time(), stopfail_g=f.agent('worker-1').get('generation', '0'),
           stopfail_retry_at=retry)
    answer = control(f, 'resume', 'res-5')
    entry = [x for x in answer['held'] if x['slot'] == 'worker-1']
    assert entry and 'API error' in entry[0]['reason'] and 'on its own' in entry[0]['next'], answer
    assert not [x for x in answer['released'] if x['slot'] == 'worker-1']
    assert float(f.agent('worker-1')['stopfail_retry_at']) == pytest.approx(retry)
    assert answer['status'] == 'rejected', 'the call changed nothing and a slot stays held'
    assert f.agent('worker-1')['generation'] == '0'


@pytest.mark.parametrize('supervisor', ['task'], indirect=True)
def test_resume_releases_a_proven_stopped_claude_supervisor(supervisor):
    f = supervisor
    online(f)
    assert '已排队' in operator(f, 'marker two')
    delivered(f, f.ready_hook)
    claude_failure(f)  # max_attempts 2: the first failure waits its backoff
    assert f.agent('supervisor')['state'] == 'held' and f.agent('supervisor').get('stopfail_retry_at')
    answer = control(f, 'resume', 'res-4')
    assert answer['status'] == 'accepted' and answer['released'][0]['proof'] == 'stop_failure'
    f.v2.tick()
    assert f.agent('supervisor')['state'] == 'idle' and f.agent('supervisor')['generation'] == '0'


# ------------------------------------------------------------------ parameters
def test_regen_keys_are_validated_one_by_one_and_never_exported_to_pi(tmp_path, monkeypatch):
    from fleetmux import fleet_config, pi_adapter
    knobs, problem = runtime_v2.regen_knobs()
    assert knobs['supervisor_regen'] is True and knobs['supervisor_regen_max'] == 3 and problem is None
    assert knobs['supervisor_regen_backoff_s'] == (60, 300, 900) and knobs['supervisor_regen_probe'] == 'auto'
    private = tmp_path / 'fleet.yaml'
    private.write_text('daemon_watch:\n  supervisor_regen_max: 0\n  supervisor_regen_window_s: 600\n'
                       '  supervisor_regen_backoff_s: 30\n  supervisor_regen_probe: ping\n  supervisor_regen: "yes"\n')
    monkeypatch.setenv(fleet_config.USER_ENV, str(private))
    knobs, problem = runtime_v2.regen_knobs()
    assert knobs['supervisor_regen_window_s'] == 600 and knobs['supervisor_regen_backoff_s'] == (30,)
    assert knobs['supervisor_regen_max'] == 3 and knobs['supervisor_regen_probe'] == 'auto'
    assert knobs['supervisor_regen'] is True
    assert all(k in problem for k in ('supervisor_regen_max', 'supervisor_regen_probe', 'supervisor_regen,'))
    held, held_problem = runtime_v2.held_watch_knobs()
    assert held == runtime_v2.HELD_WATCH and held_problem is None  # the held keys are untouched by a bad regen key
    assert pi_adapter.daemon_watch() == {'check_interval_s': 120, 'stale_after_s': 120, 'realert_after_s': 600}


def test_api_network_error_recognises_the_four_shapes():
    for text in ('fetch failed', 'Retry failed after 3 attempts', 'API Error: Connection refused (ECONNREFUSED)',
                 'API Error: Connection lost mid-response. The response above may be incomplete.'):
        assert runtime_v2.API_NETWORK_ERROR.search(text), text
    assert not runtime_v2.API_NETWORK_ERROR.search('Login expired · Please run /login')
    assert not runtime_v2.OFFLINE_ERROR.search('Retry failed after 3 attempts'), 'startup_offline must not widen'


# ------------------------------------------------------------------ §19: the adopted supervisor keeps its own path
from test_subfleet import step as tree_step, tree  # noqa: E402,F401  (fixture)
from test_supervisor_hold import adopted_supervisor_working  # noqa: E402


def test_adopted_supervisor_is_never_regenerated(tree, monkeypatch):
    t = tree
    cf, rec, thread = adopted_supervisor_working(t, monkeypatch)
    cf.v2.transaction('frozen-test', lambda: cf.v2.hold('supervisor', 'frozen; adopted pane belongs to the parent'))
    cf.tick()
    cf.setf('supervisor', held_since=awake.now() - 400, regen_due=awake.now())
    for _ in range(3):
        cf.tick()
        tree_step(t)
    assert not cf.v2.regen_on('supervisor', cf.agent('supervisor'))
    assert cf.agent('supervisor')['generation'] == '0' and not events(cf, 'supervisor_regen.started')
