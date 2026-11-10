"""Codex deliveries never use the TUI input box; turns settle only the delivery that started them.

Owner: worker-2. The single final test file for the `codex queue` channel; it replaces the earlier
`tests/test_codex_no_typing.py` (kept as evidence under worker-2/regression/).
Interfaces: worker-1/implementation-plan.md §0 I1-I7, §2, §3, §6 (module `cmux_fleet.codex_queue`).
Nothing here is skipped: every acceptance test runs against the product.

How a delivery reaches Codex is known only to the `Queue` stub (plan §6.1 seam: codex_queue.enqueue).
The stub keeps its own log of queued messages. A separate, independent log of real
send_prompt_to_idle / send_key calls (PASTE_CALLS / KEY_CALLS) proves the input box was never used.
Assertions about what was delivered check that the business body is contained in the queued text,
so a unique-delivery wrapper around the text (F1, authorized) does not change them. Rollouts always
carry exactly the text that was queued.

Real codex-cli 0.162.1 facts used as inputs (worker-1 probe/home9d, worker-2 lab/evidence4):
- `task_started.turn_attribution.turn_trigger` is "user" or "queue";
- a queued input is an `item_completed` UserMessage with its `client_id`;
- the next queued turn starts ~2 ms after the previous `task_complete`;
- an idle daemon-mode TUI consumes the queued row before `codex queue` returns, so the client_id
  is often unreadable;
- notify input-messages accumulate;
- one turn-id can notify twice;
- a queued item id never appears in the rollout and is never a turn-id.
The shared Claude/Pi text check `runtime_v2.turn_answers` is deliberately not asserted.
"""
import datetime
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from test_fleet import FakeMgr, write_rollout, fx, turn as fleet_turn  # noqa: F401  (fx fixture)
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_resume import managed, task_run, crash_and_resume  # noqa: F401  (fixtures)
import hook_sim
import test_subfleet as ts
from cmux_fleet import codex_queue, fleet, runtime_v2, subfleet

WORKERS = ['worker-1', 'worker-2']
ORIGINAL_ENQUEUE = codex_queue.enqueue  # the real CLI wrapper, captured before any stub is installed
ENTER_KEYS = ('Enter', 'Return', 'C-m', 'KP_Enter')
REPORT = 'Done:\n- {0}\nEvidence:\n- {1}\nNot done: none\nBlocked: none'
WAIT = json.dumps({'status': 'RUNNING', 'completion': 50, 'reason': 'waiting', 'dispatch': []})
# Long enough that two report follow-ups share their first 120 normalised characters.
SHARED = 'compared the eight vendor quotes line by line against the frozen bill of materials and the approved budget sheet'


QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS queued_items (id TEXT PRIMARY KEY NOT NULL, thread_id TEXT NOT NULL,
    payload_json TEXT NOT NULL, queue_order INTEGER NOT NULL, created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS queued_items_thread_order_idx ON queued_items(thread_id, queue_order);
CREATE TABLE IF NOT EXISTS queued_thread_revisions (revision INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL UNIQUE);
CREATE TRIGGER IF NOT EXISTS queued_items_revision_after_insert AFTER INSERT ON queued_items BEGIN
    INSERT INTO queued_thread_revisions (thread_id) VALUES (NEW.thread_id) ON CONFLICT(thread_id) DO UPDATE
    SET revision = (SELECT COALESCE(MAX(revision), 0) + 1 FROM queued_thread_revisions); END;
CREATE TRIGGER IF NOT EXISTS queued_items_revision_after_delete AFTER DELETE ON queued_items BEGIN
    INSERT INTO queued_thread_revisions (thread_id) VALUES (OLD.thread_id) ON CONFLICT(thread_id) DO UPDATE
    SET revision = (SELECT COALESCE(MAX(revision), 0) + 1 FROM queued_thread_revisions); END;
"""  # shared fixture: queue_1.sqlite of codex-cli 0.162.1 (worker-1 probe/w2copy .schema; migrations table omitted)


def plan(workers, note='split the work'):
    return json.dumps({'status': 'RUNNING', 'completion': 10, 'reason': note,
                       'dispatch': [{'to': w, 'instruction': 'Write ' + w + '/part.md with the findings.'}
                                    for w in workers]})


# ---------------------------------------------------------------- rollout rows (real 0.162.1 shape)
def stamp(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def turn_rows(turn, trigger, users, answer, started_ms=None, complete=True):
    """task_started (with turn_attribution) + one UserMessage per (client_id, text) + task_complete."""
    t0 = int(time.time() * 1000) if started_ms is None else started_ms
    rows = [{'timestamp': stamp(t0), 'type': 'event_msg', 'payload': {
        'type': 'task_started', 'turn_id': turn,
        'turn_attribution': {'turn_id': turn, 'turn_trigger': trigger, 'parent_turn_id': None, 'root_turn_id': turn}}}]
    for i, (client, text) in enumerate(users):
        rows.append({'timestamp': stamp(t0 + 5 + i), 'type': 'event_msg', 'payload': {
            'type': 'item_completed', 'turn_id': turn, 'item': {
                'type': 'UserMessage', 'id': uuid.uuid4().hex, 'client_id': client or '',
                'content': [{'type': 'text', 'text': text, 'text_elements': []}]}}})
    if complete:
        rows.append({'timestamp': stamp(t0 + 50), 'type': 'event_msg', 'payload': {
            'type': 'task_complete', 'turn_id': turn, 'last_agent_message': answer}})
    return rows


# ---------------------------------------------------------------- channel stub (plan §6.1 seam)
class Queue:
    """Stands in for `codex queue`; never runs the real CLI and never types.

    Each message gets a unique queued id and a unique *real* client_id (the one the rollout will
    carry). `readable=False` models the measured idle case (row consumed before the CLI returned,
    enqueue reports client_id ''). Since plan v3 the product never reads either id and never touches
    Codex's queue_1.sqlite. `hook(item)` runs while the CLI is still "running"."""

    def __init__(self):
        self.items, self.fail, self.readable, self.hook = [], None, True, None

    def enqueue(self, cfg, name, thread, text, **kw):
        if self.fail:
            raise codex_queue.QueueError(self.fail)
        real = str(uuid.uuid4())
        item = {'queued_id': str(uuid.uuid4()), 'client_id': real if self.readable else '', 'thread': thread}
        self.items.append(dict(item, name=name, text=text, real_client=real))
        if self.hook:
            self.hook(self.items[-1])
        return item


def install_queue(monkeypatch, q=None):
    q = q or Queue()
    monkeypatch.setattr(codex_queue, 'enqueue', q.enqueue)
    return q


# Independent log of real paste / key calls, separate from the queue log and from FakeMgr.sent
# (the shared conftest may record queued text in `sent`). Spied at class level, this module only.
PASTE_CALLS, KEY_CALLS = [], []


@pytest.fixture(autouse=True)
def paste_spy(monkeypatch):
    PASTE_CALLS.clear()
    KEY_CALLS.clear()
    for cls in (FakeMgr, ts.WorldMgr):
        real_paste, real_key = cls.send_prompt_to_idle, cls.send_key

        def paste(self, pane, text, bracketed=False, _real=real_paste):
            PASTE_CALLS.append((pane, text))
            return _real(self, pane, text, bracketed)

        def key(self, pane, k, _real=real_key):
            KEY_CALLS.append((pane, k))
            return _real(self, pane, k)
        monkeypatch.setattr(cls, 'send_prompt_to_idle', paste)
        monkeypatch.setattr(cls, 'send_key', key)


def input_box_use(pane, sent=()):
    """Everything that went into `pane`'s input box: real paste calls, Enter keys, and text in `sent`."""
    return ([t for p, t in PASTE_CALLS if p == pane] + [k for p, k in KEY_CALLS if p == pane and k in ENTER_KEYS]
            + [t for p, t in sent if p == pane and not str(t).startswith('<key ')])


def assert_no_input_box(f, pane=None):
    pane = pane or f.agent('supervisor')['pane']
    assert input_box_use(pane, f.mgr.sent) == [], 'the Codex input box was used'


# ---------------------------------------------------------------- harness
def drive(f, fn):
    """Run a blocking hook entry point while the daemon steps (as the real daemon would)."""
    box = {}
    thread = threading.Thread(target=lambda: box.setdefault('value', fn()))
    thread.start()
    deadline = time.time() + 25
    while thread.is_alive() and time.time() < deadline:
        f.v2.step(20)
    thread.join(2)
    assert not thread.is_alive()
    return box['value']


def typed(f, slot, pane=None):
    """Text that reached a (Claude worker) pane: pasted, or handed over by its Stop hook (hook channel,
    conftest/hook_sim). An explicit ``pane`` reports that pane's pastes only."""
    if pane:
        return [text for p, text in f.mgr.sent if p == pane]
    pane = f.agent(slot)['pane']
    return [text for p, text in f.mgr.sent if p == pane] + hook_sim.handed_to(f, slot)


def session(f, slot):
    gen = int(f.agent(slot)['generation'])
    return f.test_threads[slot] if gen == 0 else f.test_threads.setdefault((slot, gen), str(uuid.uuid4()))


def queued(f, slot='supervisor', thread=None):
    thread = thread or f.agent(slot).get('thread')
    return [q for q in f.q.items if q['thread'] == thread and q['name'] == slot]


def queued_texts(f, slot='supervisor', thread=None):
    return [q['text'] for q in queued(f, slot, thread)]


def history(f):
    return ['startup prompt'] + queued_texts(f)


def last(f):
    """(real client_id, queued text) the rollout of the in-flight delivery's own turn carries."""
    q = queued(f)
    assert q, 'nothing was queued for the supervisor'
    return q[-1]['real_client'], q[-1]['text']


def codex_turn(f, answer, *, turn=None, trigger='queue', users=None, thread=None, generation=None,
               inputs=None, before=(), after=(), started_ms=None, late=None):
    """A finished supervisor turn: write its rollout, then call the real notify entry point.
    Defaults describe the turn that the in-flight delivery itself started."""
    gen = int(f.agent('supervisor')['generation']) if generation is None else generation
    thread = thread or session(f, 'supervisor')
    turn = turn or str(uuid.uuid4())
    users = [last(f)] if users is None else users
    cwd = fleet.agent_cwd(f.cfg, 'supervisor')
    rows = list(before) + turn_rows(turn, trigger, users, answer, started_ms) + list(after)
    if late is None:
        write_rollout(f.test_sessions, thread, cwd, rows=rows)
    else:  # notify fires before Codex flushes the rollout
        write_rollout(f.test_sessions, thread, cwd, rows=list(before) + rows[len(before):len(before) + 1])
        threading.Timer(late, lambda: write_rollout(f.test_sessions, thread, cwd, rows=rows)).start()
    payload = json.dumps({'type': 'agent-turn-complete', 'thread-id': thread, 'turn-id': turn,
                          'input-messages': history(f) if inputs is None else inputs, 'last-assistant-message': answer})
    return drive(f, lambda: fleet.notify(f.path, 'supervisor', str(gen), payload, f.test_sessions))


def startup_ready(f):
    """The first prompt is an argv argument (allowed); its READY turn is user-triggered."""
    return codex_turn(f, 'READY', trigger='user', users=[('', 'startup prompt')], inputs=['startup prompt'])


def claude_turn(f, slot, text, prompt=None):
    """A Claude worker's Stop hook (hook channel: hook_sim stands in for its blocking wait)."""
    gen = str(f.agent(slot)['generation'])  # read before the hook thread: the test client has one connection
    thread = session(f, slot)
    transcript = f.test_claude / slot / (thread + '.jsonl')
    transcript.parent.mkdir(parents=True, exist_ok=True)
    prompt = prompt if prompt is not None else typed(f, slot)[-1]
    rows = [hook_sim.user_row(prompt, hook_sim.was_handed(slot, prompt)),
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}}]
    transcript.write_text('\n'.join(json.dumps(x) for x in rows) + '\n')
    raw = json.dumps({'session_id': thread, 'transcript_path': str(transcript), 'last_assistant_message': text})
    return drive(f, lambda: hook_sim.claude_stop(f.path, slot, gen, raw))


