"""`fleetmux demo` pieces that need no fleet: dependency rows, the scripted replies, the banner, the summary read
from audit records, the isolated world and its clean-up (also on Ctrl-C), and the scoped orphan-Redis sweep."""
import io
import json
import os
import signal
import sys
from pathlib import Path

import pytest

from fleetmux import demo, demo_actor, discover, fleet

FAIL_REASON = 'artifact not written: ' + demo_actor.TEST


# ------------------------------------------------------------------ dependencies
def test_missing_or_old_program_is_named_and_nothing_starts(monkeypatch, capsys):
    found = {'redis-server': '/x/redis-server', 'git': '/x/git', 'fleetmux': '/x/fleetmux'}
    versions = {'/x/redis-server': 'Redis server v=6.2.0', '/x/git': 'git version 2.40.0', '/x/fleetmux': 'fleetmux 0.2.0'}
    monkeypatch.delenv('FLEETMUX_BIN', raising=False)
    monkeypatch.setattr(demo.shutil, 'which', lambda name: None)
    rows, paths, missing = demo.check_dependencies(which=found.get, probe=lambda cmd: versions[cmd[0]])
    assert missing == ['redis-server', 'tmux']
    assert rows[0].startswith('FAIL  redis-server 6.2.0 at /x/redis-server (need 7.0.0)')
    assert rows[1].startswith('FAIL  tmux missing (need 3.4)')
    assert rows[2].startswith('ok    git 2.40.0') and rows[3].startswith('ok    fleetmux 0.2.0')
    made = []
    monkeypatch.setattr(demo, 'check_dependencies', lambda: (rows, paths, missing))
    monkeypatch.setattr(demo, 'World', lambda *a, **k: made.append(1))
    assert demo.run(no_view=True) == 1
    out = capsys.readouterr().out
    assert 'nothing is installed by fleetmux' in out and 'FAIL  tmux missing' in out
    assert not made  # no directory, no fleet


# ------------------------------------------------------------------ the scripted actor
def test_version_says_scripted_and_meets_the_deps_minimum(capsys):
    assert demo_actor.main(['--version']) == 0
    text = capsys.readouterr().out
    assert 'scripted' in text and 'no model' in text
    assert discover.below_min('claude', text) is None


def test_script_bounce_then_fix(tmp_path):
    ws = tmp_path / 'workspace'
    (ws / 'worker-1').mkdir(parents=True)
    sup = demo_actor.Script('supervisor', tmp_path / 'agents' / 'supervisor')
    work = demo_actor.Script('worker-1', ws / 'worker-1')
    assert sup.answer('There is no task yet: your final reply must be exactly READY.') == 'READY'
    first = json.loads(sup.answer('Goal: ' + demo_actor.GOAL_MARK + ' Write ...'))
    assert first['status'] == 'RUNNING' and first['dispatch'][0]['to'] == 'worker-1'
    report = work.answer('[M0] ' + first['dispatch'][0]['instruction'])
    assert report.startswith('Done: [scripted worker-1]')  # the AGENTS.md format, and the claim is false:
    assert (ws / demo_actor.SLUG).is_file() and not (ws / demo_actor.TEST).exists()
    claim = json.loads(sup.answer('[worker-1 report]\n{"done": ["[scripted worker-1] wrote it"]}'))
    assert claim['status'] == 'COMPLETED'
    fix = json.loads(sup.answer('Independent verification failed, the task cannot end: ' + FAIL_REASON
                                + '\nDispatch corrective instructions to the workers (RUNNING + dispatch).'))
    assert fix['status'] == 'RUNNING' and FAIL_REASON in fix['dispatch'][0]['instruction']  # the program's words
    work.answer(fix['dispatch'][0]['instruction'])
    assert (ws / demo_actor.TEST).is_file()
    for reply in (first['reason'], claim['reason'], fix['reason'], report):
        assert 'scripted' in reply


class FakeTerm:
    def __init__(self, rows=30, cols=100):
        self.rows, self.cols, self.lines = rows, cols, None

    def size(self):
        return self.rows, self.cols

    def draw(self, lines, cursor):
        self.lines = lines


