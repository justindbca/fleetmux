"""Isolated Redis/Fleet/bridge acceptance. No model, GUI, registry or production socket.

Redis socket bind failures are errors with the server's exact log, not skipped gates.
Run on an unsandboxed parent with: python3 -m pytest -q tests/test_runtime_v2.py
"""
import contextlib
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import queue
import threading
import time
import uuid
import sys

import pytest

from test_fleet import FakeMgr, traffic
from test_message_bus_v2 import private_redis_v2
from fleetmux import fleet, pi_adapter, runtime_v2
from fleetmux.agent_bridge import AgentBridge
from fleetmux.message_bus import BusError


@pytest.fixture
def fx(private_redis_v2, tmp_path):
    r = private_redis_v2
    r.flushdb()  # this fixture's private child only
    run = tmp_path / ('run-' + uuid.uuid4().hex)
    (run / 'workspace').mkdir(parents=True)
    cfg = dict(run=str(run), redis_socket=r.connection_pool.connection_kwargs['path'],
        python=sys.executable, workers=['worker-1', 'worker-2'], mode='service',
        supervisor_cli='pi', worker_cli='pi', pi='pi', pi_model='local/test',
        pi_home=str(run / 'pi-home'), allow_unsandboxed_pi=True, transport_version=2,
        allow_development_v2=True, stall_timeout=0, v2_lease_ms=300000)
    path = run / 'config.json'
    fleet.save(path, cfg)
    f = fleet.Fleet(path, r, FakeMgr())
    f.start_all()
    for slot in f.names:
        metadata = session(f, slot)
        event(f, dict(metadata, kind='bridge_session', request_id='register-' + slot))
        f.v2.bus.register_slot(slot, 0, metadata['thread'])
        event(f, dict(metadata, kind='startup', request_id='ready-' + slot,
                      text='READY', failed=False, aborted=False))
        event(f, dict(metadata, kind='availability', idle=True))
    r.set(f.v2.prefix + ':boot', 'running')
    yield f
    f.v2.close()


def session(f, slot):
    gen = int(f.agent(slot)['generation'])
    transcript = pi_adapter.session_dir(f.cfg, slot, gen) / 'session.jsonl'
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.touch()
    return dict(agent=slot, generation=gen, run_id=f.v2.bus.run_id,
                thread='session-' + slot + '-g' + str(gen), transcript=str(transcript))


def event(f, ev, identity=None):
    identity = identity or 'e-' + uuid.uuid4().hex
    runtime_v2.post_event(f.v2.bus, ev, identity)
    assert f.v2.step(10)
    return identity


def req(f, rid='r1', to='worker-1', reply=None):
    spec = dict(id=rid, payload={'message': '合法业务 JSON /tmp/report.md https://example.org'},
                reply=reply or {'required': ['reply'], 'allowed': ['reply']})
    if to:
        spec['to'] = to
    event(f, dict(kind='request', request_id='cmd-' + rid, spec=spec))
    return spec


def receive(f, slot):
    return f.v2.bus.receive(slot, 'bridge-' + slot + '-g' + f.agent(slot)['generation'],
        generation=int(f.agent(slot)['generation']), session_id=f.agent(slot)['thread'])


def settle(f, slot, env, value=None, **extra):
    candidates = [{'tool': 'fleet_decision' if slot == 'supervisor' else 'fleet_result',
                   'value': value or {'reply': 'OK'}}]
    ev = dict(session(f, slot), kind='settled', delivery=env, candidates=candidates,
              failed=False, aborted=False, unfinished=False)
    ev.update(extra)
    identity = event(f, ev)
    if f.agent(slot).get('state') == 'idle':
        event(f, dict(session(f, slot), kind='availability', idle=True))
    return identity


def result(f, rid='r1'):
    return json.loads(f.r.get(fleet.RESULT + rid))


