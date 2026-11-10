"""Claude worker deliveries through the Stop hook channel (feat/claude-hook-delivery).

Black box on purpose: the hook is the exact command line the daemon wrote into this
generation's Claude ``--settings`` file, run as a real subprocess with a Stop payload on
stdin (real private Redis, fake cmux). After the launch prompt, a Claude worker must
receive every delivery as ``{"decision": "block", "reason": <business text>}`` on the
hook's stdout, and never through the input box (``send_prompt_to_idle``).

Transcripts follow what Claude Code 2.1.280 was measured to write (worker-2/research.md §1):
a hook delivery is NOT a normal user prompt but an ``isMeta`` user row
``"Stop hook feedback:\\n<reason>\\n"`` followed by a ``hook_blocking_error`` attachment and a
``stop_hook_summary`` system row. A row the operator typed is a plain user row.
Escape / ctrl+x ctrl+s / a hook timeout SIGTERM the waiting hook (research §4); the fake
manager below does the same on Escape.
"""
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import threading
import time
import uuid

import pytest

from test_fleet import FakeMgr, codex_rows, received as reached, write_rollout
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from cmux_fleet import claude_hook, fleet, runtime_v2

HOOK_CHANNEL = True  # tests/conftest.py keeps the channel on for this module

WORKERS = ['worker-1', 'worker-2']
REPORT = 'Done:\n- wrote {name}\nEvidence:\n- worker-1/{name} 8 bytes\nNot done: none\nBlocked: none'
FEEDBACK = 'Stop hook feedback:\n'
CAP = 'CLAUDE_CODE_STOP_HOOK_BLOCK_CAP'
LONG_IDLE = 1800  # brief: >= 30 minutes without work must not break the channel


# ------------------------------------------------------------------ fake cmux with real hook kills
class HookMgr(FakeMgr):
    """Escape in a Claude pane SIGTERMs the Stop hook waiting there (measured, research §4)."""

    def __init__(self):
        super().__init__()
        self.hooks = []

    def send_key(self, pane, k):
        if k == 'Escape':
            for hook in self.hooks:
                if hook.pane == pane and hook.alive():
                    hook.proc.send_signal(signal.SIGTERM)
        return super().send_key(pane, k)

    def has_cli_process(self, pane, clis):
        return pane in self.alive

    def dock_control(self, history_surface):
        return True

    def kill_pane(self, pane):
        # kill_process_tree SIGTERMs Claude's children too, the waiting hook included
        for hook in self.hooks:
            if hook.pane == pane and hook.alive():
                hook.proc.send_signal(signal.SIGTERM)
        return super().kill_pane(pane)


# ------------------------------------------------------------------ hook process
class Hook:
    """One run of the generation's real Stop hook command."""

    def __init__(self, f, slot, generation, payload):
        stop_hooks, = settings(f, slot, generation)['hooks']['Stop']
        self.command = stop_hooks['hooks'][0]['command']
        self.timeout = stop_hooks['hooks'][0].get('timeout')
        self.slot, self.session, self.pane = slot, payload.get('session_id'), f.agent(slot)['pane']
        self.transcript = payload.get('transcript_path')
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
        self.proc = subprocess.Popen(shlex.split(self.command), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, env=env, cwd=str(fleet.agent_cwd(f.cfg, slot)))
        self.proc.stdin.write(json.dumps(payload))
        self.proc.stdin.close()
        self.out = self.err = None

    def alive(self):
        return self.proc.poll() is None

    def finish(self):
        if self.out is None:
            self.out, self.err = self.proc.stdout.read(), self.proc.stderr.read()
        return self.out

    def decision(self):
        """The parsed block decision, or None when the hook let Claude stop."""
        out = (self.finish() or '').strip()
        if not out:
            return None
        data = json.loads(out.splitlines()[-1])
        return data if data.get('decision') == 'block' else None

    def kill(self):
        if self.alive():
            self.proc.kill()
            self.proc.wait(5)
        self.finish()


def settings(f, slot, generation=0):
    """The generation's Claude --settings, written the way the pane launcher writes it."""
    path = fleet.agent_cwd(f.cfg, slot) / ('.fleet-settings-g' + str(generation) + '.json')
    if not path.exists():  # FakeMgr never execs the launcher, so build its argv here
        argv = fleet.claude_argv(f.path, fleet.load(f.path), slot, generation, 'launch prompt')
        path = Path(argv[argv.index('--settings') + 1])
    return json.loads(path.read_text())


def step(f, until, timeout=15.0):
    """Step the daemon until ``until()`` holds; return whether it did."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        f.v2.step(20)
        f.pump()
        if until():
            return True
    return False


def thread_of(f, slot, generation):
    return f.test_threads[slot] if generation == 0 else f.test_threads[slot] + '-g' + str(generation)


def transcript_of(f, slot, thread):
    path = f.test_claude / slot / (thread + '.jsonl')
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def append(path, rows):
    with Path(path).open('a') as out:
        out.write(''.join(json.dumps(x) + '\n' for x in rows))


def feedback_rows(reason):
    """What Claude Code 2.1.280 writes when a Stop hook blocks with ``reason``."""
    return [{'type': 'user', 'isMeta': True, 'promptId': 'p-first',
             'message': {'role': 'user', 'content': FEEDBACK + reason + '\n'}},
            {'type': 'attachment', 'attachment': {'type': 'hook_blocking_error', 'hookEvent': 'Stop',
                                                  'blockingError': {'blockingError': reason + '\n'}}},
            {'type': 'system', 'subtype': 'stop_hook_summary'}]


def payload(f, slot, thread, text, active):
    return {'session_id': thread, 'transcript_path': str(transcript_of(f, slot, thread)),
            'cwd': str(fleet.agent_cwd(f.cfg, slot)), 'hook_event_name': 'Stop', 'stop_hook_active': active,
            'last_assistant_message': text, 'prompt_id': 'p-first', 'background_tasks': []}


def stop(f, slot, text, *, generation=None, thread=None, active=True, user_prompt=None):
    """Fire the slot's Stop hook for a finished turn.

    ``user_prompt`` adds a plain user row (launch prompt, or text the operator typed); a hook
    delivery's isMeta feedback rows are written by ``delivered`` when the hook hands it out."""
    gen = int(f.agent(slot)['generation']) if generation is None else generation
    thread = thread or thread_of(f, slot, gen)
    rows = []
    if user_prompt is not None:
        rows.append({'type': 'user', 'promptId': 'p-' + uuid.uuid4().hex[:8],
                     'message': {'role': 'user', 'content': user_prompt}})
    rows.append({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}})
    append(transcript_of(f, slot, thread), rows)
    hook = Hook(f, slot, gen, payload(f, slot, thread, text, active))
    f.test_hooks.append(hook)
    f.mgr.hooks.append(hook)
    return hook


def codex_turn(f, text):
    """The Codex supervisor still answers through notify (its deliveries come from `codex queue`)."""
    gen = int(f.agent('supervisor').get('generation') or 0)
    # Codex rollout lookup needs a UUID per generation (as in test_resume)
    thread = f.test_threads['supervisor'] if gen == 0 else f.test_threads.setdefault(('supervisor', gen), str(uuid.uuid4()))
    prompt = typed(f, 'supervisor')[-1] if typed(f, 'supervisor') else 'startup prompt'
    turn_id, rows = codex_rows(thread, prompt, text)  # the queued delivery's own turn, or a typed one
    write_rollout(f.test_sessions, thread, fleet.agent_cwd(f.cfg, 'supervisor'), rows=rows)
    data = json.dumps({'type': 'agent-turn-complete', 'thread-id': thread, 'turn-id': turn_id,
                       'input-messages': [prompt], 'last-assistant-message': text})
    box = {}
    worker = threading.Thread(target=lambda: box.setdefault('v', fleet.notify(f.path, 'supervisor', str(gen), data, f.test_sessions)))
    worker.start()
    deadline = time.time() + 15
    while worker.is_alive() and time.time() < deadline:
        f.v2.step(20)
    worker.join(2)
    assert not worker.is_alive()
    return box['v']


def typed(f, slot):
    """What reached the slot's pane: its queued Codex deliveries (conftest), otherwise its real pastes."""
    return reached(f.mgr, f.agent(slot)['pane'])


def panes(f, slot):
    f.test_panes.setdefault(slot, set()).add(f.agent(slot)['pane'])
    return f.test_panes[slot]


def typed_any(f, slot):
    """Everything typed into any pane this slot ever had (generations included)."""
    mine = panes(f, slot)
    return [text for p, text in f.mgr.sent if p in mine]


def escapes(f, slot):
    mine = panes(f, slot)
    return [k for p, k in f.mgr.keys if p in mine and k == 'Escape']


def decision_json(workers, what='part.md'):
    return json.dumps({'status': 'RUNNING', 'completion': 10, 'reason': 'split the work',
                       'dispatch': [{'to': w, 'instruction': 'Write ' + w + '/' + what + ' with the findings.'}
                                    for w in workers]})