def test_banner_and_scripted_label_stay_on_screen(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'h'))
    cwd = tmp_path / 'workspace' / 'worker-1'
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)
    actor = demo_actor.Actor(['--name', 'worker-1', '--', 'There is no task yet: reply exactly READY'])
    term = FakeTerm()
    for _ in range(40):  # a long conversation scrolls; the two head lines stay
        actor.feed.append(('in', 'x' * 300))
        actor.render(term)
        assert demo_actor.BANNER in term.lines[0] and '[scripted] worker-1' in term.lines[1]
    assert any(line.startswith('❯') for line in term.lines)  # the editor claude_input looks for
    assert str(tmp_path / 'h') in str(actor.transcript)  # the transcript lives under the given HOME


# ------------------------------------------------------------------ the summary comes from audit records
def test_summary_reads_the_verdict_records():
    recs = [{'event': 'acceptance.verdict', 'at': 1000, 'detail': {'mode': 'task_artifacts', 'ok': 'false',
             'problem': FAIL_REASON, 'claimed_by': 'supervisor'}},
            {'event': 'acceptance.verdict', 'audit': {'at': 5000, 'mode': 'task_artifacts', 'ok': 'true',
             'problem': '', 'claimed_by': 'supervisor'}}]
    lines = demo.summary_lines(recs, started=0.5)
    assert 'REJECTED' in lines[2] and 'reason: ' + FAIL_REASON in lines[2] and 'PASS' in lines[3]
    assert '(+0.5s)' in lines[2] and '(+4.5s)' in lines[3]
    assert 'no acceptance.verdict' in demo.summary_lines([], 0)[2]


# ------------------------------------------------------------------ the world, clean-up, Ctrl-C
PATHS = {'redis-server': '/x/a/redis-server', 'tmux': '/x/a/tmux', 'git': '/x/b/git', 'fleetmux': '/x/c/fleetmux'}


def test_world_is_private_and_the_launcher_is_off_path(monkeypatch):
    monkeypatch.setenv('FLEETMUX_CONFIG', '/elsewhere/config.json')
    monkeypatch.setenv('CLAUDE_CODE_SESSION_ID', 'outer')
    w = demo.World(PATHS)
    try:
        assert w.root.parent == Path('/tmp').resolve() and w.root.name.startswith('fmxd-')
        assert oct(w.root.stat().st_mode & 0o777) == '0o700'
        env = w.env()
        assert env['HOME'] == str(w.home) and env['FLEETMUX_HOME'] == str(w.fh) and env['FLEETMUX_STATE'] == str(w.state)
        assert env[demo.SWEEP_ENV] == str(w.redis_root)
        assert 'FLEETMUX_CONFIG' not in env and 'CLAUDE_CODE_SESSION_ID' not in env
        assert str(w.bin) not in env['PATH'].split(os.pathsep)  # bin/claude is named only by --claude
        assert 'scripted' in w.claude.read_text() and os.access(w.claude, os.X_OK)
        assert len(os.fsencode(str(w.redis_root / ('0' * 32) / 'redis.sock'))) <= 103
    finally:
        demo.cleanup(w)
    assert not w.root.exists()


def test_ctrl_c_cleans_up(monkeypatch, capsys):
    monkeypatch.setattr(demo, 'check_dependencies', lambda: ([], PATHS, []))
    worlds = []

    def start(self):
        worlds.append(self)
        os.kill(os.getpid(), signal.SIGINT)  # Ctrl-C while the fleet starts

    monkeypatch.setattr(demo.World, 'start', start)
    before = signal.getsignal(signal.SIGINT)
    assert demo.run(no_view=True) == 130
    out = capsys.readouterr().out
    assert 'Interrupted: cleaning up.' in out and 'removed' in out and out.rstrip().endswith(demo_actor.BANNER)
    assert not worlds[0].root.exists()
    assert signal.getsignal(signal.SIGINT) is before  # the caller's handler is back


def test_keep_leaves_the_directory(monkeypatch, capsys):
    monkeypatch.setattr(demo, 'check_dependencies', lambda: ([], PATHS, []))
    worlds = []
    monkeypatch.setattr(demo.World, 'start', lambda self: worlds.append(self) or (_ for _ in ()).throw(
        RuntimeError('start failed (exit 1): test')))
    assert demo.run(keep=True, no_view=True) == 1
    try:
        assert worlds[0].root.is_dir() and 'Kept for inspection' in capsys.readouterr().out
    finally:
        demo.cleanup(worlds[0])


