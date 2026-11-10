"""dsh workers, daemon side (design §41 v1), on a private Redis. Events are posted as agent_bridge.py posts them
for a dsh slot (bound to run / slot / generation / session; settled candidates from the bridge plugin's
fleet_result tool): READY startup, a report reaching the supervisor, a missing fleet_result corrected in the
fleet_result wording, the permission channel (delete -> user, other -> supervisor ruling as a permission_reply
card), no mid-turn handoff, and a spawn that keeps the API key off the command line."""
import json
import sys
import uuid

import pytest

from test_fleet import FakeMgr
from test_message_bus_v2 import private_redis_v2  # noqa: F401
from cmux_fleet import dsh_adapter, fleet, pi_adapter, runtime_v2

MODEL = 'flashnext/Qwen3.8-Flash-Next-Uncensored-MLX-Serve-4bit'
REPORT = {'done': ['wrote worker-1/ok.txt'], 'evidence': ['worker-1/ok.txt 2 bytes'], 'not_done': 'none', 'blocked': 'none'}


def boot(r, tmp_path, mode='task'):
    r.flushdb()
    run = tmp_path / ('run-' + uuid.uuid4().hex)
    (run / 'workspace').mkdir(parents=True)
    cfg = dict(run=str(run), redis_socket=r.connection_pool.connection_kwargs['path'], python=sys.executable,
               workers=['worker-1', 'worker-2'], mode=mode, supervisor_cli='pi', worker_cli='dsh', pi='pi',
               pi_model='local/test', dsh='/opt/dsh/bin.js', dsh_node='/opt/node', dsh_model=MODEL,
               pi_home=str(run / 'pi-home'), allow_unsandboxed_pi=True, transport_version=2,
               allow_development_v2=True, stall_timeout=0, v2_lease_ms=300000, lifecycle='smoke')
    path = run / 'config.json'
    fleet.save(path, cfg)
    f = fleet.Fleet(path, r, FakeMgr())
    f.start_all()
    for slot in f.names:
        meta = session(f, slot)
        event(f, dict(meta, kind='bridge_session', request_id='register-' + slot))
        f.v2.bus.register_slot(slot, int(meta['generation']), meta['thread'])
        event(f, dict(meta, kind='startup', request_id='ready-' + slot, text='Reading AGENTS.md.\n\nREADY',
                      failed=False, aborted=False, error=''))
        event(f, dict(meta, kind='availability', idle=True))
    r.set(f.v2.prefix + ':boot', 'running')
    return f


@pytest.fixture
def dfx(private_redis_v2, tmp_path):
    f = boot(private_redis_v2, tmp_path)
    yield f
    f.v2.close()


def session(f, slot):
    gen = int(f.agent(slot)['generation'])
    if slot == 'supervisor':
        transcript, thread = pi_adapter.session_dir(f.cfg, slot, gen) / 'session.jsonl', 'session-sup-g' + str(gen)
    else:
        thread = str(uuid.uuid5(uuid.NAMESPACE_DNS, slot + str(gen)))
        transcript = dsh_adapter.session_dir(f.cfg, slot, gen) / (thread + '.jsonl')
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.touch()
    return dict(agent=slot, generation=gen, run_id=f.v2.bus.run_id, thread=thread, transcript=str(transcript),
                ctx_used=6658, ctx_window=400000)


def event(f, ev):
    runtime_v2.post_event(f.v2.bus, ev, 'e-' + uuid.uuid4().hex)
    assert f.v2.step(10)


def dispatch(f, instruction='Write worker-1/ok.txt containing OK.'):
    event(f, dict(kind='task', request_id='t-' + uuid.uuid4().hex[:8], spec={'goal': 'smoke goal for dsh'}))
    f.pump()
    sup = f.v2.bus.receive('supervisor', 'bridge-supervisor-g0', generation=0, session_id=f.agent('supervisor')['thread'])
    event(f, dict(session(f, 'supervisor'), kind='settled', delivery=sup, failed=False, aborted=False, unfinished=False,
                  candidates=[{'tool': 'fleet_decision', 'value': {'status': 'RUNNING', 'completion': 10,
                                                                   'dispatch': [{'to': 'worker-1', 'instruction': instruction}]}}]))
    f.pump()
    return f.v2.bus.receive('worker-1', 'bridge-worker-1-g0', generation=0, session_id=f.agent('worker-1')['thread'])


def settle(f, env, value=None, slot='worker-1'):
    """What agent_bridge.py posts for a dsh settled frame: the plugin's fleet_result candidates."""
    event(f, dict(session(f, slot), kind='settled', delivery=env, failed=False, aborted=False, unfinished=False,
                  candidates=[{'tool': 'fleet_result', 'value': value}] if value else []))


def ui_cards(f, slot):
    return [json.loads(fields['data']) for _, fields in f.r.xrange(f.v2.prefix + ':ui:' + slot)]


