"""Sub-fleets (docs/design.md §19): multi-fleet registries, pane handover, limits, tree stop/resume.

Two real private Redis (main fleet + sub-fleet, project-style runtime dirs) and two in-process
Fleet daemons. cmux is a fake *world* in which every fleet registry owns its own surfaces, so a
pane that one registry released is invisible to it (that is what makes the handover observable).
Hooks are the real ``claude_stop`` / ``notify`` entry points. This proves the wiring, not a real TUI.
"""
import fcntl
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

from test_fleet import codex_rows, received, write_rollout
import hook_sim  # noqa: E402
from fleetmux import clear_after_result, cmux_manager, fleet, redis_runtime, roster, runtime_v2, subfleet

# §48 (user 2026-11-09:「任何场景都不用 claude 输入框打字」): these tests exercised a channel that typed into a
# Claude input box; it is refused at start now (tests/test_no_claude_typing.py covers the refusal).
RETIRED = pytest.mark.skip(reason='docs/design.md §48: fleet never types into a Claude input box (2026-11-09)')


# ------------------------------------------------------------------ fake cmux world
class World:
    def __init__(self, state_dir):
        self.n, self.alive, self.owner, self.ws_of = 0, set(), {}, {}
        self.sent, self.keys, self.mgrs, self.order, self.closed = [], [], {}, [], []
        self.state_dir = Path(state_dir)
        self.urls, self.selected, self.select_ok, self.url_reads = {}, [], True, 0

    def manager(self, cfg):
        name = cfg.get('session') or fleet.SESSION
        if name not in self.mgrs:
            self.mgrs[name] = WorldMgr(self, name, cfg.get('agents_title') or 'Fleet agents')
        return self.mgrs[name]


class WorldMgr:
    def __init__(self, world, session, title):
        self.w, self.session, self.title = world, session, title
        self.ws = None
        self.meta_path = world.state_dir / (session + '.json')
        self.meta_path.parent.mkdir(parents=True, exist_ok=True)
        self.meta_path.write_text('{}')
        self.arranged = None

    def _new(self, kind):
        self.w.n += 1
        sid = 'pane-' + str(self.w.n)
        self.ws = self.ws or ('ws-' + self.session)
        self.w.alive.add(sid)
        self.w.owner[sid] = self.session
        self.w.ws_of[sid] = self.ws
        return sid

    def owns(self, sid):
        return sid in self.w.alive and self.w.owner.get(sid) == self.session

    def spawn_interactive_cli(self, **kw):
        self.spawned = getattr(self, 'spawned', []) + [kw]
        return SimpleNamespace(success=True, pane_id=self._new('agent'), error=None)

    def spawn_window(self, window_name, command, metadata=None, start_directory=None):
        self.windows = getattr(self, 'windows', []) + [(window_name, command)]
        return SimpleNamespace(success=True, pane_id=self._new(window_name), error=None)

    def send_prompt_to_idle(self, pane, text, bracketed=False):
        self.w.sent.append((pane, text))
        return self.owns(pane)

    def send_key(self, pane, k):
        self.w.keys.append((pane, k))
        self.w.sent.append((pane, '<key ' + k + '>'))  # one ordered log of what reached each tab
        return self.owns(pane)

    def get_pane_status(self, pane):
        return pane if self.owns(pane) else None

    def has_cli_process(self, pane, names=('codex',)):
        return self.owns(pane)

    def place_right_of(self, a, b):
        return True

    def capture_to(self, pane, path):
        Path(path).write_text('screen of ' + pane)

    def arrange(self, sup, workers, url):
        self.arranged = (sup, list(workers), url)
        return self._new('history')

    def dock_control(self, pane):
        return True

    def reload_browser(self, pane):
        self.__dict__.setdefault('reloaded', []).append(pane)
        return True

    def browser_url(self, pane):
        self.w.url_reads += 1
        return self.w.urls.get(pane) if self.owns(pane) else None

    def browser_goto(self, pane, url):
        if not self.owns(pane):
            return False
        self.w.urls[pane] = url
        return True

    def select_workspace(self, ws):
        self.w.selected.append(ws)
        return self.w.select_ok

    def tab_into(self, pane, peer, before=True):
        self.tabbed = getattr(self, 'tabbed', []) + [(pane, peer)]
        return self.owns(pane) and self.owns(peer)

    def kill_pane(self, pane):
        if self.w.owner.get(pane) != self.session:
            return False
        self.w.alive.discard(pane)
        self.w.owner.pop(pane, None)
        return True

    def rename(self, pane, label):
        self.renamed = getattr(self, 'renamed', []) + [(pane, label)]

    def get_or_create_session(self):
        return True

    # handover primitives (cmux_manager.CmuxSessionManager has the real ones)
    def workspace_id(self):
        return self.ws

    def release(self, sid):
        if self.w.owner.get(sid) != self.session:
            return None
        self.w.owner.pop(sid)
        return {'type': 'worker', 'label': 'fx-x'}

    def adopt(self, sid, peer, info):
        if sid not in self.w.alive or self.w.owner.get(sid) not in (None, self.session) or not self.owns(peer):
            return False
        self.w.owner[sid] = self.session
        self.w.ws_of[sid] = self.ws
        return True

    def _save_meta(self, meta):
        for sid in meta.get('surfaces', {}):
            if sid in self.w.alive and not self.w.owner.get(sid):
                self.w.owner[sid] = self.session

    def _tag(self, sid, **info):
        self.w.owner[sid] = self.session

    def place_after(self, ws):
        self.w.order.append((self.ws, ws))
        return True

    def session_exists(self):
        return any(self.owns(s) for s in list(self.w.alive))

    def close_all(self):
        for sid in [s for s, o in self.w.owner.items() if o == self.session]:
            self.kill_pane(sid)
        self.w.closed.append(self.session)
        return True


# ------------------------------------------------------------------ fixtures
def start_private(root, token=None):
    cfg = dict(redis_runtime.prepare(root, token or uuid.uuid4().hex[:8]), redis_server=shutil.which('redis-server'),
               transport_version=2)
    return cfg, fleet.start_redis(cfg)


def redis_down(cfg):
    return not subfleet.redis_up(cfg)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    yield from start_tree(tmp_path, monkeypatch)


def start_tree(tmp_path, monkeypatch, **over):
    """Main fleet (task mode, or ``mode='service'`` for named sub-fleets) on a private Redis + fake cmux."""
    if shutil.which('redis-server') is None:
        pytest.skip('redis-server unavailable')
    root = Path('/tmp/cfs-' + uuid.uuid4().hex[:8])  # short: macOS Unix socket path limit
    monkeypatch.setattr(fleet, 'trust_claude_dir', lambda cwd: None)  # never touch ~/.claude.json
    world = World(tmp_path / 'cmux-state')
    monkeypatch.setattr(fleet, 'manager_for', world.manager)
    rcfg, r = start_private(root)
    run = tmp_path / 'fleet-20261013-000000-a1b2c3d4'
    (run / 'workspace').mkdir(parents=True)
    cfg = dict(rcfg, run=str(run), python=sys.executable, workers=['worker-1', 'worker-2'], mode='task',
               supervisor_cli='codex', worker_cli='claude', codex_home=str(tmp_path / 'codex-home'),
               allow_development_v2=True, stall_timeout=0, v2_lease_ms=300000, max_busy=0, max_attempts=2,
               name='研报', token='a1b2c3d4',
               adopted_supervisor_hook_delivery=True)  # every new start pins it; §48 refuses a typed adoption
    cfg.update(over)
    path = run / 'config.json'
    fleet.save(path, cfg)
    t = SimpleNamespace(world=world, root=root, sessions=tmp_path / 'codex-home' / 'sessions',
                        claude=tmp_path / 'claude-projects', threads={}, fleets=[], tmp=tmp_path,
                        monkeypatch=monkeypatch)
    f = fleet.Fleet(path, r, world.manager(cfg), sessions=t.sessions)
    t.f = f
    t.fleets.append(f)
    f.start_all()
    f.r.set(f.v2.prefix + ':boot', 'running')
    for slot in f.names:
        assert hook(t, f, slot, 'READY', prompt='startup')['status'] == 'ready'
    if cfg['mode'] == 'task':
        runtime_v2.post_event(f.v2.bus, dict(kind='task', request_id='new-task', spec={'goal': 'write the report'}),
                              'new-task')
    step(t)
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


def step(t, ms=10):
    for x in t.fleets:
        if not getattr(x, 'stopped', False):
            x.v2.step(ms)
            x.pump()


def drive(t, fn):
    """Run a (blocking) hook while every in-process daemon steps."""
    box = {}
    th = threading.Thread(target=lambda: box.setdefault('value', fn()))
    th.start()
    deadline = time.time() + 15
    while th.is_alive() and time.time() < deadline:
        step(t, 20)
    th.join(2)
    assert not th.is_alive()
    return box['value']


def given(t, f, slot):
    """What the slot's current generation was given: queued for its Codex session (codex queue), typed
    into its pane (a role change, a non-hook Claude slot), or handed over by its Stop hook (a Claude
    worker's deliveries)."""
    return received(t.world, f.agent(slot)['pane']) + hook_sim.handed_to(f, slot)


def hook(t, f, slot, text, *, prompt=None, thread=None, generation=None):
    """Fire the real turn hook the CLI in ``slot`` of fleet ``f`` was started with."""
    gen = int(f.agent(slot)['generation']) if generation is None else generation
    thread = thread or t.threads.setdefault((str(f.run_dir), slot, gen), str(uuid.uuid4()))
    if prompt is None:
        prompt = given(t, f, slot)[-1]
    if slot == 'supervisor' and fleet.agent_cli(f.cfg, slot) == 'codex':
        turn_id, rows = codex_rows(thread, prompt, text)  # the queued delivery's own turn, or a typed one
        write_rollout(t.sessions, thread, fleet.agent_cwd(f.cfg, slot), rows=rows)
        payload = json.dumps({'type': 'agent-turn-complete', 'thread-id': thread, 'turn-id': turn_id,
                              'input-messages': [prompt], 'last-assistant-message': text})
        return drive(t, lambda: fleet.notify(f.path, slot, str(gen), payload, t.sessions))
    return claude_turn(t, f.path, slot, gen, thread, prompt, text)


def claude_turn(t, cfg_path, slot, gen, thread, prompt, text):
    transcript = t.claude / (thread + '.jsonl')
    transcript.parent.mkdir(parents=True, exist_ok=True)
    rows = [hook_sim.user_row(prompt, hook_sim.was_handed(slot, prompt)),
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}}]
    with transcript.open('a') as fh:
        fh.write('\n'.join(json.dumps(x) for x in rows) + '\n')
    raw = json.dumps({'session_id': thread, 'transcript_path': str(transcript), 'last_assistant_message': text})
    return drive(t, lambda: hook_sim.claude_stop(cfg_path, slot, str(gen), raw))


def no_launch(path, cfg, mgr):
    return SimpleNamespace(success=True, pane_id=None, error=None)


def spawn_m0(t, **kw):
    out = t.tmp / 'm0.md'
    rec = subfleet.spawn_child(t.f, 'worker-1', 'M0', 'Research five sources and merge them', 2,
                               output=str(out), launch=no_launch, **kw)
    return rec, out


