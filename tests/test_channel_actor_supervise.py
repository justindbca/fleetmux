"""P1f: the daemon supervises the fleet's one channel actor process (brief v2 §6.7; docs/design.md §53,
contracts §8 进程模型; supervisor rulings C13 one actor per fleet, M10 bounded stop drain).

Red until package P1f lands (phase B): ``channels/supervise.py`` is an A2 stub whose methods raise
NotImplementedError, and ``Fleet.channel_tick`` / ``Fleet.channel_stop_all`` do not exist yet (no stub in
fleet.py: the tests fetch them inside the test body, so they fail, never error at collection). The two rows
marked [基线] are green now: they pin behaviour that must not change.

Fakes only: the repo's private test Redis (unix socket, no TCP), FakeMgr, fake process handles with a fake clock.
One test starts a real child process (``sys.executable -c``) that only talks to that private Redis socket, and
signals only the handle it spawned itself.
"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture: the repo's private test Redis)
from test_runtime_v2 import audits, fx, receive, req  # noqa: F401  (fx: pi supervisor + pi worker, service, v2)
from test_fleet import FakeMgr
from test_channels_contract import Audit
from fleetmux import channels, fleet, fleetmux_sidebar, lifecycle_close, subfleet, tabs
from fleetmux.channels import envelope, supervise

OLD_PARTS = ['roster', 'advisor', 'lifecycle', 'result', 'contracts', 'subfleet', 'tabs', 'fleetmux',
             'escalations', 'waiting', 'notices', 'vetoes']  # Fleet.tick at 4cd1156 / 26392b9, in order
TWO_CHANNELS = [{'type': 'telegram', 'account': 'bot-1', 'push': list(channels.DEFAULT_PUSH)},
                {'type': 'exec', 'account': 'relay-1', 'push': list(channels.DEFAULT_PUSH)}]
ACCOUNT_Q = channels.OUTQ_ACCOUNT + 'telegram:bot-1'


class FakeProc:
    """A process handle (subprocess.Popen shape) driven by a fake clock; records every call with its time."""
    _next_pid = [70000]

    def __init__(self, argv=None, *, clock=None, code=None, exit_at=None, exit_code=0, on_exit=None,
                 dies_on_terminate=True, dies_on_kill=True, wait_timeouts=0):
        FakeProc._next_pid[0] += 1
        self.pid = FakeProc._next_pid[0]
        self.argv, self.clock, self.code = argv, clock, code
        self.exit_at, self.exit_code, self.on_exit = exit_at, exit_code, on_exit
        self.dies_on_terminate, self.dies_on_kill, self.wait_timeouts = dies_on_terminate, dies_on_kill, wait_timeouts
        self.calls = []

    def _now(self):
        return self.clock[0] if self.clock else 0.0

    def _maybe_exit(self):
        if self.code is None and self.exit_at is not None and self._now() >= self.exit_at:
            if self.on_exit:
                self.on_exit()  # what the real actor writes just before it exits
            self.code = self.exit_code

    def poll(self):
        self._maybe_exit()
        return self.code

    def terminate(self):
        self.calls.append(('terminate', self._now()))
        if self.dies_on_terminate:
            self.code = -signal.SIGTERM

    def kill(self):
        self.calls.append(('kill', self._now()))
        if self.dies_on_kill:
            self.code = -signal.SIGKILL

    def wait(self, timeout=None):
        self.calls.append(('wait', timeout))
        if self.wait_timeouts > 0 or self.code is None:
            self.wait_timeouts = max(0, self.wait_timeouts - 1)
            if self.clock is not None and timeout:
                self.clock[0] += timeout  # the wait used its whole budget
            raise subprocess.TimeoutExpired('channel actor', timeout)
        return self.code

    def names(self):
        return [c[0] for c in self.calls]


def channel_audits(f):
    return [x for _, x in f.r.xrange(f.v2.bus.audit_key) if str(x.get('event', '')).startswith('channel.')]


def config_path(f):
    return str(Path(f.cfg['run']) / 'config.json')


# ====================================================================== the tick table
def tick_parts(monkeypatch):
    """Run Fleet.tick's guarded segment table with every segment raising its own name; return the order."""
    f = object.__new__(fleet.Fleet)
    logged = []
    f.log = lambda kind, **data: logged.append((kind, data))

    def boom(name):
        def run(*args, **kwargs):
            raise RuntimeError('segment ' + name)
        return run
    for attr, name in (('roster_tick', 'roster'), ('advisor_tick', 'advisor'), ('write_result', 'result'),
                       ('sync_contract_files', 'contracts'), ('escalation_tick', 'escalations'),
                       ('waiting_tick', 'waiting'), ('relay_notices', 'notices'), ('apply_vetoes', 'vetoes'),
                       ('channel_tick', 'channels')):
        setattr(f, attr, boom(name))
    monkeypatch.setattr(lifecycle_close, 'tick', boom('lifecycle'))
    monkeypatch.setattr(subfleet, 'tick', boom('subfleet'))
    monkeypatch.setattr(tabs, 'open_all', boom('tabs'))
    monkeypatch.setattr(fleetmux_sidebar, 'tick', boom('fleetmux'))

    class Stop(BaseException):
        pass

    def stop(_f):
        raise Stop()
    monkeypatch.setattr(subfleet, 'follow_jump', stop)  # first call after the table: leave tick right there
    with pytest.raises(Stop):
        fleet.Fleet.tick(f)
    errors = [d for kind, d in logged if kind == 'tick.error']
    for d in errors:
        assert 'segment ' + d['part'] in d['error'], d  # each segment is guarded on its own
    return [d['part'] for d in errors]


