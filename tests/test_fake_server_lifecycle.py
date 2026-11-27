"""The fake fleetmux server never outlives the test that started it.

Mock / fault injection only: no fake server, shell, tmux or real binary is started, nothing is
deleted and no signal is sent. ``os.kill``, ``os.killpg``, ``shutil.rmtree`` and ``os._exit`` are replaced by
recorders for the whole of every case (also on the code before the fix, so a red run cannot reach the real calls),
and the ``ps``-based helpers of tests/test_fleetmux_backend.py answer from the case's own table.

- F1: ``_teardown`` registers each server (pid + OS start time + runtime) *before* it asks the server to stop, so a
  server that removed its ``server.json`` while exiting and then hung is still stopped, by its recorded identity.
- F2: every server a test started, a restarted one included, is in ``env.servers`` from the moment it came up.
- F3: ``finish()`` reaches the exit call even when the log write or an unlink fails.
"""
import os
import shutil
import signal
from pathlib import Path
from types import SimpleNamespace

import pytest

import fake_fleetmux as fake
import test_fleetmux_backend as fbt


class Exited(Exception):
    """Raised by the stand-in for os._exit: the case sees the call, the pytest process keeps running."""


class World:
    """One test root on disk (directories only, created here, never deleted) and the recorded side effects."""

    def __init__(self, tmp_path, monkeypatch, servers):
        self.root = tmp_path / 'root'
        self.state = self.root / 'state'
        self.events = []
        self.alive = set()                       # pids the stand-in `ps` reports as running
        self.ours = set()                        # (pid, start_time) the stand-in identity check accepts
        self.files = {}                          # path -> json the stand-in _read_json returns
        for runtime, info in servers.items():
            rt = self.state / 'fleetmux' / runtime
            rt.mkdir(parents=True)
            self.files[rt / 'server.json'] = dict(info)
        self.env = SimpleNamespace(kind='fake', root=self.root, state=self.state, servers=[])

        def send(sock, request, timeout=10):
            self.events.append(('send', Path(sock).parent.name, request.get('cmd')))
            if request.get('cmd') == 'server.shutdown':  # the exiting server removes server.json (finish)
                self.files.pop(Path(sock).parent / 'server.json', None)
            return {'id': request.get('id'), 'ok': True}

        def kill(pid, sig):
            self.events.append(('kill', pid, sig))
            if sig == signal.SIGKILL and not self.stubborn:
                self.alive.discard(pid)

        self.stubborn = False
        monkeypatch.setattr(fbt, '_send', send)
        monkeypatch.setattr(fbt, '_read_json', lambda path: dict(self.files.get(Path(path), {})))
        monkeypatch.setattr(fbt, 'alive', lambda pid: int(pid) in self.alive)
        monkeypatch.setattr(fbt, 'ps_args', lambda pid: ('--runtime ' + ' '.join(
            str(rt) for rt in self.state.glob('fleetmux/*'))) if int(pid) in self.alive else '')
        monkeypatch.setattr(fbt, '_server_is_ours',
                            lambda srv: srv['pid'] in self.alive and (srv['pid'], srv['start_time']) in self.ours,
                            raising=False)
        monkeypatch.setattr(fbt, 'TEARDOWN_WAIT_S', 0.3, raising=False)
        monkeypatch.setattr(os, 'kill', kill)
        monkeypatch.setattr(os, 'killpg', lambda pgid, sig: self.events.append(('killpg', pgid, sig)))
        monkeypatch.setattr(shutil, 'rmtree', lambda path, ignore_errors=False: self.events.append(
            ('rmtree', Path(path))))

    def server(self, pid, start_time):
        self.alive.add(pid)
        self.ours.add((pid, start_time))

    def kinds(self):
        return [e[0] for e in self.events]


# ---------------------------------------------------------------- F1: register before the stop request

def test_teardown_stops_a_server_whose_server_json_vanished_during_shutdown(tmp_path, monkeypatch):
    """The suspected leak (plan L1): server.shutdown is answered, the server removes server.json and then hangs.
    The teardown registered pid + start time before the request, so it still stops exactly that process, and
    only then removes the root."""
    w = World(tmp_path, monkeypatch, {'fm-test': {'pid': 4242, 'start_time': 1000}})
    w.server(4242, 1000)                         # it keeps running after the shutdown reply
    fbt._teardown(w.env)
    assert w.events[0] == ('send', 'fm-test', 'server.shutdown'), w.events
    assert ('kill', 4242, signal.SIGKILL) in w.events, w.events
    assert w.kinds().index('kill') < w.kinds().index('rmtree') and w.events[-1] == ('rmtree', w.root), w.events
    assert [(s['pid'], s['start_time']) for s in w.env.servers] == [(4242, 1000)]


def test_teardown_records_the_identity_before_sending_the_stop_request(tmp_path, monkeypatch):
    """Order: the identity is taken from server.json while it still exists, i.e. before the first request."""
    w = World(tmp_path, monkeypatch, {'fm-test': {'pid': 4242, 'start_time': 1000}})
    seen_at_send = []
    real_send = fbt._send

    def send(sock, request, timeout=10):
        seen_at_send.append([(s['pid'], s['start_time'], s['runtime']) for s in w.env.servers])
        return real_send(sock, request, timeout)
    monkeypatch.setattr(fbt, '_send', send)
    fbt._teardown(w.env)
    assert seen_at_send and seen_at_send[0] == [(4242, 1000, w.state / 'fleetmux' / 'fm-test')], seen_at_send


