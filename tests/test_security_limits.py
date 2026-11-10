"""P8n rate limits (brief v2.2 §8.4 test_security_limits.py; sec-3 survey §5.5 U15, U10, C19).

Red in A2' (Limits with a policy raises) except the [baseline] test and the expected PASS of
``test_parse_security_rejects_budget_keys_in_limits_points_to_brake`` (C7: it checks the already frozen P8a
``parse_security``). Real private Redis, an injected clock, no network. A limit trip is a deterministic
refusal: the channel accept answer is ``persisted`` (never ``Retryable``), commands are never limited.
"""
import copy
import inspect
import json

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from cmux_fleet import channels
from cmux_fleet.channels import envelope, limits, security


def policy(**lim):
    pol, errors = security.parse_security('')
    assert not errors
    pol = copy.deepcopy(pol)
    pol['limits'].update(dict(inbound_per_sender_10m=3, requests_per_h=2, outbound_per_h=2, on_trip='pause'))
    pol['limits'].update(lim)
    return pol


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def env(text='hello', sender='111', n=[0]):
    n[0] += 1
    return envelope.make('telegram', 'fleet-bot-1', conversation=sender, message_id=str(n[0]) + ':' + sender,
                         sender=sender, text=text)


def audit_sink(rows):
    return lambda event, **fields: rows.append(dict(fields, event=event))


def test_inbound_per_sender_61st_in_10min_not_posted_rate_limited_receipt(private_redis_v2):
    r = private_redis_v2
    r.flushdb()
    rows = []
    lim = limits.Limits(r, policy(), clock=Clock(), audit=audit_sink(rows))
    assert [lim.inbound(env()) for _ in range(3)] == [True, True, True]
    assert lim.inbound(env()) is False
    tripped = [x for x in rows if x['event'] == 'security.limit_tripped']
    assert tripped and tripped[-1].get('kind') == 'inbound'
    # through the actor: no operator_input for the 4th, a rate_limited receipt, the message counts as persisted
    from cmux_fleet.channels import actor
    submitted, replies = [], []
    out = actor.handle_inbound(r, env(), submit=lambda *a, **k: submitted.append(a),
                               reply=lambda e, text: replies.append(text), policy=policy(), audit=audit_sink(rows))
    assert not submitted and out == 'persisted'
    assert any(envelope.RECEIPTS['rate_limited'].split('（')[0] in str(x) for x in replies)
    # C19: commands are never counted nor limited
    for text in ('/status', '/stop', '/help'):
        assert lim.inbound(env(text=text)) is True


def test_inbound_window_resets_and_other_sender_unaffected(private_redis_v2):
    r = private_redis_v2
    r.flushdb()
    clock = Clock()
    lim = limits.Limits(r, policy(), clock=clock)
    for _ in range(3):
        assert lim.inbound(env()) is True
    assert lim.inbound(env()) is False
    assert lim.inbound(env(sender='222')) is True
    keys = list(r.scan_iter(channels.LIMIT + 'in:*'))
    assert keys and all(0 < r.ttl(k) <= 600 for k in keys)
    clock.t += 601
    for k in keys:                                          # the window is Redis-side: let it expire
        r.delete(k)
    assert lim.inbound(env()) is True


def test_requests_per_hour_31st_refused(private_redis_v2):
    r = private_redis_v2
    r.flushdb()
    lim = limits.Limits(r, policy(), clock=Clock())
    assert lim.request() is True and lim.request() is True
    assert lim.request() is False
    from cmux_fleet.channels import actor                   # U15: counted before submit -> never submitted
    submitted, replies = [], []
    actor.handle_inbound(r, env(text='new request'), submit=lambda *a, **k: submitted.append(a),
                         reply=lambda e, text: replies.append(text), policy=policy(), audit=audit_sink([]))
    assert not submitted and replies


def test_outbound_per_hour_301st_dead_lettered_with_reason_limit(private_redis_v2):
    r = private_redis_v2
    r.flushdb()
    lim = limits.Limits(r, policy(), clock=Clock())
    items = [envelope.outbound('result', target='last', text='done ' + str(i)) for i in range(3)]
    assert [lim.outbound(x) for x in items] == [True, True, False]
    dead = [json.loads(x) for x in r.lrange(channels.DEAD, 0, -1)]
    assert len(dead) == 1 and dead[0].get('dead_reason') == 'limit'
    # U15: an item refused by the limit is not counted again (it never reaches throttle / the send)
    assert lim.outbound(items[2]) is False and len(r.lrange(channels.DEAD, 0, -1)) == 2


def test_trip_pauses_fleet_and_alerts_once_per_hour_when_on_trip_pause(private_redis_v2, monkeypatch):
    r = private_redis_v2
    r.flushdb()
    from cmux_fleet.channels import stop
    calls = []
    real = stop.pause_service
    monkeypatch.setattr(stop, 'pause_service', lambda rr, reason, **kw: (calls.append(reason), real(rr, reason, **kw)))
    lim = limits.Limits(r, policy(requests_per_h=1), clock=Clock())
    assert lim.request() is True and lim.request() is False
    assert len(calls) == 1 and calls[0].startswith('security limit:')
    assert r.hget('fx:service', 'paused') == '1'
    alerts = [json.loads(x) for x in r.lrange(channels.OUTQ, 0, -1) if json.loads(x).get('event') == 'security']
    assert len(alerts) == 1
    assert lim.request() is False                           # same hour: no second alert
    assert len([x for x in r.lrange(channels.OUTQ, 0, -1) if json.loads(x).get('event') == 'security']) == 1
    assert r.hget('fx:service', 'paused') == '1'            # never resumed by the program
    assert not hasattr(security, 'pause')                   # security.py stays configuration only
    assert 'hset' not in inspect.getsource(limits).lower()  # P8n never writes fx:service itself
    r.flushdb()
    lim = limits.Limits(r, policy(requests_per_h=1, on_trip='alert'), clock=Clock())
    assert lim.request() is True and lim.request() is False
    assert r.hget('fx:service', 'paused') != '1'
    assert [x for x in r.lrange(channels.OUTQ, 0, -1) if json.loads(x).get('event') == 'security']


def test_limits_keys_under_channel_prefix_with_ttl(private_redis_v2):
    r = private_redis_v2
    r.flushdb()
    lim = limits.Limits(r, policy(), clock=Clock())
    lim.inbound(env())
    lim.request()
    lim.outbound(envelope.outbound('result', target='last', text='x'))
    new = [k for k in r.scan_iter('*') if k.startswith(channels.LIMIT)]
    assert len(new) >= 3
    assert all(k.startswith('fx:channel:limit:') and r.ttl(k) > 0 for k in new)


def test_parse_security_rejects_budget_keys_in_limits_points_to_brake():
    for key in ('max_usd', 'max_rounds', 'max_hours'):
        pol, errors = security.parse_security('limits:\n  ' + key + ': 5\n')
        assert pol is None and len(errors) == 1
        assert 'budget' in errors[0] and '5' not in errors[0].replace(key, '')


def test_no_policy_no_counters(private_redis_v2):  # [baseline]
    r = private_redis_v2
    r.flushdb()
    rows = []
    lim = limits.Limits(r, None, audit=audit_sink(rows))
    for _ in range(100):
        assert lim.inbound(env()) is True
    assert lim.request() is True
    assert lim.outbound(envelope.outbound('result', target='last', text='x')) is True
    assert not list(r.scan_iter(channels.LIMIT + '*'))
    assert not [x for x in rows if x['event'].startswith('security.')]
