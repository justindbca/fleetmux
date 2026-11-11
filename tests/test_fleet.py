"""Fleet daemon tests: in-memory Redis, fake cmux manager, fake Codex rollouts.
No live model, no cmux, no redis-server."""
import datetime
import json
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fleetmux import claude_hook, fleet, handoff  # noqa: E402
import hook_sim  # noqa: E402

THREADS = {'supervisor': '11111111-1111-1111-1111-111111111111',
           'worker-1': '22222222-2222-2222-2222-222222222222'}


class FakeRedis:
    def __init__(self):
        self.kv, self.h, self.l = {}, {}, {}

    def ping(self):
        return True

    def set(self, k, v, nx=False, ex=None):
        if nx and k in self.kv:
            return None
        self.kv[k] = str(v)
        return True

    def get(self, k):
        return self.kv.get(k)

    def delete(self, *ks):
        for k in ks:
            self.kv.pop(k, None); self.h.pop(k, None); self.l.pop(k, None)

    def hset(self, k, mapping):
        self.h.setdefault(k, {}).update({a: str(b) for a, b in mapping.items()})

    def hget(self, k, f):
        return self.h.get(k, {}).get(f)

    def hgetall(self, k):
        return dict(self.h.get(k, {}))

    def hincrby(self, k, f, n):
        v = int(self.h.setdefault(k, {}).get(f, 0)) + n
        self.h[k][f] = str(v)
        return v

    def rpush(self, k, v):
        self.l.setdefault(k, []).append(v)

    def lpush(self, k, v):
        self.l.setdefault(k, []).insert(0, v)

    def lpop(self, k):
        return self.l.get(k, []).pop(0) if self.l.get(k) else None

    def blpop(self, k, timeout=0):
        v = self.lpop(k)
        return (k, v) if v else None

    def llen(self, k):
        return len(self.l.get(k, []))

    def lrange(self, k, a, b):
        items = self.l.get(k, [])
        return items[a:] if b == -1 else items[a:b + 1]

    def lrem(self, k, count, v):
        self.l[k] = [x for x in self.l.get(k, []) if x != v]

    def expire(self, k, seconds):
        return k in self.kv or k in self.h or k in self.l

    def exists(self, k):
        return int(k in self.kv or k in self.h or bool(self.l.get(k)))

    def incr(self, k):
        self.kv[k] = str(int(self.kv.get(k, 0)) + 1)
        return int(self.kv[k])

    def close(self):
        pass

    def ltrim(self, k, a, b):
        items = self.l.get(k, [])
        self.l[k] = items[a:] if b == -1 else items[a:b + 1]


class FakeMgr:
    def __init__(self):
        self.n, self.sent, self.killed, self.alive, self.keys = 0, [], [], set(), []
        self.queued = []  # queued Codex deliveries (conftest); `sent` = real pastes only

    def spawn_interactive_cli(self, **kw):
        self.spawned = getattr(self, 'spawned', []) + [kw]
        self.n += 1
        pane = 'pane-' + str(self.n)
        self.alive.add(pane)
        return SimpleNamespace(success=True, pane_id=pane, error=None)

    def send_prompt_to_idle(self, pane, text, bracketed=False):
        self.sent.append((pane, text))
        self.bracketed = getattr(self, 'bracketed', []) + [(pane, bracketed)]
        return pane in self.alive

    def send_key(self, pane, k):
        self.keys.append((pane, k))
        return True

    def place_right_of(self, a, b):
        return True

    def capture_to(self, pane, path):
        Path(path).write_text('screen of ' + pane)

    def tab_panes(self):
        return {}

    def arrange(self, sup, workers, url):
        self.arranged = (sup, list(workers), url)
        self.alive.add('history-pane')
        return 'history-pane'

    def reload_browser(self, pane):
        self.reloaded = getattr(self, 'reloaded', 0) + 1
        return True

    def tab_into(self, pane, peer, before=True):
        self.tabbed = getattr(self, 'tabbed', []) + [(pane, peer)]
        return True

    def kill_pane(self, pane):
        self.killed.append(pane)
        self.alive.discard(pane)
        return True

    def get_pane_status(self, pane):
        return pane if pane in self.alive else None

    def get_or_create_session(self):
        return True


def write_rollout(sessions, thread, cwd, used=1000, rows=(), window=95000):
    folder = sessions / '2026/09/25'
    folder.mkdir(parents=True, exist_ok=True)
    lines = [{'type': 'session_meta', 'payload': {'id': thread, 'cwd': str(cwd)}},
             {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                 'last_token_usage': {'total_tokens': used}, 'model_context_window': window}}}]
    lines += list(rows)
    path = folder / ('rollout-' + thread + '.jsonl')
    path.write_text('\n'.join(json.dumps(x) for x in lines) + '\n')
    return path


# ---- fake `codex queue` (installed for every test by tests/conftest.py)
# Real Codex 0.162.1: a queued message starts its own turn whose rollout task_started has
# turn_attribution.turn_trigger "queue" and whose one UserMessage carries the queued client_id.
CODEX_QUEUE = {}  # thread -> {'turn', 'text' (queued, with its token line), 'body'} of the delivery not answered yet


def _stamp():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def codex_rows(thread, prompt, text=None, *, pop=True):
    """(turn_id, rollout rows) of a Codex turn whose input is `prompt`: the queued delivery's own
    turn when `prompt` is what was queued, else a turn the user typed. text=None: still running."""
    q = CODEX_QUEUE.get(thread)
    if q and prompt in (q['text'], q.get('body')):
        if pop:
            CODEX_QUEUE.pop(thread)
        turn, trigger, client = q['turn'], 'queue', 'c-' + uuid.uuid4().hex
        prompt = q['text']  # Codex records the queued text itself, token line included
    else:
        turn, trigger, client = 't-' + uuid.uuid4().hex, 'user', 'c-' + uuid.uuid4().hex
    rows = [{'timestamp': _stamp(), 'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_attribution': {
                'turn_id': turn, 'turn_trigger': trigger, 'root_turn_id': turn}}},
            {'timestamp': _stamp(), 'type': 'event_msg', 'payload': {'type': 'item_completed', 'turn_id': turn, 'item': {
                'type': 'UserMessage', 'client_id': client, 'content': [{'type': 'text', 'text': prompt}]}}}]
    if text is not None:
        rows.append({'timestamp': _stamp(), 'type': 'event_msg', 'payload': {
            'type': 'task_complete', 'turn_id': turn, 'last_agent_message': text}})
    return turn, rows


def append_rollout(sessions, thread, cwd, rows):
    path = Path(sessions) / '2026/09/25' / ('rollout-' + thread + '.jsonl')
    if not path.exists():
        return write_rollout(Path(sessions), thread, cwd, rows=rows)
    with path.open('a') as fh:
        fh.write('\n'.join(json.dumps(x) for x in rows) + '\n')
    return path


@pytest.fixture
def fx(tmp_path):
    run = tmp_path / 'run'
    (run / 'workspace').mkdir(parents=True)
    cfg = {'run': str(run), 'redis_socket': '/tmp/fleetmux-test.sock', 'python': sys.executable,
           'workers': ['worker-1'], 'handoff_ratio': .6, 'handoff_timeout': 900, 'window': 95000,
           'chrome_script': str(tmp_path / 'broker.sh'), 'codex_home': str(tmp_path / 'codex-home')}
    (run / 'config.json').write_text(json.dumps(cfg))
    sessions = tmp_path / 'sessions'
    f = fleet.Fleet(run / 'config.json', FakeRedis(), FakeMgr(), sessions=sessions)
    f.start_all()
    for name in f.names:  # READY turns bring every agent to idle
        write_rollout(sessions, THREADS[name], fleet.agent_cwd(cfg, name))
        f.handle(turn(name, 'READY'))
    f.handle({'kind': 'task', 'request_id': 'r1', 'spec': {'goal': 'write a test report'}})
    return f


def turn(name, text, generation=0):
    q = CODEX_QUEUE.pop(THREADS.get(name), None)  # the queued delivery this turn answers (its rows are in the rollout)
    return {'kind': 'turn', 'agent': name, 'generation': generation, 'thread': THREADS.get(name), 'text': text,
            **({'turn': q['turn']} if q else {})}


def decision(status='RUNNING', completion=30, dispatch=None, **extra):
    if dispatch is None:
        dispatch = [{'to': 'worker-1', 'instruction': 'Write worker-1/report.md with a Conclusion section, at least 50 chars'}]
    return json.dumps(dict(status=status, completion=completion, reason='r', dispatch=dispatch, **extra))


def received(mgr, pane):
    """What reached `pane`: its queued Codex deliveries (conftest), otherwise its real pastes. Never both:
    the conftest guard fails a test in which a Codex pane also got a paste."""
    box = mgr if hasattr(mgr, 'sent') else mgr.w
    return [t for p, t in getattr(box, 'queued', []) if p == pane] or [t for p, t in box.sent if p == pane]


def traffic(mgr):
    """Everything that reached any pane: real pastes and queued Codex deliveries (for "nothing reached" checks)."""
    box = mgr if hasattr(mgr, 'sent') else mgr.w
    return list(box.sent) + list(getattr(box, 'queued', []))


