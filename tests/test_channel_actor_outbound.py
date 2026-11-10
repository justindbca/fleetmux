"""P1e channel actor, outbound (brief v2 §6.6; core-1 test-plan §1, §4.3, §4.4, §5 and supervisor rulings 2 / 11).

A2 red tests: ``cmux_fleet.channels.actor`` is imported inside every test body, so a missing actor fails the test
(FAILED), never the collection (ERROR). Real private unix-socket Redis, the in-process RuntimeV2 of ``fx`` /
``mixed``, the P0 ``FakeChannel`` with fake transports. No network, no credentials, no listener; the config dir is
a temp dir (autouse ``config_dir``). ``fcntl`` locks only a temp lease file.

Interfaces pinned (test-plan §1): ``outbound_scan(r, entries, *, clock=time.time) -> int``;
``resolve_target(r, item, entries, accounts) -> dict | None``; ``send_direct(cfg, item, *, make_channel=None)``
(``make_channel(type, account) -> Channel``); ``outbound_loop(r, chans, *, stop, clock, sleep)`` and
``drain(r, chans, *, deadline, clock=time.time)`` with ``chans = {(type, account): Channel}``;
``enqueue_run_closed(f) -> bool``. Mail plan A (I2): ``fleet_name`` / ``send_ref`` set around each real send and
reset in ``finally``. Ruling 11: ``fx:channel:actor`` ``sending`` / ``sending_at`` around the real send,
``drained`` when nothing is left; never ``BRPOP`` with timeout 0.
"""
import fcntl
import json
import time

import pytest
import redis

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import fx  # noqa: F401  (fixture: v2 service fleet)
from test_v2_codex_claude import mixed  # noqa: F401  (fixture: v2 task fleet)
from test_channels_contract import Audit, FakeChannel, config_dir, fake_registered  # noqa: F401
from cmux_fleet import channels, lifecycle_close
from cmux_fleet.channels import base, envelope

ACCOUNT = 'contract-1'
ENTRY = {'type': 'fake', 'account': ACCOUNT, 'push': list(channels.DEFAULT_PUSH)}


# ---------------------------------------------------------------------------------------------- helpers
def origin(conversation='c-1', account=ACCOUNT, type_='fake'):
    env = envelope.make(type_, account, conversation=conversation, message_id='m-' + conversation, sender='u-1',
                        text='q', reply_ref={'chat': conversation})
    return envelope.origin_from(env)


def publish(r, rec, with_origin=True, conv='c-1'):
    """A result the way runtime_v2.publish leaves it: fx:result:<id> + RPUSH fx:results."""
    rec = dict({'status': 'ok', 'at': time.time(), 'result': {'reply': 'answer ' + rec['id']}}, **rec)
    if with_origin:
        r.set(channels.ORIGIN + envelope.origin_id(rec['id']), json.dumps(origin(conv)), ex=channels.ORIGIN_TTL)
    r.set('fx:result:' + rec['id'], json.dumps(rec, ensure_ascii=False))
    r.rpush('fx:results', json.dumps(rec, ensure_ascii=False))
    return rec


def queued(r, key=channels.OUTQ):
    return [json.loads(x) for x in reversed(r.lrange(key, 0, -1))]  # LPUSH queue: oldest first


def accounts(push_to='u-1'):
    values = {'PUSH_TO': push_to} if push_to else {}
    return {('fake', ACCOUNT): channels.Account('fake', ACCOUNT, ['u-1'], values)}


class Spy(FakeChannel):
    """Records what the actor set around each real send: (op, text, fleet_name, send_ref, actor hash)."""
    seen = None
    fail_first = 0
    cost = 0.0
    clock = None
    redis = None

    def _send_text(self, conversation, text, reply_ref):
        hash_ = self.redis.hgetall(channels.ACTOR) if self.redis is not None else {}
        self.seen.append(('text', text, self.fleet_name, self.send_ref, hash_))
        if self.clock is not None:
            self.clock[0] += self.cost
        if self.fail_first:
            self.fail_first -= 1
            raise OSError('flaky send')
        return 'm-' + str(len(self.seen))


