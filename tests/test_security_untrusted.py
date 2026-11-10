"""P8d B3 untrusted-request marking + A4 stranger alerts (brief v2.2 §8.2 / §8.4; sec-1 test-plan §3.3,
survey §8.5 / §8.10 / §8.18; contracts §8 「sec-1 §8.18 三个函数的签名」, X10).

A2' red tests. Module level imports only P8a names and the P8d stub ``channels.strangers``; ``actor`` is imported
inside the bodies (FAILED, never ERROR). The ancestry check is always an injected fake ``verify`` (the real
``guard.verify_ancestry`` and its L1-L6 cases belong to sec-3). Private unix-socket Redis only; no network, no
credentials, no listener.

[基线] ``test_plain_user_typed_request_sets_no_key`` needs P1d (``actor.handle_inbound``): until P1d lands it is
dependency red (precondition P1d), reported as such; the assertion is not softened, skipped or xfailed.
"""
import json
import threading
import uuid

import pytest
import redis

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture: the repo's private test Redis)
from test_runtime_v2 import fx  # noqa: F401  (fixture: v2 service fleet, pi supervisor)
from test_channels_contract import Audit, FakeChannel, config_dir, fake_registered  # noqa: F401
from cmux_fleet import channels
from cmux_fleet.channels import envelope, security, strangers

ACCOUNT = 'contract-1'
REQ = 'fx:req:'


def policy_of(text=''):
    p, errors = security.parse_security(text)
    assert errors == [], errors
    return p


class Clock:
    def __init__(self, now=1762650000.0):
        self.now = now

    def __call__(self):
        return self.now


class FakeAncestry:
    """The shape of sec-3's guard.Ancestry that sec-1 reads: outcome + reason; truthiness is an error."""

    def __init__(self, outcome, reason=''):
        self.outcome, self.reason = outcome, reason

    def __bool__(self):
        raise TypeError('check Ancestry.outcome explicitly')


def verifier(outcome='verified', reason='', exc=None):
    calls = []

    def verify(cfg, **kw):
        calls.append(cfg)
        if exc is not None:
            raise exc
        return FakeAncestry(outcome, reason)
    verify.calls = calls
    return verify


class Proxy:
    """Wraps a Redis client; counts calls by name and can make chosen reads raise."""

    def __init__(self, r, fail=()):
        self._r, self._fail, self.counts = r, set(fail), {}

    def __getattr__(self, name):
        attr = getattr(self._r, name)
        if not callable(attr):
            return attr

        def call(*a, **k):
            self.counts[name] = self.counts.get(name, 0) + 1
            if name in self._fail:
                raise redis.ConnectionError('injected')
            return attr(*a, **k)
        return call


def spec_of(message=None, **payload):
    body = dict(payload)
    if message is not None:
        body['message'] = message
    return json.dumps({'id': 'ui-x', 'payload': body})


def outq(r):
    return [json.loads(x) for x in r.lrange(channels.OUTQ, 0, -1)]


def deliver(f, text, segs=None, pol=None):
    from cmux_fleet.channels import actor
    from test_channel_actor_inbound import fleet_submit, replies_sink
    env = envelope.make('fake', ACCOUNT, conversation='c-1', message_id=uuid.uuid4().hex[:12], sender='u-1',
                        text=text, reply_ref={'chat': 'c-1'}, provenance=segs)
    submit, reply = fleet_submit(actor, f), replies_sink()
    got = actor.handle_inbound(f.r, env, submit=submit, reply=reply, policy=pol)
    rid = submit.answers[-1]['id'] if submit.answers else None
    return got, rid, reply.texts


def security_audits(f):
    return [fl for _, fl in f.r.xrange(f.v2.bus.audit_key) if str(fl.get('event', '')).startswith('security.')]


