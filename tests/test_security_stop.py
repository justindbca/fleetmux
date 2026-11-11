"""P8m: hard stop ``/stop`` (brief v2.2 §8.2 E2 / §8.4; contracts §8 「硬停 /stop」 Z1–Z11 and #17, security freeze
batch 2; core-3 P8m-freeze-candidate.md / P8m-test-plan.md).

Red until package P8m lands (phase B): ``channels/stop.py`` is an A2' stub whose bodies raise NotImplementedError
(except the no-policy branches, which already behave as frozen), and ``Fleet.security_tick`` /
``Fleet.security_keeps_pause`` do not exist yet (no stub in fleet.py, supervisor ruling #2: the tests fetch them
inside the test body, so they fail, never error at collection). The one row marked [基线] is green now.

Fakes only: the repo's private test Redis (unix socket, no TCP), FakeMgr (keys are only recorded), an in-memory
FakeFS for every stop-flag file operation (no real lock / rename / unlink), a Flaky Redis proxy for failures.
"""
import copy
import functools
import json
import math
import re
import time
import types
from pathlib import Path

import pytest
import redis

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture: the repo's private test Redis)
from test_runtime_v2 import audits, event, fx  # noqa: F401  (fx: pi supervisor + worker-1/2, service, v2)
from test_channels_contract import FakeChannel
from test_lifecycle_close import stop_tail
from fleetmux import channels, fleet, fleetmux_sidebar, lifecycle_close, runtime_v2, subfleet, tabs
from fleetmux.channels import envelope, security, stop

OLD_PARTS = ['roster', 'advisor', 'lifecycle', 'result', 'contracts', 'subfleet', 'tabs', 'fleetmux',
             'escalations', 'waiting', 'notices', 'vetoes']  # Fleet.tick at 26392b9, in order
CHANNEL = {'type': 'telegram', 'account': 'bot-1', 'push': list(channels.DEFAULT_PUSH)}
SERVICE = 'fx:service'
HEX32 = re.compile(r'[0-9a-f]{32}')


@pytest.fixture(autouse=True)
def fake_type():
    undo = channels.register('fake', FakeChannel, 'fake')
    yield
    undo()


# ---------------------------------------------------------------- helpers
class Crash(BaseException):
    """A process-level stop in the middle of a tick (not an ordinary exception)."""


class FakeFS:
    """In-memory stand-in for stop.FS (Z7). Records every call; ``fail`` = {op: n} raises OSError on the n-th call of
    that op; ``hooks['before_replace']`` runs after the temp file is written and before it replaces the target."""

    def __init__(self, files=None, fail=None, hooks=None):
        self.files = dict(files or {})
        self.fail = dict(fail or {})
        self.hooks = dict(hooks or {})
        self.calls, self.counts, self.locked = [], {}, False

    def _op(self, op, path, mode=None):
        self.calls.append((op, str(path), mode))
        self.counts[op] = self.counts.get(op, 0) + 1
        if self.fail.get(op) == self.counts[op]:
            raise OSError('fake ' + op + ' failure')

    def read(self, path):
        self._op('read', path)
        return self.files.get(str(path))

    def write_atomic(self, path, data, mode=0o600):
        self._op('write_atomic', path, mode)
        if 'before_replace' in self.hooks:
            self.hooks.pop('before_replace')()
        self.files[str(path)] = data

    def rename(self, src, dst):
        self._op('rename', src)
        if str(src) not in self.files:
            raise FileNotFoundError(str(src))
        self.files[str(dst)] = self.files.pop(str(src))

    def unlink(self, path):
        self._op('unlink', path)
        self.files.pop(str(path), None)

    def glob(self, pattern):
        self._op('glob', pattern)
        prefix = str(pattern).rstrip('*')
        return sorted(p for p in self.files if p.startswith(prefix))

    def lock(self, path):
        fs = self

        class _Lock:
            def __enter__(self):
                fs._op('acquire', path)
                assert not fs.locked, 'writers must be mutually exclusive'
                fs.locked = True

            def __exit__(self, *exc):
                fs.locked = False
                fs._op('release', path)
        return _Lock()


class Flaky:
    """Redis proxy: rule(method, args) -> None | 'before' (raise, do not run) | 'after' (run, then raise)."""

    def __init__(self, r, rule):
        self._r, self._rule, self.calls = r, rule, []

    def __getattr__(self, name):
        real = getattr(self._r, name)
        if not callable(real):
            return real

        def call(*args, **kwargs):
            self.calls.append(name)
            when = self._rule(name, args)
            if when == 'before':
                raise redis.exceptions.ConnectionError('fake: connection refused')
            value = real(*args, **kwargs)
            if when == 'after':
                raise redis.exceptions.ConnectionError('fake: connection lost after send')
            return value
        return call


