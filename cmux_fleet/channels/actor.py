"""The fleet's one channel actor (P1d inbound, P1e outbound; docs/design.md §53, contracts §8).

P1d: RedisStore, the audit sinks, handle_inbound, submit_operator_input, write_origin, remember_last,
write_status, inbound_loop. P1e: outbound_scan, resolve_target, send_direct, send_marked, outbound_loop,
drain, enqueue_run_closed, plan_processes. The daemon-side Supervisor is the one-line re-export from
supervise.py (P1f); the actor process entry (actor_main) is P1g.

Approved seams (supervisor [M0]):
- ``chans`` = ``{(type, account): Channel}``; ``send_direct(..., make_channel=None)`` with
  ``make_channel(type, account) -> Channel``;
- ``drain(r, chans, *, deadline, clock=time.time)``;
- an outbound-only exec channel takes the whole item through its adapter method ``send_item(item)``;
- ``handle_inbound`` is the accept callback of ``Channel.poll`` / ``Channel.inbound`` and the single owner of
  ``seen:<message_id>`` in the account store; it answers 'persisted' / 'duplicate' or raises channels.Retryable;
- ``channel.in`` / ``channel.no_target`` go to this run's existing v2 audit stream through ``channel_audit(r, cfg, ...)``
  (``runtime_v2.bus_for(cfg, r).audit_key``); ``audit_stream(r)`` is only the compatibility locator for direct callers
  without an ``audit=`` binding and fails explicitly (``AuditStreamUnresolved``) -- production never scans for boot.
"""
import hashlib
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

import redis

from cmux_fleet import channels
from . import envelope, provenance, security
from . import limits  # P8n
from .supervise import Supervisor  # noqa: F401  (P1f; the daemon side of the actor process)

# inbound loop timing (P1d, brief v2 §2.2): adapter errors back off 30 s -> 60 s -> ... -> 5 min; an account whose
# credentials were refused (health state needs_login) waits at least 5 min; a healthy round polls again after IDLE_S
POLL_TIMEOUT_S = 20
IDLE_S = 1.0
BACKOFF_START_S = 30
BACKOFF_MAX_S = 300
NEEDS_LOGIN_S = 300
AUDIT_TEXT_MAX = 200  # channel.in carries at most this much of the text
STATUS_FIELDS = ('state', 'heartbeat_at', 'activity_at', 'error')
LENGTH_REASON = 'operator input must be 1-' + str(envelope.TEXT_MAX) + ' chars of text'  # Fleet.handle's wording
NO_REDELIVERY = '上次投递结果未确认，本次无法重投。'
DIRECT_BUSY = '；它手上还有活，这条排在后面'  # ruling 10: the event changed (new generation), not re-posted
_IDENTITY = re.compile(r'[A-Za-z0-9_.-]+')
# Direct to a worker (design §53「直连 worker」, user 2026-11-10): `@dev-1 text` / `dev-1: text` names a worker; a reply
# inside a thread a worker answered goes back to that worker. Neither passes the supervisor.
THREAD = 'fx:channel:thread:'  # + <type>:<account>:<thread root> -> worker slot, EX ORIGIN_TTL
_WORKER = r'[A-Za-z][A-Za-z0-9_-]*-\d+(?:\.\d+)?'
_AT_WORKER = re.compile(r'\s*@(' + _WORKER + r')(?:[\s:：,，]+|$)(.*)', re.S)
_COLON_WORKER = re.compile(r'\s*(' + _WORKER + r')\s*[:：]\s*(.*)', re.S)
DIRECT_INSTRUCTION = ('The user sent this message to you directly from a chat channel (not through the supervisor). '
                      'Handle it as your role describes; put your answer text in the reply field.')


class RedisStore:
    """The actor's per-account store on the run's private Redis (P1d): keys ``fx:channel:store:<type>:<account>:<key>``,
    the same get / set / setnx / delete semantics as base.MemoryStore. Redis errors propagate (the base turns them
    into retrying / _fail)."""

    def __init__(self, r, type_, account):
        self.r, self.type, self.account = r, type_, account

    def _key(self, key):
        return channels.STORE + self.type + ':' + self.account + ':' + key

    @staticmethod
    def _px(ttl):
        return max(1, int(ttl * 1000)) if ttl else None

    def get(self, key):
        value = self.r.get(self._key(key))
        return value.decode('utf-8') if isinstance(value, bytes) else value

    def set(self, key, value, ttl=None):
        self.r.set(self._key(key), str(value), px=self._px(ttl))  # a SET without PX clears an earlier TTL

    def setnx(self, key, value, ttl=None):
        """True when the key was absent (and is now set); one atomic SET NX [PX]."""
        return bool(self.r.set(self._key(key), str(value), nx=True, px=self._px(ttl)))

    def delete(self, key):
        self.r.delete(self._key(key))


# ------------------------------------------------------------------ the run's audit stream
class AuditStreamUnresolved(LookupError):
    """No ``audit=`` binding and the run's audit stream cannot be located from ``r`` alone (no boot marker, more than
    one, or a cfg that names no run): an explicit failure, never a guessed stream and never a silent success."""


def audit_stream(r):
    """Compatibility locator for direct callers that hold only ``r`` and pass no ``audit=``: the run's v2 audit stream
    ``fx:v2:run:<run_id>:audit`` from the single ``fx:v2:run:<run_id>:boot`` marker (written by ``RuntimeV2.run``).
    Zero or several markers -> raise AuditStreamUnresolved. Production entries hold cfg and never call this. P1d."""
    marks = sorted({k.decode() if isinstance(k, bytes) else k for k in r.scan_iter(match='fx:v2:run:*:boot')})
    if len(marks) != 1:
        raise AuditStreamUnresolved('expected one fx:v2:run:<run_id>:boot marker, found ' + str(len(marks)))
    return marks[0][:-len(':boot')] + ':audit'