def child_daemon(t, rec):
    """What `fleetmux daemon <child config>` does on its first boot, in process."""
    cfg = fleet.load(Path(rec['run']) / 'config.json')
    cf = fleet.Fleet(Path(rec['run']) / 'config.json', fleet.connect(cfg), t.world.manager(cfg), sessions=t.sessions)
    cf.lock = (Path(rec['run']) / 'daemon.lock').open('a+')  # a live daemon holds its run's lock
    fcntl.flock(cf.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    t.fleets.append(cf)
    cf.start_all()
    cf.r.set(cf.v2.prefix + ':boot', 'running')
    return cf


def parent_hook(t, text, prompt):
    """A turn end of the delegated pane: its CLI still runs the parent's worker-1 hook."""
    f = t.f
    gen = int(f.agent('worker-1')['generation'])
    thread = t.threads[(str(f.run_dir), 'worker-1', gen)]
    return claude_turn(t, f.path, 'worker-1', gen, thread, prompt, text)


def stop_child_like_daemon(cf):
    """The child daemon's explicit-stop branch of RuntimeV2.run, then daemon() shutting its Redis."""
    for _ in range(5):  # consume the stop event the parent posted (after anything queued before it)
        if cf.explicit_stop:
            break
        cf.v2.step(10)
    assert cf.explicit_stop
    assert subfleet.stop_children(cf)
    assert all(cf.v2.retire(s, 'explicit fleet stop') for s in cf.names)
    die(cf)
    subfleet.shutdown_redis(cf.cfg)  # daemon() shuts its Redis after an explicit stop


def die(cf):
    """The child daemon process is gone (its Redis may still be up)."""
    cf.v2.close()
    cf.stopped = True
    cf.lock.close()


# ------------------------------------------------------------------ acceptance 2 + 3: hand over and back
@RETIRED
@pytest.mark.parametrize('path', ['hook', 'typed_fallback'])
def test_worker_pane_becomes_subfleet_supervisor_and_comes_back(tree, path):
    t, f = tree, tree.f
    sid = f.agent('worker-1')['pane']
    worker_rules = (fleet.agent_cwd(f.cfg, 'worker-1') / 'CLAUDE.md').read_text()
    rec, out = spawn_m0(t)
    child_cfg = fleet.load(Path(rec['run']) / 'config.json')
    # names, titles, tree layout on disk; latest.json is never written for a sub-fleet
    assert Path(rec['run']) == f.run_dir / 'sub' / 'M0'
    assert child_cfg['session'] == 'cmux-fleet:M0' and child_cfg['task_id'] == 'M0'
    assert child_cfg['agents_title'] == '研报 › worker-1 › 子 fleet M0'
    assert child_cfg['redis_socket'] != f.cfg['redis_socket'] and child_cfg['supervisor_cli'] == 'claude'
    assert child_cfg['adopt']['label'] == f.label('worker-1')  # §46: named in its report_up notices
    # the parent stopped delivering to the slot and left a placeholder in its place
    info = f.agent('worker-1')
    assert info['state'] == 'delegated' and info['delegated_pane'] == sid and info['pane'] == rec['placeholder']
    assert (rec['placeholder'], sid) in f.mgr.tabbed
    assert any('已委派：子 fleet M0（看侧栏「研报 › worker-1 › 子 fleet M0」）' in label for _, label in f.mgr.renamed)
    assert f.mgr.get_pane_status(sid) is None and t.world.owner[sid] == 'cmux-fleet:M0'
    # the child daemon adopts the live pane top-left (no new supervisor process) and asks for READY
    cf = child_daemon(t, rec)
    assert cf.agent('supervisor')['pane'] == sid
    assert cf.mgr.arranged[0] == sid and len(cf.mgr.arranged[1]) == 2
    assert [kw['window_name'] for kw in cf.mgr.spawned] == ['fx-M0/worker-1', 'fx-M0/worker-2']
    assert ('cmux-fleet:M0' and (sid, 'fx-M0/supervisor-1') in cf.mgr.renamed)
    assert ('ws-cmux-fleet:M0', f.mgr.workspace_id()) in t.world.order  # sidebar: right after the parent
    assert 'Supervisor: observe, judge, dispatch' in (Path(child_cfg['supervisor_cwd']) / 'CLAUDE.md').read_text()
    # §46: the adopted supervisor and the sub-workers get the sub-fleet boundary rules
    assert '## Sub-fleet boundaries (you supervise sub-fleet M0' in (Path(child_cfg['supervisor_cwd']) / 'CLAUDE.md').read_text()
    assert all('an "exception" from your supervisor' in (fleet.agent_cwd(cf.cfg, w) / 'AGENTS.md').read_text()
               for w in cf.workers)
    # follow-up 1: the delegating worker's own directory is this sub-fleet's task directory, inside its boundary
    task_dir = str(fleet.agent_cwd(f.cfg, 'worker-1'))
    assert child_cfg['task_dir'] == task_dir
    sup_rules = (Path(child_cfg['supervisor_cwd']) / 'CLAUDE.md').read_text()
    assert 'task directory `' + task_dir + '`' in sup_rules and 'not outside your boundary' in sup_rules
    for w in cf.workers:
        rules = (fleet.agent_cwd(cf.cfg, w) / 'AGENTS.md').read_text()
        assert 'task directory `' + task_dir + '`' in rules and 'not outside your boundary' in rules
        argv = fleet.claude_argv(cf.path, cf.cfg, w, 0, 'READY?')
        assert task_dir in argv[argv.index('--add-dir'):argv.index('--')], argv
    role_change = received(t.world, sid)[-1]
    assert 'sub-fleet M0' in role_change and 'READY' in role_change
    for w in cf.workers:
        assert hook(t, cf, w, 'READY', prompt='startup')['status'] == 'ready'
    # READY of the adopted pane goes through the PARENT worker-1 hook and lands in the child
    assert parent_hook(t, 'READY', role_change)['status'] == 'ready'
    assert cf.agent('supervisor')['runtime_ready'] == '1'  # READY, then the task is delivered at once
    assert cf.agent('supervisor')['thread'] == t.threads[(str(f.run_dir), 'worker-1', 0)]  # same session
    assert f.agent('worker-1')['state'] == 'delegated'
    step(t)
    assert 'Task directory of M0: ' + task_dir in cf.task()['goal']
    assert "outside this sub-fleet's workspace and its task directory" in cf.task()['goal']
    task_msg = received(t.world, sid)[-1]
    assert 'Research five sources and merge them' in task_msg
    # §26: the child writes the deliverable inside its own workspace, never the parent's path
    inside = Path(rec['run']) / 'workspace' / 'worker-1' / 'm0.md'
    assert rec['deliver'] == [[str(inside), str(out)]] and str(inside) in task_msg and str(out) not in task_msg
    decision = json.dumps({'status': 'RUNNING', 'completion': 10, 'reason': 'split', 'dispatch': [
        {'to': w, 'instruction': 'Write ' + w + '/part.md with two sources each'} for w in cf.workers]})
    assert parent_hook(t, decision, task_msg)['status'] == 'committed'
    step(t)
    for w in cf.workers:
        assert any('part.md' in x for x in given(t, cf, w))
    assert not any('part.md' in x for x in given(t, f, 'worker-2'))
    # the sub-fleet finishes: deliverable written, task completed, result.json written
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text('merged findings')
    cf.v2.finish('completed', 'all parts merged')
    cf.write_result()
    subfleet.tick(f)  # parent sees result.json and asks the child to stop
    assert subfleet.record(rec['run'])['state'] == 'stopping'
    stop_child_like_daemon(cf)
    assert fleet.load(Path(rec['run']) / 'handback.json')['pane'] == sid
    assert sid in t.world.alive  # the adopted pane was released, never killed
    if path == 'typed_fallback':
        # the documented non-hook path keeps the typed return (daemon and hooks read the same config)
        f.cfg['claude_hook_delivery'] = False
        fleet.save(f.path, dict(fleet.load(f.path), claude_hook_delivery=False))
    # Baseline BEFORE the return: everything typed / keyed so far (take-over role change included).
    sent_before, keys_before = list(t.world.sent), list(t.world.keys)
    gen_before = int(f.agent('worker-1')['generation'])
    label_before = f.label('worker-1')  # the generation that supervised M0
    subfleet.tick(f)  # parent collects: pane back in place of the placeholder, child tabs closed
    rec = subfleet.record(rec['run'])
    assert rec['state'] == 'returned' and rec['outcome'] == 'completed'
    assert out.read_text() == 'merged findings'  # the parent daemon copied it back before checking
    assert rec['placeholder'] not in t.world.alive
    assert 'cmux-fleet:M0' in t.world.closed and redis_down(child_cfg)
    assert (fleet.agent_cwd(f.cfg, 'worker-1') / 'CLAUDE.md').read_text() == worker_rules
    assert 'cmux-fleet:M0' not in [p.stem for p in t.world.state_dir.glob('*.json')]
    info = f.agent('worker-1')
    assert not info['delegate_cfg']
    new_typed = [(p, x) for p, x in t.world.sent[len(sent_before):]]
    new_keys = [(p, k) for p, k in t.world.keys[len(keys_before):]]
    report = 'Done:\n- wrote after.md\nEvidence:\n- worker-1/after.md\nNot done: none\nBlocked: none'
    if path == 'hook':
        # R1-01: a hook-channel Claude worker is never typed into after its launch prompt. The
        # returned session is retired and the next generation starts from its CLI-argument prompt.
        panes = {sid, info['pane']}
        assert info['pane'] != sid and int(info['generation']) == gen_before + 1
        assert info['state'] == 'starting'
        launch = (f.run_dir / 'prompts' / ('worker-1-g' + info['generation'] + '.md')).read_text()
        assert 'resumed after an interruption' not in launch, launch  # a return, not an interruption
        assert 'sub-fleet M0' in launch and 'worker of the main fleet' in launch and 'READY' in launch, launch
        assert not [x for p, x in new_typed if p in panes], 'the return typed into a hook worker pane'
        assert not [k for p, k in new_keys if p in panes and k == 'Enter'], 'Enter was sent to a hook worker pane'
        assert sid not in t.world.alive or t.world.owner.get(sid) != 'cmux-fleet:M0'
        # the new generation becomes READY through its own Stop hook, then work reaches it through the hook
        assert hook(t, f, 'worker-1', 'READY', prompt='startup')['status'] == 'ready'
        assert f.agent('worker-1')['state'] == 'idle'
        f.queue('worker-1', '[Supervisor instruction ' + f.task()['id'] + '] Write worker-1/after.md now')
        step(t)
        after = given(t, f, 'worker-1')[-1]
        assert 'worker-1/after.md' in after and after in hook_sim.handed_to(f, 'worker-1')
        assert not [x for p, x in t.world.sent[len(sent_before):] if p in panes | {f.agent('worker-1')['pane']}]
        assert hook(t, f, 'worker-1', report, prompt=after)['status'] == 'committed'
        notice = [f.v2.bus.message(m)['body']['text'] for m in f.v2.bus.pending('supervisor')
                  + [f.v2.bus.slot_state('supervisor').get('message_id') or 'x'] if f.v2.bus.message(m)]
        assert any('[Sub-fleet M0 completed]' in x and 'new generation' in x for x in notice)
        # the notice names the generation that supervised M0, not the one respawned after it
        assert f.label('worker-1') != label_before
        assert any('] ' + label_before + ' ran sub-fleet M0' in x for x in notice)
        # whole run (before, during and after the delegation): no parent-fleet business delivery was
        # ever typed into any pane worker-1 had; only the sub-fleet role change / child supervisor
        # traffic reached the adopted pane, and that is the child's own (typed) supervisor path
        parent_marker = 'Shared output directory: ' + str(f.workspace())
        all_panes = panes | {f.agent('worker-1')['pane']}
        assert not [x for p, x in t.world.sent if p in all_panes and (parent_marker in x or '[Supervisor instruction' in x)]
        assert any('worker-1/after.md' in x for x in hook_sim.texts('worker-1')), 'parent work reached worker-1 through the hook'
    else:
        # control: without the hook channel the returned session keeps its generation and is
        # told by a typed role-change prompt (the pre-hook behaviour)
        assert t.world.owner[sid] == 'cmux-fleet' and info['pane'] == sid and info['state'] == 'starting'
        back = [x for p, x in new_typed if p == sid][-1]
        assert 'worker of the main fleet again' in back
        assert parent_hook(t, 'READY', back)['status'] == 'ready'  # same generation, same session
        assert f.agent('worker-1')['state'] == 'idle' and int(f.agent('worker-1')['generation']) == gen_before
        assert any('[Sub-fleet M0 completed]' in f.v2.bus.message(m)['body']['text']
                   for m in f.v2.bus.pending('supervisor') + [f.v2.bus.slot_state('supervisor').get('message_id') or 'x']
                   if f.v2.bus.message(m))
        f.queue('worker-1', '[Supervisor instruction ' + f.task()['id'] + '] Write worker-1/after.md now')
        step(t)
        after = [x for p, x in t.world.sent if p == sid][-1]
        assert 'worker-1/after.md' in after and after not in hook_sim.handed_to(f, 'worker-1')
        assert parent_hook(t, report, after)['status'] == 'committed'


def test_delegated_slot_is_not_delivered_held_or_timed_out(tree):
    t, f = tree, tree.f
    rec, _ = spawn_m0(t)
    f.queue('worker-1', '[Supervisor instruction ' + f.task()['id'] + '] Write worker-1/x.md now')
    f.next_liveness = 0
    for _ in range(3):
        f.pump()
        f.tick()
    info = f.agent('worker-1')
    assert info['state'] == 'delegated'  # liveness checks the placeholder, which is alive
    assert f.v2.bus.slot_state('worker-1')['status'] == 'idle' and f.inbox_depth('worker-1') == 1


def test_failed_subfleet_marks_failure_and_missing_deliverable_fails(tree):
    t, f = tree, tree.f
    rec, out = spawn_m0(t)
    cf = child_daemon(t, rec)
    cf.v2.finish('completed', 'claimed done')  # but the deliverable was never written
    cf.write_result()
    subfleet.tick(f)
    stop_child_like_daemon(cf)
    subfleet.tick(f)
    rec = subfleet.record(rec['run'])
    assert rec['state'] == 'failed' and 'missing or empty' in rec['reason'] and rec['attempt'] == 1
    # hook-channel worker: the returned session is replaced by its next generation (never typed into)
    assert f.agent('worker-1')['generation'] == '1' and f.agent('worker-1')['pane'] != rec['pane']
    assert not [x for p, x in t.world.sent if p == rec['pane'] and 'worker of the main fleet again' in x]
    assert hook(t, f, 'worker-1', 'READY', prompt='startup')['status'] == 'ready'
    rec2, _ = spawn_m0(t)  # a retry keeps the old run (history) and counts the attempt
    assert rec2['attempt'] == 2 and rec2['run'].endswith('M0-attempt-2')



def test_deliverable_written_straight_into_the_task_directory_counts(tree):
    """Follow-up 1: a sub-fleet may write in its task directory, so a deliverable can sit at the parent's own path
    with no copy in the child's workspace. §23's copy-back finds nothing to copy and does not fail; the parent's
    check sees the file: returned."""
    t, f = tree, tree.f
    out = fleet.agent_cwd(f.cfg, 'worker-1') / 'm0.md'
    rec = subfleet.spawn_child(t.f, 'worker-1', 'M0', 'Research five sources and merge them', 2,
                               output=str(out), launch=no_launch)
    cf = child_daemon(t, rec)
    assert not Path(rec['deliver'][0][0]).exists()
    out.write_text('merged straight into the task directory\n')
    cf.v2.finish('completed', 'merged')
    cf.write_result()
    subfleet.tick(f)
    stop_child_like_daemon(cf)
    subfleet.tick(f)
    assert subfleet.record(rec['run'])['state'] == 'returned'
    assert out.read_text() == 'merged straight into the task directory\n'


def page_data(run):
    from fleetmux import history
    page = history.page_path(run).read_text()
    return json.loads(page.split('const DATA=', 1)[1].split(';\n', 1)[0])


def test_history_pages_link_parent_and_children_both_ways(tree):
    """Design §19 / nested §5.3: parent page lists its sub-fleets (jump), child page links back."""
    from fleetmux import history
    t, f = tree, tree.f
    f.refresh_history()
    assert page_data(f.run_dir)['children'] == [] and page_data(f.run_dir)['parent'] is None
    reloads = len(f.mgr.reloaded)
    rec, out = spawn_m0(t)
    data = page_data(f.run_dir)  # the parent page was rewritten (and reloaded) on spawn
    assert len(f.mgr.reloaded) > reloads
    [kid] = data['children']
    assert kid['task_id'] == 'M0' and kid['state'] == 'running' and kid['slot'] == 'worker-1'
    assert kid['url'] == history.page_url(rec['run']) and kid['title'] == '研报 › worker-1 › 子 fleet M0'
    cf = child_daemon(t, rec)
    child = page_data(rec['run'])
    assert child['parent']['url'] == history.page_url(f.run_dir) and child['children'] == []
    assert child['parent']['title'] == (f.cfg.get('agents_title') or 'Fleet agents')
    page = history.page_path(rec['run']).read_text()
    assert 'id="fleets"' in page and '回到父 fleet' in page
    # finished: the parent page shows the outcome without a manual refresh
    out.write_text('done')
    cf.v2.finish('completed', 'ok')
    cf.write_result()
    subfleet.tick(f)
    assert page_data(f.run_dir)['children'][0]['state'] == 'stopping'
    stop_child_like_daemon(cf)
    subfleet.tick(f)
    [kid] = page_data(f.run_dir)['children']
    assert kid['state'] == 'returned' and kid['outcome'] == 'completed'


def test_history_page_link_text_is_escaped(tmp_path):
    from fleetmux import history
    links = {'parent': {'task_id': 'M0', 'url': 'file:///x', 'title': '"><img src=x onerror=alert(1)>'},
             'children': [{'task_id': '</script><b>', 'url': 'file:///y', 'title': 't', 'state': 'running'}]}
    page = history.write_page(tmp_path, [], links).read_text()
    assert page.count('</script>') == 1
    assert page_data(tmp_path)['parent']['title'] == '"><img src=x onerror=alert(1)>'


def base36(n):
    digits = ''
    while True:
        n, r = divmod(n, 36)
        digits = '0123456789abcdefghijklmnopqrstuvwxyz'[r] + digits
        if not n:
            return digits


def click(url, rel, at=None, shown='file:///shown/history.html#agent=worker-1'):
    """The URL a page link click leaves in the pane (history.py): the shown page with an explicit marker."""
    from urllib.parse import urlencode
    ms = int((time.time() if at is None else at) * 1000)
    base, _, hash_ = shown.partition('#')
    return base + '?' + urlencode({subfleet.JUMP_PARAM: base36(ms) + '-' + uuid.uuid4().hex[:6], 'rel': rel,
                                   'to': url}) + ('#' + hash_ if hash_ else '')


def history_pane(f):
    return f.r.hget(fleet.SERVICE, 'history_pane')


def jump(f, url, now=None):
    t = time.time() if now is None else now
    f.jump_next = 0  # tests drive the poll interval explicitly
    f.world_url(url)
    return subfleet.follow_jump(f, t)


@pytest.fixture
def linked(tree):
    """Parent with running sub-fleet M0 and its in-process child daemon; both history panes live."""
    from fleetmux import history
    t, f = tree, tree.f
    rec, out = spawn_m0(t)
    cf = child_daemon(t, rec)
    for x in (f, cf):
        x.world_url = lambda url, x=x: t.world.urls.__setitem__(history_pane(x), url)
        t.world.urls[history_pane(x)] = history.page_url(x.run_dir)
    t.cf, t.rec, t.out = cf, rec, out
    t.ws_parent, t.ws_child = f.mgr.workspace_id(), cf.mgr.workspace_id()
    assert t.ws_parent and t.ws_child and t.ws_parent != t.ws_child
    return t


def test_clicked_page_links_switch_workspaces_both_ways_and_restore_the_source_page(linked):
    """Option (a): a link click (explicit marker) -> select-workspace of the matching fleet; the
    clicking pane goes back to its own page (last seen state), the target pane is not touched."""
    from fleetmux import history
    t, f, cf = linked, linked.f, linked.cf
    own, kid = history.page_url(f.run_dir), history.page_url(t.rec['run'])
    assert jump(f, own + '#agent=worker-2&gen=&view=note&q=') is None  # ordinary state: remembered only
    assert t.world.selected == []
    assert jump(f, click(kid, 'child')) == 'jump'
    assert t.world.selected == [t.ws_child]
    assert t.world.urls[history_pane(f)] == own + '#agent=worker-2&gen=&view=note&q='
    assert t.world.urls[history_pane(cf)] == history.page_url(cf.run_dir)  # the child's pane: untouched
    # clicked right after changing state on our own page: the marker URL's own hash wins over the last poll
    assert jump(f, click(kid, 'child', shown=own + '#agent=supervisor&view=note')) == 'jump'
    assert t.world.urls[history_pane(f)] == own + '#agent=supervisor&view=note'
    t.world.selected.pop()
    assert jump(cf, click(own, 'parent')) == 'jump'
    assert t.world.selected == [t.ws_child, t.ws_parent]
    assert t.world.urls[history_pane(cf)] == history.page_url(cf.run_dir)


def test_reload_repeated_tick_and_daemon_restart_never_switch_twice(linked):
    from fleetmux import history
    t, f = linked, linked.f
    own, kid = history.page_url(f.run_dir), history.page_url(t.rec['run'])
    url = click(kid, 'child')
    assert jump(f, url) == 'jump'
    # the goto back did not land (or the page reloaded the marker URL): the same click is not a new one
    assert jump(f, url) == 'repeat' and t.world.urls[history_pane(f)] == own
    restarted = fleet.Fleet(f.path, f.r, f.mgr, sessions=t.sessions)  # a new daemon on the same Redis
    restarted.world_url = f.world_url
    assert jump(restarted, url) == 'repeat'
    assert t.world.selected == [t.ws_child]
    # a plain reload of the marker-free page is never a click
    t.world.urls[history_pane(f)] = kid
    assert jump(f, kid) is None and t.world.selected == [t.ws_child]
    # a click left over from before a long daemon outage only puts the page back
    assert jump(f, click(kid, 'child', at=time.time() - 600)) == 'stale'
    assert t.world.selected == [t.ws_child] and t.world.urls[history_pane(f)] == own
    # the poll interval: one URL read per JUMP_POLL, not one per loop pass
    reads = t.world.url_reads
    f.jump_next = 0
    now = time.time()
    subfleet.follow_jump(f, now); subfleet.follow_jump(f, now + .1); subfleet.follow_jump(f, now + .2)
    assert t.world.url_reads == reads + 1


def test_only_registered_matching_relations_jump(linked, tmp_path):
    from fleetmux import history
    t, f, cf = linked, linked.f, linked.cf
    own, kid = history.page_url(f.run_dir), history.page_url(t.rec['run'])
    assert jump(f, click(kid, 'parent')) == 'unrelated'  # right page, wrong relation
    assert jump(cf, click(own, 'child')) == 'unrelated'  # the parent is not a child of the child
    other = tmp_path / 'other-fleet'
    other.mkdir()
    assert jump(f, click(history.page_url(other), 'child')) == 'unrelated'  # not ours
    assert jump(f, click(own, 'parent')) == 'self'  # a page link to this fleet itself
    assert jump(f, click('javascript:alert(1)', 'child')) == 'stale'  # only file pages are targets
    assert jump(f, own + '?fleet-jump=zz&rel=child') == 'stale'  # malformed marker: page back, no switch
    assert t.world.selected == []
    assert t.world.urls[history_pane(f)] == own


def test_failed_switch_and_missing_registration_show_the_target_page_in_place(linked):
    from fleetmux import history
    t, f = linked, linked.f
    kid = history.page_url(t.rec['run'])
    t.world.select_ok = False
    assert jump(f, click(kid, 'child')) == 'failed'
    assert t.world.urls[history_pane(f)] == kid  # what was clicked, without the marker
    assert t.world.selected == [t.ws_child]
    t.world.select_ok = True
    assert jump(f, t.world.urls[history_pane(f)]) is None  # no marker any more: no retry storm
    t.cf.mgr.ws = None  # the child's cmux registry lost its workspace
    assert jump(f, click(kid, 'child')) == 'unregistered'
    assert t.world.selected == [t.ws_child] and t.world.urls[history_pane(f)] == kid


def test_returned_subfleet_keeps_a_readable_page_and_never_moves_the_view(linked):
    from fleetmux import history
    t, f, cf = linked, linked.f, linked.cf
    kid = history.page_url(t.rec['run'])
    t.out.write_text('done')
    cf.v2.finish('completed', 'ok')
    cf.write_result()
    subfleet.tick(f)
    stop_child_like_daemon(cf)
    subfleet.tick(f)
    assert subfleet.record(t.rec['run'])['state'] == 'returned'
    assert jump(f, click(kid, 'child')) == 'closed'
    assert t.world.selected == [] and t.world.urls[history_pane(f)] == kid
    assert history.page_path(t.rec['run']).is_file()
    # from the closed child's page (shown in the parent's pane) 'back to the parent' is this fleet
    assert jump(f, click(history.page_url(f.run_dir), 'parent')) == 'self'
    assert t.world.selected == [] and t.world.urls[history_pane(f)].startswith(history.page_url(f.run_dir))


def test_the_daemon_tick_follows_a_click(linked):
    from fleetmux import history
    t, f = linked, linked.f
    f.jump_next = 0
    f.world_url(click(history.page_url(t.rec['run']), 'child'))
    f.tick()
    assert t.world.selected == [t.ws_child] and t.world.urls[history_pane(f)] == history.page_url(f.run_dir)


def test_manager_browser_url_goto_and_select_workspace(tmp_path, monkeypatch):
    calls = []

    def run(*args):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='file:///r/history.html?fleet-jump=x\n', stderr='')
    monkeypatch.setattr(cmux_manager, '_run', run)
    m = cmux_manager.CmuxSessionManager('cmux-fleet', state_dir=tmp_path)
    monkeypatch.setattr(m, '_surfaces', lambda w: {'h1': {'pane_id': 'p1'}} if w == 'ws' else {})
    m._save_meta({'workspace': 'ws', 'control_workspace': None, 'surfaces': {'h1': {'type': 'history'}}})
    assert m.browser_url('h1') == 'file:///r/history.html?fleet-jump=x'
    assert m.browser_url('other') is None  # never reads a surface it does not own
    assert m.browser_goto('h1', 'file:///r/history.html') and m.select_workspace('ws-2')
    assert calls == [('browser', '--surface', 'h1', 'url'), ('browser', '--surface', 'h1', 'goto', 'file:///r/history.html'),
                     ('select-workspace', '--workspace', 'ws-2')]
    assert not m.select_workspace('')


