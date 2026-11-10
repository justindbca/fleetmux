"""Liveness (RuntimeV2.tab_missing): a slot's tab is gone (10-14 Studio: an outside watchdog killed the fleet's
tmux sessions; every 30 s "runtime tab missing" held the slot and paused the whole fleet, 4.5 hours down).

Only proof that the session stopped (its tmux session ended) restarts the slot as its next generation, with what
``start --resume`` does for a dead tab (§17): the lost work is replayed once more (attempt-capped, replay false not
replayed); a tab closed over a live session is reattached; without proof a worker is held alone, the fleet goes
on; only the supervisor still pauses. A delegated supervisor_slot (§29) is never held: proven gone, its sub-fleet goes down
and the slot's next generation starts (an autostart child comes up again). Same fake world as test_subfleet.py;
the tmux probe / reattach have real-tmux tests in test_tmux_view.py.
"""
import json
from pathlib import Path

import pytest

from test_subfleet import (WorldMgr, drive, given, hook, spawn_m0, step, stop_child_like_daemon, supervisor_texts,
                           tree)  # noqa: F401
from test_subfleet_named_tmux import adopt_view_child, service_tree
from cmux_fleet import fleet, named_subfleet, subfleet


def tmux_session_alive(self, sid):
    w = self.w
    session = getattr(w, 'tmux_owner', {}).get(sid) or (getattr(w, 'views', {}).get(sid) or {}).get('tmux_session')
    if not session:
        return None
    return session not in w.__dict__.setdefault('tmux_dead', set())


def reattach_client(self, sid, peer):
    w = self.w
    if self.owns(sid) or not self.owns(peer) or w.owner.get(sid) != self.session or not self.tmux_session_alive(sid):
        return None
    new = self._new('client')
    if sid in w.tmux_owner:
        w.tmux_owner[new] = w.tmux_owner.pop(sid)
    if sid in w.views:
        w.views[new] = w.views.pop(sid)
    w.owner.pop(sid, None)
    w.__dict__.setdefault('reattached', []).append((sid, new, peer))
    return new


@pytest.fixture(autouse=True)
def probes(monkeypatch):
    monkeypatch.setattr(WorldMgr, 'tmux_session_alive', tmux_session_alive, raising=False)
    monkeypatch.setattr(WorldMgr, 'reattach_client', reattach_client, raising=False)


def tmux_slot(t, slot='worker-1'):
    """Mark ``slot``'s current tab as a client of its fleet-tmux session (as a supervisor_slot spawns)."""
    w = t.world
    for name in ('tmux_owner', 'views'):
        w.__dict__.setdefault(name, {})
    session = slot + '-g' + t.f.agent(slot)['generation']
    w.tmux_owner[t.f.agent(slot)['pane']] = session
    return session


def tab_closed(t, sid):
    t.world.alive.discard(sid)  # cmux no longer shows it; the registry entry stays until it is forgotten


def session_ended(t, session):
    t.world.__dict__.setdefault('tmux_dead', set()).add(session)


def liveness(f):
    f.next_liveness = 0
    f.v2.tick()


def audits(f, event):
    return [x[1] for x in f.r.xrange(f.v2.bus.audit_key) if x[1].get('event') == event]


def dispatch(t, slot, text):
    f = t.f
    f.queue(slot, '[Supervisor instruction ' + f.task()['id'] + '] ' + text)
    step(t)
    assert f.v2.bus.slot_state(slot).get('current_delivery')


REPORT = 'Done:\n- wrote a.md\nEvidence:\n- worker-1/a.md\nNot done: none\nBlocked: none'


def lose(t, slot='worker-1'):
    """The outside watchdog kills the slot's tmux session: its client tab ends with it."""
    f = t.f
    session = slot + '-g' + f.agent(slot)['generation']
    t.world.tmux_owner[f.agent(slot)['pane']] = session
    tab_closed(t, f.agent(slot)['pane'])
    session_ended(t, session)
    liveness(f)