def spy(r=None, name=ACCOUNT, **kw):
    ch = Spy(channels.Account('fake', name, ['u-1'], {'PUSH_TO': 'u-1'}), transport=lambda op, data: 'm-1')
    ch.seen, ch.redis = [], r
    ch.clock = None  # base.Channel.__init__ sets self.clock = time.time, shadowing Spy.clock
    for k, v in kw.items():
        setattr(ch, k, v)
    return ch


class Recording:
    """A Redis handle that records BRPOP calls (ruling 11: never timeout=0) and forwards everything."""
    def __init__(self, r):
        self._r, self.brpops = r, []

    def brpop(self, keys, timeout=0):
        self.brpops.append(timeout)
        return self._r.brpop(keys, timeout=timeout)

    def __getattr__(self, name):
        return getattr(self._r, name)


# ---------------------------------------------------------------------------------------------- the 16 tests
def test_result_with_origin_enqueued_once_per_push_channel(private_redis_v2, fake_registered):
    from cmux_fleet.channels import actor
    r = private_redis_v2
    r.flushdb()
    entries = [dict(ENTRY, push=['result']), {'type': 'exec', 'account': 'relay', 'push': ['result']}]
    publish(r, {'id': 'r1'})
    assert actor.outbound_scan(r, entries) == 2
    items = queued(r)
    assert len(items) == 2 and all(i['event'] == 'result' and i['request_id'] == 'r1' for i in items)
    assert [i['target'] for i in items if i.get('channel') != 'exec'] == ['origin']  # the conversation, once
    assert [i['channel'] for i in items if i.get('channel') == 'exec'] == ['exec']  # the outbound-only channel
    assert actor.outbound_scan(r, entries) == 0 and len(queued(r)) == 2  # a second pass never re-pushes
    assert r.exists(channels.PUSHED + 'result:r1')


def test_result_without_origin_waits_60s_then_marked_not_pushed(private_redis_v2, fake_registered):
    from cmux_fleet.channels import actor
    r = private_redis_v2
    r.flushdb()
    now = [5000.0]
    publish(r, {'id': 'ui-cli', 'at': now[0]}, with_origin=False)
    assert actor.outbound_scan(r, [ENTRY], clock=lambda: now[0]) == 0
    assert not r.exists(channels.PUSHED + 'result:ui-cli') and queued(r) == []  # looked at again next pass
    now[0] += channels.ORIGIN_GRACE_S + 1
    assert actor.outbound_scan(r, [ENTRY], clock=lambda: now[0]) == 0
    assert r.exists(channels.PUSHED + 'result:ui-cli') and queued(r) == []  # an outside request: marked, not sent


def test_long_reply_over_3_chunks_becomes_file_item(private_redis_v2, fake_registered):
    from cmux_fleet.channels import actor
    r = private_redis_v2
    r.flushdb()
    long = '行' * (3 * base.Channel.TEXT_LIMIT + 1)
    publish(r, {'id': 'r-long', 'result': {'reply': long}})
    actor.outbound_scan(r, [ENTRY])
    items = queued(r)
    files = [i for i in items if i.get('file')]
    texts = [i for i in items if i.get('text')]
    assert len(files) == 1 and len(texts) == 1 and len(items) == 2
    from pathlib import Path
    assert long in Path(files[0]['file']).read_text(encoding='utf-8')
    assert len(texts[0]['text']) <= base.Channel.TEXT_LIMIT
    assert {i['request_id'] for i in items} == {'r-long'}