def last_to(f, name):
    return received(f.mgr, f.agent(name)['pane'])[-1]


def seasoned(f, name):
    """Mark a session as having handled a message already (fresh sessions are never rotated)."""
    f.setf(name, delivered=1)


def test_all_agents_get_role_scope_file_in_own_cwd(fx):
    sup = fleet.agent_cwd(fx.cfg, 'supervisor') / 'AGENTS.md'
    wrk = fleet.agent_cwd(fx.cfg, 'worker-1') / 'AGENTS.md'
    assert 'Supervisor' in sup.read_text() and 'Worker' in wrk.read_text()
    assert sup.parent != wrk.parent
    assert '{{' not in wrk.read_text() and str(fx.workspace()) in wrk.read_text()
    assert 'chrome --config' in wrk.read_text()
    assert json.loads(fx.r.get('fx:reply:r1'))['status'] == 'accepted'
    trust = (Path(fx.cfg['codex_home']) / 'config.toml').read_text()
    assert json.dumps(str(sup.parent)) in trust and json.dumps(str(wrk.parent)) in trust  # pre-answered trust prompt
    assert trust.count('trust_level') == 2
    assert fx.mgr.spawned[-1]['env_vars']['CODEX_HOME'] == fx.cfg['codex_home']


def test_supervisor_dispatch_reaches_worker(fx):
    fx.pump()  # task goal -> supervisor
    assert 'write a test report' in last_to(fx, 'supervisor')
    fx.handle(turn('supervisor', decision()))
    assert fx.r.llen('fx:inbox:worker-1') == 1
    fx.pump()
    assert 'worker-1/report.md' in last_to(fx, 'worker-1')
    assert fx.agent('worker-1')['state'] == 'busy'


def test_auto_agents_stores_plan_and_keeps_profile_ids_out_of_slot_routing(fx):
    fx.finish('cancelled', 'replace fixture task')
    artifacts = []
    task_id = fx.submit({
        'goal': 'implement a router bug fix and tests',
        'auto_agents': True,
        'hours': 2,
        'artifacts': artifacts,
    })

    task = fx.task()
    plan = json.loads(task['dispatch_plan'])
    assignments = json.loads(task['profile_assignments'])['assignments']
    assert task['id'] == task_id and task['auto_agents'] == '1'
    assert json.loads(task['artifacts']) == artifacts
    assert [(item['id'], item['agent_id']) for item in plan['subtasks']] == [
        ('implementation', 'flashnext-coder'),
        ('review', 'claude-reviewer'),
    ]
    assert plan['subtasks'][1]['depends_on'] == ['implementation']
    assert [(item['subtask_id'], item['worker'], item['agent_id']) for item in assignments] == [
        ('implementation', 'worker-1', 'flashnext-coder'),
        ('review', 'worker-1', 'claude-reviewer'),
    ]

    queued = fx.r.l['fx:inbox:supervisor'][-1]
    assert 'flashnext-coder' in queued and 'claude-reviewer' in queued
    assert 'profile ids are recommendations, not worker names' in queued
    assert 'Profile assignments to stable worker slots:' in queued
    assert 'Use each assignment worker value for dispatch.to' in queued
    assert 'Include "Recommended profile: <agent_id>"' in queued
    assert 'Do not dispatch dependent subtasks until every id in depends_on has finished' in queued
    assert fx.labels() == ['worker-1']
    assert fx.r.llen('fx:inbox:flashnext-coder') == 0

    with pytest.raises(ValueError, match='dispatch.to must be one of worker-1'):
        fleet.parse_decision(decision(dispatch=[{
            'to': 'flashnext-coder',
            'instruction': 'Implement the planned router change and tests',
        }]), ['worker-1'], False)

    fx.pump()
    fx.handle(turn('supervisor', decision(dispatch=[{
        'to': 'worker-1',
        'instruction': 'Recommended profile: flashnext-coder. Implement the router change and tests.',
    }])))
    assert fx.r.llen('fx:inbox:worker-1') == 1
    worker_message = fx.r.l['fx:inbox:worker-1'][0]
    assert worker_message.startswith('[Recommended agent profile: flashnext-coder]\n')
    assert 'Capabilities: code-edit, test-fix, planning' in worker_message
    assert 'You are still the stable slot worker-1.' in worker_message
    assert 'Recommended profile: flashnext-coder' in worker_message
    assert fx.r.llen('fx:inbox:flashnext-coder') == 0


def test_task_without_auto_agents_has_no_profile_assignment_metadata(fx):
    task = fx.task()
    assert 'dispatch_plan' not in task and 'profile_assignments' not in task


def test_unique_worker_assignment_adds_advisory_without_explicit_marker(fx):
    fx.finish('cancelled', 'replace fixture task')
    fx.submit({'goal': 'review code changes for safety', 'auto_agents': True})
    fx.pump()
    fx.handle(turn('supervisor', decision(dispatch=[{
        'to': 'worker-1',
        'instruction': 'Review the current code changes and report findings in severity order.',
    }])))

    worker_message = fx.r.l['fx:inbox:worker-1'][0]
    assert worker_message.startswith('[Recommended agent profile: claude-reviewer]\n')
    assert 'Capabilities: code-review, safety-audit, planning' in worker_message


@pytest.mark.parametrize('bad_auto_agents', ['true', 1, 0, None, []])
def test_task_rejects_non_boolean_auto_agents(fx, bad_auto_agents):
    fx.finish('cancelled', 'replace fixture task')
    fx.handle({'kind': 'task', 'request_id': 'bad-auto', 'spec': {
        'goal': 'implement a router bug fix',
        'auto_agents': bad_auto_agents,
    }})
    answer = json.loads(fx.r.get('fx:reply:bad-auto'))
    assert answer['status'] == 'rejected'
    assert answer['reason'] == 'auto_agents must be a boolean'
    assert fx.task()['status'] == 'cancelled'


def test_task_without_auto_agents_keeps_legacy_empty_artifact_validation(fx):
    fx.finish('cancelled', 'replace fixture task')
    with pytest.raises(ValueError, match='artifacts must be a non-empty list'):
        fx.submit({'goal': 'write a report', 'artifacts': []})


def test_task_agent_registry_path_must_be_an_existing_directory(fx, tmp_path, monkeypatch):
    fx.finish('cancelled', 'replace fixture task')
    registry_file = tmp_path / 'registry.json'
    registry_file.write_text('{}')
    with pytest.raises(ValueError, match='must exist and be a directory'):
        fx.submit({'goal': 'plan this change', 'agent_registry': str(registry_file)})
    assert fleet.validate_agent_registry_path(str(tmp_path)) == tmp_path
    monkeypatch.setenv('FLEET_TEST_REGISTRY', str(tmp_path))
    with pytest.raises(ValueError, match='must exist and be a directory'):
        fleet.validate_agent_registry_path('$FLEET_TEST_REGISTRY')


def test_worker_reply_becomes_tagged_supervisor_report(fx):
    fx.pump(); fx.handle(turn('supervisor', decision())); fx.pump()
    fx.handle(turn('worker-1', 'Done: wrote report.md'))
    fx.pump()
    msg = last_to(fx, 'supervisor')
    assert '[worker-1 report]' in msg and 'Done: wrote report.md' in msg


def test_three_bad_supervisor_replies_block(fx):
    for _ in range(3):
        fx.pump()
        fx.handle(turn('supervisor', 'let me look at this first'))
    assert fx.task()['status'] == 'blocked'


def test_supervisor_cannot_submit_host_checks(fx):
    fx.pump()
    fx.handle(turn('supervisor', decision(checks=[['rm', '-rf', '/']])))
    assert fx.task()['bad_replies'] == '1' and fx.r.llen('fx:inbox:worker-1') == 0


def test_completed_with_failing_artifacts_bounced_then_passes(fx):
    art = [{'path': 'worker-1/report.md', 'min_chars': 20, 'contains': ['Conclusion']}]
    fx.pump()
    fx.handle(turn('supervisor', decision('COMPLETED', 100, [], artifacts=art)))
    assert fx.task()['status'] == 'running' and fx.task()['bounces'] == '1'
    fx.pump()
    assert 'Independent verification failed' in last_to(fx, 'supervisor')
    out = fx.run_dir / 'workspace/worker-1/report.md'
    out.write_text('Conclusion: the test passed, and this sentence is long enough to satisfy min_chars.')
    fx.handle(turn('supervisor', decision('COMPLETED', 100, [])))
    assert fx.task()['status'] == 'completed'


def test_three_false_completions_block(fx):
    for _ in range(3):
        fx.pump()
        fx.handle(turn('supervisor', decision('COMPLETED', 100, [])))
    assert fx.task()['status'] == 'blocked'