# ------------------------------------------------------------------ 1. proven stopped: next generation, work replayed
def launched_argv(f, slot, monkeypatch):
    """The argv the slot's current generation execs (the real actor, exec captured)."""
    import os
    seen = []
    monkeypatch.setattr(os, 'execvp', lambda prog, argv: seen.append(argv))
    monkeypatch.setattr(os, 'chdir', lambda path: None)
    fleet.actor(f.path, slot, 'READY please', generation=int(f.agent(slot)['generation']))
    return seen[-1]


def test_lost_session_is_resumed_and_finishes_the_replayed_work(tree, monkeypatch):
    t, f = tree, tree.f
    tmux_slot(t)
    old, thread = f.agent('worker-1')['pane'], f.agent('worker-1')['thread']
    dispatch(t, 'worker-1', 'Write worker-1/a.md now')
    lose(t)
    assert not f.paused() and not audits(f, 'execution.held')
    info = f.agent('worker-1')
    assert info['generation'] == '1' and info['state'] == 'starting' and info['pane'] not in ('', old)
    argv = launched_argv(f, 'worker-1', monkeypatch)
    assert argv[argv.index('--resume') + 1] == thread  # the same Claude session continues
    launch = (f.run_dir / 'prompts' / 'worker-1-g1.md').read_text()  # its launch prompt (CLI argument)
    assert 'the daemon resumed it' in launch and 'PROGRESS.md' in launch
    assert json.loads(audits(f, 'liveness.restarted')[-1]['detail'])['resume_session'] == thread
    assert hook(t, f, 'worker-1', 'READY', prompt='startup', thread=thread)['status'] == 'ready'
    step(t)
    again = given(t, f, 'worker-1')[-1]
    assert 'worker-1/a.md' in again  # the lost dispatch, once more, to the resumed session
    mid = f.v2.bus.slot_state('worker-1')['message_id']
    assert f.v2.bus.message(mid)['replay'] == '2'
    assert hook(t, f, 'worker-1', REPORT, prompt=again, thread=thread)['status'] == 'committed'
    assert f.task()['status'] == 'running'
    dispatch(t, 'worker-2', 'Write worker-2/b.md now')  # the fleet kept working all along


def test_resumed_session_lost_again_gets_a_fresh_generation_with_a_note_and_progress(tree, monkeypatch):
    t, f = tree, tree.f
    f.cfg['max_attempts'] = 3
    tmux_slot(t)
    progress = fleet.agent_cwd(f.cfg, 'worker-1') / 'PROGRESS.md'
    progress.write_text('- milestone 1: sources listed\n')
    dispatch(t, 'worker-1', 'Write worker-1/a.md now')
    lose(t)
    thread = f.agent('worker-1')['resume_session']
    assert hook(t, f, 'worker-1', 'READY', prompt='startup', thread=thread)['status'] == 'ready'
    step(t)
    lose(t)  # the resumed session dies again on the same delivery: back to the plain next generation
    info = f.agent('worker-1')
    assert info['generation'] == '2' and '--resume' not in launched_argv(f, 'worker-1', monkeypatch)
    note = f.run_dir / 'handoffs' / 'worker-1-g1-lost.md'
    assert str(progress) in note.read_text()
    launch = (f.run_dir / 'prompts' / 'worker-1-g2.md').read_text()
    assert str(note) in launch and 'ended unexpectedly' in launch
    assert hook(t, f, 'worker-1', 'READY', prompt='startup')['status'] == 'ready'
    step(t)
    mid = f.v2.bus.slot_state('worker-1')['message_id']
    assert 'worker-1/a.md' in given(t, f, 'worker-1')[-1] and f.v2.bus.message(mid)['replay'] == '3'


def test_session_that_cannot_be_resumed_gets_a_fresh_generation(tree, monkeypatch):
    t, f = tree, tree.f
    tmux_slot(t)
    Path(f.agent('worker-1')['transcript']).unlink()  # nothing left to resume
    dispatch(t, 'worker-1', 'Write worker-1/a.md now')
    lose(t)
    assert f.agent('worker-1')['generation'] == '1' and '--resume' not in launched_argv(f, 'worker-1', monkeypatch)
    assert 'worker-1-g0-lost.md' in (f.run_dir / 'prompts' / 'worker-1-g1.md').read_text()


