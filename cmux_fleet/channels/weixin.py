"""Weixin channel: the official ClawBot iLink protocol only (docs/design.md §53, contracts §8 微信专项).

Ported to the Python standard library from the MIT-licensed @tencent-weixin/openclaw-weixin 2.4.10 source (only
the protocol; none of the OpenClaw shell). Every protocol fact here comes from that source and is NOT verified
against the real service: whether a non-OpenClaw client may log in and exchange messages, whether a push without
a fresh ``context_token`` arrives, and how many ClawBots one Weixin account may bind are checked on a real device
after delivery, outside the development run.

Never: local decryption, reading Weixin's local data or database, keys from memory. Phase one is text only
(brief v2 §2.1-12): inbound media are refused (``rejected_files``), no CDN download or upload, no AES.

P4a: ``LOGIN(account_name, io, transport)``, the QR login used by `cmux-fleet channel login weixin`, and the
request helpers (headers, ``base_info``) every call shares. P4b: ``WeixinChannel`` (= ``ADAPTER``), receive through
``getupdates`` and send through ``sendmessage``.
"""
import base64
import hashlib
import json
import os
import re
import time
import urllib.parse

from cmux_fleet import __version__ as FLEETMUX_VERSION
from cmux_fleet import channels
from cmux_fleet.channels import base, envelope

# ------------------------------------------------------------------ protocol constants (2.4.10 source)
FIXED_HOST = 'https://ilinkai.weixin.qq.com'      # login-qr.ts:39: QR code fetch and first status polls
DEFAULT_BASE_URL = 'https://ilinkai.weixin.qq.com'  # accounts.ts:13: business calls when confirmed has no baseurl
BOT_TYPE = '3'                                     # login-qr.ts:36 DEFAULT_ILINK_BOT_TYPE
CHANNEL_VERSION = '2.4.10'  # wire protocol aligned with openclaw-weixin 2.4.10 (package.json version): it feeds
#                             iLink-App-ClientVersion and base_info.channel_version; whether the service gates on
#                             it is a real-device question (contracts §8)
ILINK_APP_ID = 'bot'                               # package.json ilink_appid
BOT_AGENT = 'fleetmux/' + FLEETMUX_VERSION        # base_info.bot_agent: honestly fleetmux, never "OpenClaw"
QR_POLL_TIMEOUT_S = 35                             # login-qr.ts:33 QR_LONG_POLL_TIMEOUT_MS
QR_POLL_PAUSE_S = 1                                # login-qr.ts:486
LOGIN_LIMIT_S = 480                                # login-qr.ts:311 / channel.ts:431
MAX_QR_REFRESH = 3                                 # login-qr.ts:253 MAX_QR_REFRESH_COUNT
POST_TIMEOUT_S = 15

# LOGIN takes no clock argument: tests patch these two module attributes.
_now = time.monotonic
_sleep = time.sleep


def client_version(version):
    """iLink-App-ClientVersion: ``(major & 0xff) << 16 | (minor & 0xff) << 8 | (patch & 0xff)`` as a decimal
    string (api.ts:99-104 buildClientVersion: a missing part counts 0, a part that is not a number counts 0)."""
    parts = []
    for piece in (str(version).split('.') + ['0', '0', '0'])[:3]:
        m = re.match(r'\s*[+-]?\d+', piece)
        parts.append(int(m.group(0)) if m else 0)
    major, minor, patch = parts
    return str(((major & 0xff) << 16) | ((minor & 0xff) << 8) | (patch & 0xff))


def _uin():
    """X-WECHAT-UIN: a random uint32 as a decimal string, base64 encoded, new for every request (api.ts:222-225)."""
    return base64.b64encode(str(int.from_bytes(os.urandom(4), 'big')).encode('ascii')).decode('ascii')


def post_headers(token=None):
    """POST headers (api.ts:240-254): five fixed ones, plus ``Authorization: Bearer`` when there is a token."""
    headers = {'Content-Type': 'application/json', 'AuthorizationType': 'ilink_bot_token', 'X-WECHAT-UIN': _uin(),
               'iLink-App-Id': ILINK_APP_ID, 'iLink-App-ClientVersion': client_version(CHANNEL_VERSION)}
    if token and str(token).strip():
        headers['Authorization'] = 'Bearer ' + str(token).strip()
    return headers