def lease(f, slot='supervisor'):
    return f.v2.bus.slot_state(slot)


def body_of(f, slot='supervisor'):
    return f.v2.bus.message(lease(f, slot)['message_id'])['body']['text']


def cq(f, field, slot='supervisor'):
    return f.r.hget(fleet.key('agent', slot), field)


def submit(f, goal='build M0 parts'):
    runtime_v2.post_event(f.v2.bus, dict(kind='task', request_id='task-' + uuid.uuid4().hex[:6], spec={'goal': goal}), 'new-task')
    assert f.v2.step(10)
    f.pump()


def audit(f):
    return [fields for _, fields in f.r.xrange(f.v2.bus.audit_key)]


def build(r, tmp_path, monkeypatch, ready=True):
    monkeypatch.setattr(fleet, 'trust_claude_dir', lambda cwd: None)  # never touch ~/.claude.json
    r.flushdb()
    run = tmp_path / ('run-' + uuid.uuid4().hex)
    (run / 'workspace').mkdir(parents=True)
    cfg = dict(run=str(run), redis_socket=r.connection_pool.connection_kwargs['path'], python=sys.executable,
               workers=list(WORKERS), mode='task', supervisor_cli='codex', worker_cli='claude',
               codex_home=str(tmp_path / 'codex-home'), transport_version=2, allow_development_v2=True,
               stall_timeout=0, v2_lease_ms=300000, max_busy=0, codex=str(tmp_path / 'no-real-codex'))
    fleet.save(run / 'config.json', cfg)
    f = fleet.Fleet(run / 'config.json', r, FakeMgr(), sessions=tmp_path / 'codex-home' / 'sessions')
    f.test_sessions, f.test_claude = tmp_path / 'codex-home' / 'sessions', tmp_path / 'claude-projects'
    f.test_threads = {s: str(uuid.uuid4()) for s in ['supervisor'] + WORKERS}
    f.q = install_queue(monkeypatch)
    f.start_all()
    r.set(f.v2.prefix + ':boot', 'running')
    if ready:
        assert startup_ready(f)['status'] == 'ready'
        for w in WORKERS:
            assert claude_turn(f, w, 'READY', prompt='startup prompt')['status'] == 'ready'
    return f


@pytest.fixture
def sup(private_redis_v2, tmp_path, monkeypatch):  # noqa: F811
    """Codex supervisor + 2 Claude workers on a private Redis, everyone READY."""
    f = build(private_redis_v2, tmp_path, monkeypatch)
    yield f
    f.v2.close()


@pytest.fixture
def booting(private_redis_v2, tmp_path, monkeypatch):  # noqa: F811
    f = build(private_redis_v2, tmp_path, monkeypatch, ready=False)
    yield f
    f.v2.close()


# ================================================================ A. completed_turn (fast callback, I5)
def _rollout(tmp_path, rows):
    cwd = tmp_path / 'agents' / 'supervisor'
    cwd.mkdir(parents=True, exist_ok=True)
    thread, sessions = str(uuid.uuid4()), tmp_path / 'sessions'
    write_rollout(sessions, thread, cwd, rows=rows)
    return cwd, thread, sessions


def test_completed_turn_accepts_a_finished_turn_after_the_next_queued_turn_started(tmp_path):
    t0 = int(time.time() * 1000)
    rows = turn_rows('T1', 'queue', [('c1', 'first')], 'ANSWER-1', t0) + \
        turn_rows('T2', 'queue', [('c2', 'second')], '', t0 + 52, complete=False)
    cwd, thread, sessions = _rollout(tmp_path, rows)
    data = {'thread-id': thread, 'turn-id': 'T1', 'last-assistant-message': 'ANSWER-1'}
    assert fleet.completed_turn(cwd, data, sessions) is True


def test_completed_turn_still_rejects_unfinished_wrong_and_foreign_turns(tmp_path):
    t0 = int(time.time() * 1000)
    rows = turn_rows('T1', 'queue', [('c1', 'first')], 'ANSWER-1', t0) + \
        turn_rows('T2', 'queue', [('c2', 'second')], '', t0 + 52, complete=False)
    cwd, thread, sessions = _rollout(tmp_path, rows)
    ask = lambda turn, text, where=cwd: fleet.completed_turn(where, {  # noqa: E731
        'thread-id': thread, 'turn-id': turn, 'last-assistant-message': text}, sessions)
    assert not ask('T2', '')                       # still running
    assert not ask('T9', 'ANSWER-1')               # unknown turn
    assert not ask('T1', 'OTHER')                  # other answer
    assert not ask('T1', 'ANSWER-1', tmp_path / 'elsewhere')  # other cwd
    assert not fleet.completed_turn(cwd, {'thread-id': 'not-a-uuid', 'turn-id': 'T1',
                                          'last-assistant-message': 'ANSWER-1'}, sessions)


# ================================================================ B. every entry avoids the input box (I7)
def test_first_prompt_is_argv_and_never_typed(sup):
    f = sup
    assert_no_input_box(f)
    assert f.q.items == []  # the start prompt is an argv argument, not a queued delivery
    spawned = [kw for kw in f.mgr.spawned if str(kw.get('prompt_file', '')).endswith('/supervisor-g0.md')]
    assert len(spawned) == 1, [kw.get('prompt_file') for kw in f.mgr.spawned]
    prompt = Path(spawned[0]['prompt_file']).read_text()
    assert prompt.strip()
    # the actor hands that prompt to codex as its last argv argument (the one allowed first prompt)
    argv = fleet.actor_argv(f.path, f.cfg, 'supervisor', 0, prompt)
    assert argv[-1] == prompt and 'queue' not in argv[:2]


def test_delivery_turn_settles_and_nothing_is_typed(sup):
    f = sup
    submit(f)
    assert_no_input_box(f)
    assert len(queued(f)) == 1 and body_of(f) in queued(f)[0]['text']
    assert cq(f, 'cq_state') == 'queued'
    assert any(e.get('event') == 'tui.queued' and e.get('slot') == 'supervisor' for e in audit(f))
    assert codex_turn(f, plan(['worker-1']))['status'] == 'committed'
    assert cq(f, 'cq_state') == 'bound'


def test_task_and_report_followups_bypass_input_box_each_once(sup):
    f = sup
    submit(f)
    assert codex_turn(f, plan(WORKERS))['status'] == 'committed'
    f.pump()
    for w in WORKERS:
        assert claude_turn(f, w, REPORT.format('wrote ' + w, w + '/part.md'))['status'] == 'committed'
        f.pump()
        assert codex_turn(f, WAIT)['status'] == 'committed'
        f.pump()
    assert_no_input_box(f)
    got = queued_texts(f)
    assert 'build M0 parts' in got[0], [g[:80] for g in got]
    for w in WORKERS:  # one follow-up per report (a later format correction may follow; also queued)
        assert len([t for t in got if '[' + w + ' report]' in t]) == 1, [g[:80] for g in got]


def test_startup_nudge_is_queued_not_typed_and_its_turn_binds(booting):
    f = booting
    for w in WORKERS:
        assert claude_turn(f, w, 'READY', prompt='startup prompt')['status'] == 'ready'
    out = codex_turn(f, 'Hello, how can I help?', trigger='user', users=[('', 'startup prompt')], inputs=['startup prompt'])
    assert out['status'] == 'rejected'
    f.v2.step(20)
    assert f.agent('supervisor')['state'] == 'starting'
    assert_no_input_box(f)
    nudges = queued_texts(f)
    assert len(nudges) == 1 and runtime_v2.STARTUP_NUDGE in nudges[0]
    assert codex_turn(f, 'READY')['status'] == 'ready'  # the nudge's own (bound) turn


def test_failed_startup_nudge_does_not_fall_back_to_typing(booting):
    f = booting
    for w in WORKERS:
        assert claude_turn(f, w, 'READY', prompt='startup prompt')['status'] == 'ready'
    f.q.fail = 'codex queue rc=1: simulated'
    codex_turn(f, 'Hello?', trigger='user', users=[('', 'startup prompt')], inputs=['startup prompt'])
    f.v2.step(20)
    assert_no_input_box(f)
    assert f.q.items == []


def test_handoff_prompt_is_queued_once_not_typed(sup):
    f = sup
    f.start_rotation('supervisor', 90000, 100000)
    f.v2.step(20)
    f.pump(); f.pump()
    assert_no_input_box(f)
    assert len([t for t in queued_texts(f) if 'same-task handoff' in t]) == 1


def test_v1_send_to_codex_supervisor_is_queued_not_typed(fx, monkeypatch):  # noqa: F811
    f = fx
    q = install_queue(monkeypatch)
    assert fleet.agent_cli(f.runtime_cfg('supervisor'), 'supervisor') == 'codex'
    pane = f.agent('supervisor')['pane']
    ok = f.send('supervisor', pane, 'v1 delivery to the Codex supervisor')
    assert input_box_use(pane, f.mgr.sent) == []
    texts = [i['text'] for i in q.items]
    assert (ok and len(texts) == 1 and 'v1 delivery to the Codex supervisor' in texts[0]) or (not ok and texts == [])


# ================================================================ C. fail closed, never fall back to paste
def test_failed_delivery_holds_and_is_never_retried_by_paste(sup):
    f = sup
    f.q.fail = 'codex queue rc=1: simulated'
    submit(f)
    assert f.agent('supervisor')['state'] == 'held' and f.paused()
    assert lease(f)['status'] == 'uncertain' and cq(f, 'cq_state') == 'failed'
    assert not any(e.get('event') in ('tui.typed', 'tui.queued') and e.get('slot') == 'supervisor' for e in audit(f))
    for _ in range(3):
        f.pump(); f.v2.step(20); f.v2.renew_tui()
    assert_no_input_box(f)
    assert f.q.items == []


def test_pane_gone_is_not_queued_and_not_typed(sup):
    """Plan §5b: a queued item outlives the TUI and would run on a later resume, so nothing is queued."""
    f = sup
    f.mgr.alive.discard(f.agent('supervisor')['pane'])
    submit(f)
    assert f.agent('supervisor')['state'] == 'held' and lease(f)['status'] == 'uncertain'
    assert f.q.items == []
    assert_no_input_box(f)


def test_v2_send_refuses_paste_fallback(sup):
    f = sup
    with pytest.raises(ValueError, match='fallback forbidden'):
        f.send('supervisor', f.agent('supervisor')['pane'], 'anything')


# ================================================================ D. settlement through fleet.notify (I4)
def test_manual_turn_with_delivery_only_in_history_is_not_settled(sup):
    """Root cause of the old text gate, checked at the Codex settlement entry (not turn_answers)."""
    f = sup
    submit(f)
    out = codex_turn(f, plan(['worker-1']), trigger='user', users=[('', 'just chatting')],
                     inputs=['startup prompt', last(f)[1], 'just chatting'])
    assert out['status'] == 'ignored', out
    assert lease(f)['status'] == 'received'
    assert typed(f, 'worker-1') == []


