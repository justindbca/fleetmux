"""Dual display of a sub-fleet's supervisor_slot (docs/design.md §19 / named sub-fleet §3.1): real tmux.

The CLI runs in the fleet's own tmux server (private socket, the fleet's minimal config); cmux tabs are
only its clients. cmux is faked, but each fake tab runs its command in a real pty, so the tmux clients,
``detach-client``, ``kill-session`` and the tab ending by itself (``exec``) are all real. The fake CLI is
a symlink to ``sleep`` named ``claude`` so ``has_cli_process`` sees the name the fleet looks for.
"""
import json
import os
from pathlib import Path
import pty
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from types import SimpleNamespace

import pytest

import _fake_claude
from fleetmux import cmux_manager as cm

TMUX = shutil.which('tmux') or '/opt/homebrew/bin/tmux'
pytestmark = pytest.mark.skipif(not Path(TMUX).exists(), reason='tmux unavailable')


class Tab:
    def __init__(self, command):
        master, slave = pty.openpty()
        self.tty = os.path.basename(os.ttyname(slave))
        env = dict(os.environ, TERM='xterm-256color')
        env.pop('TMUX', None)
        self.proc = subprocess.Popen(['/bin/bash', '-c', command], stdin=slave, stdout=slave, stderr=slave,
                                     env=env, start_new_session=True)
        os.close(slave)
        self.master = master
        threading.Thread(target=self.drain, daemon=True).start()  # a client blocks on a full pty

    def drain(self):
        try:
            while os.read(self.master, 65536):
                pass
        except OSError:
            pass

    def alive(self):
        return self.proc.poll() is None

    def kill(self):
        if self.alive():
            self.proc.kill()
            self.proc.wait(5)


class FakeCmux:
    """Workspaces of tabs; a tab disappears when its process exits (as a cmux tab after ``exec``)."""

    def __init__(self):
        self.ws, self.n, self.calls = {}, 0, []

    def tree(self, ws):
        panes = {}
        for sid, tab in self.ws.get(ws, {}).items():
            if tab.alive():
                panes.setdefault(tab.pane, []).append({'id': sid, 'pane_id': tab.pane, 'tty': tab.tty, 'title': ''})
        return {'windows': [{'workspaces': [{'id': ws, 'title': getattr(self, 'title_' + ws, ''),
                                             'panes': [{'surfaces': s} for s in panes.values()]}]}]}

    def new_tab(self, ws, command, pane=None):
        self.n += 1
        sid = 'sf-' + str(self.n)
        tab = Tab(command or 'exec sleep 600')
        tab.pane = pane or 'pane-' + sid
        self.ws.setdefault(ws, {})[sid] = tab
        return sid

    def __call__(self, *args):
        self.calls.append(args)
        a = [x for x in args if x not in ('--json', '--id-format', 'uuids')]
        opt = lambda k: a[a.index(k) + 1] if k in a else None
        ok = lambda out='OK': SimpleNamespace(returncode=0, stdout=out, stderr='')
        cmd = a[0]
        if cmd == 'tree':
            ws = opt('--workspace')
            return ok(json.dumps(self.tree(ws))) if ws in self.ws else SimpleNamespace(returncode=1, stdout='', stderr='')
        if cmd == 'new-surface':
            sid = self.new_tab(opt('--workspace'), opt('--command'), pane=opt('--pane'))
            return ok('OK ' + sid)
        if cmd == 'new-workspace':
            ws = 'ws-' + uuid.uuid4().hex[:6]
            setattr(self, 'title_' + ws, opt('--name'))
            self.new_tab(ws, opt('--command'))
            return ok('OK ' + ws)
        if cmd == 'move-surface':
            for tabs in self.ws.values():
                if opt('--surface') in tabs:
                    tabs[opt('--surface')].pane = opt('--pane')
            return ok()
        if cmd == 'close-surface':
            tab = self.ws.get(opt('--workspace'), {}).get(opt('--surface'))
            if tab is not None:
                tab.kill()
            return ok()
        if cmd == 'current-workspace':
            return ok('')
        return ok()  # rename-tab, select-workspace, focus-panel, close-workspace, send-key ...

    def tabs(self):
        return [tab for tabs in self.ws.values() for tab in tabs.values()]


