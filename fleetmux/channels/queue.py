"""Outbound queues for channels (P1c; docs/design.md §53, contracts §8): ``fx:channel:outq`` (target not yet
resolved), one ``fx:channel:outq:<type>:<account>`` per account, retries and the dead letter list
``fx:channel:dead`` (notify_bus ``queue/consumer.py``: three attempts, waits 2 s and 8 s, 1000 kept).

- ``process_one(..., block=1)``: ``block > 0`` = ``BRPOP`` with that timeout; ``block == 0`` = non-blocking
  ``RPOP`` (supervisor ruling M10: Redis ``BRPOP`` with timeout 0 blocks forever and is never used as a
  non-blocking read). The caller's Redis client needs ``socket_timeout`` > ``block``.
- Producers ``LPUSH``, the consumer pops from the right: first in, first out.
- Audits (only with ``audit=``): ``channel.send_failed`` per failed attempt, ``channel.dead`` once,
  ``channel.out`` on success, each with ``id / kind (the item's event) / channel / account``; every string
  through ``channels.redact``; ``reply_ref`` never enters an audit.
  An audit sink failure never breaks a send.
"""
import json
import time

from fleetmux import channels
from fleetmux.channels import envelope

RETRY_BACKOFF_S = (2, 8)   # notify_bus consumer: two retries after 2 s and 8 s, three attempts in all
ATTEMPTS = 1 + len(RETRY_BACKOFF_S)
DEAD_KEEP = 1000
AUDIT_TEXT_MAX = 200


def enqueue(r, item):
    """LPUSH one outbound item onto ``fx:channel:outq`` (target not yet resolved)."""
    r.lpush(channels.OUTQ, envelope.dumps(item))
    return item


def route(r, item, target):
    """Fill the resolved target (``channel / account / conversation / reply_ref``) and move the item from
    ``fx:channel:outq`` to ``fx:channel:outq:<type>:<account>`` in one transaction; returns the filled item. An
    item the caller already popped from ``outq`` is simply pushed (the LREM finds nothing)."""
    out = dict(item, channel=target['channel'], account=target['account'],
               conversation=target.get('conversation') or '', reply_ref=dict(target.get('reply_ref') or {}))
    pipe = r.pipeline(transaction=True)
    pipe.lrem(channels.OUTQ, 1, envelope.dumps(item))
    pipe.lpush(channels.OUTQ_ACCOUNT + out['channel'] + ':' + out['account'], envelope.dumps(out))
    pipe.execute()
    return out


def dead_letter(r, item, reason, *, attempts=None):
    """LPUSH a dead letter (newest first) with ``attempts`` / ``dead_reason`` / ``dead_at``; keeps 1000."""
    rec = dict(item) if isinstance(item, dict) else {'raw': str(item)[:2000]}
    if attempts is not None:
        rec['attempts'] = attempts
    rec['dead_reason'] = channels.redact(str(reason or 'unknown'))[:500]
    rec['dead_at'] = time.time()
    pipe = r.pipeline(transaction=True)
    pipe.lpush(channels.DEAD, json.dumps(rec, ensure_ascii=False, sort_keys=True, default=str))
    pipe.ltrim(channels.DEAD, 0, DEAD_KEEP - 1)
    pipe.execute()
    return rec


def _pop(r, key, block):
    if block and block > 0:
        got = r.brpop(key, timeout=block)
        return got[1] if got else None
    return r.rpop(key)


def _audit(audit, event, item, **fields):
    if audit is None:
        return
    # the item's event goes in as ``kind``: ``event`` is audit()'s own first argument
    base = {'id': str(item.get('id') or ''), 'kind': str(item.get('event') or ''),
            'channel': str(item.get('channel') or ''), 'account': str(item.get('account') or '')}
    base.update(fields)
    try:
        audit(event, **channels.redact(base))
    except Exception:  # an audit sink failure must not break a send
        pass