def policy(level='interrupt'):
    pol, errors = security.parse_security('')
    assert errors == [] and pol is not None
    pol = copy.deepcopy(pol)
    pol['stop']['level'] = level
    return pol


def arm(f, level='interrupt'):
    f.cfg['security'] = policy(level)
    return f.cfg


def with_level(cfg, level):
    other = copy.deepcopy(cfg)
    other['security'] = policy(level)
    return other


def env(sender='ou_test_user', text='/stop 别动了', mid='1'):
    return envelope.make('fake', 'contract-1', conversation='c-1', message_id=mid, sender=sender, text=text)


def cmd_of(e):
    return envelope.parse_command(e['text'])


def audit_to(f):
    return lambda event_name, **fields: f.v2.audit(event_name, **fields)


def stop_audits(f, stage=None):
    rows = audits(f, 'security.stop')
    return [a for a in rows if stage is None or a.get('stage') == stage]


def listed(value):
    return json.loads(value) if isinstance(value, str) else value


def resume_soft(f):
    return '恢复派工请运行 fleetmux resume --config ' + str(Path(f.cfg['run']) / 'config.json') + '。'


RESUME_KILL = '本 run 已显式结束，不能用 start --resume 恢复；要继续请重新 start。'


def pane(f, slot):
    return f.agent(slot)['pane']


def busy(f, *slots, state='busy'):
    for slot in slots:
        f.setf(slot, state=state)


def flag_path(f):
    return str(Path(f.cfg['run']) / 'state' / 'stop.flag')


def security_tick(f):
    return getattr(f, 'security_tick')()  # absent until P8m phase B: AttributeError -> FAILED, never ERROR


def snapshot(r):
    kind = r.type(channels.STOP)
    return (r.hgetall(SERVICE), kind, r.hgetall(channels.STOP) if kind == 'hash' else r.get(channels.STOP),
            sorted(r.keys(channels.STOP_REQ + '*')))


def restart(f):
    """start --resume: a new daemon over the same run (tests/test_advisor_inherit.py:126-131 pattern)."""
    fleet.save(f.path, f.cfg)
    f.v2.close()
    f.mgr.dock_control = lambda p: None
    again = fleet.Fleet(f.path, f.r, f.mgr)
    again.resume_requested = True
    return again