def _flat(value):
    if value is None:
        return ''
    if isinstance(value, str):
        return value
    if isinstance(value, (bool, int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _audit_writer(r, stream, type_, account):
    """audit(event, **fields) on ``stream()`` (the key, computed at the first write): every field through
    channels.redact, flattened to strings (XADD), plus channel / account when given."""
    def audit(event, **fields):
        row = {'event': event, 'at': str(int(time.time() * 1000))}
        if type_:
            row['channel'] = type_
        if account:
            row['account'] = account
        row.update({str(k): _flat(v) for k, v in channels.redact(fields).items()})
        r.xadd(stream(), row)
    return audit


def channel_audit(r, cfg, type_, account):
    """The audit callable audit(event, **fields) every Channel the actor (P1g actor_main) or send_direct (P1e)
    builds gets, and the ``audit=`` binding inbound_loop / outbound_loop / drain / send_direct pass on: fields
    through channels.redact, written to ``runtime_v2.bus_for(cfg, r).audit_key`` (no boot marker needed). type_ /
    account None = an actor-level row (channel.no_target). The key is computed at the first write; a cfg that names
    no run raises AuditStreamUnresolved there, and a write error propagates (callers decide; base.Channel.audit
    still swallows sink errors for its own rows). P8d wraps it (strangers.watch) later. P1d."""
    box = {}

    def stream():
        if 'key' not in box:
            if not isinstance(cfg, dict) or not (cfg.get('run_id') or cfg.get('run')):
                raise AuditStreamUnresolved('cfg names no run (run_id / run)')
            from cmux_fleet import runtime_v2
            box['key'] = runtime_v2.bus_for(cfg, r).audit_key
        return box['key']
    return _audit_writer(r, stream, type_, account)


# ------------------------------------------------------------------ inbound (P1d)
def request_id(env):
    """The operator_input request / event id of one inbound message: ``ch-`` + sha256(``<type>:<account>:<message_id>``)
    first 16 hex. Deterministic, so a retry of the same message reuses it (post_event's producer key)."""
    raw = env['channel'] + ':' + env['account'] + ':' + env['message_id']
    return 'ch-' + hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]


def parse_address(text):
    """(worker, rest) when the user-typed text starts with ``@<worker>`` or ``<worker>:`` (a name ending in ``-<n>``,
    optionally ``.<generation>``); else (None, text). Only the user's own text is looked at, never outside content."""
    for pattern in (_AT_WORKER, _COLON_WORKER):
        m = pattern.fullmatch(text or '')
        if m:
            return m.group(1), m.group(2).strip()
    return None, text


def thread_root(channel, account, reply_ref):
    """The thread key of a reply target (``reply_ref.thread_ts``), or None: the channel keeps no threads."""
    root = (reply_ref or {}).get('thread_ts')
    return str(channel) + ':' + str(account) + ':' + str(root) if root else None


def bind_thread(r, channel, account, reply_ref, worker):
    """Replies in this thread go to ``worker`` (its slot: a generation suffix ``.N`` dropped). Never raises."""
    key = thread_root(channel, account, reply_ref)
    if not key or not worker or worker == 'supervisor':
        return
    try:
        r.set(THREAD + key, re.sub(r'\.\d+$', '', str(worker)), ex=channels.ORIGIN_TTL)
    except redis.RedisError:
        pass


def thread_worker(r, env):
    """The worker bound to the thread this inbound message replies in (``reply_ref.in_thread``), or None."""
    ref = env.get('reply_ref') or {}
    key = thread_root(env['channel'], env['account'], ref) if ref.get('in_thread') else None
    return _s(r.get(THREAD + key)) if key else None


def _no_post(event):
    """post= for approve.handle_command when the caller bound none: nothing reaches the fleet, step two is
    unconfirmed (brief v2 §2.1-5)."""
    return {'status': 'pending', 'request_id': event.get('request_id', '') if isinstance(event, dict) else ''}


def _status_text(r, status):
    """/status: each slot and its state, from control_view.snapshot (status()) or, without it, from fx:agent:*."""
    paused = None
    if status is not None:
        snap = status() or {}
        rows = [(name, (slot or {}).get('state') or '?') for name, slot in sorted((snap.get('slots') or {}).items())]
        paused = (snap.get('daemon') or {}).get('paused')
    else:
        rows = []
        for key in sorted({k.decode() if isinstance(k, bytes) else k for k in r.scan_iter(match='fx:agent:*')}):
            name = key[len('fx:agent:'):]
            if _IDENTITY.fullmatch(name) and r.type(key) in ('hash', b'hash'):
                state = r.hget(key, 'state')
                rows.append((name, (state.decode() if isinstance(state, bytes) else state) or '?'))
        service = r.hget('fx:service', 'paused') if r.type('fx:service') in ('hash', b'hash') else None
        paused = str(service.decode() if isinstance(service, bytes) else service) == '1' if service is not None else None
    lines = ['Fleet 状态：'] + [name + '：' + state for name, state in rows] if rows else ['Fleet 状态：没有槽']
    if paused:
        lines.append('派工已暂停')
    return '\n'.join(lines)


def _in_fields(env):
    """The channel.in row: who, where, a 200-char snippet; reply_ref goes through channels.redact in the sink."""
    return {'message_id': env['message_id'], 'conversation': env['conversation'], 'sender': env['sender'],
            'text': (env.get('text') or '')[:AUDIT_TEXT_MAX], 'reply_ref': env.get('reply_ref') or {},
            'files': len(env.get('files') or []), 'rejected_files': len(env.get('rejected_files') or []),
            'provenance': [seg.get('kind', '') for seg in env.get('provenance') or []],
            'action': bool(env.get('action'))}


def handle_inbound(r, env, *, submit, reply, post=None, status=None, policy=None, stop=None, reader=None, audit=None,
                   direct=None):
    """policy: channels.security.policy(cfg) (None = no security.yaml, behaviour unchanged; P8b-P8d read it).
    stop: callable stop(env, cmd) -> str for a /stop, returning the receipt text that handle_inbound sends with its
    own reply (P8m binds functools.partial(channels.stop.handle, r, cfg, audit=...)); None -> unknown_command, as
    before P8a. reader: callable reader(segments, files, *, message_id) -> summarize dict, the P8c seam (A2'-1,
    ruling M105: the frozen stub's signature, rendered by channels.reader.render); inbound_loop binds
    functools.partial(channels.reader.summarize, policy=..., run=reader.run_cli, cfg=cfg) when a policy is set;
    None -> behaviour unchanged (no summary step). audit: audit(event, **fields) for channel.in, bound by inbound_loop to
    channel_audit(r, cfg, type, account); None (direct callers only) -> audit_stream(r). The audit sink is resolved
    before any side effect (seen claim, submit, post, reply): unresolved -> channels.Retryable, nothing done.
    direct: callable direct(worker, text, origin) -> answer, a request with ``to`` that skips the supervisor (§53
    「直连 worker」): used for ``@<worker>`` / ``<worker>:`` text and for a reply in a thread bound to a worker; None
    -> such a message gets the ``direct_unavailable`` receipt and is not forwarded.
    Returns 'persisted' / 'duplicate' or raises channels.Retryable (unchanged protocol).

    P1d (contracts §8 「入站映射」 / 「入站确认」 4): claim ``seen:<message_id>`` in the account's RedisStore (taken ->
    'duplicate'); local deterministic answers (commands, file refusals, empty / over-long body, a Codex supervisor)
    -> 'persisted'; else submit(body, origin): accepted -> origin + last + receipt, 'persisted'; rejected ->
    receipt, forget, 'persisted'; anything else (unconfirmed, an error) -> receipt, forget, channels.Retryable."""
    if audit is None:
        try:
            stream = audit_stream(r)
        except AuditStreamUnresolved as exc:
            raise channels.Retryable('audit stream unresolved: ' + str(exc)) from exc
        audit = _audit_writer(r, lambda: stream, env.get('channel'), env.get('account'))
    store = RedisStore(r, env['channel'], env['account'])
    seen = 'seen:' + env['message_id']
    if not store.setnx(seen, '1', channels.SEEN_TTL):
        return 'duplicate'
    try:
        audit('channel.in', **_in_fields(env))
        return _dispatch(r, env, store, seen, submit=submit, reply=reply, post=post, status=status, policy=policy,
                         stop=stop, audit=audit, reader=reader, direct=direct)
    except BaseException as exc:
        try:
            store.delete(seen)  # forget: the fleet did not take it, a redelivery may try again
        except Exception:
            pass
        if isinstance(exc, channels.Retryable) or not isinstance(exc, Exception):
            raise
        raise channels.Retryable(type(exc).__name__ + ': ' + str(exc)[:300]) from exc


def _dispatch(r, env, store, seen, *, submit, reply, post, status, policy, stop, audit, reader=None, direct=None):
    for refused in env.get('rejected_files') or []:
        reply(env, envelope.receipt('file_refused', name=refused.get('name', ''), reason=refused.get('reason', '')))
    cmd = envelope.parse_command(env.get('text') or '')  # command recognition only: the user-typed text
    kind = cmd['kind']
    if kind != 'text':
        if kind == 'status':
            reply(env, _status_text(r, status))
        elif kind == 'help':
            reply(env, envelope.receipt('help'))
        elif kind == 'unknown':
            reply(env, envelope.receipt('unknown_command', command=cmd['command']))
        elif kind == 'invalid':
            reply(env, envelope.receipt('invalid_command', reason=cmd['reason']))
        elif kind == 'stop':
            reply(env, envelope.receipt('unknown_command', command='stop') if stop is None else stop(env, cmd))
        elif kind in ('approve', 'reject'):
            from . import approve

            def approve_reply(answer):
                # approve.py answers with ONE argument (supervisor ruling M135, adapted here only): a text goes
                # back through reply(env, text); a card (the confirm_delete card with the nonce button) is queued
                # on fx:channel:outq as a user_only reply aimed at this conversation, for the normal outbound path.
                if not isinstance(answer, dict):
                    reply(env, answer)
                    return
                item = envelope.outbound('reply', card=answer, target='explicit', channel=env['channel'],
                                         account=env['account'], conversation=env['conversation'],
                                         reply_ref=env.get('reply_ref'), user_only=True)
                r.lpush(channels.OUTQ, envelope.dumps(item))
            text = approve.handle_command(r, env, cmd, post=post if post is not None else _no_post,
                                          reply=approve_reply)
            if isinstance(text, str) and text:
                reply(env, text)
        return 'persisted'
    # the delivered body (contracts §8 「来源标记」 scheme M, C9 N2): verbatim, never stripped
    src = provenance.legacy_view(env) if policy is None else env
    worker, typed = parse_address(src.get('text') or '')  # the user's own words only (never outside content)
    if worker is not None:
        src = dict(src, text=typed)
    else:
        worker = thread_worker(r, env)
    try:
        # only outside segments need wrap(); a user-typed-only envelope is its text verbatim either way
        body = provenance.wrap(src, policy) if src.get('provenance') else (src.get('text') or '')
        body = _reader_step(env, policy, reader, audit, body)  # P8c: policy None / reader off -> body unchanged
    except ValueError:
        reply(env, envelope.receipt('rejected', reason=LENGTH_REASON + '（超过 ' + str(envelope.TEXT_MAX) + ' 字）'))
        return 'persisted'
    paths = [f['path'] for f in env.get('files') or []]
    if paths:
        body = '\n'.join(([body] if body else []) + paths)
    if body.strip() == '':
        if (policy is None and len(src.get('provenance') or []) < len(env.get('provenance') or [])
                and not env.get('files') and not env.get('rejected_files')):
            audit('channel.drop_invalid', error='outside content only (no policy)', message_id=env['message_id'])
        elif not env.get('rejected_files'):
            reply(env, envelope.receipt('rejected', reason=LENGTH_REASON))
        return 'persisted'
    if len(body) > envelope.TEXT_MAX:
        reply(env, envelope.receipt('rejected', reason=LENGTH_REASON + '（共 ' + str(len(body)) + ' 字，上限 '
                                    + str(envelope.TEXT_MAX) + '）'))
        return 'persisted'
    if policy is not None and not limits.admit(r, env, policy, audit, reply):  # P8n: inbound + request limits
        return 'persisted'
    rid = request_id(env)
    origin = envelope.origin_from(env)
    if worker is not None:
        return _direct(r, env, store, seen, worker, body, origin, rid, direct=direct, reply=reply)
    try:
        answer = submit(body, origin)
    except Exception as exc:
        answer = {'status': 'pending', 'request_id': rid, 'error': type(exc).__name__}
    answer = answer if isinstance(answer, dict) else {'status': 'pending'}
    state = answer.get('status')
    taken = answer.get('id') or answer.get('task_id')
    if state == 'accepted' and taken:
        write_origin(r, str(taken), origin)
        remember_last(r, env)
        kinds = [seg.get('kind', '') for seg in env.get('provenance') or []] if policy is not None else []
        marked = bool(kinds) and _mark_untrusted(r, str(taken), kinds, policy, audit)  # P8d B3
        reply(env, envelope.receipt('accepted', id=taken))
        if marked:
            reply(env, envelope.receipt('untrusted_notice'))
        return 'persisted'
    if state == 'rejected':
        store.delete(seen)
        reply(env, envelope.receipt('rejected', reason=answer.get('reason') or '原因未给出'))
        return 'persisted'
    if state == 'no_operator_input':
        reply(env, envelope.receipt('no_operator_input'))
        return 'persisted'
    text = envelope.receipt('unconfirmed', id=answer.get('request_id') or rid)
    if answer.get('reason') == 'event_conflict':
        text += '\n' + NO_REDELIVERY
    reply(env, text)
    raise channels.Retryable('operator input unconfirmed (' + rid + ')')


def _direct(r, env, store, seen, worker, body, origin, rid, *, direct, reply):
    """§53「直连 worker」: the message goes to ``worker`` as a request with ``to`` (no supervisor). Accepted -> origin +
    last + the thread bound to that worker + receipt; rejected (no such worker ...) -> receipt with the fleet's
    reason, forget; unconfirmed -> receipt, forget, Retryable (same protocol as the supervisor path)."""
    if direct is None:
        reply(env, envelope.receipt('direct_unavailable', worker=worker))
        return 'persisted'
    try:
        answer = direct(worker, body, origin)
    except Exception as exc:
        answer = {'status': 'pending', 'request_id': rid, 'error': type(exc).__name__}
    answer = answer if isinstance(answer, dict) else {'status': 'pending'}
    state, taken = answer.get('status'), answer.get('id')
    if state == 'accepted' and taken:
        write_origin(r, str(taken), origin)
        remember_last(r, env)
        bind_thread(r, env['channel'], env['account'], env.get('reply_ref'), worker)
        reply(env, envelope.receipt('direct_accepted', worker=worker, id=taken,
                                    busy=DIRECT_BUSY if answer.get('busy') else ''))
        return 'persisted'
    if state == 'rejected':
        store.delete(seen)
        reply(env, envelope.receipt('rejected', reason=answer.get('reason') or '原因未给出'))
        return 'persisted'
    reply(env, envelope.receipt('unconfirmed', id=answer.get('request_id') or rid))
    raise channels.Retryable('direct request unconfirmed (' + rid + ')')


def _reader_step(env, policy, reader, audit, body):
    """P8c: with a policy, reader.mode on, a bound ``reader`` and outside segments or text attachments, the outside
    content is replaced by the reader's summary (still inside an outside-content boundary, reader.render); a failed
    reader falls back to the B1 wrap. Audits security.reader_summary / security.reader_failed (never content).
    Otherwise ``body`` unchanged. Over TEXT_MAX -> ValueError (the caller's rejected branch)."""
    if policy is None or reader is None or (policy.get('reader') or {}).get('mode') != 'on':
        return body
    from . import reader as reader_mod
    files = env.get('files') or []
    if not env.get('provenance') and not reader_mod.text_files(files):
        return body
    result = reader(provenance.segments(env), files, message_id=env['message_id'])
    if isinstance(result, dict) and result.get('ok'):
        audit('security.reader_summary', message_id=env['message_id'], suspicious=bool(result.get('suspicious')))
    else:
        error = result.get('error') if isinstance(result, dict) else 'reader returned no result'
        audit('security.reader_failed', message_id=env['message_id'], error=str(error or 'failed')[:200])
    return reader_mod.render(env, policy, result if isinstance(result, dict) else None)


def _mark_untrusted(r, rid, kinds, policy, audit):
    """P8d B3: the fast-path marker + security.untrusted_request; True when written. Never raises."""
    from . import strangers
    try:
        return strangers.mark_untrusted(r, rid, kinds, policy=policy, audit=audit)
    except Exception:  # noqa: BLE001  (the request text carries the marker anyway: untrusted_kinds' second path)
        return False


def submit_operator_input(cfg, r, text, origin, *, timeout=30):
    """The default submit: operator_input to the supervisor (generation / thread = fx:agent:supervisor now,
    source='channel:<type>:<account>', request id = request_id(origin)), then wait_reply. A native Codex supervisor
    (no input hook) -> {'status': 'no_operator_input'}, nothing posted. Ruling 10: when this id was posted before
    (its producer key exists) nothing is posted again; the same id's reply is looked up, and if the event would now
    differ (new generation / thread) an unanswered lookup comes back with reason 'event_conflict'."""
    from cmux_fleet import fleet, runtime_v2
    info = r.hgetall('fx:agent:supervisor')
    try:
        generation = int(info.get('generation', -1))
    except (TypeError, ValueError):
        generation = -1
    if (cfg.get('supervisor_cli') or 'codex') == 'codex' and not fleet.prompt_hook_slot(cfg, 'supervisor', generation):
        return {'status': 'no_operator_input'}
    bus = runtime_v2.bus_for(cfg, r)
    rid = request_id(origin)
    event = dict(kind='operator_input', agent='supervisor', generation=generation, run_id=bus.run_id,
                 thread=info.get('thread'), text=text, request_id=rid,
                 source='channel:' + origin['channel'] + ':' + origin['account'])
    stored = r.get(bus.prefix + ':producer:' + rid)
    conflict = False
    if stored is None:
        try:
            runtime_v2.post_event(bus, event, rid)
        except redis.ResponseError as exc:
            if 'event_conflict' not in str(exc):
                raise
            conflict = True
    else:
        stored = stored.decode('utf-8') if isinstance(stored, bytes) else stored
        conflict = stored != runtime_v2.dumps(event)
    answer = runtime_v2.wait_reply(bus, rid, timeout=timeout)
    if answer.get('status') == 'pending':
        answer = dict(answer, request_id=rid, **({'reason': 'event_conflict'} if conflict else {}))
    return answer


def submit_direct(cfg, r, worker, text, origin, *, timeout=30):
    """§53「直连 worker」: a service request with ``to: worker`` (Fleet.request -> assign, the supervisor is not asked),
    payload ``{"message": text}``, reply key ``reply``; request id = event id = request_id(origin), so a retry of the
    same message posts nothing twice (producer key). ``busy``: the worker was not idle when it was posted."""
    from cmux_fleet import runtime_v2
    bus = runtime_v2.bus_for(cfg, r)
    rid = request_id(origin)
    slot = _s(r.hget('fx:names', worker)) or re.sub(r'\.\d+$', '', worker)
    busy = (_s(r.hget('fx:agent:' + slot, 'state')) or 'idle') != 'idle'
    event = dict(kind='request', request_id=rid, source='channel:' + origin['channel'] + ':' + origin['account'],
                 spec={'id': rid, 'to': worker, 'payload': {'message': text}, 'instruction': DIRECT_INSTRUCTION,
                       'reply': {'required': ['reply'], 'allowed': ['reply']}})
    if r.get(bus.prefix + ':producer:' + rid) is None:
        try:
            runtime_v2.post_event(bus, event, rid)
        except redis.ResponseError as exc:
            if 'event_conflict' not in str(exc):
                raise
    answer = runtime_v2.wait_reply(bus, rid, timeout=timeout)
    if answer.get('status') == 'pending':
        answer = dict(answer, request_id=rid)
    return dict(answer, busy=busy) if answer.get('status') == 'accepted' else answer


def write_origin(r, request_id, origin):
    """fx:channel:origin:<id> = origin JSON, EX ORIGIN_TTL (7 days from acceptance; only after accepted, C8)."""
    r.set(channels.ORIGIN + request_id, json.dumps(origin, ensure_ascii=False, sort_keys=True), ex=channels.ORIGIN_TTL)


def remember_last(r, env):
    """fx:channel:last (channel, account, conversation, reply_ref JSON, sender, at) and its mirror state/last.json
    (same six string fields; temp file + rename). The mirror is best effort: a write error goes to stderr."""
    fields = {'channel': env['channel'], 'account': env['account'], 'conversation': env['conversation'],
              'reply_ref': json.dumps(env.get('reply_ref') or {}, ensure_ascii=False, sort_keys=True),
              'sender': env['sender'], 'at': str(int(time.time()))}
    r.hset(channels.LAST, mapping=fields)
    path = channels.last_path()
    tmp = path.with_name(path.name + '.tmp-' + str(os.getpid()))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(fields, ensure_ascii=False, sort_keys=True) + '\n', encoding='utf-8')
        os.replace(tmp, path)
    except OSError as exc:
        print('channel actor: state/last.json not written: ' + type(exc).__name__, file=sys.stderr)
    return fields