def test_tick_table_existing_segments_unchanged(monkeypatch):  # [基线]
    parts = tick_parts(monkeypatch)
    assert parts[:12] == OLD_PARTS


def test_tick_table_appends_channels_segment(monkeypatch):
    parts = tick_parts(monkeypatch)
    assert parts[:12] == OLD_PARTS
    assert parts[12:13] == ['channels']  # right after the old twelve (P8m's security segment may follow it)
    assert parts.count('channels') == 1  # a failing channel segment is one tick.error, the daemon goes on


# ====================================================================== spawning
def test_no_channels_spawns_nothing(fx):  # [基线]
    f = fx
    f.cfg.pop('channels', None)
    calls = []
    f.channel_spawn = lambda argv: calls.append(argv)
    for _ in range(3):
        f.tick()
    assert calls == []
    assert f.r.keys('fx:channel:*') == []
    assert channel_audits(f) == []


def test_channel_tick_spawns_actor_with_config_argv(fx, monkeypatch):
    f = fx
    now = [1000.0]
    f.cfg['channels'] = [dict(c) for c in TWO_CHANNELS]
    spawned = []

    def spawn(argv):
        spawned.append(FakeProc(list(argv), clock=now))
        return spawned[-1]
    f.channel_spawn = spawn
    f.channel_clock = lambda: now[0]
    channel_tick = getattr(f, 'channel_tick')
    channel_tick()
    assert len(spawned) == 1  # one actor for the fleet, however many channels (C13)
    argv = spawned[0].argv
    assert argv[-3:] == ['channel', 'actor', config_path(f)]
    assert not {'telegram', 'exec', 'bot-1', 'relay-1'} & set(argv)  # no per-account arguments
    actor = f.r.hgetall(channels.ACTOR)
    assert actor['pid'] == str(spawned[0].pid) and float(actor['started']) == now[0]
    assert len(audits(f, 'channel.actor.started')) == 1
    now[0] += 5
    channel_tick()
    assert len(spawned) == 1  # still running: nothing new
    # hot reload: the roster's channels changed -> the running actor is stopped and started again
    f.cfg['channels'] = [dict(TWO_CHANNELS[0])]
    now[0] += 5
    channel_tick()
    assert 'terminate' in spawned[0].names() and len(spawned) == 2
    assert spawned[1].argv[-3:] == ['channel', 'actor', config_path(f)]
    # channels removed -> stopped, nothing respawned
    f.cfg['channels'] = []
    now[0] += 5
    channel_tick()
    assert 'terminate' in spawned[1].names() and len(spawned) == 2
    # the default factory: a plain Popen child that does not inherit the daemon's fds (daemon.lock flock)
    popened = []

    class Popen:
        def __init__(self, argv, **kwargs):
            popened.append((list(argv), kwargs))
            self.pid, self.returncode = 4242, None

        def poll(self):
            return self.returncode
    f2 = fleet.Fleet(f.path, f.r, FakeMgr())
    monkeypatch.setattr(subprocess, 'Popen', Popen)
    try:
        f2.cfg['channels'] = [dict(c) for c in TWO_CHANNELS]
        getattr(f2, 'channel_tick')()
        assert len(popened) == 1
        argv, kwargs = popened[0]
        assert argv[0] == f.cfg['python'] and argv[-3:] == ['channel', 'actor', config_path(f)]
        assert kwargs.get('close_fds', True) is True and kwargs.get('stdin') == subprocess.DEVNULL
    finally:
        f2.v2.close()