# ================================================================ actor side
def test_stop_pauses_service_hash_directly_with_reason_and_sets_flag(fx, monkeypatch):
    f = fx
    fs = FakeFS()
    monkeypatch.setattr(stop, 'FS', fs)
    arm(f, 'interrupt')
    at0, t0 = f.r.hget(SERVICE, 'at'), time.time()
    text = stop.handle(f.r, f.cfg, env(), cmd_of(env()), audit=audit_to(f))
    svc = f.r.hgetall(SERVICE)
    assert svc['paused'] == '1'
    assert svc['reason'].startswith('channel stop by fake:contract-1:ou_t') and '别动了' in svc['reason']
    assert svc['at'] != at0 and float(svc['at']) >= t0 - 1
    assert svc['stop_epoch'] == '1'                       # Z1: the epoch is a field of fx:service
    assert f.r.keys('fx:channel:stop_epoch') == []        # never a separate key
    assert f.r.type(channels.STOP) == 'hash'              # Z2
    rec = f.r.hgetall(channels.STOP)
    assert {'level', 'by', 'at', 'id', 'reason', 'epoch', 'rank'} <= set(rec)
    assert rec['level'] == 'interrupt' and rec['by'] == 'fake:contract-1:ou_t' and HEX32.fullmatch(rec['id'])
    assert rec['epoch'] == '1' and rec['rank'] == '2' and f.r.ttl(channels.STOP) == -1
    req = f.r.hgetall(channels.STOP_REQ + rec['id'])     # Z3
    assert req['epoch'] == '1' and 0 < f.r.ttl(channels.STOP_REQ + rec['id']) <= 604800
    rows = stop_audits(f, 'actor')
    assert len(rows) == 1 and rows[0]['id'] == rec['id'] and rows[0]['level'] == 'interrupt' and rows[0]['epoch'] == '1'
    assert 'ou_test_user' not in json.dumps(rows[0], ensure_ascii=False)  # sender: first 4 characters only
    assert text == envelope.receipt('stopped', level='interrupt', resume=resume_soft(f))
    assert fs.calls == []
    f.tick()                                              # P8m never resumes
    assert f.r.hget(SERVICE, 'paused') == '1'

    # two /stop before the daemon handles them: the stronger level is kept, id / epoch move to the newest
    f.r.delete(channels.STOP)
    stop.handle(f.r, with_level(f.cfg, 'kill'), env(mid='2'), cmd_of(env()), audit=audit_to(f))
    stop.handle(f.r, with_level(f.cfg, 'pause'), env(mid='3', sender='ou_other'), cmd_of(env()), audit=audit_to(f))
    rec = f.r.hgetall(channels.STOP)
    assert rec['level'] == 'kill' and rec['rank'] == '3'
    assert rec['epoch'] == f.r.hget(SERVICE, 'stop_epoch') == '3'
    assert rec['by'] == 'fake:contract-1:ou_o' and f.r.hget(channels.STOP_REQ + rec['id'], 'level') == 'pause'

    # the same request id published again: dup, nothing written
    req = {'id': rec['id'], 'level': 'pause', 'by': rec['by'], 'at': rec['at'], 'reason': rec['reason']}
    before = snapshot(f.r)
    assert stop.publish(f.r, req) == ('dup', 3)
    assert snapshot(f.r) == before

    # preflight: every bad type / input is rejected before any write (Z5, X6 fail closed)
    good = {'id': 'a' * 32, 'level': 'pause', 'by': 'fake:contract-1:ou_t', 'at': time.time(), 'reason': 'x'}
    cases = [('stop_epoch', lambda: f.r.hset(SERVICE, 'stop_epoch', 'x'), {}),
             ('stop_epoch', lambda: f.r.hset(SERVICE, 'stop_epoch', '007'), {}),
             ('stop_epoch', lambda: f.r.hset(SERVICE, 'stop_epoch', str(2 ** 53)), {}),
             ('type:fx:channel:stop', lambda: (f.r.delete(channels.STOP), f.r.set(channels.STOP, 'x')), {}),
             ('stop.rank', lambda: f.r.hset(channels.STOP, mapping=dict(rec, rank='9')), {}),
             ('level', lambda: None, {'level': 'halt'}),
             ('id', lambda: None, {'id': 'zz'}),
             ('at', lambda: None, {'at': math.nan})]
    for what, setup, bad in cases:
        f.r.hset(SERVICE, 'stop_epoch', '3')
        f.r.delete(channels.STOP)
        f.r.hset(channels.STOP, mapping=rec)
        setup()
        before = snapshot(f.r)
        assert stop.publish(f.r, dict(good, **bad)) == ('invalid', what)
        assert snapshot(f.r) == before, what
    f.r.hset(SERVICE, 'stop_epoch', 'x')
    text = stop.handle(f.r, f.cfg, env(mid='4'), cmd_of(env()), audit=audit_to(f))
    assert text.startswith('Fleet：停止没有生效：fleet 状态异常（') and '请在电脑处理' in text
    assert [c for c in fs.calls if c[0] in ('acquire', 'write_atomic')] == []  # a rejected publish writes no flag (test-contract M98)


def test_stop_receipt_contains_resume_command_and_never_reaches_supervisor(fx, monkeypatch):
    from fleetmux.channels import actor
    f = fx
    arm(f, 'interrupt')
    posted, submitted, replies = [], [], []
    monkeypatch.setattr(runtime_v2, 'post_event', lambda *a, **k: posted.append(a))
    audit = audit_to(f)
    actor.handle_inbound(f.r, env(), submit=lambda *a, **k: submitted.append(a), reply=lambda _e, t: replies.append(t),
                         policy=f.cfg['security'], stop=functools.partial(stop.handle, f.r, f.cfg, audit=audit),
                         audit=audit)
    assert replies == [envelope.receipt('stopped', level='interrupt', resume=resume_soft(f))]
    for word in ('start --resume', '全部', '整棵', '子 fleet'):
        assert word not in replies[0]
    assert submitted == [] and posted == []               # never an operator_input, never the daemon's event queue
    f.r.delete(channels.STOP)
    replies.clear()
    actor.handle_inbound(f.r, env(mid='2'), submit=lambda *a, **k: submitted.append(a), reply=lambda _e, t: replies.append(t),
                         policy=None, stop=None, audit=audit)
    assert replies == [envelope.receipt('unknown_command', command='stop')]
    assert f.r.exists(channels.STOP) == 0 and submitted == [] and posted == []