def test_timeout_failed_invalid_results_carry_status_line(private_redis_v2, fake_registered):
    from cmux_fleet.channels import actor
    r = private_redis_v2
    r.flushdb()
    for status in ('timeout', 'failed', 'invalid'):
        publish(r, {'id': 'r-' + status, 'status': status, 'result': None, 'error': status + ' detail'})
    actor.outbound_scan(r, [ENTRY])
    items = {i['request_id']: i for i in queued(r)}
    for status in ('timeout', 'failed', 'invalid'):
        assert status in items['r-' + status]['text'].splitlines()[0]


def test_done_followup_routes_by_origin_id(private_redis_v2, fake_registered):
    from cmux_fleet.channels import actor
    r = private_redis_v2
    r.flushdb()
    r.set(channels.ORIGIN + 'r1', json.dumps(origin('c-77')), ex=channels.ORIGIN_TTL)  # only r1 has an origin
    publish(r, {'id': 'r1.done', 'result': {'reply': 'follow-up', 'delegated': 'M0', 'outcome': 'completed'}},
            with_origin=False)
    actor.outbound_scan(r, [ENTRY])
    items = queued(r)
    assert [(i['event'], i['request_id'], i['target']) for i in items] == [('result', 'r1.done', 'origin')]
    target = actor.resolve_target(r, items[0], [ENTRY], accounts())
    assert target['conversation'] == 'c-77' and envelope.origin_id('r1.done') == 'r1'


def test_task_done_enqueued_on_terminal_states(mixed, fake_registered):
    from cmux_fleet.channels import actor
    f = mixed
    for n, status in enumerate(('completed', 'blocked', 'budget_exhausted')):
        f.r.hset('fx:task', mapping={'id': 'T' + str(n), 'status': status, 'goal': 'goal ' + str(n)})
        actor.outbound_scan(f.r, [ENTRY])
    items = [i for i in queued(f.r) if i['event'] == 'task_done']
    assert len(items) == 3 and all(i['target'] == 'last' for i in items)
    for item, status in zip(items, ('completed', 'blocked', 'budget_exhausted')):
        assert status in item['text']
    actor.outbound_scan(f.r, [ENTRY])
    assert len([i for i in queued(f.r) if i['event'] == 'task_done']) == 3  # no repeat


def test_escalation_off_by_default_on_when_listed(private_redis_v2, fake_registered):
    from cmux_fleet.channels import actor
    r = private_redis_v2
    r.flushdb()
    r.set('fx:escalations', json.dumps({'blocked:worker-1': {'kind': 'blocked', 'reason': 'needs a decision'},
                                        'dialog:worker-2:g0': {'kind': 'dialog', 'reason': 'unknown prompt'}}))
    actor.outbound_scan(r, [ENTRY])  # DEFAULT_PUSH has no escalation
    assert [i for i in queued(r) if i['event'] == 'escalation'] == []
    r.flushdb()
    r.set('fx:escalations', json.dumps({'blocked:worker-1': {'kind': 'blocked', 'reason': 'needs a decision'}}))
    actor.outbound_scan(r, [dict(ENTRY, push=['escalation'])])
    items = [i for i in queued(r) if i['event'] == 'escalation']
    assert len(items) == 1 and items[0].get('text') and not items[0].get('card')  # text only, no buttons