def write_status(r, type_, account, fields):
    """fx:channel:status:<type>:<account> from a health dict: only state / heartbeat_at / activity_at / error; error
    through channels.redact, 500 chars; a state outside channels.INBOUND_STATES (+ the actor's lease_busy) is not
    written and audited as channel.error. Timestamps are written every call. Never raises."""
    try:
        out = {}
        for name in ('heartbeat_at', 'activity_at'):
            if fields.get(name) is not None:
                out[name] = _flat(fields[name])
        if 'error' in fields:
            out['error'] = channels.redact(_flat(fields.get('error')))[:500]
        state = fields.get('state')
        if state is not None:
            if state in channels.INBOUND_STATES or state == 'lease_busy':
                out['state'] = state
            else:
                try:
                    _audit_writer(r, lambda: audit_stream(r), type_, account)(
                        'channel.error', error='unknown inbound state ' + _flat(state)[:60], kind='')
                except Exception:
                    pass
        if out:
            r.hset(channels.STATUS + type_ + ':' + account, mapping=out)
    except Exception as exc:
        print('channel actor: status not written: ' + type(exc).__name__, file=sys.stderr)


def _poster(cfg, r):
    """post(event) -> answer for approve.handle_command: post_event + wait_reply under the event's request id."""
    def post(event):
        from cmux_fleet import runtime_v2
        import uuid
        bus = runtime_v2.bus_for(cfg, r)
        event = dict(event)
        rid = event.get('request_id') or 'ch-' + uuid.uuid4().hex[:16]
        event['request_id'] = rid
        runtime_v2.post_event(bus, event, rid)
        return runtime_v2.wait_reply(bus, rid, timeout=30)
    return post


