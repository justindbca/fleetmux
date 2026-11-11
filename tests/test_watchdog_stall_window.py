"""§24 stall window: transcript, screen and process-tree CPU all still for stall_window -> stalled.

Pure classify cases, the slot-field baseline rolling in Watchdog.check (real private Redis, fake cmux screens /
CPU, time passed explicitly), the ps-based CPU probe, and the config / roster / sub-fleet wiring.
"""
import json
import os
import subprocess
import time

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_v2_codex_claude import dispatch_one, mixed  # noqa: F401  (fixture)
from test_watchdog import BASE, WORKING, audits, keys, transitions, wd  # noqa: F401  (fixture)
from test_advisor_resident import res  # noqa: F401  (fixture)
from fleetmux import fleet, roster, runtime_v2, subfleet, watchdog


# ------------------------------------------------------------------ pure classify
WIN = dict(BASE, stall_timeout=1200, stall_window=300)


@pytest.mark.parametrize('change,state', [
    (dict(quiet=300), 'stalled'),                                  # all three signals still for the window
    (dict(quiet=299), 'working'),
    (dict(quiet=0, age=1199), 'working'),                          # something moved: the window restarted
    (dict(quiet=0, age=1200), 'stalled'),                          # hard limit: screen spins, transcript dead
    (dict(quiet=10 ** 6, stall_window=0), 'working'),              # window rule off
    (dict(quiet=10 ** 6, stall_window=0, age=1200), 'stalled'),    # ... the hard limit still holds
    (dict(quiet=10 ** 6, stall_window=0, stall_timeout=0, age=10 ** 6), 'working'),  # both off
    (dict(quiet=10 ** 6, in_flight=False), 'working'),             # starting without a delivery: READY deadline
    (dict(quiet=10 ** 6, active=False, in_flight=False), 'idle'),  # E1
    # Once stalled only the transcript brings it back (Escape itself changes the screen): quiet=0 stays stalled.
    (dict(prev='stalled', since=900, quiet=0), 'stalled'),
    (dict(prev='stalled', since=900, quiet=0, moved=True), 'working'),
    (dict(prev='stalled', since=600, quiet=0), 'frozen'),          # grace over
])
def test_classify_stall_window(change, state):
    assert watchdog.classify(**dict(WIN, **change)) == state


def test_classify_without_the_new_inputs_is_unchanged():
    # Callers that pass neither quiet nor stall_window get the original transcript-only rule.
    assert watchdog.classify(**dict(BASE, age=599)) == 'working'
    assert watchdog.classify(**dict(BASE, age=600)) == 'stalled'


# ------------------------------------------------------------------ check layer: baseline rolling
@pytest.fixture
def sw(wd):  # noqa: F811
    f = wd
    f.cfg.update(stall_timeout=1200, stall_window=300, stall_cpu_pct=5)  # CPU threshold: 15 s per window
    f.mgr.cpu = {}
    f.mgr.process_cpu = lambda pane: f.mgr.cpu.get(pane, 10.0)
    return f


def run(f, t0, steps, slot='worker-1', screen=None, cpu=None):
    """Tick at t0 + each step; `screen(dt)` / `cpu(dt)` set the pane's screen / CPU first."""
    pane = f.agent(slot)['pane']
    states = []
    for dt in steps:
        if screen is not None:
            f.mgr.screens[pane] = screen(dt)
        if cpu is not None:
            f.mgr.cpu[pane] = cpu(dt)
        f.v2.watchdog.tick(t0 + dt)
        states.append(f.agent(slot)['watch_state'])
    return states


def test_all_three_still_for_the_window_is_stalled_and_escaped(sw):
    f = sw
    dispatch_one(f)
    t0 = time.time()
    assert run(f, t0, [0, 100, 200, 299]) == ['working'] * 4
    assert keys(f, 'worker-1') == []
    assert run(f, t0, [300]) == ['stalled'] and keys(f, 'worker-1') == ['Escape']
    stalled = [r for r in audits(f, 'agent.state') if r.get('slot') == 'worker-1'][-1]
    detail = json.loads(stalled['detail'])
    assert detail['to'] == 'stalled' and detail['quiet_s'] == 300
    assert f.agent('worker-1')['state'] == 'busy' and not f.paused()


