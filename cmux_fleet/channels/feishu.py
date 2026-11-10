"""Feishu channel adapter (package P3, design §53, contracts §8).

Send (P3a): plain urllib through the injected ``transport`` (this module's ``http_transport`` by default: like
``base.http_transport`` it honours the proxy environment, and it also returns the response headers; an injected
transport may answer ``(status, body)`` or ``(status, body, headers)``): a tenant access token (``/auth/v3/tenant_access_token/internal``,
refreshed by its ``expire`` and once on 99991663 / 99991661), text / interactive card / PATCH update / file upload
then send, reply to a message when ``reply_ref`` carries its ``message_id``, else a new message to the chat.

Receive (P3b): a long-lived WebSocket client (lark-oapi 1.5.3 ``lark.ws.Client``, or the injected ``ws_factory``) runs
in a daemon thread started by ``start()``. Its callbacks only queue the schema-2.0 event dict (no I/O, no policy);
a card press answers the platform at once with the fixed toast 「已收到」. ``_poll`` drains the queue and
``parse_inbound`` turns each event into an envelope: ``im.message.receive_v1`` (text / post / image / file; image and
file bytes come from ``GET /im/v1/messages/<id>/resources/<key>``) and ``card.action.trigger`` (the operator who
pressed is the sender, the button value's ``command`` is the text). Health: heartbeat (pong) older than 180 s or
activity older than 1800 s -> ``reconnecting``; a ``ClientException`` (credential rejected) -> ``needs_login`` once,
no reconnect loop; no SDK -> ``missing_dependency``. Each state / timestamp change goes to the injected ``status``
writer (P1d wires it to the Redis status hash).

The module imports only the standard library and fleetmux: lark-oapi (the optional SDK, ``OPTIONAL_DEPS``) is
imported by ``start()`` / ``build_dispatcher()`` only, so a missing SDK never stops the sends. The WS client goes
direct (no proxy) by patching only that SDK module's ``requests`` / ``websockets`` (``_scope_direct``); the process
environment is never changed, since another channel of the same process may need the proxy.

Account file keys: ``APP_ID``, ``APP_SECRET``; ``ALLOW`` = open_ids (``ou_…``) of the whitelisted users. An open_id is
per app: the same person has another open_id under every Feishu app, so a new fleetmux app needs its own ids.
"""
import hashlib
import importlib
import json
import queue
import re
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from cmux_fleet import channels
from cmux_fleet.channels import base, envelope, provenance

API_BASE = 'https://open.feishu.cn/open-apis'
TOKEN_PATH = '/auth/v3/tenant_access_token/internal'
TOKEN_INVALID_CODES = (99991663, 99991661)  # tenant token expired / invalid: fetch a new one, retry once
TOKEN_MARGIN_S = 300                        # refresh this long before the token's own expiry
HTTP_TIMEOUT_S = 30
MAX_UPLOAD_BYTES = 30 * 1024 * 1024         # Feishu im/v1/files limit
LARK_PIP = 'lark-oapi'
LARK_PIN = 'lark-oapi==1.5.3'
HEARTBEAT_STALE_S = 180                     # no pong for this long -> reconnecting
ACTIVITY_STALE_S = 1800                     # no frame at all for this long -> reconnecting
TOAST = {'toast': {'type': 'info', 'content': '已收到'}}  # the fixed answer to a card press (within 3 s)
P2P_KEY = 'p2p:'                            # store key: chat id -> open_id that wrote to us there (or 'p2p')
MENTION = re.compile(r'@_user_\d+|@_all')
SUPPORTED_TYPES = ('text', 'post', 'image', 'file')


def http_transport(method, url, *, headers=None, body=None, timeout=30):
    """The Feishu default transport: ``base.http_transport`` (stdlib urllib, proxy environment honoured, HTTP error
    statuses returned) that also returns the response headers, so a download is told from an API answer by
    Content-Type. Returns (status, body bytes, headers dict)."""
    req = urllib.request.Request(url, data=body, headers=dict(headers or {}), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers.items()) if exc.headers else {}