def test_late_duplicate_notify_of_an_old_turn_does_not_settle_a_same_head_delivery(sup):
    f = sup
    submit(f)
    assert codex_turn(f, plan(['worker-1']))['status'] == 'committed'
    f.pump()
    assert claude_turn(f, 'worker-1', REPORT.format(SHARED + ' -- vendor A', 'worker-1/a.md'))['status'] == 'committed'
    f.pump()
    t1, first, first_body = str(uuid.uuid4()), [last(f)], body_of(f)
    assert codex_turn(f, plan(['worker-1'], 'check vendor C too'), turn=t1)['status'] == 'committed'
    f.pump()
    assert claude_turn(f, 'worker-1', REPORT.format(SHARED + ' -- vendor C', 'worker-1/c.md'))['status'] == 'committed'
    f.pump()
    second = body_of(f)
    assert runtime_v2._norm(first_body)[:120] == runtime_v2._norm(second)[:120] and first_body != second
    out = codex_turn(f, WAIT, turn=t1, users=first)
    assert out is False or out['status'] == 'ignored', out
    assert lease(f)['status'] == 'received' and body_of(f) == second


def test_duplicate_notify_of_a_settled_turn_settles_nothing_else(sup):
    f = sup
    submit(f)
    turn, users = str(uuid.uuid4()), [last(f)]
    assert codex_turn(f, plan(['worker-1']), turn=turn)['status'] == 'committed'
    f.pump()
    again = codex_turn(f, plan(['worker-2'], 'changed my mind'), turn=turn, users=users)
    assert again is False or again['status'] == 'ignored', again
    f.pump()
    assert len(typed(f, 'worker-1')) == 1 and typed(f, 'worker-2') == []


def test_old_generation_notify_is_stale(sup):
    f = sup
    submit(f)
    assert codex_turn(f, plan(['worker-1']), generation=3)['status'] == 'stale'
    assert lease(f)['status'] == 'received'


def test_other_thread_notify_never_settles(sup):
    f = sup
    submit(f)
    out = codex_turn(f, plan(['worker-1']), thread=str(uuid.uuid4()))
    assert out is False or out['status'] in ('ignored', 'stale', 'rejected'), out
    assert lease(f)['status'] == 'received' and typed(f, 'worker-1') == []


def test_notify_for_finished_turn_settles_even_if_next_turn_already_started(sup):
    f = sup
    submit(f)
    nxt = turn_rows(str(uuid.uuid4()), 'user', [('', 'operator typed next')], '', complete=False)
    out = codex_turn(f, plan(['worker-1']), after=nxt)
    assert out is not False and out['status'] == 'committed', out


def test_notify_before_rollout_flush_settles_within_retry_window(sup):
    f = sup
    submit(f)
    out = codex_turn(f, plan(['worker-1']), late=.5)
    assert out is not False and out['status'] == 'committed', out


def test_fast_bind_callback_while_delivery_is_still_queuing(sup):
    """The turn ends and notify fires before `codex queue` has returned (cq_state 'queuing').
    The token is durable before the CLI runs, so the turn binds at once (I3/I4, no waiting); the late
    CLI answer (compare-and-set) must not turn 'bound' back into 'queued'; settled exactly once."""
    f = sup
    box = {}

    def turn_ends_before_cli_returns(item):
        turn, answer = str(uuid.uuid4()), plan(['worker-1'])
        thread = item['thread']
        write_rollout(f.test_sessions, thread, fleet.agent_cwd(f.cfg, 'supervisor'),
                      rows=turn_rows(turn, 'queue', [(item['real_client'], item['text'])], answer))
        payload = json.dumps({'type': 'agent-turn-complete', 'thread-id': thread, 'turn-id': turn,
                              'input-messages': ['startup prompt', item['text']], 'last-assistant-message': answer})
        box['payload'] = payload
        th = threading.Thread(target=lambda: box.setdefault('out', fleet.notify(f.path, 'supervisor', '0', payload, f.test_sessions)))
        th.start()
        box['thread'] = th
        time.sleep(1.0)  # the CLI is still "running"; the notify must not settle against 'queuing'
        box['state_during'] = f.r.hget(fleet.key('agent', 'supervisor'), 'cq_state')

    f.q.hook = turn_ends_before_cli_returns
    submit(f)
    f.q.hook = None
    deadline = time.time() + 25
    while box['thread'].is_alive() and time.time() < deadline:
        f.v2.step(20)
    box['thread'].join(2)
    assert box['state_during'] in ('queuing', 'bound')  # the CLI had not returned yet
    assert box['out'] is not False and box['out']['status'] == 'committed', box['out']
    assert cq(f, 'cq_state') == 'bound'  # not overwritten by the late CLI answer
    again = drive(f, lambda: fleet.notify(f.path, 'supervisor', '0', box['payload'], f.test_sessions))
    assert again is False or again['status'] == 'ignored', again  # settled exactly once
    f.pump()
    assert len(typed(f, 'worker-1')) == 1  # the plan was dispatched once


def test_busy_supervisor_gets_followups_one_at_a_time_and_each_once(sup):
    f = sup
    submit(f)
    assert codex_turn(f, plan(WORKERS))['status'] == 'committed'
    f.pump()
    assert claude_turn(f, 'worker-1', REPORT.format('wrote a', 'worker-1/a.md'))['status'] == 'committed'
    f.pump()
    n = len(queued(f))
    assert claude_turn(f, 'worker-2', REPORT.format('wrote b', 'worker-2/b.md'))['status'] == 'committed'
    f.pump(); f.pump()
    assert len(queued(f)) == n and f.r.llen(f.v2.bus.pending_key('supervisor')) == 1
    assert codex_turn(f, WAIT)['status'] == 'committed'
    f.pump(); f.pump()
    assert len(queued(f)) == n + 1 and '[worker-2 report]' in queued(f)[-1]['text']
    assert_no_input_box(f)


# ================================================================ E. codex_queue.bound (plan v3 I3: the pre-send token, nothing else)
def _rec(thread, body, queued_ms, **kw):
    """A live delivery record as deliver() writes it before the CLI runs (I2)."""
    token = codex_queue.new_token()
    rec = {'cq_delivery': 'd-' + uuid.uuid4().hex[:8], 'cq_token': token, 'cq_text': codex_queue.marked(body, token),
           'cq_thread': thread, 'cq_queued_at': str(queued_ms), 'cq_state': 'queued', 'cq_queued_id': str(uuid.uuid4()),
           'cq_turn': ''}
    rec.update(kw)
    return rec


def _bind(tmp_path, rows_of, rec_kw=None):
    """rows_of(rec) -> rollout rows; returns (rec, thread, sessions) with the rows written for rec's thread."""
    cwd = tmp_path / 'agents' / 'supervisor'
    cwd.mkdir(parents=True, exist_ok=True)
    thread, sessions = str(uuid.uuid4()), tmp_path / 'sessions'
    rec = _rec(thread, 'DELIVERY', int(time.time() * 1000), **(rec_kw or {}))
    write_rollout(sessions, thread, cwd, rows=rows_of(rec))
    return rec, thread, sessions


def test_bound_accepts_the_queue_turn_carrying_this_token_with_or_without_client_id(tmp_path):
    now = int(time.time() * 1000)
    rec, thread, sessions = _bind(tmp_path, lambda rec: turn_rows('TQ', 'queue', [('', rec['cq_text'])], 'OK', now + 100)
                                  + turn_rows('TC', 'queue', [('client-1', rec['cq_text'])], 'OK', now + 200))
    assert codex_queue.bound(rec, thread, 'TQ', sessions) == (True, '')            # no client_id at all
    assert codex_queue.bound(rec, thread, 'TC', sessions) == (True, '')            # a client_id is irrelevant


def test_race_idle_check_then_user_turn_absorbs_the_delivery_is_rejected(tmp_path):
    """Looked idle, the operator started an unknown turn, the delivery was absorbed into it: even with this
    delivery's exact text (token included) a user-triggered turn is rejected."""
    now = int(time.time() * 1000)
    rec, thread, sessions = _bind(tmp_path, lambda rec: turn_rows('TU', 'user', [('', 'operator question'), ('', rec['cq_text'])], 'x', now + 50)
                                  + turn_rows('TU1', 'user', [('', rec['cq_text'])], 'x', now + 150))
    for turn in ('TU', 'TU1'):
        ok, why = codex_queue.bound(rec, thread, turn, sessions)
        assert ok is False, (turn, why)


def test_race_operator_input_merged_into_the_queue_turn_is_rejected(tmp_path):
    now = int(time.time() * 1000)
    rec, thread, sessions = _bind(tmp_path, lambda rec: turn_rows('TQ', 'queue', [('', rec['cq_text']), ('', 'operator steer')], 'm', now + 50))
    ok, why = codex_queue.bound(rec, thread, 'TQ', sessions)
    assert ok is False, why


def test_queued_item_id_is_not_a_turn_id(tmp_path):
    now = int(time.time() * 1000)
    rec, thread, sessions = _bind(tmp_path, lambda rec: turn_rows('TQ', 'queue', [('', rec['cq_text'])], 'OK', now + 50))
    ok, why = codex_queue.bound(rec, thread, rec['cq_queued_id'], sessions)
    assert ok is False, why


def test_token_decides_between_identical_bodies(tmp_path):
    now = int(time.time() * 1000)
    cwd = tmp_path / 'agents' / 'supervisor'
    cwd.mkdir(parents=True)
    thread, sessions = str(uuid.uuid4()), tmp_path / 'sessions'
    r1, r2 = _rec(thread, 'SAME', now), _rec(thread, 'SAME', now + 100)
    write_rollout(sessions, thread, cwd, rows=turn_rows('T1', 'queue', [('', r1['cq_text'])], 'OK', now + 20)
                  + turn_rows('T2', 'queue', [('', r2['cq_text'])], 'OK', now + 120))
    assert codex_queue.bound(r1, thread, 'T1', sessions)[0] and codex_queue.bound(r2, thread, 'T2', sessions)[0]
    assert not codex_queue.bound(r1, thread, 'T2', sessions)[0]
    assert not codex_queue.bound(r2, thread, 'T1', sessions)[0]


def test_identical_body_without_this_token_never_binds_even_when_it_is_the_first_queue_turn(tmp_path):
    """The first same-body queue turn after queueing (bare body, or another delivery's token) is not this
    delivery; only the later turn carrying this delivery's own token binds."""
    now = int(time.time() * 1000)
    other = codex_queue.marked('DELIVERY', codex_queue.new_token())
    rec, thread, sessions = _bind(tmp_path, lambda rec: turn_rows('TOLD', 'queue', [('', 'DELIVERY')], 'OK', now - 1000)
                                  + turn_rows('TA', 'queue', [('', 'DELIVERY')], 'OK', now + 10)
                                  + turn_rows('TB', 'queue', [('', other)], 'OK', now + 50)
                                  + turn_rows('TMINE', 'queue', [('', rec['cq_text'])], 'OK', now + 100))
    for turn in ('TOLD', 'TA', 'TB'):
        assert codex_queue.bound(rec, thread, turn, sessions)[0] is False, turn
    assert codex_queue.bound(rec, thread, 'TMINE', sessions) == (True, '')


def test_bound_rejects_other_thread_edited_text_rebinding_and_closed_states(tmp_path):
    now = int(time.time() * 1000)
    rec, thread, sessions = _bind(tmp_path, lambda rec: turn_rows('TE', 'queue', [('', rec['cq_text'].replace('DELIVERY', 'DELIVERY edited'))], 'OK', now + 50)
                                  + turn_rows('TQ', 'queue', [('', rec['cq_text'])], 'OK', now + 100))
    assert codex_queue.bound(rec, str(uuid.uuid4()), 'TQ', sessions)[0] is False          # other thread
    assert codex_queue.bound(rec, thread, 'TE', sessions)[0] is False                     # text edited
    assert codex_queue.bound(dict(rec, cq_turn='TX', cq_state='bound'), thread, 'TQ', sessions)[0] is False  # bound elsewhere
    for state in ('abandoned', ''):
        assert codex_queue.bound(dict(rec, cq_state=state), thread, 'TQ', sessions)[0] is False, state
    assert codex_queue.bound(dict(rec, cq_token=''), thread, 'TQ', sessions)[0] is False
    for state in ('queuing', 'uncertain', 'failed'):  # I3: the token in the rollout proves the item really ran
        assert codex_queue.bound(dict(rec, cq_state=state), thread, 'TQ', sessions) == (True, ''), state