def _send_once(send, item):
    try:
        res = send(item)
    except Exception as exc:  # a crashing adapter is one failed attempt, never a crashed consumer
        return False, None, '%s: %s' % (type(exc).__name__, exc)
    if getattr(res, 'ok', False):
        return True, getattr(res, 'message_id', None), None
    return False, None, str(getattr(res, 'error', None) or 'send failed')


def process_one(r, send, *, sleep=time.sleep, block=1, key=channels.OUTQ, throttle=None, audit=None, beat=None,
                now=None, guard=None):
    """Pop one item from ``key`` and send it: ``{status: sent|dead|empty|suppressed|held|blocked, id, ...}``.

    ``send(item) -> base.SendResult``; an exception is a failed attempt. Malformed items (bad JSON, a non-empty
    ``envelope.outbound_problem``) go to the dead letters once, unretried. ``throttle``: ``throttle.gate``-shaped
    ``throttle(r, item, now=now) -> (verdict, item)``; ``suppressed`` / ``held`` are returned as the status with
    nothing sent. ``beat()``: called before each attempt (a helper only; the actor's own thread writes the
    heartbeat).

    guard (A2'-2, sec-2 request #2): None -> unchanged. Otherwise guard(item) -> item | None is called once
    after the pop and before the first send and before throttle; the returned item is what gets sent.
    None -> return {'status': 'blocked', 'id': <item id>}: no send, no retry, no dead letter, no channel.out (the
    guard writes its own security.* audit and notice; contracts §8 「精确值集与外发被拦」)."""
    raw = _pop(r, key, block)
    if raw is None:
        return {'status': 'empty'}
    try:
        item = json.loads(raw)
    except (TypeError, ValueError):
        rec = dead_letter(r, raw, 'malformed: not JSON', attempts=0)
        _audit(audit, 'channel.dead', {}, reason=rec['dead_reason'], attempts=0)
        return {'status': 'dead', 'id': '', 'reason': rec['dead_reason']}
    problem = envelope.outbound_problem(item)
    if problem:
        rec = dead_letter(r, item, 'malformed: ' + problem, attempts=0)
        _audit(audit, 'channel.dead', item if isinstance(item, dict) else {}, reason=rec['dead_reason'],
               attempts=0)
        return {'status': 'dead', 'id': str(item.get('id') or '') if isinstance(item, dict) else '',
                'reason': rec['dead_reason']}
    item_id = item.get('id') or ''
    if guard is not None:
        guarded = guard(item)
        if guarded is None:
            return {'status': 'blocked', 'id': item_id}
        item = guarded
    if throttle is not None:
        verdict, item = throttle(r, item, now=now)
        if verdict in ('suppressed', 'held'):
            return {'status': verdict, 'id': item_id}
    error = None
    for attempt, backoff in enumerate((0,) + tuple(RETRY_BACKOFF_S), start=1):
        if backoff:
            sleep(backoff)
        if beat is not None:
            try:
                beat()
            except Exception:
                pass
        ok, message_id, error = _send_once(send, item)
        if ok:
            text = item.get('text') if item.get('text') is not None else (item.get('caption') or '')
            _audit(audit, 'channel.out', item, conversation=str(item.get('conversation') or ''),
                   attempts=attempt, message_id=str(message_id or ''),
                   text=channels.redact(str(text))[:AUDIT_TEXT_MAX])
            return {'status': 'sent', 'id': item_id, 'attempts': attempt, 'message_id': message_id}
        _audit(audit, 'channel.send_failed', item, attempt=attempt, error=channels.redact(error)[:500])
    rec = dead_letter(r, item, error, attempts=ATTEMPTS)
    _audit(audit, 'channel.dead', item, attempts=ATTEMPTS, reason=rec['dead_reason'])
    return {'status': 'dead', 'id': item_id, 'attempts': ATTEMPTS, 'error': rec['dead_reason']}
