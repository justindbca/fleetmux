"""Read-only v2 diagnostics for the existing daemon control terminal.

No clients, threads, files or terminal actions are created on import. This module
never schedules, reads a work consumer group, ACKs, mutates keys or shuts Redis
down. iter_events exports full details; start_control shows a plain-text state
and timeline without an interactive pager or cmux calls.

Audit references are immutable, but enrichment is current Redis state, read
non-atomically. Missing historical identities stay unknown rather than borrowing
a newer delivery/generation. Redis remains the replay source; the optional safe
JSONL archive is an audit copy, never transport.
"""

from collections import deque
from dataclasses import dataclass
import html
import json
import math
import os
from pathlib import Path
import queue
import re
import sys
import threading
import time
import unicodedata

import redis

from .presentation import mask_secrets, sanitize_text
from .redis_runtime import validate_socket


BLOCK_MS = 1000
BATCH_SIZE = 100
HEARTBEATS = frozenset(("heartbeat", "beat", "renewed", "renew", "lease_renewed",
                        "context", "token_usage", "daemon.heartbeat"))
LEVELS = frozenset(("debug", "info", "warn", "error"))
_IDENTITY = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_REQUEST = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}\Z")
_CURSOR = re.compile(r"[0-9]+-[0-9]+\Z")
_NUMBERS = frozenset(("generation", "fence", "attempt", "at", "created_at", "offered_at",
    "received_at", "started_at", "settled_at", "validated_at", "committed_at",
    "deadline_at", "lease_until", "finished_at"))
_EXTRA_SECRETS = frozenset(("auth", "authentication", "credentials", "credential",
    "auth_json", "auth_config", "authentication_config", "private_key", "signing_key",
    "session_token", "id_token", "api_token", "oauth_token", "bearer_token",
    "x_auth_token", "x_amz_security_token", "accesstoken", "refreshtoken",
    "clientsecret", "privatekey", "sessiontoken", "idtoken"))


class ControlError(RuntimeError):
    """An observer read failed. Scheduler/Redis lifecycle is never affected."""


def _identity(value, name, pattern=_IDENTITY):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError("invalid " + name)
    return value


def _cursor(value):
    if (not isinstance(value, str) or len(value) > 41 or not _CURSOR.fullmatch(value)
            or any(int(part) > 2**64 - 1 for part in value.split("-"))):
        raise ValueError("after must be a Redis stream ID (milliseconds-sequence)")
    return value


def _id_tuple(value):
    return tuple(int(part) for part in value.split("-"))