def world_root(env=None, name=None):
    """The world's short run root and whether it is kept. Outside the m4 gate: ``/tmp/cft-<8 hex>``, removed by the
    fixture as before. In the m4 gate (``FLEETMUX_GATE=m4``, brief-m4 M60): a new ``$FM4_TEST_ROOT/cft-<8 hex>``,
    kept; ``FM4_TEST_ROOT`` must be an existing absolute directory inside ``M4E_ROOT`` (``scripts/fleetmux_gate.sh``
    exports both), the path must not exist yet (never reused) and its ``run/tmux.sock`` must fit 103 bytes."""
    env = os.environ if env is None else env
    name = name or 'cft-' + uuid.uuid4().hex[:8]
    if env.get('FLEETMUX_GATE') != 'm4':
        return Path('/tmp') / name, False  # short: macOS Unix socket path limit
    test_root, base = env.get('FM4_TEST_ROOT') or '', env.get('M4E_ROOT') or ''
    if not test_root or not base:
        pytest.fail('FLEETMUX_GATE=m4 needs FM4_TEST_ROOT and M4E_ROOT (scripts/fleetmux_gate.sh exports both)')
    tr, b = Path(test_root), Path(base)
    if not (tr.is_absolute() and b.is_absolute() and tr.is_dir()):
        pytest.fail('m4 roots must be absolute and FM4_TEST_ROOT a directory: %s, %s' % (tr, b))
    if b.resolve() not in tr.resolve().parents:
        pytest.fail('FM4_TEST_ROOT %s is not inside M4E_ROOT %s' % (tr, b))
    root = tr / name
    if len(str(root / 'run' / 'tmux.sock')) > 103:
        pytest.fail('m4 world root %s gives a socket path over 103 bytes' % root)
    try:
        root.mkdir()
    except FileExistsError:
        pytest.fail('m4 world root already exists (never reused): %s' % root)
    print('m4 world root (kept): %s' % root)
    return root, True


def settle_server_gone(w, out, server_pid=None):
    """M60: ``kill-server`` can return while the server is still shutting down. Before a test fakes one
    mid-shutdown answer, the product's own three-state probe (``cm.tmux_server_gone``, its default bounded settle
    ``cm.TMUX_GONE_SETTLE``) must prove that no server is left on this test's own socket. Each probe and each tmux
    call it makes (argv, rc, stdout, stderr, monotonic times) goes to the returned evidence and to ``out``
    (JSON). Not True (False: a server answered; None: not known) fails the test at once, with no retry; so does a
    socket outside this world's root, or a socket file that is gone (tmux never unlinks it, and the probe under
    test starts from it)."""
    ev = {'sock': w.sock, 'root': str(w.root), 'settle_s': cm.TMUX_GONE_SETTLE, 'server_pid': server_pid,
          'probes': [], 'tmux_calls': []}

    def save(verdict):
        ev['verdict'] = verdict
        Path(out).write_text(json.dumps(ev, indent=1, default=str) + '\n')
        return ev

    if Path(w.root) not in Path(w.sock).parents:
        save('foreign socket')
        pytest.fail('settle: socket %s is not inside this world root %s (evidence %s)' % (w.sock, w.root, out))
    real_once, real_run = cm._tmux_probe_once, cm.subprocess.run

    def run(cmd, *a, **kw):
        t0 = time.monotonic()
        try:
            p = real_run(cmd, *a, **kw)
        except Exception as e:
            ev['tmux_calls'].append({'argv': cmd, 't0': t0, 't1': time.monotonic(), 'error': repr(e)})
            raise
        ev['tmux_calls'].append({'argv': cmd, 't0': t0, 't1': time.monotonic(), 'rc': p.returncode,
                                 'stdout': p.stdout, 'stderr': p.stderr})
        return p

    def once(sock):
        t0 = time.monotonic()
        r = real_once(sock)
        ev['probes'].append({'t0': t0, 't1': time.monotonic(), 'result': r})
        return r

    t0 = time.monotonic()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cm, '_tmux_probe_once', once)
        mp.setattr(cm.subprocess, 'run', run)
        gone = cm.tmux_server_gone(w.sock)
    ev.update(gone=gone, elapsed_s=time.monotonic() - t0, sock_exists=Path(w.sock).exists())
    if server_pid:
        try:
            os.kill(server_pid, 0)
            ev['server_pid_alive'] = True
        except ProcessLookupError:
            ev['server_pid_alive'] = False
        except OSError as e:
            ev['server_pid_alive'] = repr(e)
    if gone is not True:
        save('not proven gone')
        pytest.fail('settle: tmux_server_gone(%s) = %r, not True (evidence %s)' % (w.sock, gone, out))
    if not ev['sock_exists']:
        save('socket file gone')
        pytest.fail('settle: %s is gone, so the probe under test has nothing to ask (evidence %s)' % (w.sock, out))
    return save('gone')


def _ps_identity(pid, run=subprocess.run):
    """``(lstart, args)`` of a live, non-zombie ``pid``, read by pid (never by name); ``'gone'`` when no such process
    (or a zombie); None when ps could not be read."""
    try:
        p = run(['ps', '-ww', '-p', str(pid), '-o', 'stat=,lstart=,args='], capture_output=True, text=True,
                timeout=10, env=dict(os.environ, LC_ALL='C'))  # a fixed 5-word lstart
    except (OSError, subprocess.SubprocessError):
        return None
    line = p.stdout.strip()
    if p.returncode != 0 and not line:
        return 'gone'
    parts = line.split(None, 6)
    if len(parts) < 7:
        return 'gone' if not line else None
    if parts[0].startswith('Z'):
        return 'gone'
    return ' '.join(parts[1:6]), parts[6]