def _is_client_exception(exc):
    """lark-oapi's ClientException (the platform refused the app credential): matched by class name so the SDK need
    not be importable here."""
    return any(c.__name__ == 'ClientException' for c in type(exc).__mro__)


class _DirectRequests:
    """Stands in for the ``requests`` module inside ``lark_oapi.ws.client``: each call goes through a Session with
    ``trust_env=False`` (no proxy from the environment); every other attribute is the real module's."""

    def __init__(self, orig):
        self._orig = orig

    def _session(self):
        s = self._orig.Session()
        s.trust_env = False
        return s

    def get(self, url, **kw):
        return self._session().get(url, **kw)

    def post(self, url, **kw):
        return self._session().post(url, **kw)

    def __getattr__(self, name):
        return getattr(self._orig, name)


class _DirectWebsockets:
    """Stands in for the ``websockets`` module inside ``lark_oapi.ws.client``: ``connect`` always passes
    ``proxy=None`` (websockets otherwise reads the proxy environment); every other attribute is the real module's."""

    def __init__(self, orig):
        self._orig = orig

    def connect(self, url, **kw):
        kw['proxy'] = None
        return self._orig.connect(url, **kw)

    def __getattr__(self, name):
        return getattr(self._orig, name)


def _scope_direct(ws_mod):
    """Make the SDK's WS client connect direct, scoped to that module (``ws_mod`` = ``lark_oapi.ws.client``): replace
    its ``requests`` (endpoint discovery) and ``websockets`` (the connection). Idempotent; the process environment
    is not touched."""
    if not isinstance(ws_mod.requests, _DirectRequests):
        ws_mod.requests = _DirectRequests(ws_mod.requests)
    if not isinstance(ws_mod.websockets, _DirectWebsockets):
        ws_mod.websockets = _DirectWebsockets(ws_mod.websockets)
    return ws_mod


def build_dispatcher(on_event, on_card):
    """lark-oapi event dispatcher that hands ``im.message.receive_v1`` / ``card.action.trigger`` to ``on_event`` /
    ``on_card`` as plain schema-2.0 dicts; the card handler returns ``on_card``'s toast as the SDK response."""
    lark = importlib.import_module('lark_oapi')
    trigger = importlib.import_module('lark_oapi.event.callback.model.p2_card_action_trigger')

    def message(data):
        on_event(json.loads(lark.JSON.marshal(data)))

    def card(data):
        return trigger.P2CardActionTriggerResponse(on_card(json.loads(lark.JSON.marshal(data))))
    return (lark.EventDispatcherHandler.builder('', '')
            .register_p2_im_message_receive_v1(message)
            .register_p2_card_action_trigger(card)
            .build())


def _lark_ws_factory(lark, ws_mod):
    """The default ``ws_factory``: a lark-oapi WS client (auto reconnect) whose frames are reported to ``on_frame``:
    a pong is a heartbeat, any frame (pong included) is activity."""
    def factory(*, app_id, app_secret, on_event, on_card, on_frame):
        client = lark.ws.Client(app_id, app_secret, event_handler=build_dispatcher(on_event, on_card),
                                log_level=lark.LogLevel.WARNING, auto_reconnect=True)
        handle = client._handle_message

        async def handle_message(msg):
            try:
                frame = ws_mod.Frame()
                frame.ParseFromString(msg)
                if (ws_mod.FrameType(frame.method) == ws_mod.FrameType.CONTROL
                        and ws_mod._get_by_key(frame.headers, ws_mod.HEADER_TYPE) == ws_mod.MessageType.PONG.value):
                    on_frame('pong')
            except Exception:
                pass
            on_frame('data')
            return await handle(msg)
        client._handle_message = handle_message
        return client
    return factory