# ====================================================================== the Supervisor itself
def test_stale_beat_90s_restarts_actor():
    now, beat, procs, audit = [1000.0], [None], [], Audit()

    def spawn():
        procs.append(FakeProc(clock=now))
        return procs[-1]
    sup = supervise.Supervisor(spawn, clock=lambda: now[0], beat=lambda: beat[0], audit=audit)
    sup.tick()
    assert len(procs) == 1
    now[0] = 1060.0
    sup.tick()  # no beat yet, but inside the 90 s grace after spawn
    assert len(procs) == 1 and 'terminate' not in procs[0].names()
    beat[0] = 1001.0
    now[0] = 1090.0
    sup.tick()  # 89 s since the last beat: not stale
    assert len(procs) == 1 and 'terminate' not in procs[0].names()
    now[0] = 1092.0
    sup.tick()  # 91 s: alive but stale -> stop and start again at once
    assert 'terminate' in procs[0].names() and len(procs) == 2
    restarted = [d for e, d in audit if e == 'channel.actor.restarted']
    assert restarted and restarted[-1]['reason'] == 'stale_beat'


def test_failing_flag_audits_once_and_never_pauses(fx):
    f = fx
    now = [5000.0]
    f.cfg['channels'] = [dict(TWO_CHANNELS[0])]
    spawned = []

    def spawn(argv):
        spawned.append(FakeProc(list(argv), clock=now, code=1))  # exits at once, every time
        return spawned[-1]
    f.channel_spawn = spawn
    f.channel_clock = lambda: now[0]
    channel_tick = getattr(f, 'channel_tick')
    failing_at = None
    while len(spawned) < 7 and now[0] < 5000 + 5000:
        channel_tick()
        if failing_at is None and f.channel_supervisor.failing:
            failing_at = len(spawned)
        now[0] += 5
    channel_tick()  # notices the 7th exit
    assert len(spawned) == 7
    assert failing_at == 5  # the backoff reached its 300 s cap with the 5th exit
    assert f.channel_supervisor.failing
    assert len(audits(f, 'channel.actor.failing')) == 1  # one alarm per run of failures
    assert not f.paused() and audits(f, 'service.paused') == [] and audits(f, 'execution.held') == []
    req(f, 'r-failing', 'worker-1')  # dispatch goes on
    f.pump()
    assert receive(f, 'worker-1') is not None


# ====================================================================== stop: bounded drain (ruling M10)
def stop_world(fx, now, **proc):
    """A fresh daemon object on the fx run with one channel, its actor started by channel_tick."""
    f = fleet.Fleet(fx.path, fx.r, FakeMgr())
    for key in fx.r.keys('fx:channel:*'):
        fx.r.delete(key)
    f.cfg['channels'] = [dict(TWO_CHANNELS[0])]
    procs, slept = [], []

    def spawn(argv):
        procs.append(FakeProc(list(argv), clock=now, **proc))
        return procs[-1]

    def sleep(seconds):
        slept.append(seconds)
        now[0] += seconds
    f.channel_spawn, f.channel_clock, f.channel_sleep = spawn, (lambda: now[0]), sleep
    getattr(f, 'channel_tick')()
    assert len(procs) == 1
    return f, procs[0], slept


def exited(f):
    return [x for x in audits(f, 'channel.actor.exited') if x.get('reason') == 'stop']