def _m4_register(w, fake, run):
    """M60: the processes this world started, each with its identity, from exact handles only: its own live tab
    ``Popen``s, the tmux server and the pane pids its own socket reports, and their descendants (``pgrep -P``, a
    parent link). Nothing is found by name or path."""
    led, errors = [], []

    def add(pid, kind):
        ident = _ps_identity(pid, run)
        if isinstance(ident, tuple):
            led.append({'pid': pid, 'kind': kind, 'lstart': ident[0], 'args': ident[1]})
        elif ident is None:
            errors.append('identity of %s %d unreadable at registration' % (kind, pid))

    for tab in fake.tabs():
        if tab.proc.poll() is None:  # not reaped: the pid is still this Popen's
            add(tab.proc.pid, 'tab')
    roots = []
    if Path(w.sock).exists():
        srv = run([TMUX, '-S', w.sock, 'display-message', '-p', '#{pid}'], capture_output=True, text=True)
        roots += [(int(x), 'tmux server') for x in srv.stdout.split() if x.isdigit()]
        panes = run([TMUX, '-S', w.sock, 'list-panes', '-a', '-F', '#{pane_pid}'], capture_output=True, text=True)
        roots += [(int(x), 'pane') for x in panes.stdout.split() if x.isdigit()]
    seen = {e['pid'] for e in led}
    todo = list(roots)
    while todo:
        pid, kind = todo.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        add(pid, kind)
        kids = run(['pgrep', '-P', str(pid)], capture_output=True, text=True)
        todo += [(int(x), 'descendant of %d' % pid) for x in kids.stdout.split() if x.isdigit()]
    return led, errors


def _m4_world_teardown(w, fake, out, run=subprocess.run, kill=os.kill, sleep=time.sleep, now=time.monotonic,
                       settle=5.0):
    """M60, m4 only: end this world without any pattern kill (no ``pkill``). Register (``_m4_register``), end the
    test's own tmux (``kill-server`` on its socket) and its own tabs (``Popen.kill``), wait up to ``settle`` s, then
    SIGKILL only a registered pid whose identity still matches (checked again just before). Any such fallback, an
    identity that cannot be read, or a process still carrying this test's fake CLI path that was never registered
    (seen with ps, never signalled) fails the test; everything goes to ``out`` (JSON)."""
    ev = {'sock': w.sock, 'claude': w.claude, 'calls': [], 'signalled': [], 'unknown': [], 'strays': []}

    def rec(cmd, *a, **kw):
        ev['calls'].append(list(cmd))
        return run(cmd, *a, **kw)

    led, ev['errors'] = _m4_register(w, fake, rec)
    ev['registered'] = led
    rec([TMUX, '-S', w.sock, 'kill-server'], capture_output=True)
    for tab in fake.tabs():
        tab.kill()

    def matching():
        return [e for e in led if _ps_identity(e['pid'], rec) == (e['lstart'], e['args'])]

    deadline = now() + settle
    while matching() and now() < deadline:
        sleep(0.1)
    for e in led:
        ident = _ps_identity(e['pid'], rec)
        if ident is None:
            ev['unknown'].append(e)  # not signalled: it cannot be told apart from a reused pid
        elif ident == (e['lstart'], e['args']):
            kill(e['pid'], signal.SIGKILL)
            ev['signalled'].append(e)
    rows = rec(['ps', '-axww', '-o', 'pid=,args='], capture_output=True, text=True).stdout.splitlines()
    mine = {e['pid'] for e in led}
    for row in rows:  # evidence only: a survivor that was never registered is reported, never signalled
        pid, _, args = row.strip().partition(' ')
        if pid.isdigit() and int(pid) not in mine and w.claude in args.split():
            ev['strays'].append(row.strip())
    Path(out).write_text(json.dumps(ev, indent=1, default=str) + '\n')
    if ev['errors'] or ev['unknown'] or ev['signalled'] or ev['strays']:
        pytest.fail('m4 world teardown did not end cleanly (evidence %s): errors %s, unknown %s, signalled %s, '
                    'unregistered %s' % (out, ev['errors'], ev['unknown'], ev['signalled'], ev['strays']))
    return ev


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    # A user config that must never be read: a short history and a marker option.
    (home / '.tmux.conf').write_text('set -g history-limit 7\nset -g @poisoned 1\n')
    tmux_tmp = tmp_path / 'tmux-default'
    tmux_tmp.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('TMUX_TMPDIR', str(tmux_tmp))  # where a default server's socket would appear
    monkeypatch.setenv('SHELL', '/bin/sh')
    root, kept = world_root()  # M60: in the m4 gate a new, kept root under FM4_TEST_ROOT
    run = root / 'run'
    run.mkdir(parents=True)
    bin_dir = tmp_path / 'bin' / uuid.uuid4().hex[:8]
    bin_dir.mkdir(parents=True)
    claude = _fake_claude.make(bin_dir)
    fake = FakeCmux()
    monkeypatch.setattr(cm, '_run', fake)
    monkeypatch.setattr(cm, 'TMUX', TMUX)
    w = SimpleNamespace(fake=fake, run=run, sock=str(run / 'tmux.sock'), claude=str(claude), tmux_tmp=tmux_tmp,
                        root=root,
                        parent=cm.CmuxSessionManager('fleetmux', state_dir=tmp_path / 'state'),
                        child=cm.CmuxSessionManager('fleetmux:ops', state_dir=tmp_path / 'state'))
    yield w
    if kept:  # the m4 gate (M60): registered processes only, no pattern kill, nothing removed
        _m4_world_teardown(w, fake, root / 'teardown-ledger.json')
        return
    subprocess.run([TMUX, '-S', w.sock, 'kill-server'], capture_output=True)
    for tab in fake.tabs():
        tab.kill()
    subprocess.run(['pkill', '-f', str(claude)], capture_output=True)  # only this test's own fake CLI
    if not kept:
        shutil.rmtree(root, ignore_errors=True)