def test_usage_over_ratio_rotates_before_delivery_and_drops_stale(fx):
    cwd = fleet.agent_cwd(fx.cfg, 'supervisor')
    seasoned(fx, 'supervisor')
    write_rollout(fx.sessions, THREADS['supervisor'], cwd, used=60000)  # 0.63 of 95k
    fx.pump()
    info = fx.agent('supervisor')
    assert info['state'] == 'handoff'
    assert 'Context is almost full' in last_to(fx, 'supervisor')
    assert fx.r.llen('fx:inbox:supervisor') == 1  # task message held back
    draft = Path(info['handoff_draft'])
    draft.write_text('## Done (facts)\n- read the task\n## In progress\nnone\n## Next steps (in order)\n'
                     '1. dispatch worker-1 to write the report\n')
    os.utime(draft, ns=(draft.stat().st_mtime_ns + 10**9,) * 2)
    fx.handle(turn('supervisor', 'handoff written'))
    new = fx.agent('supervisor')
    assert new['generation'] == '1' and new['state'] == 'starting' and 'pane-1' in fx.mgr.killed
    note = list((fx.run_dir / 'handoffs').glob('supervisor-g0-*.md'))[0]
    assert 'dispatch worker-1 to write the report' in note.read_text()
    assert handoff.validate(str(note))[0]
    fx.handle(turn('supervisor', decision(), generation=0))  # late old-generation callback
    assert fx.r.llen('fx:inbox:worker-1') == 0
    assert json.loads(fx.r.l['fx:log'][-1])['kind'] == 'turn.stale'
    new_thread = '33333333-3333-3333-3333-333333333333'
    fx.handle({'kind': 'turn', 'agent': 'supervisor', 'generation': 1, 'thread': new_thread, 'text': 'READY'})
    fx.pump()
    assert 'write a test report' in last_to(fx, 'supervisor') and fx.agent('supervisor')['state'] == 'busy'


@pytest.mark.parametrize('profile,state', [('', 'busy'), ('flashnext-coder', 'handoff')])
def test_handoff_ratio_comes_from_the_running_profile_card(fx, profile, state):
    # 50k of 95k = 0.53: under the fleet's 0.6, over the FlashNext card's 0.5 (design.md §12 model cards)
    seasoned(fx, 'supervisor')
    fx.setf('supervisor', runtime_profile=profile)
    write_rollout(fx.sessions, THREADS['supervisor'], fleet.agent_cwd(fx.cfg, 'supervisor'), used=50000)
    fx.pump()
    assert fx.agent('supervisor')['state'] == state


def test_window_comes_from_rollout_not_config(fx):
    cwd = fleet.agent_cwd(fx.cfg, 'supervisor')
    write_rollout(fx.sessions, THREADS['supervisor'], cwd, used=60000, window=400000)  # 15% of a 400k model
    fx.pump()
    assert fx.agent('supervisor')['state'] == 'busy' and fx.agent('supervisor')['ctx_window'] == '400000'


def test_rotation_timeout_falls_back_to_skeleton(fx):
    fx.cfg['handoff_timeout'] = 0
    seasoned(fx, 'supervisor')
    write_rollout(fx.sessions, THREADS['supervisor'], fleet.agent_cwd(fx.cfg, 'supervisor'), used=90000)
    fx.pump()
    fx.tick()
    info = fx.agent('supervisor')
    assert info['generation'] == '1' and fx.task()['status'] == 'running'
    text = list((fx.run_dir / 'handoffs').glob('supervisor-g0-*.md'))[0].read_text()
    assert 'machine skeleton' in text and 'write a test report' in text
    assert json.loads(fx.r.l['fx:log'][-1])['source'] == 'skeleton'


def test_below_ratio_delivers_without_rotation(fx):
    fx.pump()
    assert fx.agent('supervisor')['generation'] == '0' and fx.agent('supervisor')['state'] == 'busy'


def test_busy_native_subagent_defers_rotation(fx):
    cwd = fleet.agent_cwd(fx.cfg, 'supervisor')
    seasoned(fx, 'supervisor')
    write_rollout(fx.sessions, THREADS['supervisor'], cwd, used=90000)
    child = fx.sessions / '2026/09/25/rollout-child.jsonl'
    child.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'c', 'source': {'subagent': {
        'thread_spawn': {'parent_thread_id': THREADS['supervisor']}}}}}) + '\n'
        + json.dumps({'type': 'event_msg', 'payload': {'type': 'task_started'}}) + '\n')
    fx.pump()
    assert fx.agent('supervisor')['state'] == 'idle' and fx.agent('supervisor')['generation'] == '0'
    assert fx.r.llen('fx:inbox:supervisor') == 1  # held, not fed into a near-full session


def test_notify_requires_real_completion_and_dedupes(tmp_path, monkeypatch):
    run = tmp_path / 'run'
    cfg = {'run': str(run), 'redis_socket': '/tmp/fleetmux-t.sock', 'workers': ['worker-1'], 'python': 'py',
           'codex_home': str(tmp_path / 'codex-home')}
    (tmp_path / 'c.json').write_text(json.dumps(cfg))
    r = FakeRedis()
    monkeypatch.setattr(fleet, 'connect', lambda cfg: r)
    monkeypatch.setattr(fleet.time, 'sleep', lambda s: None)
    sessions = tmp_path / 's'
    t = THREADS['worker-1']
    write_rollout(sessions, t, fleet.agent_cwd(cfg, 'worker-1'), rows=[
        {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'u1', 'last_agent_message': 'done'}}])
    good = json.dumps({'type': 'agent-turn-complete', 'thread-id': t, 'turn-id': 'u1', 'last-assistant-message': 'done'})
    title = json.dumps({'type': 'agent-turn-complete', 'thread-id': t, 'turn-id': 'u9', 'last-assistant-message': 'Title'})
    assert fleet.notify(tmp_path / 'c.json', 'worker-1', '3', title, sessions) is False
    assert fleet.notify(tmp_path / 'c.json', 'worker-1', '3', good, sessions) is True
    assert fleet.notify(tmp_path / 'c.json', 'worker-1', '3', good, sessions) is False
    assert len(r.l['fx:events']) == 1 and json.loads(r.l['fx:events'][0])['generation'] == 3


def test_connect_refuses_non_private_socket():
    for sock in ('/tmp/redis.sock', '/tmp/other-fleet-x.sock', 'fleetmux-x.sock', '/var/run/redis.sock'):
        with pytest.raises(ValueError):
            fleet.connect({'redis_socket': sock})


def test_actor_argv_hides_operator_skills(tmp_path):
    argv = fleet.actor_argv('cfg.json', {'python': 'py', 'profile': 'fleet'}, 'worker-1', 0, 'hi')
    assert 'skills.include_instructions=false' in argv


def test_actor_argv_is_configurable_and_pins_notify_generation(tmp_path):
    cfg = {'python': 'py', 'codex': '/opt/codex', 'profile': 'local-llm'}
    argv = fleet.actor_argv(tmp_path / 'c.json', cfg, 'worker-1', 2, 'hi')
    assert argv[:3] == ['/opt/codex', '--profile', 'local-llm'] and argv[-1] == 'hi'
    assert 'check_for_update_on_startup=false' in argv and '--dangerously-bypass-hook-trust' not in argv
    assert not any(a.startswith('model_auto_compact_token_limit') for a in argv)  # codex decides by default
    notify = json.loads(next(a for a in argv if a.startswith('notify='))[7:])
    assert notify[-4:] == ['notify', str(tmp_path / 'c.json'), 'worker-1', '2']
    with_limit = fleet.actor_argv(tmp_path / 'c.json', dict(cfg, compact_limit=88000), 'worker-1', 2, 'hi')
    assert 'model_auto_compact_token_limit=88000' in with_limit
    assert fleet.actor_argv(tmp_path / 'c.json', {'python': 'py'}, 'w', 0, 'x')[:3] == ['codex', '--profile', 'fleet']


def test_codex_supervisor_may_write_the_roster_directories_and_the_session_board(tmp_path, monkeypatch):
    """A Codex supervisor edits the roster to add / retire workers (2026-10-28) and writes release notes on the
    session board (2026-11-08 user: 「都放行」): its sandbox gets the roster's directory (and the night roster's)
    plus the board directory as writable roots; roster-less supervisors still get the board; workers get none."""
    board = tmp_path / 'board'
    monkeypatch.setenv('FLEETMUX_BOARD_DIR', str(board))
    day, night = tmp_path / 'cfg' / 'roster.yaml', tmp_path / 'night' / 'night.yaml'
    cfg = {'python': 'py', 'roster': str(day), 'roster_night': str(night)}
    roots = [a for a in fleet.actor_argv('c', cfg, 'supervisor', 0, 'p')
             if a.startswith('sandbox_workspace_write.writable_roots=')]
    assert roots == ['sandbox_workspace_write.writable_roots=' + json.dumps(
        [str(day.parent), str(night.parent), str(board)])]
    assert not any('writable_roots' in a for a in fleet.actor_argv('c', cfg, 'worker-1', 0, 'p'))
    bare = [a for a in fleet.actor_argv('c', {'python': 'py'}, 'supervisor', 0, 'p') if 'writable_roots' in a]
    assert bare == ['sandbox_workspace_write.writable_roots=' + json.dumps([str(board)])]


