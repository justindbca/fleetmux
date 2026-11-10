"""M1: effect plans and file-only probe plus real private Redis integration.

The plan tests run without sockets; real Redis tests deliberately keep the repository's
strict fixture (a sandbox bind refusal is an error, never a fabricated integration pass).
"""
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from cmux_fleet import codex_queue, fleet, handoff, midturn, runtime_v2, watchdog
from test_runtime_v2_planning import plan  # noqa: F401
from test_message_bus_v2 import private_redis_v2  # noqa: F401
from test_v2_codex_claude import mixed, answer_turn, dispatch_one  # noqa: F401
from test_runtime_v2 import fx, req, receive, settle as bridge_settle  # noqa: F401


SLOT = 'worker-1'


def order_plan(plan, task=False, slot=SLOT):
    f, rt, source, env = plan
    f.advisor_slot = False
    f.midturn_probe = midturn.Probe()
    f.handoff_table, f.worker_handoff = {}, {}
    f.sessions = f.run_dir / 'sessions'
    f.cfg['pi_model'] = 'local/test'
    f.profile_swaps = lambda: {}
    f.mgr.alive.add('pane-old')
    f.mgr.owns = lambda pane: pane in f.mgr.alive
    f.mgr.capture_output = lambda pane: 'working'
    f.cfg['max_attempts'] = 4
    if task:
        f.service = False
        f.cfg['mode'] = 'task'
        source.hset(fleet.TASK, mapping={'id': 'r1', 'status': 'running', 'deadline': time.time() + 3600})
    if slot == 'supervisor':
        env.update(slot=slot, session_id='session-supervisor')
        msg = source.hgetall(rt.bus.msg_key('m-test'))
        msg.update(env)
        source.hset(rt.bus.msg_key('m-test'), mapping=msg)
        source.hset(rt.bus.lease_key(slot), mapping=dict(env, current_delivery='d-test', status='received',
                                                      lease_until=int(time.time()*1000)+300000))
        source.hset(rt.bus.lease_key(SLOT), mapping={'status': 'idle', 'current_delivery': '', 'message_id': ''})
    source.hset(fleet.key('agent', slot), mapping={'state': 'busy', 'pane': 'pane-old', 'ctx_window': 200000,
                                                'delivered': 1, 'report_retries': 1})
    hid = 'fx-' + 'a' * 16
    draft = f.run_dir / 'draft.md'
    draft.write_text(handoff.partial_template())
    source.hset(rt.bus.msg_key('m-test'), mapping={'midturn_id': hid, 'midturn_splits': 1, 'replay': 2,
                                                 'clock_paused_at': time.time(), 'deadline_remaining_ms': 123456})
    source.hset(fleet.key('agent', slot), mapping={'midturn_id': hid, 'midturn_draft': str(draft),
        'midturn_mtime': draft.stat().st_mtime_ns, 'midturn_message': 'm-test', 'midturn_request': 'r1',
        'midturn_instruction': '合法 JSON 业务', 'midturn_typed_g': 0, 'midturn_tries': 1,
        'midturn_deadline': time.time()+300, 'midturn_prompt': midturn.MIDTURN_PROMPT.format(
            midturn_id=hid, path=draft, used=160000, window=200000)})
    source.hset(fleet.REQ+'r1', mapping={'clock_paused_at': time.time()-5})
    return f, rt, source, env


def finish_turn(plan, candidates=(), **extra):
    f, rt, source, env = plan
    rt.settle(env['slot'], dict(delivery=env, candidates=list(candidates), failed=False, aborted=False,
                               unfinished=False, **extra))


def decoded(f, rt, mid):
    return rt.bus._decode(json.dumps(f.r.hgetall(rt.bus.msg_key(mid))))


