"""Bounded v2 transport API for one private Redis run.

Message IDs, delivery IDs and Stream IDs are distinct. Inbox entries remain in
PEL until a validated terminal commit or an explicit confirmed-stop resolution.
Expired/uncertain work holds its slot; recovery never transfers execution to a
new consumer. Live entries and dedup data are never expired/trimmed. A confirmed
stop removes its terminal inbox entry so an unread offer cannot later execute.
Lua preflights key types, identities and groups before writing: Redis script
errors do not roll back writes. Infrastructure failures still require inspection;
this API makes no exactly-once guarantee for external tool effects.
"""

import json
import re
import time
import uuid

import redis

P = "fx:v2"
MAX_BODY = 200000  # UTF-8 bytes, for either body or result
MAX_KEY = 128
DEFAULT_BLOCK_MS = 1000


class BusError(RuntimeError):
    """Rejected identity/state or failed Redis operation; caller must inspect."""


class AuditError(BusError):
    """Audit failure is visible and prevents a normal transition."""


def _ms():
    return int(time.time() * 1000)


def _short(value, name):
    if (not isinstance(value, str) or not value or len(value) > MAX_KEY
            or not re.fullmatch(r"[A-Za-z0-9_.-]+", value)):
        raise ValueError("%s must be 1-%d identity characters" % (name, MAX_KEY))
    return value


def _request_id(value):
    """Service request correlation is not a slot or an internal Redis key ID."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}", value):
        raise ValueError("request_id must match the service ID format (1-128 chars)")
    return value


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum or value > 2**53 - 1:
        raise ValueError("%s must be an integer >= %d" % (name, minimum))
    return value


def _json(value):
    if not isinstance(value, dict):
        raise ValueError("body/result must be a dict")
    def validate(item):
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("JSON object keys must be strings")
            for sub in item.values():
                validate(sub)
        elif isinstance(item, list):
            for sub in item:
                validate(sub)
        elif item is not None and type(item) not in (str, int, float, bool):
            raise ValueError("body/result must contain JSON types")
    try:
        validate(value)
        out = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
        if len(out.encode("utf-8")) > MAX_BODY:
            raise ValueError("body/result exceeds %d UTF-8 bytes" % MAX_BODY)
    except (TypeError, OverflowError, RecursionError, UnicodeError) as exc:
        raise ValueError("invalid JSON body/result") from exc
    return out


# Common functions only read state until audit() or a transition explicitly writes.
LUA_HEAD = """
local function keytype(key, want)
  local actual = redis.call('TYPE', key).ok
  if actual ~= 'none' and actual ~= want then
    error((want == 'stream' and string.find(key, ':audit', 1, true)
      and 'AUDIT ' or '') .. 'wrong_type:' .. key .. ':' .. actual)
  end
end
local function nowms()
  local t = redis.call('TIME')
  return tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
end
local function groupcheck(stream, group)
  if redis.call('TYPE', stream).ok ~= 'stream' then error('missing_inbox') end
  for _, g in ipairs(redis.call('XINFO', 'GROUPS', stream)) do
    for i=1,#g,2 do
      if g[i] == 'name' and g[i+1] == group then return end
    end
  end
  error('missing_group')
end
local function audit(key, event, slot, mid, did, at)
  local got = redis.pcall('XADD', key, '*', 'event', event, 'slot', slot,
    'run_id', ARGV[1], 'message_id', mid or '', 'delivery_id', did or '', 'at', at)
  if type(got) == 'table' and got.err then error('AUDIT ' .. got.err) end
end
local function envelope(mkey)
  local all = redis.call('HGETALL', mkey)
  local out = {}
  for i=1,#all,2 do out[all[i]] = all[i+1] end
  return cjson.encode(out)
end
-- ARGV[1..8] are run/slot/generation/session/message/delivery/stream/fence.
local function messagecheck(mkey)
  local names = {'run_id','slot','generation','session_id','message_id',
                 'delivery_id','stream_id','fence'}
  for i,name in ipairs(names) do
    if redis.call('HGET', mkey, name) ~= ARGV[i] then
      error('identity_mismatch:' .. name)
    end
  end
