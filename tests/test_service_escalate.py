"""Service mode: non-delete dialogs and a worker's {"blocked": ...} go to the supervisor (plan F', user 2026-10-14:
"apart from deleting files, everything a worker needs a human for goes to the supervisor", service mode too).

Real private Redis, official Codex supervisor + Claude workers in service mode (a verified pair), fake cmux screens.
"""
import json
import sys
import time
import uuid

import pytest

from test_fleet import FakeMgr
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_v2_codex_claude import answer_turn, typed
from cmux_fleet import fleet, runtime_v2, watchdog

WORKERS = ['worker-1', 'worker-2']
READ_FAR = 'Read(/Users/someone/notes/today.md)\n\nRead outside the working directories?\n❯ 1. Yes\n  2. No'
DIALOG_RM = ('Bash command\n  rm -rf "$OUT/"*\n\nDangerous rm on possibly-empty variable\n'
             'Do you want to proceed?\n❯ 1. Yes\n  2. No')
WORKING = '⏺ Reading files…\n\n> '
DKEY, BKEY = 'dialog:worker-1:g0', 'blocked:worker-1'


@pytest.fixture
def svc(private_redis_v2, tmp_path, monkeypatch):  # noqa: F811
    r = private_redis_v2
    r.flushdb()
    monkeypatch.setattr(fleet, 'trust_claude_dir', lambda cwd: None)  # never touch ~/.claude.json
    run = tmp_path / ('run-' + uuid.uuid4().hex)
    (run / 'workspace').mkdir(parents=True)
    claude_bin = tmp_path / 'bin' / 'claude'
    claude_bin.parent.mkdir()
    claude_bin.write_text('#!/bin/sh\nexit 1\n')
    claude_bin.chmod(0o755)
    cfg = dict(run=str(run), redis_socket=r.connection_pool.connection_kwargs['path'], python=sys.executable,
               workers=list(WORKERS), mode='service', supervisor_cli='codex', worker_cli='claude',
               claude=str(claude_bin), codex_home=str(tmp_path / 'codex-home'), transport_version=2,
               allow_development_v2=True, stall_timeout=0, v2_lease_ms=300000, max_busy=0, request_timeout=3600,
               stall_grace=300, stall_escapes=3, watch_interval=0,
               adopted_supervisor_hook_delivery=True)  # every new start pins it; §48 refuses a typed adoption
    path = run / 'config.json'
    fleet.save(path, cfg)
    f = fleet.Fleet(path, r, FakeMgr(), sessions=tmp_path / 'codex-home' / 'sessions')
    f.test_sessions, f.test_claude = tmp_path / 'codex-home' / 'sessions', tmp_path / 'claude-projects'
    f.test_threads = {s: str(uuid.uuid4()) for s in ['supervisor'] + WORKERS}
    f.start_all()
    r.set(f.v2.prefix + ':boot', 'running')
    for slot in f.names:
        assert answer_turn(f, slot, 'READY', startup=True)['status'] == 'ready'
    f.mgr.screens, f.mgr.stats = {}, {}
    f.mgr.capture_output = lambda pane, lines=2000: f.mgr.screens.get(pane, WORKING)
    f.mgr.process_states = lambda pane: f.mgr.stats.get(pane, ['Ss', 'S+'])
    f.mgr.owns = lambda pane: pane in f.mgr.alive
    yield f
    f.v2.close()


def audits(f, event=None):
    rows = [fields for _, fields in f.r.xrange(f.v2.bus.audit_key)]
    return [x for x in rows if event is None or x.get('event') == event]


def keys(f, slot):
    pane = f.agent(slot)['pane']
    return [k for p, k in f.mgr.keys if p == pane]


def escalations(f):
    return f.escalation_state()


def request(f, rid='r1'):
    spec = {'id': rid, 'payload': {'question': 'what is in notes?'}, 'reply': {'required': ['answer']}}
    runtime_v2.post_event(f.v2.bus, dict(kind='request', request_id='cmd-' + rid, spec=spec), 'cmd-' + rid)
    assert f.v2.step(10)
    f.pump()
    route = {'status': 'RUNNING', 'reason': 'worker-1 knows the notes', 'skip': [],
             'dispatch': [{'to': 'worker-1', 'instruction': 'Answer the question from the payload.'}]}
    assert answer_turn(f, 'supervisor', json.dumps(route))['status'] == 'committed'
    f.pump()
    assert f.agent('worker-1')['state'] == 'busy' and f.agent('worker-1')['request'] == rid


def supervisor_says(f, escalations_=(), status='RUNNING', dispatch=()):
    f.pump()
    body = {'status': status, 'reason': 'ruling', 'dispatch': list(dispatch), 'skip': [],
            'escalations': list(escalations_)}
    return answer_turn(f, 'supervisor', json.dumps(body))