def test_run_closed_enqueued_at_daemon_stop_without_lifecycle(fx, monkeypatch, fake_registered):
    """Ruling 11 / core-3 §4.2: the explicit-stop tail enqueues run_closed, then channel_stop_all(drain_s=5), both
    before lifecycle_close.finalize and before Redis closes. Until P1f lands channel_stop_all is stubbed here."""
    from cmux_fleet.channels import actor
    f = fx
    f.cfg['channels'] = [dict(ENTRY, push=['run_closed'])]
    assert not f.cfg.get('lifecycle') and not lifecycle_close.managed(f)
    order, seen = [], {}

    def stop_all(drain_s=5):
        order.append(('channel_stop_all', drain_s))
        seen['outq'] = queued(f.r)
        seen['again'] = real_enqueue(f)  # deduplicated by pushed:run_closed:<run_id>
        seen['outq_after'] = len(queued(f.r))
        seen['pushed'] = f.r.exists(channels.PUSHED + 'run_closed:' + f.v2.bus.run_id)
        return {'drained': True, 'left': 0, 'in_flight': '', 'exit': 0}
    real_enqueue, real_finalize = actor.enqueue_run_closed, lifecycle_close.finalize
    monkeypatch.setattr(actor, 'enqueue_run_closed', lambda fl: order.append(('enqueue',)) or real_enqueue(fl))
    monkeypatch.setattr(f, 'channel_stop_all', stop_all, raising=False)
    monkeypatch.setattr(lifecycle_close, 'finalize', lambda fl, ok: order.append(('finalize',)) or real_finalize(fl, ok))
    f.stopping = f.explicit_stop = True
    f.v2.close()
    f.v2.reader = f.v2.bus.blocking_client(1000)
    real_close = f.v2.close
    monkeypatch.setattr(f.v2, 'close', lambda *a, **k: order.append(('close',)) or real_close(*a, **k))
    f.run()
    assert order == [('enqueue',), ('channel_stop_all', 5), ('finalize',), ('close',)]
    assert [(i['event'], i['target']) for i in seen['outq']] == [('run_closed', 'last')]
    assert seen['again'] is False and seen['outq_after'] == 1 and seen['pushed']


def test_push_list_filters_per_channel(private_redis_v2, fake_registered):
    from cmux_fleet.channels import actor
    r = private_redis_v2
    r.flushdb()
    undo = channels.register('fakeb', FakeChannel, 'fakeb')
    try:
        a = dict(ENTRY, push=['result'])
        b = {'type': 'fakeb', 'account': 'b', 'push': ['task_done']}
        accts = {**accounts(), ('fakeb', 'b'): channels.Account('fakeb', 'b', ['u-1'], {'PUSH_TO': 'u-1'})}
        publish(r, {'id': 'r1'})
        r.hset('fx:task', mapping={'id': 'T1', 'status': 'completed', 'goal': 'g'})
        r.hset(channels.LAST, mapping={'channel': 'fakeb', 'account': 'b', 'conversation': 'u-1', 'reply_ref': '{}',
                                       'sender': 'u-1', 'at': '1'})
        actor.outbound_scan(r, [a, b])
        targets = {i['event']: actor.resolve_target(r, i, [a, b], accts) for i in queued(r)}
        assert set(targets) == {'result', 'task_done'}
        assert targets['result']['channel'] == 'fake'  # only A pushes results
        assert targets['task_done']['channel'] == 'fakeb'  # only B pushes terminal task states
    finally:
        undo()


def test_resolve_target_user_only_requires_private_last(fx, fake_registered):
    from cmux_fleet.channels import actor
    f = fx
    item = envelope.outbound('prompt_wait', text='待办', user_only=True)
    f.r.hset(channels.LAST, mapping={'channel': 'fake', 'account': ACCOUNT, 'conversation': 'u-1', 'reply_ref': '{}',
                                     'sender': 'u-1', 'at': '1'})
    got = actor.resolve_target(f.r, item, [ENTRY], accounts())
    assert (got['channel'], got['account'], got['conversation']) == ('fake', ACCOUNT, 'u-1')
    f.r.delete(channels.LAST)
    assert actor.resolve_target(f.r, item, [ENTRY], accounts()) is None  # never falls back to PUSH_TO
    assert [x for _, x in f.r.xrange(f.v2.bus.audit_key) if x.get('event') == 'channel.no_target']