def test_stop_does_not_wait_for_daemon(fx, monkeypatch):
    f = fx
    arm(f, 'interrupt')
    counts = {'step': 0, 'tick': 0, 'pump': 0}
    for name, owner in (('step', f.v2), ('tick', f), ('pump', f)):
        monkeypatch.setattr(owner, name, lambda *a, _n=name, **k: counts.__setitem__(_n, counts[_n] + 1))
    started = time.monotonic()
    stop.handle(f.r, f.cfg, env(), cmd_of(env()), audit=audit_to(f))
    assert time.monotonic() - started < 1.0
    assert f.r.hget(SERVICE, 'paused') == '1' and counts == {'step': 0, 'tick': 0, 'pump': 0}
    published = {k: f.r.hget(SERVICE, k) for k in ('paused', 'reason', 'at')}

    # the shared pause helper (Z9): same three fields as Fleet.pause, no audit, never touches the epoch
    epoch, n_audit = f.r.hget(SERVICE, 'stop_epoch'), f.r.xlen(f.v2.bus.audit_key)
    stop.pause_service(f.r, 'security limit: x', now=123.0)
    assert {k: f.r.hget(SERVICE, k) for k in ('paused', 'reason', 'at')} == {
        'paused': '1', 'reason': 'security limit: x', 'at': '123.0'}
    assert set(published) == {'paused', 'reason', 'at'}
    assert f.r.hget(SERVICE, 'stop_epoch') == epoch and f.r.xlen(f.v2.bus.audit_key) == n_audit
    with pytest.raises(redis.exceptions.ConnectionError):
        stop.pause_service(Flaky(f.r, lambda m, a: 'before'), 'x')


# ================================================================ daemon side
def test_security_tick_interrupt_escapes_each_busy_worker_once_not_supervisor(fx, monkeypatch):
    f = fx
    arm(f, 'interrupt')
    busy(f, 'worker-1', 'supervisor')
    busy(f, 'worker-2', state='starting')
    p1, p2 = pane(f, 'worker-1'), pane(f, 'worker-2')
    stop.handle(f.r, f.cfg, env(), cmd_of(env()), audit=audit_to(f))
    f.mgr.keys = []
    security_tick(f)
    assert f.mgr.keys == [(p1, 'Escape'), (p2, 'Escape')]
    done = f.r.hgetall(channels.STOP_DONE + '1')
    assert done['worker-1'].startswith('sent:') and done['worker-2'].startswith('sent:')
    assert done.get('done') and done.get('audited') and 'supervisor' not in done
    assert f.r.exists(channels.STOP) == 0
    rows = stop_audits(f, 'daemon')
    assert len(rows) == 1 and listed(rows[0]['sent']) == ['worker-1', 'worker-2']
    assert listed(rows[0]['send_failed']) == [] and listed(rows[0]['unknown']) == []
    assert not f.stopping

    # the same epoch replayed: no second press
    f.r.hset(channels.STOP, mapping={'level': 'interrupt', 'by': 'fake:contract-1:ou_t', 'at': str(time.time()),
                                     'id': 'b' * 32, 'reason': 'channel stop by x: y', 'epoch': '1', 'rank': '2'})
    security_tick(f)
    assert f.mgr.keys == [(p1, 'Escape'), (p2, 'Escape')] and f.r.exists(channels.STOP) == 0
    assert len(stop_audits(f, 'daemon')) == 1

    # claimed, then the daemon dies before recording the result: reported unknown, never pressed again
    stop.handle(f.r, f.cfg, env(mid='2'), cmd_of(env()), audit=audit_to(f))
    real = f.mgr.send_key

    def crashing(p, k):
        if p == p2:
            raise Crash()
        return real(p, k)
    f.mgr.keys = []
    monkeypatch.setattr(f.mgr, 'send_key', crashing)
    with pytest.raises(Crash):
        security_tick(f)
    done = f.r.hgetall(channels.STOP_DONE + '2')
    assert done['worker-1'].startswith('sent:') and done['worker-2'].startswith('attempted:') and 'done' not in done
    assert f.r.exists(channels.STOP) == 1
    monkeypatch.setattr(f.mgr, 'send_key', real)
    f.mgr.keys = []
    security_tick(f)
    assert f.mgr.keys == []
    rows = stop_audits(f, 'daemon')
    assert listed(rows[-1]['sent']) == ['worker-1'] and listed(rows[-1]['unknown']) == ['worker-2']
    assert f.r.hget(channels.STOP_DONE + '2', 'worker-2').startswith('attempted:')

    # a /stop arriving while an epoch is processed is not deleted (RELEASE_LUA compares the epoch)
    stop.handle(f.r, f.cfg, env(mid='3'), cmd_of(env()), audit=audit_to(f))
    fired = []

    def press_and_stop_again(p, k):
        if not fired:
            fired.append(1)
            stop.handle(f.r, f.cfg, env(mid='4'), cmd_of(env()), audit=audit_to(f))
        return real(p, k)
    monkeypatch.setattr(f.mgr, 'send_key', press_and_stop_again)
    security_tick(f)
    assert f.r.hget(channels.STOP, 'epoch') == '4' and f.r.hget(channels.STOP_DONE + '3', 'done')
    f.mgr.keys = []
    security_tick(f)
    assert f.mgr.keys == [(p1, 'Escape'), (p2, 'Escape')] and f.r.exists(channels.STOP) == 0