def test_resumed_session_without_ready_in_time_falls_back_to_a_fresh_generation(tree, monkeypatch):
    t, f = tree, tree.f
    tmux_slot(t)
    dispatch(t, 'worker-1', 'Write worker-1/a.md now')
    lose(t)
    resumed = f.agent('worker-1')['pane']
    f.setf('worker-1', startup_deadline=0)  # claude --resume never came up (e.g. it failed at launch)
    f.v2.tick()
    info = f.agent('worker-1')
    assert info['generation'] == '2' and info['state'] == 'starting' and f.task()['status'] == 'running'
    assert resumed not in t.world.alive and '--resume' not in launched_argv(f, 'worker-1', monkeypatch)
    assert 'worker-1-g1-lost.md' in (f.run_dir / 'prompts' / 'worker-1-g2.md').read_text()
    assert hook(t, f, 'worker-1', 'READY', prompt='startup')['status'] == 'ready'
    step(t)
    mid = f.v2.bus.slot_state('worker-1')['message_id']
    assert 'worker-1/a.md' in given(t, f, 'worker-1')[-1]
    assert f.v2.bus.message(mid)['replay'] == '2'  # the resume that never ran is not counted


def test_lost_again_over_max_attempts_blocks_the_task(tree):
    t, f = tree, tree.f
    tmux_slot(t)
    dispatch(t, 'worker-1', 'Write worker-1/a.md now')
    lose(t)
    assert hook(t, f, 'worker-1', 'READY', prompt='startup', thread=f.agent('worker-1')['resume_session'])['status'] == 'ready'
    step(t)
    assert 'worker-1/a.md' in given(t, f, 'worker-1')[-1]
    lose(t)  # the replay (attempt 2 of max-attempts 2) is lost as well
    task = f.task()
    assert task['status'] == 'blocked' and 'lost again after 2 attempt(s)' in task['reason']


@pytest.fixture
def svc_tmux(tmp_path, monkeypatch):
    t = service_tree(tmp_path, monkeypatch)  # worker-1 is a declared supervisor_slot: a fleet-tmux client tab
    t.f.cfg['max_attempts'] = 2
    yield t
    for _ in t.gen:
        pass


def request_to_worker_1(t, rid='q1', **extra):
    f = t.f
    spec = dict(id=rid, payload={'message': 'check the gateway'}, reply={'required': ['reply']}, **extra)
    assert drive(t, lambda: fleet.request(f.cfg, dict(kind='request', spec=spec)))['status'] == 'accepted'
    route(t, rid)
    assert f.v2.bus.slot_state('worker-1').get('current_delivery')


def route(t, rid):
    f = t.f
    step(t)
    prompt = given(t, f, 'supervisor')[-1]
    assert 'needs routing' in prompt and 'check the gateway' in prompt
    text = json.dumps({'status': 'RUNNING', 'skip': [],
                       'dispatch': [{'to': 'worker-1', 'request_id': rid, 'instruction': 'Answer in reply.'}]})
    assert hook(t, f, 'supervisor', text, prompt=prompt)['status'] == 'committed'
    step(t)


def test_lost_service_request_is_routed_again_and_answered_by_the_next_generation(svc_tmux):
    t, f = svc_tmux, svc_tmux.f
    request_to_worker_1(t)
    lose(t)
    assert not f.paused() and f.agent('worker-1')['generation'] == '1'
    req = f.r.hgetall(fleet.REQ + 'q1')
    assert req['replay'] == '2' and req['status'] == 'routing' and f.r.get(fleet.RESULT + 'q1') is None
    assert hook(t, f, 'worker-1', 'READY', prompt='startup')['status'] == 'ready'
    route(t, 'q1')  # the supervisor routes it once more; the next generation answers it
    prompt = given(t, f, 'worker-1')[-1]
    assert 'check the gateway' in prompt
    assert hook(t, f, 'worker-1', json.dumps({'reply': 'gateway ok'}), prompt=prompt)['status'] == 'committed'
    res = json.loads(f.r.get(fleet.RESULT + 'q1'))
    assert res['status'] == 'ok' and res['result'] == {'reply': 'gateway ok'}


