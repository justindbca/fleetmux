"""Channels: the supervisor's other keyboard and the fleet's other way out (docs/design.md §53, contracts §8).

Frozen interface (phase A, package P0). Everything a channel package needs to build against lives in three
modules:

* this file: the adapter registry (``ADAPTER_MODULES``), message-id prefixes, the roster ``channels:`` rules
  (``parse_channels``), the push-event names, the Redis key names, the machine-local config / lease / state
  paths and the account file loader (``load_account``);
* ``base.py``: the adapter base class (ported from notify_bus ``channels/base.py``) with the never-raise
  wrappers, text splitting, card fallback, whitelist / idempotency policy and an in-memory store;
* ``envelope.py``: the inbound envelope, the origin of a request, the outbound queue item, the neutral card,
  the command grammar and the fixed receipt texts.

fleetmux is generic infrastructure: nothing here knows a business, a group id or a person. Credentials and
whitelists live only in ``config_dir()``, default ``~/.fleetmux/config/`` (never in a roster, a kit, a run directory or a worker cwd).
"""
import os
import re
from pathlib import Path

from fleetmux import paths

# ------------------------------------------------------------------ adapters
# type -> module that defines ``ADAPTER`` (a base.Channel subclass). Imported lazily: a missing optional
# dependency (lark-oapi for feishu, websocket-client for slack) disables that one channel, never the others
# or the fleet.
ADAPTER_MODULES = {
    'telegram': 'fleetmux.channels.telegram',   # P2
    'feishu': 'fleetmux.channels.feishu',       # P3
    'weixin': 'fleetmux.channels.weixin',       # P4 (official ClawBot iLink only)
    'email': 'fleetmux.channels.email',         # P5
    'exec': 'fleetmux.channels.exec',           # P6 ② (outbound only: JSON on stdin of a configured command)
    'slack': 'fleetmux.channels.slack',         # P9 (Socket Mode inbound, Web API outbound; brief v2.3 §9)
}
# message_id = '<prefix>:<platform ids>' so every channel shares one seen-key space (telegram_poll's 'tg:').
MESSAGE_ID_PREFIX = {'telegram': 'tg', 'feishu': 'fs', 'weixin': 'wx', 'email': 'em', 'exec': 'ex', 'slack': 'sk'}

_registered = {}  # tests / embedders: type -> (adapter class, message-id prefix); see register()


def register(type_, adapter, prefix):
    """Register an extra adapter type (tests: a fake channel; embedders: their own). Returns an undo callable."""
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,31}', type_ or '') or not re.fullmatch(r'[a-z][a-z0-9]{0,7}', prefix or ''):
        raise ValueError('channel type must match [a-z][a-z0-9_]{0,31} and prefix [a-z][a-z0-9]{0,7}')
    _registered[type_] = (adapter, prefix)
    return lambda: _registered.pop(type_, None)


def known_types():
    return tuple(ADAPTER_MODULES) + tuple(t for t in _registered if t not in ADAPTER_MODULES)


def prefix(type_):
    if type_ in _registered:
        return _registered[type_][1]
    return MESSAGE_ID_PREFIX[type_]


class Unavailable(RuntimeError):
    """The adapter of this type cannot be loaded here (module not built yet, optional dependency missing).
    The actor reports it (status ``unavailable``, audit ``channel.unavailable``) and runs the other channels."""


class MissingDependency(Unavailable):
    """An adapter's optional package is not installed (feishu: lark-oapi). Raised by ``Channel.start()`` only:
    the module still imports and sends still work, only inbound is off (status ``missing_dependency``)."""

    def __init__(self, package, hint=''):
        self.package, self.hint = package, hint
        super().__init__(package + ' not installed' + ('; ' + hint if hint else ''))


ERROR_KINDS = ('auth', 'conflict', 'rate_limited', 'network', 'bad_config', 'transient')