def test_security_tick_pause_level_presses_nothing(fx, monkeypatch):
    f = fx
    arm(f, 'pause')
    busy(f, 'worker-1', 'worker-2')
    text = stop.handle(f.r, f.cfg, env(), cmd_of(env()), audit=audit_to(f))
    assert 'resume --config' in text
    f.mgr.keys = []
    security_tick(f)
    assert f.mgr.keys == [] and f.r.hget(SERVICE, 'paused') == '1' and f.r.exists(channels.STOP) == 0
    row = stop_audits(f, 'daemon')[-1]
    assert row['level'] == 'pause' and listed(row['sent']) == [] and listed(row['unknown']) == []
    assert not f.stopping

    # stage isolation (survey appendix A.3): the daemon audit fails once after `done`
    stop.handle(f.r, f.cfg, env(mid='2'), cmd_of(env()), audit=audit_to(f))
    epoch = f.r.hget(channels.STOP, 'epoch')
    real_audit, failed = f.v2.audit, []

    def audit_once_broken(name, **fields):
        if name == 'security.stop' and fields.get('stage') == 'daemon' and not failed:
            failed.append(1)
            raise redis.exceptions.ConnectionError('fake audit write failure')
        return real_audit(name, **fields)
    monkeypatch.setattr(f.v2, 'audit', audit_once_broken)
    spy = []
    with pytest.raises(stop.SecurityTickError) as info:
        stop.run_stages(f, (('stop', stop.daemon_tick), ('spy', lambda g: spy.append(1))))
    assert spy == [1]                                    # the later stage still ran in this tick
    assert [n for n, _ in info.value.errors] == ['stop'] and 'ConnectionError' in info.value.errors[0][1]
    done = f.r.hgetall(channels.STOP_DONE + epoch)
    assert done.get('done') and not done.get('audited') and f.r.exists(channels.STOP) == 1
    security_tick(f)
    replayed = [a for a in stop_audits(f, 'daemon') if a.get('epoch') == epoch]
    assert len(replayed) == 1 and replayed[0].get('replayed') == '1'
    assert f.r.hget(channels.STOP_DONE + epoch, 'audited') and f.r.exists(channels.STOP) == 0 and f.mgr.keys == []
    monkeypatch.setattr(stop, 'daemon_tick', lambda g, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    n_err = len(audits(f, 'tick.error'))
    f.tick()
    errs = [a for a in audits(f, 'tick.error')[n_err:] if a.get('part') == 'security']
    assert len(errs) == 1 and 'stop=' in errs[0]['error']
    monkeypatch.undo()

    # #17: start --resume keeps a /stop pause (policy present)
    stop.handle(f.r, f.cfg, env(mid='3'), cmd_of(env()), audit=audit_to(f))   # ① fx:channel:stop pending
    again = restart(f)
    try:
        again.v2.resume_run()
        assert again.r.hget(SERVICE, 'paused') == '1'
        assert again.r.hget(SERVICE, 'reason').startswith('channel stop by ')
        kept = stop_audits(again, 'resume')
        assert len(kept) == 1 and kept[0]['kept'] == '1' and kept[0]['level'] == 'pause'
        assert kept[0]['epoch'] == again.r.hget(SERVICE, 'stop_epoch')
        assert len(audits(again, 'daemon.resumed')) >= 1
    finally:
        again.v2.close()
    again.r.delete(channels.STOP)                                          # ② only the reason is left
    again2 = fleet.Fleet(again.path, again.r, again.mgr)
    again2.resume_requested = True
    try:
        again2.v2.resume_run()
        assert again2.r.hget(SERVICE, 'paused') == '1' and len(stop_audits(again2, 'resume')) == 2
        event(again2, dict(kind='resume', request_id='res-1'))             # ③ the operator's CLI resume still works
        assert again2.r.hget(SERVICE, 'paused') == '0'
    finally:
        again2.v2.close()
    again3 = fleet.Fleet(again2.path, again2.r, again2.mgr)
    again3.resume_requested = True
    calls = []
    real_resume = again3.resume
    again3.resume = lambda: (calls.append(1), real_resume())
    try:
        again3.v2.resume_run()
        assert calls == [1] and len(stop_audits(again3, 'resume')) == 2
    finally:
        again3.v2.close()

    # ④ no policy: byte-identical to today, even with an operator reason that starts like a /stop
    again3.cfg.pop('security', None)
    fleet.save(again3.path, again3.cfg)
    again3.r.hset(SERVICE, mapping={'paused': 1, 'reason': 'channel stop by x', 'at': time.time()})
    again4 = fleet.Fleet(again3.path, again3.r, again3.mgr)
    again4.resume_requested = True
    try:
        n_sec = len([1 for _, x in again4.r.xrange(again4.v2.bus.audit_key)
                     if str(x.get('event', '')).startswith('security.')])
        recorder = Flaky(again4.r, lambda m, a: None)
        real_r, again4.r = again4.r, recorder
        try:
            assert getattr(again4, 'security_keeps_pause')() is False
        finally:
            again4.r = real_r
        assert recorder.calls == []                       # returned before any Redis command
        calls = []
        real_resume = again4.resume
        again4.resume = lambda: (calls.append(1), real_resume())
        again4.v2.resume_run()
        assert calls == [1] and again4.r.hget(SERVICE, 'paused') == '0'
        assert len([1 for _, x in again4.r.xrange(again4.v2.bus.audit_key)
                    if str(x.get('event', '')).startswith('security.')]) == n_sec
    finally:
        again4.v2.close()
        f.v2 = again4.v2                                  # the fixture's teardown closes this one again (idempotent)


def test_security_tick_kill_level_sets_stopping_and_run_closed_enqueued(fx, monkeypatch):
    f = fx
    arm(f, 'kill')
    f.cfg['channels'] = [dict(CHANNEL)]
    busy(f, 'worker-1', 'worker-2')
    p1, p2 = pane(f, 'worker-1'), pane(f, 'worker-2')
    text = stop.handle(f.r, f.cfg, env(), cmd_of(env()), audit=audit_to(f))
    assert text == envelope.receipt('stopped', level='kill', resume=RESUME_KILL)
    assert 'resume --config' not in text
    children = []
    monkeypatch.setattr(subfleet, 'stop_children', lambda g: children.append(1) or True)
    seen = []
    f.mgr.keys = []
    stop.run_stages(f, (('stop', stop.daemon_tick), ('spy', lambda g: seen.append((g.stopping, g.explicit_stop)))))
    assert f.mgr.keys == [(p1, 'Escape'), (p2, 'Escape')]
    assert f.stopping is True and f.explicit_stop is True
    assert seen == [(True, True)]                         # the mirror stage still runs in the kill tick
    assert f.r.hget(SERVICE, 'reason').startswith('channel stop by ')   # finish() did not rewrite it
    assert children == []                                 # daemon_tick never touches sub-fleets
    monkeypatch.undo()
    stop_tail(f)
    items = [json.loads(x) for x in f.r.lrange(channels.OUTQ, 0, -1)]
    closed = [i for i in items if i.get('event') == 'run_closed']
    assert len(closed) == 1 and closed[0].get('target') == 'last'
    assert f.r.exists(channels.PUSHED + 'run_closed:' + f.v2.bus.run_id) == 1


def test_adopted_or_foreign_pane_not_escaped(fx):
    f = fx
    arm(f, 'interrupt')
    busy(f, 'worker-1', 'worker-2', 'supervisor')
    p1, p2 = pane(f, 'worker-1'), pane(f, 'worker-2')
    f.mgr.owns = lambda p: p != p2
    f.cfg['adopt'] = True
    f.setf('advisor', state='busy', pane='pane-adv')
    stop.handle(f.r, f.cfg, env(), cmd_of(env()), audit=audit_to(f))
    f.mgr.keys = []
    security_tick(f)
    assert f.mgr.keys == [(p1, 'Escape')]
    row = stop_audits(f, 'daemon')[-1]
    skipped = listed(row['skipped'])
    assert any(s['slot'] == 'worker-2' and 'registry' in s['why'] for s in skipped)
    assert 'worker-2' not in f.r.hgetall(channels.STOP_DONE + '1')
    assert all(k[0] not in (pane(f, 'supervisor'), 'pane-adv') for k in f.mgr.keys)


def test_stop_redis_down_replies_stop_failed_and_writes_flag_file(fx, monkeypatch, tmp_path):
    f = fx
    arm(f, 'interrupt')
    fs = FakeFS()
    monkeypatch.setattr(stop, 'FS', fs)
    flag, lock = flag_path(f), flag_path(f) + '.lock'

    # ping fails: certainly not sent -> stop_failed, flag written under the writers' lock
    down = Flaky(f.r, lambda m, a: 'before' if m == 'ping' else None)
    text = stop.handle(down, f.cfg, env(), cmd_of(env()), audit=audit_to(f))
    assert text == envelope.receipt('stop_failed', reason='Redis 不通')
    ops = [(op, p) for op, p, _ in fs.calls]
    assert ops.index(('acquire', lock)) < ops.index(('write_atomic', flag)) < ops.index(('release', lock))
    assert [m for op, p, m in fs.calls if op == 'write_atomic'] == [0o600]
    assert flag.startswith(f.cfg['run']) and not flag.startswith(str(channels.config_dir()))
    reqs = json.loads(fs.files[flag])['requests']
    assert len(reqs) == 1 and {'id', 'level', 'by', 'at', 'reason'} <= set(reqs[0])
    assert f.r.hget(SERVICE, 'paused') != '1' and f.r.hget(SERVICE, 'stop_epoch') is None
    assert f.r.exists(channels.STOP) == 0

    # EVAL sent, then the connection broke: maybe applied -> never claims "not effective"
    after = Flaky(f.r, lambda m, a: 'after' if m in ('eval', 'evalsha') else None)
    text = stop.handle(after, f.cfg, env(mid='2'), cmd_of(env()), audit=audit_to(f))
    assert '无法确认停止是否生效' in text and '已写停止标记' in text and '停止没有生效' not in text
    assert f.r.hget(SERVICE, 'paused') == '1' and f.r.hget(SERVICE, 'stop_epoch') == '1'
    assert len(json.loads(fs.files[flag])['requests']) == 2

    # the flag could not be written either
    fs.fail = {'write_atomic': fs.counts.get('write_atomic', 0) + 1}
    before = fs.files[flag]
    text = stop.handle(after, f.cfg, env(mid='3'), cmd_of(env()), audit=audit_to(f))
    assert text == envelope.receipt('invalid_command', reason='无法确认停止是否生效，停止标记未写成，请在电脑处理'
                                    '（fleetmux pause --config ' + str(Path(f.cfg['run']) / 'config.json') + '）')
    assert fs.files[flag] == before

    # the daemon claims by rename, publishes every request (ids dedupe), acknowledges by unlinking the claim
    busy(f, 'worker-1', 'worker-2')
    f.mgr.keys = []
    fs.fail = {'unlink': fs.counts.get('unlink', 0) + 1}
    security_tick(f)
    assert f.r.hget(SERVICE, 'paused') == '1' and f.r.hget(SERVICE, 'stop_epoch') == '3'  # the 'after' one was a dup; the ping-fail one is new (test-contract M98)
    assert sorted(k[0] for k in f.mgr.keys) == sorted([pane(f, 'worker-1'), pane(f, 'worker-2')])
    assert any(a.get('source') == 'flag' for a in stop_audits(f))
    assert [p for op, p, _ in fs.calls if op == 'unlink' and p == flag] == []  # never unlinks the original name
    claims = [p for p in fs.files if '.claim-' in p]
    assert len(claims) == 1                               # the unlink failed: the claim stays
    pressed = list(f.mgr.keys)
    security_tick(f)
    assert [p for p in fs.files if '.claim-' in p] == [] and f.mgr.keys == pressed
    assert f.r.hget(SERVICE, 'stop_epoch') == '3'  # test-contract M98

    # a writer appends while the daemon claims between its read and its replace: nothing is lost
    fs.files[flag] = json.dumps({'requests': [dict(reqs[0], id='c' * 32)]})
    fs.hooks['before_replace'] = lambda: stop.daemon_tick(f, fs=fs)
    stop.handle(down, f.cfg, env(mid='5'), cmd_of(env()), audit=audit_to(f))
    merged = json.loads(fs.files[flag])['requests']
    assert {'c' * 32} < {q['id'] for q in merged}         # A and B both in the new original-name file
    security_tick(f)
    assert flag not in fs.files and [p for p in fs.files if '.claim-' in p] == []

    # a claim left by a crash is replayed first
    fs.files[flag] = json.dumps({'requests': [dict(reqs[0], id='d' * 32)]})
    real_r = f.r
    f.r = Flaky(real_r, lambda m, a: 'before' if m in ('eval', 'evalsha') else None)
    with pytest.raises(Exception):
        stop.daemon_tick(f, fs=fs)
    f.r = real_r
    assert flag not in fs.files and len([p for p in fs.files if '.claim-' in p]) == 1
    security_tick(f)
    assert [p for p in fs.files if '.claim-' in p] == []

    # an unreadable flag still stops the fleet; the same bytes again are a dup
    epoch = int(f.r.hget(SERVICE, 'stop_epoch'))
    fs.files[flag] = 'not json'
    security_tick(f)
    assert f.r.hget(SERVICE, 'paused') == '1' and int(f.r.hget(SERVICE, 'stop_epoch')) == epoch + 1
    assert any('.bad-' in p for p in fs.files)
    fs.files[flag] = 'not json'
    security_tick(f)
    assert int(f.r.hget(SERVICE, 'stop_epoch')) == epoch + 1

    # only this run's flag is ever read
    monkeypatch.setenv('FLEETMUX_CONFIG_DIR', str(tmp_path / 'cfgdir'))
    other = str(tmp_path / 'other-run' / 'state' / 'stop.flag')
    fs.files[other] = fs.files[str(channels.config_dir() / 'state' / 'stop.flag')] = json.dumps(
        {'requests': [dict(reqs[0], id='e' * 32)]})
    fs.calls.clear()
    f.mgr.keys = []
    security_tick(f)
    assert f.mgr.keys == []
    assert all(p.startswith(str(Path(f.cfg['run']) / 'state')) for _, p, _ in fs.calls)


def test_tick_table_appends_security_after_channels(monkeypatch):
    assert hasattr(fleet.Fleet, 'security_tick')        # P8m phase B adds it (no fleet.py stub at A2')
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
                       ('channel_tick', 'channels'), ('security_tick', 'security')):
        setattr(f, attr, boom(name))
    monkeypatch.setattr(lifecycle_close, 'tick', boom('lifecycle'))
    monkeypatch.setattr(subfleet, 'tick', boom('subfleet'))
    monkeypatch.setattr(tabs, 'open_all', boom('tabs'))
    monkeypatch.setattr(fleetmux_sidebar, 'tick', boom('fleetmux'))

    class Stop(BaseException):
        pass

    def stop_here(_f):
        raise Stop()
    monkeypatch.setattr(subfleet, 'follow_jump', stop_here)
    with pytest.raises(Stop):
        fleet.Fleet.tick(f)
    errors = [d for kind, d in logged if kind == 'tick.error']
    for d in errors:
        assert 'segment ' + d['part'] in d['error'], d
    parts = [d['part'] for d in errors]
    assert parts[:12] == OLD_PARTS
    assert parts[12:14] == ['channels', 'security'] and parts.count('security') == 1


