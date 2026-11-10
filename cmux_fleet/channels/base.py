"""Channel base class (ported from an in-house notify bus; contracts §8).

The four sends of the original (send_text / send_card / update_card / send_file + SendResult) are kept; what
changed for fleetmux:

* every send names its ``conversation`` (notify_bus had one configured chat per channel) and may carry the
  ``reply_ref`` of the message it answers (only the adapter reads it: Feishu message id, Weixin
  context_token, Email Message-ID, ...);
* the public methods are concrete and never raise: they validate, split text at ``TEXT_LIMIT``, fall back
  from card to text, and turn any exception into ``SendResult(ok=False)`` + an audit record. An adapter
  implements only the underscored hooks;
* inbound: ``inbound(raw)`` turns one raw platform item into envelopes through the adapter's
  ``parse_inbound`` and applies the one policy for every channel: shape, whitelist (``account.allow``),
  private chats only, verified senders only (email DKIM / SPF), and message-id idempotency (seen keys);
  ``poll(timeout)`` does the same for whatever ``_poll`` fetched. Neither raises.

An adapter never touches the network directly: it gets a ``transport`` callable at construction (its own
signature, documented by the adapter; HTTP adapters use ``http_transport``'s), so every test injects a fake
and nothing connects to a real service.
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cmux_fleet import channels
from cmux_fleet.channels import envelope


@dataclass
class SendResult:
    ok: bool
    message_id: Optional[str] = None
    error: Optional[str] = None


@dataclass
class LoginResult:
    """What ``Channel.login`` hands to `cmux-fleet channel login` (P1 writes the account file, mode 0600).
    status: ok | already_bound (the platform says this client is bound already: success, nothing new) |
    expired | failed. ``values``: account keys to write (upper case; credentials included, never printed);
    ``allow``: the default whitelist (Weixin: the scanning user's ilink_user_id) unless --allow overrides;
    ``message``: one line for the terminal, no secret."""
    status: str
    values: dict = None
    allow: tuple = ()
    message: str = ''


class MemoryStore:
    """The adapter store interface, in memory (tests; the actor gives a Redis-backed one under
    ``fx:channel:store:<type>:<account>:`` with the same four methods)."""

    def __init__(self):
        self.data, self.expiry = {}, {}

    def _live(self, key):
        if key in self.expiry and self.expiry[key] <= time.time():
            self.data.pop(key, None)
            self.expiry.pop(key, None)
        return key in self.data

    def get(self, key):
        return self.data[key] if self._live(key) else None

    def set(self, key, value, ttl=None):
        self.data[key] = str(value)
        if ttl:
            self.expiry[key] = time.time() + ttl
        else:
            self.expiry.pop(key, None)

    def setnx(self, key, value, ttl=None):
        """True when the key was absent (and is now set)."""
        if self._live(key):
            return False
        self.set(key, value, ttl)
        return True

    def delete(self, key):
        self.data.pop(key, None)
        self.expiry.pop(key, None)


def http_transport(method, url, *, headers=None, body=None, timeout=30):
    """The default transport of HTTP adapters: stdlib urllib (honours HTTPS_PROXY / NO_PROXY from the
    environment, as Telegram on a proxied host needs). Returns (status, body bytes); HTTP error statuses are
    returned, not raised. Network errors raise OSError (the base turns them into SendResult / audit)."""
    req = urllib.request.Request(url, data=body, headers=dict(headers or {}), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def text_units(text, units='chars'):
    """Length in ``chars`` (code points) or ``utf16`` code units (Telegram counts UTF-16: an emoji is 2)."""
    if units == 'utf16':
        return len(text.encode('utf-16-le')) // 2
    return len(text)


def split_text(text, limit, units='chars'):
    """Chunks of at most ``limit`` units whose concatenation is ``text``; a cut prefers the last newline in the
    second half of the window (macmini telegram_send cut at 4000 blindly), never inside a surrogate pair."""
    if limit < 1:
        raise ValueError('limit must be positive')
    out = []
    while text_units(text, units) > limit:
        fit = limit
        while text_units(text[:fit], units) > limit:  # utf16: fewer code points fit the window
            fit -= 1
        cut = text.rfind('\n', fit // 2, fit)
        cut = cut + 1 if cut >= 0 else fit
        out.append(text[:cut])
        text = text[cut:]
    out.append(text)
    return out


class Channel(ABC):
    """One account of one channel type. Subclasses set the class attributes and implement the hooks."""

    type: str = 'base'
    TEXT_LIMIT: int = 4000          # longest text one send carries (Telegram 4096 -> 4000, macmini telegram_send)
    TEXT_UNITS: str = 'chars'       # how TEXT_LIMIT counts: 'chars' or 'utf16' (Telegram: 4000 UTF-16 units)
    OVERFLOW: str = 'split'         # text over TEXT_LIMIT: 'split' into chunks, or 'file' (email: one message with
                                    # a summary and the full text attached, via _send_long_text)
    RECEIVES: bool = True           # False: outbound only (exec)
    SUPPORTS_CARDS: bool = False    # _send_card renders a card natively
    SUPPORTS_BUTTONS: bool = False  # ... with pressable buttons (else card_text spells the commands out)
    SUPPORTS_UPDATE: bool = False   # _update_card edits a sent card in place
    SUPPORTS_FILES: bool = True     # _send_file sends an attachment
    MAX_FILE_BYTES: int = 20 * 1024 * 1024  # inbound files above this are refused, never silently dropped
    PENDING: tuple = ()             # capabilities waiting for a brief v2 decision, declared honestly instead of
                                    # faked or dropped: 'files' (send_file -> ok=False "pending brief v2"),
                                    # 'inbound_media' (media arrive as rejected_files with that reason). Not "never".
    CARRIES_TOKENS: bool = True     # reaches only the whitelisted user's private chat; False (exec: an outside
                                    # command) -> every card is redacted first: no buttons, no commands, no tokens
    OPTIONAL_DEPS: tuple = ()       # importable names start() needs (feishu: ('lark_oapi',)); never imported at
                                    # module level, so a missing one only turns inbound off (MissingDependency)
    # Send metadata (mail plan A, I2; P0b): set on the instance, never passed through a constructor or send_*
    # argument and never put into reply_ref. fleet_name: channels.fleet_label(cfg), set by whoever builds the
    # channel (P1g actor, P1e send_direct); '' when unknown (never the account name). send_ref: the logical request
    # an outbound send belongs to (item request_id, else item id), set by P1e before each send and reset to None in
    # finally, the same on every chunk and retry of that item. An adapter may read both (email: subject / headers).
    fleet_name: str = ''
    send_ref = None
    # P8a (sec-2 #10): link preview of a sent message. None = the request carries no preview field (unchanged);
    # the actor / send_direct set False when policy links.preview is off. Only Telegram reads it (P8f).
    link_preview = None

    def __init__(self, account, *, transport=None, store=None, audit=None, inbox_dir=None, clock=time.time):
        """``account``: channels.Account. ``transport``: the adapter's network callable (None = its real
        default). ``store``: get / set / setnx / delete (MemoryStore shape). ``audit(event, **fields)``: the
        run's audit stream (channels.AUDIT_EVENTS names). ``inbox_dir``: where inbound files go
        (``<run>/inbox/files/``)."""
        self.account = account
        self.transport = transport
        self.store = store if store is not None else MemoryStore()
        self._audit = audit
        self.inbox_dir = Path(inbox_dir) if inbox_dir else None
        self.clock = clock
        self.last_error = ''
        self.send_key = None

    # ---------------------------------------------------------------- hooks an adapter implements
    @abstractmethod
    def _send_text(self, conversation, text, reply_ref):
        """Send one chunk (len <= TEXT_LIMIT). Return the platform message id (str) or a SendResult."""

    def _send_long_text(self, conversation, text, reply_ref):
        """OVERFLOW == 'file' only: send text longer than TEXT_LIMIT as one message (summary + attachment)."""
        raise NotImplementedError

    def _send_card(self, conversation, card, reply_ref):
        """Native card (only when SUPPORTS_CARDS). Return the message id or a SendResult."""
        raise NotImplementedError

    def _update_card(self, conversation, message_id, card):
        """Edit a sent card (only when SUPPORTS_UPDATE)."""
        raise NotImplementedError

    def _send_file(self, conversation, path, caption, reply_ref):
        """Send an attachment (only when SUPPORTS_FILES)."""
        raise NotImplementedError

    def parse_inbound(self, raw):
        """One raw platform item (an update, an event, a mail) -> list of envelope dicts (envelope.make).
        Report what the platform says: the sender id in the whitelist's format, ``private`` (a one-to-one
        chat), ``verified`` (the platform authenticated the sender; email: DKIM / SPF pass). Policy is the
        base's job. Downloads go to ``self.inbox_dir``; too large -> ``rejected_files``."""
        return []

    def _poll(self, timeout):
        """Fetch what arrived (long poll up to ``timeout`` s, or drain a queue a WS thread fills) -> raw items."""
        return []

    def start(self):
        """Start background receiving (a push channel's WS thread). Called once by the account's actor process
        after the lease is held. A push callback only queues the raw item for ``_poll`` (no I/O, no policy); a
        card / button callback that must answer the platform at once (Feishu: within 3 s) returns a fixed
        toast and queues the press, whose outcome goes out later as a normal message. Optional packages are
        imported here through ``require``. Proxies are scoped to the client (e.g. a requests Session with
        ``trust_env=False``, ``websockets.connect(..., proxy=None)``): ``os.environ`` is never changed."""

    def close(self):
        """Stop background work, release sockets. Must not raise. (A client without a stop call, like lark-oapi
        1.5.3's WS, stops when its actor process exits; that is why each account has its own process.)"""

    @classmethod
    def login(cls, name, *, transport=None, out=print, ask=input, clock=time.time):
        """Interactive login for `channel login <type> <name>`: ``out(line)`` prints (Weixin: the QR code LINK
        only, no image, no terminal-drawn code), ``ask(prompt)`` reads one line from the terminal (a pairing
        number, a token), ``clock`` for its own time limit. Returns a LoginResult; never raises for a platform
        answer (expired / refused -> status). Default: no interactive login (the CLI takes --set / --from-stdin)."""
        raise NotImplementedError(cls.type + ' has no interactive login; use --set KEY=VALUE or --from-stdin')

    def check(self):
        """Credential check for `channel test` without sending (e.g. Telegram getMe). Return a SendResult."""
        return SendResult(ok=True)

    def health(self):
        """Inbound health for the status hash: {'state': one of channels.INBOUND_STATES, 'heartbeat_at',
        'activity_at', 'error'}. Default: ok when the channel receives, else outbound_only."""
        state = getattr(self, '_state', '') or ('ok' if self.RECEIVES else 'outbound_only')
        return {'state': state, 'heartbeat_at': 0, 'activity_at': 0, 'error': self.last_error}

    def new_inbox_file(self, name, prefix=''):
        """Create a new inbound file in ``inbox_dir`` exclusively (O_CREAT | O_EXCL): ``<prefix><safe name>``, then
        ``-2``, ``-3``… if taken. Never overwrites, renames or deletes anything; a partial download stays on disk
        unreferenced (cleanup is the operator's, brief v2). Returns (path, binary file object)."""
        import os.path
        import re as _re
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        safe = _re.sub(r'[^A-Za-z0-9._-]', '_', Path(str(name)).name)[:120].lstrip('.') or 'file'
        stem, ext = os.path.splitext(safe)
        for n in range(1, 1000):
            candidate = prefix + stem + ('-' + str(n) if n > 1 else '') + ext
            try:
                return self.inbox_dir / candidate, open(self.inbox_dir / candidate, 'xb')
            except FileExistsError:
                continue
        raise channels.Retryable('no free inbox file name for ' + safe)

    def require(self, module, pip=''):
        """Import an optional package for ``start``; ImportError -> channels.MissingDependency (sends keep working)."""
        import importlib
        try:
            return importlib.import_module(module)
        except ImportError as exc:
            raise channels.MissingDependency(pip or module, 'inbound off, sending still works') from exc

    # ---------------------------------------------------------------- audit
    def audit(self, event, **fields):
        if self._audit is None:
            return
        try:  # every field through channels.redact: account values, bearer / bot<token> / key=value secrets
            self._audit(event, channel=self.type, account=self.account.name, **channels.redact(fields, self.account))
        except Exception:  # an audit sink failure must not break a send or a poll
            pass

    def redact(self, text):
        """``text`` with every credential value of this account (6+ chars) and every recognisable secret masked
        (``channels.redact``): error strings, audits and SendResult.error never carry a token, an app secret or a
        password (a token sits in Telegram URLs)."""
        return channels.redact(str(text), self.account)

    def _fail(self, what, exc):
        kind = exc.kind if isinstance(exc, channels.ChannelError) else ''
        self.last_error = self.redact(what + ': ' + type(exc).__name__ + ': ' + str(exc))[:500]
        if kind:
            # the state says what to do (needs_login: run `channel login`); the kind keeps why (auth / bad_config)
            self._state = {'auth': 'needs_login', 'bad_config': 'needs_login', 'conflict': 'conflict',
                           'rate_limited': 'rate_limited', 'network': 'reconnecting',
                           'transient': 'retrying'}[kind]
        self.audit('channel.error', error=self.last_error, kind=kind)
        return SendResult(ok=False, error=self.last_error)

    @staticmethod
    def _result(value):
        """A hook's return: a SendResult as is; anything else (a platform message id, or None for a platform
        that gives no id) means the call returned normally, i.e. sent. Failure is a raise or SendResult(ok=False)."""
        if isinstance(value, SendResult):
            return value
        return SendResult(ok=True, message_id=None if value is None else str(value))

    # ---------------------------------------------------------------- public sends (never raise)
    # ``key`` on every public send: the outbound queue item id, the same on every retry of that item (None for a
    # direct send). During the call it is ``self.send_key`` (plus ':<chunk n>' for chunks after the first), so an
    # adapter can derive a stable platform id from it (email: the Message-ID). Delivery is at least once.
    def send_text(self, conversation, text, *, reply_ref=None, key=None) -> SendResult:
        if not isinstance(conversation, str) or not conversation or not isinstance(text, str) or not text:
            return SendResult(ok=False, error='send_text needs a conversation and non-empty text')
        self.send_key = key
        if self.OVERFLOW == 'file' and text_units(text, self.TEXT_UNITS) > self.TEXT_LIMIT:
            try:
                return self._result(self._send_long_text(conversation, text, reply_ref))
            except Exception as exc:
                return self._fail('send_text', exc)
        first = None
        for i, chunk in enumerate(split_text(text, self.TEXT_LIMIT, self.TEXT_UNITS)):
            self.send_key = key if not key or i == 0 else key + ':' + str(i)
            try:
                got = self._result(self._send_text(conversation, chunk, reply_ref if i == 0 else None))
            except Exception as exc:
                return self._fail('send_text', exc)
            if not got.ok:
                self.last_error = got.error or 'send failed'
                return got
            first = first or got.message_id
        return SendResult(ok=True, message_id=first)

    def send_card(self, conversation, card, *, reply_ref=None, key=None) -> SendResult:
        self.send_key = key
        problem = envelope.card_problem(card)
        if problem:
            return SendResult(ok=False, error=problem)
        if not self.CARRIES_TOKENS:
            card = envelope.redact_card(card)
        if not self.SUPPORTS_CARDS or (card.get('buttons') and not self.SUPPORTS_BUTTONS):
            return self.send_text(conversation, envelope.card_text(card), reply_ref=reply_ref, key=key)
        try:
            return self._result(self._send_card(conversation, card, reply_ref))
        except Exception as exc:
            return self._fail('send_card', exc)

    def update_card(self, conversation, message_id, card, *, key=None) -> SendResult:
        self.send_key = key
        problem = envelope.card_problem(card)
        if problem:
            return SendResult(ok=False, error=problem)
        if not self.CARRIES_TOKENS:
            card = envelope.redact_card(card)
        if not self.SUPPORTS_UPDATE or not message_id:
            return self.send_card(conversation, card, key=key)
        try:
            return self._result(self._update_card(conversation, message_id, card))
        except Exception as exc:
            return self._fail('update_card', exc)

    def send_file(self, conversation, path, caption='', *, reply_ref=None, key=None) -> SendResult:
        self.send_key = key
        p = Path(str(path)).expanduser()
        if not p.is_file():
            return SendResult(ok=False, error='file not found: ' + str(path))
        if 'files' in self.PENDING:
            return SendResult(ok=False, error=self.type + ' file sending is pending a brief v2 decision')
        if not self.SUPPORTS_FILES:
            return self.send_text(conversation, (caption + '\n' if caption else '') + '文件：' + str(p),
                                  reply_ref=reply_ref, key=key)
        try:
            return self._result(self._send_file(conversation, str(p), caption or '', reply_ref))
        except Exception as exc:
            return self._fail('send_file', exc)

    # ---------------------------------------------------------------- inbound (never raises)
    def allowed(self, env):
        """The policy every channel shares (design draft §6): whitelisted sender, private chat, verified."""
        return env['sender'] in self.account.allow and env['private'] and env['verified']

    def private_target_ok(self, conversation):
        """True when ``conversation`` is a one-to-one chat with a whitelisted user, so a push without an inbound
        message to answer (PUSH_TO, a user_only approval card) may go there. Default: the private conversation id
        is the user id (Telegram, Weixin, email), so it must be in ALLOW. Feishu overrides it (a p2p chat id that
        an earlier message from a whitelisted open_id recorded in the store). This never opens a chat: Telegram
        cannot message a user who has not written to the bot first; such a send fails and is audited."""
        return isinstance(conversation, str) and conversation in self.account.allow

    def seen_key(self, env):
        return 'seen:' + env['message_id']  # in this account's own store (default accept)

    def forget(self, env):
        """Roll back a seen mark (the actor could not hand the message to the fleet: let a redelivery in;
        dispatch.py rolled its seen key back when the mailbox push failed)."""
        try:
            self.store.delete(self.seen_key(env))
        except Exception as exc:
            self._fail('forget', exc)

    # Receipt protocol (contracts §8 "入站确认"). For each raw item, in platform order:
    #   parse -> envelopes; each envelope: invalid -> drop_invalid, not allowed -> drop_policy (both deterministic),
    #   else accept(env) -> 'persisted' | 'duplicate'. When every envelope of the item is resolved, the item
    #   is final and ``_ack(raw)`` moves the platform cursor past it. A Retryable (parse_inbound: a download
    #   failed; accept: the seen mark could not be written) or a failing _ack stops the batch: that item and all
    #   later ones stay un-acknowledged and come back on the next poll, where accept answers 'duplicate' for
    #   whatever was already persisted. Nothing is ever turned into a rejection because it kept failing.
    def _ack(self, raw):
        """Move the platform cursor past ``raw`` (Telegram: store offset = update_id + 1; IMAP: the UID). Only
        called once ``raw`` is final. Raise to stop (the item will be fetched again). Default: nothing to ack
        (a push channel whose platform needs no acknowledgement)."""

    def on_accepted(self, env, outcome):
        """Side effect after an envelope is persisted / found duplicate (Telegram: answerCallbackQuery
        「已收到」). Errors are audited, never change the outcome or the cursor."""

    def on_dropped(self, env, reason):
        """Side effect after a deterministic drop (Telegram: an empty answerCallbackQuery). Same rules."""

    def default_accept(self, env):
        """The dedupe when no ``accept`` is passed (tests, a bare ``inbound()``): ``setnx('seen:<message_id>')`` in
        this account's store, SEEN_TTL. The actor does NOT use it: it passes its own accept (P1d
        ``actor.handle_inbound``), the one owner of the same seen key on that path, which hands the envelope to the
        fleet first and answers 'persisted' / 'duplicate' only after the fleet's answer, so the cursor never moves
        past an item the fleet has not answered (contracts §8 入站确认 4)."""
        return 'persisted' if self.store.setnx(self.seen_key(env), '1', channels.SEEN_TTL) else 'duplicate'

    def _hook(self, name, *args):
        try:
            getattr(self, name)(*args)
        except Exception as exc:
            self._fail(name, exc)

    def _receive(self, raw, accept):
        """(persisted envelopes, final?) for one raw item."""
        try:
            found = list(self.parse_inbound(raw) or [])
        except channels.Retryable as exc:
            self._retrying(raw, exc)
            return [], False
        except Exception as exc:  # malformed: retrying cannot change it
            self._fail('parse_inbound', exc)
            self.audit('channel.drop_invalid', error=self.last_error)
            return [], True
        out = []
        for env in found:
            problem = envelope.problems(env)
            if not problem and (env['channel'] != self.type or env['account'] != self.account.name):
                problem = ['envelope names another channel / account']
            if problem:
                self.audit('channel.drop_invalid', error='; '.join(problem)[:500])
                continue
            if not self.allowed(env):
                # Not answered (design draft §4.2): only logged, with who and where, never the text.
                self.audit('channel.drop_policy', sender=env['sender'], conversation=env['conversation'],
                           message_id=env['message_id'], private=env['private'], verified=env['verified'])
                self._hook('on_dropped', env, 'policy')
                continue
            try:
                outcome = (accept or self.default_accept)(env)
            except channels.Retryable as exc:
                self._retrying(raw, exc)
                return out, False
            except Exception as exc:  # an accept that fails in another way is still not a receipt
                self._retrying(raw, channels.Retryable(type(exc).__name__ + ': ' + str(exc)))
                return out, False
            if outcome == 'duplicate':
                self.audit('channel.drop_dup', message_id=env['message_id'])
            elif outcome != 'persisted':
                self._retrying(raw, channels.Retryable('accept answered ' + repr(outcome)))
                return out, False
            else:
                out.append(env)
            self._hook('on_accepted', env, outcome)
        return out, True

    def _retrying(self, raw, exc):
        self._state = 'retrying'
        self.last_error = self.redact(str(exc))[:500]
        self.audit('channel.inbound_retrying', error=self.last_error)

    def inbound(self, raw, accept=None):
        """Receive one raw item (a pushed event, or one item of a poll). Returns the persisted envelopes."""
        if not self.RECEIVES:
            return []
        out, final = self._receive(raw, accept)
        if final:
            try:
                self._ack(raw)
            except Exception as exc:
                self._fail('ack', exc)
        return out

    def poll(self, timeout=0, accept=None):
        """Fetch and receive in order; stop at the first item that is not final (see the protocol above)."""
        if not self.RECEIVES:
            return []
        try:
            raws = list(self._poll(timeout) or [])
        except Exception as exc:
            self._fail('poll', exc)
            return []
        out = []
        for raw in raws:
            got, final = self._receive(raw, accept)
            out += got
            if not final:
                break
            try:
                self._ack(raw)
            except Exception as exc:
                self._fail('ack', exc)
                break
        else:
            if getattr(self, '_state', '') == 'retrying':
                self._state = ''
        return out