def submit(f):
    runtime_v2.post_event(f.v2.bus, dict(kind='task', request_id='new-task', spec={'goal': 'build M0 parts'}), 'new-task')
    assert step(f, lambda: bool(typed(f, 'supervisor'))), 'the task never reached the supervisor'


def received(f, slot):
    return f.v2.bus.slot_state(slot).get('status') == 'received'


def status_of(f, env):
    return f.v2.bus.message(env['message_id']).get('status')


def reports(f, slot='worker-1'):
    """Reports the supervisor got (typed) or still has queued."""
    label = re.compile(r'^\[' + re.escape(slot) + r'(\.\d+)? report')  # '[worker-1.1 report (took over ...)]'
    return len([t for t in typed(f, 'supervisor') if label.match(t)]) \
        + f.r.llen(f.v2.bus.pending_key('supervisor'))


def audit(f, slot):
    return [x for _, x in f.r.xrange(f.v2.bus.audit_key) if x.get('slot') == slot]


@pytest.fixture
def hooked(private_redis_v2, tmp_path):  # noqa: F811
    """Codex supervisor + two Claude workers whose READY turns ended through the real Stop hook."""
    r = private_redis_v2
    r.flushdb()
    run = tmp_path / ('run-' + uuid.uuid4().hex)
    (run / 'workspace').mkdir(parents=True)
    sessions = tmp_path / 'codex-home' / 'sessions'
    cfg = dict(run=str(run), redis_socket=r.connection_pool.connection_kwargs['path'],
               python=sys.executable, workers=list(WORKERS), mode='task', supervisor_cli='codex',
               worker_cli='claude', codex_home=str(tmp_path / 'codex-home'), transport_version=2,
               allow_development_v2=True, stall_timeout=0, v2_lease_ms=300000, max_busy=0)
    path = run / 'config.json'
    fleet.save(path, cfg)
    f = fleet.Fleet(path, r, HookMgr(), sessions=sessions)
    f.test_sessions, f.test_claude = sessions, tmp_path / 'claude-projects'
    f.test_threads = {s: str(uuid.uuid4()) for s in ['supervisor'] + WORKERS}
    f.test_hooks, f.test_panes, f.test_close = [], {}, [f]
    import cmux_fleet.fleet as fleet_mod
    real_trust = fleet_mod.trust_claude_dir
    fleet_mod.trust_claude_dir = lambda cwd: None  # never touch the operator's ~/.claude.json
    try:
        f.start_all()
        r.set(f.v2.prefix + ':boot', 'running')
        for w in WORKERS:
            panes(f, w)
        assert codex_turn(f, 'READY')['status'] == 'ready'
        f.test_ready = {}
        for w in WORKERS:
            # The launch prompt is the only typed input; its READY turn ends through the hook.
            hook = stop(f, w, 'READY', active=False, user_prompt='startup prompt')
            assert step(f, lambda: f.agent(w).get('runtime_ready') == '1'), 'READY was not bound through the hook'
            f.test_ready[w] = hook
        yield f
    finally:
        for hook in f.test_hooks:
            hook.kill()
        for proc in getattr(f, 'test_procs', []):
            if proc.poll() is None:
                proc.kill()
        fleet_mod.trust_claude_dir = real_trust
        for each in f.test_close:
            each.v2.close()


def dispatch(f, workers, what='part.md'):
    if not typed(f, 'supervisor') or 'build M0' not in typed(f, 'supervisor')[0]:
        submit(f)
    assert codex_turn(f, decision_json(workers, what))['status'] == 'committed'
    f.pump()


def delivered(f, hook, timeout=15.0):
    """Step until the blocking hook hands over a delivery; return its block decision.

    Like Claude Code, record the hand-over in the session transcript as isMeta feedback rows."""
    assert step(f, lambda: not hook.alive(), timeout), 'the hook never handed over the delivery'
    got = hook.decision()
    assert got is not None, 'hook exited without a block decision: ' + repr((hook.out, hook.err))
    append(hook.transcript, feedback_rows(got['reason']))
    return got


def handed(f, slot='worker-1', what='part.md'):
    """Dispatch one task to ``slot`` and let its waiting READY hook hand it over."""
    dispatch(f, [slot], what)
    got = delivered(f, f.test_ready[slot])
    return got, f.v2.bus.slot_state(slot)


# ------------------------------------------------------------------ settings / launch env measured on 2.1.280
def launch_env(f, slot, monkeypatch, tmp_path):
    """The environment the pane launcher execs Claude Code with (exec intercepted)."""
    monkeypatch.setenv(CAP, 'unset-by-test')  # restored (removed) at teardown
    monkeypatch.chdir(tmp_path)  # the launcher chdirs into the slot directory

    class Exec(Exception):
        pass

    def fake_exec(prog, argv):
        raise Exec(dict(os.environ))
    monkeypatch.setattr(fleet.os, 'execvp', fake_exec)
    with pytest.raises(Exec) as caught:
        fleet.actor(f.path, slot, 'launch prompt')
    return caught.value.args[0]


def test_default_block_cap_is_off_and_hook_outlasts_long_idle(hooked, monkeypatch, tmp_path):
    f = hooked
    # Default cap 8: the 9th consecutive block is written to the transcript and never answered.
    s = settings(f, 'worker-1')
    in_settings = str((s.get('env') or {}).get(CAP))
    in_launch = launch_env(f, 'worker-1', monkeypatch, tmp_path).get(CAP)
    assert '0' in (in_settings, in_launch), CAP + ' must be 0 (settings env or launch env)'
    for gen in (0, 1):
        timeout = settings(f, 'worker-1', gen)['hooks']['Stop'][0]['hooks'][0].get('timeout')
        # No timeout means 600 s: ten idle minutes would cut the channel.
        assert timeout and timeout >= LONG_IDLE, 'Stop hook timeout must outlast a 30-minute idle wait'


def test_codex_supervisor_keeps_the_typed_path_and_no_cap_change(hooked, monkeypatch, tmp_path):
    f = hooked
    assert typed(f, 'supervisor') == [] or all('Stop hook' not in t for t in typed(f, 'supervisor'))
    dispatch(f, ['worker-1'])
    assert typed(f, 'supervisor'), 'the Codex supervisor keeps its own (codex queue) path, not the hook channel'


# ------------------------------------------------------------------ input box is never used
def test_ready_hook_waits_then_delivers_task_without_typing(hooked):
    f = hooked
    hook = f.test_ready['worker-1']
    assert hook.alive(), 'after READY the hook must block for the next delivery, not let Claude stop'
    got, lease = handed(f)
    assert 'Write worker-1/part.md' in got['reason']
    assert lease['status'] == 'received' and lease['session_id'] == f.test_threads['worker-1']
    for transport_id in (lease['message_id'], lease['current_delivery'], '.inbox', 'fleet_result'):
        assert transport_id not in got['reason']
    assert typed_any(f, 'worker-1') == [], 'a Claude worker delivery reached the input box'
    assert not [k for p, k in f.mgr.keys if p in panes(f, 'worker-1') and k == 'Enter']


def test_idle_worker_hook_hands_out_nothing(hooked):
    f = hooked
    handed(f)
    step(f, lambda: False, timeout=2)
    assert f.test_ready['worker-2'].alive()
    assert typed_any(f, 'worker-2') == []


# ------------------------------------------------------------------ settle first, then wait
def test_stop_settles_from_meta_feedback_before_blocking_again(hooked):
    f = hooked
    got, env = handed(f)
    rows = [json.loads(x) for x in Path(f.test_ready['worker-1'].transcript).read_text().splitlines()]
    assert not [x for x in rows if x['type'] == 'user' and not x.get('isMeta')
                and 'Write worker-1/part.md' in str(x['message']['content'])], 'no plain user row for a hook delivery'
    after = stop(f, 'worker-1', REPORT.format(name='part.md'))
    # The report is committed while this same hook keeps waiting for more work.
    assert step(f, lambda: status_of(f, env) == 'committed')
    assert after.alive(), 'with nothing queued the hook keeps waiting instead of letting Claude stop'
    msg = f.v2.bus.message(env['message_id'])
    assert msg['outcome'] == 'completed' and msg['result']['done'] == ['wrote part.md']
    assert f.agent('worker-1')['state'] == 'idle'
    assert step(f, lambda: any(t.startswith('[worker-1 report]') for t in typed(f, 'supervisor')))


def test_unrelated_meta_rows_do_not_hide_the_delivery(hooked):
    f = hooked
    got, env = handed(f)
    # Claude also writes other isMeta user rows (reminders, attachments) inside a turn.
    append(f.test_ready['worker-1'].transcript, [
        {'type': 'user', 'isMeta': True, 'message': {'role': 'user', 'content': '<system-reminder>todo list empty</system-reminder>'}}])
    stop(f, 'worker-1', REPORT.format(name='part.md'))
    assert step(f, lambda: status_of(f, env) == 'committed')