def inbound_loop(ch, *, r, cfg, submit=None, reply=None, clock=time.time, sleep=time.sleep, stop=lambda: False):
    """One account's inbound thread: ch.poll(POLL_TIMEOUT_S, accept=handle_inbound(...)) per round (the cursor moves
    only past final items), then the account's status hash from ch.health(). A failed round (an adapter error, a
    retrying item) backs off 30 s doubling to 5 min; needs_login waits at least NEEDS_LOGIN_S. The decision reads
    health()['state'] and whether the round set an error, never the error text (ruling 6). Never raises out of a
    round's poll (the base swallows and audits adapter errors)."""
    type_, account = ch.type, ch.account.name
    cfg = cfg or {}
    names_run = bool(cfg.get('run_id') or cfg.get('run'))
    bound_audit = channel_audit(r, cfg, type_, account)
    policy = security.policy(cfg)
    reader = None
    if policy is not None:  # P8c reader binding + P8d stranger counting on this Channel's audit; None -> unchanged
        import functools
        from . import reader as reader_mod, strangers
        reader = functools.partial(reader_mod.summarize, policy=policy, run=reader_mod.run_cli, cfg=cfg)
        if getattr(ch, '_audit', None) is not None and not getattr(ch, '_strangers_watched', False):
            ch._audit = strangers.watch(ch._audit, r=r, policy=policy, type_=type_, account=account)
            ch._strangers_watched = True
    if submit is None:
        def submit(text, origin):
            return submit_operator_input(cfg, r, text, origin)
    direct = None
    if names_run and cfg.get('mode') == 'service':  # §53「直连 worker」: only a service fleet takes requests
        def direct(worker, text, origin):
            return submit_direct(cfg, r, worker, text, origin)
    if reply is None:
        def reply(env, text):
            ch.send_text(env['conversation'], text, reply_ref=env.get('reply_ref'))
    status = None
    if names_run:
        def status():
            from cmux_fleet import control_view
            return control_view.snapshot(cfg)
    post = _poster(cfg, r) if names_run else None
    # /stop (P8m, contracts §8 Z9): bound only when the run has a policy; None -> unknown_command as before.
    # Named stop_command: this function's own `stop` is the loop-exit predicate.
    stop_command = None
    if policy is not None:
        import functools
        from . import stop as stop_module
        stop_command = functools.partial(stop_module.handle, r, cfg, audit=bound_audit)

    def accept(env):
        return handle_inbound(r, env, submit=submit, reply=reply, post=post, status=status, policy=policy,
                              stop=stop_command, reader=reader, audit=bound_audit, direct=direct)
    backoff = 0
    while not stop():
        started = clock()
        ch.last_error = ''  # this round's error only (health()['error'])
        got = ch.poll(POLL_TIMEOUT_S, accept=accept)
        health = ch.health()
        now = clock()
        fields = dict(health, heartbeat_at=now)
        if got:
            fields['activity_at'] = now
        elif not health.get('activity_at'):
            fields.pop('activity_at', None)
        write_status(r, type_, account, fields)
        state = health.get('state')
        if ch.last_error or state not in ('ok', 'outbound_only'):
            backoff = BACKOFF_START_S if not backoff else min(backoff * 2, BACKOFF_MAX_S)
            delay = max(backoff, NEEDS_LOGIN_S) if state == 'needs_login' else backoff
        else:
            backoff = 0
            delay = max(0.0, IDLE_S - (now - started))
        sleep(delay)


