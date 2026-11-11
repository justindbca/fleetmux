"""Outbound throttle for channels (P1c; docs/design.md §53, contracts §8): dedupe within a short window and storm
merging, the algorithm of notify_bus ``throttle.py`` (its async path) behind a synchronous Redis interface, keys
under ``fx:channel:throttle:``.

- ``gate(r, item, *, now=None) -> (verdict, item)``: verdict ``send`` / ``suppressed`` / ``held``; the twelfth
  item of a storm is sent rewritten as the merge notice; ``reply`` and ``user_only`` items are never held and
  their dedupe key includes the item id (distinct receipts are never swallowed).
- ``flush_items(r, *, now=None)``: the summary items of storms whose hold time is up.

Storm bucket = ``<channel>:<account>:<conversation>`` (notify_bus: per sender). ``now=`` drives the storm window
and hold times; the ``EX`` expiries (dedupe window, hold record TTL) run on the Redis clock. Not ported:
``is_too_old`` (a late result must still go out), the sync-path circuit breaker, metrics, business gates.
"""
import hashlib
import json
import time
import uuid

from fleetmux import channels

KEY_PREFIX = channels.KEY_PREFIX + 'throttle:'  # 'fx:channel:throttle:'
THROTTLE_WINDOW = 5         # s: the same text into the same conversation twice inside this window -> suppressed
STORM_WINDOW = 120          # s: sliding window of the storm counter
STORM_THRESHOLD = 12        # the item that makes the window count reach this is sent as the merge notice
STORM_FLUSH_INTERVAL = 120  # s: one summary per held storm after this long
STORM_HOLD_TTL = 600        # s: the hold record's TTL (Redis clock)

DEDUPE = KEY_PREFIX + 'dedupe:'          # + hash; SET NX EX THROTTLE_WINDOW
STORM = KEY_PREFIX + 'storm:'            # + bucket; ZSET of sends, score = now
STORM_HOLD = KEY_PREFIX + 'storm_hold:'  # + bucket; hash n / latest / event / announced / since / channel / ...


def _hash(*parts):
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p).encode('utf-8'))
        h.update(b'|')
    return h.hexdigest()[:16]


def _now(now):
    return time.time() if now is None else float(now)


def is_throttled(r, key_parts, *, window=None):
    """True when the same ``key_parts`` were seen inside ``window`` seconds (SET NX EX: the first one wins)."""
    window = THROTTLE_WINDOW if window is None else window
    return not r.set(DEDUPE + _hash(*key_parts), '1', ex=max(1, int(window)), nx=True)


def storm_record_and_check(r, bucket, *, now=None, threshold=None, window=None):
    """Record one send into ``bucket``; returns (storm, count in the window)."""
    now = _now(now)
    threshold = STORM_THRESHOLD if threshold is None else threshold
    window = STORM_WINDOW if window is None else window
    key = STORM + bucket
    pipe = r.pipeline()
    pipe.zadd(key, {'%r:%s' % (now, uuid.uuid4().hex[:12]): now})
    pipe.zremrangebyscore(key, 0, now - window)
    pipe.zcard(key)
    pipe.expire(key, int(window) + 5)
    _, _, count, _ = pipe.execute()
    return count >= threshold, int(count)


def storm_hold(r, bucket, event, text, *, now=None):
    """Count one storm item into the bucket's hold record; returns its number (1 = the merge notice, sent; 2.. =
    held). ``since`` is the time of the first held item: the summary goes out STORM_FLUSH_INTERVAL after it."""
    now = _now(now)
    key = STORM_HOLD + bucket
    n = int(r.hincrby(key, 'n', 1))
    pipe = r.pipeline()
    if n == 1:
        pipe.hsetnx(key, 'announced', now)
    else:
        pipe.hset(key, mapping={'latest': (text or '')[:500], 'event': event or 'result'})
        pipe.hsetnx(key, 'since', now)
    pipe.expire(key, STORM_HOLD_TTL)
    pipe.execute()
    return n


def _hold_keys(r):
    return list(r.scan_iter(match=STORM_HOLD + '*', count=100))


def _collect(r, now):
    out = []
    for key in _hold_keys(r):
        h = r.hgetall(key)
        if not h:
            continue
        try:
            n = int(h.get('n') or 0)
            start = float(h.get('since') or h.get('announced') or 0)
        except (TypeError, ValueError):
            r.delete(key)
            continue
        if now - start < STORM_FLUSH_INTERVAL:
            continue
        r.delete(key)
        if n <= 1:  # only the merge notice: nothing held, nothing to sum up
            continue
        out.append((key[len(STORM_HOLD):], h))
    return out


def storm_collect_flushes(r, *, now=None):
    """Hold records due for their summary, deleted as they are collected: [(bucket, event, n held, latest)]."""
    return [(bucket, h.get('event') or 'result', int(h['n']) - 1, h.get('latest') or '')
            for bucket, h in _collect(r, _now(now))]


def _body(item):
    if item.get('text') is not None:
        return item['text']
    if item.get('card') is not None:
        return json.dumps(item['card'], ensure_ascii=False, sort_keys=True)
    return str(item.get('file') or '')


def _never_held(item):
    return item.get('event') == 'reply' or bool(item.get('user_only'))


def bucket_of(item):
    return '%s:%s:%s' % (item.get('channel') or '', item.get('account') or '', item.get('conversation') or '')


def gate(r, item, *, now=None):
    """(verdict, item to send). Dedupe first (suppressed items do not count), then the storm counter."""
    now = _now(now)
    body = _body(item)
    parts = [item.get('channel') or '', item.get('account') or '', item.get('conversation') or '',
             item.get('event') or '', body, json.dumps(item.get('reply_ref') or {}, sort_keys=True)]
    if _never_held(item):
        parts.append(item.get('id') or '')  # the same item again is suppressed, a distinct receipt never
    if is_throttled(r, parts):
        return 'suppressed', item
    bucket = bucket_of(item)
    storm, count = storm_record_and_check(r, bucket, now=now)
    if not storm or _never_held(item):
        return 'send', item
    n = storm_hold(r, bucket, item.get('event'), body, now=now)
    if n > 1:
        return 'held', item
    r.hset(STORM_HOLD + bucket, mapping={'channel': item.get('channel') or '', 'account': item.get('account') or '',
                                         'conversation': item.get('conversation') or ''})
    notice = ('⚠️ %d 秒内已 %d 条，触发风暴合并：后续每 %d 秒汇总一条。本条：%s'
              % (STORM_WINDOW, count, STORM_FLUSH_INTERVAL, body[:200]))
    return 'send', dict(item, text=notice, card=None, file=None, caption='', update=None)


def flush_items(r, *, now=None):
    """The summary items of storms whose hold time is up (target explicit, the bucket's conversation, a new
    message: no reply_ref). The caller sends them like any other item."""
    from fleetmux.channels import envelope
    out = []
    for bucket, h in _collect(r, _now(now)):
        channel, account, conversation = h.get('channel'), h.get('account'), h.get('conversation')
        if not (channel and account and conversation):
            channel, account, conversation = (bucket.split(':', 2) + ['', '', ''])[:3]
        event = h.get('event') or 'result'
        if event not in channels.OUT_EVENTS:
            event = 'manual'
        text = '📊 风暴合并：%d 条已合并，最新一条：%s' % (int(h['n']) - 1, (h.get('latest') or '')[:200])
        try:
            out.append(envelope.outbound(event, text=text, target='explicit', channel=channel, account=account,
                                         conversation=conversation))
        except ValueError:
            continue
    return out