class ChannelError(RuntimeError):
    """An inbound-side failure an adapter raises from ``_poll`` / ``_ack`` / ``start`` / ``check`` (sends never
    raise: they return SendResult). ``kind``: auth (credential rejected, e.g. HTTP 401), conflict (another
    consumer of this credential elsewhere, e.g. Telegram 409), rate_limited (``retry_after`` seconds, e.g. 429),
    network, bad_config, transient. ``detail`` must already be free of secrets (Channel.redact)."""

    def __init__(self, kind, detail='', retry_after=None):
        if kind not in ERROR_KINDS:
            raise ValueError('ChannelError kind must be one of ' + ', '.join(ERROR_KINDS))
        self.kind, self.detail, self.retry_after = kind, detail, retry_after
        super().__init__(kind + (': ' + detail if detail else ''))


class Retryable(ChannelError):
    """Not received yet, try again (a download failed, the seen mark could not be written): the platform cursor
    must NOT move past this item. Raised by ``parse_inbound`` or by the actor's ``accept``."""

    def __init__(self, detail='', retry_after=None):
        super().__init__('transient', detail, retry_after)


def adapter_class(type_):
    if type_ in _registered:
        return _registered[type_][0]
    if type_ not in ADAPTER_MODULES:
        raise Unavailable('unknown channel type: ' + str(type_))
    import importlib
    try:
        module = importlib.import_module(ADAPTER_MODULES[type_])
    except ImportError as exc:
        raise Unavailable(type_ + ' channel not installed: ' + str(exc)) from exc
    cls = getattr(module, 'ADAPTER', None)
    if cls is None:
        raise Unavailable(ADAPTER_MODULES[type_] + ' defines no ADAPTER')
    return cls


# ------------------------------------------------------------------ push events (roster ``push:``)
# design draft §5.1. ``escalation`` (worker blocked / non-delete dialog waiting for the supervisor) is
# information only and off unless listed.
PUSH_EVENTS = ('result', 'task_done', 'prompt_wait', 'escalation', 'run_closed', 'daemon_down', 'security')
DEFAULT_PUSH = ('result', 'task_done', 'prompt_wait', 'run_closed', 'daemon_down', 'security')  # security: P8a alerts, on
# Outbound queue items also carry these two: a receipt to an inbound message, and `channel send` by hand.
OUT_EVENTS = PUSH_EVENTS + ('reply', 'manual')

# ------------------------------------------------------------------ roster ``channels:``
ACCOUNT_RE = r'[a-z0-9][a-z0-9._-]{0,63}'
ENTRY_KEYS = ('type', 'account', 'push')
MAX_CHANNELS = 8


def parse_channels(value):
    """Validate the roster's top-level ``channels:`` value. Returns (entries, errors).

    entries: [{'type', 'account', 'push': [...]}] in file order (the order is the fallback route: the first
    channel gets a push when nobody has talked to the fleet yet). errors: one message per problem, each naming
    ``channels[i]``; any error rejects the whole roster (same rule as the rest of roster.py). ``None`` (key
    absent) is no channels. Secrets and whitelists are never roster keys."""
    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], ['channels: must be a list of {type, account, push?} mappings']
    if len(value) > MAX_CHANNELS:
        return [], ['channels: at most ' + str(MAX_CHANNELS) + ' entries']
    entries, errors, seen = [], [], set()
    for i, item in enumerate(value):
        where = 'channels[' + str(i) + ']'
        if not isinstance(item, dict):
            errors.append(where + ': must be a mapping {type, account, push?}')
            continue
        extra = sorted(str(k) for k in item if k not in ENTRY_KEYS)
        if extra:
            errors.append(where + ': unknown key(s) ' + ', '.join(extra) + ' (allowed: ' + ', '.join(ENTRY_KEYS)
                          + '; credentials and whitelists live in ' + str(config_dir()) + ', never in a roster)')
        type_, account = item.get('type'), item.get('account')
        if type_ not in known_types():
            errors.append(where + ': type must be one of ' + ', '.join(known_types()) + ', got ' + repr(type_))
        if not isinstance(account, str) or not re.fullmatch(ACCOUNT_RE, account):
            errors.append(where + ': account must match ' + ACCOUNT_RE + ', got ' + repr(account))
        push = item.get('push', list(DEFAULT_PUSH))
        if (not isinstance(push, list) or not all(isinstance(p, str) for p in push)
                or len(set(push)) != len(push)):
            errors.append(where + ': push must be a list of distinct event names')
            push = []
        bad = [p for p in push if p not in PUSH_EVENTS]
        if bad:
            errors.append(where + ': unknown push event(s) ' + ', '.join(bad) + ' (known: ' + ', '.join(PUSH_EVENTS) + ')')
        if (type_, account) in seen:
            errors.append(where + ': duplicate channel ' + str(type_) + ':' + str(account)
                          + ' (one credential, one consumer)')
        if type_ == 'feishu' and any(t == 'feishu' for t, _ in seen):
            errors.append(where + ': at most one feishu channel per roster (lark-oapi 1.5.3 runs one WS client '
                          'per process, and a fleet has one actor process)')
        seen.add((type_, account))
        entries.append({'type': type_, 'account': account, 'push': list(push)})
    return (entries, []) if not errors else ([], errors)