def test_a_screen_that_keeps_changing_is_working_until_the_hard_limit(sw):
    # Claude / Codex / pi show an elapsed timer while a long tool runs (sleep 420, a 10-minute pytest).
    f = sw
    dispatch_one(f)
    t0 = time.time()
    states = run(f, t0, range(0, 1200, 50), screen=lambda dt: WORKING + ' (' + str(dt) + 's)')
    assert set(states) == {'working'} and keys(f, 'worker-1') == []
    # The transcript has not moved for stall_timeout: the spinner does not hide a dead stream for ever.
    assert run(f, t0, [1200], screen=lambda dt: WORKING + ' (1200s)') == ['stalled']
    assert keys(f, 'worker-1') == ['Escape']


def test_cpu_above_the_threshold_keeps_it_working(sw):
    f = sw
    dispatch_one(f)
    t0 = time.time()
    # A busy child (pytest) gains 20 CPU s per 100 s: past the 15 s / window threshold, the window restarts.
    states = run(f, t0, range(0, 1000, 100), cpu=lambda dt: 10.0 + dt * .2)
    assert set(states) == {'working'} and keys(f, 'worker-1') == []
    assert float(f.agent('worker-1')['quiet_since']) > t0


def test_idle_tui_cpu_creep_does_not_count_as_moving(sw):
    # Measured 2026-10-14: an idle Claude TUI (pasted, Enter never pressed) burns 0.6-1.5 % of a core.
    f = sw
    dispatch_one(f)
    t0 = time.time()
    states = run(f, t0, [0, 100, 200, 300], cpu=lambda dt: 10.0 + dt * .015)
    assert states == ['working', 'working', 'working', 'stalled']


def test_a_child_exiting_lowers_the_total_without_restarting_the_window(sw):
    f = sw
    dispatch_one(f)
    t0 = time.time()
    states = run(f, t0, [0, 100, 200, 300], cpu=lambda dt: 50.0 if dt == 0 else 10.0)
    assert states == ['working', 'working', 'working', 'stalled']


def test_cpu_gain_accumulates_over_checks(sw):
    # 4 CPU s per 100 s check: no single step passes 15 s, the sum does at 400 s -> the window restarted.
    f = sw
    dispatch_one(f)
    t0 = time.time()
    states = run(f, t0, [0, 100, 200, 300], cpu=lambda dt: 10.0 + dt * .06)
    assert states == ['working'] * 4      # 18 s gained within the window
    assert float(f.agent('worker-1')['quiet_since']) == pytest.approx(t0 + 300)


def test_transcript_activity_restarts_the_window(sw):
    f = sw
    dispatch_one(f)
    t0 = time.time()
    assert run(f, t0, [0, 100]) == ['working', 'working']
    transcript = f.agent('worker-1')['transcript']
    os.utime(transcript, (t0 + 200, t0 + 200))      # the agent wrote again
    assert run(f, t0, [200, 300, 499]) == ['working'] * 3
    assert run(f, t0, [500]) == ['stalled']


def test_an_unreadable_signal_never_proves_stillness(sw):
    f = sw
    dispatch_one(f)
    pane = f.agent('worker-1')['pane']
    t0 = time.time()
    f.mgr.process_cpu = lambda p: None               # ps failed / no probe: only the hard limit applies
    assert set(run(f, t0, range(0, 1200, 100))) == {'working'}
    assert run(f, t0, [1200]) == ['stalled']
    f.mgr.process_cpu = lambda p: 10.0
    f.mgr.screens[pane] = ''                          # capture failed: the screen is unknown, not still
    f.setf('worker-1', watch_state='working', stall_escapes=0, watch_since=0)
    os.utime(f.agent('worker-1')['transcript'], (t0 + 1300, t0 + 1300))
    assert set(run(f, t0, [1300, 1400, 1500, 1700])) == {'working'}