def test_resolve_target_falls_back_to_last_json_when_redis_down(fake_registered):
    from cmux_fleet.channels import actor

    class Down:
        def __getattr__(self, name):
            def boom(*a, **k):
                raise redis.ConnectionError('redis is down')
            return boom
    item = envelope.outbound('manual', text='hi')
    path = channels.last_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'channel': 'fake', 'account': ACCOUNT, 'conversation': 'c-9', 'reply_ref': {},
                                'sender': 'u-1', 'at': 1}), encoding='utf-8')
    assert actor.resolve_target(Down(), item, [ENTRY], accounts())['conversation'] == 'c-9'
    path.unlink()
    got = actor.resolve_target(Down(), item, [ENTRY], accounts())
    assert (got['channel'], got['conversation']) == ('fake', 'u-1')  # first: the first channel with PUSH_TO


def test_send_direct_bypasses_queue_and_lease(private_redis_v2, tmp_path, fake_registered):
    """Also mail plan A (I2): send_direct sets fleet_name = channels.fleet_label(cfg) and send_ref per item
    (request_id, else the item id) around the send, then resets send_ref to None; no fleet name -> ''."""
    from cmux_fleet.channels import actor
    r = private_redis_v2
    r.flushdb()
    lock = channels.lease_path('fake', ACCOUNT)
    lock.parent.mkdir(parents=True, exist_ok=True)
    holder = open(lock, 'a+')
    fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)  # another run holds this account's lease
    try:
        built = []

        def make_channel(type_, account):
            built.append(spy())
            return built[-1]
        cfg = {'name': 'fleet-a', 'run': str(tmp_path / 'run-1'), 'channels': [ENTRY],
               'redis_socket': r.connection_pool.connection_kwargs['path']}
        before = r.llen(channels.OUTQ)
        item = envelope.outbound('manual', target='explicit', channel='fake', account=ACCOUNT, conversation='u-1',
                                 text='hello', request_id='r9')
        assert actor.send_direct(cfg, item, make_channel=make_channel)['status'] == 'sent'
        assert r.llen(channels.OUTQ) == before  # not queued
        assert built[0].seen[0][1:4] == ('hello', 'fleet-a', 'r9') and built[0].send_ref is None
        plain = envelope.outbound('manual', target='explicit', channel='fake', account=ACCOUNT, conversation='u-1',
                                  text='again')
        assert actor.send_direct({'channels': [ENTRY]}, plain, make_channel=make_channel)['status'] == 'sent'
        assert built[1].seen[0][2:4] == ('', plain['id']) and built[1].send_ref is None  # never the account name
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()


def test_outbound_loop_routes_then_processes_and_throttles(private_redis_v2, fake_registered):
    """route -> account queue -> process_one -> sent; the same text again within 5 s is suppressed. I2: every
    chunk and retry of one item carries the same send_ref; it is None again after each item."""
    from cmux_fleet.channels import actor
    r = private_redis_v2
    r.flushdb()
    ch = spy(r, fail_first=1)
    chans = {('fake', ACCOUNT): ch}
    a = envelope.outbound('manual', target='explicit', channel='fake', account=ACCOUNT, conversation='u-1',
                          text='长' * 80, request_id='r1')  # FakeChannel TEXT_LIMIT 50: two chunks
    b = envelope.outbound('manual', target='explicit', channel='fake', account=ACCOUNT, conversation='u-1',
                          text='second item')
    c = dict(envelope.outbound('manual', target='explicit', channel='fake', account=ACCOUNT, conversation='u-1',
                               text='second item'))  # same text within 5 s
    for item in (a, b, c):
        r.lpush(channels.OUTQ, envelope.dumps(item))
    deadline = time.time() + 15
    actor.outbound_loop(r, chans, stop=lambda: time.time() > deadline or (
        r.llen(channels.OUTQ) == 0 and r.llen(channels.OUTQ_ACCOUNT + 'fake:' + ACCOUNT) == 0 and len(ch.seen) >= 4),
        clock=time.time, sleep=lambda s: None)
    texts = [s[1] for s in ch.seen]
    refs = [s[3] for s in ch.seen]
    assert texts.count('second item') == 1  # the third item was suppressed by the throttle
    assert refs[:-1] == ['r1'] * (len(refs) - 1) and refs[-1] == b['id']  # retry + chunks share r1
    assert len([t for t in texts if t != 'second item']) >= 3  # first try failed, then two chunks
    assert ch.send_ref is None