def apply_plan(f, rt, source):
    """Materialize only hash/list/string effects for the next unit-test turn, not Redis/Lua acceptance."""
    for key, (kind, value) in rt.effects.values.items():
        if kind == 'hash':
            source.h[key] = dict(value)
        elif kind == 'list':
            source.l[key] = list(value)
        elif kind == 'string':
            if value is None:
                source.kv.pop(key, None)
            else:
                source.kv[key] = value
    rt.effects = runtime_v2.Effects(source)
    f.r = rt.effects
    rt.context = {'intent_id': 'unit-next-' + str(time.time_ns())}
    rt.queue_index = 0


@pytest.mark.parametrize('task,slot', [(True, SLOT), (False, SLOT), (True, 'supervisor'), (False, 'supervisor')])
def test_no_report_splits_atomically_without_report_retry_attempt_or_contract_consumption(plan, task, slot):
    f, rt, source, env = order_plan(plan, task, slot)
    rt.queue(slot, 'later work', request_id='r1')
    owners = {'owners': {slot: ['M1']}}
    source.set('fx:contracts', json.dumps(owners))
    finish_turn(plan)
    old = decoded(f, rt, 'm-test')
    assert old['outcome'] == 'handoff_split'
    mids = f.r.lrange(rt.bus.pending_key(slot), 0, -1)
    next_msg = decoded(f, rt, mids[0])
    assert next_msg['kind'] == next_msg['body']['purpose'] == 'continuation'
    assert next_msg['continues'] == 'm-test' and next_msg['midturn_splits'] == '2' and next_msg['replay'] == '2'
    assert next_msg['body']['text'].endswith('合法 JSON 业务')
    assert next_msg['deadline_remaining_ms'] == '123456'
    assert len(mids) == 2 and 'later work' in decoded(f, rt, mids[1])['body']['text']
    assert slot in rt.rotation_pending and f.agent(slot)['state'] == 'handoff'
    assert f.agent(slot)['report_retries'] == '1'
    assert json.loads(f.r.get('fx:contracts')) == owners
    assert not f.r.get(fleet.RESULT+'r1') and not f.r.lrange('fx:replies:'+slot, 0, -1)
    assert not f.r.hget(fleet.TASK, 'bad_replies')
    assert ['XACK', rt.bus.inbox_key(slot), rt.bus.slot_group(slot), '1-0'] in rt.effects.commands
    assert source.hget(rt.bus.msg_key('m-test'), 'status') == 'received'  # intent is still only a plan


@pytest.mark.parametrize('task,value', [(True, {'done':['done'], 'evidence':['file'], 'not_done':'none', 'blocked':'none'}),
                                      (False, {'reply':'OK'})])
def test_valid_report_keeps_normal_settlement_and_resumes_clock(plan, task, value):
    f, rt, source, env = order_plan(plan, task)
    reports = []
    f.report = lambda *args: reports.append(args)
    finish_turn(plan, [{'tool':'fleet_result', 'value':value}])
    assert decoded(f, rt, 'm-test')['outcome'] == 'completed'
    assert not rt.rotation_pending and not f.agent(SLOT).get('midturn_id')
    assert f.agent(SLOT)['midturn_typed_g'] == '0'
    if task:
        assert len(reports) == 1
    else:
        result = json.loads(f.r.get(fleet.RESULT+'r1'))
        assert result['result'] == value and result['clock_paused_ms'] >= 5000


@pytest.mark.parametrize('flag', ['aborted', 'failed', 'unfinished'])
def test_aborted_failed_unfinished_keeps_hold_path_and_resumes_clock(plan, flag):
    f, rt, source, env = order_plan(plan)
    ev = dict(delivery=env, candidates=[], failed=False, aborted=False, unfinished=False)
    ev[flag] = True
    rt.settle(SLOT, ev)
    assert f.agent(SLOT)['state'] == 'held' and not rt.rotation_pending
    assert not any(cmd[0] == 'XACK' for cmd in rt.effects.commands)
    assert not f.agent(SLOT).get('midturn_id')