def result(f, rid='r1'):
    raw = f.r.get(fleet.RESULT + rid)
    return json.loads(raw) if raw else None


# ------------------------------------------------------------------ parser
def test_service_decision_accepts_only_dialog_and_blocked_escalations():
    ok = {'status': 'RUNNING', 'dispatch': [], 'skip': [],
          'escalations': [{'task': DKEY, 'decision': 'approve'}, {'task': BKEY, 'decision': 'revise', 'note': 'x'}]}
    assert fleet.parse_service_decision(json.dumps(ok), WORKERS)['escalations'][1]['note'] == 'x'
    for bad, why in (({'task': 'split:r1', 'decision': 'approve'}, 'service mode answers only'),  # M<n> is legal after P7 (#6)
                     ({'task': DKEY, 'decision': 'yes'}, 'approve / revise / reject'),
                     ([{'task': DKEY, 'decision': 'approve'}] * 2, 'one answer per task')):
        items = bad if isinstance(bad, list) else [bad]
        with pytest.raises(ValueError, match=why):
            fleet.parse_service_decision(json.dumps(dict(ok, escalations=items)), WORKERS)
    assert fleet.parse_service_decision(json.dumps({'status': 'RUNNING'}), WORKERS)['escalations'] == []


@pytest.mark.parametrize('value,reply,expect', [
    ({'blocked': 'need a login for the wiki'}, {'required': ['answer']}, 'need a login for the wiki'),
    ({'blocked': 'none'}, {'required': ['answer']}, ''),
    ({'blocked': 'x', 'answer': 'y'}, {'required': ['answer']}, ''),            # an ordinary answer
    ({'blocked': 'x'}, {'required': ['blocked']}, ''),                           # the request's own key
    ({'blocked': 'x'}, {'required': ['answer'], 'allowed': ['answer', 'blocked']}, ''),
])
def test_service_blocked_answer_shape(value, reply, expect):
    assert fleet.service_blocked(value, reply) == expect