def test_ten_consecutive_deliveries_with_stop_hook_active(hooked):
    f = hooked
    hook, seen = f.test_ready['worker-1'], []
    for i in range(10):  # past Claude Code's default cap of 8 consecutive blocks
        dispatch(f, ['worker-1'], what='part-' + str(i) + '.md')
        got = delivered(f, hook)
        seen.append(got['reason'])
        env = f.v2.bus.slot_state('worker-1')
        hook = stop(f, 'worker-1', REPORT.format(name='part-' + str(i) + '.md'), active=True)
        assert step(f, lambda: status_of(f, env) == 'committed'), \
            'delivery ' + str(i) + ' did not settle with stop_hook_active=true'
    assert all('part-' + str(i) + '.md' in seen[i] for i in range(10))
    assert typed_any(f, 'worker-1') == []
    assert step(f, lambda: reports(f) == 10)


# 2026-10-28 run fleet-20261028-112659 (ci-1, Opus 5.5): the follow-up reports wrote "Evidence (<qualifier>):".
QUALIFIED = ('Done:\n- merged {name}, merge commit 15a20454\nEvidence (`FLEETMUX_GATE=m3`, temporary helper copy in place):\n'
             '- collect-only on the 3 test files: 33 tests collected, 0 errors\nNot done: none.\n\nBlocked: none.')


def follow_ups(f, *names):
    """Settle the first task, then queue follow-ups the way the live supervisor did: the first while the
    worker is idle (its hook hands it over), the rest while that one runs. Returns the running lease."""
    env = f.v2.bus.slot_state('worker-1')
    hook = stop(f, 'worker-1', REPORT.format(name='part.md'))
    assert step(f, lambda: status_of(f, env) == 'committed')
    f.queue('worker-1', '[M7] follow-up ' + names[0])
    assert 'follow-up ' + names[0] in delivered(f, hook)['reason']
    for name in names[1:]:
        f.queue('worker-1', '[M7] follow-up ' + name)
    return f.v2.bus.slot_state('worker-1')


def test_queued_follow_ups_through_the_hook_each_settle_with_their_own_report(hooked):
    f = hooked
    handed(f)
    env = follow_ups(f, 'A', 'B', 'C')
    for name, nxt in (('A', 'B'), ('B', 'C'), ('C', None)):
        hook = stop(f, 'worker-1', QUALIFIED.format(name=name))
        assert step(f, lambda: status_of(f, env) == 'committed'), 'follow-up ' + name + ' never settled'
        msg = f.v2.bus.message(env['message_id'])
        assert msg['outcome'] == 'completed', 'follow-up ' + name + ': ' + repr(msg.get('outcome'))
        assert msg['result']['done'] == ['merged ' + name + ', merge commit 15a20454']
        assert msg['result']['evidence'] == ['collect-only on the 3 test files: 33 tests collected, 0 errors']
        if nxt:
            assert 'follow-up ' + nxt in delivered(f, hook)['reason']
            env = f.v2.bus.slot_state('worker-1')
    assert not [e for e in audit(f, 'worker-1') if e.get('event') == 'candidate.invalid']
    assert f.task()['status'] == 'running' and f.agent('worker-1').get('report_retries', '0') == '0'
    assert step(f, lambda: reports(f) == 4)


def test_correction_goes_before_follow_ups_already_queued(hooked):
    f = hooked
    handed(f)
    env = follow_ups(f, 'A', 'B', 'C')
    hook = stop(f, 'worker-1', 'I finished A, it is all in the report file.')
    assert step(f, lambda: status_of(f, env) == 'committed')
    assert f.v2.bus.message(env['message_id'])['outcome'] == 'failed'
    # "Your last report" must reach the worker next, while that report is still its last one.
    got = delivered(f, hook)['reason']
    assert 'not in the AGENTS.md reply format' in got and 'follow-up' not in got
    env = f.v2.bus.slot_state('worker-1')
    hook = stop(f, 'worker-1', QUALIFIED.format(name='A'))
    assert step(f, lambda: status_of(f, env) == 'committed')
    assert f.v2.bus.message(env['message_id'])['outcome'] == 'completed'
    assert f.agent('worker-1')['report_retries'] == '0' and f.task()['status'] == 'running'
    assert 'follow-up B' in delivered(f, hook)['reason']


# ------------------------------------------------------------------ foreign / manual / old turns
def test_old_generation_hook_never_settles_or_takes_a_delivery(hooked):
    f = hooked
    got, env = handed(f)
    late = stop(f, 'worker-1', REPORT.format(name='part.md'), generation=5)
    assert step(f, lambda: not late.alive(), timeout=15), 'a retired-generation hook must exit, not block'
    assert late.decision() is None
    assert received(f, 'worker-1') and status_of(f, env) != 'committed'


def test_other_session_turn_does_not_settle_the_delivery(hooked):
    f = hooked
    got, env = handed(f)
    other = stop(f, 'worker-1', REPORT.format(name='part.md'), thread=str(uuid.uuid4()))
    step(f, lambda: not other.alive(), timeout=5)
    assert status_of(f, env) != 'committed' and received(f, 'worker-1')
    if not other.alive():
        assert other.decision() is None or 'Write worker-1/part.md' not in other.decision()['reason']


def test_manual_turn_without_a_handed_delivery_settles_nothing(hooked):
    f = hooked
    # The operator typed in the idle worker-2 pane: no delivery was handed to that session.
    manual = stop(f, 'worker-2', REPORT.format(name='nothing.md'), user_prompt='just chatting')
    step(f, lambda: False, timeout=2)
    assert any(e.get('event') in ('turn.ignored', 'turn.unsolicited') for e in audit(f, 'worker-2'))
    assert f.r.llen(f.v2.bus.pending_key('supervisor')) == 0
    assert manual.alive() or manual.decision() is None


def test_manual_turn_cannot_borrow_an_old_meta_row_to_settle(hooked):
    f = hooked
    # D1 is handed and settled: its feedback row stays in the transcript history.
    got, env1 = handed(f)
    waiting = stop(f, 'worker-1', REPORT.format(name='part.md'))
    assert step(f, lambda: status_of(f, env1) == 'committed')
    # D2 is handed through the hook and its feedback row is written ...
    dispatch(f, ['worker-1'], what='second.md')
    delivered(f, waiting)
    env2 = f.v2.bus.slot_state('worker-1')
    # ... then the operator presses ctrl+x ctrl+s: the turn is interrupted and their own text runs.
    manual = stop(f, 'worker-1', REPORT.format(name='second.md'), active=False, user_prompt='HUMAN: what are you doing?')
    step(f, lambda: False, timeout=3)
    assert status_of(f, env2) != 'committed', 'a manual turn settled D2 through the earlier isMeta row'
    assert step(f, lambda: reports(f) == 1) and reports(f) == 1, 'only D1 may have been reported'
    assert typed_any(f, 'worker-1') == []
    assert manual.alive() or manual.decision() is None or 'second.md' in manual.decision()['reason']


def test_manual_turn_after_a_settled_delivery_reports_nothing_more(hooked):
    f = hooked
    got, env = handed(f)
    stop(f, 'worker-1', REPORT.format(name='part.md'))
    assert step(f, lambda: status_of(f, env) == 'committed')
    # Only history meta rows remain; the operator then chats with the idle worker.
    stop(f, 'worker-1', REPORT.format(name='part.md'), active=False, user_prompt='are you there?')
    step(f, lambda: False, timeout=3)
    assert step(f, lambda: reports(f) == 1) and reports(f) == 1


def test_repeated_stop_after_settlement_reports_once(hooked):
    f = hooked
    got, env = handed(f)
    stop(f, 'worker-1', REPORT.format(name='part.md'))
    assert step(f, lambda: status_of(f, env) == 'committed')
    again = stop(f, 'worker-1', REPORT.format(name='part.md'))
    step(f, lambda: False, timeout=2)
    assert reports(f) == 1, 'the same Stop fired twice produced two reports'
    assert again.alive() or again.decision() is None


# ------------------------------------------------------------------ fail closed: no input-box fallback
def test_hook_without_redis_never_fabricates_a_delivery(hooked, tmp_path):
    f = hooked
    broken = dict(fleet.load(f.path), redis_socket=str(tmp_path / 'missing.sock'))
    path = tmp_path / 'broken-config.json'
    fleet.save(path, broken)
    command = settings(f, 'worker-1')['hooks']['Stop'][0]['hooks'][0]['command']
    argv = [str(path) if a == str(f.path) else a for a in shlex.split(command)]
    assert str(path) in argv, 'the hook command must name the run config'
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        proc.stdin.write(json.dumps(payload(f, 'worker-1', f.test_threads['worker-1'], 'x', True)))
        proc.stdin.close()
        time.sleep(8)
    finally:
        if proc.poll() is None:
            proc.kill()
        out = proc.stdout.read().strip()
    assert not out or json.loads(out.splitlines()[-1]).get('decision') != 'block'
    assert typed_any(f, 'worker-1') == []