# ================================================================ F. end to end: ids, duplicates, merge races
def test_unreadable_client_id_still_settles_the_genuine_queue_turn_by_its_token(sup):
    """Positive F1 case: the idle TUI consumed the row before the CLI returned and the rollout carries no
    client_id. The delivery's own queue turn (its exact marked text) still settles, exactly once."""
    f = sup
    f.q.readable = False
    submit(f)
    token, text = cq(f, 'cq_token'), last(f)[1]
    assert cq(f, 'cq_state') == 'queued' and token and text == cq(f, 'cq_text')
    assert text.endswith('(fleet delivery ' + token + ')')
    turn = str(uuid.uuid4())
    assert codex_turn(f, plan(['worker-1']), turn=turn, users=[('', text)])['status'] == 'committed'
    assert cq(f, 'cq_state') == 'bound' and cq(f, 'cq_turn') == turn
    again = codex_turn(f, plan(['worker-2']), turn=turn, users=[('', text)])
    assert again is False or again['status'] == 'ignored', again


def test_unreadable_client_id_manual_turn_with_the_same_text_is_not_settled(sup):
    f = sup
    f.q.readable = False
    submit(f)
    out = codex_turn(f, plan(['worker-1']), trigger='user', users=[('', last(f)[1])])
    assert out['status'] == 'ignored', out
    assert lease(f)['status'] == 'received'


def test_identical_text_redelivery_is_not_settled_by_the_previous_turn(sup):
    """The same text is queued again (retry/replay) after the first copy's turn ran, id unreadable both
    times. The old turn must not settle the new copy (no cross-binding without the unique id)."""
    f = sup
    f.q.readable = False
    submit(f)
    client, text = last(f)
    t_old, start_old = str(uuid.uuid4()), int(time.time() * 1000)
    old_rows = turn_rows(t_old, 'queue', [(client, text)], 'first answer', start_old)
    time.sleep(.2)
    codex_queue.clear(f, 'supervisor', 'test: the first copy is abandoned before the same text is queued again')
    codex_queue.deliver(f, 'supervisor', 'replay-' + uuid.uuid4().hex[:6], text)
    assert cq(f, 'cq_state') == 'queued'
    write_rollout(f.test_sessions, session(f, 'supervisor'), fleet.agent_cwd(f.cfg, 'supervisor'), rows=old_rows)
    ok, why = codex_queue.bound(codex_queue.record(f.r.hgetall(fleet.key('agent', 'supervisor'))),
                                session(f, 'supervisor'), t_old, f.test_sessions)
    assert ok is False, why
    assert_no_input_box(f)


def test_merge_race_unknown_manual_turn_absorbing_the_delivery_is_not_settled(sup):
    """End to end: after the idle check an unknown operator turn absorbed the delivery (new turn-id,
    started after queueing, its last input even carries the delivery's client_id). Not settled;
    the delivery's own queue turn still settles afterwards."""
    f = sup
    submit(f)
    client, text = last(f)
    out = codex_turn(f, plan(['worker-1']), trigger='user', users=[('', 'operator question'), (client, text)])
    assert out is False or out['status'] == 'ignored', out
    assert lease(f)['status'] == 'received' and typed(f, 'worker-1') == []
    assert codex_turn(f, plan(['worker-1']))['status'] == 'committed'


def test_operator_input_merged_into_the_queue_turn_is_not_settled(sup):
    f = sup
    submit(f)
    out = codex_turn(f, plan(['worker-1']), users=[last(f), ('', 'operator steer')])
    assert out is False or out['status'] == 'ignored', out
    assert lease(f)['status'] == 'received'


def test_runtime_turn_id_equal_to_queued_item_id_is_not_settled(sup):
    f = sup
    submit(f)
    qid = queued(f)[-1]['queued_id']
    client, text = last(f)
    thread, answer = session(f, 'supervisor'), plan(['worker-1'])
    write_rollout(f.test_sessions, thread, fleet.agent_cwd(f.cfg, 'supervisor'),
                  rows=turn_rows(str(uuid.uuid4()), 'queue', [(client, text)], answer))
    payload = json.dumps({'type': 'agent-turn-complete', 'thread-id': thread, 'turn-id': qid,
                          'input-messages': history(f), 'last-assistant-message': answer})
    out = drive(f, lambda: fleet.notify(f.path, 'supervisor', '0', payload, f.test_sessions))
    assert out is False or out['status'] == 'ignored', out
    assert lease(f)['status'] == 'received'


# ================================================================ G. lifecycle as product behaviour (plan v3 I6)
# `abandoned` only means "this token can never settle anything". It is NOT a cancellation: the item may still
# sit in Codex's own queue and run there. The cases below therefore drive the item running afterwards.
def test_abandoned_is_not_cancelled_the_item_still_runs_and_never_settles(sup):
    f = sup
    submit(f)
    text = last(f)[1]
    other = {'cq_delivery': 'other-d', 'cq_token': codex_queue.new_token(), 'cq_text': 'x', 'cq_thread': str(uuid.uuid4()),
             'cq_generation': '0', 'cq_queued_at': '1', 'cq_state': 'queued', 'cq_queued_id': 'q', 'cq_turn': ''}
    f.r.hset(fleet.key('agent', 'codex-b'), mapping=other)  # another slot's live delivery
    rec = codex_queue.clear(f, 'supervisor', 'test abandon')
    assert rec['cq_text'] == text and cq(f, 'cq_state') == 'abandoned'
    assert f.r.hgetall(fleet.key('agent', 'codex-b')) == other  # only this slot's delivery
    out = codex_turn(f, plan(['worker-1']), users=[('', text)])  # Codex runs the queued item anyway
    assert out is False or out['status'] == 'ignored', out
    assert cq(f, 'cq_state') == 'abandoned' and lease(f)['status'] == 'received'
    assert typed(f, 'worker-1') == []
    assert_no_input_box(f)


def test_clear_while_the_turn_runs_never_reports_success(sup):
    """Consumption / cleanup race: the turn already started when the slot was cleared; its end must not be
    reported as a settled delivery (no commit, no 'bound', nothing dispatched)."""
    f = sup
    submit(f)
    text, turn = last(f)[1], str(uuid.uuid4())
    write_rollout(f.test_sessions, session(f, 'supervisor'), fleet.agent_cwd(f.cfg, 'supervisor'),
                  rows=turn_rows(turn, 'queue', [('', text)], '', complete=False))  # running
    codex_queue.clear(f, 'supervisor', 'cleanup while running')
    out = codex_turn(f, plan(['worker-1']), turn=turn, users=[('', text)])
    assert out is False or out['status'] == 'ignored', out
    assert cq(f, 'cq_state') == 'abandoned' and cq(f, 'cq_turn') == ''
    assert lease(f)['status'] == 'received' and typed(f, 'worker-1') == []


def test_clear_after_settlement_neither_unsettles_nor_settles_again(sup):
    f = sup
    submit(f)
    turn = str(uuid.uuid4())
    assert codex_turn(f, plan(['worker-1']), turn=turn)['status'] == 'committed'
    assert codex_queue.clear(f, 'supervisor', 'late cleanup') == {}
    assert cq(f, 'cq_state') == 'bound' and cq(f, 'cq_turn') == turn
    again = codex_turn(f, plan(['worker-2']), turn=turn)
    assert again is False or again['status'] == 'ignored', again


def test_finish_old_item_running_later_is_not_taken_as_a_new_delivery(sup):
    f = sup
    submit(f)
    old = codex_queue.record(f.r.hgetall(fleet.key('agent', 'supervisor')))
    f.v2.finish('stopped', 'operator stop')
    assert cq(f, 'cq_state') == 'abandoned'
    out = codex_turn(f, plan(['worker-1']), users=[('', old['cq_text'])])
    # Plan E-3: the held item's token turn completed: released, not settled (record stays abandoned).
    assert out is not False and out['status'] == 'released', out
    assert cq(f, 'cq_state') == 'abandoned'
    assert typed(f, 'worker-1') == [] and f.task()['status'] == 'stopped'
    # a later delivery of the same body gets its own token; the old item's turn never binds it
    new = codex_queue.deliver(f, 'supervisor', 'after-finish', _bare(old['cq_text']))
    assert new['token'] != old['cq_token']
    rec = codex_queue.record(f.r.hgetall(fleet.key('agent', 'supervisor')))
    thread, turn = session(f, 'supervisor'), str(uuid.uuid4())
    write_rollout(f.test_sessions, thread, fleet.agent_cwd(f.cfg, 'supervisor'),
                  rows=turn_rows(turn, 'queue', [('', old['cq_text'])], 'x'))
    assert codex_queue.bound(rec, thread, turn, f.test_sessions)[0] is False
    assert_no_input_box(f)


def test_retire_old_generation_item_is_isolated(sup):
    """Rotation: the retired session's pane is killed; if its item ever runs (manual resume of that thread),
    the notify carries the old generation and the abandoned token: it never settles."""
    f = sup
    submit(f)
    old = codex_queue.record(f.r.hgetall(fleet.key('agent', 'supervisor')))
    pane = f.agent('supervisor')['pane']
    assert f.v2.retire('supervisor', 'rotation') is True
    assert cq(f, 'cq_state') == 'abandoned' and pane in f.mgr.killed
    out = codex_turn(f, plan(['worker-1']), users=[('', old['cq_text'])], generation=0)
    assert out is False or out['status'] in ('ignored', 'stale'), out
    assert typed(f, 'worker-1') == []
    assert_no_input_box(f, pane)


def _report_in_flight(state, q):
    """task_run: a task split over two workers, worker-1 reported, its report is queued to the supervisor."""
    f = state['f']
    f.q = q
    runtime_v2.post_event(f.v2.bus, dict(kind='task', request_id='new-task', spec={
        'goal': 'write two parts', 'artifacts': [{'path': 'worker-1/a.md'}, {'path': 'worker-2/b.md'}]}), 'new-task')
    assert f.v2.step(10)
    f.pump()
    split = json.dumps({'status': 'RUNNING', 'completion': 10, 'reason': 'split', 'dispatch': [
        {'to': 'worker-1', 'instruction': 'Write worker-1/a.md with part A.'},
        {'to': 'worker-2', 'instruction': 'Write worker-2/b.md with part B.'}]})
    assert codex_turn(f, split)['status'] == 'committed'
    f.pump()
    (f.workspace() / 'worker-1').mkdir(exist_ok=True)
    (f.workspace() / 'worker-1' / 'a.md').write_text('part A')
    assert claude_turn(f, 'worker-1', REPORT.format('wrote worker-1/a.md', 'worker-1/a.md'))['status'] == 'committed'
    f.pump()
    assert lease(f)['status'] == 'received'
    return f


