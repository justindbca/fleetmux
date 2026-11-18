"""Lease calls of the applet SDK (docs/applet-interface.md section 7): a thin layer over section 3.

``LeaseCalls`` is a mixin of ``Applet``. It uses only the applet's public attributes (``name``, ``token``,
``redis``, ``config_path``, ``state_dir``, ``stopping``, ``fleet_clock``) and imports nothing of the release but
``fleetmux.awake`` (standard library only): ops go in through the documented door (``fleetmux request -``, section
3.2), run as a child process by the release's interpreter (``FLEETMUX_APPLET_PYTHON``, section 2.2), and answers
are read from ``fx:result:<id>``.

Every local wait is measured with ``awake.mono()``, a clock that stops while the machine sleeps: a closed lid is
no waiting time (docs/design.md section 35). Lease times from the host (``expires_at``) are the host fleet's awake
seconds and are compared with ``fleet_clock`` (fleetmux/awake.py).
"""
import contextlib
import fcntl
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid

from fleetmux import awake

RESULT = 'fx:result:'
POLL_S = 0.2
OPEN = ('granted', 'active', 'renew_requested')  # lease states that hold a slot
ESCALATE_GRACE_S = 60.0  # escalate(wait_s=...) waits locally this long past wait_s: the host times out on its tick
# Local bounds of the other calls when no wait_s is given (applet-interface.md 1.1 ``waits``, 7): the host's own
# bounds at their roster defaults plus ESCALATE_GRACE_S. The host passes its values in FLEETMUX_APPLET_WAITS.
WAITS = {'request_lease': 660, 'renew': 660, 'send': 9360, 'release': 60, 'notify': 60}
DOOR_MAX_S = 120.0  # one `fleetmux request` child at most this long (the command itself waits about 30 s)
STOPPING = {'status': 'rejected', 'by': 'program', 'reason': 'stopping', 'note': 'the applet is stopping'}
ESCALATE_ACCEPT_S = 10.0  # escalate(wait=False) looks this long for the host's record or refusal after the door
ESCALATE_NO_WAIT = True   # capability marker: escalate() takes wait= (an older SDK has no such name)
AUDIT_QUEUE_MAX = 1000   # audit records waiting for the door; more are dropped (and logged)
EVENT_RE = r'[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*'  # applet.audit event name (section 3.7)
_audit_lock = threading.Lock()


@contextlib.contextmanager
def file_lock(path):
    """Exclusive ``flock`` on ``<path>.lock`` (applet-interface.md section 2.5): every writer of the project YAML,
    SDK and host daemon, holds it while it reads the file again, changes its own part and replaces the file."""
    lock = str(path) + '.lock'
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class LeaseDenied(Exception):
    """A lease request was refused or not decided in time: ``by``, ``reason``, ``note``, ``retry_after_s``."""
    def __init__(self, by, reason, note='', retry_after_s=None):
        super().__init__(reason + (': ' + note if note else ''))
        self.by, self.reason, self.note, self.retry_after_s = by, reason, note, retry_after_s


class LeaseEnded(Exception):
    """The lease is over: ``state`` (released / expired / revoked / unknown) and ``end_reason``."""
    def __init__(self, state, end_reason=''):
        super().__init__('lease ended (' + state + (', ' + end_reason if end_reason else '') + ')')
        self.state, self.end_reason = state, end_reason


class Rejected(Exception):
    """Any other refused op: ``by``, ``reason``, ``note``."""
    def __init__(self, by, reason, note=''):
        super().__init__(reason + (': ' + note if note else ''))
        self.by, self.reason, self.note = by, reason, note


class Review:
    """The host's review of a fix (applet-interface.md 3.9): ``verdict`` (pass / fail / none), ``round``,
    ``findings``, ``capped``, ``reason`` (for none), ``report``."""
    def __init__(self, data):
        self.verdict, self.round = data.get('verdict', 'none'), int(data.get('round') or 0)
        self.findings, self.capped = list(data.get('findings') or []), bool(data.get('capped'))
        self.reason, self.report = data.get('reason'), data.get('report')
        self.id, self.reviewer = data.get('id', ''), data.get('reviewer', '')

    def __repr__(self):
        return 'Review(%r, round=%r, capped=%r)' % (self.verdict, self.round, self.capped)