def test_window_stall_keeps_the_escape_ladder_and_freezes_after_grace(sw):
    f = sw
    dispatch_one(f)
    old = f.agent('worker-1')['pane']
    t0 = time.time()
    run(f, t0, [0, 300])
    assert f.agent('worker-1')['watch_state'] == 'stalled'
    # Escape changed the screen; the transcript did not move: still stalled, frozen once the grace is over.
    states = run(f, t0, [400, 500], screen=lambda dt: WORKING + ' interrupted ' + str(dt))
    assert states == ['stalled', 'stalled'] and keys(f, 'worker-1') == ['Escape'] * 3
    run(f, t0, [601], screen=lambda dt: WORKING + ' interrupted')
    assert ('stalled', 'frozen') in transitions(f, 'worker-1') and old in f.mgr.killed
    assert f.agent('worker-1')['quiet_since'] == '0'   # the next generation starts a fresh window


def test_settled_slot_drops_its_window_baseline(sw):
    f = sw
    dispatch_one(f)
    t0 = time.time()
    run(f, t0, [0, 100])
    assert f.agent('worker-1')['quiet_since'] != '0'
    f.r.hset(f.v2.bus.lease_key('worker-1'), mapping={'status': 'idle', 'current_delivery': ''})
    f.v2.watchdog.tick(t0 + 200)
    assert f.agent('worker-1')['quiet_since'] == '0' and f.agent('worker-1')['watch_state'] == 'idle'


def test_window_off_leaves_only_the_hard_limit(sw):
    f = sw
    f.cfg['stall_window'] = 0
    dispatch_one(f)
    t0 = time.time()
    assert set(run(f, t0, range(0, 1200, 100))) == {'working'}
    assert run(f, t0, [1200]) == ['stalled']


# ------------------------------------------------------------------ Stop deferred = waiting on its own background tasks
# fleet-20261110-001730-b05647bd g1 / g2 / g3: the Claude turn ended with a background pytest still running (Stop
# carried background_tasks -> turn.deferred); the empty prompt sat still for stall_window, Escape on it changed
# nothing, 3 escapes -> frozen -> the session and its pytest were killed. The daemon marks the slot (bg_wait =
# the delivery whose Stop was deferred, bg_wait_act = the transcript mtime then); the window rule skips it.
BG_GRACE = 30  # watchdog.BG_WAIT_GRACE: Claude Code writes the hook result to the transcript ~1 s after Stop


def bg_mark(f, slot='worker-1', delivery=None, act=None):
    """What the daemon writes on a turn_deferred event of the slot's in-flight delivery."""
    info = f.agent(slot)
    act = f.last_activity(info) if act is None else act
    f.setf(slot, bg_wait=delivery or f.v2.bus.slot_state(slot)['current_delivery'], bg_wait_at=time.time(),
           bg_wait_act=act, bg_tasks=1)
    return act


def test_deferred_stop_is_not_quiet_past_the_window(sw):
    f = sw
    dispatch_one(f)
    t0 = time.time()
    bg_mark(f)
    assert run(f, t0, [0, 100, 200, 310]) == ['working'] * 4
    assert keys(f, 'worker-1') == [] and f.agent('worker-1')['stall_escapes'] == '0'
    assert f.agent('worker-1')['bg_wait'] == f.v2.bus.slot_state('worker-1')['current_delivery']


def test_hook_result_write_keeps_the_mark_a_real_wake_clears_it(sw):
    f = sw
    dispatch_one(f)
    t0 = time.time()
    act = bg_mark(f)
    transcript = f.agent('worker-1')['transcript']
    os.utime(transcript, (act + 1, act + 1))          # Claude Code writes the Stop hook result ~1 s later
    assert run(f, t0, [100, 410]) == ['working'] * 2
    assert f.agent('worker-1')['bg_wait'] == f.v2.bus.slot_state('worker-1')['current_delivery']
    os.utime(transcript, (act + 60, act + 60))        # the background task finished and woke the turn
    assert run(f, t0, [420]) == ['working']
    assert f.agent('worker-1')['bg_wait'] == ''
    assert run(f, t0, [500, 600, 719]) == ['working'] * 3
    assert run(f, t0, [720]) == ['stalled'] and keys(f, 'worker-1') == ['Escape']   # window from t0 + 420


