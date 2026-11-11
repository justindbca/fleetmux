"""Daemon-side supervision of the fleet's one channel actor process (P1f; docs/design.md §53, contracts §8).

- ``Supervisor``: spawn / watch / restart the actor child (30 s doubling to a 300 s cap, reset after 600 s up;
  ``failing`` once the cap (or ``failing_after`` consecutive exits) is reached; a stale ``beat`` counts as dead; ``stop()`` = terminate, wait 5 s, kill,
  wait 5 s). Only ever touches the process handles it spawned itself. Re-exported by ``channels/actor.py``.
- ``ParentWatch``: the actor's own check that its parent daemon is still there (direct parent pid only).
- ``fleet_tick(f)`` / ``stop_all(f, drain_s)``: the bodies of ``Fleet.channel_tick`` (the last row of the tick
  table) and ``Fleet.channel_stop_all`` (the bounded stop drain, supervisor ruling M10).
- Observation fields of the ``fx:channel:actor`` hash used by the bounded stop drain (supervisor ruling M10):
  the daemon's ``Fleet.channel_stop_all`` writes ``drain`` and reads ``sending`` / ``drained``. On the actor side,
  ``sending`` / ``sending_at`` are written by P1e's send wrapper (set before the adapter send, cleared in finally),
  and ``drained`` by the P1g actor loop once it has drained; the actor reads ``drain``.

Consecutive failures are an alarm only (``channel.actor.failing``, once per run of failures): the fleet is never
paused and no slot is held because of the channel actor.
"""
import json
import os
import subprocess
import time
from pathlib import Path

from fleetmux import channels

SENDING = 'sending'        # id of the outbound item the actor is sending right now ('' / absent = none)
SENDING_AT = 'sending_at'  # when that send started (epoch s)
DRAIN = 'drain'            # written by the daemon: drain deadline (epoch s); the actor stops inbound and drains
DRAINED = 'drained'        # written by the actor: '1' once both queue kinds are empty and nothing is in flight
BEAT = 'beat'              # written by the actor's own heartbeat thread (P1g), epoch s
DRAIN_STEP_S = 0.1         # how often the daemon looks at the actor while it drains