class Reply:
    """The result of one message: ``id`` (the message id), ``status``, ``result``, ``worker`` (label),
    ``elapsed_s``, ``review`` (set when the message carried ``fix=`` and the worker replied ok)."""
    def __init__(self, status, result=None, worker='', elapsed_s=0.0, review=None, id=''):
        self.status, self.result, self.worker, self.elapsed_s = status, result, worker, elapsed_s
        self.review = Review(review) if isinstance(review, dict) else None
        self.id = id

    def __repr__(self):
        return 'Reply(%r, %r)' % (self.status, self.result)


class Ruling:
    """An answered escalation: ``id`` (the escalation's op id), ``decision``, ``choice`` (branch on this), ``by``,
    ``note``."""
    def __init__(self, decision, choice=None, by='', note='', id=''):
        self.decision, self.choice, self.by, self.note, self.id = decision, choice, by, note, id

    def __repr__(self):
        return 'Ruling(%r, choice=%r, by=%r)' % (self.decision, self.choice, self.by)


class Escalation:
    """An escalation of this applet as the host records it (``fx:applet:<name>:esc:<id>``): ``id``, ``cls``,
    ``job``, ``what``, ``why``, ``options``, ``paths``, ``status`` (``awaiting`` / ``awaiting_human`` / ``ruled`` /
    ``timeout``), ``created``."""
    def __init__(self, applet, data):
        self._applet = applet
        self.id, self.cls, self.job = data['id'], data.get('class', ''), data.get('job', '')
        self.what, self.why, self.status = data.get('what', ''), data.get('why', ''), data.get('status', '')
        self.options, self.paths = _loads(data.get('options'), []), _loads(data.get('paths'), [])
        self.created = float(data.get('created') or 0)

    def __repr__(self):
        return 'Escalation(%r, cls=%r, status=%r)' % (self.id, self.cls, self.status)

    def ruling(self, *, wait_s=None):
        """The ruling (at once when answered). Waits at most ``wait_s`` seconds (None: until ruled). Raises
        Rejected as escalate() does: ``no_decision`` (host timeout, or ``wait_s`` passed), ``stopping``."""
        return self._applet._ruling(self.id, self._applet._wait_result(self.id, wait_s))


def _loads(raw, default):
    try:
        return json.loads(raw) if raw else default
    except ValueError:
        return default


def _reply(applet, rid, out):
    """A message result as a Reply; LeaseEnded / Rejected as Lease.send documents."""
    status = out.get('status')
    if status == 'lease_ended' or out.get('reason') == 'lease_not_active':
        lid = out.get('lease') or ''
        rec = applet.redis.hgetall('fx:applet:' + applet.name + ':lease:' + lid) if lid else {}
        raise LeaseEnded(rec.get('state') or 'unknown', rec.get('end_reason') or '')
    if status == 'rejected':
        raise Rejected(out.get('by', 'program'), out.get('reason', ''), out.get('note', ''))
    return Reply(status, out.get('result'), out.get('worker', ''), float(out.get('elapsed_s') or 0),
                 out.get('review'), rid)