def test_no_policy_security_tick_is_noop(fx, monkeypatch):  # [基线]
    f = fx
    f.channel_spawn = lambda argv: types.SimpleNamespace(pid=0, poll=lambda: None, terminate=lambda: None, kill=lambda: None, wait=lambda timeout=None: 0)  # test-contract M98: no real 'channel actor' child
    fs = FakeFS()
    monkeypatch.setattr(stop, 'FS', fs)
    f.cfg.pop('security', None)
    n_err = len(audits(f, 'tick.error'))
    for chans in (None, [dict(CHANNEL)]):
        if chans:
            f.cfg['channels'] = chans
        else:
            f.cfg.pop('channels', None)
        f.mgr.keys = []
        for _ in range(3):
            f.tick()
        assert [k for k in f.mgr.keys if k[1] == 'Escape'] == []
    assert [x for _, x in f.r.xrange(f.v2.bus.audit_key) if str(x.get('event', '')).startswith('security.')] == []
    assert [a for a in audits(f, 'tick.error')[n_err:] if a.get('part') == 'security'] == []
    assert f.r.keys('fx:channel:stop*') == [] and f.r.hget(SERVICE, 'stop_epoch') is None
    assert stop.daemon_tick(f) is None and stop.keep_pause_on_resume(f) is False
    text = stop.handle(f.r, f.cfg, env(text='/stop x'), envelope.parse_command('/stop x'), audit=audit_to(f))
    assert text == envelope.receipt('unknown_command', command='stop')
    assert f.r.hget(SERVICE, 'paused') != '1'
    assert f.r.keys('fx:channel:stop*') == [] and f.r.hget(SERVICE, 'stop_epoch') is None
    assert fs.calls == []