end
local function activecheck(lease, mkey)
  messagecheck(mkey)
  local names = {'run_id','slot','generation','session_id','message_id',
                 'current_delivery','stream_id','fence'}
  for i,name in ipairs(names) do
    if redis.call('HGET', lease, name) ~= ARGV[i] then
      error('stale_identity:' .. name)
    end
  end
end
local function activekeys()
  keytype(KEYS[1], 'hash'); keytype(KEYS[2], 'hash')
  keytype(KEYS[3], 'stream'); keytype(KEYS[4], 'stream')
  groupcheck(KEYS[3], ARGV[9])
end
local function expired(lease, mkey, at)
  return at >= tonumber(redis.call('HGET', mkey, 'deadline_at'))
    or at >= tonumber(redis.call('HGET', lease, 'lease_until'))
end
local function hold(state, at)
  audit(KEYS[4], state, ARGV[2], ARGV[5], ARGV[6], at)
  redis.call('HSET', KEYS[1], 'status', state)
  redis.call('HSET', KEYS[2], 'status', state)
end
local function release(outcome, result, at)
  redis.call('HSET', KEYS[2], 'status', 'committed', 'outcome', outcome,
    'result', result, 'committed_at', at)
  redis.call('XACK', KEYS[3], ARGV[9], ARGV[7])
  redis.call('HSET', KEYS[1], 'status', 'idle', 'current_delivery', '',
    'message_id', '', 'stream_id', '', 'consumer', '', 'lease_until', 0)
end
"""

LUA_REGISTER = LUA_HEAD + """
keytype(KEYS[1], 'hash'); keytype(KEYS[2], 'stream'); keytype(KEYS[3], 'stream')
local old = redis.call('HGET', KEYS[1], 'generation')
local answer = 'registered'
if old then
  if redis.call('HGET', KEYS[1], 'run_id') ~= ARGV[1] then error('foreign_run') end
  if old == ARGV[3] and redis.call('HGET', KEYS[1], 'session_id') == ARGV[4] then
    groupcheck(KEYS[2], ARGV[5]); return 'ok'
  end
  if tonumber(ARGV[3]) <= tonumber(old) then error('old_generation') end
  if redis.call('HGET', KEYS[1], 'current_delivery') ~= '' then error('busy') end
  groupcheck(KEYS[2], ARGV[5])
  if redis.call('XPENDING', KEYS[2], ARGV[5])[1] > 0 then error('undrained_pel') end
  answer = 'replaced'
end
local fence = tonumber(redis.call('HGET', KEYS[1], 'fence') or '0') + 1
local at = nowms()
audit(KEYS[3], 'slot_' .. answer, ARGV[2], '', '', at)
if not old then
  redis.call('XGROUP', 'CREATE', KEYS[2], ARGV[5], '0-0', 'MKSTREAM')
end
redis.call('HSET', KEYS[1], 'run_id', ARGV[1], 'slot', ARGV[2],
  'generation', ARGV[3], 'session_id', ARGV[4], 'fence', fence, 'attempt', 0,
  'current_delivery', '', 'message_id', '', 'stream_id', '', 'consumer', '',
  'lease_until', 0, 'status', 'idle')
return answer
"""

# Shared by the standalone bus operation and RuntimeV2's atomic effect application.
LUA_REBIND_CHECK = """
local function rebindcheck(lease, inbox, group, run, slot, generation, old_session, fence)
  local expected = {run_id=run, slot=slot, generation=tostring(generation),
    session_id=old_session, fence=tostring(fence), current_delivery='', status='idle'}
  for field, value in pairs(expected) do
    if redis.call('HGET', lease, field) ~= value then error('rebind_identity:' .. field) end
  end
  if redis.call('XPENDING', inbox, group)[1] ~= 0 then error('undrained_pel') end