def test_operator_input_dispatch_reply_no_files_paste_or_mechanical_model_round(fx):
    f = fx
    event(f, dict(session(f, 'supervisor'), kind='operator_input', text='让 worker-1 回复 OK', request_id='click'))
    ack = json.loads(f.r.xrange(f.v2.prefix + ':reply:click')[0][1]['data'])
    rid = ack['id']
    assert rid.startswith('ui-')
    f.pump(); sup = receive(f, 'supervisor')
    assert rid not in sup['body']['text'] and sup['delivery_id'] not in sup['body']['text']
    settle(f, 'supervisor', sup, {'status': 'RUNNING', 'dispatch': [{'to': 'worker-1', 'instruction': 'Reply OK in the reply field.'}], 'skip': []})
    f.pump(); worker = receive(f, 'worker-1')
    assert rid not in worker['body']['text']
    settle(f, 'worker-1', worker)
    assert result(f, rid)['result'] == {'reply': 'OK'}
    assert f.v2.bus.slot_state('worker-1')['pel_count'] == 0
    assert f.agent('worker-1')['state'] == 'idle'
    assert not f.v2.depth('supervisor') and not traffic(f.mgr)
    assert not list(f.run_dir.rglob('.inbox'))
    cards = [json.loads(fields['data']) for _, fields in f.r.xrange(f.v2.prefix + ':ui:supervisor')]
    assert cards[-1]['render'].splitlines()[0] == 'worker-1：OK' and not cards[-1]['wake_model']


def test_task_worker_result_and_independent_artifact_acceptance(fx):
    f = fx
    f.service = False; f.cfg['mode'] = 'task'
    event(f, dict(kind='task', request_id='new-task', spec={'goal': 'write an evidence report'}))
    f.pump(); env = receive(f, 'supervisor')
    settle(f, 'supervisor', env, {'status': 'RUNNING', 'completion': 20, 'dispatch': [{'to': 'worker-1', 'instruction': 'Write worker-1/report.md with Conclusion evidence.'}],
        'artifacts': [{'path': 'worker-1/report.md', 'contains': ['Conclusion']}]})
    f.pump(); worker = receive(f, 'worker-1')
    out = f.workspace() / 'worker-1/report.md'
    out.write_text('Conclusion: verified evidence')
    settle(f, 'worker-1', worker, {'done': ['report'], 'evidence': ['worker-1/report.md'], 'not_done': 'none', 'blocked': 'none'})
    f.pump(); followup = receive(f, 'supervisor')
    assert followup['message_id'] != env['message_id']
    settle(f, 'supervisor', followup, {'status': 'COMPLETED', 'completion': 100, 'dispatch': [], 'reason': 'verified'})
    assert f.task()['status'] == 'completed'


def test_duplicate_settlement_and_retained_tombstone_no_second_public_result(fx):
    req(fx); fx.pump(); env = receive(fx, 'worker-1')
    settle(fx, 'worker-1', env)
    settle(fx, 'worker-1', env)
    assert fx.r.llen(fleet.RESULTS) == 1
    fx.r.delete(fleet.RESULT + 'r1')
    event(fx, dict(kind='request', request_id='duplicate', spec={'id': 'r1', 'payload': {}, 'reply': {'required': ['reply']}}))
    assert json.loads(fx.r.get('fx:reply:duplicate'))['status'] == 'rejected'


@pytest.mark.parametrize('field,value', [('run_id','other'), ('slot','worker-2'), ('generation',9),
    ('session_id','old'), ('fence',99), ('delivery_id','d-other'), ('message_id','m-other'),
    ('stream_id','1-0'), ('consumer','other')])
def test_strict_identity_settlement_does_not_publish_or_release(fx, field, value):
    req(fx); fx.pump(); env = receive(fx, 'worker-1')
    env[field] = value
    settle(fx, 'worker-1', env)
    assert not fx.r.get(fleet.RESULT+'r1')
    assert fx.v2.bus.slot_state('worker-1')['pel_count'] == 1
    assert fx.agent('worker-1')['state'] == 'busy'


@pytest.mark.parametrize('change', [dict(aborted=True), dict(failed=True), dict(unfinished=True)])
def test_aborted_candidate_holds_active_pel_without_success(fx, change):
    req(fx); fx.pump(); env = receive(fx, 'worker-1')
    settle(fx, 'worker-1', env, **change)
    assert result(fx)['status'] == 'failed'
    assert fx.agent('worker-1')['state'] == 'held'
    assert fx.v2.bus.slot_state('worker-1')['pel_count'] == 1


@pytest.mark.parametrize('candidates', [[], [{'tool': 'fleet_result', 'value': {'reply':'a'}},
    {'tool':'fleet_result','value':{'reply':'b'}}], [{'tool':'fleet_decision','value':{'reply':'OK'}}],
    [{'tool':'fleet_result','value':{'wrong':'value'}}]])
def test_multiple_conflicting_wrong_role_invalid_schema_not_success(fx, candidates):
    req(fx); fx.pump(); env = receive(fx, 'worker-1')
    settle(fx, 'worker-1', env, candidates=candidates)
    assert result(fx)['status'] == 'invalid'
    assert fx.r.llen(fleet.RESULTS) == 1