def _integer(value, name, minimum=0, maximum=2**53 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("%s must be an integer in %d..%d" % (name, minimum, maximum))
    return value


@dataclass(frozen=True)
class _Settings:
    run_id: str
    run: str
    socket: str
    prefix: str

    @property
    def audit_key(self):
        return self.prefix + ":audit"


def _settings(cfg):
    if not isinstance(cfg, dict):
        raise ValueError("config must be a dict")
    run = cfg.get("run")
    if not isinstance(run, (str, os.PathLike)) or not str(run):
        raise ValueError("config requires a run directory")
    run_id = _identity(cfg.get("run_id") or Path(run).name, "run_id")
    socket = validate_socket(cfg)
    workers = cfg.get("workers", [])
    if not isinstance(workers, (list, tuple)):
        raise ValueError("workers must be a list of slot identities")
    for worker in workers:
        _identity(worker, "worker")
    return _Settings(run_id, str(run), str(socket), "fx:v2:run:" + run_id)


def _secret_key(key):
    base = str(key).lower().rsplit(".", 1)[-1].replace("-", "_")
    return (base in _EXTRA_SECRETS
            or mask_secrets({str(key): "__probe__"}).get(str(key)) == "***")


def _escape_text(value):
    text = sanitize_text(value)
    return "".join("\\u%04x" % ord(ch) if unicodedata.category(ch) == "Cf" else ch
                   for ch in text)


class _Redactor:
    """Preserve arbitrary JSON depth; use presentation's masker per field.

    No credential files are opened. Only known secret fields/config values are
    redacted; unlabeled arbitrary free-text secrets cannot be perfectly detected.
    JSON exports preserve business markup; the terminal renderer escapes HTML.
    """

    def __init__(self, cfg):
        known = set()
        pending = [cfg]
        while pending:
            node = pending.pop()
            if isinstance(node, dict):
                for key, value in node.items():
                    if _secret_key(key):
                        stack = [value]
                        while stack:
                            secret = stack.pop()
                            if isinstance(secret, str) and secret:
                                known.add(secret)
                            elif isinstance(secret, dict):
                                stack.extend(secret.values())
                            elif isinstance(secret, (list, tuple)):
                                stack.extend(secret)
                    elif isinstance(value, (dict, list, tuple)):
                        pending.append(value)
            elif isinstance(node, (list, tuple)):
                pending.extend(node)
        self.known = sorted(known, key=len, reverse=True)

    def text(self, value):
        # Valid JSON strings are redacted structurally so a quoted header cannot
        # swallow the rest of the business JSON (the header regex masks a line).
        if value.strip().startswith(("{", "[")):
            try:
                parsed = json.loads(value)
            except (ValueError, RecursionError):
                pass
            else:
                return json.dumps(self.safe(parsed), ensure_ascii=False, allow_nan=False)
        # Header lines can contain schemes/spaces/multiple cookies. Quoted JSON
        # fields are handled separately, preserving adjacent business fields.
        value = re.sub(
            r"(?im)(\b(?:authorization|proxy-authorization|cookie|set-cookie)\b"
            r"[\t ]*[:=][\t ]*)([^\r\n]+)", r"\1***", value)
        def redact_assignment(match):
            if not _secret_key(match["name"]):
                return match[0]
            quote = match["value"][0] if match["value"].startswith(('"', "'")) else ""
            return match["prefix"] + quote + "***" + quote
        value = re.sub(
            r'''(?P<prefix>\b(?P<name>[A-Za-z_][A-Za-z0-9_.-]*)["']?\s*[:=]\s*)'''
            r'''(?P<value>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;}]+)''',
            redact_assignment, value)
        for secret in self.known:
            value = value.replace(secret, "***")
        return _escape_text(value)

    def safe(self, value):
        # Iterative copying avoids presentation.mask_secrets' depth truncation.
        holder = [None]
        pending = [(holder, 0, value)]
        while pending:
            parent, key, item = pending.pop()
            if isinstance(item, dict):
                out = {}
                parent[key] = out
                for name, child in item.items():
                    # Field names define the export schema. Escape their controls
                    # without substituting credential substrings into schema keys.
                    safe_name = _escape_text(str(name))
                    if _secret_key(name):
                        out[safe_name] = "***"
                    else:
                        pending.append((out, safe_name, child))
            elif isinstance(item, (list, tuple)):
                out = [None] * len(item)
                parent[key] = out
                pending.extend((out, i, child) for i, child in enumerate(item))
            elif isinstance(item, str):
                parent[key] = self.text(item)
            elif isinstance(item, float) and not math.isfinite(item):
                parent[key] = None
            else:
                parent[key] = item
        return holder[0]


def _open_client(cfg, *, blocking=False):
    """Own a fresh pool; never reuse scheduler or work bridge connections."""
    settings = _settings(cfg)
    client = redis.Redis(unix_socket_path=settings.socket, db=0, decode_responses=True,
        encoding_errors="backslashreplace", socket_connect_timeout=1,
        socket_timeout=3 if blocking else 2, max_connections=1)
    try:
        client.ping()
    except redis.RedisError:
        _close_client(client)
        raise
    return client


def _close_client(client):
    if client is not None:
        try:
            client.close()
        finally:
            pool = getattr(client, "connection_pool", None)
            if pool is not None:
                pool.disconnect()


def _read_error(settings, redactor, exc):
    return "daemon cannot read private Redis socket %s: %s: %s" % (
        settings.socket, type(exc).__name__, redactor.text(str(exc)))


def _decode_hash(raw):
    out = dict(raw)
    for key, value in out.items():
        if isinstance(value, str) and key in _NUMBERS:
            try:
                out[key] = int(value)
            except ValueError:
                pass
        elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
            try:
                out[key] = json.loads(value)
            except (ValueError, RecursionError):
                pass
    return out


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _seconds(value):
    number = _number(value)
    return number / 1000 if number is not None and number >= 1e11 else number


def _level(record):
    explicit = record.get("level") or record.get("severity")
    if isinstance(explicit, str) and explicit in LEVELS:
        return explicit
    event = str(record.get("event", "")).lower()
    if any(word in event for word in ("error", "failed", "invalid", "timeout", "expired", "stalled")):
        return "error"
    if any(word in event for word in ("uncertain", "blocked", "retry", "gap", "reject")):
        return "warn"
    return "info"


def _enrich(client, settings, stream_id, fields, redactor):
    audit = _decode_hash(fields)
    slot, mid = fields.get("slot"), fields.get("message_id")
    # Corrupt audit references must not read unrelated Redis keys.
    message = (_decode_hash(client.hgetall(settings.prefix + ":message:" + mid))
               if isinstance(mid, str) and _IDENTITY.fullmatch(mid) else {})
    if message and (message.get("run_id") != settings.run_id or message.get("message_id") != mid):
        message = {}
    lease, agent = {}, {}
    if isinstance(slot, str) and _IDENTITY.fullmatch(slot):
        lease = _decode_hash(client.hgetall(settings.prefix + ":lease:" + slot))
        agent = _decode_hash(client.hgetall("fx:agent:" + slot))
    did = fields.get("delivery_id")
    matches = bool(did and message.get("delivery_id") == did and message.get("slot") == slot)
    out = dict(audit)
    out.update(audit_id=stream_id, stream_id=stream_id, audit=audit,
               message=message or None, slot_state=lease or None, agent_state=agent or None,
               enrichment={"source": "current Redis hashes, not historical snapshots",
                           "observed_at": time.time(), "delivery_matches": matches})
    out.setdefault("run_id", settings.run_id)
    out.setdefault("request_id", message.get("request_id"))
    for key in ("body", "result"):
        out.setdefault(key, message.get(key))
    for key in ("generation", "session_id", "fence", "attempt"):
        out.setdefault(key, message.get(key) if matches else None)
    out["delivery_stream_id"] = message.get("stream_id") if matches else None
    for key in _NUMBERS - {"at", "generation", "fence", "attempt"}:
        out.setdefault(key, message.get(key) if matches or key in ("created_at", "deadline_at") else None)
    out["label"] = (agent.get("label") if out.get("generation") is not None
                    and str(agent.get("generation")) == str(out["generation"]) else None)
    out["level"] = _level(out)
    return out


def _stream_info(client, key):
    try:
        return client.xinfo_stream(key)
    except redis.ResponseError as exc:
        if "no such key" in str(exc).lower():
            return {"first-entry": None, "last-generated-id": "0-0", "length": 0}
        raise


def _gap(info, after, settings):
    first = info.get("first-entry")
    trimmed = info.get("entries-added", 0) > info.get("length", 0)
    latest = info.get("last-generated-id", "0-0")
    earliest = first[0] if first else None
    deleted = info.get("max-deleted-entry-id", "0-0")
    missing = (_id_tuple(after) < _id_tuple(earliest) and (after != "0-0" or trimmed)
               if earliest else trimmed and _id_tuple(after) < _id_tuple(latest))
    deleted_ahead = _id_tuple(deleted) > _id_tuple(after)
    if missing or deleted_ahead:
        return {"event": "control.gap", "level": "warn", "run_id": settings.run_id,
                "audit_id": after, "stream_id": after, "resume_after": after,
                "first_available_id": earliest, "last_generated_id": latest,
                "max_deleted_entry_id": deleted,
                "reason": ("audit records after the cursor were deleted" if deleted_ahead else
                           "cursor precedes retained audit; missing history may have been trimmed"),
                "recovery": "replay retained Redis audit after resume_after; earlier history requires an archive"}
    return None


def _gap_signature(gap):
    return (gap["first_available_id"], gap["max_deleted_entry_id"], gap["reason"]) if gap else None


def _validate_filters(after, request_id, slot, generation, event, level, limit, follow):
    _cursor(after)
    if request_id is not None:
        _identity(request_id, "request_id", _REQUEST)
    if slot is not None:
        _identity(slot, "slot")
    if generation is not None:
        _integer(generation, "generation")
    if event is not None:
        _identity(event, "event")
    if level is not None and (not isinstance(level, str) or level not in LEVELS):
        raise ValueError("level must be debug, info, warn or error")
    _integer(limit, "limit", 1, 100000)
    if type(follow) is not bool:
        raise ValueError("follow must be boolean")


def _events(cfg, *, after="0-0", request_id=None, slot=None, generation=None, event=None,
            level=None, limit=100, follow=False, stop=None, progress=None, idle=None):
    settings = _settings(cfg)
    _validate_filters(after, request_id, slot, generation, event, level, limit, follow)
    redactor, client, reader = _Redactor(cfg), None, None
    cursor, emitted, last_gap = after, 0, None
    filters = {"request_id": request_id, "slot": slot, "generation": generation,
               "event": event, "level": level}
    try:
        client = _open_client(cfg)
        info = _stream_info(client, settings.audit_key)
        # Finite exports freeze their high-water mark, even with active writers.
        end = info.get("last-generated-id", "0-0")
        if follow:
            reader = _open_client(cfg, blocking=True)
        while stop is None or not stop.is_set():
            gap = _gap(info, cursor, settings)
            signature = _gap_signature(gap)
            if gap and signature != last_gap:
                last_gap = signature
                yield redactor.safe(gap)  # gaps bypass business filters by design
                emitted += 1
                if not follow and emitted >= limit:
                    break
            if follow:
                if idle:
                    idle()
                result = reader.xread({settings.audit_key: cursor},
                                     count=min(BATCH_SIZE, limit), block=BLOCK_MS)
                rows = result[0][1] if result else []
                info = _stream_info(client, settings.audit_key)
                end = info.get("last-generated-id", "0-0")
                # Check trim evidence before advancing the cursor.
                gap = _gap(info, cursor, settings)
                signature = _gap_signature(gap)
                if gap and signature != last_gap:
                    last_gap = signature
                    yield redactor.safe(gap)
            else:
                rows = client.xrange(settings.audit_key, min="(" + cursor, max=end,
                                     count=min(BATCH_SIZE, limit))
                if not rows:
                    break
                info = _stream_info(client, settings.audit_key)
                gap = _gap(info, cursor, settings)
                signature = _gap_signature(gap)
                if gap and signature != last_gap:
                    last_gap = signature
                    yield redactor.safe(gap)
                    emitted += 1
                    if emitted >= limit:
                        return
            for stream_id, fields in rows:
                if stop is not None and stop.is_set():
                    break
                if _id_tuple(stream_id) <= _id_tuple(cursor):
                    continue
                record = _enrich(client, settings, stream_id, fields, redactor)
                cursor = stream_id
                if progress:
                    progress(cursor)
                if any(value is not None and record.get(key) != value
                       for key, value in filters.items()):
                    continue
                yield redactor.safe(record)
                emitted += 1
                if not follow and emitted >= limit:
                    return
            if follow:
                info = _stream_info(client, settings.audit_key)
                end = info.get("last-generated-id", "0-0")
                if idle:
                    idle()
    except (redis.RedisError, OSError) as exc:
        raise ControlError(_read_error(settings, redactor, exc)) from None
    finally:
        _close_client(reader)
        _close_client(client)


def iter_events(cfg, *, after="0-0", request_id=None, slot=None, generation=None,
                event=None, level=None, limit=100, follow=False):
    """Yield full safe audit dicts after an exclusive audit_id/stream_id.

    limit bounds total records for a finite export (including gap notices).
    Follow replays then waits indefinitely, using limit as a read batch ceiling,
    never repeating a stream entry. Close the generator to close its owned pools.
    Gaps bypass filters; generation filters match only known event identities.
    Level is explicit when recorded, otherwise derived from the event name.
    """
    yield from _events(cfg, after=after, request_id=request_id, slot=slot,
        generation=generation, event=event, level=level, limit=limit, follow=follow)


def _snapshot(client, settings, cfg, redactor):
    now = time.time()
    service = _decode_hash(client.hgetall("fx:service"))
    heartbeat = _number(client.get("fx:daemon:beat"))
    task = _decode_hash(client.hgetall("fx:task"))
    names = {"supervisor"}
    workers = cfg.get("workers", [])
    if not isinstance(workers, (list, tuple)):
        raise ValueError("workers must be a list of slot identities")
    names.update(_identity(worker, "worker") for worker in workers)
    for suffix in (":lease:", ":pending:"):
        prefix = settings.prefix + suffix
        names.update(key[len(prefix):] for key in client.scan_iter(match=prefix + "*")
                     if _IDENTITY.fullmatch(key[len(prefix):]))
    slots = {}
    for slot in sorted(names):
        agent = _decode_hash(client.hgetall("fx:agent:" + slot))
        lease = _decode_hash(client.hgetall(settings.prefix + ":lease:" + slot))
        mid = lease.get("message_id")
        message = (_decode_hash(client.hgetall(settings.prefix + ":message:" + mid))
                   if isinstance(mid, str) and _IDENTITY.fullmatch(mid) else {})
        request_id = message.get("request_id") or agent.get("request") or None
        request = (_decode_hash(client.hgetall("fx:req:" + request_id))
                   if isinstance(request_id, str) and _REQUEST.fullmatch(request_id) else {})
        used, window = _number(agent.get("ctx_used")), _number(agent.get("ctx_window"))
        started = _seconds(message.get("started_at") or message.get("received_at")
                           or agent.get("busy_since"))
        active = bool(lease.get("current_delivery") or agent.get("state") in ("busy", "handoff", "starting"))
        deadline = message.get("deadline_at") or request.get("deadline_at") or request.get("deadline")
        slots[slot] = {"slot": slot, "label": agent.get("label") or None,
            "generation": lease.get("generation", agent.get("generation")),
            "state": agent.get("state") or lease.get("status") or None,
            "transport_state": lease.get("status"),
            "context": {"used": used, "window": window,
                        "ratio": used / window if used is not None and window and window > 0 else None,
                        # effective handoff threshold and where it came from (docs/design.md §22)
                        "handoff_window": agent.get("handoff_window"),
                        "handoff_ratio": _number(agent.get("handoff_ratio")),
                        "handoff_source": agent.get("handoff_source")},
            # long task split settings in effect and the last look (docs/design.md §44)
            "split": {k: agent.get("split_" + k) for k in ("check_minutes", "window_minutes", "min_calls",
                                                           "min_distinct", "restore_model", "source", "last")}
                     if agent.get("split_source") else None,
            "queue_depth": client.llen(settings.prefix + ":pending:" + slot),
            "advice": agent.get("advice_display") or None,  # <model>/<effort>/<domains> (advisor runs)
            "current_request": request_id, "current_message": mid or None,
            "current_delivery": lease.get("current_delivery") or None,
            "deadline_at": deadline, "lease_until": lease.get("lease_until"),
            "elapsed_s": max(0, now - started) if active and started is not None else None,
            "agent": agent or None, "lease": lease or None, "message": message or None,
            "request": request or None}
    return redactor.safe({"run_id": settings.run_id, "run": settings.run,
        "observed_at": now, "socket": {"path": settings.socket, "available": True},
        "daemon": {"heartbeat_at": heartbeat,
                   "heartbeat_age_s": max(0, now - heartbeat) if heartbeat is not None else None,
                   "paused": (service["paused"] == "1"
                              if service.get("paused") in ("0", "1") else None),
                   "pause_reason": service.get("reason"), "service": service or None,
                   "advisor": service.get("advisor") or "off"},  # roster advisor: on|off (advisor plan §2.1)
        "pending_requests": client.llen("fx:reqq"), "task": task or None, "slots": slots,
        "consistency": "current non-atomic read; None means unknown"})


def snapshot(cfg):
    """Return safe current state; a failed socket leaves facts unknown."""
    settings, redactor, client = _settings(cfg), _Redactor(cfg), None
    try:
        client = _open_client(cfg)
        return _snapshot(client, settings, cfg, redactor)
    except (redis.RedisError, OSError) as exc:
        return redactor.safe({"run_id": settings.run_id, "run": settings.run,
            "socket": {"path": settings.socket, "available": False,
                       "error": _read_error(settings, redactor, exc)},
            "daemon": {"heartbeat_at": None, "paused": None}, "slots": {},
            "pending_requests": None, "task": None})
    finally:
        _close_client(client)


def _cell_width(text):
    return sum(0 if unicodedata.combining(ch) else
               2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _wrap(text, width):
    # Physical wrapping is lossless (including Chinese); no 120-character cut.
    lines, current, cells = [], [], 0
    for ch in text:
        size = _cell_width(ch)
        if ch == "\n" or cells + size > width:
            lines.append("".join(current))
            current, cells = [], 0
        if ch != "\n":
            current.append(ch)
            cells += size
    lines.append("".join(current))
    return "\n".join(lines) + "\n"


def _display(value):
    return html.escape(sanitize_text(str(value)), quote=True).replace("\t", "\\t")


def _render(record, width):
    if record.get("event") == "control.state":
        state = record["snapshot"]
        daemon = state["daemon"]
        lines = ["STATE run=%s socket=%s pause=%s beat=%s adv=%s" % (
            state["run_id"], state["socket"].get("available"), daemon.get("paused"),
            daemon.get("heartbeat_at"), daemon.get("advisor") or "off")]
        if state["socket"].get("error"):
            lines.append(state["socket"]["error"])
        for slot, item in state["slots"].items():
            lines.append("%s label=%s state=%s context=%s handoff=%s@%s queue=%s request=%s elapsed=%s deadline=%s" % (
                slot, item["label"], item["state"], item["context"]["ratio"], item["context"].get("handoff_ratio"),
                item["context"].get("handoff_window"), item["queue_depth"],
                item["current_request"], item["elapsed_s"], item["deadline_at"])
                         + (" adv=%s" % item["advice"] if item.get("advice") else ""))
        return _wrap(_display("\n".join(lines)), width)
    if str(record.get("event", "")).startswith("control."):
        return _wrap(_display(record["event"] + "\n" +
                              json.dumps(record, ensure_ascii=False)), width)
    direction = record.get("direction") or {
        "queued": "producer->daemon", "offered": "daemon->agent",
        "deliver": "daemon->agent", "received": "agent->daemon",
        "settled": "agent->daemon", "committed": "daemon->result"}.get(record.get("event"), "?")
    line = "%s [%s] %s direction=%s slot=%s label=%s state=%s request=%s delivery=%s audit=%s" % (
        record.get("at", "?"), record.get("level", "info"), record.get("event", "?"),
        direction, record.get("slot", "?"), record.get("label"), record.get("status", "?"),
        record.get("request_id"),
        record.get("delivery_id"), record.get("audit_id"))
    return _wrap(_display(line), width)


def _append_archive(path, record):
    # Strict JSON, private permissions, no symlink target; never raw config/auth.
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as output:
        output.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
        output.flush()


class _ControlHandle:
    def __init__(self, cfg, stream):
        self.cfg = dict(cfg)
        self.settings, self.redactor = _settings(cfg), _Redactor(cfg)
        capacity = _integer(cfg.get("control_queue_size", 128), "control_queue_size", 1, 4096)
        self.width = _integer(cfg.get("control_width", 100), "control_width", 10, 1000)
        self.interval = _number(cfg.get("control_status_interval", 30))
        if self.interval is None or self.interval < 5:
            raise ValueError("control_status_interval must be >= 5 seconds")
        self.heartbeats = cfg.get("control_heartbeats", False)
        if type(self.heartbeats) is not bool:
            raise ValueError("control_heartbeats must be boolean")
        self.archive = cfg.get("control_archive", False)
        if type(self.archive) is not bool:
            raise ValueError("control_archive must be boolean")
        self.archive_path = Path(self.settings.run) / "control-audit.jsonl"
        self.filters = dict(cfg.get("control_filters") or {})
        allowed = {"after", "request_id", "slot", "generation", "event", "level", "limit"}
        if self.filters.keys() - allowed:
            raise ValueError("unsupported control_filters")
        self.filters.setdefault("after", "0-0")
        options = dict(after="0-0", request_id=None, slot=None, generation=None,
                       event=None, level=None, limit=100, follow=True)
        options.update(self.filters)
        _validate_filters(**options)
        self.stream = stream
        self._queue = queue.Queue(maxsize=capacity)
        self._stop = threading.Event()
        self._read_done = threading.Event()
        self._lock = threading.Lock()
        self._errors = deque(maxlen=16)
        self._gap = None
        self.cursor = self.filters["after"]
        self.rendered_cursor = self.cursor
        self._reader = threading.Thread(target=self._observe, name="fleet-control-read", daemon=True)
        self._writer = threading.Thread(target=self._output, name="fleet-control-output", daemon=True)

    @property
    def errors(self):
        with self._lock:
            return list(self._errors)

    @property
    def queue_depth(self):
        return self._queue.qsize()

    def _error(self, event, exc):
        record = self.redactor.safe({"event": event, "level": "error",
                                    "error": type(exc).__name__ + ": " + str(exc),
                                    "resume_after": self.rendered_cursor})
        with self._lock:
            self._errors.append(record)
        return record

    def _enqueue(self, record):
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            with self._lock:
                if self._gap is None:
                    self._gap = {"event": "control.gap", "level": "warn", "dropped": 0,
                        "resume_after": self.rendered_cursor,
                        "reason": "bounded output queue overflow; replay Redis audit after resume_after"}
                self._gap["dropped"] += 1
                self._gap["through"] = self.cursor

    def _advance(self, cursor):
        self.cursor = cursor

    def _observe(self):
        next_state = 0
        def state_if_due():
            nonlocal next_state
            if time.monotonic() >= next_state:
                self._enqueue({"event": "control.state", "snapshot": snapshot(self.cfg)})
                next_state = time.monotonic() + self.interval
        try:
            state_if_due()
            while not self._stop.is_set():
                filters = dict(self.filters, after=self.cursor)
                try:
                    for record in _events(self.cfg, **filters, follow=True, stop=self._stop,
                                          progress=self._advance, idle=state_if_due):
                        if self.archive and record.get("event") != "control.gap":
                            try:
                                _append_archive(self.archive_path, record)
                            except (OSError, ValueError) as exc:
                                error = self._error("control.archive_error", exc)
                                error.update(archive_disabled=True,
                                             recovery="full records remain replayable from Redis")
                                self._enqueue(error)
                                self.archive = False
                        if (self.heartbeats or self.filters.get("event") in HEARTBEATS
                                or record.get("event") not in HEARTBEATS):
                            self._enqueue(record)
                except (ControlError, ValueError) as exc:
                    self._enqueue(self._error("control.read_error", exc))
                    # Bounded retries only in observer, retaining the read cursor.
                    self._stop.wait(5)
                else:
                    break
        except Exception as exc:
            self._enqueue(self._error("control.observer_error", exc))
        finally:
            self._read_done.set()

    def _take_gap(self):
        with self._lock:
            gap, self._gap = self._gap, None
        return gap

    def _write(self, record):
        self.stream.write(_render(record, self.width))
        flush = getattr(self.stream, "flush", None)
        if flush:
            flush()

    def _output(self):
        broken = False
        while not self._read_done.is_set() or not self._queue.empty() or self._gap is not None:
            gap = self._take_gap()
            try:
                record = self._queue.get(timeout=.05)
            except queue.Empty:
                record = None
            for item in (gap, record):
                if item is None:
                    continue
                try:
                    self._write(item)
                    if item.get("audit_id") and item.get("event") != "control.gap":
                        self.rendered_cursor = item["audit_id"]
                except Exception as exc:
                    error = self._error("control.output_error", exc)
                    if not broken:
                        broken = True
                        self.stream = sys.__stderr__
                        try:
                            self._write(error)
                            self._write(item)
                        except Exception:
                            # errors remains readable even with both outputs
                            # broken. Never exit the scheduler or shutdown Redis.
                            pass

    def stop(self, timeout=1.0):
        """Return whether both owned threads stopped within a shared deadline.

        Python cannot forcibly kill an arbitrary blocked output.write. Threads
        are daemon threads; a timeout is reported, never imposed on scheduler IO.
        """
        duration = _number(timeout)
        if duration is None or duration < 0:
            raise ValueError("timeout must be a finite nonnegative number")
        self._stop.set()
        deadline = time.monotonic() + duration
        for thread in (self._reader, self._writer):
            thread.join(max(0, deadline - time.monotonic()))
        return not self._reader.is_alive() and not self._writer.is_alive()


def start_control(cfg, *, stream=None):
    """Start background diagnostics; no blocking Redis/output on the caller.

    Config-only options: control_queue_size=128, control_width=100,
    control_status_interval=30 (minimum 5s), control_heartbeats=False,
    control_filters={after/request_id/slot/generation/event/level/limit},
    control_archive=False (opt-in run/control-audit.jsonl).
    No rich dependency or interactive selection/pager is required.
    """
    handle = _ControlHandle(cfg, sys.stdout if stream is None else stream)
    handle._writer.start()
    handle._reader.start()
    return handle