def test_stop_terminates_then_kills_after_5s(fx):
    # ① Supervisor.stop(): terminate -> wait(5) -> kill -> wait(5); only its own handle
    now = [0.0]
    proc = FakeProc(clock=now, dies_on_terminate=False, wait_timeouts=1)
    other = FakeProc(clock=now)
    sup = supervise.Supervisor(lambda: proc, clock=lambda: now[0])
    sup.tick()
    sup.stop()
    assert proc.calls == [('terminate', 0.0), ('wait', 5), ('kill', 5.0), ('wait', 5)]
    assert other.calls == []

    # ② queue empty but a send in flight, the actor does not exit: no terminate before drain_s
    now = [100.0]
    f, proc, slept = stop_world(fx, now)
    try:
        f.r.hset(channels.ACTOR, supervise.SENDING, 'out-1')
        out = f.channel_stop_all(drain_s=5)
        assert float(f.r.hget(channels.ACTOR, supervise.DRAIN)) == pytest.approx(105.0)
        terms = [t for name, t in proc.calls if name == 'terminate']
        assert len(terms) == 1 and terms[0] >= 105.0 - 1e-6  # not "queue empty -> stop at once"
        assert out['drained'] is False and out['left'] == 0 and out['in_flight'] == 'out-1'
        assert exited(f) and exited(f)[-1]['in_flight'] == 'out-1'
    finally:
        f.v2.close()

    # ③ the in-flight send finishes at +2 s; the actor clears sending, writes drained=1, exits 0
    now = [200.0]

    def done():
        fx.r.hdel(channels.ACTOR, supervise.SENDING)
        fx.r.hset(channels.ACTOR, supervise.DRAINED, '1')
    f, proc, slept = stop_world(fx, now, exit_at=202.0, exit_code=0, on_exit=done)
    try:
        f.r.hset(channels.ACTOR, supervise.SENDING, 'out-2')
        out = f.channel_stop_all(drain_s=5)
        assert 'terminate' not in proc.names() and 'kill' not in proc.names()
        assert out['drained'] is True and out['left'] == 0 and out['in_flight'] == ''
        assert now[0] - 200.0 <= 5
    finally:
        f.v2.close()

    # ③b exit 0 but sending never cleared, or drained=1 with a non-zero exit -> not drained
    for code, clear in ((0, False), (1, True)):
        now = [300.0]

        def half(clear=clear):
            if clear:
                fx.r.hdel(channels.ACTOR, supervise.SENDING)
            fx.r.hset(channels.ACTOR, supervise.DRAINED, '1')
        f, proc, slept = stop_world(fx, now, exit_at=301.0, exit_code=code, on_exit=half)
        try:
            f.r.hset(channels.ACTOR, supervise.SENDING, 'out-3')
            assert f.channel_stop_all(drain_s=5)['drained'] is False
        finally:
            f.v2.close()

    # ④ two items left in the account queue, the actor does not exit -> not drained, left 2
    now = [400.0]
    f, proc, slept = stop_world(fx, now)
    try:
        for text in ('one', 'two'):
            f.r.lpush(ACCOUNT_Q, envelope.dumps(envelope.outbound('result', text=text, target='explicit',
                                                                  channel='telegram', account='bot-1',
                                                                  conversation='42')))
        out = f.channel_stop_all(drain_s=5)
        assert out['drained'] is False and out['left'] == 2
        assert exited(f)[-1]['left'] == '2' and exited(f)[-1]['drained'] in ('0', 'False')
    finally:
        f.v2.close()

    # ⑤ the final wait is bounded too: nothing dies, every wait times out -> still returns, unreaped
    now = [500.0]
    f, proc, slept = stop_world(fx, now, dies_on_terminate=False, dies_on_kill=False)
    try:
        out = f.channel_stop_all(drain_s=5)
        assert out['drained'] is False
        assert proc.names() == ['terminate', 'wait', 'kill', 'wait']
        assert now[0] - 500.0 <= 5 + 10  # drain_s + terminate wait + kill wait
        assert exited(f)[-1].get('unreaped') == '1'
    finally:
        f.v2.close()

    # ⑥ an unrouted item (run_closed just queued by the stop tail): the daemon does not route, it counts it
    now = [600.0]
    f, proc, slept = stop_world(fx, now)
    try:
        f.r.lpush(channels.OUTQ, envelope.dumps(envelope.outbound('run_closed', text='run closed')))
        out = f.channel_stop_all(drain_s=5)
        assert f.r.llen(channels.OUTQ) == 1 and f.r.keys(channels.OUTQ_ACCOUNT + '*') == []
        assert out['left'] == 1 and out['drained'] is False
    finally:
        f.v2.close()

    # ⑦ no channels and no actor ever started: zero wait, zero side effects
    now = [700.0]
    for key in fx.r.keys('fx:channel:*'):
        fx.r.delete(key)
    f = fleet.Fleet(fx.path, fx.r, FakeMgr())
    try:
        f.cfg.pop('channels', None)
        slept = []
        f.channel_clock, f.channel_sleep = (lambda: now[0]), slept.append
        before = len(channel_audits(f))
        f.channel_stop_all(drain_s=5)
        assert slept == [] and now[0] == 700.0
        assert f.r.keys('fx:channel:*') == [] and len(channel_audits(f)) == before
    finally:
        f.v2.close()


