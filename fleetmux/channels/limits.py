"""P8n rate limits (brief v2.2 §8.2 P8n; U10 / U15 / C19 rulings).

Without a policy every check lets the message through and writes nothing (behaviour unchanged). With one, three
fixed-window counters under ``fx:channel:limit:`` (INCR, EXPIRE set once when the key is new, never refreshed):
inbound per sender (10 min), requests (1 h), outbound items (1 h). A trip is a deterministic refusal: inbound /
request -> no submit, a ``rate_limited`` receipt; outbound -> dead letter with ``dead_reason=limit``. Any trip
also queues one ``security`` alert per hour and, with ``on_trip: pause``, pauses the fleet through P8m's
``stop.pause_service`` (this module never writes ``fx:service`` itself). Commands are never counted (C19).
"""
import json
import time

from fleetmux import channels
from fleetmux.channels import envelope

IN_WINDOW_S = 600
HOUR_S = 3600
ALERT_KEY = channels.LIMIT + 'alerted'
_COUNT_LUA = "local n = redis.call('INCR', KEYS[1]) if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end return n"


class Limits:
    def __init__(self, r, policy, *, clock=time.time, audit=None):
        self.r, self.policy, self.clock, self.audit = r, policy, clock, audit

    def _limits(self):
        return (self.policy or {}).get('limits') or {}

    def _count(self, key, window):
        return int(self.r.eval(_COUNT_LUA, 1, key, window))

    def _audit(self, event, **fields):
        if self.audit is not None:
            self.audit(event, **fields)

    def _trip(self, kind, limit, **fields):
        self._audit('security.limit_tripped', kind=kind, limit=limit, **fields)
        if self.r.set(ALERT_KEY, str(self.clock()), nx=True, ex=HOUR_S):
            text = 'Fleet：' + kind + ' 频率超限（上限 ' + str(limit) + '）'
            if self._limits().get('on_trip') == 'pause':
                text += '，fleet 已暂停，需人工恢复'
            item = envelope.outbound('security', target='last', user_only=True, text=text)
            self.r.lpush(channels.OUTQ, json.dumps(item, ensure_ascii=False))
        if self._limits().get('on_trip') == 'pause' and self.r.hget('fx:service', 'paused') != '1':
            from fleetmux.channels import stop
            try:
                stop.pause_service(self.r, 'security limit: ' + kind + ' over ' + str(limit))
            except Exception as exc:  # noqa: BLE001 — the refusal stands even when the pause cannot be written
                self._audit('security.limit_tripped', kind=kind, limit=limit, pause='failed: ' + type(exc).__name__)
        return False

    def inbound(self, env):
        if self.policy is None:
            return True
        if envelope.parse_command(env.get('text') or '')['kind'] != 'text':
            return True                                      # C19: commands are never counted nor limited
        limit = int(self._limits().get('inbound_per_sender_10m') or 0)
        key = (channels.LIMIT + 'in:' + str(env.get('channel', '')) + ':' + str(env.get('account', '')) + ':'
               + str(env.get('sender', '')))
        if limit and self._count(key, IN_WINDOW_S) > limit:
            return self._trip('inbound', limit, sender=str(env.get('sender', ''))[:4] + '…')
        return True

    def request(self):
        if self.policy is None:
            return True
        limit = int(self._limits().get('requests_per_h') or 0)
        if limit and self._count(channels.LIMIT + 'req', HOUR_S) > limit:
            return self._trip('request', limit)
        return True

    def outbound(self, item=None):
        if self.policy is None:
            return True
        limit = int(self._limits().get('outbound_per_h') or 0)
        if limit and self._count(channels.LIMIT + 'out', HOUR_S) > limit:
            dead = dict(item or {}, dead_reason='limit')
            self.r.lpush(channels.DEAD, json.dumps(dead, ensure_ascii=False, default=str))
            self.r.ltrim(channels.DEAD, 0, 999)
            return self._trip('outbound', limit, id=str((item or {}).get('id', '')))
        return True


def admit(r, env, policy, audit, reply):
    """actor._dispatch, before submit: inbound per sender, then the request count. False -> receipt sent."""
    if policy is None:
        return True
    lim = Limits(r, policy, audit=audit)
    if lim.inbound(env) and lim.request():
        return True
    reply(env, envelope.receipt('rate_limited', reason='请稍后再发'))
    return False


def outbound_guard(guard, r, policy, audit):
    """actor's outbound guard chain plus the outbound limit (after secrets / links, before throttle and the send).
    No policy -> ``guard`` unchanged (None stays None)."""
    if policy is None:
        return guard
    lim = Limits(r, policy, audit=audit)

    def limited(item):
        item = guard(item) if guard is not None else item
        if item is None:
            return None
        return item if lim.outbound(item) else None
    return limited