# ------------------------------------------------------------------ machine-local paths
CONFIG_ENV = 'FLEETMUX_CONFIG_DIR'  # tests point this at a temp dir; never the operator's real home


def config_dir():
    """``$FLEETMUX_CONFIG_DIR`` else ``~/.fleetmux/config`` (fleetmux/paths.py): credentials, whitelists, leases,
    cursor mirrors."""
    return Path(os.environ.get(CONFIG_ENV) or paths.config_dir()).expanduser()


def _check(type_, account):
    if not isinstance(type_, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,31}', type_):
        raise ValueError('bad channel type: ' + repr(type_))
    if not isinstance(account, str) or not re.fullmatch(ACCOUNT_RE, account):
        raise ValueError('bad channel account: ' + repr(account))
    return type_ + '-' + account


def account_path(type_, account):
    """One account = one credential = one file: ``<config>/channels/<type>-<account>.env`` (mode 0600)."""
    return config_dir() / 'channels' / (_check(type_, account) + '.env')


def lease_path(type_, account):
    """Machine-wide consumer lease: flock(LOCK_EX | LOCK_NB) on this file, holder JSON inside (lease.py, P1)."""
    return config_dir() / 'leases' / (_check(type_, account) + '.lock')


def state_path(type_, account):
    """RESERVED, not used until brief v2 decides cross-run cursors (offset / get_updates_buf / IMAP UID). Today
    cursors live only in the run's private Redis (brief §2); nothing may write here."""
    return config_dir() / 'state' / (_check(type_, account) + '.json')


def last_path():
    """Mirror of the last interaction (``fx:channel:last``) for `channel send` while no fleet is up."""
    return config_dir() / 'state' / 'last.json'


class Account:
    """One account file, parsed. ``allow``: the whitelist (sender ids as the adapter reports them);
    ``values``: every other KEY=VALUE (credentials and options; adapter-specific names, upper case)."""

    def __init__(self, type_, name, allow=(), values=None):
        _check(type_, name)
        self.type, self.name = type_, name
        self.allow = frozenset(str(a).strip() for a in allow if str(a).strip())
        self.values = dict(values or {})

    def get(self, key, default=''):
        return self.values.get(key, default)

    def __repr__(self):  # never print secrets
        return 'Account(' + self.type + ':' + self.name + ', allow=' + str(len(self.allow)) + ', keys=' \
            + ','.join(sorted(self.values)) + ')'


# P9a ruling 1: a channel type's own whitelist key, read exactly like ALLOW (into Account.allow, not values)
ALLOW_SYNONYMS = {'slack': 'SLACK_ALLOW_USERS'}


def parse_account_text(type_, name, text):
    """KEY=VALUE lines; ``#`` comments and blanks skipped; keys upper case; values stripped of spaces, of ONE
    matching pair of outer quotes (``"…"`` or ``'…'``; inner quotes are kept, so ``COMMAND="/bin/relay --x 'a b;c'"``
    keeps its shlex quoting) and of a trailing CR (notify_bus config.py: a CRLF file put ``\\r`` into a token URL,
    2026-07-06). ``ALLOW`` is the whitelist, comma or space separated; ``ALLOW_SYNONYMS[type_]``
    (slack: ``SLACK_ALLOW_USERS``, P9a ruling 1) is the same key: it goes into ``allow``, never into ``values``,
    and empty means reject all. ValueError names the bad line number, never its value."""
    values, allow = {}, []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        key, sep, value = line.partition('=')
        key = key.strip()
        if not sep or not re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}', key):
            raise ValueError(str(account_path(type_, name)) + ': line ' + str(n) + ' is not KEY=VALUE (KEY upper case)')
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        if key == 'ALLOW' or key == ALLOW_SYNONYMS.get(type_):
            allow += [a for a in re.split(r'[,\s]+', value) if a]
        else:
            values[key] = value
    return Account(type_, name, allow, values)