end
"""

LUA_REBIND = LUA_HEAD + LUA_REBIND_CHECK + """
keytype(KEYS[1], 'hash'); keytype(KEYS[2], 'stream'); keytype(KEYS[3], 'stream')
groupcheck(KEYS[2], ARGV[7])
rebindcheck(KEYS[1], KEYS[2], ARGV[7], ARGV[1], ARGV[2], ARGV[3], ARGV[4], ARGV[6])
if ARGV[4] == ARGV[5] then error('rebind_same_session') end
local fence = tonumber(ARGV[6]) + 1
audit(KEYS[3], 'slot_rebound', ARGV[2], '', '', nowms())
redis.call('HSET', KEYS[1], 'session_id', ARGV[5], 'fence', fence)
return fence
"""

LUA_ENQUEUE = LUA_HEAD + """
keytype(KEYS[1], 'list'); keytype(KEYS[2], 'hash'); keytype(KEYS[3], 'stream')
local old = redis.call('HGET', KEYS[2], 'status')
if old then
  local names = {'run_id','slot','message_id','body','request_id','kind'}
  for i,name in ipairs(names) do
    if redis.call('HGET', KEYS[2], name) ~= ARGV[i] then error('enqueue_conflict') end
  end
  if ARGV[7] ~= '' and redis.call('HGET', KEYS[2], 'deadline_at') ~= ARGV[7] then
    error('enqueue_conflict:deadline')
  end
  return 'exists'
end
if redis.call('EXISTS', KEYS[2]) == 1 then error('incomplete_message') end
local at = nowms()
local deadline = tonumber(ARGV[7]) or (at + tonumber(ARGV[8]))
if deadline <= at then error('deadline_passed') end
audit(KEYS[3], 'queued', ARGV[2], ARGV[3], '', at)
redis.call('HSET', KEYS[2], 'run_id', ARGV[1], 'slot', ARGV[2],
  'message_id', ARGV[3], 'body', ARGV[4], 'request_id', ARGV[5], 'kind', ARGV[6],
  'status', 'queued', 'deadline_at', deadline, 'created_at', at, 'attempt', 0)
redis.call('RPUSH', KEYS[1], ARGV[3])
return 'queued'
"""

LUA_OFFER = LUA_HEAD + """
keytype(KEYS[1], 'list'); keytype(KEYS[2], 'hash')
keytype(KEYS[3], 'stream'); keytype(KEYS[4], 'stream')
groupcheck(KEYS[3], ARGV[3])
if redis.call('HGET', KEYS[2], 'run_id') ~= ARGV[1] then error('no_slot_or_foreign_run') end
if redis.call('HGET', KEYS[2], 'current_delivery') ~= '' then error('busy') end
if redis.call('HGET', KEYS[2], 'status') ~= 'idle' then error('nonidle_slot') end
local mid = redis.call('LINDEX', KEYS[1], 0)
if not mid then return '' end
-- Named offers still obey FIFO; no second scheduling policy.
if ARGV[4] ~= '' and mid ~= ARGV[4] then error('not_fifo_head') end
local mk = ARGV[5] .. mid
keytype(mk, 'hash')
if redis.call('HGET', mk, 'status') ~= 'queued' then error('missing_or_nonqueued_message') end
if redis.call('HGET', mk, 'slot') ~= ARGV[2]
  or redis.call('HGET', mk, 'run_id') ~= ARGV[1]
  or redis.call('HGET', mk, 'message_id') ~= mid then error('message_identity_mismatch') end
local deadline = tonumber(redis.call('HGET', mk, 'deadline_at'))
local fence = tonumber(redis.call('HGET', KEYS[2], 'fence'))
local attempt = tonumber(redis.call('HGET', mk, 'attempt'))
if not deadline or not fence or not attempt then error('invalid_numeric_state') end
local at = nowms()
if at >= deadline then
  -- No delivery was authorized, so there is no execution to hold or stop.
  -- Keep terminal/dedup evidence; dequeue only after validation and audit.
  audit(KEYS[4], 'queued_expired', ARGV[2], mid, '', at)
  redis.call('HSET', mk, 'status', 'committed', 'outcome', 'expired',
    'result', '{}', 'committed_at', at, 'expired_before_offer', 1)
  redis.call('LPOP', KEYS[1])
  return 'expired_queued'