def tmux(w, *args):
    return subprocess.run([TMUX, '-S', w.sock, *args], capture_output=True, text=True)


def spawn(w, slot='ops-a', generation=0, lines=0):
    out = w.run / 'prompts' / (slot + '-g' + str(generation) + '.terminal.log')
    out.parent.mkdir(parents=True, exist_ok=True)
    cli = (('seq 1 ' + str(lines) + '; ') if lines else '') + w.claude + ' ' + _fake_claude.args(600)
    res = w.parent.spawn_interactive_cli(window_name='fx-' + slot, cli_command=cli, output_file=str(out),
                                         start_directory=str(w.run), auto_close=False,
                                         tmux_socket=w.sock, tmux_session=cm.tmux_session_name(slot, generation))
    assert res.success, res.error
    return res.pane_id, out


def clients(w, session):
    p = tmux(w, 'list-clients', '-t', '=' + session, '-F', '#{client_tty}')
    return p.stdout.split() if p.returncode == 0 else []


def claude_pids(w):
    """This test's fake CLI processes (the bash wrapper around it does not count). Matched on argv[0]:
    Linux's comm is only the executable's basename, cut to 15 characters."""
    p = subprocess.run(['ps', '-axww', '-o', 'pid=,args='], capture_output=True, text=True)
    rows = [line.split() for line in p.stdout.splitlines()]
    return [row[0] for row in rows if len(row) > 1 and row[1] == w.claude]