def test_rotated_worker_gets_next_name_and_old_name_still_routes(fx):
    fx.pump(); fx.handle(turn('supervisor', decision())); fx.pump()
    fx.handle(turn('worker-1', 'Done: step one'))
    write_rollout(fx.sessions, THREADS['worker-1'], fleet.agent_cwd(fx.cfg, 'worker-1'), used=60000)
    fx.cfg['handoff_timeout'] = 0  # idle past the ratio: rotates at this pump, before any new message (§32)
    fx.pump(); fx.handle(turn('supervisor', decision(dispatch=[
        {'to': 'worker-1', 'instruction': 'Write worker-1/map.md with five bullet points, at least 50 chars'}])))
    fx.pump()
    assert fx.agent('worker-1')['state'] == 'handoff'
    fx.tick()
    assert fx.label('worker-1') == 'worker-1.1'
    assert fx.mgr.spawned[-1]['window_name'] == 'fx-worker-1.1'
    new_thread = '44444444-4444-4444-4444-444444444444'
    fx.handle({'kind': 'turn', 'agent': 'worker-1', 'generation': 1, 'thread': new_thread, 'text': 'READY'})
    fx.pump()
    assert 'worker-1/map.md' in last_to(fx, 'worker-1')  # same slot dir, artifacts still valid
    write_rollout(fx.sessions, new_thread, fleet.agent_cwd(fx.cfg, 'worker-1'))
    append_rollout(fx.sessions, new_thread, fleet.agent_cwd(fx.cfg, 'worker-1'),  # rewritten: put its queued turn back
                   codex_rows(new_thread, CODEX_QUEUE[new_thread]['text'], pop=False)[1])
    q = CODEX_QUEUE.pop(new_thread, None)  # the queued delivery this turn answers
    fx.handle({'kind': 'turn', 'agent': 'worker-1', 'generation': 1, 'thread': new_thread, 'text': 'Done: step two',
               **({'turn': q['turn']} if q else {})})
    fx.pump()
    assert '[worker-1.1 report (took over from worker-1)]' in last_to(fx, 'supervisor')
    fx.handle(turn('supervisor', decision(dispatch=[
        {'to': 'worker-1.1', 'instruction': 'Append a Conclusion section to worker-1/map.md, at least 30 chars'}])))
    assert fx.r.llen('fx:inbox:worker-1') == 1


def test_empty_turn_redelivers_same_message_with_backoff(fx):
    fx.pump(); fx.handle(turn('supervisor', decision())); fx.pump()
    first = last_to(fx, 'worker-1')
    fx.handle(turn('worker-1', ''))  # backend dropped the request
    assert fx.r.llen('fx:inbox:supervisor') == 0 and fx.r.llen('fx:inbox:worker-1') == 1
    fx.pump()
    assert fx.agent('worker-1')['state'] == 'idle'  # still backing off
    fx.setf('worker-1', retry_at=0)
    fx.pump()
    assert last_to(fx, 'worker-1') == first
    for _ in range(3):
        fx.handle(turn('worker-1', ''))
        fx.setf('worker-1', retry_at=0); fx.pump()
    assert '[worker-1 report]' in last_to(fx, 'supervisor')  # 4th empty goes to the supervisor


def test_one_page_layout_and_history_page_lists_every_generation(fx):
    sup, workers, url = fx.mgr.arranged
    assert sup == fx.agent('supervisor')['pane'] and workers == [fx.agent('worker-1')['pane']]
    assert url.startswith('file://') and url.endswith('history.html')
    fx.cfg['handoff_timeout'] = 0
    for name in ('worker-1', 'worker-1', 'supervisor'):
        info = fx.agent(name)
        gen = int(info['generation'])
        seasoned(fx, name)
        write_rollout(fx.sessions, info.get('thread') or THREADS[name], fleet.agent_cwd(fx.cfg, name), used=60000)
        for n in fx.names:  # only this agent has a message, so only it rotates
            fx.r.delete('fx:inbox:' + n); fx.setf(n, state='idle')
        fx.queue(name, 'Instruction: write ' + name + '/x.md, at least 50 chars')
        old = info['pane']
        fx.pump(); fx.tick()
        assert (fx.agent(name)['pane'], old) in fx.mgr.tabbed and old in fx.mgr.killed  # successor takes its place
        thread = '5555555%d-5555-5555-5555-55555555555%d' % (gen, len(fx.mgr.killed))
        fx.handle({'kind': 'turn', 'agent': name, 'generation': gen + 1, 'thread': thread, 'text': 'READY'})
        fx.r.delete('fx:inbox:' + name); fx.setf(name, state='idle')
    assert fx.label('worker-1') == 'worker-1.2' and fx.label('supervisor') == 'supervisor-1.1'
    assert fx.mgr.spawned[-1]['window_name'] == 'fx-supervisor-1.1'
    assert fx.r.hget('fx:names', 'supervisor-1.1') is None  # the supervisor is never a dispatch target
    page = (fx.run_dir / 'history.html').read_text()
    data = json.loads(page.split('const DATA=', 1)[1].split(';\n', 1)[0])
    slots = {s['slot']: s for s in data['slots']}
    assert [g['label'] for g in slots['worker-1']['gens']] == ['worker-1', 'worker-1.1']
    assert [g['label'] for g in slots['supervisor']['gens']] == ['supervisor-1']
    assert slots['worker-1']['live'] == 'worker-1.2' and slots['supervisor']['live'] == 'supervisor-1.1'
    assert slots['supervisor']['title'] == 'supervisor-1' and slots['worker-1']['title'] == 'worker-1'
    assert slots['worker-1']['gens'][0]['screen'].startswith('screen of ') and fx.mgr.reloaded == 3


def test_history_page_escapes_script_end():
    from fleetmux import history
    import tempfile
    run = Path(tempfile.mkdtemp())
    snap = run / 's.txt'
    snap.write_text('<!-- <script> </script><b>x')
    history.record(run, slot='worker-1', label='worker-1', generation=0, snapshot=str(snap), handoff='', source='model')
    page = history.write_page(run, [('worker-1', 'worker-1.1', 'worker-1')]).read_text()
    assert page.count('</script>') == 1 and page.count('<!--') == 0
    data = json.loads(page.split('const DATA=', 1)[1].split(';\n', 1)[0])
    assert data['slots'][0]['gens'][0]['screen'] == '<!-- <script> </script><b>x'


def test_stalled_worker_is_aborted_and_reported_to_supervisor(fx):
    fx.cfg['stall_timeout'] = 600
    fx.pump(); fx.handle(turn('supervisor', decision())); fx.pump()
    assert fx.agent('worker-1')['state'] == 'busy'
    fx.tick()  # fresh: nothing happens
    assert fx.agent('worker-1')['state'] == 'busy'
    import time as _t
    stale = _t.time() - 1000
    fx.setf('worker-1', busy_since=stale)
    path = write_rollout(fx.sessions, THREADS['worker-1'], fleet.agent_cwd(fx.cfg, 'worker-1'))
    os.utime(path, (stale, stale))
    fx.tick()
    assert fx.agent('worker-1')['state'] == 'idle' and fx.agent('worker-1')['stalls'] == '1'
    assert (fx.agent('worker-1')['pane'], 'Escape') in fx.mgr.keys
    assert any(json.loads(x)['kind'] == 'agent.stalled' for x in fx.r.l['fx:log'])
    fx.pump()
    msg = last_to(fx, 'supervisor')
    assert '[worker-1 report]' in msg and 'STALLED' in msg
    fx.handle(turn('worker-1', 'Done: late reply'))  # a later real turn clears the stall counter
    assert fx.agent('worker-1')['stalls'] == '0'


def _started_turn(fx, text):
    """The rollout of a Codex turn that took `text` from the queue and then went silent (no task_complete)."""
    t = str(uuid.uuid4())
    return [{'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': t,
                                              'turn_attribution': {'turn_id': t, 'turn_trigger': 'queue'}}},
            {'type': 'event_msg', 'payload': {'type': 'item_completed', 'turn_id': t, 'item': {
                'type': 'UserMessage', 'content': [{'type': 'text', 'text': text}]}}}]


def test_stalled_supervisor_gets_one_retry_then_blocks(fx):
    """The stalled turn had taken the message from Codex's queue (Escape aborts it), so one retry is safe."""
    import time as _t
    fx.cfg['stall_timeout'] = 600
    fx.pump()
    first = last_to(fx, 'supervisor')
    stale = _t.time() - 1000
    path = write_rollout(fx.sessions, THREADS['supervisor'], fleet.agent_cwd(fx.cfg, 'supervisor'),
                         rows=_started_turn(fx, fx.agent('supervisor')['cq_text']))
    os.utime(path, (stale, stale))
    fx.setf('supervisor', busy_since=stale)
    fx.tick()
    assert fx.agent('supervisor')['state'] == 'idle' and fx.r.llen('fx:inbox:supervisor') == 1
    fx.pump()
    assert last_to(fx, 'supervisor') == first and fx.task()['status'] == 'running'
    fx.setf('supervisor', busy_since=stale); os.utime(path, (stale, stale))
    fx.tick()
    assert fx.task()['status'] == 'blocked' and 'stalled twice' in fx.task()['reason']