end
local gen = redis.call('HGET', KEYS[2], 'generation')
local session = redis.call('HGET', KEYS[2], 'session_id')
if not gen or not session then error('missing_slot_identity') end
fence = fence + 1; attempt = attempt + 1
audit(KEYS[4], 'offered', ARGV[2], mid, ARGV[6], at)
local sid = redis.call('XADD', KEYS[3], '*', 'run_id', ARGV[1], 'slot', ARGV[2],
  'message_id', mid, 'delivery_id', ARGV[6], 'generation', gen,
  'session_id', session, 'fence', fence, 'attempt', attempt)
redis.call('HSET', mk, 'status', 'offered', 'delivery_id', ARGV[6], 'stream_id', sid,
  'generation', gen, 'session_id', session, 'fence', fence, 'attempt', attempt,
  'offered_at', at, 'consumer', '')
redis.call('HSET', KEYS[2], 'status', 'offered', 'message_id', mid,
  'current_delivery', ARGV[6], 'stream_id', sid, 'fence', fence, 'attempt', attempt,
  'lease_until', math.min(at + tonumber(ARGV[7]), deadline), 'consumer', '')
redis.call('LPOP', KEYS[1])
return envelope(mk)
"""

LUA_RECEIVE = LUA_HEAD + """
activekeys(); activecheck(KEYS[1], KEYS[2])
local pending = redis.call('XPENDING', KEYS[3], ARGV[9], ARGV[7], ARGV[7], 1)
if #pending ~= 1 or pending[1][2] ~= ARGV[10] then error('not_pending_for_consumer') end
local owner = redis.call('HGET', KEYS[1], 'consumer') or ''
if owner ~= '' and owner ~= ARGV[10] then error('protected_consumer') end
local state = redis.call('HGET', KEYS[1], 'status')
local at = nowms()
if state == 'uncertain' or state == 'expired' then error('held:' .. state) end
if expired(KEYS[1], KEYS[2], at) then hold('expired', at); return 'expired' end
if state ~= 'offered' and state ~= 'received' then error('invalid_receive_state') end
if state == 'received' then return envelope(KEYS[2]) end
audit(KEYS[4], 'received', ARGV[2], ARGV[5], ARGV[6], at)
redis.call('HSET', KEYS[1], 'status', 'received', 'consumer', ARGV[10])
redis.call('HSET', KEYS[2], 'status', 'received', 'consumer', ARGV[10], 'received_at', at)
return envelope(KEYS[2])
"""

LUA_RENEW = LUA_HEAD + """
activekeys(); activecheck(KEYS[1], KEYS[2])
if redis.call('HGET', KEYS[1], 'consumer') ~= ARGV[10] then error('protected_consumer') end
local state = redis.call('HGET', KEYS[1], 'status')
if state ~= 'received' then error('not_renewable:' .. tostring(state)) end
local at = nowms()
if expired(KEYS[1], KEYS[2], at) then hold('expired', at); return 'expired' end
local until_ = math.min(at + tonumber(ARGV[11]),
  tonumber(redis.call('HGET', KEYS[2], 'deadline_at')))
until_ = math.max(until_, tonumber(redis.call('HGET', KEYS[1], 'lease_until')))
audit(KEYS[4], 'renewed', ARGV[2], ARGV[5], ARGV[6], at)
redis.call('HSET', KEYS[1], 'lease_until', until_)
return 'renewed'
"""

# §24: an official CLI's lease is renewed by the daemon itself, so one that ran out while the work deadline did
# not only proves the daemon did not run. Same checks as LUA_RENEW minus the lease_until test; nothing is written
# when the deadline has passed (the caller then holds as before).
LUA_RECLAIM = LUA_HEAD + """
activekeys(); activecheck(KEYS[1], KEYS[2])
if redis.call('HGET', KEYS[1], 'consumer') ~= ARGV[10] then error('protected_consumer') end
local state = redis.call('HGET', KEYS[1], 'status')
if state ~= 'received' then error('not_renewable:' .. tostring(state)) end
local at = nowms()
local deadline = tonumber(redis.call('HGET', KEYS[2], 'deadline_at'))
if at >= deadline then return 'deadline' end
local until_ = math.max(math.min(at + tonumber(ARGV[11]), deadline),
  tonumber(redis.call('HGET', KEYS[1], 'lease_until')))