def test_timeout_vs_late_settle_holds_slot_and_pel_until_confirmed_retirement(fx):
    fx.cfg['request_timeout'] = 900  # explicit run deadline (none by default, design §43)
    req(fx); fx.pump(); env = receive(fx, 'worker-1')
    fx.r.hset(fleet.REQ+'r1', 'created', 0)
    fx.tick()
    assert result(fx)['status'] == 'timeout'
    settle(fx, 'worker-1', env)
    assert result(fx)['status'] == 'timeout' and fx.r.llen(fleet.RESULTS) == 1
    assert fx.v2.bus.slot_state('worker-1')['pel_count'] == 1
    req(fx, 'r2'); fx.resume(); fx.pump()
    assert fx.agent('worker-1')['state'] == 'held' and fx.v2.depth('worker-1') == 1
    assert not fx.mgr.keys  # no Escape=>idle
    assert fx.v2.retire('worker-1', 'test-owned retirement')
    assert fx.v2.bus.slot_state('worker-1')['pel_count'] == 0


def test_prepared_crash_replay_ack_after_effects_and_application_loss_response(fx, monkeypatch):
    raw = {'kind':'request','request_id':'crash','spec':{'id':'crash-r','to':'worker-1','payload':{},'reply':{'required':['reply']}}}
    runtime_v2.post_event(fx.v2.bus, raw, 'crash-event')
    real_apply = fx.v2.apply
    monkeypatch.setattr(fx.v2, 'apply', lambda intent: (_ for _ in ()).throw(RuntimeError('injected crash before apply')))
    with pytest.raises(RuntimeError, match='injected crash'):
        fx.v2.step(10)
    assert not fx.r.hgetall(fleet.REQ+'crash-r')
    assert fx.r.xpending(fx.v2.events, fx.v2.group)['pending'] == 1
    monkeypatch.setattr(fx.v2, 'apply', real_apply)
    assert fx.v2.step(10)
    assert fx.r.hgetall(fleet.REQ+'crash-r') and fx.v2.depth('worker-1') == 1
    assert fx.r.xpending(fx.v2.events, fx.v2.group)['pending'] == 0
    event(fx, raw, 'different-repeat')
    assert fx.v2.depth('worker-1') == 1


def test_applying_intent_is_held_never_blindly_replayed(fx):
    intent = fx.v2.prefix + ':intent:ambiguous'
    fx.r.hset(intent, mapping={'phase':'applying','plan':'{"commands":[]}'})
    fx.v2.recover()
    assert fx.paused()
    with pytest.raises(BusError, match='ambiguous'):
        fx.v2.apply(intent)


def test_same_run_restart_never_spawns_and_active_delivery_held(fx):
    req(fx); fx.pump(); receive(fx, 'worker-1')
    count = len(fx.mgr.spawned)
    again = fleet.Fleet(fx.path, fx.r, fx.mgr)
    try:
        again.stopping = True
        again.run()
        assert fx.r.ping()
        assert len(fx.mgr.spawned) == count
        assert again.paused() and again.agent('worker-1')['state'] == 'held'
        assert again.v2.bus.slot_state('worker-1')['pel_count'] == 1
    finally:
        again.v2.close()


def test_legacy_processing_move_registration_dedup_and_original_reply(fx, monkeypatch):
    raw = json.dumps({'kind':'request','request_id':'legacy','spec':{'id':'legacy-r','payload':{},'to':'worker-1','reply':{'required':['reply']}}})
    fx.r.rpush(fleet.EVENTS, raw)
    real_post = runtime_v2.post_event
    monkeypatch.setattr(runtime_v2, 'post_event', lambda *args: (_ for _ in ()).throw(RuntimeError('crash registration')))
    with pytest.raises(RuntimeError):
        fx.v2.convert_legacy()
    assert fx.r.lrange(fx.v2.prefix+':legacy-processing',0,-1) == [raw]
    monkeypatch.setattr(runtime_v2, 'post_event', real_post)
    fx.v2.step(10)
    assert json.loads(fx.r.get('fx:reply:legacy'))['status'] == 'accepted'
    assert fx.v2.depth('worker-1') == 1
    fx.r.rpush(fleet.EVENTS, raw); fx.v2.step(10)
    assert fx.v2.depth('worker-1') == 1