def test_fleets_without_links_never_read_the_pane(tree):
    f = tree.f
    f.jump_next = 0
    assert subfleet.follow_jump(f) is None and tree.world.url_reads == 0


def test_page_links_carry_the_explicit_jump_marker(tmp_path):
    from fleetmux import history
    links = {'parent': {'task_id': '', 'url': 'file:///p/history.html', 'title': 'P'},
             'children': [{'task_id': 'M0', 'url': 'file:///c/history.html', 'title': 'C', 'state': 'failed',
                           'outcome': 'failed'}]}
    page = history.write_page(tmp_path, [], links).read_text()
    assert "'fleet-jump':" in page and 'data-rel' in page and subfleet.JUMP_PARAM == 'fleet-jump'
    assert "location.href.split(/[?#]/)[0]+'?'" in page  # the marker stays on the shown file
    assert "c.outcome!==c.state" in page  # 'failed (failed)' is shown once


def test_root_stop_stops_children_first_without_orphans(tree):
    t, f = tree, tree.f
    rec, _ = spawn_m0(t)
    child_cfg = fleet.load(Path(rec['run']) / 'config.json')
    cf = child_daemon(t, rec)
    sid = rec['pane']
    # the child daemon died (no lock held) but its Redis is up: a tree stop still cleans it up
    die(cf)
    assert subfleet.stop_children(f, wait=1)
    assert redis_down(child_cfg)
    rec = subfleet.record(rec['run'])
    assert rec['state'] == 'failed' and rec['outcome'] == 'stopped'
    # taken back from the child: a hook-channel worker's returned session is retired at once (its
    # next generation replaces it), so no orphan stays with the child; the parent's stop retires the rest
    assert sid not in t.world.alive or t.world.owner.get(sid) == 'cmux-fleet'
    assert all(f.v2.retire(s, 'explicit fleet stop') for s in f.names)
    assert sid not in t.world.alive


def test_root_resume_relaunches_running_children(tree):
    t, f = tree, tree.f
    rec, _ = spawn_m0(t)
    child_cfg = fleet.load(Path(rec['run']) / 'config.json')
    subfleet.shutdown_redis(child_cfg)
    assert redis_down(child_cfg)
    launched = []
    subfleet.resume_children(f, launch=lambda path, cfg, mgr: launched.append(path))
    assert launched == [Path(rec['run']) / 'config.json'] and not redis_down(child_cfg)
    subfleet.shutdown_redis(child_cfg)


@RETIRED
def test_child_resume_reattaches_adopted_supervisor_instead_of_respawning(tree):
    t, f = tree, tree.f
    rec, _ = spawn_m0(t)
    cf = child_daemon(t, rec)
    for w in cf.workers:
        hook(t, cf, w, 'READY', prompt='startup')
    role_change = received(t.world, rec['pane'])[-1]
    assert parent_hook(t, 'READY', role_change)['status'] == 'ready'
    die(cf)
    cfg = fleet.load(Path(rec['run']) / 'config.json')
    again = fleet.Fleet(Path(rec['run']) / 'config.json', fleet.connect(cfg), t.world.manager(cfg), sessions=t.sessions)
    t.fleets.append(again)
    again.resume_requested = True
    again.v2.resume_run()
    assert again.agent('supervisor')['pane'] == rec['pane'] and again.agent('supervisor')['generation'] == '0'
    assert rec['pane'] in t.world.alive


# ------------------------------------------------------------------ acceptance 4: limits with reasons
def test_limits_refuse_with_reasons(tree, tmp_path):
    t, f = tree, tree.f
    assert 'must be M<n>' in subfleet.check(f, 'worker-1', 'X1', 1)
    assert 'child_workers_max 3' in subfleet.check(f, 'worker-1', 'M0', 4)
    yaml = tmp_path / 'roster.yaml'
    yaml.write_text('workers:\n  - name: worker-1\n  - name: worker-2\nlimits:\n  max_agents_total: 4\n')
    f.cfg['roster'] = str(yaml)  # read at check time: hot
    assert 'max_agents_total 4: the tree has 3 agents' in subfleet.check(f, 'worker-1', 'M0', 2)
    yaml.write_text('workers:\n  - name: worker-1\n  - name: worker-2\nlimits:\n  max_children: 1\n')
    spawn_m0(t)
    assert 'max_children 1' in subfleet.check(f, 'worker-2', 'M1', 1)
    with pytest.raises(subfleet.Refused, match='max_children 1'):
        subfleet.spawn_child(f, 'worker-2', 'M1', 'other', 1, launch=no_launch)


def test_depth_limit_and_child_id_scope(tree):
    t, f = tree, tree.f
    rec, _ = spawn_m0(t)
    cf = child_daemon(t, rec)
    assert 'max_depth 2: M0.1 would be level 3' in subfleet.check(cf, 'worker-1', 'M0.1', 1)
    assert "must be M0.<n>" in subfleet.check(cf, 'worker-1', 'M1', 1)
    f.cfg['limits'] = {'max_depth': 3}
    fleet.save(f.path, dict(fleet.load(f.path), limits={'max_depth': 3}))
    reason = subfleet.check(cf, 'worker-1', 'M0.1', 1)
    assert reason and 'max_depth' not in reason  # depth allowed now; the slot is just not READY


def test_pi_and_busy_slots_are_refused(tree):
    t, f = tree, tree.f
    f.cfg['worker_cli'] = 'pi'
    assert 'Pi bridge is bound' in subfleet.check(f, 'worker-1', 'M0', 1)
    f.cfg['worker_cli'] = 'claude'
    f.setf('worker-1', state='busy')
    assert 'must be idle and READY' in subfleet.check(f, 'worker-1', 'M0', 1)