# ---------------------------------------------------------------------------------------------- 1
def test_accepted_untrusted_request_sets_key_and_audits_and_receipt_has_notice(fx, fake_registered):
    from test_channel_actor_inbound import events
    f, p = fx, policy_of()
    segs = [{'kind': 'forwarded', 'text': '转来的 UNTRUSTED-BODY'}, {'kind': 'quoted', 'text': '引的一句'}]
    got, rid, texts = deliver(f, '看看', segs, pol=p)
    assert got == 'persisted' and rid.startswith('ui-')
    key = channels.UNTRUSTED + rid
    assert f.r.get(key) == 'forwarded,quoted' and 0 < f.r.ttl(key) <= 7 * 86400
    rows = [r for r in security_audits(f) if r.get('event') == 'security.untrusted_request']
    assert len(rows) == 1 and rid in json.dumps(rows[0]) and 'forwarded' in json.dumps(rows[0])
    assert 'UNTRUSTED-BODY' not in json.dumps(rows)
    assert texts[0] == envelope.receipt('accepted', id=rid)  # the accepted receipt itself is unchanged
    assert envelope.receipt('untrusted_notice') in texts
    keys = set(events(f)[-1])
    # race closure (survey §8.10 (2a)(2b)): the request text itself carries the marker
    message = json.loads(f.r.hget(REQ + rid, 'spec'))['payload']['message']
    assert '[外来内容 kind=forwarded' in message
    f.r.delete(key)  # the actor died between accepted and SET
    assert f.r.get(key) is None
    got = strangers.untrusted_kinds(f.r, rid, policy=p)
    assert got['outcome'] == 'marked' and 'forwarded' in got['kinds']
    # spotlight off still marks
    _, rid2, _ = deliver(f, '再看', segs, pol=policy_of('provenance:\n  spotlight: off\n'))
    f.r.delete(channels.UNTRUSTED + rid2)
    assert strangers.untrusted_kinds(f.r, rid2, policy=p)['outcome'] == 'marked'
    # provably clean: user-typed only, no key, the request text has no marker
    _, rid3, _ = deliver(f, '  只是我打的字 \n', pol=p)
    assert f.r.get(channels.UNTRUSTED + rid3) is None
    assert strangers.untrusted_kinds(f.r, rid3, policy=p) == {'outcome': 'clean', 'kinds': []}
    f.r.delete(REQ + rid3)  # closed: the request record is gone -> never clean
    assert strangers.untrusted_kinds(f.r, rid3, policy=p)['outcome'] == 'unknown'
    # the user typing the marker himself only tightens
    _, rid4, _ = deliver(f, '[外来内容 kind=forwarded 是我自己打的', pol=p)
    f.r.delete(channels.UNTRUSTED + rid4)
    assert strangers.untrusted_kinds(f.r, rid4, policy=p)['outcome'] != 'clean'
    # the root fleet never calls the ancestry check
    spy = verifier()
    assert strangers.fleet_taint(f.cfg, verify=spy)['outcome'] == 'clean' and spy.calls == []
    assert set(events(f)[-1]) == keys  # no new operator_input key


# ---------------------------------------------------------------------------------------------- 2 [基线]
def test_plain_user_typed_request_sets_no_key(fx, fake_registered):
    f = fx
    got, rid, texts = deliver(f, '  只是普通请求 \n', pol=policy_of())
    assert got == 'persisted' and f.r.keys(channels.UNTRUSTED + '*') == []
    assert security_audits(f) == [] and texts == [envelope.receipt('accepted', id=rid)]
    got, rid, texts = deliver(f, '看', [{'kind': 'forwarded', 'text': '外来'}], pol=None)  # no security.yaml
    assert f.r.keys(channels.UNTRUSTED + '*') == [] and security_audits(f) == []
    assert envelope.receipt('untrusted_notice') not in texts