def test_wall_wins_over_midturn_and_does_not_split(plan):
    f, rt, source, env = order_plan(plan)
    f.on_wall = lambda *args: {'wall': {'scope': 'account'}}
    f.notify_later = lambda rec: None
    rt.settle(SLOT, dict(delivery=env, candidates=[], failed=False, aborted=False,
                       wall_text="You've hit your usage limit"))
    assert decoded(f, rt, 'm-test')['outcome'] == 'walled'
    assert not rt.rotation_pending and not f.agent(SLOT).get('midturn_id')


@pytest.mark.parametrize('task,slot', [(True, SLOT), (False, SLOT), (True, 'supervisor'), (False, 'supervisor')])
def test_split_cap_routes_to_existing_blocked_intake(plan, task, slot):
    f, rt, source, env = order_plan(plan, task, slot)
    source.hset(rt.bus.msg_key('m-test'), mapping={'midturn_splits': 3})
    f.pending_notify = []
    rt.midturn_blocked(slot, rt.bus.message('m-test'))
    assert f.agent(slot)['state'] == 'held'
    assert f.r.hget(rt.bus.msg_key('m-test'), 'midturn_limit') == '1'
    assert not f.mgr.sent and not f.mgr.killed
    assert not f.r.hget(fleet.REQ+'r1', 'clock_paused_at') or task
    if slot != 'supervisor':
        escalation = f.escalation_state()['blocked:'+slot]
        assert '请拆小' in escalation['reason'] and escalation['message_id']
        assert rt.depth('supervisor') == 1
        assert not f.paused()
    elif task:
        assert f.task()['status'] == 'blocked'
    else:
        assert f.paused()


def test_resume_requeue_keeps_split_count_and_original_instruction(plan):
    f, rt, source, env = order_plan(plan, task=True)
    msg = rt.bus.message('m-test')
    source.hset(rt.bus.msg_key('m-test'), mapping=dict(kind='continuation', continues='m-previous', midturn_instruction='original'))
    lost = rt.lost_dispatches()
    assert len(lost) == 1 and lost[0]['message_id'] == 'm-test'
    rt.requeue_lost(lost)
    next_msg = decoded(f, rt, f.r.lrange(rt.bus.pending_key(SLOT), 0, -1)[0])
    assert next_msg['midturn_splits'] == '1' and next_msg['continues'] == 'm-previous'
    assert next_msg['replay'] == '3' and next_msg['kind'] == 'continuation'
    assert next_msg['midturn_instruction'] == 'original'


def test_main_apply_drops_old_generation_and_other_message_results(plan):
    f, rt, source, env = order_plan(plan)
    for change in ({'generation': 9}, {'message_id': 'm-old'}):
        f.midturn_probe.results[SLOT] = dict(cli='pi', generation=0, message_id='m-test', used=190000,
                                            window=200000, **{}) | change
        f.midturn_apply()
    assert not f.mgr.sent
    assert f.agent(SLOT).get('ctx_used') is None


@pytest.mark.parametrize('guard', ['paused', 'prompt_wait', 'clear_required', 'switching', 'adopted', 'unowned'])
def test_guards_prevent_prompt_and_kill(plan, guard, monkeypatch):
    f, rt, source, env = order_plan(plan)
    source.hset(fleet.key('agent', SLOT), mapping={'midturn_send_pending': 1})
    if guard == 'paused':
        f.pause('test')
    elif guard == 'unowned':
        f.mgr.owns = lambda pane: False
    elif guard == 'adopted':
        monkeypatch.setattr(fleet.subfleet, 'adopted', lambda *args: True)
    else:
        source.hset(fleet.key('agent', SLOT), mapping={
            'profile_state' if guard == 'switching' else guard: 'switching' if guard == 'switching' else '1'})
    f.midturn_type(SLOT)
    assert not f.mgr.sent and not f.mgr.killed