def test_lost_service_request_over_max_attempts_is_published_timeout(svc_tmux):
    t, f = svc_tmux, svc_tmux.f
    f.cfg['max_attempts'] = 1
    request_to_worker_1(t)
    lose(t)
    res = json.loads(f.r.get(fleet.RESULT + 'q1'))
    assert res['status'] == 'timeout' and '1 attempt(s) used' in res['error'] and not f.paused()


def test_lost_service_request_with_replay_false_is_published_timeout(svc_tmux):
    t, f = svc_tmux, svc_tmux.f
    request_to_worker_1(t, replay=False)
    lose(t)
    res = json.loads(f.r.get(fleet.RESULT + 'q1'))
    assert res['status'] == 'timeout' and 'replay: false' in res['error']
    assert f.agent('worker-1')['generation'] == '1' and not f.paused()


def test_a_generation_that_never_said_ready_is_held_not_restarted(tree):
    t, f = tree, tree.f
    session = tmux_slot(t)
    f.setf('worker-1', runtime_ready=0, state='starting')  # e.g. a CLI that exits at launch: no restart loop
    tab_closed(t, f.agent('worker-1')['pane'])
    session_ended(t, session)
    liveness(f)
    info = f.agent('worker-1')
    assert info['state'] == 'held' and info['generation'] == '0' and not f.paused()


def test_paused_run_holds_the_lost_slot_and_restarts_it_after_resume(tree):
    t, f = tree, tree.f
    session = tmux_slot(t)
    f.pause('operator')
    tab_closed(t, f.agent('worker-1')['pane'])
    session_ended(t, session)
    liveness(f)
    assert f.agent('worker-1')['state'] == 'held' and f.agent('worker-1')['generation'] == '0'
    f.resume()
    liveness(f)
    assert f.agent('worker-1')['generation'] == '1' and f.agent('worker-1')['state'] == 'starting'
    assert not f.paused()


# ------------------------------------------------------------------ 2. not proven: reattach, else hold that slot only
def test_closed_tab_over_a_live_session_is_reattached_and_nothing_is_held(tree):
    t, f = tree, tree.f
    tmux_slot(t)
    old = f.agent('worker-1')['pane']
    dispatch(t, 'worker-1', 'Write worker-1/a.md now')
    tab_closed(t, old)  # only the client tab was closed: the session runs on
    liveness(f)
    info = f.agent('worker-1')
    assert t.world.reattached == [(old, info['pane'], f.agent('worker-2')['pane'])]
    assert info['generation'] == '0' and info['state'] == 'busy' and not f.paused()
    assert f.v2.bus.slot_state('worker-1').get('status') == 'received'  # the turn goes on in the same session
    assert audits(f, 'liveness.reattached') and not audits(f, 'execution.held')


def test_live_session_without_a_reattachable_tab_holds_only_that_slot(tree, monkeypatch):
    t, f = tree, tree.f
    tmux_slot(t)
    monkeypatch.setattr(WorldMgr, 'reattach_client', lambda self, sid, peer: None)
    tab_closed(t, f.agent('worker-1')['pane'])
    liveness(f)
    assert f.agent('worker-1')['state'] == 'held' and f.agent('worker-1')['generation'] == '0'
    assert not f.paused()
    dispatch(t, 'worker-2', 'Write worker-2/b.md now')