def test_resume_run_respawns_actor(fx):
    f = fx
    now = [100.0]
    f.cfg['channels'] = [dict(TWO_CHANNELS[0])]
    first = []
    f.channel_spawn = lambda argv: first.append(FakeProc(list(argv), clock=now)) or first[-1]
    f.channel_clock = lambda: now[0]
    getattr(f, 'channel_tick')()
    assert len(first) == 1 and f.r.hget(channels.ACTOR, 'pid') == str(first[0].pid)
    # start --resume: a new daemon process, no Supervisor state, an old pid left in Redis
    f2 = fleet.Fleet(f.path, f.r, FakeMgr())
    try:
        f2.resume_requested = True
        f2.cfg['channels'] = [dict(TWO_CHANNELS[0])]
        second = []
        f2.channel_spawn = lambda argv: second.append(FakeProc(list(argv), clock=now)) or second[-1]
        f2.channel_clock = lambda: now[0]
        f2.channel_tick()
        assert len(second) == 1  # the leftover pid is not taken as a running actor
        assert f2.r.hget(channels.ACTOR, 'pid') == str(second[0].pid)
        assert len(audits(f2, 'channel.actor.started')) == 2
    finally:
        f2.v2.close()


def test_subfleet_task_mode_v1_never_spawn(private_redis_v2, tmp_path):
    for variant in ('parent_config', 'task_mode', 'transport_v1'):
        never_spawns(private_redis_v2, tmp_path, variant)


def never_spawns(r, tmp_path, variant):
    r.flushdb()
    f = object.__new__(fleet.Fleet)
    f.r = r
    f.cfg = {'run': str(tmp_path), 'python': sys.executable, 'mode': 'service', 'transport_version': 2,
             'channels': [dict(c) for c in TWO_CHANNELS]}
    f.service, f.v2 = True, object()
    if variant == 'parent_config':
        f.cfg['parent_config'] = str(tmp_path / 'parent' / 'config.json')
    elif variant == 'task_mode':
        f.cfg['mode'], f.service = 'task', False
    else:
        f.cfg['transport_version'], f.v2 = 1, None
    calls, logged = [], []
    f.channel_spawn = lambda argv: calls.append(argv)
    f.channel_clock = lambda: 0.0
    f.log = lambda kind, **data: logged.append(kind)
    getattr(f, 'channel_tick')()
    assert calls == [] and r.keys('fx:channel:*') == [], variant
    assert not [k for k in logged if k.startswith('channel.')], variant


# ====================================================================== actor side
def test_actor_exits_when_parent_daemon_gone():
    ppid, now, released = [4242], [0.0], []
    watch = supervise.ParentWatch(4242, getppid=lambda: ppid[0])

    class Lease:
        def release(self):
            released.append(now[0])
    lease = Lease()
    while now[0] < 100 and watch.alive():  # the shape of P1g's actor loop around its work (1 s steps)
        now[0] += 1
        if now[0] == 10:
            ppid[0] = 1  # the daemon died: the actor was adopted
    lease.release()
    assert released and released[0] - 10 <= 5
    assert watch.alive() is False
    assert supervise.ParentWatch(os.getppid()).alive() is True  # the real call, our own parent


CHILD = ('import os, sys, time, redis\n'
         'redis.Redis(unix_socket_path=sys.argv[1]).rpush(sys.argv[2], os.getpid())\n'
         'time.sleep(60)\n')


def test_real_subprocess_killed_is_respawned(private_redis_v2):
    r = private_redis_v2
    r.flushdb()
    sock = r.connection_pool.connection_kwargs['path']
    key = 'test:p1f:actor-pids'
    procs = []

    def spawn():
        procs.append(subprocess.Popen([sys.executable, '-c', CHILD, sock, key], stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        return procs[-1]

    def pids(n):
        deadline = time.time() + 5
        while r.llen(key) < n and time.time() < deadline:
            time.sleep(0.05)
        return [int(p) for p in r.lrange(key, 0, -1)]
    now = [0.0]
    sup = supervise.Supervisor(spawn, clock=lambda: now[0])
    try:
        sup.tick()
        assert len(procs) == 1
        first = pids(1)
        assert first == [procs[0].pid]  # the child we spawned wrote its own pid: identity checked
        os.kill(procs[0].pid, signal.SIGKILL)  # only our own handle
        procs[0].wait(5)
        sup.tick()  # sees the exit (-9) and schedules the restart
        now[0] += 30
        sup.tick()
        assert len(procs) == 2
        both = pids(2)
        assert both == [procs[0].pid, procs[1].pid] and both[1] != both[0]
    finally:
        for p in procs:  # only handles this test spawned
            if p.poll() is None:
                p.kill()
                p.wait(5)