def test_resume_new_generation_gets_the_brief_once_and_old_notify_is_stale(task_run, monkeypatch):  # noqa: F811
    state = task_run
    q = install_queue(monkeypatch)
    f = _report_in_flight(state, q)
    old_pane, old_thread = f.agent('supervisor')['pane'], session(f, 'supervisor')
    f = crash_and_resume(state)
    f.q = q
    assert f.agent('supervisor')['generation'] != '0' and f.agent('supervisor')['pane'] != old_pane
    assert startup_ready(f)['status'] == 'ready'
    f.pump(); f.pump()
    assert len([t for t in queued_texts(f) if '[Resumed task ' in t]) == 1
    late = codex_turn(f, WAIT, thread=old_thread, generation=0, users=[('', 'x')])
    assert late is False or late['status'] == 'stale', late
    assert input_box_use(f.agent('supervisor')['pane'], f.mgr.sent) == [] and input_box_use(old_pane, f.mgr.sent) == []
    assert '[worker-1 report]' in queued_texts(f, thread=old_thread)[-1]  # nothing new for the old session


def test_resume_replays_once_with_a_new_token_and_the_old_item_never_settles(task_run, monkeypatch):  # noqa: F811
    """Crash while the report is queued (unsettled): --resume respawns the supervisor (new generation, new
    thread), the brief is queued once with a new token, the old item running in the old session settles
    nothing, and the new brief settles exactly once."""
    state = task_run
    q = install_queue(monkeypatch)
    f = _report_in_flight(state, q)
    old_thread, old_gen = session(f, 'supervisor'), int(f.agent('supervisor')['generation'])
    old = codex_queue.record(f.r.hgetall(fleet.key('agent', 'supervisor')))
    assert old['cq_state'] == 'queued' and '[worker-1 report]' in old['cq_text']
    f = crash_and_resume(state)
    f.q = q
    assert int(f.agent('supervisor')['generation']) != old_gen and session(f, 'supervisor') != old_thread
    assert startup_ready(f)['status'] == 'ready'
    f.pump(); f.pump()
    briefs = [t for t in queued_texts(f) if '[Resumed task ' in t]
    assert len(briefs) == 1 and cq(f, 'cq_token') != old['cq_token']
    assert queued_texts(f, thread=old_thread)[-1] == old['cq_text']  # nothing new was queued into the old thread
    assert cq(f, 'cq_thread') == session(f, 'supervisor') != old_thread
    late = codex_turn(f, WAIT, thread=old_thread, generation=old_gen, users=[('', old['cq_text'])])
    assert late is False or late['status'] in ('ignored', 'stale'), late
    turn = str(uuid.uuid4())
    assert codex_turn(f, WAIT, turn=turn, users=[('', briefs[0])])['status'] == 'committed'
    again = codex_turn(f, WAIT, turn=turn, users=[('', briefs[0])])
    assert again is False or again['status'] in ('ignored', 'stale'), again
    assert input_box_use(f.agent('supervisor')['pane'], f.mgr.sent) == []


def _resume_events(f, event):
    return [e for e in audit(f) if e.get('event') == event]


@pytest.mark.parametrize('ran', ['not-started', 'interrupted', 'completed'])
def test_resume_kills_the_old_codex_session_before_it_reads_the_token_turn(task_run, monkeypatch, ran):  # noqa: F811
    """start --resume with the supervisor's report still queued (unsettled): the old Codex session is killed
    first (a live TUI would run the old item next to the replay; abandoning does not withdraw it), then the
    old rollout decides by the delivery's token. Nothing new is ever queued into the old thread."""
    state = task_run
    q = install_queue(monkeypatch)
    f = _report_in_flight(state, q)
    old_pane, old_thread = f.agent('supervisor')['pane'], session(f, 'supervisor')
    old = codex_queue.record(f.r.hgetall(fleet.key('agent', 'supervisor')))
    if ran != 'not-started':
        rows = turn_rows(str(uuid.uuid4()), 'queue', [('', old['cq_text'])], WAIT, complete=ran == 'completed')
        write_rollout(f.test_sessions, old_thread, fleet.agent_cwd(f.cfg, 'supervisor'), rows=rows)
    f = crash_and_resume(state)
    f.q = q
    assert old_pane in state['mgr'].killed and f.agent('supervisor')['pane'] != old_pane
    event = {'not-started': 'resume.codex_not_started', 'interrupted': 'resume.codex_interrupted',
             'completed': 'resume.codex_ran'}[ran]
    got = _resume_events(f, event)
    assert len(got) == 1 and got[0]['delivery_id'] == old['cq_delivery'] and got[0]['slot'] == 'supervisor', audit(f)
    assert got[0]['replay'] == ('no' if ran == 'completed' else 'yes')
    assert queued_texts(f, thread=old_thread)[-1] == old['cq_text']  # nothing new into the old thread
    assert startup_ready(f)['status'] == 'ready'
    f.pump(); f.pump()
    assert len([t for t in queued_texts(f) if '[Resumed task ' in t]) == 1
    late = codex_turn(f, WAIT, thread=old_thread, generation=0, users=[('', old['cq_text'])])
    assert late is False or late['status'] in ('ignored', 'stale'), late
    assert_no_input_box(f)


def test_resume_never_replays_a_codex_worker_instruction_that_already_ran(task_run, monkeypatch):  # noqa: F811
    """A Codex worker's queued instruction ran to completion while the daemon was down (its token's turn is
    complete in the rollout, the report never settled). --resume must not re-queue it (it would run twice):
    the supervisor's brief lists it as already run instead; worker-2's not-started peer case is replayed."""
    state = task_run
    q = install_queue(monkeypatch)
    f = _report_in_flight(state, q)
    real = codex_queue.is_codex
    monkeypatch.setattr(codex_queue, 'is_codex', lambda cfg, name: name == 'worker-2' or real(cfg, name))
    lease2 = f.r.hgetall(f.v2.bus.lease_key('worker-2'))
    msg = f.v2.bus.message(lease2['message_id'])
    token = codex_queue.new_token()
    text = codex_queue.marked(msg['body']['text'], token)
    thread = session(f, 'worker-2')
    f.r.hset(fleet.key('agent', 'worker-2'), mapping={
        'cq_delivery': lease2['current_delivery'], 'cq_token': token, 'cq_text': text, 'cq_thread': thread,
        'cq_generation': f.agent('worker-2')['generation'], 'cq_queued_at': str(int(time.time() * 1000)),
        'cq_state': 'queued', 'cq_queued_id': 'q-1', 'cq_turn': ''})
    write_rollout(f.test_sessions, thread, fleet.agent_cwd(f.cfg, 'worker-2'),
                  rows=turn_rows(str(uuid.uuid4()), 'queue', [('', text)], 'Done: wrote worker-2/b.md'))
    old_pane = f.agent('worker-2')['pane']
    f = crash_and_resume(state)
    f.q = q
    assert old_pane in state['mgr'].killed
    got = _resume_events(f, 'resume.codex_ran')
    assert len(got) == 1 and got[0]['slot'] == 'worker-2' and got[0]['replay'] == 'no', audit(f)
    again = [f.v2.bus.message(m)['body']['text'] for m in f.v2.bus.pending('worker-2')]
    assert not [t for t in again if 'Write worker-2/b.md' in t], again  # never re-queued
    brief = [f.v2.bus.message(m)['body']['text'] for m in f.v2.bus.pending('supervisor')]
    brief = [t for t in brief if '[Resumed task ' in t]
    assert len(brief) == 1 and 'already ran to completion' in brief[0] and 'Write worker-2/b.md' in brief[0], brief


def test_finish_then_resume_does_not_replay_the_abandoned_delivery(task_run, monkeypatch):  # noqa: F811
    """abandoned is not cancelled: the stopped task's queued report may still run in Codex. A later --resume
    must not queue it (or a brief containing it) again, or the same instruction runs twice."""
    state = task_run
    q = install_queue(monkeypatch)
    f = _report_in_flight(state, q)
    f.v2.finish('stopped', 'operator stop')
    assert cq(f, 'cq_state') == 'abandoned'
    before = len(q.items)
    f = crash_and_resume(state)
    f.q = q
    startup_ready(f)
    f.pump(); f.pump()
    again = [i['text'] for i in q.items[before:] if '[worker-1 report]' in i['text'] or '[Resumed task ' in i['text']]
    assert again == [], 'the abandoned delivery was queued again after resume: ' + repr([t[:80] for t in again])


# ================================================================ G2. v1 Fleet.finish_rotation, driven directly
@pytest.mark.parametrize('timed_out', [False, True])
def test_v1_finish_rotation_abandons_the_old_delivery_and_its_late_turn_is_stale(fx, timed_out):  # noqa: F811
    """v1 rotation (fleet.py finish_rotation): the retiring session's queued handoff delivery is abandoned
    (logged), the old pane is killed, the successor's takeover prompt is argv (never queued into or typed into
    any pane), and a late turn of the old generation answering the old delivery is stale."""
    f = fx
    old_pane = f.agent('supervisor')['pane']
    f.start_rotation('supervisor', 90000, 100000)
    rec = codex_queue.record(f.agent('supervisor'))
    assert f.agent('supervisor')['state'] == 'handoff' and rec['cq_state'] == 'queued' and rec['cq_token']
    f.finish_rotation('supervisor', timed_out=timed_out)
    logs = [json.loads(x) for x in f.r.l['fx:log']]
    assert [l for l in logs if l.get('kind') == 'queue.abandoned' and l.get('delivery') == rec['cq_delivery']
            and l.get('token') == rec['cq_token']], 'the retiring delivery was not abandoned'
    new = f.agent('supervisor')
    assert new['generation'] == '1' and old_pane in f.mgr.killed and new['pane'] != old_pane
    assert codex_queue.record(new)['cq_token'] != rec['cq_token']
    f.handle(fleet_turn('supervisor', 'handoff written', generation=0))
    assert json.loads(f.r.l['fx:log'][-1])['kind'] == 'turn.stale'
    # fx uses the shared conftest queue fake, which logs *queued* text into FakeMgr.sent; real input-box use is
    # the independent paste / Enter log of this module.
    assert [p for p, _ in PASTE_CALLS if p in (old_pane, new['pane'])] == []
    assert [k for p, k in KEY_CALLS if p in (old_pane, new['pane']) and k in ENTER_KEYS] == []
    assert not [t for _, t in f.mgr.sent if '[Same-task takeover]' in str(t)]  # the takeover prompt is argv only


# ================================================================ H. subfleet adoption / restore (Codex pane)
def codex_hook(t, f, slot, text, *, users=None, trigger='queue', thread=None, generation=None):
    """A finished Codex turn in `slot` of fleet `f`, reported by that CLI's own notify argv."""
    gen = int(f.agent(slot)['generation']) if generation is None else generation
    thread = thread or t.threads.setdefault((str(f.run_dir), slot, gen), str(uuid.uuid4()))
    if users is None:
        item = [i for i in t.q.items if i['thread'] == thread][-1]
        users = [(item['real_client'], item['text'])]
    turn = str(uuid.uuid4())
    write_rollout(t.sessions, thread, fleet.agent_cwd(f.cfg, slot), rows=turn_rows(turn, trigger, users, text))
    payload = json.dumps({'type': 'agent-turn-complete', 'thread-id': thread, 'turn-id': turn,
                          'input-messages': [u[1] for u in users], 'last-assistant-message': text})
    return ts.drive(t, lambda: fleet.notify(f.path, slot, str(gen), payload, t.sessions))