def test_request_file_is_executed_by_the_daemon_tick(tree):
    t, f = tree, tree.f
    monkey = subfleet.default_launch
    subfleet.default_launch = no_launch
    try:
        rid = subfleet.request(f.run_dir, 'worker-1', 'M7', 'goal', 9)
        subfleet.tick(f)
        assert fleet.load(subfleet.answer_path(f.run_dir, rid)) == {
            'status': 'rejected', 'reason': 'child_workers_max 3: asked for 9 workers'}
        rid = subfleet.request(f.run_dir, 'worker-1', 'M7', 'goal', 1)
        subfleet.tick(f)
        assert fleet.load(subfleet.answer_path(f.run_dir, rid))['status'] == 'accepted'
    finally:
        subfleet.default_launch = monkey


def test_status_reports_the_tree(tree, capsys):
    t, f = tree, tree.f
    spawn_m0(t)
    fleet.status(f.cfg)
    out = json.loads(capsys.readouterr().out)
    assert out['subfleets']['M0']['state'] == 'running' and out['subfleets']['M0']['slot'] == 'worker-1'


def test_stale_generation_hook_is_not_routed(tree):
    t, f = tree, tree.f
    rec, _ = spawn_m0(t)
    cfg, name, gen = subfleet.delegated_route(f.cfg, 'worker-1', 5)
    assert (name, gen) == ('worker-1', 5)
    cfg, name, gen = subfleet.delegated_route(f.cfg, 'worker-1', 0)
    assert (name, gen, cfg['task_id']) == ('supervisor', 0, 'M0')


# ------------------------------------------------------------------ pure pieces
def test_titles_keep_root_and_last_two_segments():
    assert subfleet.title(['研报', 'worker-1', 'M0']) == '研报 › worker-1 › 子 fleet M0'
    assert (subfleet.title(['研报', 'worker-1', 'M0', 'worker-2', 'M0.1'])
            == '研报 › worker-1 › M0 › worker-2 › 子 fleet M0.1')
    long = subfleet.title(['a very long main fleet name for research', 'worker-1', 'M0', 'worker-2', 'M0.1'], limit=50)
    assert long.startswith('a very long') and long.endswith(' › … › worker-2 › 子 fleet M0.1')


def test_ids_levels_and_sessions():
    assert subfleet.valid_child_id(None, 'M3') and not subfleet.valid_child_id(None, 'M0.1')
    assert subfleet.valid_child_id('M0', 'M0.2') and not subfleet.valid_child_id('M0', 'M1.2')
    assert subfleet.task_level('M0') == 2 and subfleet.task_level('M0.1') == 3
    assert subfleet.session_name() == 'cmux-fleet' and subfleet.session_name('M0.1') == 'cmux-fleet:M0.1'


def test_v2_matrix_allows_claude_supervisor_only_for_adopted_subfleets():
    # claude+claude/task and (2026-11-03) claude+claude/service are verified by real smokes now.
    base = dict(transport_version=2, allow_development_v2=True, supervisor_cli='claude', worker_cli='claude')
    assert runtime_v2.v2_cli_problem(base) is None
    assert runtime_v2.v2_cli_problem(dict(base, mode='service')) is None
    assert runtime_v2.v2_cli_problem(dict(base, adopt={'pane': 'x'})) is None
    # §29 (design change): an adopted service sub-fleet (a named sub-fleet) is allowed too.
    assert runtime_v2.v2_cli_problem(dict(base, adopt={'pane': 'x'}, mode='service')) is None


def test_roster_limits_are_validated(tmp_path):
    good = tmp_path / 'r.yaml'
    good.write_text('workers:\n  - name: worker-1\nlimits:\n  max_depth: 3\n')
    assert roster.load(good) == (['worker-1'], {}) and roster.load_limits(good) == {'max_depth': 3}
    bad = tmp_path / 'b.yaml'
    bad.write_text('workers:\n  - name: worker-1\nlimits:\n  max_depth: 0\n  whatever: 2\n')
    with pytest.raises(ValueError, match='max_depth, whatever'):
        roster.load(bad)


def test_claude_worker_session_name_is_stable_per_slot(tmp_path):
    cfg = {'run': str(tmp_path / 'fleet-20261013-000000-a1b2c3d4'), 'python': sys.executable, 'worker_model': 'sonnet'}
    (Path(cfg['run']) / 'workspace' / 'worker-2').mkdir(parents=True)
    for gen in (0, 3):
        argv = fleet.claude_argv(tmp_path / 'c.json', cfg, 'worker-2', gen, 'hi')
        assert argv[argv.index('--name') + 1] == 'fa1b2c3d4-worker-2'
    sub = dict(cfg, task_id='M0', root_token='a1b2c3d4')
    argv = fleet.claude_argv(tmp_path / 'c.json', sub, 'worker-2', 0, 'hi')
    assert argv[argv.index('--name') + 1] == 'fa1b2c3d4-M0-worker-2'
    assert argv[-2:] == ['--', 'hi']


def test_start_name_sets_cfg_and_title(tmp_path, monkeypatch):
    from test_pi import mock_start
    args, _ = mock_start(tmp_path, monkeypatch)
    seen = {}

    def factory(name, **kw):
        seen.update(kw, session=name)
        return SimpleNamespace(meta_path=tmp_path / 'isolated-state' / 'cmux-fleet.json', session_exists=lambda: False,
                               spawn_window=lambda *a, **k: SimpleNamespace(success=True))
    monkeypatch.setattr(cmux_manager, 'CmuxSessionManager', factory)
    fleet.main(args + ['--name', '研报'])
    cfg = fleet.load(tmp_path / 'latest.json')
    assert cfg['name'] == '研报' and cfg['agents_title'] == 'Fleet 研报' and len(cfg['token']) == 8
    assert seen == {'agents_title': 'Fleet 研报', 'session': 'cmux-fleet'}


# ------------------------------------------------------------------ real cmux_manager primitives (fake _run)
def test_manager_release_adopt_place_after_close(tmp_path, monkeypatch):
    live = {'parent-ws': {'w1': {'pane_id': 'p1'}, 'ph': {'pane_id': 'p1'}}, 'child-ws': {'cw1': {'pane_id': 'c1'}}}
    calls = []

    def run(*args):
        calls.append(args)
        if args[0] == 'move-surface':
            sid = args[args.index('--surface') + 1]
            src = next(ws for ws in live if sid in live[ws])
            live[args[args.index('--workspace') + 1]][sid] = {'pane_id': args[args.index('--pane') + 1]}
            live[src].pop(sid)
        if args[0] == 'close-surface':
            sid = args[args.index('--surface') + 1]
            for ws in live.values():
                ws.pop(sid, None)
        return SimpleNamespace(returncode=0, stdout='OK', stderr='')
    monkeypatch.setattr(cmux_manager, '_run', run)
    parent = cmux_manager.CmuxSessionManager('cmux-fleet', state_dir=tmp_path)
    child = cmux_manager.CmuxSessionManager('cmux-fleet:M0', state_dir=tmp_path)
    for m, ws in ((parent, 'parent-ws'), (child, 'child-ws')):
        monkeypatch.setattr(m, '_surfaces', lambda w, ws=ws: {s: dict(v) for s, v in live.get(w, {}).items()})
    parent._save_meta({'workspace': 'parent-ws', 'control_workspace': None,
                       'surfaces': {'w1': {'type': 'worker'}, 'ph': {'type': 'placeholder'}}})
    child._save_meta({'workspace': 'child-ws', 'control_workspace': None, 'surfaces': {'cw1': {'type': 'worker'}}})
    info = parent.release('w1')
    assert info == {'type': 'worker'} and not parent.owns('w1')
    assert child.adopt('w1', 'cw1', info) and child.owns('w1') and live['child-ws']['w1']['pane_id'] == 'c1'
    assert child.place_after('parent-ws')
    assert ('reorder-workspace', '--workspace', 'child-ws', '--after', 'parent-ws') in calls
    assert child.release('w1') and parent.adopt('w1', 'ph', {'type': 'worker'}) and parent.owns('w1')
    monkeypatch.setattr(child, 'kill_pane', lambda sid: (live['child-ws'].pop(sid, None), child.release(sid), True)[2])
    assert child.close_all() and not live['child-ws']
    assert ('close-workspace', '--workspace', 'child-ws') in calls


def test_v2_daemon_loop_runs_the_fleet_tick(tree, monkeypatch):
    """Regression: the v2 loop called RuntimeV2.tick directly, so roster reload, result.json and
    sub-fleet collection (all in Fleet.tick) never ran in a real v2 daemon."""
    t, f = tree, tree.f
    seen = []

    def once(fl):
        seen.append(fl)
        fl.stopping = True
    monkeypatch.setattr(subfleet, 'tick', once)
    f.v2.close()
    again = fleet.Fleet(f.path, f.r, f.mgr, sessions=t.sessions)
    t.fleets[0] = again
    again.v2.run()
    assert seen == [again]


def test_parent_task_end_stops_running_children(tree):
    t, f = tree, tree.f
    rec, _ = spawn_m0(t)
    cf = child_daemon(t, rec)
    f.v2.finish('blocked', 'main task blocked')
    subfleet.tick(f)
    rec = subfleet.record(rec['run'])
    assert rec['state'] == 'stopping' and rec['outcome'] == 'cancelled'
    stop_child_like_daemon(cf)
    subfleet.tick(f)
    assert subfleet.record(rec['run'])['state'] == 'failed'


def test_cli_spawn_targets_the_owning_fleet(tree):
    t, f = tree, tree.f
    args = SimpleNamespace(task_id='M0.1', slot='worker-1', goal='g', workers=1, output=None)
    assert subfleet.cli_spawn(f.cfg, args, wait=0) == {'status': 'rejected', 'reason': 'no running sub-fleet M0'}
    args.task_id = 'M3'
    out = subfleet.cli_spawn(f.cfg, args, wait=0)
    assert out['status'] == 'pending' and (f.run_dir / 'subfleet-requests' / (out['request'] + '.json')).exists()


def test_parent_resume_keeps_a_delegated_slot_and_relaunches_the_child(tree):
    t, f = tree, tree.f
    rec, _ = spawn_m0(t)
    f.v2.close()
    again = fleet.Fleet(f.path, f.r, f.mgr, sessions=t.sessions)
    t.fleets[0] = again
    t.f = again
    again.resume_requested = True
    again.v2.resume_run()
    info = again.agent('worker-1')
    assert info['state'] == 'delegated' and info['generation'] == '0' and info['pane'] == rec['placeholder']
    assert rec['placeholder'] in t.world.alive and t.world.owner[rec['pane']] == 'cmux-fleet:M0'
    assert subfleet.delegated_route(again.cfg, 'worker-1', 0)[0]['task_id'] == 'M0'
    child_mgr = t.world.mgrs['cmux-fleet:M0']
    assert any('--resume' in cmd for _, cmd in getattr(child_mgr, 'windows', []))  # child daemon relaunched
    assert again.agent('worker-2')['state'] != 'delegated'


def test_failed_child_redis_start_rolls_back_without_killing_the_parent(tree, monkeypatch):
    t, f = tree, tree.f
    sid = f.agent('worker-1')['pane']

    def boom(cfg, tries=30):
        raise SystemExit('private Redis did not become ready')
    monkeypatch.setattr(fleet, 'start_redis', boom)
    with pytest.raises(subfleet.Refused, match='did not become ready'):
        spawn_m0(t)
    info = f.agent('worker-1')
    assert info['state'] == 'idle' and info['pane'] == sid and t.world.owner[sid] == 'cmux-fleet'
    assert not subfleet.active_children(f.run_dir)
    assert subfleet.record(f.run_dir / 'sub' / 'M0')['state'] == 'failed'
    assert len([p for p in t.world.alive if t.world.owner.get(p) == 'cmux-fleet']) == 4  # sup, 2 workers, history


@pytest.mark.parametrize('path', ['hook', pytest.param('typed_fallback', marks=RETIRED)])
def test_return_aborts_a_running_turn_before_the_return_prompt(tree, path):
    t, f = tree, tree.f
    if path == 'typed_fallback':
        f.cfg['claude_hook_delivery'] = False
        fleet.save(f.path, dict(fleet.load(f.path), claude_hook_delivery=False))
    rec, _ = spawn_m0(t)
    cf = child_daemon(t, rec)
    f.v2.finish('blocked', 'main task blocked')
    subfleet.tick(f)
    stop_child_like_daemon(cf)
    before = len(t.world.sent)
    subfleet.tick(f)
    log = [x for p, x in t.world.sent[before:] if p == rec['pane']]
    if path == 'hook':
        # no return prompt at all: the possibly still running session is killed, not typed into
        assert log == [] and rec['pane'] not in t.world.alive
        assert f.agent('worker-1')['generation'] == '1'
    else:
        assert log[-2] == '<key Escape>' and 'worker of the main fleet again' in log[-1]


def test_lost_session_is_replaced_by_the_next_generation_in_place(tree):
    t, f = tree, tree.f
    rec, out = spawn_m0(t)
    cf = child_daemon(t, rec)
    out.write_text('done')
    cf.v2.finish('completed', 'ok')
    cf.write_result()
    subfleet.tick(f)
    stop_child_like_daemon(cf)
    (Path(rec['run']) / 'handback.json').unlink()
    t.world.alive.discard(rec['pane'])  # e.g. cmux restarted: the delegated session is gone
    t.world.owner.pop(rec['pane'], None)
    subfleet.tick(f)
    info = f.agent('worker-1')
    assert info['generation'] == '1' and info['label'] == 'worker-1.1' and info['state'] == 'starting'
    assert rec['placeholder'] not in t.world.alive and info['pane'] not in (rec['pane'], rec['placeholder'])
    assert subfleet.record(rec['run'])['state'] == 'returned'


# ------------------------------------------------------------------ §29 named sub-fleets
from fleetmux import named_subfleet  # noqa: E402



SUP_ROLE = 'You are the ops sub-fleet supervisor. ROLE-MARKER-OPS.\n'


def write_roster(t, autostart=False, declare=True, extra=''):
    (t.tmp / 'sup.md').write_text(SUP_ROLE)
    (t.tmp / 'child.yaml').write_text('workers:\n  - name: ops-1\n')
    body = 'workers:\n  - name: worker-1\n  - name: worker-2\n'
    if declare:
        body += ('subfleets:\n  - name: ops\n    supervisor_slot: worker-1\n    supervisor_role: sup.md\n'
                 '    roster: child.yaml\n    autostart: ' + str(autostart).lower() + '\n    drain_s: 60\n' + extra)
    path = t.tmp / 'roster.yaml'
    path.write_text(body)
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000 * (1 + len(t.__dict__.get('rosters', [])))))
    t.__dict__.setdefault('rosters', []).append(body)
    return path


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setattr(subfleet, 'default_launch', no_launch)
    gen = start_tree(tmp_path, monkeypatch, mode='service', max_attempts=1)
    t = next(gen)
    t.f.cfg['roster'] = str(write_roster(t))
    t.f.roster_tick()
    try:
        yield t
    finally:
        for _ in gen:
            pass


