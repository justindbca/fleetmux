"""brief-m4 §7.1: the fleet's own tmux socket is removed at stop once no server is on it, on both backends.

tmux never unlinks its socket: ``kill-server`` and a server that exits by itself (last session gone) both leave
``<run>/tmux.sock`` behind with nobody listening. ``subfleet.stop_tmux`` is the one stop path both backends share; it
must remove that file, and only under every one of these conditions: the path is the run's recorded socket
(``subfleet.tmux_socket(cfg)``, directly in ``cfg['run']``), an ``lstat`` (no symlink followed) says it is a socket,
the probe confirms there is no server ("no server running" / ECONNREFUSED / ENOENT), and the entry is still the same
file (device + inode) right before the unlink. A live server (with or without sessions), an uncertain probe (timeout,
any other answer), an unrecorded or out-of-run path, a non-socket or a symlink: the file stays.

World: ``tests/_fm4_world.py`` rules (kept root ``~/.m4e/fm4-py/tc-*``, pid ledger, no ``tmp_path`` for sockets, no
``rmtree``, no ``pkill``). ``cmux``: ``tests/test_tmux_view.py``'s ``FakeCmux`` (each tab a real pty running the real
``tmux attach``); ``fleetmux``: this worktree's real binary. The tmux server and the fake CLI are real. Files only the
test made (dead sockets bound by the test, a symlink, a regular file) are left in the root on purpose: nothing here
deletes; the only removals are the product's own.
"""
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _fake_claude  # noqa: E402
import _fm4_world as W  # noqa: E402
from _fleetmux_milestone import milestone  # noqa: E402
from cmux_fleet import cmux_manager as cm  # noqa: E402
from cmux_fleet import subfleet  # noqa: E402
from test_tmux_view import FakeCmux  # noqa: E402

pytestmark = [milestone(4), pytest.mark.skipif(not Path(cm.TMUX).exists(), reason='tmux unavailable')]


@pytest.fixture(params=['cmux', 'fleetmux'])
def world(request, monkeypatch):
    kind = request.param
    root = W.new_root('tc', request, kind)
    home = root / 'h'
    home.mkdir()
    (home / '.tmux.conf').write_text('set -g history-limit 7\nset -g @poisoned 1\n')  # must never be read
    tmux_tmp = root / 'tmux-default'
    tmux_tmp.mkdir()
    cli = root / 'cli'
    cli.mkdir()
    claude = _fake_claude.make(cli)
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('TMUX_TMPDIR', str(tmux_tmp))
    monkeypatch.setenv('SHELL', '/bin/sh')
    run = root / 'run'
    (run / 'prompts').mkdir(parents=True)
    state = root / 'state'
    state.mkdir()
    w = SimpleNamespace(kind=kind, root=root, run=run, sock=str(run / 'tmux.sock'), cfg={'run': str(run)},
                        claude=str(claude), fake=None, servers=None)
    if kind == 'cmux':
        w.fake = FakeCmux()
        monkeypatch.setattr(cm, '_run', w.fake)
        w.ledger = W.Ledger(root)
        w.parent = cm.CmuxSessionManager('cmux-fleet', state_dir=state)
    else:
        W.install_fleetmux(monkeypatch, root, 'real')
        w.servers = W.Servers(root, 'real', state)
        w.ledger = w.servers.ledger
        w.parent = w.servers.root_mgr()
    W.note(root, 'tmux socket: %s' % w.sock)
    yield w
    if kind == 'fleetmux':
        w.servers.close(extra=[lambda: subfleet.stop_tmux(w.cfg)])
        return
    log = []
    for sid in list(w.parent._meta().get('surfaces', {})):
        log.append(['kill_pane', sid, str(w.parent.kill_pane(sid))])
    log.append(['stop_tmux', str(subfleet.stop_tmux(w.cfg))])
    W.wait_for(lambda: not any(t.alive() for t in w.fake.tabs()), timeout=5)  # alive() polls: reaps the tab
    W.wait_for(lambda: not any(w.ledger.alive(r['role']) for r in w.ledger.rows), timeout=5)
    W.fail_if_signalled(root, w.ledger.fallback(), log)