@pytest.fixture
def ctree(tmp_path, monkeypatch):
    """test_subfleet.tree with Codex workers (the adoptable pane is a Codex session). Reuses its
    World/helpers by import; tests/test_subfleet.py is not modified. Codex workers run v2 only as
    `--lifecycle smoke` (cli_pairs), so the parent uses it."""
    if shutil.which('redis-server') is None:
        pytest.skip('redis-server unavailable')
    root = Path('/tmp/cfq-' + uuid.uuid4().hex[:8])
    monkeypatch.setattr(fleet, 'trust_claude_dir', lambda cwd: None)
    world = ts.World(tmp_path / 'cmux-state')
    monkeypatch.setattr(fleet, 'manager_for', world.manager)
    rcfg, r = ts.start_private(root)
    run = tmp_path / 'fleet-20261013-000000-c0dec0de'
    (run / 'workspace').mkdir(parents=True)
    cfg = dict(rcfg, run=str(run), python=sys.executable, workers=['worker-1', 'worker-2'], mode='task',
               supervisor_cli='codex', worker_cli='codex', codex_home=str(tmp_path / 'codex-home'),
               allow_development_v2=True, stall_timeout=0, v2_lease_ms=300000, max_busy=0, max_attempts=2,
               name='研报', token='c0dec0de', codex=str(tmp_path / 'no-real-codex'), lifecycle='smoke')
    fleet.save(run / 'config.json', cfg)
    t = SimpleNamespace(world=world, root=root, sessions=tmp_path / 'codex-home' / 'sessions',
                        claude=tmp_path / 'claude-projects', threads={}, fleets=[], tmp=tmp_path)
    t.q = install_queue(monkeypatch)
    f = fleet.Fleet(run / 'config.json', r, world.manager(cfg), sessions=t.sessions)
    t.f = f
    t.fleets.append(f)
    f.start_all()
    f.r.set(f.v2.prefix + ':boot', 'running')
    for slot in f.names:
        assert codex_hook(t, f, slot, 'READY', trigger='user', users=[('', 'startup')])['status'] == 'ready'
    runtime_v2.post_event(f.v2.bus, dict(kind='task', request_id='new-task', spec={'goal': 'write the report'}), 'new-task')
    ts.step(t)
    yield t
    for x in t.fleets:
        try:
            x.v2.close()
        except Exception:
            pass
    for pid_file in root.glob('*/redis.pid'):
        try:
            os.kill(int(pid_file.read_text()), signal.SIGTERM)
        except (ProcessLookupError, ValueError):
            pass
    time.sleep(.2)
    shutil.rmtree(root, ignore_errors=True)


def lift_codex_adoption_guards(monkeypatch):
    """Two product guards refuse a Codex adopted pane today: subfleet.check (Claude panes only) and
    cli_pairs (a Codex+Codex child fleet runs v2 only as --lifecycle smoke). Lift both on purpose
    so worker-1's Codex adopt/restore branches (plan §3.2) are exercised."""
    monkeypatch.setattr(subfleet, 'check', lambda *a, **k: None)
    real = subfleet.child_config
    monkeypatch.setattr(subfleet, 'child_config', lambda *a, **k: dict(real(*a, **k), lifecycle='smoke'))


def test_subfleet_refuses_a_codex_worker_pane_before_queueing_or_typing(ctree):
    """Reachable product behaviour: only Claude worker panes can be adopted (subfleet.check)."""
    t, f = ctree, ctree.f
    sid = f.agent('worker-1')['pane']
    n = len(t.q.items)
    with pytest.raises(subfleet.Refused, match='only official Claude Code worker panes'):
        ts.spawn_m0(t)
    assert len(t.q.items) == n
    assert input_box_use(sid, t.world.sent) == []
    assert f.agent('worker-1')['pane'] == sid and f.agent('worker-1')['state'] != 'delegated'


def test_subfleet_adoption_and_restore_of_a_codex_pane_are_queued_never_typed(ctree, monkeypatch):
    """UNREACHABLE-BRANCH DEFENSIVE COVERAGE. The product refuses to adopt a Codex pane
    (`test_subfleet_refuses_a_codex_worker_pane_before_queueing_or_typing`, and that refusal stays).
    This test lifts those guards on purpose, only so the Codex branch of adopt/restore cannot regress
    into typing if it is ever enabled. It does not open, approve or document a new capability."""
    t, f = ctree, ctree.f
    lift_codex_adoption_guards(monkeypatch)
    sid = f.agent('worker-1')['pane']
    parent_thread = t.threads[(str(f.run_dir), 'worker-1', 0)]
    rec, out = ts.spawn_m0(t)
    cf = ts.child_daemon(t, rec)
    assert cf.agent('supervisor')['pane'] == sid
    role = [i for i in t.q.items if i['thread'] == parent_thread and 'sub-fleet M0' in i['text']]
    assert len(role) == 1 and 'READY' in role[0]['text']
    assert input_box_use(sid, t.world.sent) == []
    # its READY turn is reported by the adopted CLI's own (parent worker-1) notify and lands in the child
    assert codex_hook(t, f, 'worker-1', 'READY', thread=parent_thread)['status'] == 'ready'
    assert cf.agent('supervisor')['runtime_ready'] == '1'
    for w in cf.workers:
        assert codex_hook(t, cf, w, 'READY', trigger='user', users=[('', 'startup')])['status'] == 'ready'
    ts.step(t)
    task = [i for i in t.q.items if i['thread'] == parent_thread][-1]
    assert 'Research five sources and merge them' in task['text']
    assert input_box_use(sid, t.world.sent) == []
    inside = Path(rec['run']) / 'workspace' / 'worker-1' / 'm0.md'
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text('merged findings')
    cf.v2.finish('completed', 'all parts merged')
    cf.write_result()
    subfleet.tick(f)
    ts.stop_child_like_daemon(cf)
    subfleet.tick(f)
    assert subfleet.record(rec['run'])['state'] == 'returned'
    back = [i for i in t.q.items if i['thread'] == parent_thread and 'worker of the main fleet again' in i['text']]
    assert len(back) == 1
    assert input_box_use(sid, t.world.sent) == []
    assert codex_hook(t, f, 'worker-1', 'READY', thread=parent_thread)['status'] == 'ready'
    assert f.agent('worker-1')['state'] == 'idle' and f.agent('worker-1')['generation'] == '0'


def test_subfleet_adoption_queue_failure_blocks_without_typing(ctree, monkeypatch):
    """UNREACHABLE-BRANCH DEFENSIVE COVERAGE. The product refuses to adopt a Codex pane
    (`test_subfleet_refuses_a_codex_worker_pane_before_queueing_or_typing`, and that refusal stays).
    This test lifts those guards on purpose, only so the Codex branch of adopt/restore cannot regress
    into typing if it is ever enabled. It does not open, approve or document a new capability."""
    t, f = ctree, ctree.f
    lift_codex_adoption_guards(monkeypatch)
    sid = f.agent('worker-1')['pane']
    rec, out = ts.spawn_m0(t)
    t.q.fail = 'codex queue rc=1: simulated'
    cf = ts.child_daemon(t, rec)
    assert input_box_use(sid, t.world.sent) == []
    assert cf.agent('supervisor')['state'] == 'dead'


# ================================================================ I. identity gap: a same-body queue turn that is not this delivery
# Both turns are queue-triggered and each carries exactly one input with the same business body (not a manual
# turn, not a merged turn). The older one starts after this delivery's record was written, and no client_id is
# readable. The expectation is this delivery's identity, never "the first same-body queue turn after queueing".
def _bare(text):
    return text.rsplit('\n\n(fleet delivery ', 1)[0]


def _queue_turn_notify(f, thread, rows, gen):
    """`gen` is read before the hook thread: the test Redis client has one connection."""
    turn = rows[0]['payload']['turn_id']
    payload = json.dumps({'type': 'agent-turn-complete', 'thread-id': thread, 'turn-id': turn,
                          'input-messages': ['startup prompt'], 'last-assistant-message': rows[-1]['payload']['last_agent_message']})
    return fleet.notify(f.path, 'supervisor', gen, payload, f.test_sessions)


@pytest.mark.parametrize('other', ['older-backlog-external', 'older-backlog-fleet-token', 'concurrent-external-enqueue'])
def test_same_body_queue_turn_of_another_message_never_settles_this_delivery(sup, other):
    f = sup
    f.q.readable = False
    box = {}

    def inside_the_cli_call(item):
        box['state'] = codex_queue._real(f.r).hget(fleet.key('agent', 'supervisor'), 'cq_state')  # read during the CLI call
        text = _bare(item['text']) if other != 'older-backlog-fleet-token' else codex_queue.marked(_bare(item['text']), codex_queue.new_token())
        box['rows'] = turn_rows(str(uuid.uuid4()), 'queue', [('', text)], plan(['worker-2']), int(time.time() * 1000))
        box['other_text'] = text
        if other != 'concurrent-external-enqueue':
            # the older backlog item starts (after the record) and completes while the CLI still runs
            write_rollout(f.test_sessions, item['thread'], fleet.agent_cwd(f.cfg, 'supervisor'), rows=box['rows'])
        # (concurrent external enqueue: queued by someone else right now; its turn runs after the CLI returned)
    f.q.hook = inside_the_cli_call
    submit(f)
    f.q.hook = None
    thread = session(f, 'supervisor')
    assert box['state'] == 'queuing'  # this delivery's record existed before `codex queue` ran
    assert _bare(box['other_text']) == _bare(last(f)[1])  # identical business body; only each one's own envelope differs
    assert int(cq(f, 'cq_queued_at')) <= int(datetime.datetime.fromisoformat(
        box['rows'][0]['timestamp'].replace('Z', '+00:00')).timestamp() * 1000)  # it started after the record
    write_rollout(f.test_sessions, thread, fleet.agent_cwd(f.cfg, 'supervisor'), rows=box['rows'])
    gen = str(f.agent('supervisor')['generation'])
    out = drive(f, lambda: _queue_turn_notify(f, thread, box['rows'], gen))
    assert out is False or out['status'] in ('ignored', 'stale'), out
    assert cq(f, 'cq_state') == 'queued' and lease(f)['status'] == 'received'
    assert typed(f, 'worker-1') == [] and typed(f, 'worker-2') == []
    # positive: this delivery's own queue turn (no client_id) then settles, exactly once
    turn = str(uuid.uuid4())
    out = codex_turn(f, plan(['worker-1']), turn=turn, users=[('', last(f)[1])], before=box['rows'])
    assert out['status'] == 'committed', out
    assert cq(f, 'cq_state') == 'bound' and cq(f, 'cq_turn') == turn
    f.pump()
    assert typed(f, 'worker-1') and typed(f, 'worker-2') == []
    assert_no_input_box(f)


# ================================================================ J. same-body redelivery, CLI timeout, awkward bodies (argv + binding)
@pytest.mark.parametrize('state', ['queuing', 'queued', 'uncertain'])
def test_deliver_refuses_to_queue_behind_an_unsettled_item_in_the_same_thread(sup, state):
    """Abandoning never withdraws an item from a live Codex queue (no official cancel), so the older item
    would still run. A second delivery while one may still run is refused (the caller holds); nothing is
    queued and the live record keeps its token. (Was: the older token was superseded and both items ran.)"""
    f = sup
    f.q.readable = False
    submit(f)
    f.r.hset(fleet.key('agent', 'supervisor'), 'cq_state', state)
    old = codex_queue.record(f.r.hgetall(fleet.key('agent', 'supervisor')))
    before = len(f.q.items)
    with pytest.raises(codex_queue.QueueError, match='still unsettled'):
        codex_queue.deliver(f, 'supervisor', 'second-' + uuid.uuid4().hex[:6], _bare(old['cq_text']))
    assert len(f.q.items) == before
    assert codex_queue.record(f.r.hgetall(fleet.key('agent', 'supervisor'))) == old
    assert_no_input_box(f)