def test_prompt_wait_stops_timeout_then_restores_remaining_time(plan, monkeypatch):
    f, rt, source, env = order_plan(plan)
    now = time.time()
    source.hset(fleet.key('agent', SLOT), mapping={'midturn_deadline': now-400, 'prompt_wait': 'd-test',
                                                'midturn_wait_at': now-800})
    monkeypatch.setattr(rt, 'transaction', lambda identity, action: action())
    f.midturn_timeouts()
    assert not f.mgr.sent and not f.mgr.killed
    f.r.hset(fleet.key('agent', SLOT), mapping={'prompt_wait': ''})
    f.midturn_timeouts()
    assert float(f.agent(SLOT)['midturn_deadline']) > time.time()+399
    assert not f.mgr.sent


def test_timeout_retries_once_then_calls_only_watchdog_rotate(plan, monkeypatch):
    f, rt, source, env = order_plan(plan)
    f.r.hset(fleet.key('agent', SLOT), mapping={'midturn_deadline': 1})
    f.midturn_timeouts()
    assert len(f.mgr.sent) == 1 and f.agent(SLOT)['midturn_tries'] == '2'
    f.r.hset(fleet.key('agent', SLOT), mapping={'midturn_deadline': 1})
    calls = []
    rt.watchdog = SimpleNamespace(rotate=lambda *args, **kw: calls.append((args, kw)))
    f.midturn_timeouts()
    assert len(f.mgr.sent) == 1 and len(calls) == 1
    assert calls[0][1] == {'reason': 'midturn'} and not f.mgr.keys


@pytest.mark.parametrize('extra,ok', [('registered', True), ('operator', False), ('stale', False)])
def test_codex_binding_only_accepts_exact_registered_extra_inputs(plan, monkeypatch, extra, ok):
    f, rt, source, env = order_plan(plan)
    rec = dict(cq_delivery='d', cq_token='fx-token', cq_state='queued', cq_thread='thread', cq_generation='0',
               cq_text='business\n(fleet delivery fx-token)', **{k:v for k,v in f.agent(SLOT).items() if k.startswith('midturn_')})
    text = rec['midturn_prompt'] if extra != 'operator' else 'unregistered steer'
    if extra == 'stale':
        rec['midturn_typed_g'] = '1'
    monkeypatch.setattr(codex_queue, 'turn_record', lambda *args: dict(trigger='queue', users=[
        {'text':rec['cq_text']}, {'text':text}, {'text':text}]))
    assert codex_queue.bound(rec, 'thread', 'turn', Path('/sessions'), generation=0)[0] is ok


def test_claude_supervisor_prompt_hook_exact_match_does_not_submit_operator_input(plan, monkeypatch):
    f, rt, source, env = order_plan(plan, slot='supervisor')
    cfg = dict(f.cfg, supervisor_cli='claude')
    monkeypatch.setattr(fleet, 'load', lambda path: cfg)
    source.connection_pool = SimpleNamespace(disconnect=lambda: None)
    monkeypatch.setattr(fleet, 'connect', lambda cfg: source)
    monkeypatch.setattr(runtime_v2, 'bus_for', lambda cfg, r: rt.bus)
    monkeypatch.setattr(runtime_v2, 'post_event', lambda *args: pytest.fail('midturn became operator input'))
    prompt = f.agent('supervisor')['midturn_prompt']
    raw = json.dumps({'prompt': prompt, 'session_id':'session-supervisor'})
    assert fleet.claude_prompt('/config', 'supervisor', 0, raw) is None