def test_pause_max_busy_fifo_and_context_handoff(fx):
    fx.cfg['max_busy'] = 1
    fx.pause('operator'); req(fx,'a'); req(fx,'b',to='worker-2'); req(fx,'c')
    fx.pump(); assert fx.agent('worker-1')['state']=='idle'
    fx.resume(); fx.pump()
    assert fx.agent('worker-1')['state']=='busy' and fx.agent('worker-2')['state']=='idle'
    env=receive(fx,'worker-1'); assert env['request_id']=='a'
    settle(fx,'worker-1',env)
    fx.setf('worker-1',ctx_used=70000,ctx_window=100000,delivered=1)
    fx.pump(); assert fx.agent('worker-1')['state']=='handoff'
    handoff=receive(fx,'worker-1'); assert handoff['body']['purpose']=='handoff'
    settle(fx,'worker-1',handoff, candidates=[])
    assert fx.label('worker-1')=='worker-1.1' and fx.agent('worker-1')['state']=='starting'
    assert fx.v2.depth('worker-1')==1
    assert fx.agent('worker-1').get('handoff_pause','')=='' and not fx.paused()  # a normal rotation never holds
    # New generation must register and READY; the preserved task is never pasted.
    md=session(fx,'worker-1')
    event(fx,dict(md,kind='bridge_session',request_id='new-session'))
    fx.v2.bus.register_slot('worker-1',1,md['thread'])
    event(fx,dict(md,kind='startup',text='READY',failed=False,aborted=False,request_id='new-ready'))
    event(fx,dict(md,kind='availability',idle=True))
    fx.pump(); assert receive(fx,'worker-1')['request_id']=='c'
    assert not traffic(fx.mgr)


class Output:
    def __init__(self):
        self.frames = queue.Queue()
    def write(self, text):
        self.frames.put(json.loads(text))
    def flush(self):
        pass


def wait_until(predicate, timeout=3):
    until=time.monotonic()+timeout
    while not predicate():
        assert time.monotonic()<until, 'isolated loop did not reach expected state'
        threading.Event().wait(.01)


def test_persistent_bridge_daemon_vertical_with_separate_connections(fx):
    # Restart fixture state for direct persistent bridges; no real model/GUI.
    outputs = {slot: Output() for slot in ('supervisor','worker-1')}
    bridges = {slot: AgentBridge(fx.cfg,slot,0,outputs[slot]) for slot in outputs}
    metadata = {slot: session(fx, slot) for slot in bridges}
    observer = fleet.connect(fx.cfg)
    observer_bus = runtime_v2.bus_for(fx.cfg, observer)
    stop = threading.Event(); errors=[]
    def daemon_loop():
        try:
            while not stop.is_set():
                fx.v2.step(20); fx.pump(); fx.tick()
        except BaseException as exc:
            errors.append(exc)
    for slot in bridges:
        fx.setf(slot, state='starting', bridge_available=0)
    thread=threading.Thread(target=daemon_loop)
    thread.start()
    try:
        for slot, bridge in bridges.items():
            md=metadata[slot]
            bridge.initialize(md)
            assert len({id(c.connection_pool) for c in bridge.readers+[bridge.r]})==4
            bridge.command(dict(md,kind='startup',text='READY',failed=False,aborted=False))
            bridge.command(dict(md,kind='availability',idle=True))
        ack=bridges['supervisor'].command(dict(metadata['supervisor'],kind='operator_input',text='让 worker-1 回复 OK',correlation='ui-bridge'))
        assert ack['status']=='accepted'
        def take_delivery(slot):
            while True:
                frame=outputs[slot].frames.get(timeout=3)
                if frame['kind']=='delivery': return frame['delivery']
        sup=take_delivery('supervisor')
        bridges['supervisor'].command(dict(metadata['supervisor'],kind='settled',failed=False,aborted=False,unfinished=False,
            candidates=[{'tool':'fleet_decision','value':{'status':'RUNNING','dispatch':[{'to':'worker-1','instruction':'Reply OK in the reply field.'}],'skip':[]}}]))
        worker=take_delivery('worker-1')
        assert 'ui-bridge' not in worker['body']['text']
        bridges['worker-1'].command(dict(metadata['worker-1'],kind='settled',failed=False,aborted=False,unfinished=False,
            candidates=[{'tool':'fleet_result','value':{'reply':'OK'}}]))
        wait_until(lambda: observer.get(fleet.RESULT+ack['id']))
        assert json.loads(observer.get(fleet.RESULT+ack['id']))['status']=='ok'
        assert observer_bus.slot_state('worker-1')['pel_count']==0 and not traffic(fx.mgr)
        assert not errors
    finally:
        for bridge in bridges.values(): bridge.stop(); bridge.stop()
        stop.set(); thread.join(timeout=3)
        observer.close(); observer.connection_pool.disconnect()
    assert not thread.is_alive() and not errors