@pytest.mark.parametrize('state', ['failed', 'abandoned', 'bound'])
def test_deliver_after_a_settled_or_never_queued_item_queues_with_a_new_token(sup, state):
    """failed (the CLI said nothing was queued), abandoned or bound: a new delivery is queued with its own
    token, and a turn carrying the older token never settles it."""
    f = sup
    f.q.readable = False
    submit(f)
    f.r.hset(fleet.key('agent', 'supervisor'), 'cq_state', state)
    old = codex_queue.record(f.r.hgetall(fleet.key('agent', 'supervisor')))
    got = codex_queue.deliver(f, 'supervisor', old['cq_delivery'], _bare(old['cq_text']))
    assert got['token'] != old['cq_token'] and _bare(got['text']) == _bare(old['cq_text'])
    assert cq(f, 'cq_state') == 'queued' and cq(f, 'cq_token') == got['token']
    out = codex_turn(f, plan(['worker-1']), users=[('', old['cq_text'])])
    assert out is False or out['status'] == 'ignored', out
    assert lease(f)['status'] == 'received'
    assert codex_turn(f, plan(['worker-1']), users=[('', got['text'])])['status'] == 'committed'
    assert cq(f, 'cq_state') == 'bound'


class TimeoutQueue(Queue):
    """`codex queue` timed out after the item had in fact been queued (the 'uncertain' case)."""

    def enqueue(self, cfg, name, thread, text, **kw):
        Queue.enqueue(self, cfg, name, thread, text, **kw)
        raise codex_queue.QueueError('uncertain: codex queue gave no answer: timed out after 30 seconds')


def test_cli_timeout_is_uncertain_held_and_after_clear_its_late_turn_never_settles(sup, monkeypatch):
    f = sup
    f.q = install_queue(monkeypatch, TimeoutQueue())
    submit(f)
    assert cq(f, 'cq_state') == 'uncertain'
    text = f.q.items[-1]['text']
    assert len(f.q.items) == 1  # never retried
    f.v2.finish('stopped', 'operator stop')
    assert cq(f, 'cq_state') == 'abandoned'
    out = codex_turn(f, plan(['worker-1']), users=[('', text)])  # the item did run after all
    # Plan E-3: its token turn completed, so the held slot is released (result NOT applied, record stays abandoned).
    assert out is not False and out['status'] == 'released', out
    assert cq(f, 'cq_state') == 'abandoned' and lease(f)['status'] == 'idle'
    assert typed(f, 'worker-1') == []
    assert_no_input_box(f)



def test_cli_timeout_then_the_item_runs_releases_the_hold_without_committing_its_result(sup, monkeypatch):
    """Timeout race: `codex queue` gave no answer (uncertain, slot held) but the item did run. Its token turn
    proves that and lifts the hold (I3 hold exit); the result is never committed and nothing is dispatched."""
    f = sup
    f.q = install_queue(monkeypatch, TimeoutQueue())
    submit(f)
    assert cq(f, 'cq_state') == 'uncertain' and f.agent('supervisor')['state'] == 'held'
    assert lease(f)['status'] == 'uncertain'
    out = codex_turn(f, plan(['worker-1']), users=[('', f.q.items[-1]['text'])])
    assert out is not False and out['status'] == 'released' and out.get('execution_stopped') is True, out
    f.pump()
    assert cq(f, 'cq_state') == 'bound' and lease(f)['status'] == 'idle'
    assert typed(f, 'worker-1') == [] and len(f.q.items) == 1  # not committed, not dispatched, never retried
    assert_no_input_box(f)


def test_held_codex_delivery_is_released_only_when_its_token_turn_completes(sup, monkeypatch):
    """Plan E-3: Codex has no withdraw, so another turn ending proves nothing while the held item may still run
    from its queue. Only the rollout's task_complete of the token's own turn releases; afterwards the record
    no longer blocks the next deliver."""
    f = sup
    f.q = install_queue(monkeypatch, TimeoutQueue())
    submit(f)
    rec = codex_queue.record(f.agent('supervisor'))
    assert f.agent('supervisor')['state'] == 'held' and rec['cq_state'] == 'uncertain'
    out = codex_turn(f, 'just chatting', trigger='user', users=[('', 'operator line')], inputs=['operator line'])
    assert out['status'] == 'ignored' and 'has not started' in out['reason'], out
    token_turn = str(uuid.uuid4())
    running = turn_rows(token_turn, 'queue', [('', rec['cq_text'])], '', complete=False)
    out = codex_turn(f, 'still chatting', trigger='user', users=[('', 'operator line 2')], inputs=['x'], before=running)
    assert out['status'] == 'ignored' and 'still running' in out['reason'], out
    assert f.agent('supervisor')['state'] == 'held' and lease(f)['status'] == 'uncertain'
    out = codex_turn(f, plan(['worker-1']), turn=token_turn, users=[('', rec['cq_text'])])
    assert out['status'] == 'released', out
    assert f.agent('supervisor')['state'] == 'idle' and lease(f)['status'] == 'idle'
    assert not codex_queue.unsettled(f.agent('supervisor')) and typed(f, 'worker-1') == []
    f.q = install_queue(monkeypatch)
    assert codex_queue.deliver(f, 'supervisor', 'after-release', 'next message')['token'] != rec['cq_token']
    assert_no_input_box(f)


def test_withdrawal_during_the_cli_call_is_not_revived_by_the_late_cli_answer(sup):
    """Consumption/cleanup race inside `codex queue`: the TUI took the item and the slot was cleared before the
    CLI returned. The CLI's late success must not turn 'abandoned' back into 'queued' (compare-and-set); the
    item's turn settles nothing."""
    f = sup
    f.q.hook = lambda item: codex_queue.clear(f, 'supervisor', 'cleared while the CLI ran')
    submit(f)
    f.q.hook = None
    assert cq(f, 'cq_state') == 'abandoned'
    out = codex_turn(f, plan(['worker-1']))
    assert out is False or out['status'] in ('ignored', 'stale'), out
    assert typed(f, 'worker-1') == [] and lease(f)['status'] == 'received'
    assert_no_input_box(f)


@pytest.mark.parametrize('content', ['unknown-schema', 'not-a-database'])
def test_codex_private_queue_db_is_never_read_or_written(sup, content):
    """Plan v3 I6: Codex's queue_1.sqlite is not ours. Whatever it holds (another schema after an upgrade, or not
    a database at all), delivery, settlement, clear and finish work the same and the file is byte-identical."""
    import sqlite3
    f = sup
    path = fleet.codex_home(f.runtime_cfg('supervisor')) / 'queue_1.sqlite'
    path.parent.mkdir(parents=True, exist_ok=True)
    if content == 'unknown-schema':
        con = sqlite3.connect(str(path))
        con.execute('CREATE TABLE queued_items_v2 (uid BLOB PRIMARY KEY, data BLOB)')
        con.execute("INSERT INTO queued_items_v2 VALUES (x'00', x'ff')")
        con.commit()
        con.close()
    else:
        path.write_bytes(b'not a sqlite database\x00\xff')
    before, mtime = path.read_bytes(), path.stat().st_mtime_ns
    submit(f)
    assert codex_turn(f, plan(['worker-1']))['status'] == 'committed'
    f.pump()
    codex_queue.clear(f, 'supervisor', 'test')
    f.v2.finish('stopped', 'operator stop')
    assert path.read_bytes() == before and path.stat().st_mtime_ns == mtime
    assert_no_input_box(f)

AWKWARD = {
    'hyphen': '- write worker-1/part.md with the findings',
    'double-hyphen': '--help is not an option here; write the file',
    'multiline': 'Write worker-1/part.md:\n  1. the totals\r\n  2. the gaps\n\n- and a bullet line',
    'trailing-newlines': 'Write worker-1/part.md with the findings.\n\n\n',
    'hyphen-multiline': '-n 3\nsecond line\n',
}


def message_value(argv):
    """The --message value as a clap parser without allow_hyphen_values sees it: `--message=<v>` is always
    the value; `--message <v>` is the value only if <v> does not look like a flag."""
    glued = [a for a in argv if a.startswith('--message=')]
    if glued:
        assert len(glued) == 1 and '--message' not in argv
        return glued[0][len('--message='):]
    value = argv[argv.index('--message') + 1]
    assert not value.startswith('-'), 'clap reads %r as a flag, not as the --message value' % value[:20]
    return value


@pytest.mark.parametrize('kind', sorted(AWKWARD))
def test_enqueue_argv_carries_the_exact_marked_text_as_one_unambiguous_value(tmp_path, monkeypatch, kind):
    thread, token = str(uuid.uuid4()), codex_queue.new_token()
    text = codex_queue.marked(AWKWARD[kind], token)
    seen = {}

    def run(argv, **kw):
        seen['argv'] = argv
        return SimpleNamespace(returncode=0, stdout='Queued message q-1 for thread %s.\n' % thread, stderr='')
    cfg = {'codex': str(tmp_path / 'codex'), 'codex_home': str(tmp_path / 'home'), 'run': str(tmp_path / 'run')}
    monkeypatch.setattr(fleet, 'agent_cwd', lambda cfg, name: tmp_path)
    assert ORIGINAL_ENQUEUE(cfg, 'supervisor', thread, text, run=run)['queued_id'] == 'q-1'
    argv = seen['argv']
    assert argv[:2] == [cfg['codex'], 'queue'] and argv[argv.index('--thread') + 1] == thread
    assert message_value(argv) == text  # byte-exact: newlines, CR, indentation, leading '-' preserved
    assert text.endswith('\n\n(fleet delivery ' + token + ')') and text.startswith(AWKWARD[kind].rstrip('\n'))


@pytest.mark.parametrize('kind', sorted(AWKWARD))
def test_awkward_body_delivery_binds_its_own_exact_text_only(sup, kind):
    """The queue turn carrying exactly the queued text binds; the same body without its token, or with
    whitespace normalised, does not (fails closed, never a wrong settle)."""
    f = sup
    got = codex_queue.deliver(f, 'supervisor', 'awkward-' + kind, AWKWARD[kind])
    assert queued(f)[-1]['text'] == got['text'] == cq(f, 'cq_text')
    rec = codex_queue.record(f.r.hgetall(fleet.key('agent', 'supervisor')))
    thread, cwd = session(f, 'supervisor'), fleet.agent_cwd(f.cfg, 'supervisor')
    t0 = int(time.time() * 1000)
    variants = {'exact': got['text'], 'no-token': AWKWARD[kind],
                'normalised': '\n'.join(l.strip() for l in got['text'].replace('\r\n', '\n').split('\n')).strip()}
    rows = []
    for i, (name, text) in enumerate(variants.items()):
        rows += turn_rows('T-' + name, 'queue', [('', text)], 'a', t0 + 100 * i)
    write_rollout(f.test_sessions, thread, cwd, rows=rows)
    assert codex_queue.bound(rec, thread, 'T-exact', f.test_sessions) == (True, '')
    assert codex_queue.bound(rec, thread, 'T-no-token', f.test_sessions)[0] is False
    if variants['normalised'] != variants['exact']:
        assert codex_queue.bound(rec, thread, 'T-normalised', f.test_sessions)[0] is False
    assert_no_input_box(f)


# ================================================================ K. generation binding (worker-1, moved verbatim from
# tests/test_codex_generation_binding.py d975d35e): a queued delivery settles only on a turn of the generation it was
# queued for; the record carries cq_thread, cq_generation and cq_token before `codex queue` runs.
def test_record_is_durable_before_the_cli_and_names_the_generation_and_token(sup, monkeypatch):
    f = sup
    seen = {}
    real = codex_queue.enqueue

    def spy(cfg, name, thread, text, **kw):
        seen.update(codex_queue.record(f.r.hgetall(fleet.key('agent', name))))  # what Redis holds while the CLI runs
        return real(cfg, name, thread, text, **kw)
    monkeypatch.setattr(codex_queue, 'enqueue', spy)
    submit(f)
    gen = f.agent('supervisor')['generation']
    assert seen['cq_state'] == 'queuing' and seen['cq_generation'] == str(gen) and seen['cq_token'].startswith('fx-')
    assert seen['cq_thread'] == f.agent('supervisor')['thread']
    assert last(f)[1] == seen['cq_text'] and seen['cq_text'].endswith('(fleet delivery ' + seen['cq_token'] + ')')
    out = codex_turn(f, plan(['worker-1']))
    assert out['status'] == 'committed', out
    assert cq(f, 'cq_state') == 'bound' and cq(f, 'cq_generation') == str(gen)
    assert_no_input_box(f)