# Actual Redis / actual hooks (fake panes); these run on the orchestrator's unsandboxed rerun.
def test_real_worker_handoff_split_rotates_and_preserves_fifo(mixed):
    f = mixed
    dispatch_one(f)
    slot = SLOT
    info = f.agent(slot)
    msg = f.v2.bus.message(f.r.hget(f.v2.bus.lease_key(slot), 'message_id'))
    later = f.v2.queue(slot, 'later', request_id=msg['request_id'])
    f.midturn_order(slot, msg, 800000, 1000000, midturn.DEFAULTS)
    f.midturn_timeouts()
    draft = Path(f.agent(slot)['midturn_draft'])
    draft.write_text('## Done (facts)\n- part done\n## In progress\n- stopped in step 2\n## Next steps (in order)\n- finish step 2\n')
    answer_turn(f, slot, 'HANDOFF', prompt=msg['body']['text'])
    assert f.v2.bus.message(msg['message_id'])['outcome'] == 'handoff_split'
    assert f.agent(slot)['generation'] == '1' and f.agent(slot)['state'] == 'starting'
    pending = f.v2.bus.pending(slot)
    assert pending[1] == later and f.v2.bus.message(pending[0])['continues'] == msg['message_id']
    assert f.v2.bus.slot_state(slot)['pel_count'] == 0
    assert info['pane'] in f.mgr.killed
    assert 'stopped in step 2' in next((f.run_dir/'handoffs').glob('*.md')).read_text()


def test_real_pi_service_split_and_offer_resumes_request_clock(fx):
    f = fx
    req(f); f.pump(); env = receive(f, SLOT)
    msg = f.v2.bus.message(env['message_id'])
    f.midturn_order(SLOT, msg, 160000, 200000, midturn.DEFAULTS)
    bridge_settle(f, SLOT, env, candidates=[])
    assert f.v2.bus.message(env['message_id'])['outcome'] == 'handoff_split'
    assert f.r.hget(fleet.REQ+'r1', 'clock_paused_at')
    assert f.agent(SLOT)['generation'] == '1'


@pytest.mark.parametrize('cli,bracketed', [('pi', False), ('claude', True), ('codex', False)])
def test_threshold_types_exactly_once_per_generation_and_updates_control(plan, monkeypatch, cli, bracketed):
    f, rt, source, env = order_plan(plan)
    f.r.hset(fleet.key('agent', SLOT), mapping={k:'' for k in f.agent(SLOT) if k.startswith('midturn_')})
    f.cfg['worker_cli'] = cli
    monkeypatch.setattr(rt, 'transaction', lambda identity, action: action())
    result = dict(cli=cli, generation=0, message_id='m-test', used=160000, window=200000)
    f.midturn_probe.results[SLOT] = result
    f.midturn_apply()
    if cli == 'claude':
        # design.md §34: never typed into a Claude pane; the PostToolUse hook takes the pending prompt.
        assert f.mgr.sent == [] and f.agent(SLOT)['midturn_send_pending'] == '1'
        prompt = midturn.registered_prompt(f.agent(SLOT), 0)
        assert prompt.startswith('[FLEET MID-TURN HANDOFF fx-')
    else:
        assert len(f.mgr.sent) == 1 and f.mgr.bracketed[-1][1] is bracketed
        prompt = f.mgr.sent[0][1]
    assert len(prompt.splitlines()) == 1
    assert midturn.registered_prompt(f.agent(SLOT), 0) == prompt
    assert f.agent(SLOT)['ctx_used'] == '160000'
    assert not f.agent(SLOT).get('handoff_id')
    assert float(f.agent(SLOT)['midturn_deadline']) > time.time()+299
    f.midturn_probe.results[SLOT] = result
    f.midturn_apply()
    assert len(f.mgr.sent) == (0 if cli == 'claude' else 1)
    assert any('ctx=160000/200000 mid=160000' in str(command) for command in rt.effects.commands)


def test_type_failure_one_immediate_retry_then_waits_for_timed_fallback(plan):
    f, rt, source, env = order_plan(plan)
    f.mgr.alive.clear()
    f.mgr.owns = lambda pane: True
    f.r.hset(fleet.key('agent', SLOT), mapping={'midturn_send_pending': 1})
    f.midturn_timeouts(); f.midturn_timeouts(); f.midturn_timeouts()
    assert len(f.mgr.sent) == 2 and not f.agent(SLOT).get('midturn_send_pending') == '1'
    assert f.agent(SLOT)['midturn_tries'] == '1'