@pytest.mark.parametrize('how', ['sigterm', 'sigkill'])
def test_lost_hook_never_falls_back_to_typing(hooked, how):
    f = hooked
    # Hook timeout / Esc / ctrl+x ctrl+s SIGTERM it; a crash takes it without a word.
    hook = f.test_ready['worker-1']
    hook.proc.send_signal(signal.SIGTERM if how == 'sigterm' else signal.SIGKILL)
    hook.proc.wait(10)
    dispatch(f, ['worker-1'])
    step(f, lambda: False, timeout=3)
    assert typed_any(f, 'worker-1') == [], 'a lost hook must not fall back to the input box'
    assert not any(e.get('event') == 'tui.typed' for e in audit(f, 'worker-1'))
    env = f.v2.bus.slot_state('worker-1')
    assert env.get('message_id') and status_of(f, env) != 'committed'
    # The delivery is not lost: the next turn end of this session (any typed line) picks it up.
    revived = stop(f, 'worker-1', 'REVIVE', active=False, user_prompt='reply exactly REVIVE')
    got = delivered(f, revived)
    assert 'Write worker-1/part.md' in got['reason']
    assert typed_any(f, 'worker-1') == []


def test_escape_after_hand_over_does_not_lose_the_delivery(hooked):
    f = hooked
    got, env = handed(f)
    waiting = stop(f, 'worker-1', 'partial work', active=True)  # Claude answered part of it; the hook waits
    f.mgr.send_key(f.agent('worker-1')['pane'], 'Escape')
    step(f, lambda: not waiting.alive(), timeout=5)
    step(f, lambda: False, timeout=2)
    assert status_of(f, env) != 'committed'
    assert received(f, 'worker-1') or f.agent('worker-1')['state'] == 'held' or f.v2.depth('worker-1') >= 1
    assert typed_any(f, 'worker-1') == []


def test_orphaned_hook_does_not_steal_the_next_delivery(hooked):
    f = hooked
    # Claude SIGKILLed: its hook keeps waiting with ppid 1 (research §5). Model that with a parent
    # shell that exits one second after starting the hook.
    f.test_ready['worker-1'].kill()
    command = settings(f, 'worker-1')['hooks']['Stop'][0]['hooks'][0]['command']
    cwd = Path(fleet.agent_cwd(f.cfg, 'worker-1'))
    stdin, out = cwd / '.orphan-stdin.json', cwd / '.orphan-stdout.txt'
    stdin.write_text(json.dumps(payload(f, 'worker-1', f.test_threads['worker-1'], 'x', True)))
    shell = subprocess.Popen(['sh', '-c', command + ' < ' + shlex.quote(str(stdin)) + ' > ' + shlex.quote(str(out))
                              + ' 2>/dev/null & echo $!; sleep 1'], stdout=subprocess.PIPE, text=True)
    pid = int(shell.stdout.readline())
    shell.wait(10)
    time.sleep(1)
    try:
        dispatch(f, ['worker-1'])
        step(f, lambda: False, timeout=8)
        assert 'decision' not in (out.read_text() if out.exists() else ''), \
            'an orphaned hook (its Claude is gone) took the delivery'
        live = stop(f, 'worker-1', 'REVIVE', active=False, user_prompt='reply exactly REVIVE')
        assert 'Write worker-1/part.md' in delivered(f, live)['reason']
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_keepalive_turn_rearms_and_is_never_reported(hooked):
    f = hooked
    # Short hook timeout: the hook answers claude_hook.margin() seconds before Claude would kill it.
    fleet.save(f.path, dict(fleet.load(f.path), claude_hook_timeout=20))
    f.test_ready['worker-1'].kill()
    hook = stop(f, 'worker-1', 'REVIVE', active=False, user_prompt='reply exactly REVIVE')
    assert step(f, lambda: not hook.alive(), timeout=30), 'no keepalive before the hook timeout'
    got = hook.decision()
    assert got and 'IDLE' in got['reason'] and 'Write' not in got['reason']
    append(hook.transcript, feedback_rows(got['reason']))
    rearmed = stop(f, 'worker-1', 'IDLE')
    step(f, lambda: False, timeout=3)
    assert rearmed.alive(), 'after the keepalive turn the hook waits again'
    assert reports(f) == 0
    assert not any(e.get('event') == 'turn.unsolicited' for e in audit(f, 'worker-1'))
    dispatch(f, ['worker-1'])
    assert 'Write worker-1/part.md' in delivered(f, rearmed)['reason']


# ------------------------------------------------------------------ watchdog
def test_watchdog_leaves_an_idle_waiting_hook_alone(hooked):
    f = hooked
    f.cfg.update(stall_timeout=60, stall_grace=30, stall_escapes=3, watch_interval=0)
    for later in (0, 3600, 7200):  # hours of idle wait inside the hook
        f.v2.watchdog.tick(time.time() + later)
    assert escapes(f, 'worker-1') == [] and escapes(f, 'worker-2') == []
    assert f.test_ready['worker-1'].alive() and f.test_ready['worker-2'].alive()
    assert f.agent('worker-1')['state'] == 'idle'


def test_watchdog_still_acts_on_a_stalled_hook_delivery_without_typing(hooked):
    f = hooked
    got, env = handed(f)
    f.cfg.update(stall_timeout=60, stall_grace=10 ** 6, stall_escapes=3, watch_interval=0)
    f.v2.watchdog.tick(time.time() + 3600)
    state = f.agent('worker-1')
    assert escapes(f, 'worker-1') or state.get('watch_state') in ('stalled', 'frozen') or state['state'] == 'held', \
        'an in-flight hook delivery with no activity is still a stall'
    assert typed_any(f, 'worker-1') == []


# ------------------------------------------------------------------ generation change and resume
def rotate(f, slot, gen, what):
    """One context generation change with business work queued behind the handoff.

    Returns the business delivery's lease once the new generation's hook handed it over."""
    waiting = f.test_hooks[-1] if f.test_hooks[-1].slot == slot and f.test_hooks[-1].alive() else None
    f.setf(slot, ctx_used=990000, ctx_window=1000000, delivered=1)
    dispatch(f, [slot], what=what)  # queued behind the handoff
    got = delivered(f, waiting)
    assert f.agent(slot)['state'] == 'handoff'
    assert what not in got['reason'], 'the handoff note request comes before queued work'
    retiring = stop(f, slot, 'handoff written')  # the old generation's hook after the handoff turn
    assert step(f, lambda: f.agent(slot).get('generation') == str(gen + 1)), 'no new generation after the handoff turn'
    # The retired generation's hook lets go and never takes the new generation's work.
    assert step(f, lambda: not retiring.alive(), timeout=20), 'the old generation\'s hook kept waiting'
    assert retiring.decision() is None, 'the old generation\'s hook took a delivery'
    panes(f, slot)
    ready = stop(f, slot, 'READY', active=False, user_prompt='startup prompt g' + str(gen + 1))
    assert step(f, lambda: f.agent(slot).get('runtime_ready') == '1'), 'the new generation never became READY'
    got = delivered(f, ready)
    assert what in got['reason'], 'the queued business did not reach the new generation\'s hook'
    lease = f.v2.bus.slot_state(slot)
    assert lease['generation'] == str(gen + 1) and lease['session_id'] == thread_of(f, slot, gen + 1)
    hooked_audit = [e for e in audit(f, slot) if e.get('event') == 'tui.hooked' and e.get('message_id') == lease['message_id']]
    # e2e run1 (5fb0c91a) could not prove this: tui.hooked carried no generation.
    assert hooked_audit and hooked_audit[-1].get('generation') == str(gen + 1)
    return lease


def test_handoff_goes_through_the_hook_and_next_generation_resumes_there(hooked):
    f = hooked
    got, env = handed(f)
    stop(f, 'worker-1', REPORT.format(name='part.md'))
    assert step(f, lambda: status_of(f, env) == 'committed')
    f.usage = lambda name, _real=f.usage: (990000, 1000000) if name == 'worker-1' else _real(name)
    lease = rotate(f, 'worker-1', 0, 'after-handoff.md')
    # A late Stop of the retired generation cannot settle the new generation's delivery.
    late = stop(f, 'worker-1', REPORT.format(name='after-handoff.md'), generation=0)
    assert step(f, lambda: not late.alive(), timeout=15)
    assert late.decision() is None and received(f, 'worker-1')
    # The new generation's own Stop settles it and the supervisor gets the report.
    before = reports(f)
    stop(f, 'worker-1', REPORT.format(name='after-handoff.md'))
    assert step(f, lambda: status_of(f, lease) == 'committed'), 'the new generation could not settle its delivery'
    assert f.v2.bus.message(lease['message_id'])['outcome'] == 'completed'
    assert step(f, lambda: reports(f) == before + 1)
    assert typed_any(f, 'worker-1') == [], 'handoff / next-generation deliveries reached the input box'
    assert not any(e.get('event') == 'tui.typed' for e in audit(f, 'worker-1'))