def ask(t, op, name='ops', rid=None, **extra):
    rid = rid or 'op-' + uuid.uuid4().hex[:8]
    ev = dict(kind='request', spec=dict(id=rid, op='subfleet.' + op, name=name, **extra))
    return rid, drive(t, lambda: fleet.request(t.f.cfg, ev))


def result(f, rid):
    raw = f.r.get(fleet.RESULT + rid)
    return json.loads(raw) if raw else None


def supervisor_answers(t, rid, decision='approve', note='fine'):
    f = t.f
    step(t)
    prompt = given(t, f, 'supervisor')[-1]  # Codex supervisor: its queued delivery (no typing)
    assert rid in prompt and '"subfleet"' in prompt
    text = json.dumps({'status': 'RUNNING', 'dispatch': [], 'skip': [],
                       'subfleet': {'id': rid, 'decision': decision, 'note': note}})
    assert hook(t, f, 'supervisor', text, prompt=prompt)['status'] == 'committed'


def adopt_child(t, name='ops'):
    """The child daemon boots in process and its supervisor (the parent's worker pane) says READY."""
    rec = subfleet.record(t.f.run_dir / 'sub' / name)
    # §48: the role change reaches the adopted pane through its (parent-pinned) Stop hook, never typed
    cf, _ = adopt_by_hook(t, t.monkeypatch, rec)
    return rec, cf


def bring_up(t, name='ops'):
    rid, answer = ask(t, 'up', name)
    assert answer['status'] == 'accepted' and result(t.f, rid) is None, answer
    supervisor_answers(t, rid)
    subfleet.tick(t.f)
    assert named_subfleet.state(t.f, name)['state'] == 'starting'
    rec, cf = adopt_child(t, name)
    subfleet.tick(t.f)
    return rid, rec, cf


def take_down(t, cf, name='ops'):
    rid, _ = ask(t, 'down', name)
    supervisor_answers(t, rid)
    subfleet.tick(t.f)  # draining -> quiet -> stop posted
    stop_child_like_daemon(cf)
    subfleet.tick(t.f)  # collected
    return rid


def typed_return(t):
    """Run this test on the typed fallback path (``claude_hook_delivery: false``, as the §19 tests'
    ``typed_fallback`` parameter does). The §29 / §3.1 promise "the delegated session itself comes back
    as the worker" holds only there: on the hook channel (the default since the no-typing batch) a
    returned Claude worker is never typed into, it is retired and its next generation starts (R1-01;
    covered by the ``*_on_the_hook_channel_*`` tests). The user chose the hook-channel behaviour for named
    sub-fleets on 2026-10-14 (design §29: return = retire the session, next generation in the parent slot)."""
    f = t.f
    f.cfg['claude_hook_delivery'] = False
    fleet.save(f.path, dict(fleet.load(f.path), claude_hook_delivery=False))


def supervisor_texts(f):
    mids = f.v2.bus.pending('supervisor') + [f.v2.bus.slot_state('supervisor').get('message_id') or 'x']
    return [f.v2.bus.message(m)['body']['text'] for m in mids if f.v2.bus.message(m)]


def test_named_roster_section_is_validated(tmp_path):
    (tmp_path / 'sup.md').write_text('x')
    (tmp_path / 'child.yaml').write_text('workers:\n  - name: a\n')
    base = 'workers:\n  - name: worker-1\n  - name: worker-2\n'
    good = tmp_path / 'good.yaml'
    good.write_text(base + 'subfleets:\n  - {name: ops, supervisor_slot: worker-1, supervisor_role: sup.md, '
                    'roster: child.yaml, claude: bin/ro-claude, worker_model: haiku, timeouts: {stall: 150}, '
                    'max_busy: 1}\n')
    roster.load_entries(good)  # the worker part is unchanged
    [e] = roster.load_subfleets(good)
    assert e['autostart'] is False and e['mode'] == 'service' and e['drain_s'] == 600
    assert e['stall_timeout'] == 150 and 'request_timeout' not in e and e['max_busy'] == 1
    assert e['claude'] == str(tmp_path / 'bin/ro-claude') and e['roster'] == str(tmp_path / 'child.yaml')
    bad = tmp_path / 'bad.yaml'
    bad.write_text(base + 'subfleets:\n'
                   '  - {name: worker-2, supervisor_slot: worker-1, supervisor_role: sup.md, roster: child.yaml}\n'
                   '  - {name: b, supervisor_slot: nobody, supervisor_role: none.md, roster: child.yaml, mode: x}\n'
                   '  - {name: c, supervisor_slot: worker-2, supervisor_role: sup.md, roster: child.yaml, autostart: 1}\n'
                   '  - {name: d, supervisor_slot: worker-2, supervisor_role: sup.md, roster: child.yaml, drain_s: 0}\n')
    with pytest.raises(ValueError) as exc:
        roster.load_entries(bad)
    text = str(exc.value)
    for want in ('name is also a worker name', 'supervisor_slot must be one of the workers',
                 'supervisor_role file not found', 'mode must be one of', 'autostart must be true or false',
                 'already supervises another sub-fleet', 'drain_s must be a positive'):
        assert want in text


@RETIRED
def test_named_entry_clear_switches_are_validated_and_never_inherited(tmp_path):
    """§29 a): clear_after_result / clear_timeout / claude_hook_delivery are entry keys of their own:
    validated with the roster, passed into the child config, and never taken from the parent."""
    (tmp_path / 'sup.md').write_text('x')
    (tmp_path / 'child.yaml').write_text('workers:\n  - name: a\n')
    base = 'workers:\n  - name: worker-1\n  - name: worker-2\n'
    good = tmp_path / 'good.yaml'
    good.write_text(base + 'subfleets:\n'
                    '  - {name: ops, supervisor_slot: worker-1, supervisor_role: sup.md, roster: child.yaml,\n'
                    '     clear_after_result: true, clear_timeout: 45, claude_hook_delivery: false}\n'
                    '  - {name: plain, supervisor_slot: worker-2, supervisor_role: sup.md, roster: child.yaml}\n')
    ops, plain = roster.load_subfleets(good)
    assert (ops['clear_after_result'], ops['clear_timeout'], ops['claude_hook_delivery']) == (True, 45, False)
    assert not {'clear_after_result', 'clear_timeout', 'claude_hook_delivery'} & set(plain)
    parent = dict(fleet.DEFAULTS, mode='service', clear_after_result=True, clear_timeout=99,
                  claude_hook_delivery=False)
    cfg = subfleet.child_worker_cfg(parent, ops)
    assert (cfg['clear_after_result'], cfg['clear_timeout'], cfg['claude_hook_delivery']) == (True, 45, False)
    cfg = subfleet.child_worker_cfg(parent, plain)  # not written: the built-in default, not the parent's
    assert cfg['clear_after_result'] is False and cfg['clear_timeout'] == 20 and 'claude_hook_delivery' not in cfg
    bad = tmp_path / 'bad.yaml'
    bad.write_text(base + 'subfleets:\n'
                   '  - {name: ops, supervisor_slot: worker-1, supervisor_role: sup.md, roster: child.yaml,\n'
                   '     clear_after_result: 1, clear_timeout: 0, claude_hook_delivery: "no"}\n')
    with pytest.raises(ValueError) as exc:
        roster.load_entries(bad)
    for want in ('clear_after_result must be true or false', 'claude_hook_delivery must be true or false',
                 'clear_timeout must be a positive number'):
        assert want in str(exc.value)


@RETIRED
def test_named_up_with_clear_on_the_hook_channel_is_rejected_by_program(svc):
    """§29 a): the §28 / hook-delivery exclusion holds for a named child too, refused at its up."""
    t, f = svc, svc.f
    f.cfg['roster'] = str(write_roster(t, extra='    clear_after_result: true\n'))
    f.roster_tick()
    rid, answer = ask(t, 'up')
    assert answer['status'] == 'accepted', answer
    res = result(f, rid)
    assert res['status'] == 'rejected' and res['reason'].startswith('program: '), res
    assert 'claude_hook_delivery: false' in res['reason']
    assert not subfleet.active_children(f.run_dir)


@RETIRED
def test_named_child_with_clear_and_typed_delivery_runs_with_its_own_switches(svc):
    """§29 a): clear_after_result + claude_hook_delivery: false comes up; the child's config carries
    the entry's switches and passes the run-config check (a named service child has lifecycle None)."""
    t, f = svc, svc.f
    f.cfg['roster'] = str(write_roster(
        t, extra='    clear_after_result: true\n    clear_timeout: 30\n    claude_hook_delivery: false\n'))
    f.roster_tick()
    rid, rec, cf = bring_up(t)
    assert result(f, rid)['status'] == 'ok'
    child_cfg = fleet.load(Path(rec['run']) / 'config.json')
    assert child_cfg['lifecycle'] is None and child_cfg['mode'] == 'service'
    assert (child_cfg['clear_after_result'], child_cfg['clear_timeout'], child_cfg['claude_hook_delivery']) \
        == (True, 30, False)
    assert runtime_v2.validate_config(child_cfg) == 2
    assert clear_after_result.enabled(child_cfg, 'ops-1')
    loose = dict(child_cfg, named=None)  # outside a named child, lifecycle None still refuses §28
    with pytest.raises(ValueError):
        clear_after_result.validate(loose)


def test_named_entry_worker_domains_is_validated(tmp_path):
    """§29 d): worker_domains is the child's own --worker-domains floor: a list of names, [] allowed."""
    (tmp_path / 'sup.md').write_text('x')
    (tmp_path / 'child.yaml').write_text('workers:\n  - name: a\n')
    base = 'workers:\n  - name: worker-1\n  - name: worker-2\n'
    good = tmp_path / 'good.yaml'
    good.write_text(base + 'subfleets:\n'
                    '  - {name: mem, supervisor_slot: worker-1, supervisor_role: sup.md, roster: child.yaml,\n'
                    '     worker_domains: []}\n'
                    '  - {name: plain, supervisor_slot: worker-2, supervisor_role: sup.md, roster: child.yaml}\n')
    mem, plain = roster.load_subfleets(good)
    assert mem['worker_domains'] == [] and 'worker_domains' not in plain
    bad = tmp_path / 'bad.yaml'
    bad.write_text(base + 'subfleets:\n'
                   '  - {name: mem, supervisor_slot: worker-1, supervisor_role: sup.md, roster: child.yaml,\n'
                   '     worker_domains: futures}\n')
    with pytest.raises(ValueError) as exc:
        roster.load_entries(bad)
    assert 'worker_domains must be a list of domain names' in str(exc.value)


def domain_parent(t):
    from test_kit import make_kit
    t.f.cfg.update(worker_kit=str(make_kit(t.tmp / 'kit')), worker_domains=['us_options'])


@pytest.mark.parametrize('extra, want', [('', ['us_options']), ('    worker_domains: []\n', []),
                                         ('    worker_domains: [research_web]\n', ['research_web'])])
def test_named_child_worker_domains_replaces_the_parents_floor_or_inherits_it(svc, extra, want):
    """§29 d) (user 10-14): an entry's worker_domains replaces the parent's floor ([] = none, e.g. a
    memory-extraction child that needs no trading skills); not written = the parent's, as before."""
    t, f = svc, svc.f
    domain_parent(t)
    f.cfg['roster'] = str(write_roster(t, extra=extra))
    f.roster_tick()
    rid, rec, cf = bring_up(t)
    assert result(f, rid)['status'] == 'ok'
    assert fleet.load(Path(rec['run']) / 'config.json')['worker_domains'] == want
    assert cf.floor_domains() == want


def test_named_child_worker_domains_not_in_the_kit_is_rejected_by_program(svc):
    t, f = svc, svc.f
    domain_parent(t)
    f.cfg['roster'] = str(write_roster(t, extra='    worker_domains: [nope]\n'))
    f.roster_tick()
    rid, answer = ask(t, 'up')
    assert answer['status'] == 'accepted', answer
    res = result(f, rid)
    assert res['status'] == 'rejected' and res['reason'].startswith('program: '), res
    assert 'worker_domains not in the domain table' in res['reason'] and 'nope' in res['reason']
    assert not subfleet.active_children(f.run_dir)


@pytest.mark.parametrize('switch', ['', 'advisor: off\n', 'advisor: on\n'])
def test_named_child_roster_has_its_own_advisor_switch_default_off(svc, switch):
    """§29 c) (user 10-14): a sub-fleet roster may say advisor: on | off; not written = off. The parent's
    switch never reaches the child (child_config drops it)."""
    t, f = svc, svc.f
    f.cfg['advisor'] = True  # the parent's own (in-memory) switch is on
    path = write_roster(t)
    (t.tmp / 'child.yaml').write_text('workers:\n  - name: ops-1\n' + switch)
    f.cfg['roster'] = str(path)
    f.roster_tick()
    rid, answer = ask(t, 'up')
    assert answer['status'] == 'accepted', answer
    supervisor_answers(t, rid)
    subfleet.tick(f)
    rec = subfleet.record(f.run_dir / 'sub' / 'ops')
    assert 'advisor' not in fleet.load(Path(rec['run']) / 'config.json')
    cf = child_daemon(t, rec)  # the child daemon's first boot reads its own roster
    on = switch == 'advisor: on\n'
    assert cf.advisor_online() is on
    assert (cf.r.hget(fleet.SERVICE, 'advisor') == 'on') is on
    # advisor on: the child's Claude worker starts held for its first configuration sheet (§27)
    assert (cf.agent('ops-1').get('launch_ready') == '0') is on


def test_named_child_roster_advisor_value_is_checked_with_the_parent_roster(tmp_path):
    (tmp_path / 'sup.md').write_text('x')
    (tmp_path / 'child.yaml').write_text('workers:\n  - name: a\nadvisor: maybe\n')
    bad = tmp_path / 'bad.yaml'
    bad.write_text('workers:\n  - name: worker-1\nsubfleets:\n'
                   '  - {name: ops, supervisor_slot: worker-1, supervisor_role: sup.md, roster: child.yaml}\n')
    with pytest.raises(ValueError, match='advisor must be on or off'):
        roster.load_entries(bad)