class Supervisor:
    """Supervises one child process made by ``spawn()`` (a ``subprocess.Popen``-shaped handle: ``pid``,
    ``poll``, ``terminate``, ``kill``, ``wait(timeout)``). ``tick()`` is called from the daemon loop."""

    def __init__(self, spawn, clock=time.time, settings=None, *, audit=None, beat=None,
                 stale_s=90, grace_s=90, stop_wait_s=5):
        self.spawn = spawn
        self.clock = clock
        self.settings_arg = settings
        self.audit = audit
        self.beat = beat
        self.stale_s = stale_s
        self.grace_s = grace_s
        self.stop_wait_s = stop_wait_s
        self.proc = None
        self.started_at = None
        self.next_start = None   # None = start at the next tick
        self.delay = None        # the backoff applied to the last restart (None = no failure in this run)
        self._failing = False
        self._restart_reason = None
        self.failures = 0        # consecutive exits since the last stable run

    # ------------------------------------------------------------------ public
    @property
    def settings(self):
        return dict(channels.SUPERVISE_DEFAULTS, **(self.settings_arg or {}))

    @property
    def failing(self):
        """True once the restart backoff has reached its cap (brief v2 P1f), until 600 s of stable running."""
        return self._failing

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def tick(self):
        now = self.clock()
        s = self.settings
        if self.proc is not None:
            code = self.proc.poll()
            if code is not None:
                self._exited(code, now, s)
            else:
                if self.delay is not None and now - self.started_at >= s['stable_reset_s']:
                    self._reset()  # stable: the backoff starts over
                if self._stale(now):
                    self._audit('channel.actor.restarted', reason='stale_beat', pid=self._pid())
                    self.stop()
                    self._start(now, reason=None)
                    return
        if self.proc is None and (self.next_start is None or now >= self.next_start):
            self._start(now, reason=self._restart_reason)

    def stop(self):
        """terminate -> wait(stop_wait_s) -> kill -> wait(stop_wait_s); bounded, own handle only. Returns
        ``{'code', 'killed', 'unreaped'}`` (``None`` without a process). The process is forgotten afterwards."""
        proc, self.proc = self.proc, None
        self.next_start = None
        if proc is None:
            return None
        killed = unreaped = False
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=self.stop_wait_s)
            except subprocess.TimeoutExpired:
                killed = True
                proc.kill()
                try:
                    proc.wait(timeout=self.stop_wait_s)
                except subprocess.TimeoutExpired:
                    unreaped = True
        return {'code': proc.poll(), 'killed': killed, 'unreaped': unreaped, 'pid': getattr(proc, 'pid', None)}

    # ------------------------------------------------------------------ internals
    def _pid(self):
        return getattr(self.proc, 'pid', None)

    def _start(self, now, reason):
        self.proc = self.spawn()
        self.started_at, self.next_start, self._restart_reason = now, None, None
        if reason:
            self._audit('channel.actor.restarted', reason=reason, pid=self._pid())
        else:
            self._audit('channel.actor.started', pid=self._pid())

    def _exited(self, code, now, s):
        pid = self._pid()
        self.proc = None
        if self.delay is not None and now - self.started_at >= s['stable_reset_s']:
            self._reset()
        self.failures += 1
        self.delay = s['backoff_start_s'] if self.delay is None else min(self.delay * 2, s['backoff_cap_s'])
        self.next_start = now + self.delay
        self._restart_reason = 'exited'
        self._audit('channel.actor.exited', code=code, pid=pid, restart_in_s=self.delay)
        if (self.delay >= s['backoff_cap_s'] or self.failures >= s['failing_after']) and not self._failing:
            self._failing = True
            self._audit('channel.actor.failing', code=code, backoff_s=self.delay)

    def _reset(self):
        self.delay, self._failing, self.failures = None, False, 0

    def _stale(self, now):
        if self.beat is None:
            return False
        try:
            beat = self.beat()
            beat = float(beat) if beat not in (None, '') else None
        except Exception:
            beat = None
        if beat is None or beat < self.started_at:  # nothing from this process yet: the start-up grace
            return now - self.started_at > self.grace_s
        return now - beat > self.stale_s

    def _audit(self, event, **fields):
        if self.audit is None:
            return
        try:
            self.audit(event, **fields)
        except Exception:  # an audit sink failure never stops supervision
            pass


class ParentWatch:
    """Inside the actor: is the parent daemon (the process that spawned us) still our parent? When the daemon
    dies the actor is re-parented (to 1 / a subreaper), so ``getppid()`` changes. The actor loop checks this at
    least every second and exits (releasing its leases) as soon as it is False."""

    def __init__(self, parent_pid, *, getppid=os.getppid):
        self.parent_pid = parent_pid
        self.getppid = getppid

    def alive(self):
        try:
            return int(self.getppid()) == int(self.parent_pid)
        except Exception:
            return False


# ====================================================================== the daemon side (Fleet)
def wanted(f):
    """The main fleet in service mode on transport v2 with a non-empty ``channels:`` gets the actor."""
    cfg = getattr(f, 'cfg', None) or {}
    return bool(cfg.get('channels')) and not cfg.get('parent_config') and bool(getattr(f, 'service', False)) \
        and cfg.get('mode', 'service') == 'service' and getattr(f, 'v2', None) is not None


def actor_argv(f):
    cfg = f.cfg
    fleet_py = str(Path(__file__).resolve().parent.parent / 'fleet.py')
    config = str(Path(cfg['run']) / 'config.json') if cfg.get('run') else str(f.path)
    return [cfg.get('python') or 'python3', fleet_py, 'channel', 'actor', config]


def default_spawn(f):
    def spawn(argv):
        log = None
        try:
            log = open(Path(f.cfg['run']) / 'channel-actor.log', 'ab') if f.cfg.get('run') else None
        except OSError:
            log = None
        try:
            return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log or subprocess.DEVNULL,
                                    stderr=subprocess.STDOUT if log else subprocess.DEVNULL, close_fds=True)
        finally:
            if log is not None:
                log.close()
    return spawn


def _clock(f):
    return getattr(f, 'channel_clock', None) or time.time


def _audit(f):
    return lambda event, **fields: f.log(event, **fields)


def _signature(f):
    return json.dumps(f.cfg.get('channels') or [], sort_keys=True, default=str)


