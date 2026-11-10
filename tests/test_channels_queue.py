"""P1c: outbound throttle and queues of the channels (brief v2 §6.4; docs/design.md §53, contracts §8).

Red until package P1c lands (phase B): ``channels/throttle.py`` and ``channels/queue.py`` are A2 stubs whose
functions raise NotImplementedError.

What is pinned (core-3 test plan, supervisor rulings):
- throttle = notify_bus ``throttle.py`` (async path) behind a synchronous Redis interface, keys under
  ``fx:channel:throttle:``; storm bucket = ``<channel>:<account>:<conversation>``; ``reply`` / ``user_only`` items
  are never held and their dedupe key includes the item id (distinct receipts are never swallowed).
- queue: three attempts (waits 2 s and 8 s), dead letters capped at 1000, ``block == 0`` is a real non-blocking
  ``RPOP`` (``BRPOP`` with timeout 0 blocks forever, ruling M10), ``reply_ref`` never enters any audit.
- Fake clock honesty: ``now=`` drives the storm window and hold times only; Redis ``EX`` expiry runs on the real
  clock, so "allowed again after 5 s" and "hold record expires" are not claimed here.

Fakes only: the repo's private test Redis (unix socket, no TCP); no network, no real credentials.
"""
import json
import time

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture: the repo's private test Redis)
from test_channels_contract import Audit
from cmux_fleet import channels
from cmux_fleet.channels import base, envelope, queue, throttle

FAKE_CONTEXT_TOKEN = 'ctx-FAKE-0123456789'
FAKE_BOT_TOKEN = '123:FAKE'


@pytest.fixture(autouse=True)
def config_dir(monkeypatch, tmp_path):
    monkeypatch.setenv('FLEETMUX_CONFIG_DIR', str(tmp_path / 'fleetmux-config'))


@pytest.fixture
def r(private_redis_v2):
    private_redis_v2.flushdb()
    return private_redis_v2


def item(text='hello', event='result', conversation='42', **kw):
    """An outbound item with its target resolved explicitly (one storm bucket per conversation)."""
    return envelope.outbound(event, text=text, target='explicit', channel='telegram', account='bot-1',
                             conversation=conversation, **kw)


def fail(_item):
    return base.SendResult(ok=False, error='down')


# ====================================================================== throttle
def dedupe_traffic(r, now=1000.0):
    a, b = item('same'), item('same')
    assert throttle.gate(r, a, now=now)[0] == 'send'
    assert throttle.gate(r, b, now=now + 1)[0] == 'suppressed'  # same text, other id, inside 5 s
    assert throttle.gate(r, item('same!'), now=now + 2)[0] == 'send'  # one character differs
    # receipts: the same text for two different inbound messages are two receipts, both sent
    for event, extra in (('reply', {}), ('prompt_wait', {'user_only': True})):
        first = item('Fleet 输入被拒绝：x', event=event, **extra)
        second = item('Fleet 输入被拒绝：x', event=event, **extra)
        assert throttle.gate(r, first, now=now + 3)[0] == 'send'
        assert throttle.gate(r, second, now=now + 3)[0] == 'send', event  # distinct id: never swallowed
        assert throttle.gate(r, first, now=now + 4)[0] == 'suppressed', event  # the same item again


def storm_traffic(r, conversation, t0):
    """15 different texts into one bucket at 1 s steps, with 3 reply and 2 user_only items after the 13th."""
    verdicts = []
    for i in range(15):
        verdict, out = throttle.gate(r, item('status line %d' % (i + 1), conversation=conversation), now=t0 + i)
        verdicts.append((verdict, out))
        if i == 12:
            for k in range(3):
                assert throttle.gate(r, item('回执 %d' % k, event='reply', conversation=conversation),
                                     now=t0 + i)[0] == 'send'  # never held
            for k in range(2):
                assert throttle.gate(r, item('删除确认 %d' % k, event='prompt_wait', conversation=conversation,
                                             user_only=True), now=t0 + i)[0] == 'send'  # never held
    return verdicts