def load_account(type_, name):
    """Read ``account_path``. Missing file -> FileNotFoundError: the actor reports that account as ``needs_login``
    (error kind ``bad_config``, only that account is off; `channel login` fixes it)."""
    path = account_path(type_, name)
    return parse_account_text(type_, name, path.read_text(encoding='utf-8'))


# ------------------------------------------------------------------ exec account keys (P6 ②; contracts §8)
EXEC_TIMEOUT_S = 30  # TIMEOUT_S default (lifecycle_close._run_command)


def exec_argv(value):
    """The exec account file's ``COMMAND=`` value -> argv list (brief v2 §2.1-1, supervisor ruling): ``shlex.split``
    (POSIX rules), then ``subprocess`` with ``shell=False`` -- never a shell, so no pipes / ``;`` / ``$VAR`` /
    ``~`` / glob expansion happen (shlex only honours quotes and backslashes). Empty, unbalanced quotes or a NUL ->
    ValueError, which says what is wrong and never echoes the value (it may hold a relay URL or token)."""
    import shlex
    if not isinstance(value, str) or not value.strip():
        raise ValueError('COMMAND is empty')
    if '\0' in value:
        raise ValueError('COMMAND contains a NUL byte')
    try:
        argv = shlex.split(value)
    except ValueError:
        raise ValueError('COMMAND is not a valid shlex command line (unbalanced quotes)') from None
    if not argv:
        raise ValueError('COMMAND is empty')
    return argv


def exec_timeout(value):
    """``TIMEOUT_S=`` -> positive finite seconds; empty / missing -> EXEC_TIMEOUT_S."""
    import math
    if value in (None, ''):
        return EXEC_TIMEOUT_S
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise ValueError('TIMEOUT_S must be a positive number of seconds') from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError('TIMEOUT_S must be a positive number of seconds')
    return seconds


# ------------------------------------------------------------------ Redis keys (the run's private Redis)
# Every key the channels add is under this prefix; no existing key changes meaning (brief §5).
KEY_PREFIX = 'fx:channel:'
# Inbound dedupe (brief v2 §0-2): the base's default accept, ``store.setnx('seen:<message_id>')`` in the
# account's own store -- for the actor a Redis store under STORE + '<type>:<account>:' (P1d actor.RedisStore), so
# ids are account-scoped. ``forget()`` deletes it when the fleet did not take the message (retry possible).
# There is no inbox stream: nothing may require one (supervisor ruling C14).
SEEN_TTL = 3600                    # the seen mark's lifetime (default accept, actor and tests alike)
SEEN = KEY_PREFIX + 'seen:'        # kept name only (no longer the dedupe key; v2 dedupes in the account store)
INBOX_SEEN_TTL = 7 * 86400         # kept name only (no inbox stream in v2)
def seen_key(env):
    """An account-scoped name for an inbound envelope (kept for compatibility; the v2 dedupe key is the account
    store's ``seen:<message_id>``)."""
    return SEEN + env['channel'] + ':' + env['account'] + ':' + env['message_id']


INBOX = KEY_PREFIX + 'inbox:'      # kept name only: no inbox stream in v2, no code may write or require it
ORIGIN = KEY_PREFIX + 'origin:'    # + request id or task id; JSON origin (envelope.origin_from). Written by the
                                   # actor once the fleet answered accepted, SET EX ORIGIN_TTL (never by Fleet.handle)
ORIGIN_TTL = 7 * 86400             # 7 days from acceptance
ORIGIN_GRACE_S = 60                # a result found without an origin younger than this is looked at again next
                                   # scan (actor write vs. a fast answer); older -> marked pushed, never sent
LAST = KEY_PREFIX + 'last'         # hash: channel, account, conversation, reply_ref (JSON), sender, at
OUTQ = KEY_PREFIX + 'outq'         # list of outbound items (envelope.outbound), target not yet resolved: LPUSH
                                   # producers; the actor's route step resolves the target and moves the item to