def get_headers():
    """GET headers (api.ts:312-320, only get_qrcode_status): the two iLink-* ones, nothing else."""
    return {'iLink-App-Id': ILINK_APP_ID, 'iLink-App-ClientVersion': client_version(CHANNEL_VERSION)}


def base_info():
    """``base_info`` carried in every business POST body (api.ts:203-208)."""
    return {'channel_version': CHANNEL_VERSION, 'bot_agent': BOT_AGENT}


def _url(base_url, endpoint):
    return str(base_url).rstrip('/') + '/' + endpoint.lstrip('/')


def _json(status, body):
    """The JSON object of a 2xx answer, else None."""
    if not isinstance(status, int) or not 200 <= status < 300:
        return None
    try:
        data = json.loads(body.decode('utf-8') if isinstance(body, (bytes, bytearray)) else body)
    except (ValueError, UnicodeDecodeError, AttributeError):
        return None
    return data if isinstance(data, dict) else None


def _post(transport, base_url, endpoint, payload, *, token=None, timeout=POST_TIMEOUT_S):
    """(status, body bytes) of one POST. ``payload`` is sent as given (business calls add base_info themselves)."""
    body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    return transport('POST', _url(base_url, endpoint), headers=post_headers(token), body=body, timeout=timeout)


def _is_timeout(exc):
    """A client-side timeout (socket timeout, or urllib's URLError wrapping one)."""
    return isinstance(exc, TimeoutError) or isinstance(getattr(exc, 'reason', None), TimeoutError)


# ------------------------------------------------------------------ P4a: QR login
def _local_tokens(account_name):
    """``local_token_list`` for get_bot_qrcode: the BOT_TOKEN of THIS account's own file only, [] without one.
    Supervisor-approved deviation from the source (login-qr.ts:81-102 sends every bot token stored on the machine):
    other accounts' tokens never leave the machine. Whether only this account's token still lets the service
    answer binded_redirect is a real-device question."""
    try:
        token = channels.load_account('weixin', account_name).get('BOT_TOKEN')
    except (OSError, ValueError):
        return []
    return [token] if token else []


def _fetch_qr(transport, account_name, io):
    """POST get_bot_qrcode on the fixed host (never the redirect host), print the link. -> qrcode."""
    try:
        status, body = _post(transport, FIXED_HOST, 'ilink/bot/get_bot_qrcode?bot_type=' + BOT_TYPE,
                             {'local_token_list': _local_tokens(account_name)})
    except Exception as exc:
        raise channels.ChannelError('network', 'get_bot_qrcode failed: ' + type(exc).__name__) from None
    data = _json(status, body)
    if not data or not data.get('qrcode'):
        raise channels.ChannelError('transient', 'get_bot_qrcode gave no QR code (HTTP ' + str(status) + ')')
    link = data.get('qrcode_img_content') or ''
    if link:
        io.print('用微信扫描这个链接里的二维码，连接 ClawBot：' + str(link))
    else:
        io.print('服务端没有给二维码链接，请稍后重试。')
    return str(data['qrcode'])


def _poll_status(transport, poll_base, qrcode, verify_code):
    """GET get_qrcode_status. A timeout, a gateway error (524), a network error or a bad answer is ``wait``
    (login-qr.ts:147-156)."""
    endpoint = 'ilink/bot/get_qrcode_status?qrcode=' + urllib.parse.quote(qrcode, safe='')
    if verify_code:
        endpoint += '&verify_code=' + urllib.parse.quote(verify_code, safe='')
    try:
        status, body = transport('GET', _url(poll_base, endpoint), headers=get_headers(), body=None,
                                 timeout=QR_POLL_TIMEOUT_S)
    except Exception:
        return {'status': 'wait'}
    return _json(status, body) or {'status': 'wait'}