audit(KEYS[4], 'reclaimed', ARGV[2], ARGV[5], ARGV[6], at)
redis.call('HSET', KEYS[1], 'lease_until', until_)
return 'reclaimed'
"""

LUA_COMMIT = LUA_HEAD + """
activekeys(); messagecheck(KEYS[2])
if redis.call('HGET', KEYS[2], 'consumer') ~= ARGV[10] then error('protected_consumer') end
-- Dedup checks the immutable message authorization before the mutable slot lease.
if redis.call('HGET', KEYS[2], 'status') == 'committed' then
  if redis.call('HGET', KEYS[2], 'outcome') ~= ARGV[11]
    or redis.call('HGET', KEYS[2], 'result') ~= ARGV[12] then error('commit_conflict') end
  return 'already'
end
activecheck(KEYS[1], KEYS[2])
if redis.call('HGET', KEYS[1], 'consumer') ~= ARGV[10] then error('protected_consumer') end
local state = redis.call('HGET', KEYS[1], 'status')
if state ~= 'received' then error('not_committable:' .. tostring(state)) end
local pend = redis.call('XPENDING', KEYS[3], ARGV[9], ARGV[7], ARGV[7], 1)
if #pend ~= 1 or pend[1][2] ~= ARGV[10] then error('not_pending_for_consumer') end
local at = nowms()
if expired(KEYS[1], KEYS[2], at) then hold('expired', at); return 'expired' end
audit(KEYS[4], 'committed', ARGV[2], ARGV[5], ARGV[6], at)
release(ARGV[11], ARGV[12], at)
return 'committed'
"""

LUA_HOLD = LUA_HEAD + """
activekeys(); activecheck(KEYS[1], KEYS[2])
local state = redis.call('HGET', KEYS[1], 'status')
if state == 'uncertain' or state == 'expired' then return state end
local at = nowms()
local nextstate = expired(KEYS[1], KEYS[2], at) and 'expired' or 'uncertain'
hold(nextstate, at)
redis.call('HSET', KEYS[2], 'uncertain_reason', ARGV[10])
return nextstate
"""

LUA_RESOLVE = LUA_HEAD + """
activekeys(); messagecheck(KEYS[2])
if redis.call('HGET', KEYS[2], 'status') == 'committed' then
  if redis.call('HGET', KEYS[2], 'stop_confirmed') ~= '1'
    or redis.call('HGET', KEYS[2], 'outcome') ~= ARGV[10] then error('resolution_conflict') end
  return 'already'
