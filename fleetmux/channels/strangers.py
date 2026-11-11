"""B3 untrusted-request marking + A4 stranger alerts (brief v2.2 §8.2 P8d; contracts §8, sec-1 survey §8.18). A2' STUB.

* ``mark_untrusted`` / ``untrusted_kinds``: the ``fx:channel:untrusted:<origin_id>`` marker and its three-state
  reader (``off`` / ``clean`` / ``marked`` / ``unknown``);
* ``fleet_taint`` / ``taint_snapshot``: the split child's taint; the only ancestry check is sec-3's
  ``guard.verify_ancestry`` (injected as ``verify=``; resolved lazily, guard.py lands with P8i);
* ``watch(audit, ...)``: wraps a channel audit callable and counts ``channel.drop_policy`` (one Lua script).

``policy is None`` -> ``watch`` returns ``audit`` unchanged, ``untrusted_kinds`` is ``off`` and ``mark_untrusted``
writes nothing (no security.yaml: behaviour unchanged).
"""
import json
import re
import secrets
import time

from fleetmux import channels
from fleetmux.channels import envelope, provenance

REQ = 'fx:req:'          # = fleet.REQ (the request hash; spec.payload.message is the delivered text)
UNTRUSTED_TTL = 7 * 86400

TAINT_KEY = 'security_taint'
ALLOWED_KINDS = ('forwarded', 'quoted', 'email_body', 'attachment', 'contact_card', 'location', 'caption',
                 'reader_summary')


_MARK = re.compile(r'\[外来内容 kind=([a-z_]+)')
_CONTROL = re.compile(r'[\x00-\x1f\x7f]')


def _valid_rid(request_id):
    return isinstance(request_id, str) and request_id != '' and not _CONTROL.search(request_id)


def _unknown(kinds=()):
    out = [k for k in kinds if k != 'unknown']
    return {'outcome': 'unknown', 'kinds': out + ['unknown']}


def mark_untrusted(r, request_id, kinds, *, policy, audit=None):
    """SET fx:channel:untrusted:<origin_id(request_id)> '<k1,k2>' EX 7 d; audit security.untrusted_request.
    -> True when the key was written. The key is the fast path and long-lived copy; the request text itself carries
    the marker (untrusted_kinds' second path), so a failed write only loses speed, never the tightening."""
    if policy is None:
        return False
    kinds = list(dict.fromkeys(k if k in ALLOWED_KINDS else 'unknown' for k in kinds or ()))
    if not kinds:
        return False
    say = audit or (lambda event, **fields: None)
    if not _valid_rid(request_id):
        _quiet(say, 'security.untrusted_request', request_id='', kinds=','.join(kinds), error='no request id')
        return False
    rid = envelope.origin_id(request_id)
    try:
        r.set(channels.UNTRUSTED + rid, ','.join(kinds), ex=UNTRUSTED_TTL)
    except Exception as exc:  # noqa: BLE001
        _quiet(say, 'security.untrusted_request', request_id=rid, kinds=','.join(kinds),
               error='marker not written: ' + type(exc).__name__)
        return False
    _quiet(say, 'security.untrusted_request', request_id=rid, kinds=','.join(kinds))
    return True


def untrusted_kinds(r, request_id, *, policy):
    """-> {'outcome': 'off' | 'clean' | 'marked' | 'unknown', 'kinds': [...]} (survey §8.18 #1).

    Only 'clean' is clean: the marker key is missing AND fx:req:<origin_id>'s spec.payload.message is a string with
    no '[外来内容'. A missing / malformed key value, a missing request, a wrong spec shape, a marker with no legal
    kind, a bad request id or any read error is 'unknown' (kinds contain 'unknown')."""
    if policy is None:
        return {'outcome': 'off', 'kinds': []}
    if not _valid_rid(request_id):
        return _unknown()
    rid = envelope.origin_id(request_id)
    try:
        value = r.get(channels.UNTRUSTED + rid)
        if value is not None:
            parts = value.split(',') if isinstance(value, str) else []
            if not parts or any(p not in ALLOWED_KINDS and p != 'unknown' for p in parts):
                return _unknown()
            kinds = list(dict.fromkeys(parts))
            return _unknown(kinds) if 'unknown' in kinds else {'outcome': 'marked', 'kinds': kinds}
        spec = r.hget(REQ + rid, 'spec')
    except Exception:  # noqa: BLE001
        return _unknown()
    try:
        spec = json.loads(spec) if isinstance(spec, str) else None
    except ValueError:
        return _unknown()
    payload = spec.get('payload') if isinstance(spec, dict) else None
    message = payload.get('message') if isinstance(payload, dict) else None
    if not isinstance(message, str):
        return _unknown()
    kinds, bad, at = [], False, message.find(provenance.MARK_OPEN)
    while at != -1:
        m = _MARK.match(message, at)
        if m and m.group(1) in ALLOWED_KINDS:
            kinds.append(m.group(1))
        else:
            bad = True
        at = message.find(provenance.MARK_OPEN, at + 1)
    kinds = list(dict.fromkeys(kinds))
    if bad:
        return _unknown(kinds)
    return {'outcome': 'marked', 'kinds': kinds} if kinds else {'outcome': 'clean', 'kinds': []}