def test_startup_text_ending_in_ready_settles_and_usage_is_recorded(dfx):
    info = dfx.agent('worker-1')
    assert info['state'] == 'idle' and info['runtime_ready'] == '1'
    assert (info['ctx_used'], info['ctx_window']) == ('6658', '400000')


def test_spawn_writes_the_fleet_result_role_and_keeps_the_key_off_the_command_line(dfx):
    spawned = [kw for kw in dfx.mgr.spawned if kw['start_directory'].endswith('worker-1')]
    assert spawned and spawned[-1]['env_vars'].keys() == {'PATH'}
    assert spawned[-1]['metadata'] == {'type': 'worker', 'cli_type': 'dsh', 'profile': MODEL}
    role = (fleet.agent_cwd(dfx.cfg, 'worker-1') / 'AGENTS.md').read_text()
    assert 'Final tool: fleet_result' in role and 'Done: <one item per line' not in role


def test_a_fleet_result_report_is_committed_and_reaches_the_supervisor(dfx):
    env = dispatch(dfx)
    settle(dfx, env, REPORT)
    assert dfx.v2.bus.message(env['message_id'])['status'] == 'committed'
    assert any('worker-1/ok.txt 2 bytes' in t for t in dfx.v2.pending_text('supervisor'))


def test_a_turn_without_fleet_result_gets_the_fleet_result_correction(dfx):
    env = dispatch(dfx)
    settle(dfx, env)
    assert dfx.agent('worker-1')['report_retries'] == '1' and dfx.task()['status'] == 'running'
    correction = dfx.v2.pending_text('worker-1')[0]
    assert 'Call fleet_result exactly once' in correction and 'Done: / Evidence:' not in correction


def test_delete_permission_goes_to_the_user_in_the_dsh_tui(dfx):
    env = dispatch(dfx)
    event(dfx, dict(session(dfx, 'worker-1'), kind='permission_request', delivery=env, session=dfx.agent('worker-1')['thread'],
                    request={'id': 'call_1', 'action': 'bash', 'resources': ['rm -rf /outside/build'],
                             'message': 'clean up'}))
    info = dfx.agent('worker-1')
    assert (info['prompt_route'], info['prompt_channel']) == ('user', 'dsh') and dfx.paused()
    assert any('该 dsh 窗口里批准或拒绝' in c['render'] for c in ui_cards(dfx, 'supervisor'))
    assert any(a.get('event') == 'dsh.permission_request' for _, a in dfx.r.xrange(dfx.v2.bus.audit_key))


def test_other_permission_is_ruled_by_the_supervisor_as_a_permission_reply_card(dfx):
    env = dispatch(dfx)
    event(dfx, dict(session(dfx, 'worker-1'), kind='permission_request', delivery=env, session=dfx.agent('worker-1')['thread'],
                    request={'id': 'call_2', 'action': 'write', 'resources': ['/outside/x.txt'], 'message': 'step 3'},
                    **{'class': 'other'}))
    key = fleet.DIALOG_KEY + 'worker-1:g0'
    assert dfx.escalation_state()[key]['channel'] == 'permission_reply'
    assert any('(dsh) asks permission' in t for t in dfx.v2.pending_text('supervisor'))
    dfx.dialog_answer(key, {'decision': 'approve'})
    (card,) = [c for c in ui_cards(dfx, 'worker-1') if c.get('kind') == 'permission_reply']
    assert (card['request_id'], card['decision'], card['session_id']) == ('call_2', 'once', dfx.agent('worker-1')['thread'])


def test_no_mid_turn_handoff_for_dsh_is_said_once_as_a_warning(dfx):
    dfx.v2.transaction('mt-' + uuid.uuid4().hex, lambda: dfx.v2.midturn_no_usage('worker-1', 0))
    rows = [a for _, a in dfx.r.xrange(dfx.v2.bus.audit_key) if a.get('event') == 'midturn.no_usage']
    assert rows and rows[-1]['level'] == 'warn' and 'dsh' in rows[-1]['error']


def test_handoff_turn_of_a_dsh_worker_settles_without_a_report_and_rotates(dfx):
    env = dispatch(dfx)
    settle(dfx, env, REPORT)
    dfx.setf('worker-1', ctx_used=300000, ctx_window=400000)  # over 0.6 x the measured 400K window
    event(dfx, dict(session(dfx, 'worker-1'), kind='availability', idle=True, ctx_used=300000))
    dfx.pump()
    hand = dfx.v2.bus.receive('worker-1', 'bridge-worker-1-g0', generation=0, session_id=dfx.agent('worker-1')['thread'])
    assert hand and hand['body']['purpose'] == 'handoff'
    settle(dfx, hand)  # a handoff turn takes no fleet_result (the plugin refuses it)
    assert dfx.v2.bus.message(hand['message_id'])['status'] == 'committed'
    assert dfx.agent('worker-1')['generation'] == '1'  # rotated: next generation in the same slot