OUTQ_ACCOUNT = KEY_PREFIX + 'outq:'  # + '<type>:<account>'; one queue per account, drained by that account's sender
                                     # inside the fleet's single actor (throttle / retry / dead letter per account)
DEAD = KEY_PREFIX + 'dead'         # list: items that failed every retry, capped at 1000 (consumer.py dead letter)
PUSHED = KEY_PREFIX + 'pushed:'    # + '<event>:<id>'; SET NX EX ORIGIN_TTL: a push survives actor restarts once
PENDING = KEY_PREFIX + 'pending:'  # + pending id ('p-' + 8 hex); hash: a delete dialog waiting for the user's
                                   # answer (brief v2 §2.1-5 / -13). Owner: P6b `channels/approve.py`
                                   # (register / card / handle_command / mark_done); written only in runs with channels
PENDING_SLOT = KEY_PREFIX + 'pending_slot:'  # + slot; the slot's current pending id (a new dialog / generation
                                             # marks the old record stale)
# Approval time limits: configurable positive defaults (supervisor-approved; run config.json keys
# approve_confirm_s / approve_ttl_s; config validation and behaviour land test-first in v2, not in P0).
# Past either one the approval fails closed: no key, status expired, the dialog stays for the person at the desk.
APPROVE_CONFIRM_S = 120   # step one -> step two
APPROVE_TTL_S = 1800      # the whole record
STATUS = KEY_PREFIX + 'status:'    # + '<type>:<account>'; hash: state, holder, last_in, last_out, last_error, ...
STORE = KEY_PREFIX + 'store:'      # + '<type>:<account>:<key>'; the adapter's store (cursors, tokens cache)
ACTOR = KEY_PREFIX + 'actor'       # hash: the fleet's ONE actor process: pid, started, beat (heartbeat, s; its own
#   thread every BEAT_S, never blocked by a send), sending / sending_at (the item being sent, set by P1e right
#   around the real adapter call and cleared in finally), drain (deadline the daemon's channel_stop_all writes),
#   drained (1 once the actor sent everything and holds nothing in flight). The actor checks its parent only with
#   os.getppid(), never by probing daemon.lock.
BEAT_S = 10

# ------------------------------------------------------------------ process model (contracts §8; brief v2, ruling C13)
# ONE actor process per fleet: `fleetmux channel actor <config.json>`, a child of the daemon, which supervises it
# (Fleet.channel_tick, P1f). Inside it every (type, account) entry has its own inbound thread and its own outbound
# queue (OUTQ_ACCOUNT). One account failing (needs login, lease held elsewhere, adapter crash) only stops that
# account; it never ends the actor, never touches another account and never pauses the fleet. The daemon never
# imports an adapter; send_direct (channel send / lifecycle_close) only sends: no inbound, no lease. A run without
# `channels:` starts no actor process and behaves exactly as before.
ACTOR_EXIT_CONFIG = 3  # kept name only: no single account makes the actor exit in v2 (see CONFIG_RETRY_S)
ACTOR_EXIT_LEASE = 4   # kept name only: a held lease disables that account, retried every lease_retry_s
CONFIG_RETRY_S = 600   # an account whose credentials failed is retried when its account file changes, else this often
# Timings: configurable defaults (run config.json `channel_supervise: {<key>: <number>}`; a missing key takes the
# default, a bad value is refused by start / daemon config validation like every other config key).
SUPERVISE_DEFAULTS = {
    'backoff_start_s': 30,    # the actor process: first restart delay after an exit, doubling each time
    'backoff_cap_s': 300,     # restart delay cap (notify_bus daemon.py: 30 s doubling, 5 min cap)
    'stable_reset_s': 600,    # a process up this long resets the backoff
    'failing_after': 5,       # consecutive failures before channel.actor.failing (an alarm, never a fleet stop)
    'lease_retry_s': 300,     # inside the actor: an account whose lease is held elsewhere is retried this often
    'config_retry_s': CONFIG_RETRY_S,  # inside the actor: an account with failed credentials (or its file changes)
}
# Inbound states an adapter reports through Channel.health() (status hash, `channel status`, control):
# ok (healthy), needs_login (credentials refused, or the account file missing / bad: error kind auth or
# bad_config; `channel login` fixes it; only that account is off), retrying (a transient inbound failure: the cursor
# stays, the item comes back next poll). The actor alone also writes lease_busy into the status hash.
INBOUND_STATES = ('ok', 'reconnecting', 'retrying', 'needs_login', 'missing_dependency', 'conflict', 'rate_limited',
                  'stopped', 'outbound_only')