# pi+claude left this list on 2026-10-13: both rows verified by a real smoke (cli_pairs.yaml); pi+codex is still unverified.
@pytest.mark.parametrize('cfg', [dict(transport_version=2,supervisor_cli='pi',worker_cli='codex',allow_development_v2=True),
    dict(transport_version=2,supervisor_cli='codex',worker_cli='codex',allow_development_v2=True),
    dict(transport_version=2,supervisor_cli='pi',worker_cli='pi')])
def test_v2_rejects_unsupported_config_before_dependencies_or_provisioning(cfg):
    with pytest.raises(ValueError): runtime_v2.validate_config(cfg)


def test_lease_time_expires_between_preparation_and_apply_never_commits_success(fx, monkeypatch):
    import redis
    req(fx); fx.pump(); env=receive(fx,'worker-1')
    real_apply=fx.v2.apply
    def delayed(intent):
        fx.r.hset(fx.v2.bus.lease_key('worker-1'),'lease_until',1)
        return real_apply(intent)
    monkeypatch.setattr(fx.v2,'apply',delayed)
    with pytest.raises(redis.ResponseError,match='settlement_expired_before_apply'):
        settle(fx,'worker-1',env)
    assert not fx.r.get(fleet.RESULT+'r1')
    assert fx.v2.bus.slot_state('worker-1')['pel_count']==1
    monkeypatch.setattr(fx.v2,'apply',real_apply)
    fx.v2.recover()
    assert fx.paused() and fx.agent('worker-1')['state']=='held'


def test_control_events_continue_after_ambiguous_event_without_ack_of_held_record(fx):
    raw={'kind':'pause','request_id':'ambiguous-control','reason':'ambiguous'}
    runtime_v2.post_event(fx.v2.bus,raw,'ambiguous-control')
    rows=fx.r.xreadgroup(fx.v2.group,fx.v2.consumer,{fx.v2.events:'>'},count=1)
    sid=rows[0][1][0][0]
    fx.r.hset(fx.v2.prefix+':intent:ambiguous-control',mapping={'phase':'applying','plan':'{"commands":[]}'})
    assert fx.v2.step(10)
    event(fx,{'kind':'pause','request_id':'safe-control','reason':'human inspection'})
    assert json.loads(fx.r.get('fx:reply:safe-control'))['status']=='accepted'
    pending=fx.r.xpending_range(fx.v2.events,fx.v2.group,'-','+',10)
    assert [p['message_id'] for p in pending]==[sid]


def test_applied_intent_duplicate_stream_entry_ack_does_not_repeat_effects(fx):
    spec={'id':'dedup','to':'worker-1','payload':{},'reply':{'required':['reply']}}
    raw={'kind':'request','request_id':'dedup','spec':spec}
    event(fx,raw,'logical-dedup')
    fx.r.xadd(fx.v2.events,{'event_id':'logical-dedup','data':runtime_v2.dumps(raw)})
    assert fx.v2.step(10)
    assert fx.v2.depth('worker-1')==1
    assert fx.r.xpending(fx.v2.events,fx.v2.group)['pending']==0


def test_service_skip_is_ui_only_and_does_not_dispatch(fx):
    req(fx,to=None); fx.pump(); env=receive(fx,'supervisor')
    settle(fx,'supervisor',env,{'status':'RUNNING','dispatch':[],'skip':[{'reason':'no work required'}]})
    assert result(fx)['status']=='skipped'
    assert not fx.v2.depth('supervisor') and not fx.v2.depth('worker-1') and not traffic(fx.mgr)


def test_control_failure_cannot_fail_daemon_recovery_or_shutdown_redis(fx,monkeypatch):
    from fleetmux import control_view
    monkeypatch.setattr(control_view,'start_control',lambda cfg: (_ for _ in ()).throw(RuntimeError('slow panel failed')))
    fx.stopping=True
    fx.run()
    assert fx.r.ping() and fx.paused()
    assert any(fields['event']=='control.unavailable' for _,fields in fx.v2.bus.audit_tail(100))


# -- a handoff-deadline hold lifts its own pause once the old generation is retired and the next one is READY
HANDOFF_REASON = 'worker-1: handoff deadline expired'


def audits(f, name):
    return [x for _, x in f.r.xrange(f.v2.bus.audit_key) if x.get('event') == name]