def LOGIN(account_name, io, transport):
    """Interactive QR login for `cmux-fleet channel login weixin <account_name>` (brief v2 §2.1-2, P4a).

    ``io``: ``print(text)`` / ``readline()``; ``transport``: base.http_transport's signature (None = the real one).
    Returns the account values to write: exactly BOT_TOKEN, BASE_URL, BOT_ID, USER_ID, ALLOW (= the scanning user);
    ``{}`` when the service says this client is already bound (binded_redirect: success, nothing new, the file is
    left alone). Never writes a file itself. Failures raise a redacted ``channels.ChannelError``: kind ``auth`` when
    the login itself was refused (QR expired / blocked more than 3 times, confirmed without ilink_bot_id), kind
    ``transient`` when the 480 s limit runs out. The QR code is printed as a link only (no drawn code, no image).

    Two hosts (login-qr.ts:435-474): ``scaned_but_redirect.redirect_host`` only moves the status polling;
    ``confirmed.baseurl`` (else DEFAULT_BASE_URL) is the business base written as BASE_URL."""
    transport = transport or base.http_transport
    deadline = _now() + LOGIN_LIMIT_S
    qrcode = _fetch_qr(transport, account_name, io)
    poll_base, verify_code, refreshes, scanned_said = FIXED_HOST, '', 0, False
    while _now() < deadline:
        answer = _poll_status(transport, poll_base, qrcode, verify_code)
        state = answer.get('status')
        if state == 'confirmed':
            token, bot_id, user_id = (str(answer.get(k) or '').strip()
                                      for k in ('bot_token', 'ilink_bot_id', 'ilink_user_id'))
            if not bot_id:
                raise channels.ChannelError('auth', 'login confirmed without ilink_bot_id; run channel login again')
            if not token or not user_id:
                raise channels.ChannelError('auth', 'login confirmed without bot_token / ilink_user_id')
            base_url = str(answer.get('baseurl') or '').strip() or DEFAULT_BASE_URL
            io.print('已连接 ClawBot。')
            return {'BOT_TOKEN': token, 'BASE_URL': base_url, 'BOT_ID': bot_id, 'USER_ID': user_id, 'ALLOW': user_id}
        if state == 'binded_redirect':
            io.print('这个 ClawBot 已经绑定在本机，凭证不变。')
            return {}
        if state == 'need_verifycode':
            io.print('输入手机微信上显示的数字，以继续连接：' if not verify_code
                     else '数字不匹配，请重新输入手机微信上显示的数字：')
            verify_code = str(io.readline() or '').strip()
            continue  # the next poll goes out at once with the code (login-qr.ts:357-358)
        if state in ('expired', 'verify_code_blocked'):
            verify_code = ''
            refreshes += 1
            if refreshes > MAX_QR_REFRESH:
                raise channels.ChannelError('auth', 'QR code expired or blocked more than ' + str(MAX_QR_REFRESH)
                                            + ' times; run channel login again later')
            io.print('二维码已失效，正在刷新（' + str(refreshes) + '/' + str(MAX_QR_REFRESH) + '）。')
            qrcode, scanned_said = _fetch_qr(transport, account_name, io), False
        elif state == 'scaned':
            verify_code = ''
            if not scanned_said:
                io.print('已扫码，请在手机上确认。')
                scanned_said = True
        elif state == 'scaned_but_redirect':
            host = str(answer.get('redirect_host') or '').strip()
            if host:
                poll_base = host if host.startswith('https://') else 'https://' + host
        _sleep(QR_POLL_PAUSE_S)
    raise channels.ChannelError('transient', 'login timeout: no confirmation within ' + str(LOGIN_LIMIT_S) + ' s')


# ------------------------------------------------------------------ P4b: receive / send
LONG_POLL_TIMEOUT_S = 35            # api.ts:211 / monitor.ts:13 DEFAULT_LONG_POLL_TIMEOUT_MS
API_TIMEOUT_S = 15                  # api.ts:213 DEFAULT_API_TIMEOUT_MS (sendmessage)
CONFIG_TIMEOUT_S = 10               # api.ts:215 DEFAULT_CONFIG_TIMEOUT_MS (notifystart / notifystop)
SESSION_EXPIRED = -14               # session-guard.ts:6 STALE_TOKEN_ERRCODE
SESSION_PAUSE_S = 3600              # session-guard.ts:3: pause every call of the account for an hour
MAX_CONSECUTIVE_FAILURES = 3        # monitor.ts: three failures in a row -> 30 s, else 2 s
BACKOFF_S = 30
RETRY_S = 2
CURSOR_KEY = 'get_updates_buf'      # store key of the getupdates cursor
CTX_PREFIX = 'ctx:'                 # store key prefix of the newest context_token per user
MSG_USER, MSG_BOT = 1, 2            # types.ts MessageType
STATE_FINISH = 2                    # types.ts MessageState.FINISH
ITEM_TEXT, ITEM_IMAGE, ITEM_VOICE, ITEM_FILE, ITEM_VIDEO = 1, 2, 3, 4, 5
MEDIA_LABEL = {ITEM_IMAGE: '[图片]', ITEM_VOICE: '[语音]', ITEM_FILE: '[文件]', ITEM_VIDEO: '[视频]'}
MEDIA_NAME = {ITEM_IMAGE: 'image', ITEM_VOICE: 'voice', ITEM_FILE: 'file', ITEM_VIDEO: 'video'}
REFUSED = '微信第一期只收文字'      # brief v2 §2.1-12: phase one is text only
SECRET_KEYS = ('BOT_TOKEN',)        # the account's one credential (the other keys are ids / the base URL)
# Weixin secrets channels.redact does not recognise (M145): a JSON-quoted secret-named key ("context_token": "…",
# "typing_ticket": "…", "bot_token": "…"), and the QR / login keys of the login URLs (qrcode=, verify_code=,
# qrcode_img_content=), and a key-like URL parameter (?key= / &ticket= / &sign= / &auth=). Group 1 is kept, the
# value becomes ***.
_WX_SECRET_PATTERNS = (
    re.compile(r'(?i)("[A-Za-z0-9_.-]*(?:token|secret|password|ticket)[A-Za-z0-9_.-]*"\s*:\s*")[^"]*'),
    re.compile(r'(?i)(\b(?:qrcode_img_content|qrcode|verify_code)\s*[=:]\s*["\']?)[^\s"\'&,;#]+'),
    re.compile(r'(?i)([?&](?:key|ticket|sign|signature|auth)=)[^\s"\'&#]+'),
)