def test_throttle_dedupe_same_text_within_5s(r):
    dedupe_traffic(r)


def test_throttle_storm_12_in_120s_merges_into_summary(r):
    t0 = 2000.0
    verdicts = storm_traffic(r, '42', t0)
    for i in range(11):
        assert verdicts[i][0] == 'send' and verdicts[i][1]['text'] == 'status line %d' % (i + 1)
    twelfth = verdicts[11]
    assert twelfth[0] == 'send' and '12' in twelfth[1]['text'] and '合并' in twelfth[1]['text']
    assert [v for v, _ in verdicts[12:]] == ['held', 'held', 'held']
    # a second storm in another conversation starts 100 s later: its own bucket, its own hold time
    storm_traffic(r, '43', t0 + 100)
    assert throttle.storm_collect_flushes(r, now=t0 + 12 + 119) == []  # held since t0+12: not yet
    flushes = throttle.storm_collect_flushes(r, now=t0 + 12 + 120)
    assert len(flushes) == 1
    bucket, event, n, latest = flushes[0]
    assert bucket.endswith(':42') and event == 'result' and n == 3 and 'status line 15' in latest
    summaries = throttle.flush_items(r, now=t0 + 112 + 120)
    assert len(summaries) == 1
    s = summaries[0]
    assert '3 条已合并' in s['text'] and s['target'] == 'explicit'
    assert (s['channel'], s['account'], s['conversation']) == ('telegram', 'bot-1', '43')
    assert envelope.outbound_problem(s) == ''
    assert throttle.flush_items(r, now=t0 + 400) == [] and throttle.storm_collect_flushes(r, now=t0 + 400) == []


def test_throttle_keys_under_channel_prefix(r):
    dedupe_traffic(r)
    storm_traffic(r, '42', 3000.0)
    throttle.flush_items(r, now=3000.0 + 300)
    keys = [k for k in r.keys('*') if not k.startswith(channels.OUTQ) and k != channels.DEAD]
    assert keys and all(k.startswith('fx:channel:throttle:') for k in keys), keys
    assert not [k for k in r.keys('*') if k.startswith('notify:')]


# ====================================================================== queue
def test_route_moves_item_to_account_queue(r):
    it = envelope.outbound('result', text='done', target='last')
    queue.enqueue(r, it)
    assert r.llen(channels.OUTQ) == 1
    out = queue.route(r, it, {'channel': 'telegram', 'account': 'bot-1', 'conversation': 'c1',
                              'reply_ref': {'m': 1}})
    key = channels.OUTQ_ACCOUNT + 'telegram:bot-1'
    assert r.llen(key) == 1 and r.llen(channels.OUTQ) == 0
    got = json.loads(r.lindex(key, 0))
    assert got['id'] == it['id'] and got['text'] == 'done'
    assert (got['channel'], got['account'], got['conversation'], got['reply_ref']) == ('telegram', 'bot-1', 'c1',
                                                                                     {'m': 1})
    assert out['id'] == it['id'] and out['conversation'] == 'c1'


def test_dead_letter_record_and_cap_1000(r):
    it = item('x')
    queue.enqueue(r, it)
    waits = []
    out = queue.process_one(r, fail, sleep=waits.append, block=1)
    assert out['status'] == 'dead'
    first = json.loads(r.lindex(channels.DEAD, 0))
    assert first['id'] == it['id'] and first['attempts'] == 3 and 'down' in first['dead_reason']
    for i in range(1000):
        queue.dead_letter(r, item('n%d' % i), 'test')
    assert r.llen(channels.DEAD) == 1000
    newest = json.loads(r.lindex(channels.DEAD, 0))
    assert newest['text'] == 'n999' and newest['dead_reason']  # newest first; the oldest fell off


def test_send_failed_audited_per_attempt_and_dead_once(r):
    queue.enqueue(r, item('x'))
    audit, waits = Audit(), []
    out = queue.process_one(r, fail, sleep=waits.append, block=1, audit=audit)
    assert out['status'] == 'dead' and waits == [2, 8]
    failed = [f for e, f in audit if e == 'channel.send_failed']
    assert [int(f['attempt']) for f in failed] == [1, 2, 3]
    assert audit.events().count('channel.dead') == 1 and 'channel.out' not in audit.events()
    for e, f in audit:
        assert f['channel'] == 'telegram' and f['account'] == 'bot-1', (e, f)