def _submit(applet, spec, cut=None):
    """Default door: ``fleetmux request -`` against the host config, run by the release's interpreter
    (``FLEETMUX_APPLET_PYTHON``), never the applet's own; returns the bus answer. ``cut``: a callable checked
    while the child runs; once it is true (or after DOOR_MAX_S) the child is killed and the answer is ``pending``,
    as the command answers when the host does not (the op may still have reached the host). The child stays in
    the applet's process group, so the host's sweep of that group after SIGKILL covers it too."""
    if cut is not None and cut():
        return {'status': 'pending', 'note': 'not sent'}
    src = getattr(applet, '_env', None) or os.environ
    env = dict(os.environ)
    sdk = src.get('FLEETMUX_APPLET_SDK')
    if sdk:
        env['PYTHONPATH'] = sdk + (os.pathsep + env['PYTHONPATH'] if env.get('PYTHONPATH') else '')
    python = src.get('FLEETMUX_APPLET_PYTHON') or sys.executable
    proc = subprocess.Popen([python, '-m', 'fleetmux.fleet', 'request', '-', '--config', str(applet.config_path)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=env)
    data, end = json.dumps(spec), awake.mono() + DOOR_MAX_S
    while True:
        try:
            out, err = proc.communicate(data, timeout=POLL_S)
            break
        except subprocess.TimeoutExpired:
            data = None  # sent with the first call
            if (cut is not None and cut()) or awake.mono() >= end:
                proc.kill()
                proc.communicate()
                return {'status': 'pending', 'note': 'the door was cut before the host answered'}
    lines = [x for x in out.splitlines() if x.strip().startswith('{')]
    if not lines:
        return {'status': 'rejected', 'reason': 'request command failed: ' + (err or '')[-300:]}
    return json.loads(lines[-1])


def _bounded(out):
    """A call that passed its own local bound: ``rejected`` ``no_answer`` (the host may still act on the op)."""
    if out.get('local'):
        return {'status': 'rejected', 'by': 'program', 'reason': 'no_answer',
                'note': 'no answer from the host within wait_s; the op may still be processed'}
    return out


class Lease:
    """An applet lease on one worker slot."""

    def __init__(self, applet, data):
        self._applet = applet
        self.id, self.slot, self.job = data['id'], data.get('slot', ''), data.get('job', '')
        exp = data.get('expires_at')
        self.expires_at = float(exp) if exp not in (None, '') else None

    def __repr__(self):
        return 'Lease(%r, slot=%r)' % (self.id, self.slot)

    def _record(self):
        return self._applet.redis.hgetall('fx:applet:' + self._applet.name + ':lease:' + self.id)

    def send(self, payload, *, instruction, required, allowed=None, timeout_s=None, replay=True, fix=None,
             msg_id=None, wait_s=None):
        """Send one message to the leased worker and wait for its result.
        fix={"dir", "base", "head", "files"} marks the message that hands back a fix: the host then has
        another slot review it and the reply carries .review. Raises LeaseEnded when the lease is over.
        msg_id: the message id (default: a new one); save it first to read the result back after a restart
        (Applet.message_result). The Reply carries it as .id. wait_s: local bound (default waits['send']);
        past it Rejected ``no_answer``."""
        a = self._applet
        spec = {'id': msg_id or a._new_id(), 'to': self.slot, 'lease': self.id, 'applet': a.name, 'token': a.token,
                'instruction': instruction, 'payload': payload, 'replay': replay,
                'reply': {'required': list(required), **({'allowed': list(allowed)} if allowed is not None else {})}}
        if timeout_s is not None:
            spec['timeout_s'] = timeout_s
        if fix is not None:
            spec['fix'] = fix
        return _reply(a, spec['id'], dict(_bounded(a._call(spec, a._bound('send', wait_s))), lease=self.id))

    def renew(self, *, need_s, progress, why=None, wait_s=None):
        """Ask for more time. True when granted (expires_at updated); False when denied, unanswered within
        wait_s (default waits['renew']) or stopping."""
        a = self._applet
        out = a._op('applet.lease.renew', a._bound('renew', wait_s), lease=self.id, need_s=need_s,
                    progress=progress, **({'why': why} if why else {}))
        if out.get('status') == 'ok':
            self.expires_at = float(out['lease']['expires_at'])
            return True
        return False

    def release(self, outcome=None, *, force=False, wait_s=None):
        """Give the worker back. Call as soon as the job is done; a later job requests a new lease. wait_s: local
        bound (default waits['release']); past it Rejected ``no_answer``."""
        a = self._applet
        out = a._op('applet.lease.release', a._bound('release', wait_s), lease=self.id, outcome=outcome or {},
                    force=force)
        if out.get('status') != 'ok' and out.get('reason') != 'lease_not_active':
            raise Rejected(out.get('by', 'program'), out.get('reason', ''), out.get('note', ''))

    def remaining_s(self):
        """Awake seconds until expiry (``expires_at`` is the host fleet's awake time, see ``fleet_clock``)."""
        if self.expires_at is None:
            exp = self._record().get('expires_at')
            self.expires_at = float(exp) if exp else None
        return max(0.0, self.expires_at - self._applet._fleet_now()) if self.expires_at else float(
            self._record().get('ttl_s') or 0)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        """Releases the lease (force=True when leaving on an exception)."""
        try:
            self.release(force=exc[0] is not None)
        except (Rejected, LeaseEnded):
            pass


class LeaseCalls:
    """Mixin of ``Applet``: request_lease, leases, escalate, escalations, message_result, notify, audit."""

    def submit(self, spec):
        """The op door (``fleetmux request -``); returns the bus answer."""
        return _submit(self, spec)

    def _door(self, spec, deadline=None, cut=None):
        """``submit`` for one call: the default door is cut when the Event ``cut`` is set or at ``deadline``
        (answer ``pending``). A replaced ``submit`` (a test double) is called as it is."""
        if 'submit' in vars(self) or type(self).submit is not LeaseCalls.submit:
            return self.submit(spec)
        return _submit(self, spec, lambda: (cut is not None and cut.is_set())
                       or (deadline is not None and awake.mono() >= deadline))

    def _bound(self, call, wait_s):
        """The local bound of ``call``: ``wait_s``, else the host's ``waits`` value, else WAITS."""
        if wait_s is not None:
            return float(wait_s)
        return float((getattr(self, 'waits', None) or {}).get(call, WAITS[call]))

    def _fleet_now(self):
        """The host fleet's awake time (``fleet_clock``, synced by the heartbeat); Unix time without one."""
        clock = getattr(self, 'fleet_clock', None)
        return clock.now() if clock is not None else time.time()

    def _new_id(self):
        return self.name + '.' + uuid.uuid4().hex[:16]

    def _wait_result(self, rid, wait_s):
        deadline = None if wait_s is None else awake.mono() + wait_s
        while True:
            raw = self.redis.get(RESULT + rid)
            if raw:
                return json.loads(raw)
            if self.stopping.is_set():
                return {'status': 'rejected', 'by': 'program', 'reason': 'stopping', 'note': 'the applet is stopping'}
            if deadline is not None and awake.mono() >= deadline:
                return {'status': 'timeout', 'reason': 'no_decision', 'local': True,
                        'note': 'no answer from the host within the local wait'}
            time.sleep(POLL_S)

    def _call(self, spec, wait_s):
        """Submit ``spec`` and wait for its result, both within ``wait_s`` (None: no bound). Once ``stopping`` is
        set nothing is sent, and a call already waiting returns at once (its door child is killed): ``stopping``."""
        deadline = None if wait_s is None else awake.mono() + wait_s
        if self.stopping.is_set():
            return dict(STOPPING)
        answer = self._door(spec, deadline, self.stopping)
        status = answer.get('status')
        if status == 'rejected' and not str(answer.get('reason', '')).startswith('duplicate'):
            reason = str(answer.get('reason', ''))
            return {'status': 'rejected', 'by': 'program', 'reason': reason.split(':', 1)[0], 'note': reason}
        return self._wait_result(spec['id'], None if deadline is None else max(0.0, deadline - awake.mono()))

    def _op(self, op, result_wait_s, **fields):
        spec = {'id': self._new_id(), 'op': op, 'applet': self.name, 'token': self.token, 'v': 1}
        spec.update({k: v for k, v in fields.items() if v is not None})
        return _bounded(self._call(spec, result_wait_s))

    def request_lease(self, *, job, purpose, ttl_s=None, paths=(), want=None, context=None, wait_s=None):
        """Ask the supervisor for a worker. Blocks until granted; if no worker is free the host wakes or
        adds one. Raises LeaseDenied (with .by, .reason, .note, .retry_after_s) when refused or not
        decided in time. wait_s: local bound (default waits['request_lease']); past it LeaseDenied ``no_answer``
        (the request may still be granted: leases() finds it)."""
        out = self._op('applet.lease.request', self._bound('request_lease', wait_s), job=job, purpose=purpose,
                       ttl_s=ttl_s, paths=list(paths), want=want, context=context)
        if out.get('status') == 'ok':
            return Lease(self, out['lease'])
        if out.get('status') == 'timeout':
            raise LeaseDenied('program', 'no_decision', 'the supervisor did not answer in time')
        raise LeaseDenied(out.get('by', 'program'), out.get('reason', ''), out.get('note', ''),
                          out.get('retry_after_s'))

    def leases(self):
        """Open leases of this applet as the host records them (use after a restart to re-attach)."""
        out = []
        for lid in sorted(self.redis.smembers('fx:applet:' + self.name + ':leases')):
            rec = self.redis.hgetall('fx:applet:' + self.name + ':lease:' + lid)
            if rec.get('slot') and rec.get('state') in OPEN:
                out.append(Lease(self, rec))
        return out

    def _return_leases(self, wait_s):
        """Stop path (section 2.4): release every open lease of this applet (``force``: a message may still run) and
        wait, at most ``wait_s`` in all, until the host records each one ended. Ignores ``stopping``; each release
        is submitted on its own thread so a door that does not answer cannot hold the exit. Returns the lease ids
        still open."""
        deadline = awake.mono() + wait_s
        ids = [lease.id for lease in self.leases()]
        for lid in ids:
            spec = {'id': self._new_id(), 'op': 'applet.lease.release', 'applet': self.name, 'token': self.token,
                    'v': 1, 'lease': lid, 'outcome': {}, 'force': True}
            threading.Thread(target=self._door, args=(spec, deadline), name='applet-release', daemon=True).start()

        def still_open():
            return [lid for lid in ids
                    if self.redis.hget('fx:applet:' + self.name + ':lease:' + lid, 'state') in OPEN]
        left = still_open()
        while left and awake.mono() < deadline:
            time.sleep(POLL_S)
            left = still_open()
        return left

    def escalate(self, *, cls, what, why, job=None, options=(), paths=(), wait_s=None, op_id=None, wait=True):
        """Hand a decision to the supervisor (or, for cls='delete' and cls='human', the human owner) and wait for
        the ruling. Returns a Ruling (``id`` = the op id) for approve / revise / reject. Raises Rejected: refused by
        the program; ``no_decision`` when ``wait_s`` passed (the host's timeout, or locally ESCALATE_GRACE_S after
        it when no host answers); ``stopping``. op_id: the op id (default: a new one); save it first to re-attach
        after a restart (escalations(), or escalate again with the same op_id).

        ``wait=False``: return the op id (str) once the host has opened the escalation, without waiting for the
        ruling; claim it later with escalations() / Escalation.ruling(). Raises Rejected for a program refusal
        and ``stopping``. Safe to repeat with the same op_id: an open, ruled or timed-out escalation is not opened
        again."""
        rid = op_id or self._new_id()
        spec = {'id': rid, 'op': 'applet.escalate', 'applet': self.name, 'token': self.token, 'v': 1,
                'class': cls, 'what': what, 'why': why, 'options': list(options), 'paths': list(paths)}
        spec.update({k: v for k, v in (('job', job), ('wait_s', wait_s)) if v is not None})
        if not wait:
            return self._opened(rid, spec)
        local = None if wait_s is None else min(float(wait_s), 86400.0) + ESCALATE_GRACE_S
        return self._ruling(rid, self._call(spec, local))

    def _opened(self, rid, spec):
        """escalate(wait=False): send ``spec`` through the door (cut by ``stopping``, bounded by DOOR_MAX_S; once
        ``stopping`` is set nothing is sent), raise a program refusal (the host publishes it at
        ``fx:result:<id>`` before the door answers ``accepted``), else return ``rid`` once the escalation record
        or any result is there, or after ESCALATE_ACCEPT_S. A ``duplicate`` id with neither left (kept time over)
        is Rejected ``duplicate``: the host will not open it again."""
        if self.stopping.is_set():
            raise Rejected('program', 'stopping', 'the applet is stopping')
        answer = self._door(spec, awake.mono() + DOOR_MAX_S, self.stopping)
        reason = str(answer.get('reason', ''))
        spent = reason.startswith('duplicate')
        out = None
        if answer.get('status') == 'rejected' and not spent:
            out = {'status': 'rejected', 'by': 'program', 'reason': reason.split(':', 1)[0], 'note': reason}
        deadline = awake.mono() + ESCALATE_ACCEPT_S
        while out is None:
            raw = self.redis.get(RESULT + rid)
            if raw:
                out = json.loads(raw)
            elif self.redis.exists('fx:applet:' + self.name + ':esc:' + rid):
                return rid
            elif spent:
                out = {'status': 'rejected', 'by': 'program', 'reason': 'duplicate', 'note': answer.get('reason', '')}
            elif self.stopping.is_set():
                out = {'status': 'rejected', 'by': 'program', 'reason': 'stopping', 'note': 'the applet is stopping'}
            elif awake.mono() >= deadline:
                return rid
            else:
                time.sleep(POLL_S)
        if out.get('status') == 'rejected' and out.get('by', 'program') == 'program':
            raise Rejected('program', out.get('reason', ''), out.get('note', ''))
        return rid

    def _ruling(self, rid, out):
        if out.get('status') == 'ok':
            return Ruling(out.get('decision', 'approve'), out.get('choice'), out.get('by', ''), out.get('note', ''),
                          rid)
        if out.get('status') == 'rejected' and out.get('by') in ('supervisor', 'root', 'human'):
            return Ruling('reject', None, out['by'], out.get('note', ''), rid)
        raise Rejected(out.get('by', 'program'), out.get('reason', out.get('status', '')), out.get('note', ''))

    def escalations(self):
        """Escalations of this applet the host keeps, open and answered, oldest first (use after a restart to
        re-attach: Escalation.ruling())."""
        rows = [self.redis.hgetall(k) for k in self.redis.scan_iter('fx:applet:' + self.name + ':esc:*')]
        return sorted((Escalation(self, x) for x in rows if x.get('id')), key=lambda e: (e.created, e.id))

    def message_result(self, msg_id, *, wait_s=None):
        """The result of a message sent earlier with this id (Lease.send msg_id), read back instead of sending it
        again. Waits at most ``wait_s`` seconds (None: until it comes); None when the host has none by then.
        Raises as Lease.send does (LeaseEnded, Rejected)."""
        out = self._wait_result(msg_id, wait_s)
        if out.get('local'):
            return None
        return _reply(self, msg_id, out)

    def notify(self, text, *, job=None, wait_s=None):
        """One information line to the supervisor. Rate limited; costs a model turn. wait_s: local bound (default
        waits['notify']); past it Rejected ``no_answer``."""
        out = self._op('applet.notify', self._bound('notify', wait_s), text=text, job=job)
        if out.get('status') != 'ok':
            raise Rejected(out.get('by', 'program'), out.get('reason', ''), out.get('note', ''))

    def audit(self, event, **detail):
        """One audit record (`applet.event`). Returns at once: the record is submitted in order on a background
        thread, best effort (a failure is logged to stderr, never raised; a stop does not refuse it). Raises
        ValueError only for a malformed ``event`` or ``detail``, before any I/O."""
        if not isinstance(event, str) or not re.fullmatch(EVENT_RE, event):
            raise ValueError('audit event must match ' + EVENT_RE)
        try:
            size = len(json.dumps(detail, ensure_ascii=False, separators=(',', ':')))
        except (TypeError, ValueError) as exc:
            raise ValueError('audit detail must be JSON: ' + str(exc)[:200]) from None
        if size > 4000:
            raise ValueError('audit detail must be at most 4000 characters as JSON')
        spec = {'id': self._new_id(), 'op': 'applet.audit', 'applet': self.name, 'token': self.token, 'v': 1,
                'event': event, 'detail': detail}
        try:
            self._audits().put_nowait(spec)
        except queue.Full:
            print('applet audit: queue full, dropped ' + event, file=sys.stderr, flush=True)

    def _audits(self):
        with _audit_lock:
            q = self.__dict__.get('_audit_q')
            if q is None:
                q = self._audit_q = queue.Queue(AUDIT_QUEUE_MAX)
                self._audit_cut = threading.Event()  # set by _drop_audits on exit
                threading.Thread(target=self._audit_loop, args=(q, self._audit_cut), name='applet-audit',
                                 daemon=True).start()
            return q

    def _audit_loop(self, q, cut):
        while True:
            spec = q.get()
            try:
                if cut.is_set():
                    print('applet audit: ' + spec['event'] + ' not sent: exit flush time is over', file=sys.stderr,
                          flush=True)
                    continue
                out = self._door(spec, None, cut)
                if out.get('status') == 'rejected':
                    print('applet audit: ' + spec['event'] + ' refused: ' + str(out.get('reason'))[:200],
                          file=sys.stderr, flush=True)
            except Exception as exc:
                print('applet audit: ' + spec['event'] + ' not sent: ' + str(exc)[:200], file=sys.stderr, flush=True)
            finally:
                q.task_done()

    def _flush_audits(self, wait_s):
        """Wait at most ``wait_s`` until queued audit records are submitted; returns how many are left."""
        q = self.__dict__.get('_audit_q')
        if q is None:
            return 0
        deadline = awake.mono() + wait_s
        while q.unfinished_tasks and awake.mono() < deadline:
            time.sleep(min(POLL_S, max(0.0, deadline - awake.mono())) or 0.01)
        return q.unfinished_tasks

    def _drop_audits(self):
        """Exit path, after the bounded flush: the record in flight is cut (its door child killed) and the
        records still queued are dropped (logged)."""
        cut = self.__dict__.get('_audit_cut')
        if cut is not None:
            cut.set()
