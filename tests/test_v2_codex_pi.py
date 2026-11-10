"""Transport v2 with a Codex TUI supervisor and Pi bridge workers in one run (codex+pi).

Real private Redis + the real Codex notify hook entry for the supervisor; the Pi
workers' bridge is driven through the same v2 events it posts (bridge_session /
startup / availability / settled). Fake cmux, no model. Proves the mixed daemon
wiring, not a real Codex/Pi TUI (that is scripts/smoke_pair.sh codex pi <mode>).
"""
import json
import sys
import uuid

import pytest

from test_fleet import FakeMgr
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_v2_codex_claude import answer_turn, decision, drive, no_message_files, typed  # noqa: F401
from test_runtime_v2 import event, receive, session, settle
from cmux_fleet import fleet, runtime_v2

WORKERS = ['worker-1', 'worker-2']


# ------------------------------------------------------------------ gate
@pytest.mark.parametrize('mode', ['task', 'service'])
def test_codex_pi_verified_by_real_smoke(mode):
    # Real smoke codex+pi/<mode> passed (cli_pairs.yaml verified_by): it runs without smoke now,
    # and the development opt-in is still required.
    cfg = dict(transport_version=2, supervisor_cli='codex', worker_cli='pi', mode=mode,
               allow_development_v2=True)
    assert runtime_v2.validate_config(cfg) == 2
    with pytest.raises(ValueError, match='allow-development-v2'):
        runtime_v2.validate_config(dict(cfg, allow_development_v2=False))


# ------------------------------------------------------------------ live wiring
def pi_ready(f, slot):
    metadata = session(f, slot)
    event(f, dict(metadata, kind='bridge_session', request_id='register-' + slot))
    f.v2.bus.register_slot(slot, int(f.agent(slot)['generation']), metadata['thread'])
    event(f, dict(metadata, kind='startup', request_id='ready-' + slot, text='READY', failed=False, aborted=False))
    event(f, dict(metadata, kind='availability', idle=True))


def build(r, tmp_path, mode):
    r.flushdb()
    run = tmp_path / ('run-' + uuid.uuid4().hex)
    (run / 'workspace').mkdir(parents=True)
    sessions = tmp_path / 'codex-home' / 'sessions'
    cfg = dict(run=str(run), redis_socket=r.connection_pool.connection_kwargs['path'],
               python=sys.executable, workers=list(WORKERS), mode=mode, supervisor_cli='codex',
               worker_cli='pi', pi='pi', pi_model='local/test', pi_home=str(run / 'pi-home'),
               allow_unsandboxed_pi=True, codex_home=str(tmp_path / 'codex-home'), transport_version=2,
               allow_development_v2=True, lifecycle='smoke', stall_timeout=0, v2_lease_ms=300000, max_busy=0)
    path = run / 'config.json'
    fleet.save(path, cfg)
    f = fleet.Fleet(path, r, FakeMgr(), sessions=sessions)
    f.test_sessions = sessions
    f.test_threads = {'supervisor': str(uuid.uuid4())}
    f.start_all()
    r.set(f.v2.prefix + ':boot', 'running')
    assert answer_turn(f, 'supervisor', 'READY', startup=True)['status'] == 'ready'
    for slot in WORKERS:
        pi_ready(f, slot)
    return f


@pytest.fixture
def codex_pi(private_redis_v2, tmp_path):  # noqa: F811
    f = build(private_redis_v2, tmp_path, 'task')
    yield f
    f.v2.close()


@pytest.fixture
def codex_pi_service(private_redis_v2, tmp_path):  # noqa: F811
    f = build(private_redis_v2, tmp_path, 'service')
    yield f
    f.v2.close()


def submit(f):
    runtime_v2.post_event(f.v2.bus, dict(kind='task', request_id='new-task', spec={'goal': 'nightly parts'}), 'new-task')
    assert f.v2.step(10)
    f.pump()


def task_decision(workers):
    value = json.loads(decision(workers))
    value['artifacts'] = [{'path': w + '/part.md', 'contains': ['findings']} for w in workers]
    return json.dumps(value)


REPORT = {'done': ['wrote part.md'], 'evidence': ['worker-1/part.md'], 'not_done': 'none', 'blocked': 'none'}


def test_mixed_ready_binds_tui_supervisor_and_pi_bridges(codex_pi):
    f = codex_pi
    for slot in f.names:
        assert f.agent(slot)['state'] == 'idle' and f.agent(slot)['runtime_ready'] == '1'
    assert runtime_v2.tui_slot(f.cfg, 'supervisor') and not runtime_v2.tui_slot(f.cfg, 'worker-1')
    sup_role = (fleet.agent_cwd(f.cfg, 'supervisor') / 'AGENTS.md').read_text()
    assert 'fleet_decision' not in sup_role                     # Codex keeps the AGENTS.md JSON wording
    assert sorted(s['metadata']['cli_type'] for s in f.mgr.spawned) == ['codex', 'pi', 'pi']
    assert 'PI_' in json.dumps(sorted(set().union(*(s['env_vars'] for s in f.mgr.spawned
                                                     if s['metadata']['cli_type'] == 'pi'))))