# Audit events (the run's v2 audit stream, `fleetmux logs`). Detail fields are listed in contracts §8.
AUDIT_EVENTS = (
    'channel.in', 'channel.out', 'channel.send_failed', 'channel.dead',
    'channel.drop_policy', 'channel.drop_dup', 'channel.drop_size', 'channel.drop_invalid', 'channel.drop_auth',
    'channel.file_partial',
    'channel.error', 'channel.no_target', 'channel.lease_busy', 'channel.unavailable', 'channel.inbound_retrying',
    'channel.actor.started', 'channel.actor.exited', 'channel.actor.restarted', 'channel.actor.failing',
    'dialog.user_answered', 'dialog.user_refused',
    'channel.cursor_reset', 'channel.lease_reclaimed', 'dialog.user_resumed',  # P0b (brief v2 §2.2)
    # P8a (brief v2.2 §8.2 ④): the security pack
    'security.stranger_alert', 'security.untrusted_request', 'security.reader_summary', 'security.reader_failed',
    'security.secret_blocked', 'security.link_rewritten', 'security.canary_tripped', 'security.action_pending',
    'security.action_approved', 'security.action_denied', 'security.action_timeout', 'security.oob_refused',
    'security.stop', 'security.limit_tripped', 'security.audit_chain', 'security.sandbox_off',
    'security.tool_call',  # P8j: after-the-fact row, audit only; guaranteed only on P8j's pi bridge (others: by capability)
)
# P8a keys of the security pack (contracts §8 key table; the packages that write them are named there)
UNTRUSTED = KEY_PREFIX + 'untrusted:'   # + envelope.origin_id(request_id); comma-separated outside kinds (P8d), EX 7 d
STOP = KEY_PREFIX + 'stop'              # hash {level, by, at, id, reason, epoch, rank}: an unhandled /stop (P8m Z2)
STOP_REQ = KEY_PREFIX + 'stop_req:'     # + request id; hash {epoch, level, at}, EX 604800: id already published (Z3)
STOP_DONE = KEY_PREFIX + 'stop_done:'   # + epoch; hash <slot> attempted/sent/send_failed, done, audited, EX 604800 (Z4)
STRANGERS = KEY_PREFIX + 'strangers:'   # + '<type>:<account>'; hash {start, count}: non-whitelisted senders in the window
#   (P8d; one Lua script counts, resets the window, checks the cooldown key PUSHED + 'security:strangers:<t>:<a>'
#   (string: last alert time) and LPUSHes the alert, atomically)
LIMIT = KEY_PREFIX + 'limit:'           # + 'in:<type>:<account>:<sender>' / 'req' / 'out' counters (P8n)
CANARY = KEY_PREFIX + 'canary'          # string: this run's canary value; P8h SETs it at start and again on resume
#                                         (off / no policy -> unwritten, never read)
TOTP_USED = KEY_PREFIX + 'totp_used:'   # + code; one use per code (P8l)
AUDITLOG = KEY_PREFIX + 'auditlog'      # hash {last_id}: last audit stream id written to the chain (P8g; no policy -> unwritten)
GUARD_TICKET = 'fx:guard:ticket:'       # + key_hash (local: action_hash, routed: route_hash); single use (P8k/P8i)
GUARD_WAIT = 'fx:guard:wait:'           # + key_hash (as GUARD_TICKET); the PreToolUse hook waits on it (P8i / P8k)
GUARD_CALLS = 'fx:guard:calls:'         # + <run_id>:<slot>:<generation>:<delivery_id or 'startup'>; tool calls (P8i / P8n)