def cards(f, slot):
    return [json.loads(x['data']) for _, x in f.r.xrange(f.v2.prefix + ':ui:' + slot)]


def mark(f, slot='worker-1'):
    return json.loads(f.agent(slot).get('handoff_pause') or 'null')


def timed_out_rotation(f, double=True, before_deadline=None):
    """worker-1 past its ratio takes the handoff turn (it can run tools), never writes the draft, the deadline
    passes. double: handoff_timeout=0, so the delivery deadline expires too and expire() holds first, then
    finish_rotation holds again in the same tick (the 2026-11-02 live shape); else only finish_rotation holds."""
    if double:
        f.cfg['handoff_timeout'] = 0
    req(f, 'a')
    f.pump()
    settle(f, 'worker-1', receive(f, 'worker-1'))
    f.setf('worker-1', ctx_used=70000, ctx_window=100000, delivered=1)
    req(f, 'c')                                   # business queued behind the handoff
    f.pump()
    assert f.agent('worker-1')['state'] == 'handoff'
    handoff = receive(f, 'worker-1')
    assert handoff['body']['purpose'] == 'handoff'
    old = f.agent('worker-1')['pane']
    if before_deadline:
        before_deadline()
    if double:
        time.sleep(0.15)
    else:
        f.setf('worker-1', handoff_deadline=time.time() - 1)
    f.tick()
    return handoff, old


def ready(f, slot='worker-1'):
    md = session(f, slot)
    event(f, dict(md, kind='bridge_session', request_id='reg-' + md['thread']))
    f.v2.bus.register_slot(slot, int(md['generation']), md['thread'])
    event(f, dict(md, kind='startup', text='READY', failed=False, aborted=False, request_id='ready-' + md['thread']))
    event(f, dict(md, kind='availability', idle=True))


def test_handoff_timeout_double_hold_lifts_its_own_pause_after_successor_ready(fx):
    f = fx
    handoff, old = timed_out_rotation(f)
    mid = handoff['message_id']
    hid = mid[len('m-handoff-'):]
    assert [(x['message_id'], x['error']) for x in audits(f, 'execution.held')] == [
        (mid, 'lease/delivery deadline expired; execution stop unconfirmed'), (mid, 'handoff deadline expired')]
    assert f.paused() and f.r.hget(fleet.SERVICE, 'reason') == HANDOFF_REASON
    m = mark(f)
    assert m['generation'] == 0 and m['hid'] == hid and m['at'] == f.r.hget(fleet.SERVICE, 'at')
    w = f.agent('worker-1')
    assert w['generation'] == '1' and w['state'] == 'starting' and old in f.mgr.killed
    msg = f.r.hgetall(f.v2.bus.msg_key(mid))
    assert msg['outcome'] == 'cancelled' and msg['stop_confirmed'] == '1'   # confirm_retired, unchanged
    assert f.v2.depth('worker-1') == 1                                    # 'c' still queued, not delivered
    ready(f)
    assert f.agent('worker-1')['runtime_ready'] == '1' and f.agent('worker-1')['state'] == 'idle'
    assert f.paused()                                                     # lifted by the tick, not by READY
    f.tick()
    assert not f.paused(), f.r.hget(fleet.SERVICE, 'reason')
    resumed = audits(f, 'execution.auto_resumed')
    assert len(resumed) == 1 and resumed[0]['slot'] == 'worker-1' and resumed[0]['message_id'] == mid
    detail = json.loads(resumed[0]['detail'])
    assert (detail['from_generation'], detail['to_generation']) == (0, 1)
    assert (detail['prev_label'], detail['label']) == ('worker-1', 'worker-1.1')
    assert detail['pause_reason'] == HANDOFF_REASON and detail['paused_at'] == m['at']
    for slot in ('worker-1', 'supervisor'):
        assert any(c['metadata']['event_id'].startswith('ui-autoresume-') for c in cards(f, slot)), slot
    assert f.agent('worker-1')['handoff_pause'] == ''
    assert f.r.hgetall(f.v2.bus.msg_key(mid)) == msg                      # the lift writes no delivery state
    f.pump()
    env = receive(f, 'worker-1')
    assert env['request_id'] == 'c' and int(env['generation']) == 1
    f.tick()
    assert len(audits(f, 'execution.auto_resumed')) == 1