def test_stalled_supervisor_whose_queued_item_never_started_is_not_queued_twice(fx):
    """No turn carries the delivery's token: the item still sits in Codex's queue (abandoning cannot withdraw
    it) and runs once the TUI is free. No second copy is queued; its own turn still settles it."""
    import time as _t
    fx.cfg['stall_timeout'] = 600
    fx.pump()
    first, rec = last_to(fx, 'supervisor'), dict(fx.agent('supervisor'))
    stale = _t.time() - 1000
    path = write_rollout(fx.sessions, THREADS['supervisor'], fleet.agent_cwd(fx.cfg, 'supervisor'))
    os.utime(path, (stale, stale))
    fx.setf('supervisor', busy_since=stale)
    fx.tick()
    assert fx.r.llen('fx:inbox:supervisor') == 0 and fx.agent('supervisor')['state'] == 'busy'
    assert fx.agent('supervisor')['cq_token'] == rec['cq_token'] and fx.agent('supervisor')['cq_state'] == 'queued'
    fx.pump()
    assert last_to(fx, 'supervisor') == first and fx.task()['status'] == 'running'
    assert any(json.loads(x)['kind'] == 'queue.kept' for x in fx.r.l['fx:log'])


def test_rollout_activity_keeps_a_busy_agent_alive(fx):
    import time as _t
    fx.cfg['stall_timeout'] = 600
    fx.pump(); fx.handle(turn('supervisor', decision())); fx.pump()
    fx.setf('worker-1', busy_since=_t.time() - 1000)  # delivered long ago ...
    write_rollout(fx.sessions, THREADS['worker-1'], fleet.agent_cwd(fx.cfg, 'worker-1'))  # ... but still writing
    fx.tick()
    assert fx.agent('worker-1')['state'] == 'busy'


def test_stall_watchdog_can_be_disabled(fx):
    import time as _t
    fx.cfg['stall_timeout'] = 0
    fx.pump(); fx.handle(turn('supervisor', decision())); fx.pump()
    fx.setf('worker-1', busy_since=_t.time() - 10**6)
    fx.tick()
    assert fx.agent('worker-1')['state'] == 'busy'


def test_chrome_broker_only_runs_vetted_verbs(fx, monkeypatch):
    ran = []

    def fake_run(argv, **kw):
        ran.append(argv)
        return SimpleNamespace(returncode=0, stdout='OK', stderr='')
    monkeypatch.setattr(fleet.subprocess, 'run', fake_run)
    slept = []
    monkeypatch.setattr(fleet.time, 'sleep', slept.append)
    assert fx.chrome(['activate'])['status'] == 'rejected'
    assert fx.chrome(['js', '1', '/etc/passwd'])['status'] == 'rejected'
    script = fx.run_dir / 'workspace/worker-1/p.js'
    script.parent.mkdir(parents=True, exist_ok=True); script.write_text('1')
    assert fx.chrome(['js', '1', str(script)])['status'] == 'ok'
    assert fx.chrome(['newtab', 'https://example.com'])['rc'] == 0
    assert [a[1] for a in ran] == ['js', 'newtab'] and ran[0][0] == fx.cfg['chrome_script']
    assert len(slept) == 1 and slept[0] > 15  # second page action waits out the 20 s pacing


def test_chrome_broker_disabled_without_script(fx):
    fx.cfg.pop('chrome_script')
    assert fx.chrome(['check'])['status'] == 'rejected'
    assert 'disabled' in fx.chrome(['check'])['reason']


def test_handoff_merge_keeps_skeleton_for_mangled_sections():
    base = handoff.build_skeleton('h1', 'test', 'goal', next_lines=['do x'])
    merged = handoff.merge_partial(base, '## Done (facts)\n- wrote a.md\n## In progress\n\n## Next steps (in order)\n')
    assert '- wrote a.md' in merged and '1. do x' in merged  # empty model sections keep skeleton text
    assert merged.count('## ') == 8
    assert handoff.merge_partial(base, 'sure, writing it now') == base


def test_prepare_codex_home_links_login_and_profile(tmp_path, monkeypatch):
    user = tmp_path / 'user-codex'; user.mkdir()
    (user / 'auth.json').write_text('{"tokens": {}}')
    (user / 'fleet.config.toml').write_text('model = "x"\n')
    monkeypatch.setattr(fleet, 'USER_CODEX_HOME', user)
    home = fleet.prepare_codex_home(tmp_path / 'agents-home', 'fleet')
    assert (home / 'auth.json').is_symlink() and (home / 'fleet.config.toml').read_text() == 'model = "x"\n'
    assert 'check_for_update_on_startup = false' in (home / 'config.toml').read_text()
    assert not (home / 'hooks.json').exists()
    with pytest.raises(SystemExit):
        fleet.prepare_codex_home(tmp_path / 'other-home', 'missing-profile')


def test_fresh_session_over_ratio_is_delivered_not_rotated_forever(fx):
    # Baseline usage above the ratio: rotating would only spawn another over-budget session.
    cwd = fleet.agent_cwd(fx.cfg, 'supervisor')
    write_rollout(fx.sessions, THREADS['supervisor'], cwd, used=90000)  # 95% of 95k, nothing delivered yet
    fx.pump()
    sup = fx.agent('supervisor')
    assert sup['state'] == 'busy' and sup['generation'] == '0' and sup['delivered'] == '1'
    assert 'write a test report' in last_to(fx, 'supervisor')
    assert json.loads([x for x in fx.r.l['fx:log'] if 'rotate.impossible' in x][-1])['agent'] == 'supervisor'
    # ... but once it has handled a message, the usual rotation applies before the next one.
    fx.handle(turn('supervisor', decision()))
    fx.queue('supervisor', 'anything else')
    fx.pump()
    assert fx.agent('supervisor')['state'] == 'handoff'


# ---------------------------------------------------------------- claude workers
@pytest.fixture
def cfx(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet, 'CLAUDE_JSON', tmp_path / 'claude.json')
    kit = tmp_path / 'kit'
    (kit / '.claude/agents').mkdir(parents=True)
    (kit / '.claude/agents/research.md').write_text('agent')
    (kit / 'mcp.json').write_text('{"mcpServers": {}}')
    run = tmp_path / 'run'
    (run / 'workspace').mkdir(parents=True)
    cfg = {'run': str(run), 'redis_socket': '/tmp/fleetmux-test.sock', 'python': sys.executable,
           'workers': ['worker-1'], 'handoff_ratio': .6, 'handoff_timeout': 900, 'window': 95000,
           'codex_home': str(tmp_path / 'codex-home'), 'worker_cli': 'claude', 'worker_kit': str(kit),
           'claude': '/bin/claude', 'claude_window': 200000}
    (run / 'config.json').write_text(json.dumps(cfg))
    f = fleet.Fleet(run / 'config.json', FakeRedis(), FakeMgr(), sessions=tmp_path / 'sessions')
    f.start_all()
    for name in f.names:
        f.handle(turn(name, 'READY'))
    f.handle({'kind': 'task', 'request_id': 'r1', 'spec': {'goal': 'write a test report'}})
    return f


def test_claude_worker_gets_claude_md_kit_and_trust(cfx, tmp_path):
    cwd = fleet.agent_cwd(cfx.cfg, 'worker-1')
    assert (cwd / 'CLAUDE.md').read_text() == (cwd / 'AGENTS.md').read_text()
    assert (cwd / '.claude/agents/research.md').exists() and (cwd / 'mcp.json').exists()
    trusted = json.loads((tmp_path / 'claude.json').read_text())['projects']
    assert trusted[str(cwd.resolve())]['hasTrustDialogAccepted'] is True
    assert not (fleet.agent_cwd(cfx.cfg, 'supervisor') / 'CLAUDE.md').exists()  # supervisor stays codex


def test_claude_argv_pins_stop_hook_generation(cfx):
    argv = fleet.claude_argv(cfx.path, cfx.cfg, 'worker-1', 4, 'hi')
    assert argv[0] == '/bin/claude' and '--dangerously-skip-permissions' in argv and argv[-2:] == ['--', 'hi']
    # acceptance 10/13 P1-5: an unattended pane must never open a choice dialog
    assert argv[argv.index('--disallowedTools') + 1] == 'AskUserQuestion'
    assert argv.index('--disallowedTools') < argv.index('--settings')  # variadic: an option must follow
    settings = json.loads(Path(argv[argv.index('--settings') + 1]).read_text())
    cmd = settings['hooks']['Stop'][0]['hooks'][0]['command']
    assert cmd.endswith('claude-stop ' + str(cfx.path) + ' worker-1 4')
    assert argv[argv.index('--mcp-config') + 1].endswith('mcp.json')


def test_claude_multiline_message_goes_through_a_file(cfx):
    cfx.handle(turn('supervisor', decision()))
    cfx.pump()
    # Claude worker: the one-line pointer goes to its Stop-hook mailbox, never into the pane.
    assert not [t for p, t in cfx.mgr.sent if p == cfx.agent('worker-1')['pane']]
    entry = json.loads(cfx.r.l[claude_hook.keys('fx', 'worker-1', 0)['mailbox']][-1])
    sent = entry['text']
    assert entry['kind'] == 'pointer' and entry['msg_id'] == cfx.agent('worker-1')['msg_id']
    assert '\n' not in sent and '.inbox/msg-' in sent
    path = Path(sent.split('the full message is in ')[1].split(' . Read')[0])
    assert 'Write worker-1/report.md' in path.read_text()