def test_plain_worker_tab_missing_holds_only_that_slot_with_a_notice(tree):
    t, f = tree, tree.f
    tab_closed(t, f.agent('worker-2')['pane'])  # a plain cmux pane: no stop evidence at all
    liveness(f)
    assert f.agent('worker-2')['state'] == 'held' and f.agent('worker-2')['generation'] == '0'
    assert not f.paused()
    held = audits(f, 'execution.held')
    assert held and 'stop evidence unavailable' in held[-1]['error']
    dispatch(t, 'worker-1', 'Write worker-1/a.md now')  # monitor and the other slots work on


# ------------------------------------------------------------------ 4. the supervisor
def test_plain_supervisor_tab_missing_still_pauses(tree):
    t, f = tree, tree.f
    tab_closed(t, f.agent('supervisor')['pane'])
    liveness(f)
    assert f.agent('supervisor')['state'] == 'held' and f.paused()


def test_supervisor_is_never_restarted_it_pauses_even_with_stop_evidence(tree):
    t, f = tree, tree.f
    session = tmux_slot(t, 'supervisor')  # never the case (a supervisor never runs in tmux): still only a hold
    tab_closed(t, f.agent('supervisor')['pane'])
    session_ended(t, session)
    liveness(f)
    assert f.agent('supervisor')['state'] == 'held' and f.agent('supervisor')['generation'] == '0' and f.paused()


# ------------------------------------------------------------------ 3. a named sub-fleet's supervisor_slot (§29)
def killed_up_autostart(tmp_path, monkeypatch):
    t = service_tree(tmp_path, monkeypatch, autostart=True)
    f = t.f
    subfleet.tick(f)  # declared + idle READY: delegated at once
    rec, cf, view = adopt_view_child(t)
    subfleet.tick(f)
    assert named_subfleet.state(f, 'ops')['state'] == 'up'
    return t, f, rec, cf, view


def test_killed_supervisor_slot_takes_its_subfleet_down_and_autostart_brings_it_back(tmp_path, monkeypatch):
    t, f, rec, cf, view = killed_up_autostart(tmp_path, monkeypatch)
    try:
        sid = f.agent('worker-1')['pane']
        for pane in (sid, view):  # the outside watchdog killed the session: both client tabs ended
            tab_closed(t, pane)
        session_ended(t, 'worker-1-g0')
        liveness(cf)  # the child: its adopted supervisor is gone; the parent takes it down, no pause
        assert cf.agent('supervisor')['state'] == 'held' and not cf.paused()
        liveness(f)
        assert not f.paused() and f.agent('worker-1')['state'] == 'delegated'  # never held
        assert subfleet.record(rec['run'])['state'] == 'stopping'
        assert json.loads(audits(f, 'liveness.delegated_tab_missing')[-1]['detail']) == {'subfleet_down': True}
        stop_child_like_daemon(cf)  # the child stops: its dead client tab hands back fine
        subfleet.tick(f)
        rec = subfleet.record(rec['run'])
        assert rec['state'] == 'down' and named_subfleet.state(f, 'ops')['state'] == 'down'
        info = f.agent('worker-1')
        assert info['generation'] == '1' and info['state'] == 'starting' and info['pane'] != sid
        assert (f.run_dir / 'handoffs' / 'worker-1-g0-return.md').exists()
        assert hook(t, f, 'worker-1', 'READY', prompt='startup')['status'] == 'ready'
        subfleet.tick(f)  # autostart: delegated again, from the new generation
        again = subfleet.record(f.run_dir / 'sub' / 'ops')
        assert again['state'] == 'running' and again['pane'] == f.agent('worker-1')['pane'] and again['generation'] == 1
        assert not f.paused()
    finally:
        for _ in t.gen:
            pass


def test_closed_tabs_of_a_delegated_live_session_are_reattached_on_both_sides(tmp_path, monkeypatch):
    t, f, rec, cf, view = killed_up_autostart(tmp_path, monkeypatch)
    try:
        sid = f.agent('worker-1')['pane']
        tab_closed(t, sid)
        liveness(f)
        info = f.agent('worker-1')
        assert info['state'] == 'delegated' and info['pane'] != sid and info['delegated_pane'] == info['pane']
        assert subfleet.record(rec['run'])['pane'] == info['pane'] and not f.paused()
        tab_closed(t, view)
        liveness(cf)
        assert cf.agent('supervisor')['pane'] not in ('', view) and not cf.paused()
        assert cf.agent('supervisor')['state'] != 'held'
    finally:
        for _ in t.gen:
            pass