def test_teardown_never_signals_a_pid_that_is_no_longer_its_server(tmp_path, monkeypatch):
    """The recorded pid is alive but its start time differs (reused by another process): no signal at all."""
    w = World(tmp_path, monkeypatch, {'fm-test': {'pid': 4242, 'start_time': 1000}})
    w.alive.add(4242)                            # alive, but (4242, 1000) is not in w.ours
    fbt._teardown(w.env)
    assert 'kill' not in w.kinds() and w.events[-1] == ('rmtree', w.root), w.events


def test_teardown_keeps_the_root_and_fails_when_its_server_survives(tmp_path, monkeypatch):
    """A recorded server that is still ours after SIGKILL is not hidden: the teardown fails naming it, and the
    root stays (nothing is removed under a live process)."""
    w = World(tmp_path, monkeypatch, {'fm-test': {'pid': 4242, 'start_time': 1000}})
    w.server(4242, 1000)
    w.stubborn = True
    with pytest.raises(pytest.fail.Exception, match='4242'):
        fbt._teardown(w.env)
    assert ('kill', 4242, signal.SIGKILL) in w.events and 'rmtree' not in w.kinds(), w.events


# ---------------------------------------------------------------- F2: every server the test started

def test_a_restarted_server_and_the_first_one_are_both_recorded_and_stopped(tmp_path, monkeypatch):
    """The fixture's spawn wrapper registers each server as it comes up; after a restart server.json names only
    the second, yet the teardown still handles the first one (here hung, so it gets SIGKILL)."""
    w = World(tmp_path, monkeypatch, {'fm-test': {'pid': 101, 'start_time': 1}})
    rt = w.state / 'fleetmux' / 'fm-test'
    mgr = SimpleNamespace(runtime_root=rt, server_json=rt / 'server.json')
    spawn = fbt._recording_spawn(lambda m: True, w.env.servers)
    assert spawn(mgr) is True
    w.files[rt / 'server.json'] = {'pid': 202, 'start_time': 2}   # the test killed 101 and started again
    assert spawn(mgr) is True
    assert [(s['pid'], s['start_time']) for s in w.env.servers] == [(101, 1), (202, 2)]
    w.server(101, 1)                                              # the first one did not die after all
    w.server(202, 2)
    fbt._teardown(w.env)
    assert ('kill', 101, signal.SIGKILL) in w.events, w.events
    assert w.events[-1] == ('rmtree', w.root), w.events


def test_a_spawn_that_raises_still_records_a_server_that_came_up(tmp_path, monkeypatch):
    """Failure path: if the start raises after the server wrote server.json, the server is recorded anyway."""
    w = World(tmp_path, monkeypatch, {'fm-test': {'pid': 303, 'start_time': 3}})
    rt = w.state / 'fleetmux' / 'fm-test'

    def failing(m):
        raise RuntimeError('start reported a failure')
    spawn = fbt._recording_spawn(failing, w.env.servers)
    with pytest.raises(RuntimeError):
        spawn(SimpleNamespace(runtime_root=rt, server_json=rt / 'server.json'))
    assert [(s['pid'], s['start_time']) for s in w.env.servers] == [(303, 3)]


# ---------------------------------------------------------------- F3: finish() always reaches the exit call

@pytest.fixture
def exit_calls(monkeypatch):
    calls = []

    def stand_in(code):
        calls.append(code)
        raise Exited(code)
    monkeypatch.setattr(os, '_exit', stand_in)
    assert fake.os._exit is stand_in             # never the real one in this process
    return calls


def server_at(runtime):
    srv = object.__new__(fake.Server)            # no listener, no threads: only what finish() touches
    srv.views, srv.runtime = [], runtime
    return srv


def test_finish_reaches_exit_when_the_log_write_fails(tmp_path, monkeypatch, exit_calls):
    def failing_log(path, level, msg):
        raise PermissionError('server.log not writable')
    monkeypatch.setattr(fake, 'log_line', failing_log)
    with pytest.raises(Exited):
        server_at(tmp_path).finish()
    assert exit_calls == [0]


def test_finish_reaches_exit_when_the_runtime_dir_is_gone(tmp_path, exit_calls):
    """The runtime directory does not exist (never created here): every unlink and the log open fail."""
    with pytest.raises(Exited):
        server_at(tmp_path / 'not-there').finish()
    assert exit_calls == [0]


def test_the_sigterm_path_reaches_exit_when_the_log_write_fails(tmp_path, monkeypatch, exit_calls):
    def failing_log(path, level, msg):
        raise OSError('disk full')
    monkeypatch.setattr(fake, 'log_line', failing_log)
    with pytest.raises(Exited):
        server_at(tmp_path).finish(live=True)
    assert exit_calls == [0]


def test_finish_reaches_exit_when_an_unlink_fails(tmp_path, monkeypatch, exit_calls):
    def failing_unlink(path):
        raise PermissionError('read-only runtime')
    monkeypatch.setattr(fake, 'unlink', failing_unlink)
    monkeypatch.setattr(fake, 'log_line', lambda *a: None)
    with pytest.raises(Exited):
        server_at(tmp_path).finish()
    assert exit_calls == [0]