class Spy:
    """Records the list-pop calls made on the Redis client, passes everything through."""
    POPS = ('brpop', 'blpop', 'rpop', 'lpop', 'brpoplpush', 'blmove', 'lmove', 'rpoplpush')

    def __init__(self, r):
        self.r, self.calls = r, []

    def __getattr__(self, name):
        attr = getattr(self.r, name)
        if name not in self.POPS:
            return attr

        def recorded(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return attr(*args, **kwargs)
        return recorded


def test_process_one_empty_queue_returns_empty_without_sleep(r):
    waits, sent = [], []
    start = time.monotonic()
    out = queue.process_one(r, sent.append, sleep=waits.append, block=1)
    assert out['status'] == 'empty' and waits == [] and sent == []
    assert time.monotonic() - start < 1.9  # one blocking second at most (client socket_timeout is 2 s)
    # block=0 (the drain): a real non-blocking RPOP, never BRPOP (BRPOP with timeout 0 blocks forever)
    spy = Spy(r)
    start = time.monotonic()
    out = queue.process_one(spy, sent.append, sleep=waits.append, block=0)
    assert out['status'] == 'empty' and time.monotonic() - start < 0.5
    names = [name for name, _, _ in spy.calls]
    assert 'rpop' in names and 'brpop' not in names and 'blpop' not in names, spy.calls
    queue.enqueue(r, item('drained'))
    out = queue.process_one(spy, lambda it: base.SendResult(ok=True, message_id='m-1'), sleep=waits.append,
                            block=0)
    assert out['status'] == 'sent' and 'brpop' not in [name for name, _, _ in spy.calls]


def test_out_audit_masks_values_and_truncates_200(r):
    it = item('x' * 500, reply_ref={'context_token': FAKE_CONTEXT_TOKEN})
    queue.enqueue(r, it)
    answers = [base.SendResult(ok=False, error='POST https://api.telegram.org/bot' + FAKE_BOT_TOKEN
                               + '/sendMessage 500'),
               base.SendResult(ok=True, message_id='m-1')]
    audit, waits = Audit(), []
    out = queue.process_one(r, lambda _it: answers.pop(0), sleep=waits.append, block=1, audit=audit)
    assert out['status'] == 'sent'
    outs = [f for e, f in audit if e == 'channel.out']
    assert len(outs) == 1 and len(outs[0]['text']) <= 200
    blob = json.dumps(audit, ensure_ascii=False, default=str)
    assert FAKE_CONTEXT_TOKEN not in blob and FAKE_BOT_TOKEN not in blob  # no field of any audit
    failed = [f for e, f in audit if e == 'channel.send_failed']
    assert failed and '***' in failed[0]['error']  # redacted (P0b channels.redact), not dropped
    assert all('reply_ref' not in f for _, f in audit)  # reply_ref never enters an audit (ruling)


def test_consumer_exception_in_send_becomes_dead_not_crash(r):
    queue.enqueue(r, item('x'))
    waits = []

    def boom(_it):
        raise RuntimeError('boom')
    out = queue.process_one(r, boom, sleep=waits.append, block=1)
    assert out['status'] == 'dead' and waits == [2, 8]
    assert 'boom' in json.loads(r.lindex(channels.DEAD, 0))['dead_reason']
    # malformed items never crash the consumer either: each goes to the dead letters once
    r.lpush(channels.OUTQ, '{not json')
    assert queue.process_one(r, boom, sleep=waits.append, block=1)['status'] == 'dead'
    r.lpush(channels.OUTQ, json.dumps({'id': 'out-bad', 'event': 'weather'}))
    assert queue.process_one(r, boom, sleep=waits.append, block=1)['status'] == 'dead'
    assert r.llen(channels.DEAD) == 3 and waits == [2, 8]  # malformed items are not retried
