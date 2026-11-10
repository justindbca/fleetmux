"""Frozen official Claude worker -> resume its own session first (docs/design.md §43, user 2026-11-09 「resume也能接受」).

The watchdog's frozen rotation (§24) used to start a FRESH successor from a skeleton note and never re-run the
cancelled work. For an official Claude worker whose transcript is still there and that is not already a resumed
generation, the next generation now runs `claude --resume <thread>` in the same tab and gets the cancelled work
again (task: requeue_lost, service: replay_request; counted in replay / max_attempts, `replay: false` not
replayed). The old process tree is killed and confirmed gone before the successor's CLI may start (launch gate):
two processes never write one session. A resumed generation that freezes again, or misses READY, falls back to
today's fresh generation. Supervisor / Codex / pi slots are unchanged.

Real private Redis + real hook entry points; fake cmux screens / ps, time passed to Watchdog.tick.
"""
import json
import os
from pathlib import Path
import time

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_v2_codex_claude import answer_turn, dispatch_one, mixed, typed  # noqa: F401  (fixture)
from test_v2_codex_service import service  # noqa: F401  (fixture)
from test_watchdog import WORKING, audits, screens, wd  # noqa: F401  (fixture)
from test_request_no_deadline import post, result
from cmux_fleet import fleet

SLOT = 'worker-1'


def freeze(f, t=None):
    t = time.time() if t is None else t
    f.mgr.stats[f.agent(SLOT)['pane']] = ['Ss', 'U+']   # stuck in the kernel twice in a row -> frozen
    f.v2.watchdog.tick(t)
    f.v2.watchdog.tick(t + 1)


def launched_argv(f, slot, monkeypatch):
    """The argv the slot's current generation execs (the real actor, exec captured)."""
    seen = []
    monkeypatch.setattr(os, 'execvp', lambda prog, argv: seen.append(argv))
    monkeypatch.setattr(os, 'chdir', lambda path: None)
    fleet.actor(f.path, slot, 'READY please', generation=int(f.agent(slot)['generation']))
    return seen[-1]


def watched(f):
    f.cfg.update(stall_timeout=600, stall_grace=300, stall_escapes=3, watch_interval=0)
    f.mgr.screens, f.mgr.stats = {}, {}
    f.mgr.capture_output = lambda pane, lines=2000: f.mgr.screens.get(pane, WORKING)
    f.mgr.process_states = lambda pane: f.mgr.stats.get(pane, ['Ss', 'S+'])
    f.mgr.owns = lambda pane: pane in f.mgr.alive
    return f


def test_frozen_claude_worker_resumes_its_session_in_place_and_gets_its_work_again(wd, monkeypatch):
    f = wd
    dispatch_one(f)
    old, thread = f.agent(SLOT)['pane'], f.agent(SLOT)['thread']
    msg_id = f.v2.bus.slot_state(SLOT)['message_id']
    seen = {}
    real_kill = f.mgr.kill_pane

    def kill(pane):
        if pane == old:   # the successor exists but its CLI may not start before the old tree is gone
            w = f.agent(SLOT)
            seen.update(launch_ready=w['launch_ready'], resume=w.get('resume_session'))
        return real_kill(pane)
    monkeypatch.setattr(f.mgr, 'kill_pane', kill)
    freeze(f)
    assert seen == dict(launch_ready='0', resume=thread)
    w = f.agent(SLOT)
    assert old in f.mgr.killed and w['generation'] == '1' and w['label'] == 'worker-1.1'
    assert w['state'] == 'starting' and w['launch_ready'] == '1' and (w['pane'], old) in f.mgr.tabbed  # same tab
    assert w['resume_session'] == thread and w['resume_generation'] == '1'
    argv = launched_argv(f, SLOT, monkeypatch)
    assert argv[argv.index('--resume') + 1] == thread
    assert 'the daemon resumed it' in (f.run_dir / 'prompts' / 'worker-1-g1.md').read_text()
    msg = f.v2.bus.message(msg_id)
    assert msg['outcome'] == 'cancelled' and str(msg['stop_confirmed']) == '1'
    assert not f.paused() and f.task()['status'] == 'running'
    assert f.v2.depth('supervisor') == 0                      # no FROZEN report: the work goes on
    assert f.v2.depth(SLOT) == 1
    again = f.v2.bus.message(f.v2.bus.pending(SLOT)[0])
    assert again['replay'] == '2' and 'worker-1/part.md' in again['body']['text']
    restarted, = audits(f, 'watch.frozen_restarted')
    assert restarted['resumed'] == thread and restarted['label'] == 'worker-1.1'
    assert any(n.endswith('-frozen.txt') for n in screens(f))
    assert not list((f.run_dir / 'handoffs').glob('worker-1-g0-frozen-*.md'))   # no skeleton note to read
    assert answer_turn(f, SLOT, 'READY', startup=True)['status'] == 'ready'
    f.pump()
    assert f.agent(SLOT)['state'] == 'busy' and 'worker-1/part.md' in typed(f, SLOT)[-1]


def fresh_checks(f, old, msg_id, generation=1):
    """Today's frozen rotation: fresh successor from a skeleton note, the work cancelled and NOT re-run."""
    w = f.agent(SLOT)
    assert old in f.mgr.killed and w['generation'] == str(generation) and w['state'] == 'starting'
    assert w.get('resume_generation', '') != str(generation)
    msg = f.v2.bus.message(msg_id)
    assert msg['outcome'] == 'cancelled' and str(msg['stop_confirmed']) == '1'
    assert f.v2.depth(SLOT) == 0
    report = f.v2.pending_text('supervisor')[0]
    assert report.startswith('[' + w['label'] + ' report') and 'NOT re-run' in report
    assert list((f.run_dir / 'handoffs').glob('worker-1-g' + str(generation - 1) + '-frozen-*.md'))