def test_claude_stop_skips_background_work(tmp_path, monkeypatch):
    (tmp_path / 'c.json').write_text(json.dumps({'redis_socket': '/tmp/fleetmux-t.sock'}))
    r = FakeRedis()
    monkeypatch.setattr(fleet, 'connect', lambda cfg: r)
    busy = json.dumps({'session_id': 's1', 'transcript_path': '/x', 'last_assistant_message': 'wait',
                       'background_tasks': [{'id': 'a'}]})
    done = json.dumps({'session_id': 's1', 'transcript_path': '/x', 'last_assistant_message': 'Done: x',
                       'background_tasks': []})
    assert hook_sim.claude_stop(tmp_path / 'c.json', 'worker-1', '1', busy) is False
    assert hook_sim.claude_stop(tmp_path / 'c.json', 'worker-1', '1', done) is True
    ev = json.loads(r.l['fx:events'][0])
    assert ev['text'] == 'Done: x' and ev['transcript'] == '/x' and ev['generation'] == 1


def claude_turn(f, name, text):
    """A Stop for the message currently delivered to a claude worker."""
    return dict(turn(name, text), msg_id=f.agent(name).get('msg_id'))


def sup_traffic(f):
    sup = f.agent('supervisor')['pane']
    seen = received(f.mgr, sup) + f.r.l.get('fx:inbox:supervisor', [])
    return ''.join(seen)


def test_claude_turn_must_answer_the_current_delivery_once(cfx):
    cfx.handle(turn('supervisor', decision()))
    cfx.pump()
    first = cfx.agent('worker-1')['msg_id']
    assert first and cfx.agent('worker-1')['state'] == 'busy'
    stop_a = claude_turn(cfx, 'worker-1', 'Done: report.md updated')
    cfx.handle(stop_a)
    cfx.handle(stop_a)  # same Stop again: dropped
    assert sup_traffic(cfx).count('Done: report.md updated') == 1
    for n in (2, 3):  # rework twice, identical reply text each time: both are real turns
        cfx.handle(turn('supervisor', decision(dispatch=[{'to': 'worker-1', 'instruction': 'Rework worker-1/report.md round ' + str(n)}])))
        cfx.pump()
        assert cfx.agent('worker-1')['msg_id'] != first
        cfx.handle(stop_a)  # a stale Stop for the first delivery never counts
        assert cfx.agent('worker-1')['state'] == 'busy'
        cfx.handle(claude_turn(cfx, 'worker-1', 'Done: report.md updated'))
    assert sup_traffic(cfx).count('Done: report.md updated') == 3


def test_claude_stop_reads_delivery_id_from_transcript(tmp_path, monkeypatch):
    (tmp_path / 'c.json').write_text(json.dumps({'redis_socket': '/tmp/fleetmux-t.sock'}))
    r = FakeRedis()
    monkeypatch.setattr(fleet, 'connect', lambda cfg: r)
    tr = tmp_path / 't.jsonl'
    rows = [{'type': 'user', 'message': {'content': 'x -- the full message is in /w/.inbox/msg-20261008-090500-abc123.md . Read'}},
            {'type': 'user', 'message': {'content': '<task-notification>agent done</task-notification>'}},
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'partial'}]}}]
    rows += [{'type': 'user', 'message': {'content': [{'type': 'tool_result', 'content': 'z' * 32768}]}}] * 40
    tr.write_text('\n'.join(json.dumps(x) for x in rows) + '\n')  # pointer is > 1 MiB from the end
    payload = json.dumps({'session_id': 's', 'transcript_path': str(tr), 'last_assistant_message': 'Done', 'background_tasks': []})
    assert hook_sim.claude_stop(tmp_path / 'c.json', 'worker-1', '0', payload) is True
    assert json.loads(r.l['fx:events'][0])['msg_id'] == 'msg-20261008-090500-abc123'


def test_trust_claude_dir_refuses_to_overwrite_unreadable_config(tmp_path, monkeypatch):
    cj = tmp_path / 'claude.json'
    cj.write_text('{"projects": {"/a": {"x": 1}}')  # half-written
    monkeypatch.setattr(fleet, 'CLAUDE_JSON', cj)
    with pytest.raises(ValueError):
        fleet.trust_claude_dir(tmp_path)
    assert cj.read_text() == '{"projects": {"/a": {"x": 1}}'
    cj.write_text('{"projects": {"/a": {"x": 1}}, "other": 2}')
    fleet.trust_claude_dir(tmp_path)
    data = json.loads(cj.read_text())
    assert data['other'] == 2 and data['projects']['/a'] == {'x': 1}
    assert data['projects'][str(tmp_path.resolve())]['hasTrustDialogAccepted'] is True
    (tmp_path / 'child').mkdir()
    fleet.trust_claude_dir(tmp_path / 'child')  # parent already trusted: only the child's import answer
    child = json.loads(cj.read_text())['projects'][str((tmp_path / 'child').resolve())]
    assert child == {'hasClaudeMdExternalIncludesApproved': False, 'hasClaudeMdExternalIncludesWarningShown': True}
    before = cj.read_text()
    fleet.trust_claude_dir(tmp_path / 'child')  # everything already answered: no write at all
    assert cj.read_text() == before


def test_trust_goes_to_fleet_home_once(tmp_path, monkeypatch):
    cj = tmp_path / 'claude.json'
    cj.write_text('{}')
    home = tmp_path / 'fleet-home'
    run_cwd = home / 'runs' / 'r1' / 'workspace' / 'worker-1'
    run_cwd.mkdir(parents=True)
    monkeypatch.setattr(fleet, 'CLAUDE_JSON', cj)
    monkeypatch.setattr(fleet, 'HOME', home)
    fleet.trust_claude_dir(run_cwd)
    projects = json.loads(cj.read_text())['projects']
    assert [k for k, v in projects.items() if v.get('hasTrustDialogAccepted')] == [str(home.resolve())]
    assert projects[str(run_cwd.resolve())] == {'hasClaudeMdExternalIncludesApproved': False,
                                                'hasClaudeMdExternalIncludesWarningShown': True}


def test_external_imports_answer_is_never_overridden(tmp_path, monkeypatch):
    cj = tmp_path / 'claude.json'
    cwd = tmp_path / 'w'
    cwd.mkdir()
    cj.write_text(json.dumps({'projects': {str(cwd.resolve()): {'hasClaudeMdExternalIncludesApproved': True,
                                                               'hasClaudeMdExternalIncludesWarningShown': True},
                                           str(tmp_path.resolve()): {'hasTrustDialogAccepted': True}}}))
    before = cj.read_text()
    monkeypatch.setattr(fleet, 'CLAUDE_JSON', cj)
    fleet.trust_claude_dir(cwd)
    assert cj.read_text() == before  # a recorded human answer stays as it is


def test_claude_usage_and_activity_come_from_transcript(cfx, tmp_path):
    tr = tmp_path / 'proj' / 'sess.jsonl'
    tr.parent.mkdir()
    rows = [{'type': 'assistant', 'message': {'usage': {'input_tokens': 10, 'cache_read_input_tokens': 120000,
                                                         'cache_creation_input_tokens': 5, 'output_tokens': 5}}},
            {'type': 'assistant', 'isSidechain': True, 'message': {'usage': {'input_tokens': 999999}}}]
    tr.write_text('\n'.join(json.dumps(x) for x in rows) + '\n')
    cfx.setf('worker-1', transcript=str(tr))
    assert cfx.usage('worker-1') == (120020, 1000000)  # sonnet: native 1M (§22)
    sub = tr.with_suffix('') / 'subagents' / 'agent-1.jsonl'
    sub.parent.mkdir(parents=True)
    sub.write_text('{}\n')
    os.utime(tr, (1000, 1000))
    os.utime(sub, (5000, 5000))
    assert cfx.last_activity(dict(cfx.agent('worker-1'), busy_since=0)) == 5000


# ------------------------------------------------------------ service mode
SVC_THREADS = {'supervisor': THREADS['supervisor'], 'exit': '66666666-6666-6666-6666-666666666666',
               'reentry': '77777777-7777-7777-7777-777777777777'}


@pytest.fixture
def sfx(tmp_path):
    run = tmp_path / 'run'
    (run / 'workspace').mkdir(parents=True)
    roles = tmp_path / 'roles'
    roles.mkdir()
    (roles / 'exit.md').write_text('You judge exits. Propose only trail_dd.')
    cfg = {'run': str(run), 'redis_socket': '/tmp/fleetmux-test.sock', 'python': sys.executable,
           'workers': ['exit', 'reentry'], 'mode': 'service', 'roles': str(roles), 'max_busy': 1,
           'handoff_ratio': .6, 'handoff_timeout': 900, 'window': 95000, 'codex_home': str(tmp_path / 'codex-home')}
    (run / 'config.json').write_text(json.dumps(cfg))
    f = fleet.Fleet(run / 'config.json', FakeRedis(), FakeMgr(), sessions=tmp_path / 'sessions')
    f.start_all()
    for name in f.names:
        write_rollout(f.sessions, SVC_THREADS[name], fleet.agent_cwd(cfg, name))
        f.handle({'kind': 'turn', 'agent': name, 'generation': 0, 'thread': SVC_THREADS[name], 'text': 'READY'})
    return f


def req(rid, to=None, **extra):
    spec = dict({'id': rid, 'payload': {'did': 'cu-1', 'round': 3}, 'reply': {'required': ['verdict', 'param']}}, **extra)
    if to:
        spec['to'] = to
    return spec