# §5.1 + §5.2
def test_external_up_is_gated_approved_and_answered_with_the_child_address(svc):
    t, f = svc, svc.f
    rid, answer = ask(t, 'up', 'nope')
    assert answer['status'] == 'accepted', answer
    assert result(f, rid)['status'] == 'rejected' and result(f, rid)['reason'].startswith('program: ')
    assert not f.r.llen(fleet.REQQ)  # never a service request queue entry
    rid, rec, cf = bring_up(t)
    res = result(f, rid)
    child_cfg = fleet.load(Path(rec['run']) / 'config.json')
    assert res['status'] == 'ok' and res['state'] == 'up' and res['redis_socket'] == child_cfg['redis_socket']
    assert res['run_dir'] == str(f.run_dir / 'sub' / 'ops')
    assert f.agent('worker-1')['state'] == 'delegated' and child_cfg['mode'] == 'service'
    assert child_cfg['lifecycle'] is None and child_cfg['workers'] == ['ops-1']
    assert 'ROLE-MARKER-OPS' in (fleet.agent_cwd(f.cfg, 'worker-1') / 'AGENTS.md').read_text()
    # up again (new id): answered by the program from state, same address
    again, _ = ask(t, 'up')
    assert {k: result(f, again)[k] for k in ('status', 'redis_socket')} == {'status': 'ok',
                                                                            'redis_socket': res['redis_socket']}
    # the same id again is a duplicate
    _, dup = ask(t, 'up', rid=again)
    assert dup['status'] == 'rejected' and 'duplicate' in dup['reason']
    # a service request sent to the child's socket reaches the child supervisor (the parent's worker session)
    spec = dict(id='q1', payload={'message': 'check the gateway'}, reply={'required': ['reply']})
    assert drive(t, lambda: fleet.request(child_cfg, dict(kind='request', spec=spec)))['status'] == 'accepted'
    step(t)
    assert any('check the gateway' in x for x in supervisor_texts(cf))  # §48: its hook mailbox, never typed
    assert not [x for p, x in t.world.sent if p == rec['pane']]
    assert not subfleet.child_result(rec['run'])  # a service child never writes result.json


# §5.3
def test_reject_silence_and_busy_slot(svc, monkeypatch):
    t, f = svc, svc.f
    rid, _ = ask(t, 'up')
    supervisor_answers(t, rid, 'reject', 'not now')
    assert result(f, rid) == dict(result(f, rid), status='rejected', reason='supervisor: not now')
    silent, _ = ask(t, 'up')
    for _ in range(3):
        subfleet.tick(f)
    assert result(f, silent) is None and not subfleet.active_children(f.run_dir)
    assert f.agent('worker-1')['state'] == 'idle'
    step(t)
    prompt = given(t, f, 'supervisor')[-1]  # Codex supervisor: its queued delivery (no typing)
    f.setf('worker-1', state='busy')
    hook(t, f, 'supervisor', json.dumps({'status': 'RUNNING', 'dispatch': [], 'skip': [],
                                         'subfleet': {'id': silent, 'decision': 'approve'}}), prompt=prompt)
    subfleet.tick(f)
    assert result(f, silent) is None and not subfleet.active_children(f.run_dir)  # waits for the slot
    monkeypatch.setattr(named_subfleet, 'SLOT_WAIT', 0)
    subfleet.tick(f)
    assert result(f, silent) == dict(result(f, silent), status='timeout', reason='slot busy')


# §5.4 + §5.6a
@RETIRED
def test_down_drains_returns_the_same_session_and_the_name_comes_back_again(svc):
    t, f = svc, svc.f
    typed_return(t)  # same-session return: typed path only (see typed_return)
    sid = f.agent('worker-1')['pane']
    worker_rules = (fleet.agent_cwd(f.cfg, 'worker-1') / 'CLAUDE.md').read_text()
    for round_ in range(3):  # more ups than max_attempts (1): never refused, one directory
        _, rec, cf = bring_up(t)
        assert rec['run'] == str(f.run_dir / 'sub' / 'ops') and rec['attempt'] == round_ + 1
        child_cfg = fleet.load(Path(rec['run']) / 'config.json')
        cr = fleet.connect(child_cfg)
        cr.rpush(fleet.REQQ, 'in-flight')  # an unanswered child request: the down waits for it
        rid, _ = ask(t, 'down')
        supervisor_answers(t, rid)
        subfleet.tick(f)
        assert named_subfleet.state(f, 'ops')['state'] == 'draining'
        assert subfleet.record(rec['run'])['state'] == 'running' and result(f, rid) is None
        cr.lrem(fleet.REQQ, 0, 'in-flight')
        cr.close()
        subfleet.tick(f)
        assert subfleet.record(rec['run'])['state'] == 'stopping'
        stop_child_like_daemon(cf)
        subfleet.tick(f)
        rec = subfleet.record(rec['run'])
        assert rec['state'] == 'down' and result(f, rid) == dict(result(f, rid), status='ok', state='down')
        assert named_subfleet.state(f, 'ops')['state'] == 'down' and redis_down(child_cfg)
        assert (fleet.agent_cwd(f.cfg, 'worker-1') / 'CLAUDE.md').read_text() == worker_rules
        info = f.agent('worker-1')
        assert info['pane'] == sid and info['generation'] == '0'
        back = [x for p, x in t.world.sent if p == sid][-1]
        assert parent_hook(t, 'READY', back)['status'] == 'ready' and f.agent('worker-1')['state'] == 'idle'
        assert not any('[Sub-fleet' in x for x in supervisor_texts(f))  # no failed / completed notice
        again, _ = ask(t, 'down')
        assert result(f, again)['status'] == 'ok'  # already down: answered from state
    assert len(list((f.run_dir / 'sub' / 'ops' / 'archive').glob('*/*'))) == 2
    assert [d.name for d in subfleet.children_dirs(f.run_dir)] == ['ops']


def hook_yielded(f):
    """The worker's Stop hook that waited before the delegation yielded to the typed role change (claude_hook
    OperatorQueue): no hook of this channel waits any more (hook_sim would otherwise still count it armed)."""
    hook_sim._armed.pop(f.v2.hook_keys('worker-1', int(f.agent('worker-1')['generation']))['mailbox'], None)


def returning_rewake(t, monkeypatch):
    """The session's own asyncRewake from its last Stop while it supervised the sub-fleet (real
    ``fleet.claude_rewake`` in a thread). Grace / stopping shortened; the rest is the real fallback."""
    from fleetmux import claude_hook
    monkeypatch.setattr(claude_hook, 'GRACE', 0)
    # The rewake runs in-process, so its "Claude Code parent" is pytest's parent: a detached pytest
    # (nohup ... &, ppid 1) reads as orphaned and the rewake quits at once. Pin a live-looking parent.
    monkeypatch.setattr(fleet.os, 'getppid', lambda: 424242)
    f = t.f
    thread = t.threads[(str(f.run_dir), 'worker-1', 0)]
    raw = json.dumps({'session_id': thread, 'transcript_path': str(t.claude / (thread + '.jsonl'))})
    f.r.delete(f.v2.hook_keys('worker-1', 0)['stopping'])  # its last Stop finished settling
    hook_yielded(f)
    box = {}
    th = threading.Thread(target=lambda: box.setdefault('text', fleet.claude_rewake(f.path, 'worker-1', '0', raw)),
                          daemon=True)
    th.start()
    return th, box, thread


def down_and_collect(t, cf):
    rid, _ = ask(t, 'down')
    supervisor_answers(t, rid)
    subfleet.tick(t.f)  # approved: draining
    subfleet.tick(t.f)  # quiet: stop posted to the child
    stop_child_like_daemon(cf)
    subfleet.tick(t.f)  # collected
    return rid


def launches(f):
    """Each launch of this fleet's manager, with its prompt file's text when the prompt went by file."""
    out = []
    for kw in getattr(f.mgr, 'spawned', []):
        path = kw.get('prompt_file')
        out.append(json.dumps(kw, ensure_ascii=False) + (Path(path).read_text() if path and Path(path).is_file() else ''))
    return out


def test_named_down_on_the_hook_channel_hands_over_through_the_sessions_own_note(svc, monkeypatch):
    """§29 (user 10-14): return = the returned session writes a handoff note (design §4 rotation on its own
    hook channel, no typing: its rewake fallback hands the dump prompt over), then worker-1's next
    generation reads it; a late Stop of the old session settles nothing."""
    t, f = svc, svc.f
    sid = f.agent('worker-1')['pane']
    _, rec, cf = bring_up(t)
    th, box, thread = returning_rewake(t, monkeypatch)
    time.sleep(1.5)
    assert th.is_alive(), 'the rewake must wait while the session is delegated, not quit'
    before = len(t.world.sent)
    rid = down_and_collect(t, cf)
    assert subfleet.record(rec['run'])['state'] == 'down' and result(f, rid)['status'] == 'ok'
    assert not any('[Sub-fleet' in x for x in supervisor_texts(f))
    info = f.agent('worker-1')
    assert info['state'] == 'handoff' and info['generation'] == '0' and info['pane'] == sid
    th.join(30)
    assert not th.is_alive() and 'Sub-fleet ops is down' in box['text'] and info['handoff_draft'] in box['text']
    assert not [x for p, x in t.world.sent[before:] if p == sid], 'a hook-channel worker is never typed into'
    # Claude runs the rewake-delivered turn: writes the note, its Stop (now the parent's channel) settles it
    Path(info['handoff_draft']).write_text(
        '## Done (facts)\n- ops answered the morning requests\n## In progress\nnone\n'
        '## Next steps (in order)\n1. wait for the next request\n')
    transcript = t.claude / (thread + '.jsonl')
    with transcript.open('a') as fh:
        rows = [{'type': 'user', 'origin': {'kind': 'task-notification'}, 'promptId': 'p-ret',
                 'message': {'role': 'user', 'content': '<task-notification>\n<summary>Stop hook feedback</summary>\n'
                             '</task-notification>\n<system-reminder>\nFleet delivery: ' + box['text']
                             + '\n</system-reminder>'}},
                {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'handoff written'}]}}]
        fh.write('\n'.join(json.dumps(x) for x in rows) + '\n')
    raw = json.dumps({'session_id': thread, 'transcript_path': str(transcript), 'last_assistant_message': 'handoff written'})
    drive(t, lambda: hook_sim.claude_stop(f.path, 'worker-1', '0', raw))
    step(t)
    info = f.agent('worker-1')
    assert info['generation'] == '1' and info['state'] == 'starting' and info['pane'] != sid
    assert sid not in t.world.alive and not info.get('subfleet_return')
    note = next((f.run_dir / 'handoffs').glob('worker-1-g0-*.md'))
    text = note.read_text()
    assert 'ops answered the morning requests' in text  # the model's sections over the skeleton (design §4)
    done = [x[1] for x in f.r.xrange(f.v2.bus.audit_key) if x[1].get('event') == 'rotate.done']
    assert done and done[-1].get('source') == 'model'
    launch = launches(f)[-1]
    assert 'sub-fleet ops' in launch and str(note) in launch and 'resumed after an interruption' not in launch
    assert f.r.hget(fleet.SERVICE, 'paused') != '1'
    late = claude_turn(t, f.path, 'worker-1', 0, thread, 'anything', 'late reply')  # old session's late Stop
    assert late['status'] == 'stale'
    assert hook(t, f, 'worker-1', 'READY', prompt='startup')['status'] == 'ready'
    rid2, _ = ask(t, 'up')  # the name comes back, now on the new generation's session
    supervisor_answers(t, rid2)
    subfleet.tick(f)
    rec2 = subfleet.record(f.run_dir / 'sub' / 'ops')
    assert rec2['attempt'] == 2 and rec2['pane'] == info['pane'] and rec2['generation'] == 1


def test_named_return_handoff_nobody_takes_falls_back_to_the_skeleton_without_holding(svc):
    """§29: no rewake left to wake the returned session: after RETURN_WAKE_S the daemon's skeleton note
    (sub-fleet name, up / down time, answered and open request ids) goes to the next generation; the
    untaken handoff ran nothing, so the fleet is not held / paused."""
    t, f = svc, svc.f
    sid = f.agent('worker-1')['pane']
    _, rec, cf = bring_up(t)
    cr = fleet.connect(fleet.load(Path(rec['run']) / 'config.json'))
    cr.rpush(fleet.RESULTS, json.dumps({'id': 'req-answered-1', 'status': 'ok'}))
    cr.hset(fleet.REQ + 'req-open-2', mapping={'spec': '{}'})
    cr.close()
    hook_yielded(f)  # and no rewake is left to take the handoff
    down_and_collect(t, cf)
    info = f.agent('worker-1')
    assert info['state'] == 'handoff' and info['generation'] == '0'
    subfleet.tick(f)  # within RETURN_WAKE_S: still waiting for the session's rewake
    assert f.agent('worker-1')['generation'] == '0'
    back = json.loads(info['subfleet_return'])
    f.setf('worker-1', subfleet_return=json.dumps(dict(back, at=back['at'] - named_subfleet.RETURN_WAKE_S - 1)))
    subfleet.tick(f)
    info = f.agent('worker-1')
    assert info['generation'] == '1' and info['state'] == 'starting' and sid not in t.world.alive
    assert f.r.hget(fleet.SERVICE, 'paused') != '1'
    events = [x[1].get('event') for x in f.r.xrange(f.v2.bus.audit_key)]
    assert 'subfleet.return_untaken' in events and 'execution.held' not in events
    text = next((f.run_dir / 'handoffs').glob('worker-1-g0-*.md')).read_text()
    assert 'named sub-fleet ops' in text and 'req-answered-1 ok' in text and 'req-open-2' in text
    launch = launches(f)[-1]
    assert 'sub-fleet ops' in launch and 'Read the handoff note' in launch and 'resumed after an interruption' not in launch


def test_named_return_with_the_session_gone_starts_the_next_generation_on_a_skeleton_note(svc):
    """§29: the delegated session died with the sub-fleet: nobody can write, the skeleton note goes
    straight to the next generation (never "resumed after an interruption")."""
    t, f = svc, svc.f
    sid = f.agent('worker-1')['pane']
    _, rec, cf = bring_up(t)
    t.world.alive.discard(sid)  # the session is gone
    down_and_collect(t, cf)
    info = f.agent('worker-1')
    assert info['generation'] == '1' and info['state'] == 'starting' and not info.get('subfleet_return')
    note = f.run_dir / 'handoffs' / 'worker-1-g0-return.md'
    assert 'named sub-fleet ops' in note.read_text()
    launch = launches(f)[-1]
    assert str(note) in launch and 'sub-fleet ops' in launch and 'resumed after an interruption' not in launch