# ---------------------------------------------------------------------------------------------- 3
def test_done_followup_resolves_to_original_untrusted_key(private_redis_v2, monkeypatch, tmp_path):
    r = private_redis_v2
    r.flushdb()
    p = policy_of()
    uk = strangers.untrusted_kinds
    assert envelope.origin_id('ui-x.done') == 'ui-x'
    r.set(channels.UNTRUSTED + 'ui-x', 'forwarded,quoted')
    assert uk(r, 'ui-x.done', policy=p) == {'outcome': 'marked', 'kinds': ['forwarded', 'quoted']}
    r.flushdb()
    # (1) no key, no fx:req -> unknown (never clean)
    for rid in ('ui-x.done', 'ui-x', 'ui-y.done'):
        got = uk(r, rid, policy=p)
        assert got['outcome'] == 'unknown' and 'unknown' in got['kinds']
    # (2) bad request shapes -> unknown
    for mapping in ({'status': 'queued'}, {'spec': '{not json'}, {'spec': '[1]'}, {'spec': '{}'},
                    {'spec': json.dumps({'payload': 'x'})}, {'spec': json.dumps({'payload': {}})},
                    {'spec': spec_of(5)}):
        r.delete(REQ + 'ui-x')
        r.hset(REQ + 'ui-x', mapping=mapping)
        assert uk(r, 'ui-x', policy=p)['outcome'] == 'unknown', mapping
    # (3) the one clean shape; markers found in the decoded message
    cases = [(spec_of('普通文字'), 'clean', []),
             (spec_of('看\n[外来内容 kind=forwarded name= 边界=abcd1234]\nx\n[/外来内容 abcd1234]'), 'marked',
              ['forwarded']),
             (json.dumps({'payload': {'message': '[外来内容 kind=forwarded 边界=abcd1234]'}}, ensure_ascii=True),
              'marked', ['forwarded'])]
    for spec, outcome, kinds in cases:
        r.delete(REQ + 'ui-x')
        r.hset(REQ + 'ui-x', 'spec', spec)
        assert uk(r, 'ui-x', policy=p) == {'outcome': outcome, 'kinds': kinds}
    for message in ('[外来内容 kind=pigeon 边界=abcd1234]', '[外来内容 没有 kind'):
        r.delete(REQ + 'ui-x')
        r.hset(REQ + 'ui-x', 'spec', spec_of(message))
        assert 'unknown' in uk(r, 'ui-x', policy=p)['kinds']
    r.delete(REQ + 'ui-x')
    r.hset(REQ + 'ui-x', 'spec', json.dumps({'payload': {'symbol': 'X'}}))  # not a channel request: conservative
    assert uk(r, 'ui-x', policy=p)['outcome'] == 'unknown'
    # (4) malformed fast-path values -> unknown, never []
    for value in ('', ',', 'pigeon'):
        r.set(channels.UNTRUSTED + 'ui-x', value)
        got = uk(r, 'ui-x', policy=p)
        assert got['outcome'] == 'unknown' and got['kinds'] and 'unknown' in got['kinds']
    r.flushdb()
    # (5) read errors are injected, not provoked
    r.hset(REQ + 'ui-x', 'spec', spec_of('普通文字'))
    for fail in (('get',), ('hget',), ('get', 'hget')):
        got = uk(Proxy(r, fail), 'ui-x', policy=p)
        assert got['outcome'] == 'unknown' and 'unknown' in got['kinds']
    # (6) no policy -> off
    assert uk(r, 'ui-x', policy=None) == {'outcome': 'off', 'kinds': []}
    # (8) fleet_taint, injected verify (the real chain check is sec-3's)
    root = {'run': str(tmp_path / 'root')}
    spy = verifier()
    assert strangers.fleet_taint(root, verify=spy)['outcome'] == 'clean' and spy.calls == []
    assert strangers.fleet_taint(dict(root, security_taint={'kinds': ['forwarded'], 'root_request': 'ui-x'}),
                                 verify=spy)['outcome'] == 'taint'
    bad = strangers.fleet_taint(dict(root, security_taint={'kinds': []}), verify=spy)
    assert bad['outcome'] == 'unknown' and bad['taint']['kinds'] == ['unknown']
    missing = str(tmp_path / 'nowhere' / 'config.json')
    child = {'run': str(tmp_path / 'child'), 'parent_config': missing, 'root_config': missing,
             'task_id': 'M1', 'adopt': {'slot': 'worker-1', 'generation': 1}}
    opened = []
    real_open = open
    monkeypatch.setattr('builtins.open', lambda f, *a, **k: (opened.append(str(f)), real_open(f, *a, **k))[1])
    ok = verifier('verified')
    assert strangers.fleet_taint(child, verify=ok)['outcome'] == 'clean' and len(ok.calls) == 1
    no = verifier('refused', 'not_delegated')
    got = strangers.fleet_taint(dict(child, security_taint={'kinds': ['forwarded'], 'root_request': 'ui-x'}),
                                verify=no)
    assert got['outcome'] == 'deny' and got['reason'] and 'not_delegated' in got['reason'] and len(no.calls) == 1
    assert strangers.fleet_taint(child, verify=verifier(exc=RuntimeError('boom')))['outcome'] == 'deny'
    # (7) taint_snapshot
    clock = Clock()
    snap = strangers.taint_snapshot
    got = snap(child, r, 'ui-x', clock=clock, verify=no)
    assert got['outcome'] == 'refuse' and got['taint'] is None and 'not_delegated' in got['reason']
    inherited = {'kinds': ['forwarded'], 'root_request': 'ui-0'}
    got = snap(dict(root, security_taint=inherited), r, 'ui-x', clock=clock, verify=spy)
    assert got['outcome'] == 'taint' and got['taint'] == inherited
    got = snap(dict(root, security_taint='junk'), r, 'ui-x', clock=clock, verify=spy)
    assert got['outcome'] == 'taint' and got['taint']['kinds'] == ['unknown']
    assert snap(root, r, 'ui-x', clock=clock, verify=spy)['outcome'] == 'none'  # no taint, no policy
    with_policy = dict(root, security=p)
    r.set(channels.UNTRUSTED + 'ui-x', 'forwarded')
    got = snap(with_policy, r, 'ui-x.done', clock=clock, verify=spy)
    assert got['outcome'] == 'taint' and got['taint']['root_request'] == 'ui-x' and got['taint']['kinds'] == ['forwarded']
    got = snap(with_policy, Proxy(r, ('get', 'hget')), 'ui-x', clock=clock, verify=spy)
    assert got['outcome'] == 'taint' and 'unknown' in got['taint']['kinds']
    r.delete(channels.UNTRUSTED + 'ui-x')
    r.hset(REQ + 'ui-z', 'spec', spec_of('普通文字'))
    assert snap(with_policy, r, 'ui-z', clock=clock, verify=spy)['outcome'] == 'none'
    for rid in ('', 'ui-a\nb'):
        got = snap(with_policy, r, rid, clock=clock, verify=spy)
        assert got['outcome'] == 'taint' and got['taint']['kinds'] == ['unknown']
    assert not any(missing in o for o in opened)  # never walks the chain itself
    # (9) the fast-path key: origin_id suffix, comma-separated kinds
    r.flushdb()
    strangers.mark_untrusted(r, 'ui-m', ['forwarded', 'quoted'], policy=p, audit=Audit())
    assert r.get(channels.UNTRUSTED + 'ui-m') == 'forwarded,quoted'
    assert uk(r, 'ui-m.done', policy=p) == {'outcome': 'marked', 'kinds': ['forwarded', 'quoted']}