def test_second_split_keeps_original_reply_spec_and_does_not_nest_prefixes(plan):
    f, rt, source, env = order_plan(plan)
    first = rt.bus.message('m-test')
    first['body']['reply'] = {'required':['reply']}
    first.update(kind='continuation', midturn_instruction='original unchanged', original_kind='worker')
    first['body']['text'] = '[Continuation] previous wrapper'
    rt.midturn_split(SLOT, env, first)
    again = decoded(f, rt, f.r.lrange(rt.bus.pending_key(SLOT),0,0)[0])
    assert again['body']['reply'] == {'required':['reply']}
    assert again['body']['text'].count('[Continuation]') == 1
    assert again['body']['text'].endswith('original unchanged')
    assert again['original_kind'] == 'worker'


def test_request_expiry_and_queue_deadline_use_paused_age(plan):
    f, rt, source, env = order_plan(plan)
    now = time.time()
    source.hset(fleet.REQ+'r1', mapping={'created':now-1000, 'clock_paused_at':now-990})
    f.cfg['request_timeout'] = 20
    f.expire_requests(now)
    assert not f.r.get(fleet.RESULT+'r1')
    rt.midturn_resume_request('r1')
    queued = rt.queue(SLOT, '[Request r1] remaining business')
    assert decoded(f, rt, queued)['deadline_at'] == runtime_v2.NO_DEADLINE_MS   # §43: expiry is expire()'s alone
    f.cfg['request_timeout'] = None                                             # no deadline: never expires
    f.expire_requests(now+11)
    assert not f.r.get(fleet.RESULT+'r1')
    f.cfg['request_timeout'] = 20
    f.expire_requests(now+11)
    result = json.loads(f.r.get(fleet.RESULT+'r1'))
    assert result['status'] == 'timeout' and result['clock_paused_ms'] >= 990000


def test_continuation_deliver_skips_live_profile_and_advisor_but_retains_task_binding(plan, monkeypatch):
    f, rt, source, env = order_plan(plan, task=True)
    finish_turn(plan)
    apply_plan(f, rt, source)
    f.setf(SLOT, state='idle', delivered=0, bridge_available=1, generation=1)
    f.profile_swaps = lambda: {SLOT:'changed roster profile'}
    f.prepare_worker_profile = lambda *args: pytest.fail('continuation re-routed profile')
    f.advise_before_delivery = lambda *args: pytest.fail('continuation consulted advisor')
    f.usage = lambda slot: (10, 200000)
    delivered = []
    monkeypatch.setattr(rt, 'deliver', lambda slot: delivered.append(slot))
    f.deliver(SLOT)
    assert delivered == [SLOT]
    f.r.hset(fleet.TASK, mapping={'id':'other-task'})
    f.finish = lambda status, reason: delivered.append(status)
    f.deliver(SLOT)
    assert delivered[-1] == 'blocked'


def test_successor_ready_over_midturn_line_blocks_instead_of_rotating_again(plan, monkeypatch):
    f, rt, source, env = order_plan(plan, task=True)
    finish_turn(plan)
    apply_plan(f, rt, source)
    f.setf(SLOT, state='idle', delivered=0, bridge_available=1, generation=1)
    f.usage = lambda slot: (160000, 200000)
    monkeypatch.setattr(rt, 'transaction', lambda identity, action: action())
    f.deliver(SLOT)
    assert rt.depth(SLOT) == 0
    assert '请拆小' in f.escalation_state()['blocked:'+SLOT]['reason']
    assert not f.mgr.sent and not f.mgr.killed