class FeishuError(RuntimeError):
    """A Feishu API answer that is not a success (HTTP error status or ``code != 0``); never carries a secret."""

    def __init__(self, what, status=None, code=None, msg=''):
        self.status, self.code = status, code
        detail = what + (' HTTP ' + str(status) if status is not None else '')
        detail += (' code ' + str(code) if code is not None else '') + (': ' + str(msg)[:200] if msg else '')
        super().__init__(detail)


def _json_bytes(obj):
    return json.dumps(obj, ensure_ascii=False).encode('utf-8')


class FeishuChannel(base.Channel):
    type = 'feishu'
    TEXT_LIMIT = 4000
    SUPPORTS_CARDS = True
    SUPPORTS_BUTTONS = True
    SUPPORTS_UPDATE = True
    SUPPORTS_FILES = True
    OPTIONAL_DEPS = ('lark_oapi',)

    def __init__(self, account, *, ws_factory=None, status=None, **kw):
        """``ws_factory`` / ``status``: Feishu-only keywords (receive side, P3b); the base constructor is unchanged."""
        super().__init__(account, **kw)
        self.ws_factory = ws_factory
        self.status = status
        self._token_value = ''
        self._token_until = 0.0
        self._events = queue.Queue()  # filled by the WS thread's callbacks
        self._pending = []            # events handed to poll and not acknowledged yet (come back on the next poll)
        self._lock = threading.RLock()
        self._ws_state = ''           # '' until start(); then ok / reconnecting / needs_login / missing_dependency
        self._ws_error = ''
        self._heartbeat_at = 0
        self._activity_at = 0
        self._thread = None
        self._closed = False

    # ---------------------------------------------------------------- HTTP
    def _transport(self):
        return self.transport if self.transport is not None else http_transport

    def redact(self, text):
        """Account values (base) and the current tenant token: neither ever reaches an error, audit or status."""
        text = super().redact(text)
        if self._token_value and len(self._token_value) >= 6:
            text = text.replace(self._token_value, '***')
        return text

    def _raw(self, method, path, *, query='', body=None, headers=None):
        """(status, body, response headers or None). The transport may answer (status, body) or
        (status, body, headers); None means it gave no headers."""
        url = API_BASE + path + ('?' + query if query else '')
        got = self._transport()(method, url, headers=headers or {}, body=body, timeout=HTTP_TIMEOUT_S)
        if len(got) >= 3:
            return got[0], got[1], dict(got[2] or {})
        return got[0], got[1], None

    @staticmethod
    def _is_json_answer(status, resp_headers):
        """Whether a binary download answered an API JSON object instead of the file: by Content-Type when the
        transport gave headers, else by the HTTP status. Never by the first byte (a .json file starts with '{')."""
        if resp_headers is not None:
            ctype = next((str(v) for k, v in resp_headers.items() if str(k).lower() == 'content-type'), '')
            return status >= 400 or ctype.split(';')[0].strip().lower() == 'application/json'
        return status >= 400

    @staticmethod
    def _decode(what, status, data):
        """JSON answer -> dict; HTTP error status or ``code != 0`` -> raise (auth / rate limit as ChannelError)."""
        try:
            answer = json.loads(data.decode('utf-8') if isinstance(data, bytes) else data)
        except (ValueError, AttributeError, UnicodeDecodeError):
            answer = None
        code = answer.get('code') if isinstance(answer, dict) else None
        msg = answer.get('msg', '') if isinstance(answer, dict) else ''
        if status in (401, 403):
            raise channels.ChannelError('auth', str(FeishuError(what, status, code, msg)))
        if status == 429:
            raise channels.ChannelError('rate_limited', str(FeishuError(what, status, code, msg)))
        if status >= 400 or not isinstance(answer, dict):
            raise FeishuError(what, status, code, msg)
        if code not in (0, None):
            raise FeishuError(what, None, code, msg)
        return answer

    def _token(self, force=False):
        now = self.clock()
        if self._token_value and not force and now < self._token_until:
            return self._token_value
        values = self.account.values
        status, data, _ = self._raw('POST', TOKEN_PATH, body=_json_bytes(
            {'app_id': values.get('APP_ID', ''), 'app_secret': values.get('APP_SECRET', '')}),
            headers={'Content-Type': 'application/json; charset=utf-8'})
        try:
            answer = self._decode('tenant token', status, data)
        except FeishuError as exc:
            if exc.code is None:  # HTTP error / garbage: not a verdict on the credentials
                raise
            raise channels.ChannelError('auth', str(exc)) from None  # the platform refused APP_ID / APP_SECRET
        token = answer.get('tenant_access_token')
        if not isinstance(token, str) or not token:
            raise channels.ChannelError('auth', 'tenant token: no tenant_access_token in the answer')
        expire = int(answer.get('expire') or 0)
        self._token_value = token
        self._token_until = now + max(expire - min(TOKEN_MARGIN_S, expire // 2), 0)
        return token

    def _api(self, method, path, *, query='', payload=None, body=None, content_type=None, binary=False):
        """One authorised call; a 99991663 / 99991661 answer refreshes the token once and retries once."""
        for attempt in (0, 1):
            headers = {'Authorization': 'Bearer ' + self._token(force=attempt == 1)}
            data = body
            if payload is not None:
                data = _json_bytes(payload)
                headers['Content-Type'] = 'application/json; charset=utf-8'
            elif content_type:
                headers['Content-Type'] = content_type
            status, answer, resp_headers = self._raw(method, path, query=query, body=data, headers=headers)
            if binary and not self._is_json_answer(status, resp_headers):
                return answer
            try:
                return self._decode(method + ' ' + path.split('?')[0], status, answer)
            except FeishuError as exc:
                if attempt == 0 and exc.code in TOKEN_INVALID_CODES:
                    continue
                if exc.code in TOKEN_INVALID_CODES:
                    raise channels.ChannelError('auth', str(exc)) from None
                raise
        raise FeishuError(method + ' ' + path)  # not reached

    # ---------------------------------------------------------------- send hooks
    def _uuid(self, kind):
        """Feishu dedupes a create / reply by ``uuid`` (1 h): the queue item key (per chunk, per message kind) makes
        retries of one item idempotent without merging a file and its caption."""
        if not self.send_key:
            return None
        return hashlib.sha256((str(self.send_key) + '|' + kind).encode()).hexdigest()[:32]

    def _post_message(self, conversation, msg_type, content, reply_ref):
        payload = {'msg_type': msg_type, 'content': json.dumps(content, ensure_ascii=False)}
        key = self._uuid(msg_type)
        if key:
            payload['uuid'] = key
        parent = reply_ref.get('message_id') if isinstance(reply_ref, dict) else None
        if isinstance(parent, str) and parent:
            answer = self._api('POST', '/im/v1/messages/' + parent + '/reply', payload=payload)
        else:
            payload['receive_id'] = conversation
            answer = self._api('POST', '/im/v1/messages', query='receive_id_type=chat_id', payload=payload)
        data = answer.get('data') or {}
        return data.get('message_id') or None

    def _send_text(self, conversation, text, reply_ref):
        return self._post_message(conversation, 'text', {'text': text}, reply_ref)

    @staticmethod
    def card_json(card):
        """The neutral card -> a Feishu interactive card; each button's ``value`` is ``{'command': …}``, which the
        ``card.action.trigger`` event hands back when it is pressed."""
        elements = []
        if card.get('text'):
            elements.append({'tag': 'div', 'text': {'tag': 'plain_text', 'content': card['text']}})
        buttons = [{'tag': 'button', 'text': {'tag': 'plain_text', 'content': b['label']},
                    'type': 'danger' if b['command'].startswith('/approve') else 'default',
                    'value': {'command': b['command']}} for b in card.get('buttons') or []]
        if buttons:
            elements.append({'tag': 'action', 'actions': buttons})
        return {'config': {'wide_screen_mode': True, 'update_multi': True},
                'header': {'title': {'tag': 'plain_text', 'content': card.get('title') or ''}, 'template': 'blue'},
                'elements': elements}

    def _send_card(self, conversation, card, reply_ref):
        return self._post_message(conversation, 'interactive', self.card_json(card), reply_ref)

    def _update_card(self, conversation, message_id, card):
        self._api('PATCH', '/im/v1/messages/' + message_id,
                  payload={'content': json.dumps(self.card_json(card), ensure_ascii=False)})
        return message_id

    def _send_file(self, conversation, path, caption, reply_ref):
        p = Path(path)
        size = p.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            return base.SendResult(ok=False, error='file over ' + str(MAX_UPLOAD_BYTES // (1024 * 1024))
                                   + ' MB: ' + p.name)
        boundary = '----fleetmux' + uuid.uuid4().hex
        parts = []
        for name, value in (('file_type', 'stream'), ('file_name', p.name)):
            parts.append(('--' + boundary + '\r\nContent-Disposition: form-data; name="' + name + '"\r\n\r\n'
                          + value + '\r\n').encode('utf-8'))
        parts.append(('--' + boundary + '\r\nContent-Disposition: form-data; name="file"; filename="'
                      + p.name.replace('"', '_') + '"\r\nContent-Type: application/octet-stream\r\n\r\n')
                     .encode('utf-8') + p.read_bytes() + b'\r\n')
        parts.append(('--' + boundary + '--\r\n').encode('utf-8'))
        answer = self._api('POST', '/im/v1/files', body=b''.join(parts),
                           content_type='multipart/form-data; boundary=' + boundary)
        file_key = (answer.get('data') or {}).get('file_key')
        if not file_key:
            raise FeishuError('upload file: no file_key in the answer')
        message_id = self._post_message(conversation, 'file', {'file_key': file_key}, reply_ref)
        if caption:
            self._post_message(conversation, 'text', {'text': caption}, None)
        return message_id

    def check(self):
        """`channel test` without sending: fetch one tenant token."""
        try:
            self._token(force=True)
        except Exception as exc:
            return self._fail('check', exc)
        return base.SendResult(ok=True)

    # ---------------------------------------------------------------- receive: WS thread, status
    def _fields(self):
        return {'state': self._ws_state, 'heartbeat_at': self._heartbeat_at, 'activity_at': self._activity_at,
                'error': self.redact(self._ws_error)[:500]}

    def _write_status(self):
        if self.status is None:
            return
        try:
            self.status(self._fields())
        except Exception:  # a status sink failure must not break receiving
            pass

    def _evaluate(self):
        """ok <-> reconnecting by the heartbeat / activity age (terminal states stay). True when it changed."""
        if self._ws_state not in ('ok', 'reconnecting'):
            return False
        now = self.clock()
        fresh = now - self._heartbeat_at <= HEARTBEAT_STALE_S and now - self._activity_at <= ACTIVITY_STALE_S
        new = 'ok' if fresh else 'reconnecting'
        changed, self._ws_state = new != self._ws_state, new
        if new == 'ok':
            self._ws_error = ''
        elif changed:
            self._ws_error = ('no heartbeat for ' + str(HEARTBEAT_STALE_S) + ' s'
                              if now - self._heartbeat_at > HEARTBEAT_STALE_S
                              else 'no activity for ' + str(ACTIVITY_STALE_S) + ' s')
        return changed

    def _on_frame(self, kind):
        """WS thread: a pong (heartbeat) or any frame (activity). Every call writes the status with the new time."""
        with self._lock:
            if self._closed or self._ws_state not in ('ok', 'reconnecting'):
                return
            now = self.clock()
            if kind == 'pong':
                self._heartbeat_at = now
            else:
                self._activity_at = now
            self._evaluate()
            self._write_status()

    def _on_event(self, event):
        """WS thread: queue the event dict; no I/O, no policy."""
        self._events.put(event)
        self._on_frame('data')

    def _on_card(self, event):
        """WS thread: queue the press and answer the platform at once with the fixed toast."""
        self._events.put(event)
        self._on_frame('data')
        return json.loads(json.dumps(TOAST))

    def _set_terminal(self, state, error):
        with self._lock:
            self._ws_state, self._ws_error = state, error
            self._write_status()

    def _ws_failed(self, exc):
        """The WS client stopped for good (lark-oapi raises only ClientException; it reconnects on anything else).
        Recorded once; nothing here starts it again."""
        if _is_client_exception(exc):
            err = channels.ChannelError('auth', 'Feishu refused the app credential (ClientException '
                                        + str(getattr(exc, 'code', '')) + ': ' + str(getattr(exc, 'msg', exc))
                                        + '); check APP_ID / APP_SECRET, then `channel login`')
            state = 'needs_login'
        else:
            err, state = exc, 'stopped'
        with self._lock:
            self._fail('ws', err)
            self._set_terminal(state, self.last_error)
        return err

    def _run(self, client):
        try:
            client.start()
        except BaseException as exc:  # noqa: BLE001 — the thread must record, never die silently
            if not self._closed:
                self._ws_failed(exc)

    def start(self):
        """Start the WS client thread. Without an injected ``ws_factory``: import lark-oapi (missing ->
        MissingDependency, status ``missing_dependency``; sends keep working), scope it direct, build its client."""
        factory = self.ws_factory
        if factory is None:
            try:
                lark = importlib.import_module('lark_oapi')
                ws_mod = importlib.import_module('lark_oapi.ws.client')
            except ImportError:
                self._set_terminal('missing_dependency', LARK_PIP + ' not installed: Feishu inbound off, sending '
                                   'still works (pip install ' + LARK_PIN + ')')
                raise channels.MissingDependency(LARK_PIP, 'pip install ' + LARK_PIN
                                                 + '; inbound off, sending still works') from None
            _scope_direct(ws_mod)
            factory = _lark_ws_factory(lark, ws_mod)
        with self._lock:
            now = self.clock()
            self._closed = False
            self._ws_state, self._ws_error = 'reconnecting', 'connecting'
            self._heartbeat_at = self._activity_at = now
            self._write_status()
        values = self.account.values
        try:
            client = factory(app_id=values.get('APP_ID', ''), app_secret=values.get('APP_SECRET', ''),
                             on_event=self._on_event, on_card=self._on_card, on_frame=self._on_frame)
        except Exception as exc:
            err = self._ws_failed(exc)
            if isinstance(err, channels.ChannelError):
                raise err from None
            raise channels.ChannelError('transient', self.last_error) from None
        self._thread = threading.Thread(target=self._run, args=(client,), name='feishu-ws-' + self.account.name,
                                        daemon=True)
        self._thread.start()

    def close(self):
        """Stop receiving. lark-oapi 1.5.3's client has no stop call: its daemon thread ends with the process."""
        try:
            self._closed = True
            if self._thread is not None:
                self._thread.join(0.2)
        except Exception:
            pass

    def health(self):
        if not self._ws_state:
            return super().health()
        with self._lock:
            return self._fields()

    def _poll(self, timeout):
        """Re-check the health thresholds, then drain the event queue (wait up to ``timeout`` s when nothing is
        pending). Events not acknowledged by the base come back on the next poll."""
        with self._lock:
            if self._evaluate():
                self._write_status()
        if not self._pending:
            try:
                self._pending.append(self._events.get(timeout=timeout) if timeout and timeout > 0
                                     else self._events.get_nowait())
            except queue.Empty:
                return []
        while True:
            try:
                self._pending.append(self._events.get_nowait())
            except queue.Empty:
                break
        return list(self._pending)

    def _ack(self, raw):
        for i, item in enumerate(self._pending):
            if item is raw:
                del self._pending[i]
                return

    # ---------------------------------------------------------------- receive: events -> envelopes
    def parse_inbound(self, raw):
        if not isinstance(raw, dict) or not isinstance(raw.get('header'), dict) or not isinstance(
                raw.get('event'), dict):
            raise ValueError('not a schema-2.0 Feishu event')
        kind = raw['header'].get('event_type')
        if kind == 'im.message.receive_v1':
            return self.envelope_from_message(raw)
        if kind == 'card.action.trigger':
            return self.action_from_card(raw)
        self.audit('channel.drop_invalid', error='unhandled Feishu event type ' + str(kind)[:80])
        return []

    @staticmethod
    def _at(raw_ms, fallback):
        try:
            return int(raw_ms) // 1000 or int(fallback)
        except (TypeError, ValueError):
            return int(fallback)

    def private_target_ok(self, conversation):
        """A p2p chat id that a message from a whitelisted open_id recorded in this account's store."""
        if not isinstance(conversation, str) or not conversation:
            return False
        try:
            who = self.store.get(P2P_KEY + conversation)
        except Exception:
            return False
        return bool(who) and who in self.account.allow

    def _chat_is_p2p(self, chat_id):
        """Only this account's store (written from a trusted p2p message event) or a clear ``chat_mode == 'p2p'``
        from the chat API make a chat private; any failure or doubt is not private. What a card event says about its
        own chat is not trusted."""
        if not isinstance(chat_id, str) or not chat_id:
            return False
        try:
            if self.store.get(P2P_KEY + chat_id):
                return True
        except Exception:
            return False
        try:
            answer = self._api('GET', '/im/v1/chats/' + chat_id)
        except Exception:
            return False
        if (answer.get('data') or {}).get('chat_mode') != 'p2p':
            return False
        try:
            self.store.set(P2P_KEY + chat_id, 'p2p')
        except Exception:
            pass
        return True

    def _download(self, message_id, key, rtype, name):
        """Fetch one resource into the inbox: ({name, path, size}, None) or (None, {name, reason})."""
        if not isinstance(key, str) or not key:
            return None, {'name': name, 'reason': 'Feishu sent no resource key'}
        try:
            data = self._api('GET', '/im/v1/messages/' + message_id + '/resources/' + key, query='type=' + rtype,
                             binary=True)
        except (FeishuError, channels.ChannelError) as exc:
            status = getattr(exc, 'status', None)
            if isinstance(exc, channels.ChannelError) or status is None or status >= 500:
                raise channels.Retryable('download ' + rtype + ': ' + self.redact(str(exc))[:200]) from None
            return None, {'name': name, 'reason': 'download refused: ' + self.redact(str(exc))[:200]}
        except Exception as exc:  # network: not received yet, try again
            raise channels.Retryable('download ' + rtype + ': ' + type(exc).__name__) from None
        if not isinstance(data, (bytes, bytearray)):
            return None, {'name': name, 'reason': 'download answered no bytes'}
        if len(data) > self.MAX_FILE_BYTES:
            return None, {'name': name, 'reason': 'over ' + str(self.MAX_FILE_BYTES) + ' bytes ('
                          + str(len(data)) + ')'}
        if self.inbox_dir is None:
            return None, {'name': name, 'reason': 'no inbox directory for inbound files'}
        prefix = re.sub(r'[^A-Za-z0-9._-]', '_', message_id)[:80] + '-'
        path, fh = self.new_inbox_file(name, prefix)
        with fh:
            fh.write(bytes(data))
        return {'name': name, 'path': str(path), 'size': len(data)}, None

    @staticmethod
    def _post_text(content):
        """Flatten a ``post`` (rich text) to plain text: title, then each line's text / link / code pieces."""
        lines = [content.get('title') or ''] if content.get('title') else []
        for line in content.get('content') or []:
            parts = []
            for el in line if isinstance(line, list) else []:
                if isinstance(el, dict) and el.get('tag') in ('text', 'a', 'code_block', 'md'):
                    parts.append(str(el.get('text') or ''))
            lines.append(''.join(parts))
        return '\n'.join(lines)

    def envelope_from_message(self, raw):
        """``im.message.receive_v1`` -> one envelope (or [] for a bot's own message / unparsable content)."""
        ev = raw['event']
        msg = ev.get('message') or {}
        sender = ev.get('sender') or {}
        if sender.get('sender_type') not in (None, 'user'):
            return []  # an app / bot message, never an operator's input
        open_id = (sender.get('sender_id') or {}).get('open_id')
        mid, chat = msg.get('message_id'), msg.get('chat_id')
        if not all(isinstance(v, str) and v for v in (open_id, mid, chat)):
            raise ValueError('message event without sender open_id / message_id / chat_id')
        private = msg.get('chat_type') == 'p2p'
        mtype = msg.get('message_type')
        try:
            content = json.loads(msg.get('content') or '')
            if not isinstance(content, dict):
                raise ValueError('content is not a JSON object')
        except (ValueError, TypeError):
            self._fail('parse_inbound', ValueError('message ' + mid + ' (' + str(mtype)[:20]
                                                   + '): content is not JSON'))
            return []
        if private:
            try:  # trusted platform event: this chat is one-to-one with open_id (button privacy, pushes)
                self.store.set(P2P_KEY + chat, open_id)
            except Exception:
                pass
        text, files, rejected = '', [], []
        if mtype == 'text':
            text = str(content.get('text') or '')
        elif mtype == 'post':
            text = self._post_text(content)
        elif mtype == 'image':
            key = content.get('image_key')
            got, bad = self._download(mid, key, 'image', str(key or 'image'))
            text = '[图片]'
            files, rejected = ([got] if got else []), ([bad] if bad else [])
        elif mtype == 'file':
            name = str(content.get('file_name') or content.get('file_key') or 'file')
            got, bad = self._download(mid, content.get('file_key'), 'file', name)
            text = '[文件] ' + name
            files, rejected = ([got] if got else []), ([bad] if bad else [])
        else:
            rejected = [{'name': str(mtype or 'message'), 'reason': '飞书 ' + str(mtype)[:40] + ' 消息暂不支持'}]
        for m in msg.get('mentions') or []:
            if isinstance(m, dict) and isinstance(m.get('key'), str) and m['key']:
                text = text.replace(m['key'], '')
        text = MENTION.sub('', text).strip()
        rejected = rejected + provenance.feishu_unfetched(msg)  # P8b scheme R: quote / merge-forward not fetched
        return [envelope.make(self.type, self.account.name, conversation=chat, message_id=mid, sender=open_id,
                              text=text, private=private, verified=True,
                              at=self._at(msg.get('create_time') or raw['header'].get('create_time'), self.clock()),
                              files=files, rejected_files=rejected,
                              reply_ref={'message_id': mid, 'chat_id': chat})]

    def action_from_card(self, raw):
        """``card.action.trigger`` -> one action envelope: the operator who pressed is the sender, the button's
        ``value.command`` is the text."""
        ev = raw['event']
        operator = (ev.get('operator') or {}).get('open_id')
        value = (ev.get('action') or {}).get('value')
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                value = None
        command = value.get('command') if isinstance(value, dict) else None
        ctx = ev.get('context') or {}
        chat, card_mid = ctx.get('open_chat_id'), ctx.get('open_message_id')
        event_id = raw['header'].get('event_id')
        if not all(isinstance(v, str) and v for v in (operator, command, chat, event_id)):
            raise ValueError('card action without operator / command / chat / event_id')
        action = {'command': command}
        if isinstance(card_mid, str) and card_mid:
            action['card_message_id'] = card_mid
        return [envelope.make(self.type, self.account.name, conversation=chat, message_id='cb:' + event_id,
                              sender=operator, text=command, private=self._chat_is_p2p(chat), verified=True,
                              at=self._at(raw['header'].get('create_time'), self.clock()), action=action)]


ADAPTER = FeishuChannel