def wait(check, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            return False
        time.sleep(.05)
    return True


def test_session_name_never_has_a_tmux_separator():
    assert cm.tmux_session_name('tgog-ops', 3) == 'tgog-ops-g3'
    assert cm.tmux_session_name('ops.a:1', 0) == 'ops_a_1-g0'


def test_supervisor_slot_runs_in_the_fleets_tmux_and_shows_in_two_workspaces(world):
    w = world
    sid, _ = spawn(w)
    assert wait(lambda: len(clients(w, 'ops-a-g0')) == 1)
    # the CLI hangs under the tmux server, not under the tab; liveness follows the tmux pane
    assert wait(lambda: w.parent.has_cli_process(sid, ('claude',)))
    assert claude_pids(w)
    assert w.parent.process_states(sid)
    assert w.parent.process_cpu(sid) is not None  # §24 stall window: the tmux pane's process tree
    # the tab only runs the client, exec'd: nothing of the CLI is on its tty
    tab = next(t for t in w.fake.tabs() if t.alive())
    ps = subprocess.run(['ps', '-t', tab.tty, '-o', 'comm='], capture_output=True, text=True).stdout
    assert 'claude' not in ps and 'tmux' in ps and 'bash' not in ps
    # the fleet's own minimal config; the user's ~/.tmux.conf is never read; no default server started
    assert tmux(w, 'show-options', '-gv', 'history-limit').stdout.strip() == '50000'
    assert tmux(w, 'show-options', '-gv', 'window-size').stdout.strip() == 'latest'
    assert '@poisoned' not in tmux(w, 'show-options', '-g').stdout
    assert not list(w.tmux_tmp.rglob('default'))
    # delegation: the sub-fleet opens a second client in its own workspace, the parent tab stays
    peer = w.child.spawn_window('worker-1', 'sleep 600', metadata={'type': 'worker'}, start_directory=str(w.run))
    assert peer.success
    info = w.parent.tmux_view_info(sid)
    assert info['tmux_session'] == 'ops-a-g0' and info['tmux_socket'] == w.sock
    view = w.child.attach_view(info, peer.pane_id)
    assert view and view != sid and w.child.owns(view) and w.parent.owns(sid)
    assert wait(lambda: len(clients(w, 'ops-a-g0')) == 2)
    assert w.child.has_cli_process(view, ('claude',))
    # return: the child's client is detached, its tab ends by itself; the session and parent tab live on
    assert w.child.detach_view(view)
    assert wait(lambda: len(clients(w, 'ops-a-g0')) == 1)
    assert not w.child.owns(view) and view not in w.child._meta()['surfaces']
    assert w.parent.has_cli_process(sid, ('claude',)) and 'ops-a-g0' in cm.tmux_sessions(w.sock)
    assert not any('--force' in c for c in w.fake.calls)  # never a forced close (dialogs are the user's)


def test_closing_the_childs_tabs_never_ends_the_session(world):
    w = world
    sid, _ = spawn(w)
    peer = w.child.spawn_window('worker-1', 'sleep 600', metadata={'type': 'worker'}, start_directory=str(w.run))
    view = w.child.attach_view(w.parent.tmux_view_info(sid), peer.pane_id)
    assert view and wait(lambda: len(clients(w, 'ops-a-g0')) == 2)
    assert w.child.close_all()  # a sub-fleet closing its workspace (or a crashed child's tabs)
    assert wait(lambda: len(clients(w, 'ops-a-g0')) == 1)
    assert 'ops-a-g0' in cm.tmux_sessions(w.sock) and w.parent.has_cli_process(sid, ('claude',))


def test_retirement_kills_the_session_without_orphans_and_keeps_scrollback(world):
    w = world
    sid, out = spawn(w, lines=3000)
    assert wait(lambda: w.parent.has_cli_process(sid, ('claude',)))
    assert wait(lambda: '3000' in w.parent.capture_output(sid))
    snapshot = w.run / 'handoffs' / 'ops-a-g0.terminal.txt'
    w.parent.capture_to(sid, str(snapshot))  # history page: the whole scrollback, not the current screen
    lines = snapshot.read_text().split()
    assert '1' in lines and '1500' in lines and '3000' in lines
    assert w.parent.kill_pane(sid) and w.parent.get_pane_status(sid) is None
    assert 'ops-a-g0' not in cm.tmux_sessions(w.sock)
    assert wait(lambda: not claude_pids(w))  # no orphan CLI left in the tmux server
    assert '1500' in out.read_text().split()
    assert not any(t.alive() for t in w.fake.tabs())
    assert cm.tmux_kill_server(w.sock)


def test_handoff_keeps_both_generations_until_the_old_one_retires(world):
    w = world
    old, _ = spawn(w, generation=0)
    new, _ = spawn(w, generation=1)  # the predecessor still has its tab: never reaped by the spawn
    assert set(cm.tmux_sessions(w.sock)) == {'ops-a-g0', 'ops-a-g1'}
    assert w.parent.tab_into(new, old)
    assert w.parent.kill_pane(old)
    # the successor's CLI may still be starting: its session exists as soon as spawn returns, the process a moment later
    assert cm.tmux_sessions(w.sock) == ['ops-a-g1'] and wait(lambda: w.parent.has_cli_process(new, ('claude',)))
    assert len(claude_pids(w)) == 1


def test_resume_without_the_tab_starts_the_next_generation_and_reaps_the_old_session(world):
    w = world
    old, out = spawn(w, generation=1, lines=50)
    assert wait(lambda: '50' in w.parent.capture_output(old).split())
    for tab in w.fake.tabs():
        tab.kill()  # the tab was closed (cmux restarted); the session is still in the tmux server
    assert wait(lambda: w.parent.get_pane_status(old) is None)
    assert cm.tmux_sessions(w.sock) == ['ops-a-g1']
    new, _ = spawn(w, generation=2)
    assert cm.tmux_sessions(w.sock) == ['ops-a-g2']
    assert wait(lambda: len(claude_pids(w)) == 1)
    assert '50' in out.read_text().split()  # the reaped generation's history was kept
    assert w.parent.kill_pane(old)  # forgetting the closed tab never touches the live generation
    assert cm.tmux_sessions(w.sock) == ['ops-a-g2']


def test_fleet_exit_ends_its_tmux_server_and_leaves_other_servers_alone(world):
    w = world
    other = str(w.run / 'other.sock')
    subprocess.run([TMUX, '-S', other, '-f', '/dev/null', 'new-session', '-d', '-s', 'user', 'sleep 600'], check=True)
    try:
        spawn(w)
        from fleetmux import subfleet
        subfleet.stop_tmux({'run': str(w.run)})
        assert cm.tmux_sessions(w.sock) == []
        assert wait(lambda: not claude_pids(w))
        assert subprocess.run([TMUX, '-S', other, 'has-session', '-t', '=user']).returncode == 0
    finally:
        subprocess.run([TMUX, '-S', other, 'kill-server'], capture_output=True)


def test_failed_tab_leaves_no_session(world, monkeypatch):
    w = world
    monkeypatch.setattr(w.parent, '_new_surface', lambda control=False, command=None: (None, False))
    out = w.run / 'x.log'
    res = w.parent.spawn_interactive_cli(window_name='fx-ops-a', cli_command=w.claude + ' ' + _fake_claude.args(600),
                                         output_file=str(out), start_directory=str(w.run), tmux_socket=w.sock,
                                         tmux_session='ops-a-g0')
    assert not res.success and cm.tmux_sessions(w.sock) == []


def test_session_probe_proves_a_stop_only_from_tmux_itself(world, monkeypatch):
    """Liveness evidence (runtime_v2.tab_missing): False only when tmux shows the session gone."""
    w = world
    sid, _ = spawn(w)
    plain = w.parent.spawn_window('worker-2', 'sleep 600', metadata={'type': 'worker'}, start_directory=str(w.run))
    assert w.parent.tmux_session_alive(sid) is True and w.parent.tmux_session_alive(plain.pane_id) is None
    other, _ = spawn(w, slot='ops-b')
    tmux(w, 'kill-session', '-t', '=ops-a-g0')  # the session ended; the server still answers
    assert w.parent.tmux_session_alive(sid) is False and w.parent.tmux_session_alive(other) is True
    tmux(w, 'kill-server')  # no server at all; its socket file is left behind
    assert Path(w.sock).exists() and w.parent.tmux_session_alive(other) is False
    monkeypatch.setattr(cm, 'TMUX', str(w.run / 'no-such-tmux'))  # tmux unreadable: no evidence either way
    assert w.parent.tmux_session_alive(other) is None


def test_session_probe_rides_out_a_server_that_is_shutting_down(world, monkeypatch, tmp_path):
    """A tmux server exits by itself when its last session ends; a probe landing mid-shutdown reads
    'server exited unexpectedly' (VM: 22 of 300 under load). That is no answer yet: ask again, and the
    next reply ('no server running') is the proof. Read as None it held the slot (CI 10-28, run 40612068578)."""
    w = world
    sid, _ = spawn(w)
    real = cm._tmux
    calls = []

    ev = {}  # M60 evidence: the settle, then what each call under test got (rewritten after every call)

    def keep(entry):
        ev.setdefault('calls_under_test', []).append(entry)
        (tmp_path / 'probe-evidence.json').write_text(json.dumps(ev, indent=1, default=str) + '\n')

    def dying_server(sock, *args):
        calls.append(args)
        if len(calls) == 1:
            keep({'args': args, 'injected': True, 't': time.monotonic()})
            return subprocess.CompletedProcess(args, 1, '', 'server exited unexpectedly\n')
        p = real(sock, *args)
        keep({'args': args, 'rc': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr, 't': time.monotonic()})
        return p

    pid = tmux(w, 'display-message', '-p', '#{pid}').stdout.strip()
    tmux(w, 'kill-server')
    # M60: the real server must be gone first (product probe, bounded), so the next real answer is 'no server running'
    ev.update(settle_server_gone(w, tmp_path / 'probe-evidence.json', int(pid) if pid.isdigit() else None))
    monkeypatch.setattr(cm, '_tmux', dying_server)
    monkeypatch.setattr(cm.time, 'sleep', lambda s: None)
    assert w.parent.tmux_session_alive(sid) is False and len(calls) == 2

    calls.clear()
    monkeypatch.setattr(cm, '_tmux', lambda sock, *a: calls.append(a) or subprocess.CompletedProcess(
        a, 1, '', 'server exited unexpectedly\n'))  # never settles: still no evidence, and bounded
    assert w.parent.tmux_session_alive(sid) is None and len(calls) == cm.TMUX_SHUTDOWN_RETRIES + 1


def test_closed_client_tab_is_reattached_with_its_registry_entry(world):
    w = world
    sid, out = spawn(w)
    peer = w.parent.spawn_window('worker-2', 'sleep 600', metadata={'type': 'worker'}, start_directory=str(w.run))
    assert wait(lambda: len(clients(w, 'ops-a-g0')) == 1)
    assert w.parent.reattach_client(sid, peer.pane_id) is None  # its tab is still there
    next(t for s, t in w.fake.ws[w.parent._where(sid)].items() if s == sid).kill()  # the ✕ on the tab
    assert wait(lambda: w.parent.get_pane_status(sid) is None)
    assert wait(lambda: not clients(w, 'ops-a-g0')) and 'ops-a-g0' in cm.tmux_sessions(w.sock)
    new = w.parent.reattach_client(sid, peer.pane_id)
    assert new and w.parent.owns(new) and wait(lambda: len(clients(w, 'ops-a-g0')) == 1)
    entry = w.parent._meta()['surfaces'][new]
    assert entry['tmux_role'] == 'owner' and entry['output_file'] == str(out) and entry['label'] == 'fx-ops-a'
    assert sid not in w.parent._meta()['surfaces'] and w.parent.has_cli_process(new, ('claude',))
    assert w.parent.kill_pane(new) and 'ops-a-g0' not in cm.tmux_sessions(w.sock)  # still the owner
    assert w.parent.reattach_client(new, peer.pane_id) is None  # the session is gone: nothing to reattach


# ---- M60: no-process self-tests of the world root and of settle_server_gone (fake probes, no tmux started) ----

def _fake_world(tmp_path):
    root = tmp_path / 'r'
    (root / 'run').mkdir(parents=True)
    sock = root / 'run' / 'tmux.sock'
    sock.write_text('')  # a stand-in file: these tests never connect
    return SimpleNamespace(root=root, sock=str(sock))


def _probes(monkeypatch, answers):
    """cm._tmux_probe_once answers from ``answers`` (the last one repeats); no subprocess is run."""
    seq = list(answers)
    monkeypatch.setattr(cm, '_tmux_probe_once', lambda sock: seq.pop(0) if len(seq) > 1 else seq[0])
    monkeypatch.setattr(cm.subprocess, 'run', lambda *a, **k: pytest.fail('a self-test started a process'))


def test_m60_settle_returns_once_the_product_probe_proves_gone(tmp_path, monkeypatch):
    w = _fake_world(tmp_path)
    _probes(monkeypatch, [None, None, True])
    ev = settle_server_gone(w, tmp_path / 'ev.json')
    assert ev['gone'] is True and [p['result'] for p in ev['probes']] == [None, None, True]
    assert json.loads((tmp_path / 'ev.json').read_text())['verdict'] == 'gone'


@pytest.mark.parametrize('answer', [None, False])
def test_m60_settle_fails_at_once_when_not_proven_gone(tmp_path, monkeypatch, answer):
    w = _fake_world(tmp_path)
    _probes(monkeypatch, [answer])
    t0 = time.monotonic()
    with pytest.raises(pytest.fail.Exception, match='not True'):
        settle_server_gone(w, tmp_path / 'ev.json')
    assert time.monotonic() - t0 < cm.TMUX_GONE_SETTLE + 1  # bounded by the product's own settle
    ev = json.loads((tmp_path / 'ev.json').read_text())
    assert ev['verdict'] == 'not proven gone' and ev['gone'] is answer and len(ev['probes']) >= 2


def test_m60_settle_refuses_a_socket_outside_its_world(tmp_path, monkeypatch):
    w = _fake_world(tmp_path)
    w.sock = str(tmp_path / 'elsewhere.sock')
    _probes(monkeypatch, [True])
    with pytest.raises(pytest.fail.Exception, match='not inside this world root'):
        settle_server_gone(w, tmp_path / 'ev.json')
    assert json.loads((tmp_path / 'ev.json').read_text())['probes'] == []  # nothing was asked


def test_m60_settle_fails_when_the_socket_file_is_gone(tmp_path, monkeypatch):
    w = _fake_world(tmp_path)
    Path(w.sock).unlink()
    _probes(monkeypatch, [True])
    with pytest.raises(pytest.fail.Exception, match='is gone'):
        settle_server_gone(w, tmp_path / 'ev.json')


def test_m60_world_root_outside_m4_is_the_old_tmp_path(tmp_path):
    root, kept = world_root({'FLEETMUX_GATE': 'm3'}, 'cft-12345678')
    assert (root, kept) == (Path('/tmp/cft-12345678'), False) and not root.exists()


def test_m60_world_root_in_m4_is_new_kept_and_never_reused():
    base = Path(tempfile.mkdtemp(prefix='m60-'))  # short (TMPDIR): tmp_path would push the socket past 103 bytes; kept
    (base / 'r.x').mkdir()
    env = {'FLEETMUX_GATE': 'm4', 'M4E_ROOT': str(base), 'FM4_TEST_ROOT': str(base / 'r.x')}
    root, kept = world_root(env, 'cft-abcdef01')
    assert kept and root == base / 'r.x' / 'cft-abcdef01' and root.is_dir()
    with pytest.raises(pytest.fail.Exception, match='never reused'):
        world_root(env, 'cft-abcdef01')
    assert root.is_dir()  # the collision kept what was there


@pytest.mark.parametrize('env, why', [
    ({'FLEETMUX_GATE': 'm4'}, 'needs FM4_TEST_ROOT'),
    ({'FLEETMUX_GATE': 'm4', 'M4E_ROOT': 'rel', 'FM4_TEST_ROOT': 'rel/r'}, 'absolute'),
    ({'FLEETMUX_GATE': 'm4', 'M4E_ROOT': '/nonexistent-m4e', 'FM4_TEST_ROOT': '/'}, 'not inside M4E_ROOT'),
])
def test_m60_world_root_in_m4_refuses_bad_roots(env, why):
    with pytest.raises(pytest.fail.Exception, match=why):
        world_root(env, 'cft-00000000')


# ---- M60: no-process self-tests of the m4 world teardown (a fake process table; nothing is started or signalled) ----

class _Procs:
    """A fake process table: ``run`` answers ps / pgrep / tmux from it and records every argv; ``kill`` records."""

    def __init__(self, table, children=None, server=None, panes=(), on_kill_server=()):
        self.table, self.children, self.server, self.panes = dict(table), children or {}, server, list(panes)
        self.on_kill_server, self.argv, self.killed = list(on_kill_server), [], []

    def run(self, cmd, *a, **kw):
        self.argv.append(list(cmd))
        out, rc = '', 0
        if cmd[0] == 'ps' and '-p' in cmd:
            ident = self.table.get(int(cmd[cmd.index('-p') + 1]))
            out, rc = ('S    %s %s\n' % ident, 0) if ident else ('', 1)
        elif cmd[0] == 'ps':
            out = ''.join('%d %s\n' % (pid, ident[1]) for pid, ident in self.table.items())
        elif cmd[0] == 'pgrep':
            out = ''.join('%d\n' % c for c in self.children.get(int(cmd[2]), []))
        elif cmd[-1] == 'kill-server':
            for pid, ident in self.on_kill_server:
                if ident is None:
                    self.table.pop(pid, None)
                else:
                    self.table[pid] = ident  # the pid now belongs to someone else
        elif 'display-message' in cmd:
            out = '%d\n' % self.server if self.server else ''
        elif 'list-panes' in cmd:
            out = ''.join('%d\n' % p for p in self.panes)
        return subprocess.CompletedProcess(cmd, rc, out, '')

    def kill(self, pid, sig):
        self.killed.append((pid, sig))
        self.table.pop(pid, None)


def _td_world(tmp_path):
    (tmp_path / 'run').mkdir()
    sock = tmp_path / 'run' / 'tmux.sock'
    sock.write_text('')  # a stand-in file: nothing connects
    return SimpleNamespace(sock=str(sock), claude=str(tmp_path / 'bin' / 'claude'))


def _clock():
    t = [0.0]
    return (lambda: t[0]), (lambda s: t.__setitem__(0, t[0] + s))


LS = 'Thu Nov  5 05:00:00 2026'


def _run_td(tmp_path, procs, tabs=()):
    w = _td_world(tmp_path)
    now, sleep = _clock()
    fake = SimpleNamespace(tabs=lambda: list(tabs))
    try:
        return _m4_world_teardown(w, fake, tmp_path / 'ledger.json', run=procs.run, kill=procs.kill,
                                  sleep=sleep, now=now, settle=1.0), None
    except pytest.fail.Exception as e:
        return None, e
    finally:
        assert not any(c and c[0] == 'pkill' for c in procs.argv), 'the m4 teardown ran pkill'


def test_m60_m4_teardown_normal_end_signals_nothing_and_never_pkills(tmp_path):
    w_claude = str(tmp_path / 'bin' / 'claude')
    procs = _Procs({10: (LS, 'tmux -S x new-session'), 11: (LS, 'sh -c x'), 12: (LS, w_claude + ' 600')},
                   children={11: [12]}, server=10, panes=[11],
                   on_kill_server=[(10, None), (11, None), (12, None)])
    ev, err = _run_td(tmp_path, procs)
    assert err is None and procs.killed == [] and {e['pid'] for e in ev['registered']} == {10, 11, 12}


def test_m60_m4_teardown_falls_back_only_on_a_registered_matching_pid_and_fails(tmp_path):
    w_claude = str(tmp_path / 'bin' / 'claude')
    procs = _Procs({10: (LS, 'tmux server'), 11: (LS, 'sh -c x'), 12: (LS, w_claude + ' 600'),
                    99: (LS, w_claude + ' 600 (not registered)')},
                   children={11: [12]}, server=10, panes=[11], on_kill_server=[(10, None), (11, None)])
    ev, err = _run_td(tmp_path, procs)
    assert err is not None and procs.killed == [(12, signal.SIGKILL)]  # 99 is reported, never signalled
    led = json.loads((tmp_path / 'ledger.json').read_text())
    assert [e['pid'] for e in led['signalled']] == [12] and any(r.startswith('99 ') for r in led['strays'])


def test_m60_m4_teardown_never_signals_a_reused_pid(tmp_path):
    procs = _Procs({10: (LS, 'tmux server'), 11: (LS, 'sh -c x')}, server=10, panes=[11],
                   on_kill_server=[(10, None), (11, ('Thu Nov  5 05:00:09 2026', 'someone else'))])
    ev, err = _run_td(tmp_path, procs)
    assert err is None and procs.killed == [] and ev['signalled'] == []


def test_m60_m4_teardown_fails_without_signal_when_identity_is_unreadable(tmp_path):
    procs = _Procs({10: (LS, 'tmux server')}, server=10, on_kill_server=[(10, None)])
    real_run = procs.run
    state = {'killed_server': False}

    def run(cmd, *a, **kw):
        if cmd[-1] == 'kill-server':
            state['killed_server'] = True
        if state['killed_server'] and cmd[0] == 'ps' and '-p' in cmd:
            raise OSError('ps unavailable')
        return real_run(cmd, *a, **kw)

    procs.run = run
    ev, err = _run_td(tmp_path, procs)
    assert err is not None and 'unknown' in str(err) and procs.killed == []