def _taint_ok(taint):
    return (isinstance(taint, dict) and isinstance(taint.get('kinds'), list) and taint['kinds']
            and all(isinstance(k, str) for k in taint['kinds']))


def _verify(verify):
    if verify is not None:
        return verify
    from fleetmux import guard  # lazy: guard.py is sec-3's (P8i)
    return guard.verify_ancestry


def fleet_taint(cfg, *, verify=None):
    """-> {'outcome': 'clean' | 'taint' | 'unknown' | 'deny', 'taint', 'reason'}; verify None = guard.verify_ancestry.
    A sub-fleet (parent_config) is checked by ``verify`` first and only there (this never walks the chain): not
    verified or raised -> 'deny'. Then a well-formed security_taint -> 'taint', a malformed one -> 'unknown' with
    {'root_request': '?', 'kinds': ['unknown']}; none -> 'clean'."""
    cfg = cfg if isinstance(cfg, dict) else {}
    if cfg.get('parent_config'):
        try:
            got = _verify(verify)(cfg)
            outcome = getattr(got, 'outcome', None)
        except Exception as exc:  # noqa: BLE001
            return {'outcome': 'deny', 'taint': None, 'reason': 'ancestry check failed: ' + type(exc).__name__}
        if outcome not in ('root', 'verified'):
            return {'outcome': 'deny', 'taint': None,
                    'reason': 'ancestry refused: ' + str(getattr(got, 'reason', '') or outcome)}
    if TAINT_KEY in cfg:
        taint = cfg[TAINT_KEY]
        if _taint_ok(taint):
            return {'outcome': 'taint', 'taint': dict(taint), 'reason': ''}
        return {'outcome': 'unknown', 'taint': {'root_request': '?', 'kinds': ['unknown']}, 'reason': 'malformed taint'}
    return {'outcome': 'clean', 'taint': None, 'reason': ''}


def taint_snapshot(parent_cfg, parent_r, rid, *, clock=time.time, verify=None):
    """-> {'outcome': 'none' | 'taint' | 'refuse', 'taint', 'reason'} (survey §8.18 #3). Inherit first, then no
    policy -> 'none'; a request that is not provably clean -> 'taint'; any failure tightens ('unknown')."""
    from fleetmux.channels import security
    root = envelope.origin_id(rid) if _valid_rid(rid) else '?'
    try:
        ft = fleet_taint(parent_cfg, verify=verify)
        if ft['outcome'] == 'deny':
            return {'outcome': 'refuse', 'taint': None, 'reason': ft['reason']}
        if ft['outcome'] in ('taint', 'unknown'):
            return {'outcome': 'taint', 'taint': ft['taint'], 'reason': ''}
        policy = security.policy(parent_cfg)
        if policy is None:
            return {'outcome': 'none', 'taint': None, 'reason': ''}
        k = untrusted_kinds(parent_r, rid, policy=policy)
        if k['outcome'] == 'clean':
            return {'outcome': 'none', 'taint': None, 'reason': ''}
        kinds = k['kinds'] if k['outcome'] == 'marked' else _unknown(k['kinds'])['kinds']
        return {'outcome': 'taint', 'reason': '',
                'taint': {'root_request': root, 'kinds': kinds, 'from_run': parent_cfg.get('run', ''), 'at': clock()}}
    except Exception as exc:  # noqa: BLE001
        run = parent_cfg.get('run', '') if isinstance(parent_cfg, dict) else ''
        return {'outcome': 'taint', 'reason': 'snapshot failed: ' + type(exc).__name__,
                'taint': {'root_request': root, 'kinds': ['unknown'], 'from_run': run, 'at': time.time()}}