# ---------------------------------------------------------------------------------------------- 4
def test_strangers_counter_alerts_once_after_threshold_within_window(private_redis_v2):
    r = private_redis_v2
    r.flushdb()
    p = policy_of()
    d = security.DEFAULTS['strangers']
    k1 = channels.STRANGERS + 'fake:' + ACCOUNT
    k2 = channels.PUSHED + 'security:strangers:fake:' + ACCOUNT
    clock, inner = Clock(), Audit()
    proxy = Proxy(r)
    w = strangers.watch(inner, r=proxy, policy=p, type_='fake', account=ACCOUNT, clock=clock)

    def drops(n, start=0, watcher=None):
        for i in range(start, start + n):
            (watcher or w)('channel.drop_policy', sender='stranger-%03d' % i, conversation='conv-secret-%03d' % i)

    drops(d['alert_after'])
    items = outq(r)
    assert len(items) == 1
    item = items[0]
    assert item['event'] == 'security' and item['user_only'] is True and item['target'] == 'last'
    assert ACCOUNT in item['text'] and str(d['alert_after']) in item['text'] and 'stra' in item['text']
    assert 'conv-secret' not in item['text']
    assert inner.events().count('security.stranger_alert') == 1
    assert proxy.counts.get('lpush', 0) == 0 and proxy.counts.get('rpush', 0) == 0  # queued inside the script only
    assert r.type(k1) == 'hash' and r.type(k2) == 'string' and r.ttl(k1) > 0
    drops(15, start=10)
    assert len(outq(r)) == 1  # cooldown
    second = strangers.watch(Audit(), r=r, policy=p, type_='fake', account=ACCOUNT, clock=clock)
    drops(d['alert_after'], start=40, watcher=second)
    assert len(outq(r)) == 1  # the cooldown holds across instances
    clock.now += d['cooldown_s']
    drops(d['alert_after'], start=60)
    assert len(outq(r)) == 2
    # concurrency: 8 watchers on their own clients, 20 drops -> exactly one alert
    r.flushdb()
    sock = r.connection_pool.connection_kwargs['path']
    audits = [Audit() for _ in range(8)]
    clock2 = Clock(1762660000.0)

    def worker(i):
        client = redis.Redis(unix_socket_path=sock, decode_responses=True)
        wi = strangers.watch(audits[i], r=client, policy=p, type_='fake', account=ACCOUNT, clock=clock2)
        for j in range(20 if i == 0 else 3):
            wi('channel.drop_policy', sender='s-%d-%d' % (i, j), conversation='c')
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join(10) for t in threads]
    assert len(outq(r)) == 1 and sum(a.events().count('security.stranger_alert') for a in audits) == 1
    # the real count is reported (window 7200, cooldown 60)
    r.flushdb()
    p2 = policy_of('strangers:\n  window_s: 7200\n  cooldown_s: 60\n  alert_after: 5\n')
    clock3 = Clock()
    w3 = strangers.watch(Audit(), r=r, policy=p2, type_='fake', account=ACCOUNT, clock=clock3)
    drops(5, watcher=w3)
    clock3.now += 61
    drops(1, start=5, watcher=w3)
    items = list(reversed(outq(r)))
    assert len(items) == 2 and '5' in items[0]['text'] and '6' in items[1]['text'] and '5 条' not in items[1]['text']
    assert all(envelope.outbound_problem(i) == '' for i in items)
    # wrong key types: no exception, zero writes, the inner audit still gets every row
    for k, prepare in ((k1, lambda: r.set(k1, 'x')), (k2, lambda: r.rpush(k2, 'x'))):
        r.flushdb()
        prepare()
        inner = Audit()
        wb = strangers.watch(inner, r=r, policy=p, type_='fake', account=ACCOUNT, clock=Clock())
        drops(d['alert_after'], watcher=wb)
        assert r.llen(channels.OUTQ) == 0
        assert r.keys('*') == [k]
        assert inner.events().count('channel.drop_policy') == d['alert_after']
        errors = [fl for e, fl in inner if e == 'channel.error']
        assert errors and errors[0].get('kind') == 'bad_config'