@RETIRED
def test_drain_timeout_takes_the_child_back_anyway(svc):
    t, f = svc, svc.f
    typed_return(t)  # same-session return: typed path only (see typed_return)
    _, rec, cf = bring_up(t)
    cr = fleet.connect(fleet.load(Path(rec['run']) / 'config.json'))
    cr.rpush(fleet.REQQ, 'stuck')
    cr.close()
    rid, _ = ask(t, 'down')
    supervisor_answers(t, rid)
    subfleet.tick(f)
    assert subfleet.record(rec['run'])['state'] == 'running'
    f.r.hset(named_subfleet.HASH + 'ops', mapping={'drain_since': time.time() - 61})
    subfleet.tick(f)
    assert subfleet.record(rec['run'])['state'] == 'stopping'
    assert result(f, rid) == dict(result(f, rid), status='timeout', reason='drain exceeded')
    assert any(x[1].get('event') == 'subfleet.drain_timeout' for x in f.r.xrange(f.v2.bus.audit_key))
    stop_child_like_daemon(cf)
    subfleet.tick(f)
    assert subfleet.record(rec['run'])['state'] == 'down' and f.agent('worker-1')['pane'] == rec['pane']


# §5.5
@RETIRED
def test_autostart_delegates_on_ready_revives_and_stops_on_crashloop(svc):
    t, f = svc, svc.f
    typed_return(t)  # same-session return: typed path only (see typed_return)
    write_roster(t, autostart=True)
    f.roster_tick()
    rid, _ = ask(t, 'down')
    assert result(f, rid)['reason'].startswith('program: ops is autostart')
    subfleet.tick(f)  # worker-1 is idle and READY: delegated at once, no supervisor involved
    rec = subfleet.record(f.run_dir / 'sub' / 'ops')
    assert rec['state'] == 'running' and rec['autostart'] is True
    _, cf = adopt_child(t)
    subfleet.tick(f)
    assert named_subfleet.state(f, 'ops')['state'] == 'up'
    child_cfg = fleet.load(Path(rec['run']) / 'config.json')
    mgr = t.world.mgrs['cmux-fleet:ops']
    for crash in range(1, 4):
        die(cf)
        subfleet.shutdown_redis(child_cfg)
        rec = subfleet.record(rec['run'])
        rec['launched'] = rec['created'] = time.time() - subfleet.LAUNCH_GRACE - 1
        subfleet.save_record(rec['run'], rec)
        windows = len(getattr(mgr, 'windows', []))
        subfleet.tick(f)
        rec = subfleet.record(rec['run'])
        if crash < 3:  # relaunched before the collect branch: Redis back, daemon --resume
            assert rec['state'] == 'running' and not redis_down(child_cfg)
            assert '--resume' in mgr.windows[-1][1] and len(mgr.windows) == windows + 1
            cf = fleet.Fleet(Path(rec['run']) / 'config.json', fleet.connect(child_cfg), t.world.manager(child_cfg),
                             sessions=t.sessions)
            cf.lock = (Path(rec['run']) / 'daemon.lock').open('a+')
            fcntl.flock(cf.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            t.fleets.append(cf)
        else:
            assert rec['state'] == 'failed' and rec['outcome'] == 'crashloop'  # collected, not relaunched
    h = named_subfleet.state(f, 'ops')
    assert h['crashloop'] == '1' and h['state'] == 'down'
    assert any(x[1].get('event') == 'subfleet.crashloop' for x in f.r.xrange(f.v2.bus.audit_key))
    assert f.agent('worker-1')['pane'] == rec['pane']
    parent_hook(t, 'READY', [x for p, x in t.world.sent if p == rec['pane']][-1])
    subfleet.tick(f)
    assert not subfleet.active_children(f.run_dir)  # waits for an operator


# §5.5 (resume) + §5.7
@RETIRED
def test_parent_resume_restarts_autostart_and_takes_back_external_ones(svc):
    t, f = svc, svc.f
    typed_return(t)  # same-session return: typed path only (see typed_return)
    _, rec, cf = bring_up(t)
    die(cf)
    subfleet.resume_children(f, launch=lambda *a: pytest.fail('an external sub-fleet is never relaunched'))
    assert subfleet.record(rec['run'])['state'] == 'down' and named_subfleet.state(f, 'ops')['state'] == 'down'
    assert f.agent('worker-1')['pane'] == rec['pane'] and redis_down(fleet.load(Path(rec['run']) / 'config.json'))
    parent_hook(t, 'READY', [x for p, x in t.world.sent if p == rec['pane']][-1])
    write_roster(t, autostart=True)
    f.roster_tick()
    subfleet.tick(f)
    rec = subfleet.record(rec['run'])
    _, cf = adopt_child(t)
    die(cf)
    launched = []
    subfleet.resume_children(f, launch=lambda path, cfg, mgr: launched.append(path))
    assert launched == [Path(rec['run']) / 'config.json']
    assert subfleet.stop_children(f, wait=1)  # parent stop: taken back, no orphan Redis
    child_cfg = fleet.load(Path(rec['run']) / 'config.json')
    assert redis_down(child_cfg) and named_subfleet.state(f, 'ops')['state'] == 'down'
    assert subfleet.record(rec['run'])['state'] == 'down'


# §5.6
def test_rotation_gate_rotates_before_delegating(svc, monkeypatch):
    t, f = svc, svc.f
    usage = {'worker-1': (90, 100)}
    monkeypatch.setattr(f, 'usage', lambda name: usage.get(name, (0, 0)))
    rotations = []

    def rotate(name, used, window):
        rotations.append(name)
        f.setf(name, state='handoff')
    monkeypatch.setattr(f, 'start_rotation', rotate)
    f.setf('worker-1', delivered=3)
    rid, _ = ask(t, 'up')
    supervisor_answers(t, rid)
    subfleet.tick(f)
    assert rotations == ['worker-1'] and not subfleet.active_children(f.run_dir)
    subfleet.tick(f)
    assert rotations == ['worker-1']  # handing off: not idle, no second rotation
    usage['worker-1'] = (5, 100)  # the next generation is READY with a fresh session
    f.setf('worker-1', state='idle', generation=1, label='worker-1.1', delivered=0)
    subfleet.tick(f)
    assert subfleet.record(f.run_dir / 'sub' / 'ops')['generation'] == 1


@RETIRED
def test_autostart_supervisor_over_threshold_goes_down_and_rotates_once(svc, monkeypatch):
    t, f = svc, svc.f
    typed_return(t)  # same-session return: typed path only (see typed_return)
    usage = {'worker-1': (5, 100)}
    monkeypatch.setattr(f, 'usage', lambda name: usage.get(name, (0, 0)))
    rotations = []
    monkeypatch.setattr(f, 'start_rotation', lambda name, used, window: rotations.append(name)
                        or f.setf(name, state='handoff'))
    write_roster(t, autostart=True)
    f.roster_tick()
    subfleet.tick(f)
    rec, cf = adopt_child(t)
    subfleet.tick(f)
    assert named_subfleet.state(f, 'ops')['state'] == 'up'
    usage['worker-1'] = (95, 100)
    f.r.hset(named_subfleet.HASH + 'ops', mapping={'usage_at': 0})
    subfleet.tick(f)  # over threshold while idle: draining
    assert any(x[1].get('event') == 'subfleet.rotate' for x in f.r.xrange(f.v2.bus.audit_key))
    assert named_subfleet.state(f, 'ops')['state'] == 'draining'
    subfleet.tick(f)  # quiet: stop posted
    assert subfleet.record(rec['run'])['state'] == 'stopping'
    stop_child_like_daemon(cf)
    subfleet.tick(f)
    parent_hook(t, 'READY', [x for p, x in t.world.sent if p == rec['pane']][-1])
    subfleet.tick(f)  # the same session is never delegated again: rotation first
    assert rotations == ['worker-1'] and not subfleet.active_children(f.run_dir)
    subfleet.tick(f)
    assert rotations == ['worker-1']


# §5.8
@RETIRED
def test_hot_roster_declare_remove_cancel_and_redeclare(svc):
    t, f = svc, svc.f
    typed_return(t)  # same-session return: typed path only (see typed_return)
    _, rec, cf = bring_up(t)
    write_roster(t, declare=False)
    f.roster_tick()
    assert named_subfleet.state(f, 'ops')['state'] == 'draining' and 'ops' not in named_subfleet.declared(f)
    write_roster(t)  # added back before it went down: cancelled
    f.roster_tick()
    assert named_subfleet.state(f, 'ops')['state'] == 'up'
    subfleet.tick(f)
    assert subfleet.record(rec['run'])['state'] == 'running'
    write_roster(t, declare=False)
    f.roster_tick()
    subfleet.tick(f)
    stop_child_like_daemon(cf)
    subfleet.tick(f)
    assert subfleet.record(rec['run'])['state'] == 'down'
    rid, _ = ask(t, 'up')
    assert result(f, rid)['status'] == 'rejected'  # no longer declared
    parent_hook(t, 'READY', [x for p, x in t.world.sent if p == rec['pane']][-1])
    write_roster(t)
    f.roster_tick()
    bring_up(t)  # declared again (the name was used in this run): not refused


# §5.9
def test_child_workers_take_entry_values_or_builtin_defaults_never_the_parents(svc):
    t, f = svc, svc.f
    f.cfg.update(claude='/opt/ro-wrapper/claude-read-only', stall_timeout=150, request_timeout=77, max_busy=4)
    [entry] = named_subfleet.declared(f).values()
    cfg = subfleet.child_config(f, 'worker-1', 'ops', 1, t.tmp / 'c', 'pane-x', None, {}, 'n', named=entry)
    assert 'claude' not in cfg and cfg['stall_timeout'] == fleet.DEFAULTS['stall_timeout']
    assert cfg['request_timeout'] == fleet.DEFAULTS['request_timeout'] and cfg['max_busy'] == 0
    assert cfg['worker_model'] == fleet.DEFAULTS['worker_model'] and cfg['worker_cli'] == 'claude'
    entry = dict(entry, claude='/opt/child/claude', worker_model='haiku', stall_timeout=600, request_timeout=900,
                 max_busy=2)
    cfg = subfleet.child_config(f, 'worker-1', 'ops', 1, t.tmp / 'c', 'pane-x', None, {}, 'n', named=entry)
    assert (cfg['claude'], cfg['worker_model'], cfg['stall_timeout'], cfg['request_timeout'], cfg['max_busy']) == (
        '/opt/child/claude', 'haiku', 600, 900, 2)
    assert f.cfg['claude'] == '/opt/ro-wrapper/claude-read-only'  # the delegated slot itself is unchanged


# §5.10
def test_child_self_pause_shows_as_paused_in_the_parent_hash(svc):
    t, f = svc, svc.f
    _, rec, cf = bring_up(t)
    cf.pause('supervisor gave no valid decision JSON three times in a row')
    subfleet.tick(f)
    assert named_subfleet.state(f, 'ops')['state'] == 'paused'
    rid, _ = ask(t, 'up')
    assert result(f, rid)['state'] == 'paused'
    cf.resume()
    subfleet.tick(f)
    assert named_subfleet.state(f, 'ops')['state'] == 'up'


def test_child_roster_names_need_a_number_when_declared_not_when_already_declared(svc):
    # docs/design.md §3 (2026-10-28): the child roster's workers become slots when the sub-fleet comes up.
    # A newly declared entry is checked; an entry the run already declared (stored before the rule) is not.
    t, f = svc, svc.f
    (t.tmp / 'legacy.yaml').write_text('workers:\n  - name: analyst\n')
    ops2 = '  - {name: ops2, supervisor_slot: worker-2, supervisor_role: sup.md, roster: legacy.yaml}\n'
    path = write_roster(t, extra=ops2)
    f.roster_tick()
    rejected = [x for _, x in f.r.xrange(f.v2.bus.audit_key) if x.get('event') == 'roster.rejected']
    errors = json.loads(rejected[-1]['errors'])
    assert any(e.startswith('subfleets[1] (ops2): ') and 'workers[0] (analyst)' in e and 'rename it analyst-1' in e
               for e in errors)
    assert 'ops2' not in named_subfleet.declared(f) and 'ops' in named_subfleet.declared(f)
    f.cfg['subfleets'] = roster.load_subfleets(path)  # a run that declared ops2 before the rule
    f.roster_stamp = f.roster_digest = None  # the daemon's first tick after a resume re-reads the roster
    f.roster_tick()
    assert 'ops2' in named_subfleet.declared(f)


# ------------------------------------------------------------------ adopted supervisor on the hook channel
# The delegated pane keeps the parent's hook command (worker-1, generation 0). A run started with the switch
# (start pins ``adopted_supervisor_hook_delivery``, like ``claude_supervisor_hook_delivery``) never types into
# it: the child posts to its own Redis supervisor mailbox and that pinned hook, routed by ``delegated_route``,
# waits there and takes it. These tests run the REAL blocking wait (``claude_hook_next``) for the moments the
# route changes; hook_sim stands in only for the ordinary turns in between.
REAL_HOOK_NEXT = fleet.claude_hook_next  # captured at import, before hook_sim stands in for it


def route_on(t):
    t.f.cfg['adopted_supervisor_hook_delivery'] = True
    fleet.save(t.f.path, dict(fleet.load(t.f.path), adopted_supervisor_hook_delivery=True))


def waiting_hook(t, monkeypatch):
    """The real Stop-hook wait of worker-1's session, started from the parent's pinned hook identity."""
    monkeypatch.setattr(fleet.os, 'getppid', lambda: 424242)  # a live-looking Claude Code parent (see returning_rewake)
    f = t.f
    hook_yielded(f)  # the stand-in no longer counts the parent mailbox as armed: this thread is the waiting hook
    gen = int(f.agent('worker-1')['generation'])
    thread = t.threads[(str(f.run_dir), 'worker-1', gen)]
    own = fleet.load(f.path)
    box = {}

    def run():
        box['v'] = REAL_HOOK_NEXT(own, 'worker-1', gen, thread, time.monotonic() + 120,
                                  transcript=str(t.claude / (thread + '.jsonl')), own=(own, 'worker-1', gen))
    th = threading.Thread(target=run, daemon=True)
    th.start()
    return th, box, thread


def hooked_turn(t, slot, gen, thread, reason, text, cfg_path=None):
    """Claude runs the turn a Stop-hook block started (its isMeta feedback row), then the pane's Stop hook fires."""
    transcript = t.claude / (thread + '.jsonl')
    transcript.parent.mkdir(parents=True, exist_ok=True)
    rows = [hook_sim.user_row(reason, True), {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}}]
    with transcript.open('a') as fh:
        fh.write('\n'.join(json.dumps(x) for x in rows) + '\n')
    raw = json.dumps({'session_id': thread, 'transcript_path': str(transcript), 'last_assistant_message': text})
    return drive(t, lambda: hook_sim.claude_stop(cfg_path or t.f.path, slot, str(gen), raw))