# ------------------------------------------------------------------ redaction (P0b, brief v2 §2.2 ①)
SECRET_KEY_RE = re.compile(r'token|secret|password|authorization', re.I)
_AUTH_HEADER = re.compile(r'(?i)\b((?:proxy-)?authorization)\s*:\s*[^\r\n]*')
_BEARER = re.compile(r'(?i)\bbearer\s+[^\s"\',;]+')
_BOT_URL = re.compile(r'/bot[^/\s?#"\']+')
_SLACK_TOKEN = re.compile(r'\bx(?:oxb|oxp|app)-[A-Za-z0-9-]+')  # P9a: Slack bot / user / app tokens, prefix too
_KEY_VALUE = re.compile(r'(?i)\b([a-z0-9_.-]*(?:token|secret|password)[a-z0-9_.-]*)(\s*[=:]\s*)(["\']?)'
                        r'(?!\d+\b)[^\s"\'&,;]+')
# M150 (owner change; wx-1 M145 / e9643d4, weixin.py _WX_SECRET_PATTERNS): a JSON-quoted secret-named key
# ("context_token": "…", "typing_ticket": "…"), the QR / login keys (qrcode=, verify_code=, qrcode_img_content=) and a
# key-like URL parameter (?key= / &ticket= / &sign= / &auth=). Group 1 is kept, the value becomes ***.
_EXTRA_SECRET_PATTERNS = (
    re.compile(r'(?i)("[A-Za-z0-9_.-]*(?:token|secret|password|ticket)[A-Za-z0-9_.-]*"\s*:\s*")[^"]*'),
    re.compile(r'(?i)(\b(?:qrcode_img_content|qrcode|verify_code)\s*[=:]\s*["\']?)[^\s"\'&,;#]+'),
    re.compile(r'(?i)([?&](?:key|ticket|sign|signature|auth)=)[^\s"\'&#]+'),
)


def _account_values(account):
    values = account if isinstance(account, dict) else getattr(account, 'values', None)
    if not isinstance(values, dict):
        return []
    return sorted((v for v in values.values() if isinstance(v, str) and len(v) >= 6), key=len, reverse=True)


def redact(value, account=None):
    """``value`` with secrets masked as ``***``; strings, dicts and lists (recursively), anything else as is.
    Masked: every value of ``account`` (a channels.Account or its values dict) of 6+ chars, an ``Authorization:``
    header value, ``Bearer <token>``, the ``bot<token>`` URL segment, a Slack token (``xoxb-`` / ``xoxp-`` / ``xapp-``,
    the prefix masked too), ``<name>=<value>`` / ``<name>: <value>`` where
    the name holds token / secret / password, a JSON-quoted key holding token / secret / password / ticket
    (``"context_token": "…"``, ``"typing_ticket": "…"``), the login keys ``qrcode=`` / ``verify_code=`` /
    ``qrcode_img_content=`` and the URL parameters ``key= / ticket= / sign= / signature= / auth=`` (M150), and in a
    dict every string value whose key holds token / secret / password / authorization. Numbers are kept (``{'tokens': 5}`` stays). Every string Channel._fail and
    Channel.audit emit goes through it."""
    if isinstance(value, dict):
        return {k: ('***' if isinstance(v, str) and v and SECRET_KEY_RE.search(str(k)) else redact(v, account))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact(v, account) for v in value)
    if not isinstance(value, str):
        return value
    text = value
    for secret in _account_values(account):
        text = text.replace(secret, '***')
    text = _AUTH_HEADER.sub(lambda m: m.group(1) + ': ***', text)
    text = _BEARER.sub('Bearer ***', text)
    text = _BOT_URL.sub('/bot***', text)
    text = _SLACK_TOKEN.sub('***', text)
    text = _KEY_VALUE.sub(lambda m: m.group(1) + m.group(2) + m.group(3) + '***', text)
    for pattern in _EXTRA_SECRET_PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + '***', text)
    return text


def fleet_label(cfg):
    """The fleet's display name for a channel (mail plan A, I2): cfg name, else token, else the run directory's
    name; the same chain as lifecycle_close.fleet_name. No cfg / none of the three -> '' (never an account name)."""
    if not isinstance(cfg, dict):
        return ''
    if cfg.get('name') or cfg.get('token'):
        return str(cfg.get('name') or cfg.get('token'))
    return Path(str(cfg['run'])).name if cfg.get('run') else ''