def test_two_generation_changes_keep_delivering_through_the_newest_hook(hooked):
    f = hooked
    f.usage = lambda name, _real=f.usage: (990000, 1000000) if name == 'worker-1' else _real(name)
    got, env = handed(f)
    stop(f, 'worker-1', REPORT.format(name='part.md'))
    assert step(f, lambda: status_of(f, env) == 'committed')
    for gen in (0, 1):  # g0 -> g1 -> g2, like the real-cmux e2e (worker-1 reached g3)
        what = 'rot-' + str(gen + 1) + '.md'
        lease = rotate(f, 'worker-1', gen, what)
        stop(f, 'worker-1', REPORT.format(name=what))
        assert step(f, lambda: status_of(f, lease) == 'committed'), 'generation ' + str(gen + 1) + ' did not settle'
    assert f.agent('worker-1')['generation'] == '2'
    retired = [h for h in f.test_hooks if h.slot == 'worker-1' and h.session != thread_of(f, 'worker-1', 2)]
    assert step(f, lambda: not any(h.alive() for h in retired), timeout=20), 'a retired generation still has a hook'
    assert typed_any(f, 'worker-1') == []
    assert not any(e.get('event') == 'tui.typed' for e in audit(f, 'worker-1'))


def test_daemon_restart_with_resume_never_types_into_a_hook_worker(hooked):
    f = hooked
    got, env = handed(f)  # worker-1 is mid-delivery when the daemon dies
    f.v2.close()
    again = fleet.Fleet(f.path, f.r, f.mgr, sessions=f.test_sessions)
    for name in ('test_sessions', 'test_claude', 'test_threads', 'test_hooks', 'test_panes', 'test_ready', 'test_close'):
        setattr(again, name, getattr(f, name))
    f.test_close.append(again)
    again.resume_requested = True
    again.v2.resume_run()
    step(again, lambda: False, timeout=3)
    assert typed_any(again, 'worker-1') == [], 'resume typed the lost delivery into a hook-channel pane'
    lost = again.v2.bus.message(env['message_id'])
    assert lost.get('outcome') != 'completed', 'a delivery the daemon lost was settled as done'
    gen = int(again.agent('worker-1')['generation'])
    if gen and again.agent('worker-1')['state'] == 'starting':
        # Resume replaced the in-flight generation (its Escape would have killed the hook): READY it.
        panes(again, 'worker-1')
        hook = stop(again, 'worker-1', 'READY', active=False, user_prompt='startup prompt g' + str(gen))
        assert step(again, lambda: again.agent('worker-1').get('runtime_ready') == '1')
    else:
        hook = stop(again, 'worker-1', 'REVIVE', active=False, user_prompt='reply exactly REVIVE')
    if again.agent('worker-1')['state'] != 'held':
        # The lost dispatch is redone through the hook channel, never typed.
        assert 'Write worker-1/part.md' in delivered(again, hook)['reason']
    assert typed_any(again, 'worker-1') == []


# ------------------------------------------------------------------ worker-2 round-3 measurements
LONG_BODY = '\n'.join(['## 第 %d 节：把下面的结果写进 worker-1/long.md，逐字保留' % i + '\n'
                       + '这是很长的中文业务正文，含特殊字符 "引号" \\ 反斜杠 $HOME `反引号` <tag> & 和代码块：\n'
                       + '```python\nprint("第 %d 行", {"k": [1, 2, 3]})\n```' % i for i in range(160)])


def test_declaration_rides_the_launch_args_and_a_30kb_chinese_body_arrives_verbatim(hooked):
    f = hooked
    argv = fleet.claude_argv(f.path, fleet.load(f.path), 'worker-1', 0, 'launch prompt')
    assert '--append-system-prompt' in argv, 'without the channel declaration models refuse long deliveries'
    declaration = argv[argv.index('--append-system-prompt') + 1]
    assert 'Stop hook feedback:' in declaration
    assert argv[-2:] == ['--', 'launch prompt'], 'the launch prompt stays the last CLI argument'
    assert len(LONG_BODY.encode()) > 30000
    submit(f)
    plan = json.dumps({'status': 'RUNNING', 'completion': 10, 'reason': 'long',
                       'dispatch': [{'to': 'worker-1', 'instruction': LONG_BODY}]})
    assert codex_turn(f, plan)['status'] == 'committed'
    f.pump()
    got = delivered(f, f.test_ready['worker-1'])
    assert LONG_BODY in got['reason'], 'the long body was altered on its way through the hook'
    assert typed_any(f, 'worker-1') == []


def test_claude_supervisor_uses_the_hook_channel(hooked):
    f = hooked
    cfg = dict(fleet.load(f.path), supervisor_cli='claude', claude_supervisor_hook_delivery=True)
    argv = fleet.claude_argv(f.path, cfg, 'supervisor', 0, 'launch prompt')
    s = json.loads(Path(argv[argv.index('--settings') + 1]).read_text())
    assert s['env'][CAP] == '0' and 'StopFailure' in s['hooks']
    assert len(s['hooks']['Stop'][0]['hooks']) == 2 and s['hooks']['Stop'][0]['hooks'][1]['asyncRewake']
    assert argv[argv.index('--append-system-prompt') + 1] == claude_hook.SUPERVISOR_DECLARATION