# ---------------------------------------------------------------------------------------------- 5
def test_strangers_window_resets_after_600s(private_redis_v2):
    r = private_redis_v2
    r.flushdb()
    p, clock, inner = policy_of(), Clock(), Audit()
    d = security.DEFAULTS['strangers']
    w = strangers.watch(inner, r=r, policy=p, type_='fake', account=ACCOUNT, clock=clock)
    for i in range(d['alert_after'] - 1):
        w('channel.drop_policy', sender='s-%d' % i, conversation='c')
    clock.now += d['window_s'] + 1
    w('channel.drop_policy', sender='s-last', conversation='c')
    assert r.llen(channels.OUTQ) == 0 and 'security.stranger_alert' not in inner.events()
    assert r.hget(channels.STRANGERS + 'fake:' + ACCOUNT, 'count') == '1'


# ---------------------------------------------------------------------------------------------- 6
def test_stranger_alert_never_replies_to_the_stranger(private_redis_v2, fake_registered, tmp_path):
    from cmux_fleet.channels import base
    from test_channels_contract import FAKE_CONTRACT
    r = private_redis_v2
    r.flushdb()
    p = policy_of()
    transport = FAKE_CONTRACT['transport']()
    lines = '\n'.join(k + '=' + str(v) for k, v in FAKE_CONTRACT['account'].items())
    account = channels.parse_account_text('fake', ACCOUNT, lines)
    inner = Audit()
    ch = FakeChannel(account, transport=transport, store=base.MemoryStore(),
                     audit=strangers.watch(inner, r=r, policy=p, type_='fake', account=ACCOUNT, clock=Clock()),
                     inbox_dir=tmp_path / 'inbox' / 'files')
    for i in range(security.DEFAULTS['strangers']['alert_after']):
        ch.inbound({'id': 100 + i, 'chat': 'c-9', 'from': 'u-9', 'text': 'hello from a stranger'},
                   accept=lambda env: 'persisted')
    assert [op for op, _ in transport.sent] == []  # nothing is sent to anyone, least of all the stranger
    assert r.llen(channels.OUTQ) == 1  # the alert waits in the queue for the owner (target resolution is P1e)
    assert 'security.stranger_alert' in inner.events()


# ---------------------------------------------------------------------------------------------- 7
def test_watch_wrapper_passes_every_other_audit_through_unchanged(private_redis_v2):
    r = private_redis_v2
    r.flushdb()
    p = policy_of()
    rows = [('channel.in', {'message_id': 'fk:1', 'sender': 'u-1'}),
            ('channel.error', {'kind': 'transport', 'error': 'x'}),
            ('channel.drop_dup', {'message_id': 'fk:1'}),
            ('channel.drop_policy', {'sender': 'u-9', 'conversation': 'c-9'})]
    direct, wrapped = Audit(), Audit()
    w = strangers.watch(wrapped, r=r, policy=p, type_='fake', account=ACCOUNT, clock=Clock())
    for event, fields in rows:
        direct(event, **fields)
        w(event, **fields)
    assert list(wrapped) == list(direct)

    def broken(event, **fields):
        raise RuntimeError('sink down')
    strangers.watch(broken, r=r, policy=p, type_='fake', account=ACCOUNT, clock=Clock())('channel.in', x=1)
    dead = redis.Redis(unix_socket_path='/nonexistent/fleetmux-test.sock', decode_responses=True,
                       socket_connect_timeout=0.2)
    inner = Audit()
    wd = strangers.watch(inner, r=dead, policy=p, type_='fake', account=ACCOUNT, clock=Clock())
    for event, fields in rows:
        wd(event, **fields)  # never raises
    assert list(inner) == list(direct)