def test_handoff_timeout_single_hold_lifts_its_own_pause(fx):
    f = fx
    handoff, old = timed_out_rotation(f, double=False)
    assert [x['error'] for x in audits(f, 'execution.held')] == ['handoff deadline expired']
    assert f.paused() and mark(f)['generation'] == 0 and mark(f)['at'] == f.r.hget(fleet.SERVICE, 'at')
    ready(f)
    f.tick()
    assert not f.paused() and len(audits(f, 'execution.auto_resumed')) == 1
    assert f.agent('worker-1')['handoff_pause'] == ''
    f.pump()
    assert receive(f, 'worker-1')['request_id'] == 'c'


def test_handoff_timeout_keeps_pause_until_successor_ready(fx):
    f = fx
    timed_out_rotation(f)
    for _ in range(3):
        f.tick()
    assert f.paused() and mark(f) and not audits(f, 'execution.auto_resumed')
    md = session(f, 'worker-1')
    event(f, dict(md, kind='bridge_session', request_id='reg-failed'))
    f.v2.bus.register_slot('worker-1', 1, md['thread'])
    f.tick()                                                              # its own lease, but no READY yet
    assert f.paused() and mark(f) and not audits(f, 'execution.auto_resumed')
    at = f.r.hget(fleet.SERVICE, 'at')
    event(f, dict(md, kind='startup', text='READY', failed=True, aborted=False, request_id='ready-failed'))
    assert f.agent('worker-1')['state'] == 'dead'
    assert f.r.hget(fleet.SERVICE, 'at') != at                            # service finish('blocked') paused again
    f.tick()
    assert f.paused() and f.agent('worker-1')['handoff_pause'] == '' and not audits(f, 'execution.auto_resumed')


def test_handoff_timeout_keeps_a_pause_set_before_the_hold(fx):
    f = fx
    timed_out_rotation(f, before_deadline=lambda: f.pause('paused by operator'))
    assert f.r.hget(fleet.SERVICE, 'reason') == HANDOFF_REASON            # today: the hold overwrites the reason
    assert f.agent('worker-1').get('handoff_pause', '') == ''            # never marked: the pause was not its own
    ready(f)
    f.tick()
    assert f.paused() and not audits(f, 'execution.auto_resumed')


@pytest.mark.parametrize('later', ['operator', 'other_hold', 'prompt_wait', 'recover'])
def test_handoff_timeout_keeps_a_pause_set_after_the_hold(fx, later):
    f = fx
    timed_out_rotation(f)
    at = f.r.hget(fleet.SERVICE, 'at')
    assert mark(f)['at'] == at
    if later == 'operator':
        f.pause('paused by operator')
    elif later == 'other_hold':
        f.v2.transaction('t-' + uuid.uuid4().hex, lambda: f.v2.hold('worker-2', 'bridge disconnected'))
    elif later == 'prompt_wait':
        f.v2.transaction('t-' + uuid.uuid4().hex, lambda: f.v2.prompt_wait('worker-2', 'g0', 'dialog', '/tmp/s'))
    else:
        f.v2.recover()
    reason = f.r.hget(fleet.SERVICE, 'reason')
    assert f.r.hget(fleet.SERVICE, 'at') != at and reason != HANDOFF_REASON
    ready(f)
    f.tick()
    assert f.paused() and f.r.hget(fleet.SERVICE, 'reason') == reason
    assert f.agent('worker-1')['handoff_pause'] == '' and not audits(f, 'execution.auto_resumed')


def test_handoff_timeout_keeps_pause_when_brake_tripped_during_pause(fx):
    f = fx
    timed_out_rotation(f)
    at, reason = f.r.hget(fleet.SERVICE, 'at'), f.r.hget(fleet.SERVICE, 'reason')
    f.brake_trip('wall', 'worker-2 hit a wall', agent='worker-2')         # already paused: reason / at untouched
    assert (f.r.hget(fleet.SERVICE, 'at'), f.r.hget(fleet.SERVICE, 'reason')) == (at, reason)
    assert f.r.hget(fleet.BRAKE, 'tripped') and mark(f)['at'] == at
    ready(f)
    f.tick()
    assert f.paused() and len(audits(f, 'execution.auto_resume_refused')) == 1
    assert f.agent('worker-1')['handoff_pause'] == '' and not audits(f, 'execution.auto_resumed')


def test_handoff_timeout_keeps_pause_when_budget_over_line(fx):
    f = fx
    timed_out_rotation(f)
    f.brake_start({'max_calls': 1})
    f.r.hset(fleet.BRAKE, mapping={'calls': 5})
    assert not f.r.hget(fleet.BRAKE, 'tripped') and f.paused() and mark(f)
    ready(f)
    f.tick()
    assert f.paused() and len(audits(f, 'execution.auto_resume_refused')) == 1
    assert f.agent('worker-1')['handoff_pause'] == '' and not audits(f, 'execution.auto_resumed')