# ------------------------------------------------------------------ outbound (P1e)
RESULTS = 'fx:results'
RESULT = 'fx:result:'
TASK = 'fx:task'
ESCALATIONS = 'fx:escalations'
TASK_TERMINAL = ('completed', 'blocked', 'budget_exhausted')
ESCALATION_PREFIXES = ('dialog:', 'blocked:')
LONG_CHUNKS = 3        # a reply over this many TEXT_LIMIT chunks goes out as a file plus a short text
ROUTE_BATCH = 100      # unrouted items moved per loop round before the account queues get a turn
IDLE_WAIT_S = 1        # outbound_loop idles on BRPOP fx:channel:outq with this timeout (never 0: 0 blocks forever)
EXEC_CONVERSATION = 'stdin'  # the conversation of an item for an outbound-only channel (its command's stdin)


def _s(value):
    return value.decode('utf-8') if isinstance(value, bytes) else value


def _push(entry):
    return entry.get('push') if entry.get('push') is not None else channels.DEFAULT_PUSH


def _receives(type_):
    """An outbound-only channel (exec: RECEIVES False) gets its own copy of each pushed event; the conversation
    target (origin / last / first) is chosen among the channels that receive."""
    if type_ == 'exec':
        return False
    try:
        return bool(getattr(channels.adapter_class(type_), 'RECEIVES', True))
    except Exception:
        return True


def _destinations(entries, event):
    """(any receiving channel lists ``event``, [outbound-only entries that list it])."""
    listing = [e for e in entries if event in _push(e)]
    return any(_receives(e['type']) for e in listing), [e for e in listing if not _receives(e['type'])]


def _outbox_file(outbox, rid, text):
    folder = Path(outbox) if outbox else channels.config_dir() / 'state' / 'outbox'
    folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r'[^A-Za-z0-9._-]', '_', str(rid))[:80] or 'result'
    path = folder / (safe + '-' + uuid.uuid4().hex[:8] + '.txt')
    path.write_text(text, encoding='utf-8')
    return str(path.resolve())


def _bodies(text, rid, outbox):
    """[{text}] or, over LONG_CHUNKS chunks of the base TEXT_LIMIT, [{text: head + note}, {file, caption}]."""
    from . import base
    limit = base.Channel.TEXT_LIMIT
    if len(text) <= LONG_CHUNKS * limit:
        return [{'text': text}]
    path = _outbox_file(outbox, rid, text)
    note = '\n…（全文 ' + str(len(text)) + ' 字，见附件 ' + Path(path).name + '）'
    return [{'text': text[:max(1, limit - len(note))] + note}, {'file': path, 'caption': rid}]


def _result_text(rid, rec):
    """The pushed text of a result: an ok reply verbatim; otherwise a first line naming the status, then the error
    and whatever reply there is."""
    status = str(rec.get('status') or '')
    result = rec.get('result')
    if isinstance(result, dict) and isinstance(result.get('reply'), str):
        reply = result['reply']
    elif result is None:
        reply = ''
    else:
        reply = json.dumps(result, ensure_ascii=False, sort_keys=True)
    if status == 'ok' and reply.strip():
        return reply
    lines = ['Fleet 结果 ' + rid + '：' + (status or 'unknown')]
    if rec.get('error'):
        lines.append(str(rec['error']))
    if reply.strip():
        lines.append(reply)
    return '\n'.join(lines)


def _origin(r, rid):
    try:
        raw = r.get(channels.ORIGIN + envelope.origin_id(rid))
        origin = json.loads(_s(raw)) if raw else None
    except (redis.RedisError, ValueError, TypeError):
        return None
    return origin if isinstance(origin, dict) else None


def _result_worker(rec):
    """The worker slot that answered a result (generation suffix dropped), or '' (the supervisor answered it, a
    delegated split, no worker)."""
    worker = str(rec.get('worker') or '')
    result = rec.get('result') if isinstance(rec.get('result'), dict) else {}
    if not worker or worker == 'supervisor' or result.get('delegated'):
        return ''
    return re.sub(r'\.\d+$', '', worker)


def _direct_hint(rec, who, workers):
    """One line after a pushed result: how to reach a worker without the supervisor (user 2026-11-10). '' without
    a worker list."""
    try:
        names = [re.sub(r'\.\d+$', '', str(w)) for w in (workers() if workers else [])]
    except Exception:
        names = []
    names = list(dict.fromkeys(n for n in names if n))
    if not names:
        return ''
    if who:
        return ('\n\n—— 在本帖里回复 = 接着找 ' + who + '；新消息开头写 @' + names[0] + ' 直接派活（可点名：'
                + '、'.join(names) + '）')
    return '\n\n—— 要直接派活，消息开头写 @' + names[0] + ' 之类（可点名：' + '、'.join(names) + '）'


def _items(event, target, bodies, outs, *, request_id='', user_only=False):
    out = []
    for body in bodies:
        if target is not None:
            out.append(envelope.outbound(event, target=target, request_id=request_id, user_only=user_only, **body))
        for e in outs:
            out.append(envelope.outbound(event, target='explicit', channel=e['type'], account=e['account'],
                                         conversation=EXEC_CONVERSATION, request_id=request_id, **body))
    return out