def adopt_by_hook(t, monkeypatch, rec, waiting=None):
    """Child first boot while worker-1's hook waits: the role change arrives as that hook's block decision."""
    th, box, thread = waiting or waiting_hook(t, monkeypatch)
    gen = int(t.f.agent('worker-1')['generation'])  # the delegated pane's generation (its pinned hooks)
    cf = child_daemon(t, rec)
    assert not received(t.world, rec['pane']), 'the role change was typed into the adopted pane'
    for w in cf.workers:
        assert hook(t, cf, w, 'READY', prompt='startup')['status'] == 'ready'
    th.join(30)
    assert not th.is_alive() and box.get('v'), 'the waiting hook never took the role change'
    role = box['v']['reason']
    assert box['v']['decision'] == 'block' and 'sub-fleet' in role and 'READY' in role
    # Claude runs it as a "Stop hook feedback" turn; its Stop (still the parent's command) lands in the child
    assert hooked_turn(t, 'worker-1', gen, thread, role, 'READY')['status'] == 'ready'
    assert cf.agent('supervisor')['thread'] == thread and cf.agent('supervisor')['runtime_ready'] == '1'
    return cf, thread


def test_adopted_supervisor_takes_role_change_and_task_through_the_parent_hook(tree, monkeypatch):
    t, f = tree, tree.f
    route_on(t)
    sid = f.agent('worker-1')['pane']
    waiting = waiting_hook(t, monkeypatch)  # worker-1's READY Stop hook waits on the PARENT mailbox
    time.sleep(1)
    rec, out = spawn_m0(t)  # delegated while it waits: it must move to the child's supervisor mailbox
    assert fleet.load(Path(rec['run']) / 'config.json')['adopt']['hook'] is True
    cf, thread = adopt_by_hook(t, monkeypatch, rec, waiting)
    step(t)
    task = hook_sim.handed_to(cf, 'supervisor')[-1]  # the child's supervisor mailbox, child Redis
    assert 'Research five sources and merge them' in task
    decision = json.dumps({'status': 'RUNNING', 'completion': 10, 'reason': 'split', 'dispatch': [
        {'to': w, 'instruction': 'Write ' + w + '/part.md with two sources each'} for w in cf.workers]})
    assert hooked_turn(t, 'worker-1', 0, thread, task, decision)['status'] == 'committed'
    step(t)
    for w in cf.workers:
        assert any('part.md' in x for x in given(t, cf, w))
    assert not received(t.world, sid), 'nothing is ever typed into the adopted pane'
    assert not hook_sim.texts('worker-1', f.v2.bus.prefix), 'nothing went through the parent worker-1 mailbox'


def test_adopted_supervisor_with_work_in_flight_is_reattached_on_child_resume(tree, monkeypatch):
    t, f = tree, tree.f
    route_on(t)
    rec, _ = spawn_m0(t)
    cf, _ = adopt_by_hook(t, monkeypatch, rec)
    step(t)
    assert cf.v2.bus.slot_state('supervisor').get('current_delivery')  # the task is in flight
    die(cf)
    cfg = fleet.load(Path(rec['run']) / 'config.json')
    again = fleet.Fleet(Path(rec['run']) / 'config.json', fleet.connect(cfg), t.world.manager(cfg), sessions=t.sessions)
    t.fleets.append(again)
    again.resume_requested = True
    again.v2.resume_run()
    # the pane belongs to the parent: never a fresh supervisor process in its place
    assert again.agent('supervisor')['pane'] == rec['pane'] and again.agent('supervisor')['generation'] == '0'


def test_late_stop_of_another_generation_is_not_routed_into_the_child(tree, monkeypatch):
    t, f = tree, tree.f
    route_on(t)
    rec, _ = spawn_m0(t)
    cf = child_daemon(t, rec)
    thread = str(uuid.uuid4())
    assert hooked_turn(t, 'worker-1', 5, thread, 'old work', 'late reply')['status'] == 'stale'
    k = cf.v2.hook_keys('supervisor', 0)
    assert not cf.r.exists(k['stopping']) and not cf.r.exists(k['bind'])
    assert cf.agent('supervisor')['state'] == 'starting' and not cf.agent('supervisor').get('thread')


def test_hook_waiting_on_a_vanished_child_takes_nothing_and_ends_with_the_return(tree, monkeypatch):
    t, f = tree, tree.f
    route_on(t)
    sid = f.agent('worker-1')['pane']
    rec, _ = spawn_m0(t)
    cf, thread = adopt_by_hook(t, monkeypatch, rec)
    hook_sim._armed.pop(cf.v2.hook_keys('supervisor', 0)['mailbox'], None)
    th, box, _ = waiting_hook(t, monkeypatch)  # the READY turn's Stop hook waits on the child mailbox
    die(cf)
    subfleet.shutdown_redis(cf.cfg)  # crashed: daemon and Redis gone, no handback.json
    time.sleep(6)
    assert th.is_alive(), 'while the parent still delegates the slot the hook waits, it never falls back early'
    assert f.agent('worker-1')['state'] == 'delegated' and not f.r.llen(f.v2.hook_keys('worker-1', 0)['mailbox'])
    r = subfleet.record(rec['run'])
    r['created'] = time.time() - 10 * subfleet.LAUNCH_GRACE
    subfleet.save_record(rec['run'], r)
    subfleet.tick(f)  # gone without result.json: stopping, collected, next generation in place
    th.join(30)
    assert not th.is_alive() and box['v'] is None
    assert f.agent('worker-1')['generation'] == '1'
    assert not [x for p, x in t.world.sent if p == sid]
    events = [x for _, x in f.r.xrange(f.v2.bus.audit_key) if x.get('slot') == 'worker-1']
    assert not [x for x in events if x.get('event') in ('settled', 'turn.settled')]


def test_named_adopted_supervisor_goes_back_to_the_parent_channel_for_the_return_handoff(svc, monkeypatch):
    t, f = svc, svc.f
    route_on(t)
    sid = f.agent('worker-1')['pane']
    rid, answer = ask(t, 'up')
    supervisor_answers(t, rid)
    subfleet.tick(f)
    rec = subfleet.record(f.run_dir / 'sub' / 'ops')
    cf, thread = adopt_by_hook(t, monkeypatch, rec)
    subfleet.tick(f)
    assert named_subfleet.state(f, 'ops')['state'] == 'up'
    hook_sim._armed.pop(cf.v2.hook_keys('supervisor', 0)['mailbox'], None)
    th, box, _ = waiting_hook(t, monkeypatch)  # the READY turn's Stop hook waits on the child mailbox
    before = len(t.world.sent)
    down_and_collect(t, cf)
    th.join(40)
    assert not th.is_alive() and box.get('v'), 'the hook stayed on the vanished child channel'
    info = f.agent('worker-1')
    assert 'Sub-fleet ops is down' in box['v']['reason'] and info['handoff_draft'] in box['v']['reason']
    assert f.v2.handoff_taken('worker-1')  # taken by the Stop hook itself, no rewake needed
    assert not [x for p, x in t.world.sent[before:] if p == sid]
    Path(info['handoff_draft']).write_text('## Done (facts)\n- ops ran\n## In progress\nnone\n## Next steps (in order)\n1. wait\n')
    hooked_turn(t, 'worker-1', 0, thread, box['v']['reason'], 'handoff written')
    step(t)
    info = f.agent('worker-1')
    assert info['generation'] == '1' and info['state'] == 'starting' and sid not in t.world.alive


def test_adopted_supervisor_handover_without_a_hook_channel_is_refused(tree):
    """§48 (user 2026-11-09): a run started before adopted_supervisor_hook_delivery could reach the handed-over
    pane only by typing into its Claude input box: the hand-over is refused with the reason, nothing typed."""
    t, f = tree, tree.f
    f.cfg['adopted_supervisor_hook_delivery'] = False
    fleet.save(f.path, dict(fleet.load(f.path), adopted_supervisor_hook_delivery=False))
    with pytest.raises(subfleet.Refused) as err:
        spawn_m0(t)
    assert 'cannot be handed over' in str(err.value) and '§48' in str(err.value)
    assert f.agent('worker-1')['state'] != 'delegated' and not t.world.sent
    # and a child config that still says adopt without hook never starts its daemon
    with pytest.raises(ValueError, match='adopt.hook'):
        runtime_v2.validate_config(dict(f.cfg, supervisor_cli='claude', adopt={'pane': 'p', 'slot': 'worker-1'}))


def test_stop_failure_of_the_adopted_supervisor_is_recorded_in_the_child(tree, monkeypatch):
    """The pane's StopFailure hook is pinned to the parent (worker-1): like Stop, it records where the route points
    (the child's Redis: its audit and the turnend mark the rewake fallback reads), never in the parent's Redis."""
    t, f = tree, tree.f
    route_on(t)
    rec, _ = spawn_m0(t)
    cf, thread = adopt_by_hook(t, monkeypatch, rec)
    assert fleet.claude_stop_failure(f.path, 'worker-1', 0, json.dumps(
        {'session_id': thread, 'error': 'unknown', 'last_assistant_message': 'API Error: computer went to sleep'}))
    child = [x for _, x in cf.r.xrange(cf.v2.bus.audit_key) if x.get('event') == 'hook.stop_failure']
    assert child and child[-1]['slot'] == 'supervisor'
    assert cf.r.get(cf.v2.hook_keys('supervisor', 0)['turnend'])
    assert not [k for k in f.r.scan_iter('*' + cf.v2.bus.prefix + '*')], 'child keys written into the parent Redis'
    assert not [x for _, x in f.r.xrange(f.v2.bus.audit_key) if x.get('event') == 'hook.stop_failure']


def sleep_holds_the_adopted_supervisor(t, monkeypatch):
    """The real-run sequence (fleet-20261109-053936-beb7e74c): the adopted supervisor's turn ends with an API error
    (the machine slept), its StopFailure starts the pane's rewake fallback (it follows the route into the child), the
    lease runs out while the daemon slept, and the child daemon holds the supervisor and pauses."""
    from fleetmux import claude_hook
    t.f.cfg['adopted_supervisor_hook_delivery'] = True
    route_on(t)
    rec, _ = spawn_m0(t)
    cf, thread = adopt_by_hook(t, monkeypatch, rec)
    step(t)
    task = hook_sim.handed_to(cf, 'supervisor')[-1]
    assert 'Research five sources' in task and cf.v2.bus.slot_state('supervisor').get('current_delivery')
    raw = json.dumps({'session_id': thread, 'transcript_path': str(t.claude / (thread + '.jsonl')),
                      'error': 'unknown', 'last_assistant_message': 'API Error: Your computer went to sleep'})
    assert fleet.claude_stop_failure(t.f.path, 'worker-1', 0, raw)
    monkeypatch.setattr(claude_hook, 'GRACE', 0)
    cf.r.delete(cf.v2.hook_keys('supervisor', 0)['stopping'])
    hook_sim._armed.pop(cf.v2.hook_keys('supervisor', 0)['mailbox'], None)  # nothing waits on a Stop: it failed
    box = {}
    th = threading.Thread(target=lambda: box.setdefault('text', fleet.claude_rewake(t.f.path, 'worker-1', '0', raw)),
                          daemon=True)
    th.start()
    cf.cfg['lease_reclaim'] = 'off'  # §24 (11-10): on, the lease is taken back; this is the hold the run had then
    cf.r.hset(cf.v2.bus.lease_key('supervisor'), 'lease_until', int(time.time() * 1000) - 1)
    cf.tick()
    assert cf.agent('supervisor')['state'] == 'held' and cf.paused()
    return cf, rec, thread, task, th, box


def test_held_adopted_supervisor_is_taken_back_in_its_own_session_after_resume(tree, monkeypatch):
    """Follow-up 2: `fleet.sh resume --config sub/M0/config.json` unpauses, and the held adopted supervisor whose
    session still runs is taken back in that session: the held delivery is cancelled after an Escape, the slot is
    idle again and the same message goes to it once more (its waiting rewake fallback hands it over)."""
    t, f = tree, tree.f
    cf, rec, thread, task, th, box = sleep_holds_the_adopted_supervisor(t, monkeypatch)
    pane = cf.agent('supervisor')['pane']
    runtime_v2.post_event(cf.v2.bus, dict(kind='resume', request_id='op-resume'), 'op-resume')
    for _ in range(3):
        step(t)
        cf.tick()
    assert not cf.paused()
    assert (pane, 'Escape') in t.world.keys
    th.join(15)
    assert not th.is_alive() and box.get('text') and 'Research five sources' in box['text'], box
    assert cf.agent('supervisor')['state'] in ('idle', 'busy') and cf.agent('supervisor')['generation'] == '0'
    decision = json.dumps({'status': 'RUNNING', 'completion': 10, 'reason': 'split', 'dispatch': [
        {'to': w, 'instruction': 'Write ' + w + '/part.md with two sources each'} for w in cf.workers]})
    assert hooked_turn(t, 'worker-1', 0, thread, box['text'], decision)['status'] == 'committed'
    assert subfleet.record(rec['run'])['state'] != 'failed'


def test_held_adopted_supervisor_whose_session_is_gone_hands_the_sub_fleet_back(tree, monkeypatch):
    """Follow-up 2: nothing to take back (the session's CLI is gone): the sub-fleet ends failed after the resume and
    the parent takes it back (§19 collect: the slot's next generation)."""
    t, f = tree, tree.f
    cf, rec, thread, task, th, box = sleep_holds_the_adopted_supervisor(t, monkeypatch)
    monkeypatch.setattr(type(cf.mgr), 'has_cli_process', lambda self, pane, names=('codex',): False)
    runtime_v2.post_event(cf.v2.bus, dict(kind='resume', request_id='op-resume'), 'op-resume')
    for _ in range(3):
        step(t)
        cf.tick()
    assert cf.task()['status'] == 'failed' and 'adopted supervisor' in cf.task()['reason']
    cf.write_result()
    subfleet.tick(f)
    stop_child_like_daemon(cf)
    subfleet.tick(f)
    assert subfleet.record(rec['run'])['state'] == 'failed'
    assert f.agent('worker-1')['generation'] == '1' and f.agent('worker-1')['state'] != 'delegated'


def test_named_child_workers_start_on_the_run_default_then_switch_to_their_roster_card(svc):
    """brief-m4 §7.3: a named sub-fleet's first up starts its workers on the run default; each switches to its
    roster card once idle (roster.profile_switched). Only a split's approved cards (§44) start on the card."""
    t = svc
    (t.tmp / 'child.yaml').write_text('workers:\n  - name: ops-1\n    profile: claude-opus\n')
    _, _, cf = bring_up(t)
    assert cf.cfg['worker_profiles'] == {'ops-1': 'claude-opus'}
    assert not cf.agent('ops-1').get('roster_profile')
    assert cf.profile_swaps() == ['ops-1']