@pytest.mark.parametrize('mark', [
    dict(delivery='d-another-delivery'),               # an earlier delivery's Stop was deferred
    dict(act_back=BG_GRACE + 10),                      # the transcript moved past the grace since the mark
])
def test_a_mark_of_another_delivery_or_an_old_transcript_does_not_count(sw, mark):
    f = sw
    dispatch_one(f)
    t0 = time.time()
    act = f.last_activity(f.agent('worker-1'))
    bg_mark(f, delivery=mark.get('delivery'), act=act - mark.get('act_back', 0))
    assert run(f, t0, [0, 299]) == ['working'] * 2
    assert run(f, t0, [300]) == ['stalled'] and keys(f, 'worker-1') == ['Escape']
    assert f.agent('worker-1')['bg_wait'] == ''


def test_deferred_stop_keeps_the_hard_limit_and_the_frozen_ladder(sw):
    f = sw
    dispatch_one(f)
    old = f.agent('worker-1')['pane']
    t0 = time.time()
    bg_mark(f)
    assert set(run(f, t0, [0, 300, 600, 1100])) == {'working'}
    # The background task hangs: the transcript does not move for stall_timeout -> stalled, Escape ladder, frozen.
    assert run(f, t0, [1200]) == ['stalled'] and keys(f, 'worker-1') == ['Escape']
    run(f, t0, [1300, 1400, 1500])
    assert ('stalled', 'frozen') in transitions(f, 'worker-1') and old in f.mgr.killed
    assert f.agent('worker-1')['bg_wait'] == ''        # the next generation starts unmarked


def test_deferred_stop_keeps_the_uninterruptible_rule(sw):
    f = sw
    dispatch_one(f)
    old = f.agent('worker-1')['pane']
    t0 = time.time()
    bg_mark(f)
    f.mgr.stats[old] = ['Ss', 'U+']
    run(f, t0, [0, 1])
    assert ('working', 'frozen') in transitions(f, 'worker-1') and old in f.mgr.killed


# ------------------------------------------------------------------ the advisor tab is not exempt (§27)
def test_resident_advisor_quiet_for_the_window_is_stalled_too(res):  # noqa: F811
    """A consultation quiet on all three signals for 300 s is already past ADVICE_WAIT (180 s): its answer
    would be dropped as late, and every queued consultation would wait behind it. Same rule as any slot."""
    from test_advisor_resident import dispatch, ready_advisor
    from test_v2_codex_claude import submit
    f = res
    f.cfg.update(stall_timeout=1200, stall_window=300, stall_cpu_pct=5, watch_interval=0)
    f.mgr.capture_output = lambda pane, lines=2000: WORKING
    f.mgr.process_cpu = lambda pane: 10.0
    f.mgr.owns = lambda pane: pane in f.mgr.alive
    submit(f)
    dispatch(f, ('worker-1', 'Research the semis rotation into worker-1/notes.md'))
    f.pump()
    ready_advisor(f)
    f.pump()
    assert f.agent('advisor')['state'] == 'busy'
    assert f.v2.bus.slot_state('advisor').get('status') == 'received'
    t0 = time.time()
    states = run(f, t0, [0, 299], slot='advisor')
    assert states == ['working', 'working']
    assert run(f, t0, [300], slot='advisor') == ['stalled']
    assert [k for p, k in f.mgr.keys if p == f.agent('advisor')['pane']] == ['Escape']
    assert f.task()['status'] == 'running' and not f.paused()


# ------------------------------------------------------------------ CPU probe
@pytest.mark.parametrize('text,seconds', [
    ('0:01.55', 1.55), ('12:03.50', 723.5), ('1:02:03.50', 3723.5),   # macOS [H:]MM:SS.cc
    ('00:00:07', 7.0), ('2-01:00:00', 176400.0),                       # Linux [DD-]HH:MM:SS
])
def test_ps_time_formats(text, seconds):
    assert watchdog.ps_seconds(text) == pytest.approx(seconds)


PS = """    1     0 ??       10:00.00
  100     1 ttys003   0:00.10
  101   100 ttys003   1:00.50
  102   101 ??        0:30.00
  103   102 ??        0:05.25
  200     1 ttys004   9:00.00
  201   200 ??        0:44.00
"""


def fake_ps(monkeypatch, stdout=PS, returncode=0):
    calls = []

    def runner(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr='')
    monkeypatch.setattr(subprocess, 'run', runner)
    return calls