def tmux(w, *args):
    return subprocess.run([cm.TMUX, '-S', w.sock, *args], capture_output=True, text=True, timeout=10)


def server_pid(w):
    pid = tmux(w, 'display-message', '-p', '#{pid}').stdout.strip()
    assert pid.isdigit(), 'no tmux server on ' + w.sock
    return int(pid)


def cli_pid(w, session):
    """The fake CLI under ``session``'s tmux pane (argv[0] is our ``claude``), found from the pane pid down."""
    done = tmux(w, 'list-panes', '-s', '-t', '=' + session, '-F', '#{pane_pid}')
    tree = {int(p) for p in done.stdout.split() if p.isdigit()}
    table = subprocess.run(['ps', '-axww', '-o', 'pid=,ppid=,args='], capture_output=True, text=True).stdout
    rows = [r.split(None, 2) for r in table.splitlines()]
    rows = [(int(r[0]), int(r[1]), r[2]) for r in rows if len(r) == 3 and r[0].isdigit() and r[1].isdigit()]
    grew = True
    while grew:
        grew = False
        for pid, ppid, _ in rows:
            if ppid in tree and pid not in tree:
                tree.add(pid)
                grew = True
    return next((pid for pid, _, args in rows if pid in tree and args.split()[:1] == [w.claude]), None)


def spawn(w, slot='ops-a', generation=0):
    """A tmux slot through the backend's own spawn path (it starts the run's tmux server); everything registered."""
    session = cm.tmux_session_name(slot, generation)
    out = w.run / 'prompts' / (session + '.terminal.log')
    res = w.parent.spawn_interactive_cli('fx-' + slot, w.claude + ' ' + _fake_claude.args(600), output_file=str(out),
                                         start_directory=str(w.run), auto_close=False, tmux_socket=w.sock,
                                         tmux_session=session)
    assert res.success, (res.error, getattr(w.parent, 'last_error', None))
    if w.kind == 'fleetmux':
        w.ledger.register('fleetmux-server:' + w.parent.session_name, w.parent._server_pid())
    else:
        for tab in w.fake.tabs():
            w.ledger.register('tab:' + tab.tty, tab.proc.pid)
    pid = server_pid(w)
    assert w.ledger.register('tmux-server:%d' % pid, pid)
    assert W.wait_for(lambda: cli_pid(w, session) is not None), 'no fake CLI under ' + session
    assert w.ledger.register('fake-cli:' + session, cli_pid(w, session))
    W.note(w.root, 'spawned %s on %s (tmux server %d)' % (session, w.sock, pid))
    return res.pane_id, session, pid


def server_alive(w, pid):
    return w.ledger.alive('tmux-server:%d' % pid)


def is_socket(path):
    try:
        return stat.S_ISSOCK(os.lstat(path).st_mode)
    except FileNotFoundError:
        return False


def dead_socket(path):
    """A socket file nobody listens on (what tmux leaves behind), made by the test itself."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(path))
    s.close()
    assert is_socket(path)
    return str(path)


def end_by_itself(w, sid, pid):
    """Retire the slot's only session: the tmux server exits on its own and leaves its socket behind."""
    assert w.parent.kill_pane(sid)
    assert W.wait_for(lambda: not server_alive(w, pid)), 'tmux server %d did not exit by itself' % pid
    assert is_socket(w.sock), 'tmux unlinked its own socket: the premise of brief-m4 §7.1 changed'


def outcome(w, label, value):
    W.note(w.root, '%s -> %r' % (label, value))
    return value


def test_normal_stop_ends_the_server_and_removes_its_socket(world):
    w = world
    _, session, pid = spawn(w)
    assert is_socket(w.sock)
    assert outcome(w, 'stop_tmux', subfleet.stop_tmux(w.cfg)) == 'removed'
    assert not os.path.lexists(w.sock)
    assert W.wait_for(lambda: not server_alive(w, pid))
    assert W.wait_for(lambda: not w.ledger.alive('fake-cli:' + session))
    assert W.need(cm, 'tmux_server_gone')(w.sock) is True  # ENOENT: nothing left on the path
    assert outcome(w, 'stop_tmux again', subfleet.stop_tmux(w.cfg)) == 'absent'