def test_codex_supervisor_dispatches_pi_workers_and_pi_report_reaches_codex(codex_pi):
    f = codex_pi
    submit(f)
    body, = typed(f, 'supervisor')
    assert 'Reply with the JSON format from AGENTS.md.' in body and 'fleet_decision' not in body
    assert answer_turn(f, 'supervisor', task_decision(['worker-1']))['status'] == 'committed'
    f.pump()
    assert not [p for p, _ in f.mgr.sent if p != f.agent('supervisor')['pane']]   # Pi gets no keystrokes
    env = receive(f, 'worker-1')
    assert env and 'Write worker-1/part.md' in env['body']['text'] and '.inbox' not in env['body']['text']
    out = f.workspace() / 'worker-1' / 'part.md'
    out.parent.mkdir(parents=True, exist_ok=True); out.write_text('findings')
    settle(f, 'worker-1', env, REPORT)
    assert f.v2.bus.message(env['message_id'])['outcome'] == 'completed'
    f.pump()
    followup = typed(f, 'supervisor')[-1]
    assert followup.startswith('[worker-1 report]') and 'wrote part.md' in followup
    done = json.dumps({'status': 'COMPLETED', 'completion': 100, 'dispatch': [], 'reason': 'verified'})
    assert answer_turn(f, 'supervisor', done)['status'] == 'committed'
    assert f.task()['status'] == 'completed'
    assert no_message_files(f)


def test_mixed_stale_duplicate_and_manual_turns_rejected(codex_pi):
    f = codex_pi
    submit(f)
    assert answer_turn(f, 'supervisor', decision(['worker-1']), prompt='operator chatting')['status'] == 'ignored'
    late = fleet.tui_turn(f.cfg, 'supervisor', 4, thread=f.test_threads['supervisor'], transcript='',
                          text=decision(['worker-1']), inputs=[typed(f, 'supervisor')[-1]], wait=1)
    assert late['status'] == 'stale'                            # retired Codex generation
    stale = dict(session(f, 'worker-1'), generation=4)
    assert f.v2.bus.slot_state('supervisor')['status'] == 'received'
    assert answer_turn(f, 'supervisor', decision(['worker-1']))['status'] == 'committed'
    f.pump()
    env = receive(f, 'worker-1')
    settle(f, 'worker-1', env, REPORT)
    settle(f, 'worker-1', env, REPORT)                          # bridge replay
    event(f, dict(stale, kind='settled', delivery=env, failed=False, aborted=False, unfinished=False,
                  candidates=[{'tool': 'fleet_result', 'value': REPORT}]))   # retired Pi generation
    assert f.r.llen(f.v2.bus.pending_key('supervisor')) == 1     # one report follow-up only


def test_invalid_pi_report_gets_corrections_then_blocks(codex_pi):
    """G1: a malformed pi report is corrected like an official CLI's, not a whole-task BLOCKED at once."""
    f = codex_pi
    submit(f)
    answer_turn(f, 'supervisor', decision(['worker-1']))
    f.pump()
    env = receive(f, 'worker-1')
    for attempt in range(1, runtime_v2.TUI_REPORT_RETRIES + 1):
        settle(f, 'worker-1', env, {'done': 'prose'})
        assert f.v2.bus.message(env['message_id'])['outcome'] == 'failed'  # never a fake success
        assert f.task()['status'] == 'running' and not f.paused()
        assert f.agent('worker-1')['report_retries'] == str(attempt)
        assert f.r.llen(f.v2.bus.pending_key('supervisor')) == 0  # no bogus report
        f.pump()  # the correction goes through the normal delivery path
        env = receive(f, 'worker-1')
        assert 'Call fleet_result exactly once' in env['body']['text'] and 'Do not redo the work' in env['body']['text']
    settle(f, 'worker-1', env, {'done': 'still prose'})
    assert f.task()['status'] == 'blocked'


def test_service_request_routed_by_codex_result_card_rendered_off_model(codex_pi_service):
    f = codex_pi_service
    spec = dict(id='r1', payload={'message': 'say OK'}, reply={'required': ['reply'], 'allowed': ['reply']})
    event(f, dict(kind='request', request_id='cmd-r1', spec=spec))
    f.pump()
    body = typed(f, 'supervisor')[-1]
    assert 'Reply with the service decision JSON from AGENTS.md.' in body
    route = json.dumps({'status': 'RUNNING', 'skip': [],
                        'dispatch': [{'to': 'worker-1', 'request_id': 'r1', 'instruction': 'Reply OK in the reply field.'}]})
    assert answer_turn(f, 'supervisor', route)['status'] == 'committed'
    f.pump()
    env = receive(f, 'worker-1')
    sent_before = len(f.mgr.sent) + len(f.mgr.queued)  # neither pasted nor queued
    settle(f, 'worker-1', env, {'reply': 'OK'})
    assert json.loads(f.r.get(fleet.RESULT + 'r1'))['result'] == {'reply': 'OK'}
    f.v2.render_cards()
    log = (f.run_dir / 'cards' / 'supervisor.log').read_text()
    assert 'worker-1：OK' in log
    assert 'worker-1：OK' in (f.run_dir / 'history.html').read_text()   # history page "Cards" view
    assert not (f.run_dir / 'cards' / 'worker-1.log').exists()          # Pi shows its own cards
    assert len(f.mgr.sent) + len(f.mgr.queued) == sent_before   # card never typed or queued into Codex
    assert not f.v2.depth('supervisor')                          # and never queued for the model