def _ok_answer(status, body, what):
    """The JSON object of a business answer; raise a redacted-safe ChannelError for HTTP / ret / errcode errors
    (no body text in the error: a body may echo a context_token)."""
    if not isinstance(status, int) or not 200 <= status < 300:
        raise channels.ChannelError('transient', what + ' HTTP ' + str(status))
    data = _json(status, body)
    if data is None:
        raise channels.ChannelError('transient', what + ' gave no JSON object (HTTP ' + str(status) + ')')
    ret, errcode = data.get('ret'), data.get('errcode')
    if SESSION_EXPIRED in (ret, errcode):
        raise channels.ChannelError('auth', what + ' errcode ' + str(SESSION_EXPIRED)
                                    + ': session expired; run `cmux-fleet channel login weixin <account>`')
    if (ret is not None and ret != 0) or (errcode is not None and errcode != 0):
        raise channels.ChannelError('transient', what + ' ret=' + str(ret) + ' errcode=' + str(errcode)
                                    + ' errmsg=' + str(data.get('errmsg') or '')[:200])
    return data


class WeixinChannel(base.Channel):
    """One ClawBot account (contracts §8 微信专项). Account keys: BOT_TOKEN, BASE_URL, BOT_ID, USER_ID, ALLOW.

    ``transport``: base.http_transport's signature. ``_poll`` is one ``getupdates`` long poll (the server sets its
    length; the ``timeout`` argument is not used). The cursor ``get_updates_buf`` sits in the store and moves only
    when the whole batch is final (base receipt protocol). Each inbound ``context_token`` is kept in the store under
    ``ctx:<user>``; ``_send_text`` carries the reply_ref's one, else the newest stored one."""

    type = 'weixin'
    TEXT_LIMIT = 4000
    TEXT_UNITS = 'chars'
    OVERFLOW = 'split'
    RECEIVES = True
    SUPPORTS_CARDS = False
    SUPPORTS_BUTTONS = False
    SUPPORTS_UPDATE = False
    SUPPORTS_FILES = False      # phase one: send_file degrades to the base's 「文件：<path>」 text
    PENDING = ()
    OPTIONAL_DEPS = ()

    def __init__(self, account, **kw):
        super().__init__(account, **kw)
        self._next_timeout = LONG_POLL_TIMEOUT_S
        self._failures = 0
        self._not_before = 0.0       # back-off: no getupdates before this clock value
        self._paused_until = 0.0     # errcode -14: no call at all before this clock value
        self._batch_last = None      # the last raw item of the fetched batch, and the cursor it commits
        self._batch_cursor = ''
        self._ctx_known = set()      # context tokens this instance has seen (masked in errors and audits)

    # ---------------------------------------------------------------- helpers
    def _base_url(self):
        return str(self.account.values.get('BASE_URL') or '').strip() or DEFAULT_BASE_URL

    def _call(self, endpoint, payload, timeout):
        """POST one business call with base_info and the bearer token -> (status, body)."""
        body = dict(payload)
        body['base_info'] = base_info()
        return _post(self.transport or base.http_transport, self._base_url(), endpoint, body,
                     token=self.account.values.get('BOT_TOKEN'), timeout=timeout)

    def _paused(self):
        return self.clock() < self._paused_until

    def _mask_ctx(self, value):
        if isinstance(value, dict):
            return {k: self._mask_ctx(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(self._mask_ctx(v) for v in value)
        if not isinstance(value, str):
            return value
        for token in sorted(self._ctx_known, key=len, reverse=True):
            if len(token) >= 6:
                value = value.replace(token, '***')
        for pattern in _WX_SECRET_PATTERNS:  # tokens this instance never saw, QR / login keys
            value = pattern.sub(lambda m: m.group(1) + '***', value)
        return value

    def redact(self, text):
        """The base redaction plus every context_token this instance has seen."""
        return self._mask_ctx(super().redact(text))

    def audit(self, event, **fields):
        """Audit fields masked for the credential (BOT_TOKEN), recognisable secrets and every seen context_token.
        Ids (USER_ID / ALLOW / BOT_ID, BASE_URL) stay readable here, so a drop names who and where; error strings
        are already fully redacted by ``redact`` (every account value) before they get here."""
        if self._audit is None:
            return
        secrets = {k: v for k, v in self.account.values.items() if k in SECRET_KEYS}
        try:
            self._audit(event, channel=self.type, account=self.account.name,
                        **channels.redact(self._mask_ctx(fields), secrets))
        except Exception:  # an audit sink failure must not break a send or a poll
            pass

    def _remember_ctx(self, token):
        if isinstance(token, str) and token:
            self._ctx_known.add(token)

    # ---------------------------------------------------------------- receive
    def _poll(self, timeout):
        now = self.clock()
        if now < self._paused_until or now < self._not_before:
            return []
        cursor = self.store.get(CURSOR_KEY) or ''
        try:
            try:
                status, body = self._call('ilink/bot/getupdates', {'get_updates_buf': cursor}, self._next_timeout)
            except Exception as exc:
                if _is_timeout(exc):  # api.ts:441-483: a client timeout is an empty batch, cursor unchanged
                    return []
                raise channels.ChannelError('network', 'getupdates: ' + type(exc).__name__ + ': ' + str(exc)) \
                    from None
            data = _ok_answer(status, body, 'getupdates')
        except channels.ChannelError as exc:
            if exc.kind == 'auth':  # -14: pause every call for an hour, no failure count (monitor.ts)
                self._paused_until = self.clock() + SESSION_PAUSE_S
                self._failures = 0
            else:
                self._failures += 1
                if self._failures >= MAX_CONSECUTIVE_FAILURES:
                    self._failures = 0
                    self._not_before = self.clock() + BACKOFF_S
                else:
                    self._not_before = self.clock() + RETRY_S
            raise
        self._failures = 0
        if getattr(self, '_state', '') in ('needs_login', 'reconnecting', 'retrying'):
            self._state = ''
        wait_ms = data.get('longpolling_timeout_ms')
        if isinstance(wait_ms, (int, float)) and not isinstance(wait_ms, bool) and wait_ms > 0:
            self._next_timeout = wait_ms / 1000
        new_cursor = data.get('get_updates_buf')
        new_cursor = new_cursor if isinstance(new_cursor, str) else ''
        msgs = [m for m in (data.get('msgs') or []) if m is not None] if isinstance(data.get('msgs'), list) else []
        if not msgs:
            if new_cursor:
                self.store.set(CURSOR_KEY, new_cursor)
            self._batch_last = None
            return []
        self._batch_last, self._batch_cursor = msgs[-1], new_cursor
        return msgs

    def _ack(self, raw):
        """Commit the batch's cursor once its last item is final (an empty cursor never overwrites)."""
        if self._batch_last is not None and raw is self._batch_last:
            self._batch_last = None
            if self._batch_cursor:
                self.store.set(CURSOR_KEY, self._batch_cursor)

    @staticmethod
    def _message_id(raw):
        """Top-level message_id, else the first item_list[].msg_id (inbound.ts:202), else seq, else a stable hash."""
        mid = raw.get('message_id')
        if mid not in (None, '', 0):
            return str(mid)
        for item in raw.get('item_list') or []:
            if isinstance(item, dict) and item.get('msg_id') not in (None, '', 0):
                return str(item['msg_id'])
        if raw.get('seq') not in (None, ''):
            return 'seq:' + str(raw['seq'])
        canon = json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str)
        return 'h:' + hashlib.sha256(canon.encode('utf-8')).hexdigest()[:16]

    def parse_inbound(self, raw):
        if not isinstance(raw, dict):
            raise ValueError('weixin message must be an object')
        if raw.get('message_type') != MSG_USER:
            return []  # the bot's own messages (and anything else): no envelope, not audited
        sender = str(raw.get('from_user_id') or '').strip()
        mid = self._message_id(raw)
        group = str(raw.get('group_id') or '').strip()
        if group:  # private chats only; logged with who and where, never the text
            self.audit('channel.drop_policy', reason='group', sender=sender, conversation=group,
                       message_id=channels.prefix(self.type) + ':' + mid, private=False, verified=True)
            return []
        if not sender:
            raise ValueError('weixin message without from_user_id')
        texts, labels, rejected = [], [], []
        for item in raw.get('item_list') or []:
            if not isinstance(item, dict):
                continue
            kind = item.get('type')
            if kind == ITEM_TEXT:
                text = (item.get('text_item') or {}).get('text')
                if isinstance(text, str) and text:
                    texts.append(text)
                continue
            if kind == ITEM_VOICE:
                transcript = (item.get('voice_item') or {}).get('text')
                if isinstance(transcript, str) and transcript.strip():
                    texts.append(transcript)
                    continue
            if kind in MEDIA_LABEL:
                name = ''
                if kind == ITEM_FILE:
                    name = str((item.get('file_item') or {}).get('file_name') or '')
                rejected.append({'name': name or MEDIA_NAME[kind], 'reason': REFUSED})
                labels.append(MEDIA_LABEL[kind])
        text = '\n'.join(texts) if texts else ' '.join(labels)
        ctx = raw.get('context_token')
        reply_ref = {'to_user_id': sender}
        if isinstance(ctx, str) and ctx:
            reply_ref['context_token'] = ctx
            self._remember_ctx(ctx)
            try:
                self.store.set(CTX_PREFIX + sender, ctx)
            except Exception as exc:  # the store is down: keep the item for the next poll
                raise channels.Retryable('context_token not stored: ' + type(exc).__name__) from None
        ms = raw.get('create_time_ms')
        at = int(ms) // 1000 if isinstance(ms, (int, float)) and not isinstance(ms, bool) and ms > 0 else None
        return [envelope.make(self.type, self.account.name, conversation=sender, message_id=mid, sender=sender,
                              text=text, private=True, verified=True, at=at, reply_ref=reply_ref,
                              rejected_files=rejected or None)]

    # ---------------------------------------------------------------- send
    def _send_text(self, conversation, text, reply_ref):
        if self._paused():
            raise channels.ChannelError('auth', 'session paused after errcode -14; run `cmux-fleet channel login '
                                                'weixin <account>`')
        ctx = (reply_ref or {}).get('context_token') if isinstance(reply_ref, dict) else None
        if not ctx:
            ctx = self.store.get(CTX_PREFIX + conversation)
        client_id = 'fleetmux-weixin:' + str(int(self.clock() * 1000)) + '-' + os.urandom(4).hex()
        msg = {'from_user_id': '', 'to_user_id': conversation, 'client_id': client_id, 'message_type': MSG_BOT,
               'message_state': STATE_FINISH, 'item_list': [{'type': ITEM_TEXT, 'text_item': {'text': text}}]}
        if ctx:
            msg['context_token'] = str(ctx)
            self._remember_ctx(str(ctx))
        status, body = self._call('ilink/bot/sendmessage', {'msg': msg}, API_TIMEOUT_S)
        data = _ok_answer(status, body, 'sendmessage')
        server_id = data.get('message_id')
        return str(server_id) if server_id not in (None, '') else client_id

    # ---------------------------------------------------------------- lifecycle
    def _notify(self, endpoint):
        """notifystart / notifystop: a failure is only audited, never raised (receiving goes on)."""
        what = endpoint.rsplit('/', 1)[-1]
        try:
            status, body = self._call(endpoint, {}, CONFIG_TIMEOUT_S)
            _ok_answer(status, body, what)
        except Exception as exc:
            self.audit('channel.error', error=self.redact(what + ': ' + type(exc).__name__ + ': ' + str(exc))[:500],
                       kind=getattr(exc, 'kind', ''))

    def start(self):
        self._notify('ilink/bot/msg/notifystart')

    def close(self):
        self._notify('ilink/bot/msg/notifystop')


ADAPTER = WeixinChannel