def test_closed_placeholder_of_a_delegated_slot_holds_nothing(tree):
    t, f = tree, tree.f
    rec, _ = spawn_m0(t)  # a plain cmux pane: §19 placeholder in the parent
    tab_closed(t, rec['placeholder'])
    liveness(f)
    assert f.agent('worker-1')['state'] == 'delegated' and not f.paused()
    assert subfleet.record(rec['run'])['state'] == 'running'


def child_request(t, cf, rid):
    """An outside caller's request at the child's socket, routed by the child supervisor to its worker."""
    cfg = cf.cfg
    spec = dict(id=rid, payload={'message': 'extract the memory'}, reply={'required': ['reply']})
    assert drive(t, lambda: fleet.request(cfg, dict(kind='request', spec=spec)))['status'] == 'accepted'
    step(t)
    return cf.r.hgetall(fleet.REQ + rid)


def test_crashed_child_daemon_is_revived_with_resume_and_replays_its_requests(tmp_path, monkeypatch):
    """Reused as is (§29 revive -> daemon --resume -> §17 requeue_requests): the request continues in the same
    child Redis, attempt counted."""
    import fcntl
    import time
    t, f, rec, cf, view = killed_up_autostart(tmp_path, monkeypatch)
    try:
        assert child_request(t, cf, 'mem-1')['status'] == 'routing'
        from test_subfleet import die
        die(cf)  # the child daemon dies; its Redis (AOF) and the supervisor session stay
        rec = subfleet.record(rec['run'])
        rec['launched'] = rec['created'] = time.time() - subfleet.LAUNCH_GRACE - 1
        subfleet.save_record(rec['run'], rec)
        subfleet.tick(f)
        mgr = t.world.mgrs['cmux-fleet:ops']
        assert subfleet.record(rec['run'])['state'] == 'running' and '--resume' in mgr.windows[-1][1]
        cfg = dict(fleet.load(Path(rec['run']) / 'config.json'), max_attempts=2)  # this run's parent had 1
        fleet.save(Path(rec['run']) / 'config.json', cfg)
        again = fleet.Fleet(Path(rec['run']) / 'config.json', fleet.connect(cfg), t.world.manager(cfg),
                            sessions=t.sessions)
        again.lock = (Path(rec['run']) / 'daemon.lock').open('a+')
        fcntl.flock(again.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        t.fleets.append(again)
        again.resume_requested = True
        again.v2.resume_run()
        req = again.r.hgetall(fleet.REQ + 'mem-1')
        assert req['replay'] == '2' and req['status'] == 'routing' and not again.paused()
        assert again.agent('supervisor')['pane'] == view  # the same session supervises on (no new client)
    finally:
        for _ in t.gen:
            pass


def test_lost_supervisor_slot_lists_the_childs_open_requests_in_the_return_note(tmp_path, monkeypatch):
    """§29: a taken-down child's open requests belong to their caller (who re-delivers them after the next up):
    they are read before its Redis goes and written into the slot's return note."""
    t, f, rec, cf, view = killed_up_autostart(tmp_path, monkeypatch)
    try:
        child_request(t, cf, 'mem-2')
        for pane in (f.agent('worker-1')['pane'], view):
            tab_closed(t, pane)
        session_ended(t, 'worker-1-g0')
        liveness(f)
        assert subfleet.record(rec['run'])['requests']['open'] == ['mem-2']
        stop_child_like_daemon(cf)
        subfleet.tick(f)
        assert 'mem-2' in (f.run_dir / 'handoffs' / 'worker-1-g0-return.md').read_text()
    finally:
        for _ in t.gen:
            pass