def test_api_error_turn_reports_through_stop_failure_and_is_not_adopted(hooked):
    f = hooked
    got, env = handed(f)
    s = settings(f, 'worker-1')
    failure, = s['hooks']['StopFailure']
    command = failure['hooks'][0]['command']
    data = payload(f, 'worker-1', f.test_threads['worker-1'], 'API Error: 429 rate limited', False)
    data.update(hook_event_name='StopFailure', error='rate_limit')
    proc = subprocess.run(shlex.split(command), input=json.dumps(data), capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0 and proc.stdout.strip() == '', 'StopFailure output is ignored by Claude Code'
    step(f, lambda: False, timeout=2)
    events = [e for e in audit(f, 'worker-1') if e.get('event') == 'hook.stop_failure']
    assert events and events[-1].get('error') == 'rate_limit'
    # task mode (§24): the turn ended, so the delivery is settled as not adopted (retried: test_stop_failure_retry)
    assert status_of(f, env) == 'committed' and f.v2.bus.message(env['message_id'])['outcome'] == 'failed'
    assert reports(f) == 0 and not f.paused()
    assert typed_any(f, 'worker-1') == []


def rewake(f, slot, text='partial', generation=None):
    """The background asyncRewake entry of the same Stop event (settings Stop hooks[1])."""
    gen = int(f.agent(slot)['generation']) if generation is None else generation
    entry = settings(f, slot, gen)['hooks']['Stop'][0]['hooks'][1]
    assert entry.get('asyncRewake') is True
    proc = subprocess.Popen(shlex.split(entry['command']), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
    proc.stdin.write(json.dumps(payload(f, slot, thread_of(f, slot, gen), text, True)))
    proc.stdin.close()
    f.test_procs = getattr(f, 'test_procs', []) + [proc]
    return entry, proc


def rewake_rows(text, prefix='Fleet delivery: '):
    """Transcript row of an asyncRewake wake-up (a task-notification, not isMeta). ``prefix`` is the
    configured rewakeMessage, or Claude Code's default when that internal setting is not honoured."""
    return [{'type': 'user', 'origin': {'kind': 'task-notification'}, 'promptId': 'p-' + uuid.uuid4().hex[:8],
             'message': {'role': 'user', 'content': '<task-notification>\n<summary>Stop hook feedback</summary>\n'
                                                     '</task-notification>\n<system-reminder>\n'
                                                     + prefix + text + '\n</system-reminder>'}}]


@pytest.mark.parametrize('prefix', ['Fleet delivery: ', 'Stop hook blocking error from command "fleet.py claude-rewake": '])
def test_rewake_fallback_hands_over_without_typing_when_no_hook_waits(hooked, prefix):
    f = hooked
    hook = f.test_ready['worker-1']
    entry, proc = rewake(f, 'worker-1')
    assert entry.get('rewakeMessage')
    f.mgr.send_key(hook.pane, 'Escape')  # the waiting Stop hook is SIGTERMed: channel unarmed
    hook.proc.wait(10)
    try:
        dispatch(f, ['worker-1'])
        assert step(f, lambda: proc.poll() is not None, timeout=40), 'rewake never took over'
        err = proc.stderr.read()
        assert proc.returncode == 2 and 'Write worker-1/part.md' in err
        assert typed_any(f, 'worker-1') == []
        assert any(e.get('event') == 'hook.rewake' for e in audit(f, 'worker-1'))
        env = f.v2.bus.slot_state('worker-1')
        append(transcript_of(f, 'worker-1', f.test_threads['worker-1']), rewake_rows(err, prefix))
        stop(f, 'worker-1', REPORT.format(name='part.md'), active=False)
        assert step(f, lambda: status_of(f, env) == 'committed'), 'a rewake-delivered turn did not settle'
    finally:
        if proc.poll() is None:
            proc.kill()


def test_rewake_stays_quiet_while_a_stop_hook_waits(hooked):
    f = hooked
    entry, proc = rewake(f, 'worker-1')
    try:
        dispatch(f, ['worker-1'])
        got = delivered(f, f.test_ready['worker-1'])
        assert 'Write worker-1/part.md' in got['reason']
        step(f, lambda: False, timeout=12)  # longer than claude_hook.GRACE
        assert proc.poll() != 2, 'the rewake fallback took a delivery although the Stop hook was waiting'
    finally:
        if proc.poll() is None:
            proc.kill()


def test_task_notification_rows_do_not_start_a_new_turn(hooked):
    f = hooked
    got, env = handed(f)
    # The delivery ran a background task; its completion wakes Claude with a task-notification row.
    append(f.test_ready['worker-1'].transcript, [
        {'type': 'user', 'origin': {'kind': 'task-notification'},
         'message': {'role': 'user', 'content': '<task-notification>shell bg-1 completed</task-notification>'}}])
    stop(f, 'worker-1', REPORT.format(name='part.md'), active=False)
    assert step(f, lambda: status_of(f, env) == 'committed'), 'the background task continuation lost the delivery'


def test_stop_with_background_tasks_returns_at_once_without_settling(hooked):
    f = hooked
    got, env = handed(f)
    data = payload(f, 'worker-1', f.test_threads['worker-1'], REPORT.format(name='part.md'), True)
    data['background_tasks'] = [{'id': 'bg-1', 'type': 'shell', 'status': 'running'}]
    command = settings(f, 'worker-1')['hooks']['Stop'][0]['hooks'][0]['command']
    started = time.time()
    proc = subprocess.run(shlex.split(command), input=json.dumps(data), capture_output=True, text=True, timeout=60)
    assert time.time() - started < 20 and proc.stdout.strip() == ''
    assert status_of(f, env) != 'committed' and received(f, 'worker-1')


# ------------------------------------------------------------------ watchdog frozen -> next generation
def test_frozen_hook_worker_is_replaced_without_typing(hooked):
    f = hooked
    got, env = handed(f)
    f.cfg.update(stall_timeout=60, stall_grace=60, stall_escapes=1, watch_interval=0)
    now = time.time()
    for i in range(1, 8):
        f.v2.watchdog.tick(now + 3600 * i)
        f.v2.step(20)
        if f.agent('worker-1').get('generation') != '0':
            break
    assert f.agent('worker-1').get('generation') == '1', 'frozen worker was not replaced'
    assert step(f, lambda: not f.test_ready['worker-1'].alive(), timeout=10), 'the frozen generation kept its hook'
    panes(f, 'worker-1')
    assert typed_any(f, 'worker-1') == [], 'frozen recovery typed into a hook-channel pane'
    ready = stop(f, 'worker-1', 'READY', active=False, user_prompt='startup prompt g1')
    assert step(f, lambda: f.agent('worker-1').get('runtime_ready') == '1')
    assert ready.alive() or ready.decision() is not None
    assert f.v2.bus.message(env['message_id']).get('outcome') != 'completed'
    assert typed_any(f, 'worker-1') == []


# ------------------------------------------------------------------ resume of an idle hook worker
def test_resume_reattaches_an_idle_hook_worker_without_escape(hooked):
    f = hooked
    waiting = f.test_ready['worker-1']
    f.v2.close()
    again = fleet.Fleet(f.path, f.r, f.mgr, sessions=f.test_sessions)
    for name in ('test_sessions', 'test_claude', 'test_threads', 'test_hooks', 'test_panes', 'test_ready', 'test_close'):
        setattr(again, name, getattr(f, name))
    f.test_close.append(again)
    again.resume_requested = True
    again.v2.resume_run()
    step(again, lambda: False, timeout=2)
    assert again.agent('worker-1')['generation'] == '0', 'an idle hook worker must be reattached, not replaced'
    assert escapes(again, 'worker-1') == [], 'Escape kills the waiting hook (research §4)'
    assert waiting.alive()
    if again.agent('supervisor')['state'] == 'starting':  # the Codex supervisor is respawned on resume
        assert codex_turn(again, 'READY')['status'] == 'ready'
    dispatch(again, ['worker-1'])
    assert 'Write worker-1/part.md' in delivered(again, waiting)['reason']
    assert typed_any(again, 'worker-1') == []


# ------------------------------------------------------------------ sub-fleet delegation
def test_delegated_slot_hook_lets_go_and_takes_nothing(hooked):
    f = hooked
    hook = f.test_ready['worker-1']
    # A sub-fleet adopts this pane and types its role change: the waiting hook must step aside.
    f.setf('worker-1', state='delegated')
    assert step(f, lambda: not hook.alive(), timeout=15), 'the hook kept the delegated pane busy'
    assert hook.decision() is None


# ------------------------------------------------------------------ R1-02: operator lines while the hook waits
# Row shapes measured on Claude Code 2.1.280 (worker-1/evidence/manual_input/run_*/transcript.jsonl):
# Enter writes {"type":"queue-operation","operation":"enqueue","content":<line>} at once (<0.5 s);
# the line starts its own turn with a content-less "dequeue" and a plain user row
# (origin.kind=human, promptSource=queued); absorbed into a running tool turn it is a "remove"
# (reason absorbed_mid_turn) plus a queued_command attachment. Peer messages and <task-notification>
# entries (rewakes, background tasks) queue the same way and make the hook yield too (D-1).
YIELD_WITHIN = 12  # one hook POLL (5 s) plus margin


def queue_op(f, hook, operation, content=None, **extra):
    row = dict({'type': 'queue-operation', 'operation': operation, 'sessionId': hook.session}, **extra)
    if content is not None:
        row['content'] = content
    append(hook.transcript, [row])


def operator_turn(f, slot, line, answer):
    """Claude runs the queued operator line as its own turn, then its Stop hook fires."""
    thread = thread_of(f, slot, int(f.agent(slot)['generation']))
    append(transcript_of(f, slot, thread), [
        {'type': 'queue-operation', 'operation': 'dequeue', 'sessionId': thread},
        {'type': 'user', 'origin': {'kind': 'human'}, 'promptSource': 'queued', 'promptId': 'p-' + uuid.uuid4().hex[:8],
         'message': {'role': 'user', 'content': line}}])
    return stop(f, slot, answer, active=False)


def mailbox_len(f, slot='worker-1'):
    return f.r.llen(f.v2.bus.prefix + ':hook:' + slot + ':g' + f.agent(slot)['generation'] + ':mailbox')


def test_operator_line_queued_while_idle_makes_the_hook_yield(hooked):
    f = hooked
    hook = f.test_ready['worker-1']
    queue_op(f, hook, 'enqueue', 'Reply with exactly HUMAN1')
    assert step(f, lambda: not hook.alive(), timeout=YIELD_WITHIN), 'the hook kept the operator line queued'
    assert hook.decision() is None, 'yielding must not hand anything over'
    assert mailbox_len(f) == 0 and reports(f) == 0
    # the operator's own turn settles nothing and the next Stop hook waits again
    again = operator_turn(f, 'worker-1', 'Reply with exactly HUMAN1', 'HUMAN1')
    step(f, lambda: False, timeout=3)
    assert again.alive(), 'after the operator turn the hook must wait for deliveries again'
    assert any(e.get('event') in ('turn.ignored', 'turn.unsolicited') for e in audit(f, 'worker-1'))
    assert reports(f) == 0
    # business is neither lost nor duplicated
    dispatch(f, ['worker-1'])
    got = delivered(f, again)
    assert 'Write worker-1/part.md' in got['reason']
    env = f.v2.bus.slot_state('worker-1')
    stop(f, 'worker-1', REPORT.format(name='part.md'))
    assert step(f, lambda: status_of(f, env) == 'committed') and step(f, lambda: reports(f) == 1)
    assert typed_any(f, 'worker-1') == []


def test_queued_rewake_notification_makes_the_hook_yield_once_and_keeps_the_mailbox(hooked):
    # D-1: a queued notification behind a waiting hook would starve (measured > 5 min, worker-2 lab2)
    f = hooked
    hook = f.test_ready['worker-1']
    queue_op(f, hook, 'enqueue', '<task-notification>\n<summary>Stop hook feedback</summary>\n</task-notification>')
    assert step(f, lambda: not hook.alive(), timeout=YIELD_WITHIN), 'the hook kept a queued notification waiting'
    assert hook.decision() is None, 'yielding must not hand anything over'
    # a delivery posted meanwhile stays in the mailbox: neither typed nor taken
    dispatch(f, ['worker-1'])
    step(f, lambda: False, timeout=2)
    assert mailbox_len(f) == 1 and typed_any(f, 'worker-1') == []
    # the notification row is never dequeued: the next Stop must not yield to it again
    again = stop(f, 'worker-1', 'idle', active=False)
    got = delivered(f, again)
    assert 'Write worker-1/part.md' in got['reason'] and mailbox_len(f) == 0


def test_operator_line_absorbed_mid_turn_does_not_make_the_hook_yield(hooked):
    f = hooked
    got, env = handed(f)
    hook = f.test_ready['worker-1']
    # typed while the delivery turn runs a tool: Claude Code absorbs it into that turn
    queue_op(f, hook, 'enqueue', 'Reply with exactly HUMAN2')
    queue_op(f, hook, 'remove', 'Reply with exactly HUMAN2', reason='absorbed_mid_turn')
    append(hook.transcript, [{'type': 'attachment', 'attachment': {'type': 'queued_command', 'prompt': 'Reply with exactly HUMAN2',
                                                                    'origin': {'kind': 'human'}, 'humanTurn': True}}])
    waiting = stop(f, 'worker-1', REPORT.format(name='part.md'))
    # the delivery turn settles as before (R2-02, Claude Code's own behaviour) and nothing is pending:
    # the injected line is part of that turn, never a separate operator turn
    assert step(f, lambda: status_of(f, env) == 'committed')
    assert step(f, lambda: reports(f) == 1)
    step(f, lambda: False, timeout=YIELD_WITHIN)
    assert waiting.alive(), 'a balanced (removed) operator entry made the hook yield'
    assert not any(e.get('event') in ('turn.ignored', 'turn.unsolicited') for e in audit(f, 'worker-1')
                   if e.get('error') != 'no received delivery (manual or duplicate turn)'), \
        'the injected line was treated as its own operator turn'


def test_line_queued_during_a_delivery_turn_settles_it_then_yields_and_loses_nothing(hooked):
    f = hooked
    got, env1 = handed(f)
    hook = f.test_ready['worker-1']
    # typed while the delivery turn generates text: stays queued until the turn ends (no dequeue yet)
    queue_op(f, hook, 'enqueue', 'Reply with exactly HUMAN3')
    after = stop(f, 'worker-1', REPORT.format(name='part.md'))
    assert step(f, lambda: status_of(f, env1) == 'committed'), 'the delivery turn must still settle first'
    assert step(f, lambda: not after.alive(), timeout=YIELD_WITHIN), 'the hook did not yield to the queued line'
    assert after.decision() is None
    # a delivery arriving while the operator turn runs waits in the mailbox: not typed, not lost
    dispatch(f, ['worker-1'], what='second.md')
    step(f, lambda: False, timeout=2)
    assert typed_any(f, 'worker-1') == [] and mailbox_len(f) == 1
    env2 = f.v2.bus.slot_state('worker-1')
    again = operator_turn(f, 'worker-1', 'Reply with exactly HUMAN3', 'HUMAN3')
    got = delivered(f, again)
    assert 'second.md' in got['reason']
    assert mailbox_len(f) == 0
    stop(f, 'worker-1', REPORT.format(name='second.md'))
    assert step(f, lambda: status_of(f, env2) == 'committed')
    assert step(f, lambda: reports(f) == 2) and reports(f) == 2, 'a delivery was lost or reported twice'
    assert typed_any(f, 'worker-1') == []


def test_rewake_keeps_off_the_mailbox_during_the_operator_turn(hooked):
    f = hooked
    hook = f.test_ready['worker-1']
    entry, proc = rewake(f, 'worker-1')
    try:
        queue_op(f, hook, 'enqueue', 'Reply with exactly HUMAN4')
        assert step(f, lambda: not hook.alive(), timeout=YIELD_WITHIN)
        dispatch(f, ['worker-1'])
        step(f, lambda: proc.poll() is not None, timeout=25)  # longer than claude_hook.GRACE
        assert proc.poll() != 2, 'the rewake took the delivery into the operator turn'
        assert mailbox_len(f) == 1 and typed_any(f, 'worker-1') == []
        again = operator_turn(f, 'worker-1', 'Reply with exactly HUMAN4', 'HUMAN4')
        assert 'Write worker-1/part.md' in delivered(f, again)['reason']
    finally:
        if proc.poll() is None:
            proc.kill()


def test_an_operator_entry_never_dequeued_makes_the_hook_yield_only_once(hooked):
    f = hooked
    hook = f.test_ready['worker-1']
    queue_op(f, hook, 'enqueue', 'Reply with exactly HUMAN5')
    assert step(f, lambda: not hook.alive(), timeout=YIELD_WITHIN)
    # the queued row is never dequeued (e.g. Claude Code crashed mid-write); the next Stop must not
    # yield to the same row again, or the channel would never re-arm
    again = stop(f, 'worker-1', 'idle', active=False)
    step(f, lambda: False, timeout=YIELD_WITHIN)
    assert again.alive(), 'the hook yielded twice to the same queued row'
    dispatch(f, ['worker-1'])
    assert 'Write worker-1/part.md' in delivered(f, again)['reason']


@pytest.mark.parametrize('source', ['hook', 'rewake'])
def test_operator_retyping_the_delivery_verbatim_is_not_the_delivery(hooked, source):
    # R2-01 (review round 2): settlement must look at where the latest start input came from (hook
    # feedback / rewake vs a typed row), not only at its text. Failed on 9df19c2c (verbatim retype settled).
    f = hooked
    got, env = handed(f)
    # the delivery turn is interrupted (Escape kills the waiting hook; no Stop for that turn) ...
    f.mgr.send_key(f.agent('worker-1')['pane'], 'Escape')
    # ... and the operator then resends the same instruction by hand: a plain user row, not hook feedback
    manual = stop(f, 'worker-1', REPORT.format(name='part.md'), active=False, user_prompt=got['reason'])
    step(f, lambda: status_of(f, env) == 'committed', timeout=5)
    assert status_of(f, env) != 'committed', 'an operator-typed row settled the hook delivery'
    manual.kill()
    # the same delivery arriving through a legitimate source (hook feedback / rewake wake-up) still settles
    transcript = transcript_of(f, 'worker-1', f.test_threads['worker-1'])
    append(transcript, feedback_rows(got['reason']) if source == 'hook' else rewake_rows(got['reason']))
    stop(f, 'worker-1', REPORT.format(name='part.md'), active=source == 'hook')
    assert step(f, lambda: status_of(f, env) == 'committed'), 'a %s-delivered turn did not settle' % source
    assert f.v2.bus.message(env['message_id'])['outcome'] == 'completed'


def test_line_queued_before_the_ready_hook_starts_makes_it_yield_at_once(hooked):
    f = hooked
    f.test_ready['worker-2'].kill()
    # the operator typed while the READY turn was still running: the enqueue row is already there
    # when that turn's Stop hook starts (measured scenario C, worker-1/manual-input-measurement.md)
    thread = thread_of(f, 'worker-2', 0)
    append(transcript_of(f, 'worker-2', thread), [
        {'type': 'queue-operation', 'operation': 'enqueue', 'sessionId': thread, 'content': 'Reply with exactly EARLY'}])
    hook = stop(f, 'worker-2', 'READY', active=False)
    assert step(f, lambda: not hook.alive(), timeout=YIELD_WITHIN), 'a line queued before the wait was not seen'
    assert hook.decision() is None


def test_dequeue_without_content_balances_fifo_and_the_second_line_still_runs(hooked):
    f = hooked
    hook = f.test_ready['worker-1']
    queue_op(f, hook, 'enqueue', 'Reply with exactly FIRST')
    queue_op(f, hook, 'enqueue', 'Reply with exactly SECOND')
    assert step(f, lambda: not hook.alive(), timeout=YIELD_WITHIN)
    # Claude Code runs the oldest line: one content-less dequeue pops FIRST (FIFO), SECOND stays queued
    after_first = operator_turn(f, 'worker-1', 'Reply with exactly FIRST', 'FIRST')
    assert step(f, lambda: not after_first.alive(), timeout=YIELD_WITHIN), \
        'SECOND is still queued, but the hook did not yield to it (starved until the next delivery)'
    after_second = operator_turn(f, 'worker-1', 'Reply with exactly SECOND', 'SECOND')
    step(f, lambda: False, timeout=YIELD_WITHIN)
    assert after_second.alive(), 'nothing is queued any more: the hook must wait for deliveries'


def test_remove_matches_by_content_and_leaves_the_other_line_queued(hooked):
    f = hooked
    got, env = handed(f)
    hook = f.test_ready['worker-1']
    queue_op(f, hook, 'enqueue', 'Reply with exactly KEEP')
    queue_op(f, hook, 'enqueue', 'Reply with exactly ABSORBED')
    # only the second (not the oldest) line is absorbed into the running tool turn
    queue_op(f, hook, 'remove', 'Reply with exactly ABSORBED', reason='absorbed_mid_turn')
    after = stop(f, 'worker-1', REPORT.format(name='part.md'))
    assert step(f, lambda: status_of(f, env) == 'committed')
    assert step(f, lambda: not after.alive(), timeout=YIELD_WITHIN), 'KEEP is still queued but the hook did not yield'
    again = operator_turn(f, 'worker-1', 'Reply with exactly KEEP', 'KEEP')
    step(f, lambda: False, timeout=YIELD_WITHIN)
    assert again.alive()


def test_notification_and_a_later_operator_line_each_get_their_own_yield(hooked):
    f = hooked
    hook = f.test_ready['worker-1']
    note = '<task-notification>\n<summary>Background command finished</summary>\n</task-notification>'
    queue_op(f, hook, 'enqueue', note)
    assert step(f, lambda: not hook.alive(), timeout=YIELD_WITHIN), 'a queued notification did not make the hook yield'
    assert hook.decision() is None
    # the notification runs as its own turn; the operator types while it runs
    thread = hook.session
    append(hook.transcript, [{'type': 'queue-operation', 'operation': 'dequeue', 'sessionId': thread},
                             {'type': 'user', 'origin': {'kind': 'task-notification'}, 'message': {'role': 'user', 'content': note}}])
    queue_op(f, hook, 'enqueue', 'Reply with exactly AFTER-NOTE')
    after_note = stop(f, 'worker-1', 'noted', active=False)
    assert step(f, lambda: not after_note.alive(), timeout=YIELD_WITHIN), 'the operator line behind a notification was missed'
    assert after_note.decision() is None
    # once both ran, the hook waits for deliveries again and hands the next one over
    after_line = operator_turn(f, 'worker-1', 'Reply with exactly AFTER-NOTE', 'AFTER-NOTE')
    step(f, lambda: False, timeout=YIELD_WITHIN)
    assert after_line.alive(), 'nothing is queued any more: the hook must wait for deliveries'
    dispatch(f, ['worker-1'])
    assert 'Write worker-1/part.md' in delivered(f, after_line)['reason']


def test_queued_peer_message_makes_the_hook_yield(hooked):
    f = hooked
    hook = f.test_ready['worker-1']
    queue_op(f, hook, 'enqueue', '<cross-session-message from="uds:/tmp/cc-socks/1.sock" from-name="peer">\n'
                                 'status?\n</cross-session-message>')
    assert step(f, lambda: not hook.alive(), timeout=YIELD_WITHIN), 'the hook kept a queued peer message waiting'
    assert hook.decision() is None and mailbox_len(f) == 0


# ------------------------------------------------------------------ D-1: turns the hook did not start
# A turn can start without the Stop hook yielding to it: the operator types after Escape killed the
# waiting hook, or a queued entry starts while no hook waits. The rewake fallback of the last Stop
# must not take a delivery into such a turn (TurnWatch); Escape (an interrupted row) or a StopFailure
# ending it makes the rewake eligible again, and a normal Stop hands the delivery over itself.
INTERRUPTED_ROW = {'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'text', 'text': '[Request interrupted by user]'}]}}
TURN_STARTS = {
    'typed': lambda thread: [{'type': 'user', 'origin': {'kind': 'human'}, 'promptId': 'p-typed',
                              'message': {'role': 'user', 'content': 'Reply with exactly TYPED'}}],
    'dequeue': lambda thread: [{'type': 'queue-operation', 'operation': 'dequeue', 'sessionId': thread}],
    'peer': lambda thread: [{'type': 'user', 'origin': {'kind': 'peer'}, 'promptId': 'p-peer',
                             'message': {'role': 'user', 'content': '<cross-session-message from="x">hi</cross-session-message>'}}],
}
TAKES_WITHIN = claude_hook.GRACE + 15


def rewake_watching(f, slot='worker-1'):
    """Start the rewake of the READY Stop and wait until it watches the transcript (TurnWatch takes
    the transcript size at its start: rows written before that are not seen)."""
    key = f.v2.bus.prefix + ':hook:' + slot + ':rewake'
    before = f.r.get(key)
    entry, proc = rewake(f, slot)
    assert step(f, lambda: f.r.get(key) != before, timeout=10), 'the rewake never started'
    return proc


def unarmed_turn(f, start='typed'):
    """Escape kills the waiting READY hook, then a turn starts that the hook did not yield to."""
    hook = f.test_ready['worker-1']
    proc = rewake_watching(f)
    f.mgr.send_key(hook.pane, 'Escape')
    hook.proc.wait(10)
    append(hook.transcript, TURN_STARTS[start](hook.session))
    return hook, proc


def stays_off(f, proc):
    step(f, lambda: proc.poll() is not None, timeout=claude_hook.GRACE + 5)
    assert proc.poll() != 2, 'the rewake took the delivery into a running turn'
    assert mailbox_len(f) == 1 and typed_any(f, 'worker-1') == []


@pytest.mark.parametrize('start', sorted(TURN_STARTS))
def test_rewake_keeps_off_a_turn_the_hook_did_not_start(hooked, start):
    f = hooked
    hook, proc = unarmed_turn(f, start)
    try:
        dispatch(f, ['worker-1'])
        env = f.v2.bus.slot_state('worker-1')
        stays_off(f, proc)
        # that turn ends with its own Stop: the sync hook hands the delivery over and it settles
        after = stop(f, 'worker-1', 'TURN', active=False)
        got = delivered(f, after)
        assert 'Write worker-1/part.md' in got['reason'] and mailbox_len(f) == 0
        assert any(e.get('event') == 'tui.hooked' for e in audit(f, 'worker-1'))
        stop(f, 'worker-1', REPORT.format(name='part.md'))
        assert step(f, lambda: status_of(f, env) == 'committed')
        assert step(f, lambda: reports(f) == 1) and proc.poll() != 2
        assert typed_any(f, 'worker-1') == []
    finally:
        if proc.poll() is None:
            proc.kill()


def test_escape_during_a_turn_the_hook_did_not_start_lets_the_rewake_recover(hooked):
    f = hooked
    hook, proc = unarmed_turn(f)
    try:
        dispatch(f, ['worker-1'])
        env = f.v2.bus.slot_state('worker-1')
        stays_off(f, proc)
        append(hook.transcript, [INTERRUPTED_ROW])  # Escape ends that turn without a Stop
        assert step(f, lambda: proc.poll() is not None, timeout=TAKES_WITHIN), 'the rewake never recovered'
        err = proc.stderr.read()
        assert proc.returncode == 2 and 'Write worker-1/part.md' in err and mailbox_len(f) == 0
        append(hook.transcript, rewake_rows(err))
        stop(f, 'worker-1', REPORT.format(name='part.md'), active=False)
        assert step(f, lambda: status_of(f, env) == 'committed'), 'the rewake-delivered turn did not settle'
        assert typed_any(f, 'worker-1') == []
    finally:
        if proc.poll() is None:
            proc.kill()


def test_stop_failure_ending_a_busy_turn_lets_the_rewake_recover(hooked):
    f = hooked
    hook, proc = unarmed_turn(f)
    try:
        dispatch(f, ['worker-1'])
        stays_off(f, proc)
        failure, = settings(f, 'worker-1')['hooks']['StopFailure']
        data = payload(f, 'worker-1', hook.session, 'API Error: 529 overloaded', False)
        data.update(hook_event_name='StopFailure', error='overloaded')
        done = subprocess.run(shlex.split(failure['hooks'][0]['command']), input=json.dumps(data),
                              capture_output=True, text=True, timeout=30)
        assert done.returncode == 0
        assert f.r.get(f.v2.bus.prefix + ':hook:worker-1:turnend'), 'StopFailure did not mark the turn end'
        assert step(f, lambda: proc.poll() is not None, timeout=TAKES_WITHIN), 'the rewake never recovered'
        assert proc.returncode == 2 and 'Write worker-1/part.md' in proc.stderr.read()
        assert typed_any(f, 'worker-1') == []
    finally:
        if proc.poll() is None:
            proc.kill()


def test_rewake_ignores_the_final_assistant_row_of_the_stopped_turn(hooked):
    f = hooked
    hook = f.test_ready['worker-1']
    proc = rewake_watching(f)
    try:
        f.mgr.send_key(hook.pane, 'Escape')
        hook.proc.wait(10)
        # Claude Code flushes the stopped turn's final assistant row after the Stop event
        append(hook.transcript, [{'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'READY'}]}}])
        dispatch(f, ['worker-1'])
        assert step(f, lambda: proc.poll() is not None, timeout=TAKES_WITHIN), 'an assistant row kept the rewake off'
        assert proc.returncode == 2 and 'Write worker-1/part.md' in proc.stderr.read()
    finally:
        if proc.poll() is None:
            proc.kill()


def test_human_hold_expiry_does_not_let_the_rewake_into_a_running_turn(hooked):
    # 622aebea: once the 600 s HUMAN_HOLD ran out, a still running operator turn got the delivery
    # mid-turn. The expiry is simulated by deleting the key; no real-time wait.
    f = hooked
    hook = f.test_ready['worker-1']
    proc = rewake_watching(f)
    try:
        queue_op(f, hook, 'enqueue', 'Reply with exactly LONG')
        assert step(f, lambda: not hook.alive(), timeout=YIELD_WITHIN)
        human = f.v2.bus.prefix + ':hook:worker-1:human'
        ttl = f.r.ttl(human)
        assert claude_hook.HUMAN_HOLD - 60 < ttl <= claude_hook.HUMAN_HOLD == 600
        # the operator's turn starts and outlives the hold
        append(hook.transcript, [{'type': 'queue-operation', 'operation': 'dequeue', 'sessionId': hook.session},
                                 {'type': 'user', 'origin': {'kind': 'human'}, 'promptSource': 'queued',
                                  'message': {'role': 'user', 'content': 'Reply with exactly LONG'}}])
        f.r.delete(human)
        dispatch(f, ['worker-1'])
        stays_off(f, proc)
        # the long turn ends (Escape): the rewake recovers the delivery
        append(hook.transcript, [INTERRUPTED_ROW])
        assert step(f, lambda: proc.poll() is not None, timeout=TAKES_WITHIN), 'the rewake never recovered'
        assert proc.returncode == 2 and 'Write worker-1/part.md' in proc.stderr.read()
    finally:
        if proc.poll() is None:
            proc.kill()