@pytest.mark.parametrize('kill', ['refused', 'still_alive'])
def test_handoff_timeout_keeps_pause_when_old_session_not_closed(fx, kill):
    f = fx
    if kill == 'refused':
        f.mgr.kill_pane = lambda pane: False
    else:  # kill_pane says yes but the pane is still there
        f.mgr.kill_pane = lambda pane: f.mgr.killed.append(pane) or True
    handoff, old = timed_out_rotation(f)
    w = f.agent('worker-1')
    assert w['launch_ready'] == '0' and w['state'] == 'dead' and w['retired_pane'] == old
    assert 'could not close the old session' in f.r.hget(fleet.SERVICE, 'reason') and f.paused()
    assert w.get('handoff_pause', '') == ''                               # finish('blocked') paused again
    # White box (task mode may leave `at` unchanged): restore the hold's own pause and fake a READY successor.
    at = str(time.time())
    f.r.hset(fleet.SERVICE, mapping={'paused': 1, 'reason': HANDOFF_REASON, 'at': at})
    f.setf('worker-1', state='idle', runtime_ready=1, handoff_pause=json.dumps(
        {'hid': handoff['message_id'][len('m-handoff-'):], 'at': at, 'generation': 0, 'label': 'worker-1'}))
    f.tick()
    assert f.paused() and not audits(f, 'execution.auto_resumed')
    assert mark(f)['at'] == at                                            # held back by launch_ready / stop_confirmed


def test_handoff_timeout_lift_ignores_reason_overwrite(fx):
    f = fx
    timed_out_rotation(f)
    f.r.hset(fleet.SERVICE, mapping={'reason': 'supervisor decision text'})   # fleet.py service decision rewrite
    ready(f)
    f.tick()
    assert not f.paused() and len(audits(f, 'execution.auto_resumed')) == 1


def test_handoff_timeout_no_mark_for_tmux_backed_pane(fx):
    f = fx
    old = f.agent('worker-1')['pane']
    f.mgr.tmux_view_info = lambda sid: {'tmux_socket': '/x', 'tmux_session': 'worker-1-g0'} if sid == old else None
    timed_out_rotation(f)
    assert f.paused() and f.agent('worker-1').get('handoff_pause', '') == ''
    ready(f)
    f.tick()
    assert f.paused() and not audits(f, 'execution.auto_resumed')


def test_handoff_timeout_mark_dropped_when_generation_moves_on(fx):
    f = fx
    timed_out_rotation(f)
    assert mark(f)['generation'] == 0
    f.setf('worker-1', generation=2)                                      # another rotation since
    f.tick()
    assert f.agent('worker-1')['handoff_pause'] == '' and f.paused() and not audits(f, 'execution.auto_resumed')


def test_handoff_timeout_lift_keeps_other_slots_uncertain_delivery_held(fx):
    f = fx
    req(f, 'w2', to='worker-2')
    f.pump()
    env2 = receive(f, 'worker-2')
    # link_lost()'s hold (pause=False) without its watchdog restart flag: lease and message become uncertain
    f.v2.transaction('lost-' + uuid.uuid4().hex, lambda: f.v2.hold('worker-2', 'bridge heartbeat stopped', pause=False))
    assert f.agent('worker-2')['state'] == 'held' and not f.paused()
    timed_out_rotation(f)
    ready(f)
    f.tick()
    assert not f.paused() and len(audits(f, 'execution.auto_resumed')) == 1
    f.pump()
    lease2 = f.r.hgetall(f.v2.bus.lease_key('worker-2'))
    msg2 = f.r.hgetall(f.v2.bus.msg_key(env2['message_id']))
    assert f.agent('worker-2')['state'] == 'held'
    assert lease2['status'] == 'uncertain' and lease2['message_id'] == env2['message_id']
    assert msg2['status'] == 'uncertain' and not msg2.get('outcome')
    assert not f.r.get(fleet.RESULT + 'w2') or result(f, 'w2')['status'] != 'ok'


def test_handoff_timeout_no_lift_when_task_not_running(fx, monkeypatch):
    f = fx
    timed_out_rotation(f)
    assert mark(f)
    monkeypatch.setattr(f, 'running', lambda: False)
    ready(f)
    f.tick()
    assert f.paused() and f.agent('worker-1')['handoff_pause'] == '' and not audits(f, 'execution.auto_resumed')