# ------------------------------------------------------------------ the orphan-Redis sweep of `start`
def test_start_sweep_is_limited_to_the_demo_redis_root(monkeypatch, _isolation_mp):
    # conftest replaces fleet.sweep_orphan_redis with a no-op for every test (no test may touch a real Redis);
    # the function under test is the original it saved
    real = next(value for obj, name, value in _isolation_mp._setattr
                if obj is fleet and name == 'sweep_orphan_redis')
    calls = []
    monkeypatch.setattr(fleet, 'reap_orphan_redis', lambda keep=None, root=None: calls.append(root))
    monkeypatch.delenv(demo.SWEEP_ENV, raising=False)
    real()
    monkeypatch.setenv(demo.SWEEP_ENV, '/tmp/fmxd-x/r')
    real()
    assert calls == [None, '/tmp/fmxd-x/r']  # unset: every abandoned run as before; set: that root only


def test_demo_is_a_fleetmux_command(capsys):
    with pytest.raises(SystemExit) as done:
        fleet.main(['demo', '--help'])
    out = ' '.join(capsys.readouterr().out.split())  # argparse wraps the description
    assert done.value.code == 0 and demo_actor.BANNER in out and '--keep' in out and '--no-view' in out


def test_status_falls_back_to_result_json_once_the_run_closed_its_redis(tmp_path):
    class Gone:
        def hget(self, *_):
            raise ConnectionError('Error 2 connecting to /tmp/fmxd-x/r/1/redis.sock')

    world = type('W', (), {'cfg': {'run': str(tmp_path)}})()
    assert demo.task_status(world, Gone()) == 'closed'
    (tmp_path / 'result.json').write_text(json.dumps({'status': 'completed'}))
    assert demo.task_status(world, Gone()) == 'completed'


# ------------------------------------------------------------------ the window view (desktop, not ssh)
def test_desktop_opens_a_window_in_its_own_session_and_closes_its_group(monkeypatch):
    started, killed = [], []

    class Proc:
        pid = 4242

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    def popen(argv, **kw):
        started.append((argv, kw))
        return Proc()

    w = demo.World(PATHS)  # before the stand-ins below: it runs real processes (the liveness marker probe)
    monkeypatch.setattr(fleet, 'window_here', lambda binary: True)
    monkeypatch.setattr(fleet, 'bring_window_to_front', lambda pid: started.append(('front', pid)))
    monkeypatch.setattr(demo, 'runtime_of', lambda world: '/tmp/fmxd-x/s/fleetmux/tok')
    monkeypatch.setattr(demo.subprocess, 'Popen', popen)
    monkeypatch.setattr(demo.os, 'killpg', lambda pgid, sig: killed.append((pgid, sig)))
    try:
        assert demo.open_view(w, no_view=True) is None and not started  # --no-view: nothing opens
        assert demo.open_view(w, no_view=False).pid == 4242 and w.view_kind == 'window'
        argv, kw = started[0]
        assert argv == [PATHS['fleetmux'], 'gui', '--runtime', '/tmp/fmxd-x/s/fleetmux/tok']
        assert kw['start_new_session'] and kw['env']['HOME'] == str(w.home)
        if sys.platform == 'darwin':
            w.raising.join(5)  # the raise runs on its own thread
            assert ('front', 4242) in started
        demo.close_view(w)
        assert killed == [(4242, signal.SIGTERM)]  # the window's whole group: fleetmux gui and its fleetmux-web
    finally:
        w.view = None
        monkeypatch.undo()  # the Popen / killpg stand-ins are global: the clean-up runs on the real ones
        demo.cleanup(w)


def test_clean_up_finds_a_half_started_run_on_disk():
    w = demo.World(PATHS)
    try:
        run = w.runs / 'fleet-x'
        run.mkdir()
        (run / 'config.json').write_text(json.dumps({'run': str(run), 'redis_socket': str(w.redis_root / 'a/redis.sock')}))
        assert demo.found_config(w)['run'] == str(run) and w.cfg_path == run / 'config.json'
    finally:
        demo.cleanup(w)
    assert not w.root.exists()


def test_summary_waits_for_the_passing_verdict():
    class Audit:
        error = None

        def __init__(self):
            self.calls, self.stop_flag = 0, type('E', (), {'set': lambda self: None})()

        def is_alive(self):
            return True

        def verdicts(self):
            self.calls += 1
            first = {'event': 'acceptance.verdict', 'detail': {'ok': 'false'}}
            return [first] if self.calls < 4 else [first, {'event': 'acceptance.verdict', 'detail': {'ok': 'true'}}]

    got = demo.settle_audit(Audit(), 'completed', limit=5)
    assert [v['detail']['ok'] for v in got] == ['false', 'true']