# ------------------------------------------------------------------ dialog
def test_service_non_delete_dialog_is_ruled_by_the_supervisor_and_the_request_completes(svc):
    f = svc
    request(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = READ_FAR
    f.v2.watchdog.tick(time.time())
    rec = escalations(f)[DKEY]
    assert rec['kind'] == 'dialog' and rec['keys_approve'] == ['Enter'] and not f.paused()
    assert f.v2.bus.message(rec['message_id'])['request_id'] == 'r1' and keys(f, 'worker-1') == []
    assert f.v2.bus.slot_state('worker-1')['status'] == 'received' and f.agent('worker-1')['prompt_route'] == 'supervisor'
    assert supervisor_says(f, [{'task': DKEY, 'decision': 'approve'}])['status'] == 'committed'
    assert typed(f, 'supervisor')[-1].startswith('[dialog] worker-1')
    assert escalations(f)[DKEY]['status'] == 'answered' and keys(f, 'worker-1') == []
    f.v2.watchdog.tick(time.time() + 1)
    assert keys(f, 'worker-1') == ['Enter'] and DKEY not in escalations(f)
    assert audits(f, 'watch.prompt_answered')[-1]['authorized'] == 'supervisor:approve'
    assert answer_turn(f, 'worker-1', json.dumps({'answer': 'the notes say hi'}))['status'] == 'committed'
    assert result(f)['status'] == 'ok' and result(f)['result'] == {'answer': 'the notes say hi'}


def test_service_dialog_timeout_and_unavailable_supervisor_fall_back_to_the_user(svc):
    f = svc
    request(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = READ_FAR
    now = time.time()
    f.v2.watchdog.tick(now)
    f.pump()                                               # the supervisor receives the [dialog] message
    f.v2.watchdog.tick(now + 1)
    f.v2.watchdog.tick(now + 1 + 600)
    assert f.paused() and f.agent('worker-1')['prompt_route'] == 'user' and keys(f, 'worker-1') == []
    assert audits(f, 'dialog.to_human')[-1]['reason'] == 'supervisor_timeout'


def test_service_dialog_goes_to_the_user_while_the_supervisor_is_held(svc):
    f = svc
    request(f)
    f.setf('supervisor', state='held')
    f.mgr.screens[f.agent('worker-1')['pane']] = READ_FAR
    f.v2.watchdog.tick(time.time())
    assert f.paused() and f.agent('worker-1')['prompt_route'] == 'user' and DKEY not in escalations(f)


def test_service_delete_dialog_still_goes_only_to_the_user(svc):
    f = svc
    request(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = DIALOG_RM
    for i in range(5):
        f.v2.watchdog.tick(time.time() + i)
    assert keys(f, 'worker-1') == [] and f.paused() and escalations(f) == {}
    assert f.agent('worker-1')['prompt_route'] == 'user' and f.v2.bus.slot_state('worker-1')['status'] == 'received'


def test_service_reply_that_ignores_the_dialog_is_sent_back_once_then_the_user_gets_it(svc):
    f = svc
    request(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = READ_FAR
    f.v2.watchdog.tick(time.time())
    supervisor_says(f)
    assert f.r.hget(fleet.SERVICE, 'bad_replies') in (None, '0') and escalations(f)[DKEY]['nudged'] == 1
    supervisor_says(f)
    assert f.paused() and f.agent('worker-1')['prompt_route'] == 'user' and DKEY not in escalations(f)
    assert audits(f, 'dialog.to_human')[-1]['reason'] == 'supervisor_unanswered'


# ------------------------------------------------------------------ blocked
def stuck(f):
    request(f)
    out = answer_turn(f, 'worker-1', json.dumps({'blocked': 'the wiki needs a login; propose using the cached copy'}))
    assert out['status'] == 'committed'


def test_service_blocked_answer_keeps_the_request_open_and_asks_the_supervisor(svc):
    f = svc
    stuck(f)
    assert result(f) is None and f.r.hget(fleet.REQ + 'r1', 'status') == 'dispatched'
    rec = escalations(f)[BKEY]
    assert rec['kind'] == 'blocked' and rec['request'] == 'r1' and 'cached copy' in rec['reason']
    assert f.agent('worker-1')['state'] == 'idle' and not f.paused()
    f.pump()
    assert typed(f, 'supervisor')[-1].startswith('[blocked] worker-1 (request r1)')


@pytest.mark.parametrize('decision,note', [('approve', 'the cached copy is fine'), ('revise', 'ask for the PDF export'),
                                           ('reject', 'answer without the wiki')])
def test_service_ruling_returns_to_the_worker_as_the_same_request(svc, decision, note):
    f = svc
    stuck(f)
    assert supervisor_says(f, [{'task': BKEY, 'decision': decision, 'note': note}])['status'] == 'committed'
    assert f.r.hget(fleet.SERVICE, 'bad_replies') in (None, '0') and BKEY not in escalations(f)
    f.pump()
    got = typed(f, 'worker-1')[-1]
    assert '[Supervisor ruling] ' + decision + ': ' + note in got and 'Payload (JSON)' in got
    assert answer_turn(f, 'worker-1', json.dumps({'answer': 'done'}))['status'] == 'committed'
    assert result(f)['status'] == 'ok' and result(f)['worker'] == 'worker-1'


def test_service_revise_without_a_note_is_a_bad_reply(svc):
    f = svc
    stuck(f)
    supervisor_says(f, [{'task': BKEY, 'decision': 'revise'}])
    assert f.r.hget(fleet.SERVICE, 'bad_replies') == '1' and escalations(f)[BKEY]['status'] == 'escalated'


def test_service_unanswered_blocked_is_sent_back_once_then_counts(svc):
    f = svc
    stuck(f)
    supervisor_says(f)
    assert f.r.hget(fleet.SERVICE, 'bad_replies') in (None, '0') and escalations(f)[BKEY]['nudged'] == 1
    supervisor_says(f)
    assert f.r.hget(fleet.SERVICE, 'bad_replies') == '1' and BKEY in escalations(f)


def test_service_blocked_question_is_moot_once_the_request_times_out(svc):
    f = svc
    stuck(f)
    f.v2.transaction('t-pub', lambda: f.v2.publish('r1', 'timeout', worker='worker-1', error='too slow'))
    assert BKEY not in escalations(f) and audits(f, 'blocked.moot')
    assert supervisor_says(f, [{'task': BKEY, 'decision': 'approve'}])['status'] == 'committed'   # stale: logged only
    assert f.r.hget(fleet.SERVICE, 'bad_replies') in (None, '0')


def test_service_blocked_reply_status_still_pauses(svc):
    f = svc
    stuck(f)
    supervisor_says(f, status='BLOCKED')
    assert f.paused()


def test_supervisor_prompts_carry_the_trust_escape_warning():
    from pathlib import Path
    agents = Path(fleet.__file__).resolve().parent / 'agents'
    for name in ('supervisor.md', 'supervisor_v2.md', 'contracts_supervisor.md', 'service_supervisor.md',
                 'service_supervisor_v2.md', 'service_supervisor_tui_v2.md'):
        text = (agents / name).read_text()
        assert 'exits that session' in text and 'dialog:worker-N:g0' in text, name
    for name in ('service_supervisor.md', 'service_supervisor_v2.md', 'service_supervisor_tui_v2.md'):
        assert 'blocked:worker-N' in (agents / name).read_text(), name
    for name in ('service_worker.md', 'service_worker_v2.md'):
        assert '"blocked"' in (agents / name).read_text(), name