def test_stale_socket_after_the_server_exited_by_itself_is_removed(world):
    w = world
    sid, _, pid = spawn(w)
    end_by_itself(w, sid, pid)
    assert W.need(cm, 'tmux_server_gone')(w.sock) is True  # ECONNREFUSED / "no server running"
    assert outcome(w, 'stop_tmux', subfleet.stop_tmux(w.cfg)) == 'removed'
    assert not os.path.lexists(w.sock)


def test_a_live_server_keeps_its_socket(world, monkeypatch):
    w = world
    sid, session, pid = spawn(w)
    cleanup = W.need(subfleet, 'remove_dead_tmux_socket')
    assert W.need(cm, 'tmux_server_gone')(w.sock) is False
    got = outcome(w, 'cleanup, live server with a session', cleanup(w.cfg))
    assert got.startswith('kept') and 'alive' in got
    assert is_socket(w.sock) and server_alive(w, pid) and session in cm.tmux_sessions(w.sock)
    # alive with no session at all: list-sessions exits 0 with nothing listed -- still a server, never "gone"
    assert tmux(w, 'set-option', '-g', 'exit-empty', 'off').returncode == 0
    assert w.parent.kill_pane(sid)
    assert W.wait_for(lambda: cm.tmux_sessions(w.sock) == []) and server_alive(w, pid)
    assert cm.tmux_server_gone(w.sock) is False
    got = outcome(w, 'cleanup, live server without sessions', cleanup(w.cfg))
    assert got.startswith('kept') and 'alive' in got and is_socket(w.sock)
    # kill-server did not take effect: the stop keeps the socket of the server that is still there
    with monkeypatch.context() as m:
        m.setattr(cm, 'tmux_kill_server', lambda sock: False)
        got = outcome(w, 'stop_tmux, kill-server without effect', subfleet.stop_tmux(w.cfg))
    assert got.startswith('kept') and 'alive' in got and is_socket(w.sock) and server_alive(w, pid)
    assert outcome(w, 'stop_tmux', subfleet.stop_tmux(w.cfg)) == 'removed'
    assert not os.path.lexists(w.sock) and W.wait_for(lambda: not server_alive(w, pid))


def test_an_uncertain_probe_keeps_the_socket(world, monkeypatch):
    w = world
    sid, _, pid = spawn(w)
    end_by_itself(w, sid, pid)  # really gone: only the probe's answer is uncertain
    stubs = w.root / 'stub'
    stubs.mkdir()
    odd = stubs / 'tmux-odd'
    odd.write_text('#!/bin/sh\necho "server exited unexpectedly" >&2\nexit 1\n')
    slow = stubs / 'tmux-slow'
    slow.write_text('#!/bin/sh\nexec sleep 30\n')  # killed by the probe's timeout (exec: no orphan shell)
    for f in (odd, slow):
        f.chmod(0o755)
    cases = [('other answer', odd, subfleet.stop_tmux), ('timeout', slow, W.need(subfleet, 'remove_dead_tmux_socket')),
             ('tmux not runnable', stubs / 'missing', W.need(subfleet, 'remove_dead_tmux_socket'))]
    for label, binary, step in cases:
        with monkeypatch.context() as m:
            m.setattr(cm, 'TMUX', str(binary))
            assert W.need(cm, 'tmux_server_gone')(w.sock) is None, label
            got = outcome(w, 'uncertain probe (%s)' % label, step(w.cfg))
        assert got.startswith('kept') and 'uncertain' in got, (label, got)
        assert is_socket(w.sock), label
    assert outcome(w, 'stop_tmux, probe certain again', subfleet.stop_tmux(w.cfg)) == 'removed'
    assert not os.path.lexists(w.sock)