def test_liveness_marker_is_a_sleep_named_claude_inside_the_demo_directory():
    w = demo.World(PATHS)
    try:
        assert w.live == w.bin / 'live' / 'claude' and w.live.is_symlink()
        assert os.path.realpath(w.live) in [os.path.realpath(p) for p in demo.SLEEPS]
        assert w.env()[demo.LIVE_ENV] == str(w.live)
        assert str(w.live.parent) not in w.env()['PATH'].split(os.pathsep)
    finally:
        demo.cleanup(w)


# ------------------------------------------------------------------ narrow panes: escape sequences and the banner
SGR = __import__('re').compile(r'\x1b\[[0-?]*[ -/]*[@-~]')


def test_clip_counts_only_visible_text_and_ends_with_a_reset():
    line = '\x1b[1;7m ' + demo_actor.BANNER + ' \x1b[0m'
    for cols in (3, 12, 40):
        out = demo_actor.clip(line, cols)
        assert demo_actor.width_of(SGR.sub('', out)) == cols  # the escape bytes take no columns
        assert out.startswith('\x1b[1;7m') and out.endswith('\x1b[0m')  # cut before the original reset: one added
    whole = demo_actor.clip(line, 200)
    assert SGR.sub('', whole) == SGR.sub('', line) and whole.endswith('\x1b[0m')
    assert demo_actor.clip('plain ✻ text', 7) == 'plain ✻'  # no escape in, none added


def test_narrow_banner_does_not_leak_bold_reverse_into_later_text():
    import pyte
    rows, cols = 3, 20
    screen = pyte.Screen(cols, rows)
    stream = pyte.Stream(screen)
    term = object.__new__(demo_actor.Term)
    term.size = lambda: (rows, cols)
    term.out = lambda *parts: stream.feed(''.join(parts))
    term.draw(['\x1b[1;7m ' + demo_actor.BANNER + ' \x1b[0m', 'after the banner', ''], (3, 1))
    first, second = screen.buffer[0], screen.buffer[1]
    assert first[0].reverse and first[cols - 1].reverse  # the banner itself keeps its look
    assert ''.join(second[x].data for x in range(16)) == 'after the banner'
    assert not any(second[x].reverse or second[x].bold for x in range(cols))
    assert not any(screen.buffer[2][x].reverse for x in range(cols))


# ------------------------------------------------------------------ the window raise runs beside the demo
def _window_world(monkeypatch, front):
    class Proc:
        pid = 4242

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    w = demo.World(PATHS)
    monkeypatch.setattr(fleet, 'window_here', lambda binary: True)
    monkeypatch.setattr(fleet, 'bring_window_to_front', front)
    monkeypatch.setattr(demo, 'runtime_of', lambda world: '/tmp/fmxd-x/s/fleetmux/tok')
    monkeypatch.setattr(demo.subprocess, 'Popen', lambda argv, **kw: Proc())
    monkeypatch.setattr(demo.sys, 'platform', 'darwin')
    return w


def test_a_slow_window_raise_does_not_hold_up_the_demo(monkeypatch, capsys):
    import threading
    import time
    release, raised = threading.Event(), []

    def slow_front(pid):  # bring_window_to_front waits for fleetmux-web and retries osascript: seconds
        release.wait(10)
        raised.append(pid)

    w = _window_world(monkeypatch, slow_front)
    try:
        t0 = time.monotonic()
        assert demo.open_view(w, no_view=False).pid == 4242
        assert time.monotonic() - t0 < 1.0 and not raised  # back at once, the raise still going
        assert 'Opened a window' in capsys.readouterr().out
        release.set()
        w.raising.join(5)
        assert raised == [4242]
    finally:
        release.set()
        w.view = None
        monkeypatch.undo()
        demo.cleanup(w)


def test_a_failing_window_raise_does_not_fail_the_demo(monkeypatch, capsys):
    def broken_front(pid):
        raise OSError('osascript not allowed')

    w = _window_world(monkeypatch, broken_front)
    try:
        assert demo.open_view(w, no_view=False).pid == 4242
        w.raising.join(5)
        assert not w.raising.is_alive() and w.view_kind == 'window'
        assert 'Traceback' not in capsys.readouterr().err
    finally:
        w.view = None
        monkeypatch.undo()
        demo.cleanup(w)