def test_exec_channel_gets_whole_item_on_stdin(private_redis_v2, fake_registered):
    from cmux_fleet.channels import actor
    r = private_redis_v2
    r.flushdb()
    got = []

    class FakeExec(FakeChannel):
        type = 'fakeexec'
        RECEIVES = False
        CARRIES_TOKENS = False

        def send_item(self, item):  # P6a: the whole outbound item, as one JSON line on the command's stdin
            got.append(self.transport('send_item', envelope.dumps(item)))
            return base.SendResult(ok=True, message_id='x-1')
    undo = channels.register('fakeexec', FakeExec, 'fakeexec')
    try:
        ch = FakeExec(channels.Account('fakeexec', 'relay', []), transport=lambda op, data: data)
        card = envelope.card('待办', '删除 /tmp/x', [{'label': '批准', 'command': '/approve p-0123abcd'}], ref='p-0123abcd')
        item = envelope.outbound('prompt_wait', target='explicit', channel='fakeexec', account='relay',
                                 conversation='stdin', card=card)
        r.lpush(channels.OUTQ, envelope.dumps(item))
        deadline = time.time() + 10
        actor.outbound_loop(r, {('fakeexec', 'relay'): ch}, stop=lambda: time.time() > deadline or got,
                            clock=time.time, sleep=lambda s: None)
        assert len(got) == 1
        sent = json.loads(got[0])
        assert got[0] == envelope.dumps(sent)  # one canonical JSON line
        assert sent['id'] == item['id'] and sent['card'] == envelope.redact_card(card)  # no buttons, no token
    finally:
        undo()


def test_outbound_scan_reads_results_incrementally(private_redis_v2, fake_registered):
    from cmux_fleet.channels import actor
    r = private_redis_v2
    r.flushdb()
    for n in range(3):
        publish(r, {'id': 'a' + str(n)})
    assert actor.outbound_scan(r, [ENTRY]) == 3
    r.ltrim('fx:results', 2, -1)  # runtime_v2.publish trims the list (LTRIM -1000): indexes move
    for n in range(2):
        publish(r, {'id': 'b' + str(n)})
    assert actor.outbound_scan(r, [ENTRY]) == 2  # only the two new ones
    assert [i['request_id'] for i in queued(r)] == ['a0', 'a1', 'a2', 'b0', 'b1']


def test_actor_exit_drains_queue_up_to_5s(private_redis_v2, fake_registered):
    """Ruling 11: drain routes what is still unrouted first, then sends without blocking (never BRPOP timeout=0);
    sending / sending_at are set around each real send and cleared in finally (also when it raises);
    drained=1 only when nothing is left."""
    from cmux_fleet.channels import actor
    r = private_redis_v2
    r.flushdb()
    rec = Recording(r)
    now = [100.0]
    ch = spy(r, fail_first=1, clock=now)
    chans = {('fake', ACCOUNT): ch}
    acct_q = channels.OUTQ_ACCOUNT + 'fake:' + ACCOUNT
    items = [envelope.outbound('manual', target='explicit', channel='fake', account=ACCOUNT, conversation='u-1',
                               text='item ' + str(n)) for n in range(3)]
    r.lpush(channels.OUTQ, envelope.dumps(items[0]))  # not routed yet
    for item in items[1:]:
        r.lpush(acct_q, envelope.dumps(item))
    actor.drain(rec, chans, deadline=now[0] + 5, clock=lambda: now[0])
    sent = [s for s in ch.seen]
    assert {s[1] for s in sent} >= {'item 0', 'item 1', 'item 2'}  # all three went out (one after a retry)
    for s in sent:
        assert s[4].get('sending') and s[4].get('sending_at')  # set around the real send
    assert not r.hget(channels.ACTOR, 'sending') and not r.hget(channels.ACTOR, 'sending_at')  # cleared
    assert 0 not in rec.brpops
    assert r.llen(channels.OUTQ) == 0 and r.llen(acct_q) == 0 and r.hget(channels.ACTOR, 'drained') == '1'
    # slow sends: 3 s each against a 5 s budget -> the third item stays queued, nothing claims drained
    r.flushdb()
    now[0] = 200.0
    slow = spy(r, clock=now, cost=3.0)
    for item in items:
        r.lpush(acct_q, envelope.dumps(item))
    actor.drain(Recording(r), {('fake', ACCOUNT): slow}, deadline=now[0] + 5, clock=lambda: now[0])
    assert len(slow.seen) <= 2 and r.llen(acct_q) >= 1
    assert r.hget(channels.ACTOR, 'drained') != '1'