def submit(f, spec):
    f.handle({'kind': 'request', 'request_id': 'q-' + spec['id'], 'spec': spec})
    return json.loads(f.r.get('fx:reply:q-' + spec['id']))


def answer(f, name, text):
    q = CODEX_QUEUE.pop(SVC_THREADS[name], None)  # the queued delivery this turn answers
    f.handle({'kind': 'turn', 'agent': name, 'generation': int(f.agent(name)['generation']),
              'thread': SVC_THREADS[name], 'text': text, **({'turn': q['turn']} if q else {})})


def results(f):
    return [json.loads(x) for x in f.r.lrange('fx:results', 0, -1)]


def test_service_scope_files_carry_each_workers_own_prompt(sfx):
    exit_md = (fleet.agent_cwd(sfx.cfg, 'exit') / 'AGENTS.md').read_text()
    re_md = (fleet.agent_cwd(sfx.cfg, 'reentry') / 'AGENTS.md').read_text()
    sup_md = (fleet.agent_cwd(sfx.cfg, 'supervisor') / 'AGENTS.md').read_text()
    assert 'service mode' in exit_md and 'You judge exits' in exit_md and 'Your role (exit)' in exit_md
    assert 'You judge exits' not in re_md and 'judge each request yourself' in sup_md


SERVICE_SUPERVISOR_TEMPLATES = ('service_supervisor.md', 'service_supervisor_v2.md', 'service_supervisor_tui_v2.md')


def test_service_supervisor_templates_let_it_answer_simple_requests():
    # M0: the service supervisor judges each request itself: it answers a simple factual one in answer[],
    # dispatches everything else to a worker, and dispatches when unsure. v1 / v2 / tui say the same rule.
    agents = ROOT / 'fleetmux' / 'agents'
    rules = set()
    for name in SERVICE_SUPERVISOR_TEMPLATES:
        text = (agents / name).read_text(encoding='utf-8')
        assert 'Never answer a request yourself' not in text and 'Route it.' not in text, name
        assert 'judge each request yourself' in text.splitlines()[0], name
        rule = [ln for ln in text.splitlines() if ln.startswith("- Do not do a request's work yourself.")]
        assert len(rule) == 1 and 'When unsure, dispatch.' in rule[0] and 'no file reads or writes' in rule[0], name
        rules.add(rule[0])
        section3 = text[text.index('## 3.'):]
        assert '- `answer`: `[{' in section3 and '"result": "..."' in section3 and '"answer": []' in section3, name
        assert 'only for a request that needs no answer at all' in section3, name
        assert 'empty `dispatch` and `skip`' not in text, name
        assert text.count('empty `dispatch`, `answer` and `skip`') >= 2, name
    assert len(rules) == 1  # word for word the same rule in all three templates
    v1 = (agents / 'service_supervisor.md').read_text(encoding='utf-8')
    assert '`[{"request_id": "<id>", "result": "..."}]`' in v1
    for name in SERVICE_SUPERVISOR_TEMPLATES[1:]:  # v2 / tui: the daemon binds the id
        text = (agents / name).read_text(encoding='utf-8')
        assert '`[{"result": "..."}]`' in text and 'dispatch/answer/skip' in text, name
    assert 'fleet_decision' in (agents / 'service_supervisor_v2.md').read_text(encoding='utf-8')


def test_direct_request_gets_validated_json_result(sfx):
    assert submit(sfx, req('r1', to='exit'))['status'] == 'accepted'
    sfx.pump()
    msg = last_to(sfx, 'exit')
    assert msg.startswith('[Request r1]') and '"round": 3' in msg and 'verdict, param' in msg
    assert sfx.agent('supervisor')['state'] == 'idle'  # direct requests cost no supervisor turn
    answer(sfx, 'exit', 'thinking...\n{"verdict": "sold early", "param": {"trail_dd": 0.25}}')
    res = results(sfx)[-1]
    assert res['id'] == 'r1' and res['status'] == 'ok' and res['worker'] == 'exit'
    assert res['result']['param'] == {'trail_dd': 0.25}
    assert json.loads(sfx.r.get('fx:result:r1'))['status'] == 'ok'
    assert not sfx.r.hgetall('fx:req:r1') and sfx.r.llen('fx:reqq') == 0
    assert submit(sfx, req('r1', to='exit'))['status'] == 'rejected'  # ids are single-use


def test_invalid_reply_gets_one_correction_then_invalid_result(sfx):
    submit(sfx, req('r2', to='exit'))
    sfx.pump()
    answer(sfx, 'exit', 'the trail was too tight')
    sfx.pump()
    assert 'Your last reply was rejected (no JSON object found)' in last_to(sfx, 'exit')
    assert last_to(sfx, 'exit').startswith('[Request r2]')
    answer(sfx, 'exit', '{"verdict": "x"}')
    res = results(sfx)[-1]
    assert res['status'] == 'invalid' and 'missing keys: param' in res['error']


def test_allowed_keys_are_enforced(sfx):
    submit(sfx, req('r3', to='exit', reply={'required': ['verdict'], 'allowed': ['verdict', 'param']}))
    sfx.pump()
    answer(sfx, 'exit', '{"verdict": "ok", "qty": 4}')
    sfx.pump()
    assert 'keys not allowed: qty' in last_to(sfx, 'exit')


def test_supervisor_routes_or_skips(sfx):
    submit(sfx, req('r4'))
    submit(sfx, req('r5'))
    sfx.pump()
    assert '[Request r4] A new request needs routing' in last_to(sfx, 'supervisor')
    answer(sfx, 'supervisor', json.dumps({'reason': 'exit question', 'dispatch': [
        {'to': 'exit', 'request_id': 'r4', 'instruction': 'Check the last sell against the high after it'},
        {'to': 'exit', 'request_id': 'nope', 'instruction': 'Check something that does not exist'}]}))
    sfx.pump()
    assert 'Check the last sell' in last_to(sfx, 'exit') and 'verdict, param' in last_to(sfx, 'exit')
    answer(sfx, 'supervisor', json.dumps({'skip': [{'request_id': 'r5', 'reason': 'nothing changed'}]}))
    assert results(sfx)[-1] == dict(results(sfx)[-1], id='r5', status='skipped', reason='nothing changed')


def test_max_busy_caps_concurrent_workers(sfx):
    submit(sfx, req('r6', to='exit'))
    submit(sfx, req('r7', to='reentry'))
    sfx.pump()
    assert sfx.agent('exit')['state'] == 'busy' and sfx.agent('reentry')['state'] == 'idle'
    answer(sfx, 'exit', '{"verdict": 1, "param": 2}')
    sfx.pump()
    assert sfx.agent('reentry')['state'] == 'busy'


def test_pause_stops_delivery_and_rotation_until_resume(sfx):
    sfx.handle({'kind': 'pause', 'request_id': 'p', 'reason': 'market closed'})
    seasoned(sfx, 'exit')
    write_rollout(sfx.sessions, SVC_THREADS['exit'], fleet.agent_cwd(sfx.cfg, 'exit'), used=90000)
    submit(sfx, req('r8', to='exit'))
    sfx.pump()
    assert sfx.agent('exit')['state'] == 'idle' and sfx.r.llen('fx:inbox:exit') == 1
    sfx.handle({'kind': 'resume', 'request_id': 'p2'})
    sfx.pump()
    assert sfx.agent('exit')['state'] == 'handoff'  # the held rotation runs only after resume


@pytest.mark.parametrize('delivered,state', [(1, 'handoff'), (0, 'idle')])
def test_idle_over_ratio_rotates_without_waiting_for_a_message(sfx, delivered, state):
    # design.md §32: an idle worker past its ratio rotates now, not at its next delivery
    sfx.setf('exit', delivered=delivered)
    write_rollout(sfx.sessions, SVC_THREADS['exit'], fleet.agent_cwd(sfx.cfg, 'exit'), used=90000)
    sfx.pump()
    assert sfx.r.llen('fx:inbox:exit') == 0 and sfx.agent('exit')['state'] == state


def test_idle_rotation_is_checked_once_per_delivery(sfx):
    seasoned(sfx, 'exit')
    sfx.pump()  # under the ratio: checked and marked
    write_rollout(sfx.sessions, SVC_THREADS['exit'], fleet.agent_cwd(sfx.cfg, 'exit'), used=90000)
    sfx.pump()
    assert sfx.agent('exit')['state'] == 'idle'  # same (generation, delivered): not read again
    sfx.setf('exit', delivered=2)
    sfx.pump()
    assert sfx.agent('exit')['state'] == 'handoff'


def test_routing_message_lists_worker_state_queue_request_and_role(sfx):
    submit(sfx, req('r20', to='exit'))
    sfx.pump()
    submit(sfx, req('r21'))
    sfx.pump()
    text = last_to(sfx, 'supervisor')
    assert 'Workers: exit, reentry' in text
    assert '- exit: busy, has request r20, role: You judge exits. Propose only trail_dd.' in text
    assert '- reentry: idle' in text and 'reentry: idle,' not in text