def outbound_scan(r, entries, *, clock=time.time, outbox=None, workers=None):
    """One pass of the outbound watch; returns how many items it enqueued on fx:channel:outq (LPUSH, oldest first
    out). Sources: fx:results (+ fx:result:<id>) with an origin (envelope.origin_id: <id>.done uses <id>'s) ->
    'result' to the origin; a result without an origin younger than ORIGIN_GRACE_S is looked at again next pass,
    older -> marked pushed, never sent (an outside request); fx:task entering completed / blocked /
    budget_exhausted -> 'task_done' to last; new dialog: / blocked: keys of fx:escalations -> 'escalation' (text
    only) to last. Each event goes to the conversation target once (if a receiving channel lists it in ``push``)
    and once to every outbound-only channel that lists it; fx:channel:pushed:<event>:<id> (SET NX EX 7 d) makes a
    restart or a second pass push nothing twice. fx:results is LTRIMmed, so the pass reads the whole list and
    skips what is pushed already (no index cursor). A reply over 3 chunks becomes a file (``outbox``, P1g passes
    <run>/outbox; None -> config_dir()/state/outbox) plus a short text.

    §53「直连 worker」: a result answered by a worker binds the origin's thread to that worker (a reply there goes
    straight back to it); ``workers()`` -> the fleet's live worker labels: each pushed result then ends with one
    hint line on how to reach a worker directly (None or empty -> no hint)."""
    from . import queue
    now = clock()
    items = []
    raws = [_s(x) for x in r.lrange(RESULTS, 0, -1)]
    recs = []
    for raw in raws:
        try:
            rec = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(rec, dict) and isinstance(rec.get('id'), str) and rec['id']:
            recs.append(rec)
    if recs:
        pipe = r.pipeline(transaction=False)
        for rec in recs:
            pipe.exists(channels.PUSHED + 'result:' + rec['id'])
        done = pipe.execute()
        conv, outs = _destinations(entries, 'result')
        for rec, pushed in zip(recs, done):
            rid = rec['id']
            if pushed or not (conv or outs):
                continue
            full = r.get(RESULT + rid)
            try:
                rec = json.loads(_s(full)) if full else rec
            except (TypeError, ValueError):
                pass
            mark = channels.PUSHED + 'result:' + rid
            if r.get(channels.ORIGIN + envelope.origin_id(rid)) is None:
                try:
                    at = float(rec.get('at') or 0)
                except (TypeError, ValueError):
                    at = 0.0
                if now - at >= channels.ORIGIN_GRACE_S:
                    r.set(mark, '1', nx=True, ex=channels.ORIGIN_TTL)  # an outside request: marked, never sent
                continue
            if not r.set(mark, '1', nx=True, ex=channels.ORIGIN_TTL):
                continue
            who = _result_worker(rec)
            if who:
                origin = _origin(r, rid)
                if origin:
                    bind_thread(r, origin.get('channel'), origin.get('account'), origin.get('reply_ref'), who)
            text = _result_text(rid, rec) + _direct_hint(rec, who, workers)
            items += _items('result', 'origin' if conv else None, _bodies(text, rid, outbox), outs, request_id=rid)
    if r.type(TASK) in ('hash', b'hash'):
        task = {_s(k): _s(v) for k, v in r.hgetall(TASK).items()}
        status, tid = task.get('status'), task.get('id')
        conv, outs = _destinations(entries, 'task_done')
        if status in TASK_TERMINAL and tid and (conv or outs):
            if r.set(channels.PUSHED + 'task_done:' + tid + ':' + status, '1', nx=True, ex=channels.ORIGIN_TTL):
                text = 'Fleet 任务 ' + tid + '：' + status
                for extra in (task.get('reason'), task.get('goal')):
                    if extra:
                        text += '\n' + str(extra)[:1000]
                items += _items('task_done', 'last' if conv else None, [{'text': text}], outs)
    if r.type(ESCALATIONS) in ('string', b'string'):
        conv, outs = _destinations(entries, 'escalation')
        try:
            escalations = json.loads(_s(r.get(ESCALATIONS)) or '{}')
        except (TypeError, ValueError):
            escalations = {}
        if isinstance(escalations, dict) and (conv or outs):
            for key, value in escalations.items():
                if not str(key).startswith(ESCALATION_PREFIXES):
                    continue
                if not r.set(channels.PUSHED + 'escalation:' + key, '1', nx=True, ex=channels.ORIGIN_TTL):
                    continue
                reason = value.get('reason') if isinstance(value, dict) else value
                text = 'Fleet 待监工处理：' + key + ('\n' + str(reason)[:1000] if reason else '')
                items += _items('escalation', 'last' if conv else None, [{'text': text}], outs)
    for item in items:
        queue.enqueue(r, item)
    return len(items)


def _channel_for(type_, account, r):
    """An adapter instance for target checks only (private_target_ok); no network at construction."""
    cls = channels.adapter_class(type_)
    return cls(account, transport=lambda *a, **k: None, store=RedisStore(r, type_, account.name))


def _private_ok(type_, account, conversation, r):
    try:
        return bool(_channel_for(type_, account, r).private_target_ok(conversation))
    except Exception:
        return isinstance(conversation, str) and conversation in account.allow