# ================================================================ actor binding (M108 / M115, appended)
class _OneRoundChannel:
    """A fake account for actor.inbound_loop: one poll round that hands one envelope to accept()."""
    account = type('Account', (), {'name': 'contract-1'})()  # before `type` below shadows the builtin
    type = 'fake'
    last_error = ''

    def __init__(self, e):
        self.e = e

    def poll(self, timeout, accept):
        accept(self.e)
        return 1

    def health(self):
        return {'state': 'ok'}


def _one_inbound_round(f, e):
    from fleetmux.channels import actor
    replies, submitted, rounds = [], [], iter([False, True])
    actor.inbound_loop(_OneRoundChannel(e), r=f.r, cfg=f.cfg, submit=lambda *a, **k: submitted.append(a),
                       reply=lambda _e, t: replies.append(t), sleep=lambda s: None, stop=lambda: next(rounds, True))
    return replies, submitted


def test_inbound_loop_binds_stop_only_with_policy(fx):
    """Z9: the real inbound_loop hands handle_inbound stop=stop.handle (bound to r, cfg, audit) only when the run has
    a policy; without one /stop stays unknown_command and nothing is written."""
    f = fx
    f.cfg['security'] = policy('interrupt')
    replies, submitted = _one_inbound_round(f, env(mid='1'))
    assert replies == [envelope.receipt('stopped', level='interrupt', resume=resume_soft(f))], replies
    assert f.r.hget(SERVICE, 'paused') == '1' and f.r.hget(channels.STOP, 'level') == 'interrupt'
    assert submitted == []
    f.cfg.pop('security')
    f.r.delete(channels.STOP)
    replies, submitted = _one_inbound_round(f, env(mid='2'))
    assert replies == [envelope.receipt('unknown_command', command='stop')], replies
    assert f.r.exists(channels.STOP) == 0 and submitted == []