def test_timeout_publishes_and_late_work_is_dropped(sfx):
    submit(sfx, req('r9', to='exit', timeout_s=10))
    sfx.r.hset('fx:req:r9', mapping={'created': 0})
    sfx.tick()
    assert results(sfx)[-1]['status'] == 'timeout'
    sfx.pump()  # the queued message for a finished request is never delivered
    assert sfx.agent('exit')['state'] == 'idle' and sfx.r.llen('fx:inbox:exit') == 0
    submit(sfx, req('r10', to='exit', timeout_s=10))
    sfx.pump()
    sfx.r.hset('fx:req:r10', mapping={'created': 0})
    sfx.tick()
    answer(sfx, 'exit', '{"verdict": 1, "param": 2}')
    assert [r['status'] for r in results(sfx)] == ['timeout', 'timeout']
    assert json.loads(sfx.r.l['fx:log'][-1])['kind'] == 'result.late'


def test_service_never_ends_blocked_pauses_and_tasks_are_refused(sfx):
    sfx.handle({'kind': 'task', 'request_id': 't', 'spec': {'goal': 'x'}})
    assert json.loads(sfx.r.get('fx:reply:t'))['status'] == 'rejected'
    submit(sfx, req('r11'))
    sfx.pump()
    answer(sfx, 'supervisor', '{"status": "BLOCKED", "reason": "login expired"}')
    assert sfx.paused() and 'login expired' in sfx.r.hget('fx:service', 'reason')
    sfx.handle({'kind': 'resume', 'request_id': 'p'})
    for _ in range(3):
        sfx.pump()
        answer(sfx, 'supervisor', 'not json')
    assert sfx.paused() and sfx.r.hgetall('fx:req:r11')  # the request is still waiting, nothing ended


def test_stalled_worker_publishes_stalled_result(sfx):
    sfx.cfg['stall_timeout'] = 600
    submit(sfx, req('r12', to='exit'))
    sfx.pump()
    sfx.setf('exit', busy_since=1)
    os.utime(fleet.rollouts(SVC_THREADS['exit'], sfx.sessions)[0], (1, 1))
    sfx.tick()
    assert results(sfx)[-1]['status'] == 'stalled' and sfx.agent('exit')['state'] == 'idle'


def test_role_profiles_and_request_validation(sfx):
    cfg = dict(sfx.cfg, profile='fleet', supervisor_profile='flashnext')
    assert fleet.actor_argv('c', cfg, 'supervisor', 0, 'p')[2] == 'flashnext'
    assert fleet.actor_argv('c', cfg, 'exit', 0, 'p')[2] == 'fleet'
    for bad in (dict(req('a b')), dict(req('x'), reply={'required': []}), dict(req('x'), to='ghost'),
                dict(req('x'), reply={'required': ['a'], 'allowed': ['b']}), dict(req('x'), timeout_s=1)):
        with pytest.raises(ValueError):
            fleet.validate_request(bad, ['exit', 'reentry'])


def test_kill_pane_retries_with_force_when_cmux_asks(monkeypatch, tmp_path):
    from fleetmux import cmux_manager as cm
    calls = []

    def fake_run(*args):
        calls.append(args)
        refused = args[0] == 'close-surface' and '--force' not in args
        if args[0] == 'close-surface' and '--force' in args:
            live.clear()
        return SimpleNamespace(returncode=1 if refused else 0, stdout='',
                               stderr='Error: confirmation_required: Surface has a running process' if refused else '')
    monkeypatch.setattr(cm, '_run', fake_run)
    mgr = cm.CmuxSessionManager.__new__(cm.CmuxSessionManager)
    state = {'workspace': 'ws', 'control_workspace': None, 'surfaces': {'s1': {}}}
    live = {'s1': {'pane_id': 'p1', 'tty': None}}
    monkeypatch.setattr(mgr, '_meta', lambda: json.loads(json.dumps(state)), raising=False)
    monkeypatch.setattr(mgr, '_save_meta', lambda m: state.update(m), raising=False)
    monkeypatch.setattr(mgr, '_where', lambda sid: 'ws', raising=False)
    monkeypatch.setattr(mgr, '_surfaces', lambda ws: dict(live), raising=False)
    assert mgr.kill_pane('s1')
    closes = [c for c in calls if c[0] == 'close-surface']
    assert len(closes) == 2 and '--force' in closes[1] and '--force' not in closes[0]


def test_workers_become_tabs_of_one_pane_and_successor_takes_its_place(monkeypatch):
    from fleetmux import cmux_manager as cm
    calls = []

    def fake_run(*args):
        calls.append(args)
        out = 'OK hist-1 pane-h' if args[:4] == ('--id-format', 'uuids', 'new-surface', '--type') else ''
        return SimpleNamespace(returncode=0, stdout=out, stderr='')
    monkeypatch.setattr(cm, '_run', fake_run)
    mgr = cm.CmuxSessionManager.__new__(cm.CmuxSessionManager)
    panes = {'sup': 'p0', 'w1': 'p0', 'w2': 'p0', 'w3': 'p0'}
    monkeypatch.setattr(mgr, '_where', lambda sid: 'ws', raising=False)
    monkeypatch.setattr(mgr, '_surfaces', lambda ws: {k: {'pane_id': v} for k, v in panes.items()}, raising=False)
    monkeypatch.setattr(mgr, '_tag', lambda *a, **k: None, raising=False)
    monkeypatch.setattr(mgr, 'rename', lambda *a: None, raising=False)
    monkeypatch.setattr(mgr, '_meta', lambda: {'surfaces': {}}, raising=False)

    def place_right_of(w, sup):
        panes[w] = 'p1'
        return True
    monkeypatch.setattr(mgr, 'place_right_of', place_right_of, raising=False)
    orig_run = fake_run

    def tracking_run(*args):  # a move-surface lands the surface in the target pane
        if args[:4] == ('--id-format', 'uuids', 'new-surface', '--type'):
            panes['hist-1'] = args[args.index('--pane') + 1]
        if args[0] == 'split-off':
            panes[args[args.index('--surface') + 1]] = 'pane-h'
        if args[0] == 'move-surface':
            panes[args[args.index('--surface') + 1]] = args[args.index('--pane') + 1]
        return orig_run(*args)
    monkeypatch.setattr(cm, '_run', tracking_run)
    assert mgr.arrange('sup', ['w1', 'w2', 'w3'], 'file:///h.html') == 'hist-1'
    moves = [c for c in calls if c[0] == 'move-surface']
    assert [m[m.index('--surface') + 1] for m in moves] == ['w2', 'w3'] and all('--before' not in m for m in moves)
    assert panes['w1'] == panes['w2'] == panes['w3'] == 'p1' and panes['sup'] == 'p0'
    assert not [c for c in calls if c[0] == 'split-off' and 'w2' in c]  # workers are tabs, never split again
    calls.clear()
    panes['w1-succ'] = 'p0'
    assert mgr.tab_into('w1-succ', 'w1')
    move = [c for c in calls if c[0] == 'move-surface'][0]
    assert move[move.index('--before') + 1] == 'w1' and panes['w1-succ'] == 'p1'


def test_history_page_follows_the_control_tab_theme(fx, monkeypatch):
    """design §39: the daemon asks the control tab once, the page follows that answer."""
    from fleetmux import terminal_theme
    monkeypatch.setattr(terminal_theme, 'probe', lambda timeout=0.3: {'bg': '#1e1e1e', 'fg': '#e8e6e1'})
    fx.capture_terminal_theme()
    saved = json.loads((fx.run_dir / terminal_theme.FILE).read_text())
    assert saved['bg'] == '#1e1e1e' and saved['fg'] == '#e8e6e1'
    assert any(json.loads(x)['kind'] == 'theme.captured' for x in fx.r.l['fx:log'])
    before = getattr(fx.mgr, 'reloaded', 0)
    fx.refresh_history()
    page = (fx.run_dir / 'history.html').read_text()
    assert page.count('<style id="terminal-theme">') == 1
    assert '--bg:#1e1e1e' in page and '--fg:#e8e6e1' in page and 'color-scheme:dark' in page
    assert fx.mgr.reloaded == before + 1


def test_capture_terminal_theme_logs_when_the_terminal_does_not_answer(fx, monkeypatch):
    """A silent terminal must not block the start: no file, one `theme.unavailable`, page as before."""
    from fleetmux import terminal_theme
    monkeypatch.setattr(terminal_theme, 'probe', lambda timeout=0.3: None)
    assert fx.capture_terminal_theme() is None
    assert not (fx.run_dir / terminal_theme.FILE).exists()
    assert [json.loads(x)['kind'] for x in fx.r.l['fx:log']].count('theme.unavailable') == 1
    fx.refresh_history()
    assert '<style id="terminal-theme">' not in (fx.run_dir / 'history.html').read_text()


def test_run_asks_for_the_terminal_theme_before_starting_the_slots(fx, monkeypatch):
    """run(): the query happens first, so the first page start_all writes already carries the theme."""
    calls = []
    monkeypatch.setattr(fleet.Fleet, 'capture_terminal_theme', lambda self: calls.append('theme'))
    monkeypatch.setattr(fleet.Fleet, 'start_all', lambda self: calls.append('start_all'))
    fx.stopping = True
    fx.run()
    assert calls == ['theme', 'start_all']