def test_channel_without_push_uses_default_push(private_redis_v2, fake_registered):
    """M119 ruling C2: a channel the actor was given without ``ch.push`` gets channels.DEFAULT_PUSH, never every
    event: escalation (off by default) is not routed to it and is audited channel.no_target; task_done is sent."""
    from cmux_fleet.channels import actor
    r = private_redis_v2
    r.flushdb()
    ch = spy(r, clock=None)
    assert getattr(ch, 'push', None) is None and 'escalation' not in channels.DEFAULT_PUSH
    r.hset(channels.LAST, mapping={'channel': 'fake', 'account': ACCOUNT, 'conversation': 'u-1', 'reply_ref': '{}',
                                   'sender': 'u-1', 'at': '1'})
    for event, text in (('escalation', 'esc text'), ('task_done', 'done text')):
        r.lpush(channels.OUTQ, envelope.dumps(envelope.outbound(event, target='last', text=text)))
    audit = Audit()
    acct_q = channels.OUTQ_ACCOUNT + 'fake:' + ACCOUNT
    deadline = time.time() + 10
    actor.outbound_loop(r, {('fake', ACCOUNT): ch}, audit=audit, clock=time.time, sleep=lambda s: None,
                        stop=lambda: time.time() > deadline or (r.llen(channels.OUTQ) == 0 and r.llen(acct_q) == 0
                                                                and len(ch.seen) >= 1))
    assert [s[1] for s in ch.seen] == ['done text']
    assert 'channel.no_target' in audit.events()


def test_channel_with_empty_push_gets_no_push_events(private_redis_v2, fake_registered):
    """M156 (core-2 P1g finding): a roster entry ``push: []`` means "push nothing". An empty ``ch.push`` must not
    fall back to DEFAULT_PUSH (only a missing ``push`` does): task_done is not routed to it and is audited
    channel.no_target; a reply (not a push event) still goes out."""
    from cmux_fleet.channels import actor
    r = private_redis_v2
    r.flushdb()
    ch = spy(r, clock=None)
    ch.push = []
    assert 'task_done' in channels.DEFAULT_PUSH
    r.hset(channels.LAST, mapping={'channel': 'fake', 'account': ACCOUNT, 'conversation': 'u-1', 'reply_ref': '{}',
                                   'sender': 'u-1', 'at': '1'})
    for event, text in (('task_done', 'done text'), ('reply', 'reply text')):
        r.lpush(channels.OUTQ, envelope.dumps(envelope.outbound(event, target='last', text=text)))
    audit = Audit()
    acct_q = channels.OUTQ_ACCOUNT + 'fake:' + ACCOUNT
    deadline = time.time() + 10
    actor.outbound_loop(r, {('fake', ACCOUNT): ch}, audit=audit, clock=time.time, sleep=lambda s: None,
                        stop=lambda: time.time() > deadline or (r.llen(channels.OUTQ) == 0 and r.llen(acct_q) == 0
                                                                and len(ch.seen) >= 1))
    assert [s[1] for s in ch.seen] == ['reply text']
    assert 'channel.no_target' in audit.events()