def test_a_record_from_another_generation_never_settles_even_with_its_exact_token(sup):
    f = sup
    submit(f)
    gen = int(f.agent('supervisor')['generation'])
    f.r.hset(fleet.key('agent', 'supervisor'), 'cq_generation', str(gen + 1))  # queued for another runtime
    out = codex_turn(f, plan(['worker-1']))  # the exact token, thread and a queue-triggered turn
    assert out is False or out['status'] in ('ignored', 'stale'), out
    assert cq(f, 'cq_state') != 'bound' and lease(f)['status'] == 'received'
    assert typed(f, 'worker-1') == []
    assert_no_input_box(f)


def test_bound_checks_thread_generation_token_and_turn(tmp_path, monkeypatch):
    thread, turn = str(uuid.uuid4()), str(uuid.uuid4())
    token = codex_queue.new_token()
    rec = {'cq_delivery': 'd-1', 'cq_token': token, 'cq_text': codex_queue.marked('do it', token), 'cq_thread': thread,
           'cq_generation': '3', 'cq_queued_at': '1', 'cq_state': 'queued', 'cq_queued_id': 'q', 'cq_turn': ''}
    facts = {'trigger': 'queue', 'users': [{'text': rec['cq_text']}]}
    monkeypatch.setattr(codex_queue, 'turn_record', lambda th, tu, s: facts if (th, tu) == (thread, turn) else None)
    assert codex_queue.bound(rec, thread, turn, tmp_path, 3) == (True, '')
    assert codex_queue.bound(rec, thread, turn, tmp_path, '3') == (True, '')
    assert not codex_queue.bound(rec, thread, turn, tmp_path, 4)[0]
    assert not codex_queue.bound(dict(rec, cq_generation=''), thread, turn, tmp_path, 3)[0]  # unknown: fail closed
    assert not codex_queue.bound(rec, str(uuid.uuid4()), turn, tmp_path, 3)[0]
    assert not codex_queue.bound(rec, thread, str(uuid.uuid4()), tmp_path, 3)[0]
    assert not codex_queue.bound(dict(rec, cq_turn='other'), thread, turn, tmp_path, 3)[0]
    other = codex_queue.new_token()
    assert not codex_queue.bound(dict(rec, cq_token=other), thread, turn, tmp_path, 3)[0]
    facts['users'] = [{'text': 'do it'}]  # same body without this delivery's token
    assert not codex_queue.bound(rec, thread, turn, tmp_path, 3)[0]


# ---------------------------------------------------------------- L. F1: no client_id anywhere; timeout identity
def test_f1_without_any_client_id_consecutive_deliveries_complete_normally_by_their_tokens(sup):
    """F1 positive, end to end: the rollout never carries a client_id. The task delivery settles on its own
    token's turn and dispatches the worker; the worker's report reaches the supervisor as a new delivery with a
    new token, and that one settles too. Each settles exactly once; nothing is typed."""
    f = sup
    f.q.readable = False
    submit(f)
    first_token, first_text = cq(f, 'cq_token'), last(f)[1]
    assert first_text.endswith('(fleet delivery ' + first_token + ')')
    assert codex_turn(f, plan(['worker-1']), users=[('', first_text)])['status'] == 'committed'
    f.pump()
    assert typed(f, 'worker-1'), 'worker-1 was dispatched'
    assert claude_turn(f, 'worker-1', REPORT.format('wrote worker-1', 'worker-1/part.md'))['status'] == 'committed'
    f.pump()
    second_token, second_text = cq(f, 'cq_token'), last(f)[1]
    assert second_token != first_token and '[worker-1 report]' in second_text
    assert cq(f, 'cq_state') in ('queuing', 'queued')
    turn = str(uuid.uuid4())
    assert codex_turn(f, WAIT, turn=turn, users=[('', second_text)])['status'] == 'committed'
    assert cq(f, 'cq_state') == 'bound' and cq(f, 'cq_turn') == turn
    again = codex_turn(f, WAIT, turn=turn, users=[('', second_text)])
    assert again is False or again['status'] == 'ignored', again
    assert_no_input_box(f)


def test_cli_timeout_withdraws_no_pending_item_and_its_late_turn_never_settles(sup, monkeypatch):
    """`codex queue` times out after it really queued the item. The delivery is `uncertain` and held; nothing
    in Codex's queue_1.sqlite is deleted or changed: not this thread's own item, not a same-body item of this
    thread or another thread (their ownership is unprovable). After clear + finish the rows are the same, and
    the item's late turn, carrying this delivery's token, settles nothing. Nothing is pasted."""
    import sqlite3
    f = sup
    db = fleet.codex_home(f.runtime_cfg('supervisor')) / 'queue_1.sqlite'
    db.parent.mkdir(parents=True, exist_ok=True)
    other = str(uuid.uuid4())

    def put(con, th, text, order):
        con.execute('INSERT INTO queued_items VALUES (?, ?, ?, ?, 1, 1)', (str(uuid.uuid4()), th, json.dumps(
            {'UserInput': {'content': [{'type': 'text', 'text': text, 'text_elements': []}], 'client_id': ''}}), order))

    def timed_out(cfg, name, th, text, **kw):
        body = text.rsplit('\n\n(fleet delivery ', 1)[0]
        con = sqlite3.connect(str(db))
        con.executescript(QUEUE_SCHEMA)
        put(con, th, body, 0)     # an older same-body message of another sender on this thread
        put(con, th, text, 1)     # this delivery's own item: really queued, but the CLI never answered
        put(con, other, body, 0)  # same body, another thread
        con.commit()
        con.close()
        raise codex_queue.QueueError('uncertain: codex queue gave no answer: timed out after 30 seconds')
    monkeypatch.setattr(codex_queue, 'enqueue', timed_out)

    def rows():
        con = sqlite3.connect('file:' + str(db) + '?mode=ro', uri=True)
        try:
            return con.execute('SELECT id, thread_id, payload_json, queue_order FROM queued_items ORDER BY id').fetchall()
        finally:
            con.close()
    submit(f)
    snap = rows()
    assert len(snap) == 3
    assert f.agent('supervisor')['state'] == 'held' and f.paused()
    rec = codex_queue.record(f.agent('supervisor'))
    assert rec['cq_state'] == 'uncertain' and rec['cq_token'] and rec['cq_generation'] == f.agent('supervisor')['generation']
    assert any(e.get('event') == 'queue.uncertain' for e in audit(f))
    assert codex_queue.clear(f, 'supervisor', 'operator')['cq_state'] == 'uncertain'
    assert cq(f, 'cq_state') == 'abandoned'
    assert rows() == snap  # clear withdraws nothing from Codex's queue
    late = codex_turn(f, plan(['worker-1']), users=[('', rec['cq_text'])])  # the item runs after all
    # Plan E-3: its token turn completed: the hold is released, nothing settled (record stays abandoned).
    assert late is not False and late['status'] == 'released', late
    assert cq(f, 'cq_state') == 'abandoned'
    assert typed(f, 'worker-1') == []
    f.v2.finish('stopped', 'operator stop')
    assert rows() == snap
    assert_no_input_box(f)


# ---------------------------------------------------------------- M. real e2e: candidate.invalid correction (worker-3 runs A2/A3/M11)
REAL_E2E_ERROR = 'no worker is busy: RUNNING must dispatch at least one instruction'


def test_candidate_invalid_correction_names_the_real_error_and_settles_and_dispatches_once(sup):
    """Real e2e (Codex supervisor; worker-3 audit.jsonl of runs A2/A3/M11 and rehearsal w3e2e-fa): the supervisor
    answers a worker report with RUNNING + no dispatch ("waiting for worker-2's report").
    - While worker-2's report is still queued for the supervisor, that decision is valid: another supervisor
      turn is guaranteed (Fleet.others_active), so nothing is rejected, nothing is dispatched, no strike.
    - Once both reports have been delivered (no worker busy, nothing queued for the supervisor), the same decision
      is rejected by the v2 validator with the error above, and:
      the rejected turn settles its own delivery exactly once (a repeat notify is ignored, no second strike);
      nothing is dispatched for it; the correction is queued (never typed) and names the validator's real error,
      not '(substring not found)'; the next valid decision dispatches exactly once, with no duplicate round."""
    f = sup
    submit(f)
    assert codex_turn(f, plan(['worker-1', 'worker-2']))['status'] == 'committed'
    f.pump()
    for w in ('worker-1', 'worker-2'):
        (f.workspace() / w).mkdir(exist_ok=True)
        (f.workspace() / w / 'part.md').write_text('part of ' + w)
        assert claude_turn(f, w, REPORT.format('wrote ' + w + '/part.md', w + '/part.md'))['status'] == 'committed'
        f.pump()
    assert '[worker-1 report]' in last(f)[1] and len(typed(f, 'worker-1')) == len(typed(f, 'worker-2')) == 1
    waiting = json.dumps({'status': 'RUNNING', 'completion': 50, 'reason': "waiting for worker-2's report", 'dispatch': []})
    # 1) worker-2's report is still queued for the supervisor: "waiting" is a valid decision
    assert codex_turn(f, waiting)['status'] == 'committed'
    assert not [e for e in audit(f) if e.get('event') == 'candidate.invalid']
    assert f.task()['bad_replies'] == '0'
    f.pump(); f.pump()
    assert '[worker-2 report]' in last(f)[1]  # FIFO: the queued report is delivered next, not lost
    assert len(typed(f, 'worker-1')) == len(typed(f, 'worker-2')) == 1
    # 2) both reports delivered, nobody busy, nothing queued for the supervisor: the same decision is invalid
    turn, text = str(uuid.uuid4()), last(f)[1]
    out = codex_turn(f, waiting, turn=turn)
    assert out['status'] == 'committed', out  # the delivery is settled; the decision itself is rejected
    invalid = [e for e in audit(f) if e.get('event') == 'candidate.invalid' and e.get('slot') == 'supervisor']
    assert [e.get('error') for e in invalid] == [REAL_E2E_ERROR]
    assert f.task()['status'] == 'running' and f.task()['bad_replies'] == '1'
    again = codex_turn(f, waiting, turn=turn, users=[('', text)])  # the same turn notified twice
    assert again is False or again['status'] in ('ignored', 'stale'), again
    assert len([e for e in audit(f) if e.get('event') == 'candidate.invalid']) == 1 and f.task()['bad_replies'] == '1'
    f.pump(); f.pump()
    assert len(typed(f, 'worker-1')) == len(typed(f, 'worker-2')) == 1  # nothing dispatched for the rejected reply
    correction = last(f)[1]
    assert REAL_E2E_ERROR in correction and 'substring not found' not in correction
    assert correction.startswith('Your reply is not a valid decision JSON')
    assert codex_turn(f, plan(['worker-1'], 'round 2'))['status'] == 'committed'  # the valid answer to the correction
    f.pump(); f.pump()
    texts = queued_texts(f)
    assert [sum(m in t for t in texts) for m in ('[worker-1 report]', '[worker-2 report]', REAL_E2E_ERROR)] == [1, 1, 1]
    assert len(typed(f, 'worker-1')) == 2 and len(typed(f, 'worker-2')) == 1  # round 1 + round 2 only
    assert f.task()['status'] == 'running' and f.task()['bad_replies'] == '0'
    assert_no_input_box(f)