def test_frozen_worker_without_a_transcript_gets_todays_fresh_generation(wd, monkeypatch):
    f = wd
    dispatch_one(f)
    old, msg_id = f.agent(SLOT)['pane'], f.v2.bus.slot_state(SLOT)['message_id']
    Path(f.agent(SLOT)['transcript']).unlink()
    freeze(f)
    fresh_checks(f, old, msg_id)
    assert '--resume' not in launched_argv(f, SLOT, monkeypatch)


def test_dispatch_over_max_attempts_gets_todays_fresh_generation(wd):
    f = wd
    dispatch_one(f)
    old, msg_id = f.agent(SLOT)['pane'], f.v2.bus.slot_state(SLOT)['message_id']
    f.r.hset(f.v2.bus.msg_key(msg_id), 'replay', f.cfg['max_attempts'])
    freeze(f)
    fresh_checks(f, old, msg_id)


def test_resumed_generation_that_freezes_again_gets_a_fresh_one_and_the_work_is_not_rerun(wd, monkeypatch):
    f = wd
    f.cfg['max_attempts'] = 5
    dispatch_one(f)
    freeze(f)
    assert answer_turn(f, SLOT, 'READY', startup=True)['status'] == 'ready'
    f.pump()
    old, msg_id = f.agent(SLOT)['pane'], f.v2.bus.slot_state(SLOT)['message_id']
    freeze(f, time.time() + 10)
    fresh_checks(f, old, msg_id, generation=2)
    assert '--resume' not in launched_argv(f, SLOT, monkeypatch)


def test_resumed_generation_without_ready_falls_back_to_a_fresh_one_that_runs_the_work_once(wd, monkeypatch):
    f = wd
    f.mgr.dock_control = lambda history_pane: True    # respawn re-docks the control tab (not on this FakeMgr)
    dispatch_one(f)
    freeze(f)
    resumed = f.agent(SLOT)['pane']
    f.setf(SLOT, startup_deadline=0)          # claude --resume never came up
    f.v2.tick()
    w = f.agent(SLOT)
    assert w['generation'] == '2' and w['state'] == 'starting' and resumed in f.mgr.killed
    assert '--resume' not in launched_argv(f, SLOT, monkeypatch)
    assert 'worker-1-g1-lost.md' in (f.run_dir / 'prompts' / 'worker-1-g2.md').read_text()
    assert f.v2.depth(SLOT) == 1 and not f.paused()
    assert answer_turn(f, SLOT, 'READY', startup=True)['status'] == 'ready'
    f.pump()
    mid = f.v2.bus.slot_state(SLOT)['message_id']
    assert 'worker-1/part.md' in typed(f, SLOT)[-1] and f.v2.bus.message(mid)['replay'] == '2'


def test_failed_resume_rotation_rolls_back_without_leaving_a_resume_mark(wd, monkeypatch):
    f = wd
    dispatch_one(f)
    old = f.agent(SLOT)['pane']
    real_kill = f.mgr.kill_pane
    monkeypatch.setattr(f.mgr, 'kill_pane', lambda pane: pane != old and real_kill(pane))  # the frozen one stays
    freeze(f)
    w = f.agent(SLOT)
    assert w['pane'] == old and w['generation'] == '0' and w['state'] == 'held'
    assert w.get('resume_session', '') == '' and w.get('resume_generation', '') == ''
    assert f.v2.depth(SLOT) == 0                      # nothing re-queued: the old execution is not confirmed stopped
    assert audits(f, 'watch.rotate_failed')


def test_frozen_supervisor_keeps_todays_rotation(wd):
    f = wd
    dispatch_one(f)
    from test_v2_codex_claude import submit  # noqa: F401
    f.queue('supervisor', 'status check')
    f.pump()
    old = f.agent('supervisor')['pane']
    f.mgr.stats[old] = ['U']
    f.v2.watchdog.tick(time.time()); f.v2.watchdog.tick(time.time() + 1)
    assert old in f.mgr.killed and f.agent('supervisor').get('resume_generation', '') == ''


# ------------------------------------------------------------------ service mode
def test_frozen_service_worker_resumes_and_its_request_is_replayed(service):
    f = watched(service)
    post(f, 'r1', to=SLOT)
    thread = f.agent(SLOT)['thread']
    freeze(f)
    w = f.agent(SLOT)
    assert w['generation'] == '1' and w['resume_session'] == thread and not f.paused()
    assert result(f, 'r1') is None                                    # not published `stalled`
    req = f.r.hgetall(fleet.REQ + 'r1')
    assert req['status'] == 'dispatched' and req['worker'] == SLOT and req['replay'] == '2'
    assert f.v2.depth(SLOT) == 1
    assert answer_turn(f, SLOT, 'READY', startup=True)['status'] == 'ready'
    f.pump()
    assert answer_turn(f, SLOT, '{"reply": "fills ok"}')['status'] == 'committed'
    assert result(f, 'r1')['status'] == 'ok'


def test_frozen_service_request_with_replay_false_is_published_stalled_as_today(service):
    f = watched(service)
    post(f, 'r1', to=SLOT, replay=False)
    freeze(f)
    assert f.agent(SLOT).get('resume_generation', '') != '1'
    assert result(f, 'r1')['status'] == 'stalled' and f.v2.depth(SLOT) == 0