def _read_last(r):
    """fx:channel:last, or (Redis down) the state/last.json mirror, or None."""
    try:
        last = {_s(k): _s(v) for k, v in r.hgetall(channels.LAST).items()}
    except redis.RedisError:
        try:
            last = json.loads(channels.last_path().read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return None
    if not isinstance(last, dict) or not (last.get('channel') and last.get('account') and last.get('conversation')):
        return None
    ref = last.get('reply_ref') or {}
    if isinstance(ref, str):
        try:
            ref = json.loads(ref)
        except ValueError:
            ref = {}
    return {'channel': str(last['channel']), 'account': str(last['account']),
            'conversation': str(last['conversation']), 'reply_ref': ref if isinstance(ref, dict) else {},
            'sender': str(last.get('sender') or '')}


def resolve_target(r, item, entries, accounts, *, audit=None):
    """audit: the actor-level channel_audit binding for channel.no_target, resolved only when that row is due;
    None (direct callers only) -> audit_stream(r), AuditStreamUnresolved if it cannot be located.

    -> {channel, account, conversation, reply_ref} or None. explicit -> as given. Otherwise origin (target
    'origin': fx:channel:origin:<origin_id(request_id)>) > last (fx:channel:last, Redis down -> state/last.json) >
    first (the first entry whose account has a private PUSH_TO). Only channels that receive and, for a push event,
    list it in ``push`` are candidates. user_only: only last, and only a whitelisted sender's private chat. None
    -> channel.no_target audited."""
    if item.get('target') == 'explicit':
        return {'channel': item['channel'], 'account': item['account'], 'conversation': item['conversation'],
                'reply_ref': dict(item.get('reply_ref') or {})}
    event = item.get('event')
    allowed = [(e['type'], e['account']) for e in entries
               if _receives(e['type']) and (event not in channels.PUSH_EVENTS or event in _push(e))]

    def ok(cand):
        return cand is not None and (cand['channel'], cand['account']) in allowed
    found = None
    if not item.get('user_only') and item.get('target') == 'origin' and item.get('request_id'):
        try:
            raw = r.get(channels.ORIGIN + envelope.origin_id(item['request_id']))
            origin = json.loads(_s(raw)) if raw else None
        except (redis.RedisError, ValueError, TypeError):
            origin = None
        if isinstance(origin, dict) and ok(origin):
            found = {'channel': origin['channel'], 'account': origin['account'],
                     'conversation': origin['conversation'], 'reply_ref': dict(origin.get('reply_ref') or {})}
    if found is None:
        last = _read_last(r)
        if ok(last):
            account = accounts.get((last['channel'], last['account']))
            if not item.get('user_only') or (account is not None and last['sender'] in account.allow):
                found = {k: last[k] for k in ('channel', 'account', 'conversation', 'reply_ref')}
    if found is None and not item.get('user_only') and item.get('target') != 'explicit':
        for type_, name in allowed:
            account = accounts.get((type_, name))
            push_to = (account.values.get('PUSH_TO') or '') if account is not None else ''
            if push_to and _private_ok(type_, account, push_to, r):
                found = {'channel': type_, 'account': name, 'conversation': push_to, 'reply_ref': {}}
                break
    if found is None:
        sink = audit if audit is not None else _audit_writer(r, lambda: audit_stream(r), None, None)
        sink('channel.no_target', id=str(item.get('id') or ''), kind=str(event or ''),
             target=str(item.get('target') or ''), user_only=bool(item.get('user_only')))
    return found


def _wire(item, ch):
    """The item as this channel may carry it: no buttons / ref on a card for CARRIES_TOKENS False (exec)."""
    if item.get('card') is not None and not getattr(ch, 'CARRIES_TOKENS', True):
        return dict(item, card=envelope.redact_card(item['card']))
    return item


def _send_item(ch, item):
    """One real send of ``item`` on ``ch`` (send_ref = request_id or the item id, reset in finally); an
    outbound-only channel with send_item gets the whole item."""
    from . import base
    item = _wire(item, ch)
    ch.send_ref = item.get('request_id') or item.get('id')
    try:
        if hasattr(ch, 'send_item') and not getattr(ch, 'RECEIVES', True):
            return ch.send_item(item)
        conv, ref, key = item.get('conversation') or '', item.get('reply_ref') or None, item.get('id')
        if item.get('event') == 'result' and ref and ref.get('thread_ts'):
            ref = dict(ref, broadcast=True)  # a result in a thread is also shown in the chat itself (Slack)
        elif item.get('event') not in ('result', 'reply') and ref and ref.get('thread_ts'):
            # a push to the last conversation (prompt_wait card, task_done, daemon_down ...) is not an answer to the
            # user's last message: top level, never inside whatever thread the user last typed in (§53)
            ref = {k: v for k, v in ref.items() if k not in ('thread_ts', 'in_thread')}
        if item.get('text') is not None:
            return ch.send_text(conv, item['text'], reply_ref=ref, key=key)
        if item.get('card') is not None:
            if item.get('update'):
                return ch.update_card(conv, item['update'], item['card'], key=key)
            return ch.send_card(conv, item['card'], reply_ref=ref, key=key)
        if item.get('file') is not None:
            return ch.send_file(conv, item['file'], item.get('caption') or '', reply_ref=ref, key=key)
        return base.SendResult(ok=False, error='outbound item has no body')
    finally:
        ch.send_ref = None


def _guard_chain(policy, audit, *, notify=None, r=None, scanner=None):
    """None (no policy: unchanged, nothing called) or guard(item) -> item | None: secrets, then links (P8e / P8f).
    ``notify(notice)`` gets the one fixed ``secret_blocked`` notice of a blocked item (never raises into the send
    path); ``scanner``: a list shared between chains, holding the one lazily built ``secrets.Scanner`` (account
    files read once, at the first item)."""
    if policy is None:
        return None
    from . import links, secrets
    box = scanner if scanner is not None else []

    def safe_notify(notice):
        if notify is None:
            return
        try:
            notify(notice)
        except Exception as exc:  # a failed notice must not resend or leak the blocked item
            print('channel actor: secret_blocked notice not queued: ' + type(exc).__name__, file=sys.stderr)

    def guard(item):
        if not box:
            box.append(secrets.Scanner(policy, r=r))
        item = secrets.guard(item, policy, scanner=box[0], notify=safe_notify, audit=audit)
        if item is None:
            return None
        return links.rewrite_item(item, policy, audit=audit)
    return guard


def _queue_notice(r, notice):
    """A notice from the account-queue guard: back onto that account's queue (already routed), else the router."""
    if notice.get('channel') and notice.get('account'):
        r.lpush(channels.OUTQ_ACCOUNT + notice['channel'] + ':' + notice['account'], envelope.dumps(notice))
    else:
        r.lpush(channels.OUTQ, envelope.dumps(notice))


def _connect(cfg):
    path = cfg.get('redis_socket') if isinstance(cfg, dict) else None
    if not path:
        return None
    try:
        client = redis.Redis(unix_socket_path=str(path), db=0, decode_responses=True, socket_connect_timeout=3,
                             socket_timeout=5)
        client.ping()
        return client
    except (redis.RedisError, OSError):
        return None


class _NoRedis:
    """Stands in for an unreachable Redis: every call raises ConnectionError (resolve_target falls back)."""
    def __getattr__(self, name):
        def down(*a, **k):
            raise redis.ConnectionError('redis unavailable')
        return down


def send_direct(cfg, item, *, make_channel=None):
    """Signature unchanged. A2'-2 (sec-2 request #1): reads channels.security.policy(cfg); None -> unchanged.
    Otherwise the item passes the guard chain secrets -> links (channels.secrets.guard, then
    channels.links.rewrite_item) before the adapter send: a secrets hit returns
    {'status': 'failed', 'reason': 'secret_blocked'} with nothing sent; links.preview == 'off' sets
    ch.link_preview = False on the channel it builds.

    P1e: no Redis queue, no lease, no actor hash. The target is resolved from cfg['channels'] (Redis down ->
    state/last.json, then first); the channel is make_channel(type, account) or the registered adapter with its
    account file; fleet_name = channels.fleet_label(cfg); send_ref per item. -> {status: sent | failed |
    no_target, ...}."""
    cfg = cfg if isinstance(cfg, dict) else {}
    policy = security.policy(cfg)
    entries = list(cfg.get('channels') or [])
    r = _connect(cfg)
    try:
        names_run = bool(cfg.get('run_id') or cfg.get('run'))
        if r is not None and names_run:
            actor_audit = channel_audit(r, cfg, None, None)
        else:
            def actor_audit(event, **fields):
                print('channel send: ' + event + ' (no audit stream)', file=sys.stderr)
        accounts = {}
        for e in entries:
            try:
                accounts[(e['type'], e['account'])] = channels.load_account(e['type'], e['account'])
            except Exception:
                continue
        target = resolve_target(r if r is not None else _NoRedis(), item, entries, accounts, audit=actor_audit)
        if target is None:
            return {'status': 'no_target'}
        item = dict(item, channel=target['channel'], account=target['account'],
                    conversation=target['conversation'], reply_ref=dict(target.get('reply_ref') or {}))
        if make_channel is not None:
            ch = make_channel(target['channel'], target['account'])
        else:
            account = accounts.get((target['channel'], target['account'])) or channels.load_account(
                target['channel'], target['account'])
            audit = channel_audit(r, cfg, target['channel'], target['account']) if r is not None and names_run else None
            ch = channels.adapter_class(target['channel'])(
                account, store=RedisStore(r, target['channel'], target['account']) if r is not None else None,
                audit=audit)
        ch.fleet_name = channels.fleet_label(cfg)
        if policy is not None:
            if (policy.get('links') or {}).get('preview') == 'off':
                ch.link_preview = False  # before the guard: a notice sent from inside it goes out preview-off too
            item = _guard_chain(policy, actor_audit, notify=lambda notice: _send_item(ch, notice), r=r)(item)
            if item is None:
                return {'status': 'failed', 'reason': 'secret_blocked'}
        res = _send_item(ch, item)
        if getattr(res, 'ok', False):
            return {'status': 'sent', 'message_id': getattr(res, 'message_id', None), 'channel': target['channel'],
                    'account': target['account']}
        return {'status': 'failed', 'error': str(getattr(res, 'error', '') or 'send failed')}
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass


def send_marked(r, ch, item, send):
    """P1e internal seam: sending / sending_at around the real adapter call, send_ref set and reset in finally."""
    from .supervise import SENDING, SENDING_AT
    r.hset(channels.ACTOR, mapping={SENDING: str(item.get('id') or ''), SENDING_AT: repr(time.time())})
    try:
        return send(item)
    finally:
        try:
            r.hdel(channels.ACTOR, SENDING, SENDING_AT)
        finally:
            ch.send_ref = None


def _safe_audit(r, audit):
    """The outbound audit sink: a failure (stream not located, write error) goes to stderr and the actor hash
    field audit_error, never into a resend (contracts §8 通道审计入口)."""
    def write(event, **fields):
        try:
            if audit is not None:
                audit(event, **fields)
            else:
                _audit_writer(r, lambda: audit_stream(r), None, None)(event, **fields)
        except Exception as exc:
            reason = channels.redact(event + ': ' + type(exc).__name__ + ': ' + str(exc))[:300]
            print('channel actor: audit not written: ' + reason, file=sys.stderr)
            try:
                r.hset(channels.ACTOR, 'audit_error', reason)
            except Exception:
                pass
    return write


def _layout(chans):
    """entries / accounts from the actor's channels, in their order; ``ch.push`` (set by P1g from the roster
    entry) filters push events: absent / None -> channels.DEFAULT_PUSH, the roster default (M119 ruling C2); an
    empty list (roster ``push: []``) means no push events at all (M156)."""
    def push_of(ch):
        push = getattr(ch, 'push', None)
        return list(channels.DEFAULT_PUSH if push is None else push)
    entries = [{'type': t, 'account': a, 'push': push_of(ch)} for (t, a), ch in chans.items()]
    return entries, {k: ch.account for k, ch in chans.items()}


def _route(r, raw, chans, entries, accounts, audit, guard=None):
    """Route one popped fx:channel:outq item into its account queue; True when something was moved / settled.
    ``guard`` (policy present): the secrets -> links chain runs before resolve_target / queue.route; a blocked
    item is settled here (its notice is routed by the guard's notify)."""
    from . import queue
    try:
        item = json.loads(_s(raw))
    except (TypeError, ValueError):
        queue.dead_letter(r, _s(raw), 'malformed: not JSON', attempts=0)
        return True
    if not isinstance(item, dict) or envelope.outbound_problem(item):
        queue.dead_letter(r, item, 'malformed: ' + (envelope.outbound_problem(item) or 'not an object'), attempts=0)
        return True
    if guard is not None:
        item = guard(item)
        if item is None:
            return True  # blocked: security.secret_blocked audited, the notice routed instead
    target = resolve_target(r, item, entries, accounts, audit=audit)
    if target is None:
        return True  # channel.no_target audited, not sent
    if (target['channel'], target['account']) not in chans:
        queue.dead_letter(r, item, 'no such channel in this actor: ' + target['channel'] + ':' + target['account'],
                          attempts=0)
        return True
    queue.route(r, item, target)
    return True


def _guards(r, policy, sink, chans, entries, accounts):
    """(route_guard, queue_guard) for outbound_loop / drain, or (None, None) without a policy. One Scanner shared.
    route_guard: before queue.route, a notice is routed at once (sent in the same round); queue_guard:
    process_one(guard=) for what enters an account queue unrouted-by-us (storm summaries, direct pushes)."""
    if policy is None:
        return None, None
    box = []
    route = _guard_chain(policy, sink, r=r, scanner=box,
                         notify=lambda notice: _route(r, envelope.dumps(notice), chans, entries, accounts, sink))
    return route, _guard_chain(policy, sink, r=r, scanner=box, notify=lambda notice: _queue_notice(r, notice))


def _account_send(r, ch):
    def send(item):
        return send_marked(r, ch, item, lambda it: _send_item(ch, it))
    return send


def outbound_loop(r, chans, *, stop, clock=time.time, sleep=time.sleep, audit=None, policy=None):
    """audit: passed through to resolve_target (actor_main binds channel_audit(r, cfg, None, None)).
    policy: channels.security.policy(cfg) (A2'-2, sec-2 request #1). None -> behaviour unchanged; otherwise each
    item passes the guard chain secrets -> links (channels.secrets.guard, then channels.links.rewrite_item)
    before queue.route, handed to queue.process_one(guard=...).

    P1e: each round routes what waits in fx:channel:outq (RPOP, up to ROUTE_BATCH), sends one item per account
    queue (queue.process_one, RPOP, throttle.gate, retries, dead letters; sending / sending_at around the real
    send), queues due storm summaries; an idle round waits on BRPOP fx:channel:outq with IDLE_WAIT_S (never 0)."""
    from . import queue, throttle
    entries, accounts = _layout(chans)
    sink = _safe_audit(r, audit)
    route_guard, guard = _guards(r, policy, sink, chans, entries, accounts)
    guard = limits.outbound_guard(guard, r, policy, sink)  # P8n: after secrets -> links, before throttle / send
    while not stop():
        busy = False
        for _ in range(ROUTE_BATCH):
            raw = r.rpop(channels.OUTQ)
            if raw is None:
                break
            busy = _route(r, raw, chans, entries, accounts, sink, route_guard) or busy
        for (type_, name), ch in chans.items():
            res = queue.process_one(r, _account_send(r, ch), sleep=sleep, block=0,
                                    key=channels.OUTQ_ACCOUNT + type_ + ':' + name, throttle=throttle.gate,
                                    audit=audit, now=clock(), guard=guard)
            busy = busy or res.get('status') != 'empty'
        for summary in throttle.flush_items(r, now=clock()) or []:
            if (summary.get('channel'), summary.get('account')) in chans:
                r.lpush(channels.OUTQ_ACCOUNT + summary['channel'] + ':' + summary['account'], envelope.dumps(summary))
                busy = True
        if not busy and not stop():
            got = r.brpop(channels.OUTQ, timeout=IDLE_WAIT_S)
            if got:
                _route(r, got[1], chans, entries, accounts, sink, route_guard)


def drain(r, chans, *, deadline, clock=time.time, audit=None, policy=None):
    """audit: passed through to resolve_target for items still unrouted (same binding as outbound_loop).
    policy: as outbound_loop (A2'-2): None -> unchanged; otherwise the same secrets -> links guard chain runs on
    every drained item before queue.route.

    P1e: before ``deadline`` (checked before every pop): route everything in fx:channel:outq, then send each
    account queue without blocking (process_one block=0 = RPOP; never BRPOP timeout 0; retry waits cut to the
    time left). Both queue kinds empty and nothing in flight -> fx:channel:actor drained=1. Returns
    {drained, left}."""
    from . import queue, throttle
    from .supervise import DRAINED, SENDING
    entries, accounts = _layout(chans)
    sink = _safe_audit(r, audit)
    route_guard, guard = _guards(r, policy, sink, chans, entries, accounts)
    guard = limits.outbound_guard(guard, r, policy, sink)  # P8n: after secrets -> links, before throttle / send

    def bounded_sleep(seconds):
        time.sleep(max(0.0, min(seconds, deadline - clock())))
    while clock() < deadline:
        raw = r.rpop(channels.OUTQ)
        if raw is None:
            break
        _route(r, raw, chans, entries, accounts, sink, route_guard)
    for (type_, name), ch in chans.items():
        key = channels.OUTQ_ACCOUNT + type_ + ':' + name
        while clock() < deadline:
            res = queue.process_one(r, _account_send(r, ch), sleep=bounded_sleep, block=0, key=key,
                                    throttle=throttle.gate, audit=audit, now=clock(), guard=guard)
            if res.get('status') == 'empty':
                break
    left = r.llen(channels.OUTQ) + sum(r.llen(channels.OUTQ_ACCOUNT + t + ':' + a) for t, a in chans)
    drained = left == 0 and not r.hget(channels.ACTOR, SENDING)
    if drained:
        r.hset(channels.ACTOR, DRAINED, '1')
    return {'drained': drained, 'left': left}


def enqueue_run_closed(f):
    """The daemon's explicit-stop tail (brief v2 §2.1-8): with cfg channels and no managed lifecycle (that one
    notifies through lifecycle_close.notify), LPUSH one run_closed item to last, once per run
    (fx:channel:pushed:run_closed:<run_id>). True when enqueued."""
    from cmux_fleet import lifecycle_close
    from . import queue
    if not f.cfg.get('channels') or lifecycle_close.managed(f):
        return False
    if getattr(f, 'v2', None) is not None:
        run_id = f.v2.bus.run_id
    else:
        run_id = f.cfg.get('run_id') or Path(f.cfg['run']).name
    if not f.r.set(channels.PUSHED + 'run_closed:' + run_id, '1', nx=True, ex=channels.ORIGIN_TTL):
        return False
    name = channels.fleet_label(f.cfg)
    queue.enqueue(f.r, envelope.outbound('run_closed', target='last',
                                         text='Fleet ' + (name + ' ' if name else '') + '已停止（' + run_id + '）。'))
    return True


# ------------------------------------------------------------------ process model (contracts §8)
def plan_processes(cfg, config_path):
    """C13: one actor child per fleet for all its channels -> [argv] (same shape as supervise.actor_argv:
    python, fleet.py, 'channel', 'actor', <config.json>); no channels -> []."""
    if not (isinstance(cfg, dict) and cfg.get('channels')):
        return []
    fleet_py = str(Path(__file__).resolve().parent.parent / 'fleet.py')
    return [[cfg.get('python') or 'python3', fleet_py, 'channel', 'actor', str(config_path)]]