def test_tree_cpu_counts_detached_tool_children_of_the_tty(monkeypatch):
    # Claude's Bash tool children run in their own session (tty ??): found by ppid, not by tty.
    calls = fake_ps(monkeypatch)
    assert watchdog.pane_tty_cpu(['/dev/ttys003']) == pytest.approx(.10 + 60.5 + 30 + 5.25)
    assert calls == [['ps', '-axo', 'pid=,ppid=,tty=,time=']]
    assert watchdog.pane_tty_cpu(['/dev/ttys003', 'ttys004']) == pytest.approx(95.85 + 540 + 44)


def test_tree_cpu_is_none_when_unreadable(monkeypatch):
    fake_ps(monkeypatch)
    assert watchdog.pane_tty_cpu(['/dev/ttys009']) is None          # nothing on that tty
    assert watchdog.pane_tty_cpu([None, '']) is None
    fake_ps(monkeypatch, returncode=1)
    assert watchdog.pane_tty_cpu(['/dev/ttys003']) is None


def test_tree_cpu_of_this_process_is_readable():
    tty = subprocess.run(['ps', '-o', 'tty=', '-p', str(os.getpid())], capture_output=True, text=True).stdout.strip()
    if tty in ('', '??', '?'):
        assert watchdog.pane_tty_cpu(['/dev/ttys999999']) is None
    else:
        assert watchdog.pane_tty_cpu(['/dev/' + tty]) >= 0


# ------------------------------------------------------------------ config / roster / named sub-fleet
def test_defaults_and_validation():
    assert fleet.DEFAULTS['stall_timeout'] == 1200
    assert watchdog.DEFAULTS['stall_window'] == 300 and watchdog.DEFAULTS['stall_cpu_pct'] == 5
    cfg = dict(transport_version=2, supervisor_cli='codex', worker_cli='claude', mode='task', allow_development_v2=True)
    assert runtime_v2.validate_config(dict(cfg, stall_window=0, stall_cpu_pct=0)) == 2
    for key in ('stall_window', 'stall_cpu_pct'):
        with pytest.raises(ValueError, match=key):
            runtime_v2.validate_config(dict(cfg, **{key: -1}))
        with pytest.raises(ValueError, match=key):
            runtime_v2.validate_config(dict(cfg, **{key: 'x'}))


def test_start_flags(monkeypatch):
    seen = {}
    monkeypatch.setattr(fleet, 'start', lambda args: seen.update(vars(args)))
    fleet.main(['start'])
    assert (seen['stall_timeout'], seen['stall_window'], seen['stall_cpu_pct']) == (1200, 300, 5)
    fleet.main(['start', '--stall-window', '420', '--stall-cpu-pct', '8'])
    assert (seen['stall_window'], seen['stall_cpu_pct']) == (420, 8)


def test_named_subfleet_can_set_its_stall_window(tmp_path):
    (tmp_path / 'sup.md').write_text('x')
    (tmp_path / 'child.yaml').write_text('workers:\n  - name: a\n')
    base = 'workers:\n  - name: worker-1\n'
    good = tmp_path / 'good.yaml'
    good.write_text(base + 'subfleets:\n  - {name: ops, supervisor_slot: worker-1, supervisor_role: sup.md, '
                    'roster: child.yaml, timeouts: {stall: 900, stall_window: 420}}\n')
    [e] = roster.load_subfleets(good)
    assert e['stall_timeout'] == 900 and e['stall_window'] == 420
    bad = tmp_path / 'bad.yaml'
    bad.write_text(base + 'subfleets:\n  - {name: ops, supervisor_slot: worker-1, supervisor_role: sup.md, '
                   'roster: child.yaml, timeouts: {stall_window: 0}}\n')
    with pytest.raises(ValueError, match='stall / stall_window / request to positive seconds'):
        roster.load_entries(bad)
    # The entry value, else the built-in default: never the parent's.
    parent = {'stall_window': 77, 'stall_timeout': 88, 'worker_cli': 'codex'}
    assert subfleet.child_worker_cfg(parent, {'stall_window': 420})['stall_window'] == 420
    child = subfleet.child_worker_cfg(parent, {})
    assert 'stall_window' not in child and watchdog.setting(child, 'stall_window') == 300
    assert child['stall_timeout'] == fleet.DEFAULTS['stall_timeout']
