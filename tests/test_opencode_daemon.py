"""opencode workers, daemon side of the plugin / bridge contract (interface.md §6, §12.5), on a private Redis.

Bridge events are posted as agent_bridge.py would post them (already bound to run / slot / generation / session):
permission_request routing (delete -> user only, other -> supervisor ruling sent back as one permission_reply
UI card, never a key), provider refusals (FreeTierError / 429 / model unavailable: reported by name, committed,
paused, not a task failure, never re-sent, never another model), model_lock (a failure), startup without a
typed correction, spawn without a cwd AGENTS.md, and the watchdog pressing nothing on an opencode permission wait.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

import pytest

from test_fleet import FakeMgr
from test_message_bus_v2 import private_redis_v2  # noqa: F401
from cmux_fleet import fleet, opencode_adapter, pi_adapter, runtime_v2

MODEL = 'opencode/deepseek-v4.1-flash-free'


def boot(r, tmp_path, mode='service', ready=True):
    r.flushdb()
    run = tmp_path / ('run-' + uuid.uuid4().hex)
    (run / 'workspace').mkdir(parents=True)
    cfg = dict(run=str(run), redis_socket=r.connection_pool.connection_kwargs['path'],
               python=sys.executable, workers=['worker-1', 'worker-2'], mode=mode,
               supervisor_cli='pi', worker_cli='opencode', pi='pi', pi_model='local/test',
               opencode='opencode', opencode_model=MODEL, opencode_window=200000,
               pi_home=str(run / 'pi-home'), allow_unsandboxed_pi=True, transport_version=2,
               allow_development_v2=True, stall_timeout=0, v2_lease_ms=300000, lifecycle='smoke')
    path = run / 'config.json'
    fleet.save(path, cfg)
    f = fleet.Fleet(path, r, FakeMgr())
    f.start_all()
    for slot in f.names:
        metadata = session(f, slot)
        event(f, dict(metadata, kind='bridge_session', request_id='register-' + slot))
        f.v2.bus.register_slot(slot, int(metadata['generation']), metadata['thread'])
        if ready:
            event(f, dict(metadata, kind='startup', request_id='ready-' + slot, text='READY', failed=False, aborted=False))
            event(f, dict(metadata, kind='availability', idle=True))
    r.set(f.v2.prefix + ':boot', 'running')
    return f


@pytest.fixture
def ofx(private_redis_v2, tmp_path):
    f = boot(private_redis_v2, tmp_path)
    yield f
    f.v2.close()


def session(f, slot):
    gen = int(f.agent(slot)['generation'])
    if slot == 'supervisor':
        transcript = pi_adapter.session_dir(f.cfg, slot, gen) / 'session.jsonl'
        thread = 'session-' + slot + '-g' + str(gen)
    else:
        thread = 'ses_' + slot.replace('-', '') + 'g' + str(gen) + 'abc'
        transcript = opencode_adapter.session_dir(f.cfg, slot, gen) / (thread + '.jsonl')
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.touch()
    return dict(agent=slot, generation=gen, run_id=f.v2.bus.run_id, thread=thread, transcript=str(transcript))


def event(f, ev, identity=None):
    identity = identity or 'e-' + uuid.uuid4().hex
    runtime_v2.post_event(f.v2.bus, ev, identity)
    assert f.v2.step(10)
    return identity


def reply_of(f, correlation):
    rows = f.r.xrange(f.v2.prefix + ':reply:' + correlation)
    return json.loads(rows[-1][1]['data']) if rows else None


def req(f, rid='r1', to='worker-1'):
    spec = dict(id=rid, payload={'message': '合法业务 JSON'}, reply={'required': ['reply'], 'allowed': ['reply']}, to=to)
    event(f, dict(kind='request', request_id='cmd-' + rid, spec=spec))
    f.pump()
    slot = to
    return f.v2.bus.receive(slot, 'bridge-' + slot + '-g' + f.agent(slot)['generation'],
                            generation=int(f.agent(slot)['generation']), session_id=f.agent(slot)['thread'])


def result(f, rid='r1'):
    raw = f.r.get(fleet.RESULT + rid)
    return json.loads(raw) if raw else None


def ui_cards(f, slot):
    return [json.loads(fields['data']) for _, fields in f.r.xrange(f.v2.prefix + ':ui:' + slot)]


def fail_turn(f, env, cls, text, slot='worker-1'):
    """What the plugin sends for a failed delivery execution: a model candidate, then settled(failed)."""
    event(f, dict(session(f, slot), kind='candidate', delivery=env,
                  candidate={'tool': 'model', 'error': text, 'error_class': cls}))
    event(f, dict(session(f, slot), kind='settled', delivery=env, candidates=[], failed=True, aborted=False,
                  unfinished=False, oc_message_id='msg_ours', inbox=['msg_ours'], request_id='settle-' + env['delivery_id']))


# ---------------------------------------------------------------- spawn
def test_spawn_writes_the_role_only_into_the_fresh_instance_dir_and_injects_its_env(ofx):
    f = ofx
    spawned = [kw for kw in f.mgr.spawned if '/worker-1' in kw['start_directory'] or kw['start_directory'].endswith('worker-1')]
    assert spawned, [kw['start_directory'] for kw in f.mgr.spawned]
    cwd = fleet.agent_cwd(f.cfg, 'worker-1')
    assert not (cwd / 'AGENTS.md').exists() and not (cwd / 'CLAUDE.md').exists()
    inst = opencode_adapter.instance_dir(f.cfg, 'worker-1', 0)
    assert (inst / 'cfg' / 'AGENTS.md').read_text().strip()
    env = spawned[-1]['env_vars']
    assert env['XDG_DATA_HOME'] == str(inst / 'xdgdata') and env['OPENCODE_CONFIG_DIR'] == str(inst / 'cfg')
    prompt = open(spawned[-1]['prompt_file']).read()
    assert 'Read AGENTS.md' not in prompt and env['CMUX_FLEET_OC_STARTUP'] == prompt
    assert json.loads(env['OPENCODE_CONFIG_CONTENT'])['model'] == MODEL
    with pytest.raises(FileExistsError):  # a generation's instance dir is never reused
        opencode_adapter.prepare_instance(f.cfg, 'worker-1', 0, 'role', task_dir=cwd)


def test_opencode_bridge_session_with_a_pi_style_identity_is_rejected(private_redis_v2, tmp_path):
    f = boot(private_redis_v2, tmp_path, ready=False)
    try:
        gen = int(f.agent('worker-2')['generation'])
        bad = pi_adapter.session_dir(f.cfg, 'worker-2', gen) / 'session.jsonl'
        bad.parent.mkdir(parents=True, exist_ok=True)
        f.setf('worker-2', thread='')
        event(f, dict(agent='worker-2', generation=gen, run_id=f.v2.bus.run_id, thread='session-worker-2',
                      transcript=str(bad), kind='bridge_session', request_id='pi-style'))
        assert reply_of(f, 'pi-style')['status'] == 'rejected'
        assert f.agent('worker-2')['thread'] == ''
    finally:
        f.v2.close()


# ---------------------------------------------------------------- startup
def test_opencode_startup_without_ready_is_never_nudged_and_fails_explicitly(private_redis_v2, tmp_path):
    f = boot(private_redis_v2, tmp_path, mode='task', ready=False)
    try:
        event(f, dict(session(f, 'worker-1'), kind='startup', request_id='s1', text='Hello, how can I help?',
                      failed=False, aborted=False))
        assert reply_of(f, 's1')['status'] == 'rejected'
        assert f.v2.startup_nudges == set() and not f.mgr.sent  # nothing typed into the opencode pane
        assert f.agent('worker-1')['state'] == 'dead'
        assert 'opencode startup failed before READY' in f.task().get('reason', '')
        assert "reply was not READY: 'Hello" in f.task()['reason']
    finally:
        f.v2.close()


def test_opencode_startup_provider_refusal_is_named_in_the_failure(private_redis_v2, tmp_path):
    f = boot(private_redis_v2, tmp_path, mode='task', ready=False)
    try:
        event(f, dict(session(f, 'worker-1'), kind='startup', request_id='s2', text='', failed=True, aborted=False,
                      error="FreeTierError: OpenCode's free tier can only be used from within OpenCode.",
                      error_class='free_tier_rejected'))
        assert reply_of(f, 's2')['status'] == 'rejected'
        assert 'free_tier_rejected: FreeTierError' in f.task()['reason'] and 'not retried, model unchanged' in f.task()['reason']
        assert f.cfg['opencode_model'] == MODEL
    finally:
        f.v2.close()


# ---------------------------------------------------------------- provider refusals / model lock
@pytest.mark.parametrize('cls,text,title', [
    ('free_tier_rejected', "FreeTierError: OpenCode's free tier can only be used from within OpenCode.", 'FreeTierError'),
    ('rate_limited', 'Too Many Requests', '429'),
    ('model_unavailable', 'Model unavailable: opencode/deepseek-v4.1-flash-free', '模型下线'),
])
def test_provider_refusal_is_reported_by_name_not_a_failure_not_resent_not_switched(ofx, cls, text, title):
    f = ofx
    env = req(f)
    fail_turn(f, env, cls, text)
    out = result(f)
    assert out['status'] == 'blocked' and out['opencode_error'] == cls and title in out['error']
    msg = f.v2.bus.message(env['message_id'])
    assert msg['status'] == 'committed' and msg['outcome'] == cls  # turn over: committed, never held as uncertain
    assert f.v2.depth('worker-1') == 0 and f.v2.bus.slot_state('worker-1')['pel_count'] == 0  # nothing re-sent
    assert f.agent('worker-1')['state'] == 'idle' and f.agent('worker-1').get('oc_error', '') == ''
    assert f.paused() and cls in f.r.hget(fleet.SERVICE, 'reason')
    assert f.cfg['opencode_model'] == MODEL and f.runtime_cfg('worker-1')['opencode_model'] == MODEL
    cards = [c['render'] for c in ui_cards(f, 'supervisor')]
    assert any(title in c and '未重投、未换模型' in c for c in cards)


def test_model_lock_fails_the_request_and_never_switches_model(ofx):
    f = ofx
    env = req(f)
    fail_turn(f, env, 'model_lock', 'cost_nonzero: session usage cost 0.01')
    out = result(f)
    assert out['status'] == 'failed' and out['opencode_error'] == 'model_lock' and 'cost_nonzero' in out['error']
    assert f.v2.bus.message(env['message_id'])['outcome'] == 'failed'
    assert f.v2.depth('worker-1') == 0 and f.cfg['opencode_model'] == MODEL


def test_model_lock_fails_the_task_in_task_mode(private_redis_v2, tmp_path):
    f = boot(private_redis_v2, tmp_path, mode='task')
    try:
        event(f, dict(kind='task', request_id='new-task', spec={'goal': 'write an evidence report'}))
        f.pump()
        sup = f.v2.bus.receive('supervisor', 'bridge-supervisor-g0', generation=0, session_id=f.agent('supervisor')['thread'])
        event(f, dict(session(f, 'supervisor'), kind='settled', delivery=sup, failed=False, aborted=False, unfinished=False,
                      candidates=[{'tool': 'fleet_decision', 'value': {'status': 'RUNNING', 'completion': 10, 'dispatch': [
                          {'to': 'worker-1', 'instruction': 'Write worker-1/report.md with the evidence.'}]}}]))
        f.pump()
        env = f.v2.bus.receive('worker-1', 'bridge-worker-1-g0', generation=0, session_id=f.agent('worker-1')['thread'])
        fail_turn(f, env, 'model_lock', 'model_lock: expected opencode/deepseek-v4.1-flash-free, got opencode/deepseek-v4.1-flash')
        assert f.task()['status'] == 'blocked' and 'opencode model_lock' in f.task()['reason']
    finally:
        f.v2.close()


def test_a_refused_handoff_turn_keeps_the_generic_hold_and_publishes_nothing(ofx):
    f = ofx
    env = req(f)
    f.r.hset(f.v2.bus.msg_key(env['message_id']), 'body', json.dumps(dict(f.v2.bus.message(env['message_id'])['body'],
                                                                           purpose='handoff')))
    fail_turn(f, env, 'free_tier_rejected', 'FreeTierError: x')
    assert f.v2.bus.message(env['message_id'])['status'] == 'uncertain' and f.agent('worker-1')['state'] == 'held'
    assert (result(f) or {}).get('opencode_error') is None


def test_a_model_error_recorded_for_another_delivery_is_not_attributed(ofx):
    f = ofx
    env = req(f)
    other = dict(env, delivery_id='d-foreign')
    event(f, dict(session(f, 'worker-1'), kind='candidate', delivery=other,
                  candidate={'tool': 'model', 'error': 'Too Many Requests', 'error_class': 'rate_limited'}))
    event(f, dict(session(f, 'worker-1'), kind='settled', delivery=env, candidates=[], failed=True, aborted=False,
                  unfinished=False, request_id='settle-x'))
    # Not this delivery's refusal: the generic path holds it (execution stop unconfirmed), never 'blocked'.
    assert f.v2.bus.message(env['message_id'])['status'] == 'uncertain'
    assert result(f)['status'] == 'failed' and 'opencode_error' not in result(f)


def test_interrupted_delivery_keeps_the_generic_hold(ofx):
    f = ofx
    env = req(f)
    event(f, dict(session(f, 'worker-1'), kind='candidate', delivery=env,
                  candidate={'tool': 'model', 'error': 'interrupted: user', 'error_class': 'aborted'}))
    event(f, dict(session(f, 'worker-1'), kind='settled', delivery=env, candidates=[], failed=True, aborted=True,
                  unfinished=False, request_id='settle-a'))
    assert f.v2.bus.message(env['message_id'])['status'] == 'uncertain' and f.agent('worker-1')['state'] == 'held'


def test_settled_accepts_the_opencode_audit_fields(ofx):
    f = ofx
    env = req(f)
    event(f, dict(session(f, 'worker-1'), kind='settled', delivery=env, failed=False, aborted=False, unfinished=False,
                  candidates=[{'tool': 'fleet_result', 'value': {'reply': 'OK'}}], oc_message_id='msg_1', inbox=['msg_1'],
                  request_id='settle-ok'))
    assert result(f)['status'] == 'ok' and reply_of(f, 'settle-ok')['status'] == 'committed'


# ---------------------------------------------------------------- permission requests
def permission(f, env, rid, command, cls, slot='worker-1', **extra):
    ev = dict(session(f, slot), kind='permission_request', class_=None, session=f.agent(slot)['thread'],
              request={'id': rid, 'permission': 'bash', 'patterns': [command], 'command': command})
    ev.pop('class_')
    ev['class'] = cls
    if env:
        ev['delivery'] = env
    ev.update(extra)
    event(f, ev)


def replies(f, slot='worker-1'):
    return [c for c in ui_cards(f, slot) if c.get('kind') == 'permission_reply']


def test_delete_permission_goes_to_the_user_only_with_the_screen_saved(ofx):
    f = ofx
    env = req(f)
    permission(f, env, 'per_del1', 'rm -rf build', 'delete')
    info = f.agent('worker-1')
    assert info['prompt_wait'] == env['delivery_id'] and info['prompt_route'] == 'user' and info['prompt_channel'] == 'opencode'
    rec = json.loads(info['oc_permission'])
    assert rec['request_id'] == 'per_del1' and rec['class'] == 'delete'
    screen = open(rec['screen']).read()
    assert 'rm -rf build' in screen and f.v2.bus.run_id in screen and 'worker-1' in screen
    assert f.paused() and not replies(f)
    assert not any(k.startswith(fleet.DIALOG_KEY) for k in f.escalation_state())
    assert any('rm -rf build' in c['render'] and '程序不代答' in c['render'] for c in ui_cards(f, 'supervisor'))
    assert f.v2.bus.message(env['message_id'])['status'] == 'received'  # not a hold: the user's answer lets it settle


def test_a_plugin_claim_of_other_for_a_delete_command_still_goes_to_the_user(ofx):
    f = ofx
    env = req(f)
    permission(f, env, 'per_lie', 'rm -rf x', 'other')
    assert f.agent('worker-1')['prompt_route'] == 'user' and not f.escalation_state()


def test_other_permission_is_ruled_by_the_supervisor_and_answered_by_a_permission_reply_card(ofx):
    f = ofx
    env = req(f)
    permission(f, env, 'per_ls', 'ls -la', 'other')
    key = fleet.DIALOG_KEY + 'worker-1:g0'
    rec = f.escalation_state()[key]
    assert rec['channel'] == 'permission_reply' and rec['request_id'] == 'per_ls' and rec['keys_approve'] == []
    assert f.agent('worker-1')['prompt_route'] == 'supervisor' and not f.paused()
    assert any('allows this one request once' in t for t in f.v2.pending_text('supervisor'))
    f.dialog_answer(key, {'decision': 'approve'})
    (card,) = replies(f)
    assert {k: card[k] for k in ('run_id', 'slot', 'generation', 'session_id', 'request_id', 'class', 'decision')} == {
        'run_id': f.v2.bus.run_id, 'slot': 'worker-1', 'generation': 0, 'session_id': f.agent('worker-1')['thread'],
        'request_id': 'per_ls', 'class': 'other', 'decision': 'once'}
    assert key not in f.escalation_state() and f.agent('worker-1')['prompt_wait'] == '' and not f.mgr.keys


def test_reject_ruling_sends_reject(ofx):
    f = ofx
    env = req(f)
    permission(f, env, 'per_cat', 'cat a.txt', 'other')
    f.dialog_answer(fleet.DIALOG_KEY + 'worker-1:g0', {'decision': 'reject'})
    assert [c['decision'] for c in replies(f)] == ['reject']


def test_a_ruling_for_a_slot_that_moved_on_sends_nothing(ofx):
    f = ofx
    env = req(f)
    permission(f, env, 'per_old', 'pytest -q', 'other')
    f.setf('worker-1', thread='ses_otherSession1')  # e.g. a new session meanwhile
    f.dialog_answer(fleet.DIALOG_KEY + 'worker-1:g0', {'decision': 'approve'})
    assert not replies(f) and fleet.DIALOG_KEY + 'worker-1:g0' not in f.escalation_state()


def test_permission_bound_to_a_delivery_not_in_flight_goes_to_the_user(ofx):
    f = ofx
    env = req(f)
    permission(f, dict(env, delivery_id='d-stale'), 'per_stale', 'ls', 'other')
    assert f.agent('worker-1')['prompt_route'] == 'user' and not f.escalation_state()


def test_manual_turn_permission_is_a_notice_only(ofx):
    f = ofx
    permission(f, None, 'per_manual', 'rm x', 'delete')
    assert f.agent('worker-1').get('prompt_wait', '') == '' and not f.paused()
    assert any('手动回合' in c['render'] for c in ui_cards(f, 'worker-1'))


def test_watchdog_presses_nothing_and_keeps_an_opencode_permission_wait_despite_activity(ofx):
    f = ofx
    env = req(f)
    permission(f, env, 'per_wd', 'rm -rf build', 'delete')
    transcript = f.agent('worker-1')['transcript']
    later = time.time() + 3600
    os.utime(transcript, (later, later))  # the plugin logs every event: activity is not an answer
    f.v2.watchdog.next_check.clear()
    f.v2.watchdog.tick(later)
    assert f.agent('worker-1')['prompt_wait'] == env['delivery_id'] and not f.mgr.keys


def test_supervisor_routed_permission_wait_is_not_cleared_by_activity(ofx):
    f = ofx
    env = req(f)
    permission(f, env, 'per_wd2', 'ls', 'other')
    transcript = f.agent('worker-1')['transcript']
    later = time.time() + 30
    os.utime(transcript, (later, later))
    f.v2.watchdog.next_check.clear()
    f.v2.watchdog.tick(later)
    assert fleet.DIALOG_KEY + 'worker-1:g0' in f.escalation_state() and not f.mgr.keys
    assert f.agent('worker-1')['prompt_route'] == 'supervisor'


# ---------------------------------------------------------------- permission_resolved / permission_refused (§12.4)
def pstate(f, kind, rid, slot='worker-1', **fields):
    """A bridge permission state event; fields override the matched identity (agent / run / generation / thread)."""
    ev = dict(session(f, slot), kind=kind, request_id=rid, session=f.agent(slot)['thread'])
    ev.update(fields)
    event(f, ev)


def audits(f, name):
    return [json.loads(row.get('detail') or '{}') for _, row in f.r.xrange(f.v2.bus.audit_key) if row.get('event') == name]


def waiting_on(f, env, rid, slot='worker-1'):
    info = f.agent(slot)
    return info['prompt_wait'] == env['delivery_id'] and json.loads(info['oc_permission'])['request_id'] == rid


@pytest.mark.parametrize('field, value', [('generation', 7), ('run_id', 'run-other'), ('thread', 'ses_otherSession1')])
def test_a_permission_state_with_another_identity_changes_nothing(ofx, field, value):
    f = ofx
    env = req(f)
    permission(f, env, 'per_id', 'ls -la', 'other')
    key = fleet.DIALOG_KEY + 'worker-1:g0'
    for kind in ('permission_resolved', 'permission_refused'):
        pstate(f, kind, 'per_id', **{field: value, 'class': 'other', 'reply': 'once', 'decided_by': 'tui_user',
                                     'held_for_user': True, 'reason': 'x'})
    assert waiting_on(f, env, 'per_id') and f.agent('worker-1')['prompt_route'] == 'supervisor'
    assert f.escalation_state()[key]['request_id'] == 'per_id' and not replies(f)
    assert not audits(f, 'opencode.permission_resolved') and not audits(f, 'opencode.permission_refused')


def test_a_resolved_from_another_slot_never_closes_this_slots_wait(ofx):
    f = ofx
    env = req(f)
    permission(f, env, 'per_same', 'ls -la', 'other')
    pstate(f, 'permission_resolved', 'per_same', slot='worker-2', reply='once', decided_by='tui_user', reason='replied')
    assert waiting_on(f, env, 'per_same') and fleet.DIALOG_KEY + 'worker-1:g0' in f.escalation_state()
    assert [d['closed_wait'] for d in audits(f, 'opencode.permission_resolved')] == [False]


def test_permission_states_from_a_pi_slot_are_rejected(ofx):
    f = ofx
    for kind in ('permission_resolved', 'permission_refused'):
        pstate(f, kind, 'per_pi', slot='supervisor', reply='once', decided_by='tui_user', held_for_user=True, reason='x')
    assert not audits(f, 'opencode.permission_resolved') and not audits(f, 'opencode.permission_refused')
    assert len(audits(f, 'permission.rejected')) == 2


@pytest.mark.parametrize('decided_by, recorded, user', [
    ('tui_user', 'tui_user', True), ('fleet', 'fleet', False), ('unattributed', 'unattributed', False),
    ('none', 'none', False), ('bogus', 'unattributed', False), (None, 'unattributed', False)])
def test_resolved_records_who_decided_and_only_tui_user_is_the_users_decision(ofx, decided_by, recorded, user):
    f = ofx
    env = req(f)
    permission(f, env, 'per_who', 'rm -rf build', 'delete')
    fields = {'class': 'delete', 'reply': 'reject', 'reason': 'replied'}
    if decided_by is not None:
        fields['decided_by'] = decided_by
    pstate(f, 'permission_resolved', 'per_who', **fields)
    (detail,) = audits(f, 'opencode.permission_resolved')
    assert (detail['request_id'], detail['decided_by'], detail['user_decision'], detail['closed_wait']) == (
        'per_who', recorded, user, True)
    info = f.agent('worker-1')
    assert info['prompt_wait'] == '' and info['prompt_route'] == '' and info['oc_permission'] == ''
    assert not replies(f) and f.v2.bus.message(env['message_id'])['status'] == 'received'  # the turn goes on


def test_resolved_normalises_an_unknown_reply_to_none(ofx):
    f = ofx
    env = req(f)
    permission(f, env, 'per_rep', 'rm x', 'delete')
    pstate(f, 'permission_resolved', 'per_rep', reply='yes please', decided_by='tui_user', reason='replied')
    assert audits(f, 'opencode.permission_resolved')[0]['reply'] is None


def test_resolved_for_another_request_keeps_the_wait_and_the_escalation(ofx):
    f = ofx
    env = req(f)
    permission(f, env, 'per_keep', 'ls -la', 'other')
    key = fleet.DIALOG_KEY + 'worker-1:g0'
    pstate(f, 'permission_resolved', 'per_gone', reply='once', decided_by='tui_user', reason='replied')
    assert waiting_on(f, env, 'per_keep') and f.agent('worker-1')['prompt_route'] == 'supervisor'
    assert f.escalation_state()[key]['request_id'] == 'per_keep' and not replies(f)
    (detail,) = audits(f, 'opencode.permission_resolved')
    assert detail['request_id'] == 'per_gone' and detail['closed_wait'] is False


def test_resolved_for_another_request_keeps_a_users_delete_wait_paused(ofx):
    f = ofx
    env = req(f)
    permission(f, env, 'per_del', 'rm -rf build', 'delete')
    pstate(f, 'permission_resolved', 'per_other', reply=None, decided_by='none', reason='cleared')
    assert waiting_on(f, env, 'per_del') and f.agent('worker-1')['prompt_route'] == 'user' and f.paused()


def test_tui_user_answer_closes_the_supervisor_escalation_and_sends_no_ruling(ofx):
    f = ofx
    env = req(f)
    permission(f, env, 'per_tui', 'ls -la', 'other')
    key = fleet.DIALOG_KEY + 'worker-1:g0'
    pstate(f, 'permission_resolved', 'per_tui', reply='once', decided_by='tui_user', reason='replied')
    assert key not in f.escalation_state() and f.agent('worker-1')['prompt_wait'] == ''  # no ruling left to give
    assert not replies(f) and audits(f, 'opencode.permission_resolved')[0]['user_decision'] is True


def test_refused_held_for_user_keeps_the_request_with_the_user_and_resends_nothing(ofx):
    f = ofx
    env = req(f)
    permission(f, env, 'per_held', 'pytest -q', 'other')
    f.dialog_answer(fleet.DIALOG_KEY + 'worker-1:g0', {'decision': 'reject'})
    assert f.agent('worker-1')['prompt_wait'] == ''
    pstate(f, 'permission_refused', 'per_held', **{'class': 'other', 'held_for_user': True,
           'reason': 'reject not sent: it would also decline pending delete request(s) [per_rm]'})
    info = f.agent('worker-1')
    assert waiting_on(f, env, 'per_held') and info['prompt_route'] == 'user' and info['prompt_channel'] == 'opencode'
    assert f.paused() and [c['decision'] for c in replies(f)] == ['reject']  # the one ruling, never re-sent
    (detail,) = audits(f, 'opencode.permission_refused')
    assert detail['request_id'] == 'per_held' and detail['held_for_user'] is True
    pstate(f, 'permission_resolved', 'per_held', reply='reject', decided_by='tui_user', reason='replied')
    assert f.agent('worker-1')['prompt_wait'] == '' and [c['decision'] for c in replies(f)] == ['reject']


@pytest.mark.parametrize('held', ['yes', 1, 'true', None])
def test_refused_without_a_boolean_true_held_for_user_is_a_notice_only(ofx, held):
    f = ofx
    env = req(f)
    permission(f, env, 'per_note', 'cat a', 'other')
    f.dialog_answer(fleet.DIALOG_KEY + 'worker-1:g0', {'decision': 'approve'})
    fields = {'class': 'other', 'reason': 'request no longer pending'}
    if held is not None:
        fields['held_for_user'] = held
    pstate(f, 'permission_refused', 'per_note', **fields)
    assert f.agent('worker-1')['prompt_wait'] == '' and not f.paused()
    assert audits(f, 'opencode.permission_refused')[0]['held_for_user'] is False


def test_refused_held_for_user_in_a_manual_turn_waits_on_nothing(ofx):
    f = ofx
    permission(f, None, 'per_man', 'ls', 'other')
    pstate(f, 'permission_refused', 'per_man', held_for_user=True, reason='x')
    assert f.agent('worker-1').get('prompt_wait', '') == '' and not f.paused()
    assert audits(f, 'opencode.permission_refused')[0]['held_for_user'] is True


# ---------------------------------------------------------------- strict cost == 0 (finite number) model lock
@pytest.mark.parametrize('cost', [0, 0.0, -0.0])
def test_model_lock_accepts_only_an_exact_finite_zero_cost(cost):
    cfg = {'opencode_model': MODEL}
    info = {'model': {'providerID': 'opencode', 'id': 'deepseek-v4.1-flash-free'}, 'cost': cost}
    assert opencode_adapter.model_problem(cfg, info) is None


@pytest.mark.parametrize('cost', [1e-300, 5e-324, 1e-9, 0.01, -1, -1e-12, float('nan'), float('inf'), float('-inf'),
                                  None, '0', '0.0', True, False, [0], {'total': 0}, 'missing'])
def test_model_lock_refuses_any_other_cost(cost):
    cfg = {'opencode_model': MODEL}
    info = {'model': {'providerID': 'opencode', 'id': 'deepseek-v4.1-flash-free'}}
    if cost != 'missing':
        info['cost'] = cost
    assert opencode_adapter.model_problem(cfg, info).startswith('cost_nonzero:')


# ---------------------------------------------------------------- a manual first turn is never the startup turn
HARNESS = Path(__file__).with_name('js') / 'oc_plugin_harness.mjs'
PLUGIN = Path(fleet.__file__).with_name('opencode_plugin') / 'index.js'


def harness(scenario):
    node = shutil.which('node')
    if not node:
        pytest.skip('node is not installed')
    out = subprocess.run([node, str(HARNESS), str(PLUGIN), scenario], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_plugin_does_not_take_a_manual_first_turn_for_the_startup_turn():
    out = harness('manual_first')
    assert out['after_manual'] == ['init']  # the human's execution (inbox [msg_manual]) settles nothing
    startups = [f for f in out['frames'] if f['kind'] == 'startup']
    assert len(startups) == 1 and startups[0]['text'] == 'READY' and startups[0]['failed'] is False


def test_plugin_fails_a_startup_mixed_with_manual_input_and_types_no_correction():
    out = harness('mixed_startup')
    (startup,) = [f for f in out['frames'] if f['kind'] == 'startup']
    assert startup['failed'] is True and 'mixed fleet msg_startup with other input' in startup['error']


def test_plugin_creates_its_session_before_setup_returns():
    # The session exists when setup() returns. Not the P1-01 fix on its own: with it the 2.0.30 tab still stayed on
    # the home screen (E2E R3/R4); the TUI half below opens the session.
    out = harness('setup_waits_for_session')
    assert out['create_called'] is True and out['returned_before_create'] is False and out['bound_at_return'] is True
    (startup,) = [f for f in out['frames'] if f['kind'] == 'startup']
    assert startup['text'] == 'READY' and startup['failed'] is False


def test_tui_half_opens_the_session_the_server_half_bound():
    # P1-01: opencode 2.0.30's --continue lists the newest session once at TUI start; the plugin's session is not
    # there yet, the tab stayed on the home screen and a typed line opened a second session (E2E C3/C4/R3/R4).
    # tui.js (the TUI loads it from the plugin directory) opens the bound session by id, once it is bound.
    out = harness('tui_shows_bound_session')
    assert out['tui_present'] is True and out['tui_id'] == 'cmux-fleet.bridge.tui'
    assert out['navigations_before_create'] == 0
    assert out['navigations'] == [{'type': 'session', 'sessionID': 'ses_harness01'}]
    (startup,) = [f for f in out['frames'] if f['kind'] == 'startup']
    assert startup['text'] == 'READY' and startup['failed'] is False


def test_opencode_config_loads_the_plugin_directory_with_its_tui_half():
    # opencode 2.0.30 resolves a local plugin DIRECTORY's entrypoints as <dir>/server|<dir>/index (server) and
    # <dir>/tui (TUI); a file entry is server-only. So the config names the directory, which has both halves.
    plugins = json.loads(opencode_adapter.config_content({'opencode_model': 'opencode/deepseek-v4.1-flash-free'}))['plugin']
    assert plugins == [str(opencode_adapter.PLUGIN_DIR)] and Path(plugins[0]).is_dir()
    assert (Path(plugins[0]) / 'index.js').is_file() and (Path(plugins[0]) / 'tui.js').is_file()
    assert not (Path(plugins[0]) / 'server.js').exists()  # would replace index.js as the server entrypoint


def test_daemon_keeps_a_starting_opencode_slot_starting_on_manual_activity(private_redis_v2, tmp_path):
    f = boot(private_redis_v2, tmp_path, ready=False)
    try:
        event(f, dict(session(f, 'worker-1'), kind='availability', idle=True))
        event(f, dict(session(f, 'worker-1'), kind='settled', candidates=[], failed=False, aborted=False,
                      unfinished=False, request_id='no-delivery'))
        assert f.agent('worker-1')['state'] == 'starting' and f.agent('worker-1')['runtime_ready'] == '0'
    finally:
        f.v2.close()
