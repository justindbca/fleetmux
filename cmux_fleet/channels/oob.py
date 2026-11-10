"""Out-of-band second step for high-tier actions (P8l; contracts §8 "高危带外方式 desk / second_channel / totp",
approve-1 P8kl-freeze-candidate.md C1 / O7 / X3).

``check(rec, ev, policy, *, r, now=None) -> (ok, reason)`` checks a step-two event against the record's frozen
``oob_modes`` (X3: never the current policy; ``policy`` is accepted for the interface only). ``approve.answer_action``
calls it last, after the nonce / window / identity checks; the ISSUE script then re-checks the record's version
before any ticket is written. The common checks (one-time nonce, ``approve_confirm_s`` / ``approve_ttl_s``, the
pending record binding) stay in ``answer_action`` for every mode.

  * ``desk``: only the local ``cmux-fleet channel approve`` (P8l CLI, after P1g): ``ev['desk_proof']`` must equal
    ``desk_proof(key, rec)``, an HMAC-SHA256 over the record's id and nonce hash with ``config_dir()/desk.key``
    (``approve.desk_key``). ``channel == 'desk'`` alone proves nothing (O7). No key file -> refused (never created
    here).
  * ``second_channel``: the step comes from another bound ``type:account`` than step one, from a sender in that
    account's ALLOW, in a private verified chat.
  * ``totp``: the same sender and conversation as step one plus ``ev['code']``, an RFC 6238 code (SHA-1, 30 s,
    6 digits, +-1 step) from ``config_dir()/totp.env`` ``TOTP_SECRET`` (base32); a code is used once
    (``SET NX fx:channel:totp_used:<code> EX 90``).

Never raises for a bad event (a reason instead); the reason never carries a secret, a code or the nonce.
"""
import base64
import hashlib
import hmac
import re
import struct
import time

from cmux_fleet import channels

TOTP_STEP_S = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1
TOTP_USED_TTL_S = 90
MODES = ('desk', 'second_channel', 'totp')


def desk_proof(key, rec):
    """What the local CLI puts into ``desk_proof``: bound to this record and its current confirmation (nonce hash)."""
    msg = ('desk\0' + str(rec.get('id', '')) + '\0' + str(rec.get('nonce_hash', ''))).encode()
    return hmac.new(str(key).encode(), msg, hashlib.sha256).hexdigest()


def _read_desk_key():
    path = channels.config_dir() / 'desk.key'
    try:
        value = path.read_text(encoding='utf-8').strip()
    except OSError:
        return ''
    return value


def totp_secret():
    """The base32 ``TOTP_SECRET`` of ``config_dir()/totp.env`` as bytes; b'' when missing / malformed."""
    path = channels.config_dir() / 'totp.env'
    try:
        text = path.read_text(encoding='utf-8')
    except OSError:
        return b''
    for line in text.splitlines():
        name, _, value = line.partition('=')
        if name.strip() == 'TOTP_SECRET':
            value = re.sub(r'\s', '', value).upper()
            try:
                return base64.b32decode(value + '=' * (-len(value) % 8))
            except (ValueError, TypeError):
                return b''
    return b''


def totp_code(secret, counter, digits=TOTP_DIGITS):
    """RFC 4226 HOTP on the RFC 6238 counter (SHA-1)."""
    digest = hmac.new(secret, struct.pack('>Q', counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % (10 ** digits)).zfill(digits)


def _check_desk(rec, ev):
    if ev.get('channel') != 'desk':
        return False, 'desk mode: only the local cmux-fleet channel approve answers'
    proof = ev.get('desk_proof')
    key = _read_desk_key()
    if not proof or not key or not isinstance(proof, str):
        return False, 'desk mode: no valid desk proof'
    if not hmac.compare_digest(proof, desk_proof(key, rec)):
        return False, 'desk mode: no valid desk proof'
    return True, ''


def _check_second_channel(rec, ev):
    first = (rec.get('channel', ''), rec.get('account', ''))
    here = (ev.get('channel', ''), ev.get('account', ''))
    if not all(first) or not all(here):
        return False, 'second channel: step one / step two channel unknown'
    if here == first:
        return False, 'second channel: step two came from the step-one channel'
    if ev.get('private') is not True or ev.get('verified') is not True:
        return False, 'second channel: not a private verified chat'
    try:
        account = channels.load_account(here[0], here[1])
    except (OSError, ValueError):
        return False, 'second channel: account not bound'
    if ev.get('sender') not in account.allow:
        return False, 'second channel: sender not in that account\'s whitelist'
    return True, ''


def _check_totp(rec, ev, r, now):
    if ev.get('sender') != rec.get('sender') or ev.get('conversation') != rec.get('conversation'):
        return False, 'totp: not the step-one sender / conversation'
    code = ev.get('code')
    if not isinstance(code, str) or not re.fullmatch(r'[0-9]{' + str(TOTP_DIGITS) + '}', code):
        return False, 'totp: no 6-digit code'
    secret = totp_secret()
    if not secret:
        return False, 'totp: not set up (channel login totp)'
    counter = int(now // TOTP_STEP_S)
    if not any(hmac.compare_digest(code, totp_code(secret, c))
               for c in range(counter - TOTP_WINDOW, counter + TOTP_WINDOW + 1) if c >= 0):
        return False, 'totp: wrong code'
    if not r.set(channels.TOTP_USED + code, '1', nx=True, ex=TOTP_USED_TTL_S):
        return False, 'totp: code already used'
    return True, ''


def check(rec, ev, policy, *, r, now=None):
    """(True, '') when the step-two event ``ev`` satisfies the record's frozen ``oob_modes``; else (False, reason)."""
    rec, ev = dict(rec or {}), dict(ev or {})
    mode = rec.get('oob_modes', '')
    if rec.get('kind') != 'action' or rec.get('tier') != 'high':
        return False, 'out-of-band check is for high-tier actions only'
    if mode not in MODES:
        return False, 'no valid confirmation mode on the record'
    now = time.time() if now is None else float(now)
    if mode == 'desk':
        return _check_desk(rec, ev)
    if mode == 'second_channel':
        return _check_second_channel(rec, ev)
    return _check_totp(rec, ev, r, now)