end
activecheck(KEYS[1], KEYS[2])
local state = redis.call('HGET', KEYS[1], 'status')
if state ~= 'uncertain' and state ~= 'expired' then error('not_held') end
local at = nowms()
audit(KEYS[4], 'stop_resolved', ARGV[2], ARGV[5], ARGV[6], at)
release(ARGV[10], '{}', at)
redis.call('HSET', KEYS[2], 'stop_confirmed', 1)
-- XACK cannot suppress an offer that has never entered PEL. Remove only this
-- confirmed-stopped terminal entry, not any live or unrelated delivery.
redis.call('XDEL', KEYS[3], ARGV[7])
return 'resolved'
"""


class MessageBus:
    """Use a decoded Unix-socket Redis client. Scheduling/acceptance stays in daemon.

    receive requires the adapter's fixed generation/session identity. renew/commit/
    mark_uncertain/resolve_uncertain require the full offer/receive envelope. On a
    recovered envelope execution_allowed=False: reconcile, do not run tools again.
    commit is for already validated results; external public-result/UI publication
    is outside this standalone module and still needs fleet integration.
    """

    def __init__(self, r, run_id, lease_ms=300000, deadline_ms=3600000):
        self.r = r
        self.run_id = _short(run_id, "run_id")
        options = r.connection_pool.connection_kwargs
        if not options.get("decode_responses") or not options.get("path"):
            raise ValueError("decoded private Unix-socket Redis client required")
        self.lease_ms = _integer(lease_ms, "lease_ms", 100)
        self.deadline_ms = _integer(deadline_ms, "deadline_ms", 100)
        self.prefix = P + ":run:" + self.run_id
        self.audit_key = self.prefix + ":audit"

    def pending_key(self, slot):
        return self.prefix + ":pending:" + _short(slot, "slot")

    def msg_key(self, message_id):
        return self.prefix + ":message:" + _short(message_id, "message_id")

    def lease_key(self, slot):
        return self.prefix + ":lease:" + _short(slot, "slot")

    def inbox_key(self, slot):
        return self.prefix + ":inbox:" + _short(slot, "slot")

    def slot_group(self, slot):
        return _short(slot, "slot") + "/agents"

    def _call(self, script, keys, argv):
        try:
            return self.r.eval(script, len(keys), *(keys + argv))
        except redis.RedisError as exc:
            if "AUDIT " in str(exc):
                raise AuditError(str(exc)) from exc
            raise BusError(str(exc)) from exc

    def _decode(self, raw):
        out = json.loads(raw)
        for key in ("generation", "fence", "attempt", "deadline_at", "created_at",
                    "offered_at", "received_at", "committed_at"):
            if key in out:
                out[key] = int(out[key])
        out["body"] = json.loads(out["body"])
        if "result" in out:
            out["result"] = json.loads(out["result"])
        out.update(version=2, stream_key=self.inbox_key(out["slot"]),
                   group=self.slot_group(out["slot"]))
        return out

    def _authorization(self, slot, envelope):
        _short(slot, "slot")
        if not isinstance(envelope, dict):
            raise ValueError("full delivery envelope required")
        values = []
        for key in ("run_id", "slot", "generation", "session_id", "message_id",
                    "delivery_id", "stream_id", "fence"):
            value = envelope.get(key)
            if key in ("generation", "fence"):
                _integer(value, key)
            elif key == "stream_id":
                if not isinstance(value, str) or not re.fullmatch(r"\d+-\d+", value):
                    raise ValueError("invalid stream_id")
            else:
                _short(value, key)
            values.append(str(value))
        if envelope["run_id"] != self.run_id or envelope["slot"] != slot:
            raise BusError("foreign run/slot envelope")
        keys = [self.lease_key(slot), self.msg_key(envelope["message_id"]),
                self.inbox_key(slot), self.audit_key]
        return keys, values + [self.slot_group(slot)]

    def register_slot(self, slot, generation, session_id):
        _short(slot, "slot"); _integer(generation, "generation")
        _short(session_id, "session_id")
        return self._call(LUA_REGISTER,
                          [self.lease_key(slot), self.inbox_key(slot), self.audit_key],
                          [self.run_id, slot, generation, session_id, self.slot_group(slot)])

    def rebind_slot(self, slot, generation, old_session_id, session_id, fence):
        """Same generation only; compare old binding/fence and require an idle, drained slot."""
        _short(slot, 'slot'); _integer(generation, 'generation'); _integer(fence, 'fence')
        _short(old_session_id, 'old_session_id'); _short(session_id, 'session_id')
        return self._call(LUA_REBIND, [self.lease_key(slot), self.inbox_key(slot), self.audit_key],
                          [self.run_id, slot, generation, old_session_id, session_id, fence, self.slot_group(slot)])

    def enqueue(self, slot, body, request_id=None, kind="task", deadline_at=None,
                message_id=None):
        _short(slot, "slot")
        message_id = _short(message_id if message_id is not None else
                            "m-" + uuid.uuid4().hex, "message_id")
        request_id = _request_id(request_id if request_id is not None else
                                 "req-" + message_id)
        if kind not in ("task", "operator", "supervisor", "worker", "followup"):
            raise ValueError("unsupported kind")
        if deadline_at is not None:
            _integer(deadline_at, "deadline_at", 1)
        self._call(LUA_ENQUEUE,
                   [self.pending_key(slot), self.msg_key(message_id), self.audit_key],
                   [self.run_id, slot, message_id, _json(body), request_id, kind,
                    "" if deadline_at is None else deadline_at, self.deadline_ms])
        return message_id

    def offer(self, slot, message_id=None):
        """Offer FIFO work, atomically retiring never-offered expired heads first.

        Unnamed offers advance to the next valid head (or return None). A named
        offer must be the head; if it expires, retire only that head and return
        None. Offered/received expiry still holds the slot until confirmed stop.
        """
        if message_id is not None:
            _short(message_id, "message_id")
        while True:
            raw = self._call(LUA_OFFER,
                             [self.pending_key(slot), self.lease_key(slot),
                              self.inbox_key(slot), self.audit_key],
                             [self.run_id, slot, self.slot_group(slot), message_id or "",
                              self.prefix + ":message:", "d-" + uuid.uuid4().hex, self.lease_ms])
            if raw != "expired_queued":
                return self._decode(raw) if raw else None
            if message_id is not None:
                return None

    def blocking_client(self, block_ms=DEFAULT_BLOCK_MS):
        """New dedicated pool, retaining the same Unix socket; caller closes it."""
        _integer(block_ms, "block_ms", 1)
        options = dict(self.r.connection_pool.connection_kwargs)
        options["socket_timeout"] = max(5, block_ms / 1000.0 + 5)
        options.pop("connection_pool", None)
        pool = redis.ConnectionPool(connection_class=self.r.connection_pool.connection_class,
                                    max_connections=1, **options)
        return redis.Redis(connection_pool=pool)

    def receive(self, slot, consumer, block_ms=DEFAULT_BLOCK_MS, *, generation, session_id,
                reader=None):
        _short(consumer, "consumer"); _integer(generation, "generation")
        _short(session_id, "session_id"); _integer(block_ms, "block_ms", 1)
        current = self.r.hgetall(self.lease_key(slot))
        if (current.get("run_id") != self.run_id
                or current.get("generation") != str(generation)
                or current.get("session_id") != session_id):
            raise BusError("receive identity mismatch")
        if current.get("status") in ("expired", "uncertain"):
            raise BusError("slot held: " + current["status"])
        if current.get("consumer") not in ("", consumer):
            raise BusError("protected_consumer")
        owned_reader = reader is None
        reader = reader if reader is not None else self.blocking_client(block_ms)
        recovered = False
        try:
            # First inspect this consumer's PEL; never XCLAIM another consumer.
            rows = reader.xreadgroup(self.slot_group(slot), consumer,
                                     {self.inbox_key(slot): "0-0"}, count=1)
            recovered = bool(rows and rows[0][1])
            if not recovered:
                rows = reader.xreadgroup(self.slot_group(slot), consumer,
                                         {self.inbox_key(slot): ">"}, count=1, block=block_ms)
        except redis.RedisError as exc:
            raise BusError("receive failed: " + str(exc)) from exc
        finally:
            if owned_reader:
                reader.close()
                reader.connection_pool.disconnect()
        if not rows or not rows[0][1]:
            return None
        sid, fields = rows[0][1][0]
        env = dict(fields, stream_id=sid)
        for key in ("generation", "fence"):
            env[key] = int(env[key])
        # A generation replacement during BLOCK cannot be accepted by an old adapter.
        if env["generation"] != generation or env["session_id"] != session_id:
            raise BusError("received stale adapter identity; PEL retained for reconciliation")
        keys, argv = self._authorization(slot, env)
        raw = self._call(LUA_RECEIVE, keys, argv + [consumer])
        if raw == "expired":
            raise BusError("expired; slot and PEL held until confirmed stop")
        out = self._decode(raw)
        out.update(recovered=recovered, execution_allowed=not recovered)
        return out

    def renew(self, slot, envelope, lease_ms=None):
        duration = self.lease_ms if lease_ms is None else _integer(lease_ms, "lease_ms", 100)
        keys, argv = self._authorization(slot, envelope)
        consumer = _short(envelope.get("consumer"), "consumer")
        return self._call(LUA_RENEW, keys, argv + [consumer, duration]) == "renewed"

    def reclaim(self, slot, envelope, lease_ms=None):
        """Renew a received lease whose lease_until passed but whose deadline did not (§24, daemon-renewed slots
        only: the caller proves nobody else consumes this slot). False when the deadline passed; nothing written."""
        duration = self.lease_ms if lease_ms is None else _integer(lease_ms, "lease_ms", 100)
        keys, argv = self._authorization(slot, envelope)
        consumer = _short(envelope.get("consumer"), "consumer")
        return self._call(LUA_RECLAIM, keys, argv + [consumer, duration]) == "reclaimed"

    def commit(self, slot, envelope, result, status="completed"):
        if status not in ("completed", "failed"):
            raise ValueError("status must be completed or failed")
        keys, argv = self._authorization(slot, envelope)
        consumer = _short(envelope.get("consumer"), "consumer")
        answer = self._call(LUA_COMMIT, keys, argv + [consumer, status, _json(result)])
        if answer == "expired":
            raise BusError("expired; slot and PEL held until confirmed stop")
        return answer

    def mark_uncertain(self, slot, envelope, reason="execution state unknown"):
        if not isinstance(reason, str) or not reason or len(reason) > 1000:
            raise ValueError("reason must be 1-1000 characters")
        keys, argv = self._authorization(slot, envelope)
        return self._call(LUA_HOLD, keys, argv + [reason])

    def recover(self, slot, consumer, min_idle_ms=0, claim=False):
        """Inspect same-run work; never transfer or authorize execution.

        Another consumer's PEL remains protected even after expiry. Expired lease/
        deadline is marked held; explicit confirmed stop is needed to release it.
        """
        _short(consumer, "consumer"); _integer(min_idle_ms, "min_idle_ms")
        if claim is not False:
            raise ValueError("automatic claiming is unsupported; confirm stop explicitly")
        state = self.slot_state(slot)
        out = dict(state=state, execution_allowed=False, own_pending=[], protected=[])
        for item in state.get("pel", []):
            if item["time_since_delivered"] >= min_idle_ms:
                out["own_pending" if item["consumer"] == consumer else "protected"].append(item)
        if state.get("current_delivery"):
            msg = self.message(state["message_id"])
            if _ms() >= min(int(state["lease_until"]), int(msg["deadline_at"])):
                self.mark_uncertain(slot, msg, "lease/deadline expired during recovery")
                out["state"] = self.slot_state(slot)
        return out

    def resolve_uncertain(self, slot, envelope, resolution, *, execution_stopped=False):
        """Trusted daemon confirms the old execution has stopped before releasing it."""
        if execution_stopped is not True:
            raise BusError("explicit execution_stopped=True confirmation required")
        if resolution not in ("failed", "cancelled"):
            raise ValueError("resolution must be failed or cancelled")
        keys, argv = self._authorization(slot, envelope)
        return self._call(LUA_RESOLVE, keys, argv + [resolution])

    def pending(self, slot, limit=0):
        _integer(limit, "limit")
        return self.r.lrange(self.pending_key(slot), 0, limit - 1 if limit else -1)

    def message(self, message_id):
        raw = self.r.hgetall(self.msg_key(message_id))
        return self._decode(json.dumps(raw)) if raw else {}

    def status(self, message_id):
        return self.r.hget(self.msg_key(message_id), "status") or ""

    def slot_state(self, slot):
        state = self.r.hgetall(self.lease_key(slot))
        if not state:
            return dict(slot=slot, registered=False)
        summary = self.r.xpending(self.inbox_key(slot), self.slot_group(slot))
        state.update(registered=True, pending_ids=self.pending(slot),
                     pel_count=summary["pending"],
                     pel=self.r.xpending_range(self.inbox_key(slot), self.slot_group(slot),
                                               "-", "+", 100))
        return state

    def slots(self):
        prefix = self.prefix + ":lease:"
        return sorted(key[len(prefix):] for key in self.r.scan_iter(match=prefix + "*"))

    def inspect(self, slots=None):
        return {slot: self.slot_state(slot) for slot in (self.slots() if slots is None else slots)}

    def audit_tail(self, count=50):
        _integer(count, "count", 1)
        return self.r.xrevrange(self.audit_key, count=count)