@pytest.mark.parametrize('failure', [None, 'kill', 'paused', 'unowned', 'prompt_wait'])
def test_watchdog_midturn_kill_continues_without_frozen_report_and_failure_resumes_clock(plan, monkeypatch, failure):
    f, rt, source, env = order_plan(plan)
    # Exercise the real rotation method; only launch, retirement proof and Redis execution are stand-ins.
    f.refresh_history = lambda: None
    def spawn(slot, prompt, launch_held=False):
        f.mgr.alive.add('pane-new')
        f.setf(slot, pane='pane-new', state='starting', delivered=0, launch_ready=0)
        return 'pane-new'
    f.spawn = spawn
    monkeypatch.setattr(rt, 'transaction', lambda identity, action: action())
    monkeypatch.setattr(rt, 'confirm_retired', lambda slot: f.r.hset(rt.bus.lease_key(slot),
        mapping={'status':'idle', 'current_delivery':'', 'message_id':''}))
    monkeypatch.setattr(f.r, 'hdel', lambda key, field: f.r.value(key, 'hash').pop(field, None), raising=False)
    wd = watchdog.Watchdog(rt)
    wd.tell = lambda *args: pytest.fail('midturn emitted FROZEN report')
    if failure == 'kill':
        original = f.mgr.kill_pane
        f.mgr.kill_pane = lambda pane: False if pane == 'pane-old' else original(pane)
    elif failure == 'paused':
        f.pause('operator paused')
    elif failure == 'unowned':
        f.mgr.owns = lambda pane: False
    elif failure == 'prompt_wait':
        f.setf(SLOT, prompt_wait='d-test')
    ok = wd.rotate(SLOT, 'midturn timeout', 'screen', reason='midturn')
    if failure:
        assert not ok and not rt.depth(SLOT)
        if failure == 'prompt_wait':
            assert f.agent(SLOT)['state'] == 'busy' and not f.mgr.killed
        else:
            assert f.agent(SLOT)['state'] == 'held'
            assert f.r.hget(fleet.REQ+'r1','clock_paused_at') == ''
    else:
        assert ok and f.agent(SLOT)['generation'] == '1' and f.agent(SLOT)['launch_ready'] == '1'
        assert decoded(f, rt, 'm-test')['outcome'] == 'handoff_split'
        assert decoded(f, rt, f.r.lrange(rt.bus.pending_key(SLOT),0,0)[0])['continues'] == 'm-test'
        assert f.mgr.killed == ['pane-old']
        assert 'Mid-turn continuation' in next((f.run_dir/'handoffs').glob('*.md')).read_text()


@pytest.mark.parametrize('reason,msg,text', [
    ('daemon holds execution; stop confirmation required', {'midturn_limit': '1'}, midturn.LIMIT_STATUS),
    ('daemon holds execution; stop confirmation required', {'uncertain_reason': 'blocked: reviewer: no candidate'},
     '已被 fleet 扣下：blocked: reviewer: no candidate'),
    ('lease expired; delivery held', {'midturn_limit': '1'}, '通信中断；fleet 将自动重启本会话。'),
    ('Error 61 connecting to unix socket', {}, '通信中断；fleet 将自动重启本会话。')])
def test_bridge_fault_text_says_held_or_link_lost(reason, msg, text):
    """The pane says "通信中断" only when the link itself is gone; a daemon hold shows the daemon's reason."""
    import threading
    from cmux_fleet.agent_bridge import AgentBridge
    frames = []
    bus = SimpleNamespace(message=lambda mid: msg, mark_uncertain=lambda *a: None)
    me = SimpleNamespace(lock=threading.RLock(), faulted=False, stop_event=threading.Event(),
                         idle_event=threading.Event(), active={'message_id': 'm-test'}, bus=bus,
                         emit=frames.append, slot=SLOT, event=lambda *a: None)
    AgentBridge.fault(me, reason)
    assert frames == [{'kind': 'fault', 'reason': text}]