def test_paths_not_recorded_for_this_run_are_never_removed(world):
    w = world
    _, _, pid = spawn(w)
    other = dead_socket(w.run / 'other.sock')  # in the run dir, but not the recorded socket
    outside = dead_socket(w.root / 'elsewhere' / 'tmux.sock')  # the recorded name, another dir
    link_run = w.root / 'run-link'
    link_run.mkdir()
    (link_run / 'tmux.sock').symlink_to(outside)  # lstat: a symlink, never followed
    plain_run = w.root / 'run-plain'
    plain_run.mkdir()
    (plain_run / 'tmux.sock').write_text('not a socket\n')
    target_run = w.root / 'run-target'
    target_sock = dead_socket(target_run / 'tmux.sock')
    alias_run = w.root / 'run-alias'
    alias_run.symlink_to(target_run, target_is_directory=True)  # the run dir itself is a symlink
    for label, run in (('symlinked socket', link_run), ('regular file', plain_run), ('symlinked run dir', alias_run)):
        got = outcome(w, 'stop_tmux (%s)' % label, subfleet.stop_tmux({'run': str(run)}))
        assert isinstance(got, str) and got.startswith('kept'), (label, got)
    assert os.path.islink(link_run / 'tmux.sock') and (plain_run / 'tmux.sock').read_text() == 'not a socket\n'
    assert outcome(w, 'stop_tmux (own run)', subfleet.stop_tmux(w.cfg)) == 'removed'
    assert not os.path.lexists(w.sock) and W.wait_for(lambda: not server_alive(w, pid))
    for path in (other, outside, target_sock):
        assert is_socket(path), path


class _SwappedStatView:
    """``subfleet``'s view of ``os`` (only that module's name is patched): once ``swapped`` is set, the run's
    ``tmux.sock`` entry reads as ``decoy``'s stat (another dead socket: same type and owner, other device/inode),
    so the product sees its socket replaced without anything being moved on disk. Every other attribute and every
    other path go to the real ``os``; a removal call is never forwarded while swapped, only recorded."""

    def __init__(self, real, sock, decoy):
        self._real, self._sock, self._decoy = real, str(sock), str(decoy)
        self.swapped, self.blocked = False, []

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if name in ('unlink', 'remove') and callable(attr):
            def guarded(*args, **kwargs):
                if self.swapped:
                    self.blocked.append((name, args))
                    raise AssertionError('removal attempted after the identity changed: %r' % (args,))
                return attr(*args, **kwargs)
            return guarded
        return attr

    def stat(self, path, *, dir_fd=None, follow_symlinks=True):
        if self.swapped and dir_fd is not None and path == os.path.basename(self._sock):
            return self._real.stat(self._decoy, follow_symlinks=False)
        return self._real.stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    def lstat(self, path, *, dir_fd=None):
        if self.swapped and dir_fd is None and str(path) == self._sock:
            return self._real.lstat(self._decoy)
        return self._real.lstat(path, dir_fd=dir_fd)


def test_a_socket_replaced_between_the_check_and_the_delete_is_kept(world, monkeypatch):
    w = world
    sid, _, pid = spawn(w)
    end_by_itself(w, sid, pid)
    decoy = dead_socket(w.root / 'decoy' / 'tmux.sock')  # made up front, its own path; nothing is moved
    before = os.lstat(w.sock)
    assert (os.lstat(decoy).st_dev, os.lstat(decoy).st_ino) != (before.st_dev, before.st_ino)
    view = _SwappedStatView(subfleet.os, w.sock, decoy)
    real_probe = W.need(cm, 'tmux_server_gone')
    probes = []

    def probe_then_swap(sock, *args, **kwargs):
        answer = real_probe(sock, *args, **kwargs)
        probes.append(answer)
        view.swapped = True  # after the first lstat and the probe, before the delete
        return answer

    with monkeypatch.context() as m:
        m.setattr(subfleet, 'os', view)
        m.setattr(cm, 'tmux_server_gone', probe_then_swap)
        got = outcome(w, 'stop_tmux, socket identity changed during the probe', subfleet.stop_tmux(w.cfg))
    assert probes == [True], 'the probe was never reached, or did not prove the server gone: %r' % probes
    assert got.startswith('kept') and 'changed' in got, got
    assert view.blocked == []  # the product never even tried to remove it
    after = os.lstat(w.sock)
    assert is_socket(w.sock) and (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino) and is_socket(decoy)
    # identity as before again: the run's normal stop removes its own dead socket, never the decoy
    assert outcome(w, 'stop_tmux, identity unchanged', subfleet.stop_tmux(w.cfg)) == 'removed'
    assert not os.path.lexists(w.sock) and is_socket(decoy)