def _supervisor(f):
    sup = getattr(f, 'channel_supervisor', None)
    if sup is not None:
        return sup
    spawn = getattr(f, 'channel_spawn', None) or default_spawn(f)
    argv = actor_argv(f)

    def start():
        proc = spawn(list(argv))
        f.r.hdel(channels.ACTOR, DRAIN, DRAINED, SENDING, SENDING_AT, BEAT)  # nothing of an older actor counts
        f.r.hset(channels.ACTOR, mapping={'pid': str(getattr(proc, 'pid', '')), 'started': repr(_clock(f)())})
        return proc

    def beat():
        return f.r.hget(channels.ACTOR, BEAT)
    settings = f.cfg.get('channel_supervise') if isinstance(f.cfg.get('channel_supervise'), dict) else None
    sup = Supervisor(start, clock=_clock(f), settings=settings, audit=_audit(f), beat=beat)
    sup.signature = _signature(f)
    f.channel_supervisor = sup
    return sup


def fleet_tick(f):
    """``Fleet.channel_tick``: start the actor when channels are configured, restart it on exit (backoff) or a
    stale heartbeat, restart it when the roster's ``channels:`` changed, stop it when they are gone. Without
    channels (or in a sub-fleet / task mode / v1) and no actor of ours: no Redis write, no audit."""
    sup = getattr(f, 'channel_supervisor', None)
    if not wanted(f):
        if sup is not None and sup.proc is not None:
            out = sup.stop() or {}
            f.log('channel.actor.exited', reason='config_removed', code=out.get('code'), pid=out.get('pid'))
        if sup is not None:
            f.channel_supervisor = None
        return
    if sup is not None and sup.signature != _signature(f):
        if sup.proc is not None:
            out = sup.stop() or {}
            f.log('channel.actor.exited', reason='config_changed', code=out.get('code'), pid=out.get('pid'))
        f.channel_supervisor = None
        sup = _supervisor(f)
        sup._restart_reason = 'config_changed'
    _supervisor(f).tick()


def _left(r):
    keys = [channels.OUTQ] + list(r.scan_iter(match=channels.OUTQ_ACCOUNT + '*', count=100))
    return sum(int(r.llen(k) or 0) for k in keys)


def stop_all(f, drain_s=5):
    """``Fleet.channel_stop_all``: the bounded stop drain (ruling M10). Writes ``drain`` (deadline), waits up to
    ``drain_s`` for the actor to exit by itself (it routes and sends what is queued, clears ``sending``, writes
    ``drained=1``, exits 0), then ``Supervisor.stop()``. Returns ``{drained, left, in_flight, code}``;
    ``drained`` only for exit 0 + ``drained=1`` + nothing in flight + nothing left. The daemon itself never
    routes or sends. No actor of ours: returns at once, no Redis write, no audit."""
    sup = getattr(f, 'channel_supervisor', None)
    if sup is None or sup.proc is None:
        return {'drained': True, 'left': 0, 'in_flight': '', 'code': None, 'stopped': False}
    clock = _clock(f)
    sleep = getattr(f, 'channel_sleep', None) or time.sleep
    deadline = clock() + drain_s
    f.r.hset(channels.ACTOR, DRAIN, repr(deadline))
    code = sup.proc.poll()
    while code is None and clock() < deadline:
        sleep(min(DRAIN_STEP_S, max(0.0, deadline - clock())))
        code = sup.proc.poll()
    out = {'code': code, 'killed': False, 'unreaped': False}
    if code is None:
        out = sup.stop()
    else:
        sup.proc, sup.next_start = None, None
    f.channel_supervisor = None
    actor = f.r.hgetall(channels.ACTOR)
    in_flight = actor.get(SENDING) or ''
    left = _left(f.r)
    drained = out.get('code') == 0 and actor.get(DRAINED) == '1' and not in_flight and left == 0
    fields = {'reason': 'stop', 'code': out.get('code'), 'drained': '1' if drained else '0', 'left': str(left),
              'in_flight': in_flight}
    if out.get('unreaped'):
        fields['unreaped'] = '1'
    if out.get('killed'):
        fields['killed'] = '1'
    f.log('channel.actor.exited', **fields)
    return {'drained': drained, 'left': left, 'in_flight': in_flight, 'code': out.get('code'), 'stopped': True}