# One script: type pre-check (no write on a wrong type), window count, cooldown, LPUSH, then SET the cooldown
# (survey §8.5, approved). Time comes from the caller (an injectable clock); real TTLs only collect garbage.
_LUA = """
local want = {'hash', 'string', 'list'}
for i = 1, 3 do
  local t = redis.call('TYPE', KEYS[i])['ok']
  if t ~= 'none' and t ~= want[i] then return {-1, 0, KEYS[i]} end
end
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local start = redis.call('HGET', KEYS[1], 'start')
if (not start) or (now - tonumber(start) >= window) then
  redis.call('HSET', KEYS[1], 'start', ARGV[1], 'count', 0)
end
local count = redis.call('HINCRBY', KEYS[1], 'count', 1)
redis.call('EXPIRE', KEYS[1], math.ceil(window) + 60)
if count >= tonumber(ARGV[3]) then
  local last = redis.call('GET', KEYS[2])
  if (not last) or (now - tonumber(last) >= tonumber(ARGV[4])) then
    local item = string.gsub(ARGV[5], ARGV[6], tostring(count), 1)
    redis.call('LPUSH', KEYS[3], item)
    redis.call('SET', KEYS[2], ARGV[1], 'EX', math.ceil(tonumber(ARGV[4])) + 60)
    return {count, 1}
  end
end
return {count, 0}
"""


def _quiet(audit, event, **fields):
    try:
        audit(event, **fields)
    except Exception:  # noqa: BLE001  (an audit sink failure never breaks the caller)
        pass


def _minutes(seconds):
    m = seconds / 60
    return str(int(m)) if m == int(m) else ('%.1f' % m)


def watch(audit, *, r=None, policy=None, type_=None, account=None, clock=time.time):
    """The audit callable with stranger counting. ``policy is None`` -> ``audit`` itself (unchanged).

    Every row goes to ``audit`` first, unchanged. A ``channel.drop_policy`` row also runs one Lua script that counts
    it in fx:channel:strangers:<type>:<account> (hash {start, count}, window from ``clock``), and at
    ``strangers.alert_after`` within ``window_s`` LPUSHes one ``security`` alert for the owner (user_only, target
    last) unless fx:channel:pushed:security:strangers:<type>:<account> holds an alert younger than ``cooldown_s``.
    Nothing is sent to the stranger; no message text is counted or quoted. Never raises."""
    if policy is None:
        return audit
    conf = policy.get('strangers') or {}
    keys = [channels.STRANGERS + str(type_) + ':' + str(account),
            channels.PUSHED + 'security:strangers:' + str(type_) + ':' + str(account), channels.OUTQ]
    state = {}

    def script():
        if 'script' not in state:
            state['script'] = r.register_script(_LUA)
        return state['script']

    def wrapped(event, **fields):
        _quiet(audit, event, **fields)
        if event != 'channel.drop_policy' or r is None:
            return
        try:
            marker = '@@FMXCOUNT' + secrets.token_hex(4) + '@@'
            sender = str(fields.get('sender') or '?')[:4]
            text = (str(account) + ' ' + _minutes(conf.get('window_s', 600)) + ' 分钟内 ' + marker
                    + ' 条非白名单来信（最近一位 sender 前 4 位：' + sender + '…）')
            template = envelope.dumps(envelope.outbound('security', target='last', user_only=True, text=text))
            if template.count(marker) != 1:
                return
            got = script()(keys=keys, args=[repr(float(clock())), conf.get('window_s', 600),
                                             conf.get('alert_after', 5), conf.get('cooldown_s', 3600),
                                             template, marker])
        except Exception:  # noqa: BLE001  (Redis down: swallowed; the audit row itself already went through)
            return
        if int(got[0]) == -1:
            _quiet(audit, 'channel.error', kind='bad_config', error='stranger counter key has the wrong type')
        elif int(got[1]) == 1:
            _quiet(audit, 'security.stranger_alert', count=int(got[0]), window_s=conf.get('window_s', 600))
    return wrapped
