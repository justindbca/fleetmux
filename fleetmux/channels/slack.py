"""Slack channel (package P9: P9b send, P9c receive; brief v2.3 §9, contracts §8 "Slack 专项", design §53).

Account file keys: ``SLACK_BOT_TOKEN`` (``xoxb-``: every Web API call), ``SLACK_APP_TOKEN`` (``xapp-``: only
``apps.connections.open``), ``SLACK_ALLOW_USERS`` (the whitelist; ``parse_account_text`` puts it into
``Account.allow``, empty = reject everyone), optional ``API_BASE`` (default the public Web API).

``transport(method, url, *, headers, body, timeout)`` returns ``(status, body bytes)`` or ``(status, body bytes,
headers)`` (ruling 2); the default here is urllib returning the triple, so a 429's ``Retry-After`` header can be read.
Network errors raise. Constructor keywords beyond the base class (ruling 3): ``ws_factory=None`` (``ws_factory(url)``
-> an object with ``recv()`` / ``send(str)`` / ``close()``; default websocket-client) and ``sleep=time.sleep``
(429 waits, reconnect backoff). Tests inject fakes for all three; nothing here is imported beyond the stdlib at
module level (websocket-client is optional and only imported by ``start()``).

Send (P9b): ``chat.postMessage`` always with ``unfurl_links=False`` / ``unfurl_media=False`` (chunks of 3000 chars,
the chunks after the first threaded under the first); a user id conversation (``U…`` / ``W…``) is opened with
``conversations.open`` first (proactive push); cards are Block Kit (plain_text section + buttons, ``action_id`` =
command name, ``value`` = the rest of the command), sent as typed commands instead when a button value is over 2000
bytes or the section text over 2900 chars (never truncated); ``chat.update`` edits a card; files go through
``files.getUploadURLExternal`` -> upload -> ``files.completeUploadExternal``.

Receive (P9c): Socket Mode. ``start()`` runs one reader thread: ``apps.connections.open`` (``xapp-``) ->
``ws_factory(url)`` -> every frame with an ``envelope_id`` is acked first, then queued; ``hello`` sets health
(``num_connections > 1`` -> conflict, ruling 9), ``disconnect`` / a dead socket reconnects with a new URL (backoff
through ``sleep``; one ping on an idle read timeout first). ``poll`` drains the queue through ``parse_inbound``
(``message`` events -> ``sk:<channel>:<ts>``, ``block_actions`` -> ``sk:act:<channel>:<action_ts>``) and the base
policy; envelope / event ids already seen by this process are dropped as ``channel.drop_dup`` at the reader.
"""
import collections
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from fleetmux import channels
from fleetmux.channels import base, envelope

DEFAULT_API = 'https://slack.com/api'
HTTP_TIMEOUT_S = 30
RETRY_MAX_S = 30            # a 429 asking for longer than this fails the send instead of waiting
RATE_ATTEMPTS = 3
VALUE_MAX_BYTES = 2000      # Slack button value limit (ruling 5)
SECTION_MAX_CHARS = 2900    # section text above this -> the card goes out as typed commands (ruling 5)
LABEL_MAX_CHARS = 75        # Slack button text limit (a label is not what the user approves; the command is)
ACTION_ID_MAX = 200
WS_PIP = 'websocket-client==1.8.0'   # optional dependency (deps.yaml optional_python_packages, min 1.8.0)
WS_MIN = (1, 8, 0)
WS_CONNECT_TIMEOUT_S = 30
WS_READ_TIMEOUT_S = 120     # no frame (Slack pings in between) for this long -> treat as dead, reconnect
BACKOFF_CAP_S = 60
AUTH_RETRY_S = 300          # a refused app token: retry rarely (health says needs_login)
RECENT_MAX = 2000           # envelope / event ids remembered for redelivery dedupe
FILE_REASON = 'Slack 第一期不收文件'  # ruling 10
AUTH_ERRORS = ('invalid_auth', 'not_authed', 'account_inactive', 'token_revoked', 'token_expired', 'no_permission',
               'missing_scope', 'not_allowed_token_type', 'ekm_access_denied')


def urllib_transport(method, url, *, headers=None, body=None, timeout=HTTP_TIMEOUT_S):
    """Default transport: stdlib urllib (honours HTTPS_PROXY / NO_PROXY). -> (status, body bytes, headers dict)."""
    req = urllib.request.Request(url, data=body, headers=dict(headers or {}), method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers.items())
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers.items()) if exc.headers else {}


def escape(text):
    """Slack message text: only ``&``, ``<`` and ``>`` are control characters (mentions, links); escape them so a
    worker's output can never ping ``<!channel>`` or forge a link."""
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def unescape(text):
    return text.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')


def _is_timeout(exc):
    """A socket read timeout: ``TimeoutError`` (= ``socket.timeout``) or websocket-client's
    ``WebSocketTimeoutException`` (not a TimeoutError subclass, so matched by name)."""
    return isinstance(exc, TimeoutError) or type(exc).__name__ == 'WebSocketTimeoutException'


def _retry_after(headers):
    for k, v in (headers or {}).items():
        if str(k).lower() == 'retry-after':
            try:
                return max(0, int(float(v)))
            except (TypeError, ValueError):
                return 1
    return 1


class SlackChannel(base.Channel):
    type = 'slack'
    TEXT_LIMIT = 3000
    TEXT_UNITS = 'chars'
    OVERFLOW = 'split'
    RECEIVES = True
    SUPPORTS_CARDS = True
    SUPPORTS_BUTTONS = True
    SUPPORTS_UPDATE = True
    SUPPORTS_FILES = True
    OPTIONAL_DEPS = ('websocket',)

    def __init__(self, account, *, ws_factory=None, sleep=time.sleep, **kw):
        super().__init__(account, **kw)
        self.ws_factory = ws_factory
        self.sleep = sleep
        self.api_base = (account.get('API_BASE') or DEFAULT_API).rstrip('/')
        self._thread_ts = None
        # receive (P9c): the reader thread acks and queues whole frames; poll drains them in order
        self._queue = collections.deque()
        self._cond = threading.Condition()
        self._recent = collections.OrderedDict()  # envelope / event ids seen by this process (redelivery dedupe)
        self._stopping = threading.Event()
        self._thread = None
        self._ws = None

    # ---------------------------------------------------------------- Web API
    def _token(self, key):
        token = (self.account.get(key) or '').strip()
        if not token:
            raise channels.ChannelError('bad_config', key + ' missing in the account file')
        return token

    def _request(self, method, url, headers, body):
        got = (self.transport or urllib_transport)(method, url, headers=headers, body=body, timeout=HTTP_TIMEOUT_S)
        if len(got) == 3:
            status, raw, rheaders = got
        else:
            (status, raw), rheaders = got, {}
        return status, raw, rheaders or {}

    def _api(self, name, data=None, *, token_key='SLACK_BOT_TOKEN', form=False):
        """POST one Web API method; returns the decoded answer (``ok`` true). Rate limits wait through ``sleep``
        (``Retry-After`` <= RETRY_MAX_S) or raise ChannelError('rate_limited'); credential errors raise
        ChannelError('auth'); any other refusal raises RuntimeError naming Slack's error code."""
        headers = {'Authorization': 'Bearer ' + self._token(token_key)}
        if form:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
            body = urllib.parse.urlencode({k: v for k, v in (data or {}).items() if v is not None}).encode()
        else:
            headers['Content-Type'] = 'application/json; charset=utf-8'
            body = json.dumps(data or {}, ensure_ascii=False).encode()
        url = self.api_base + '/' + name
        for attempt in range(RATE_ATTEMPTS):
            status, raw, rheaders = self._request('POST', url, headers, body)
            try:
                answer = json.loads(raw or b'{}')
            except ValueError:
                answer = {}
            if not isinstance(answer, dict):
                answer = {}
            error = str(answer.get('error') or '')
            if status == 429 or error == 'ratelimited':
                wait = _retry_after(rheaders)
                if wait > RETRY_MAX_S or attempt == RATE_ATTEMPTS - 1:
                    raise channels.ChannelError('rate_limited', name + ': rate limited, retry after '
                                                + str(wait) + ' s', retry_after=wait)
                self.sleep(wait)
                continue
            if status in (401, 403) or error in AUTH_ERRORS:
                raise channels.ChannelError('auth', name + ': ' + (error or 'HTTP ' + str(status)))
            if status >= 400 or not answer.get('ok'):
                raise RuntimeError(name + ': ' + (error or 'HTTP ' + str(status)))
            return answer
        raise channels.ChannelError('rate_limited', name + ': rate limited')

    def _channel(self, conversation, reply_ref):
        """The channel id to post into: the inbound ``reply_ref.channel``, else the conversation; a user id is
        opened as a DM first (proactive push), cached in the store as ``dm:<U>``."""
        target = str((reply_ref or {}).get('channel') or conversation)
        if target[:1] not in ('U', 'W'):
            return target
        cached = self.store.get('dm:' + target)
        if cached:
            return cached
        opened = self._api('conversations.open', {'users': target})
        dm = str((opened.get('channel') or {}).get('id') or '')
        if not dm:
            raise RuntimeError('conversations.open: no channel id')
        self.store.set('dm:' + target, dm)
        if target in self.account.allow:
            self.store.set('dmuser:' + dm, target)
        return dm

    # ---------------------------------------------------------------- sends (P9b)
    def send_text(self, conversation, text, *, reply_ref=None, key=None):
        """Ruling 8 (contracts §8, the one registered exception): reset the thread state, then the base send."""
        self._thread_ts = None
        return super().send_text(conversation, text, reply_ref=reply_ref, key=key)

    def _send_text(self, conversation, text, reply_ref):
        channel = self._channel(conversation, reply_ref)
        thread = (reply_ref or {}).get('thread_ts') or self._thread_ts
        data = {'channel': channel, 'text': escape(text), 'unfurl_links': False, 'unfurl_media': False}
        if thread:
            data['thread_ts'] = str(thread)
            if (reply_ref or {}).get('broadcast'):  # §53: a result also shows in the chat itself
                data['reply_broadcast'] = True
        ts = self._api('chat.postMessage', data).get('ts')
        if self._thread_ts is None:
            self._thread_ts = thread or ts  # the chunks after the first follow it in its thread
        return ts

    @staticmethod
    def _blocks(card):
        """Block Kit for a card, or None when it must go out as typed commands (ruling 5): a button value over
        VALUE_MAX_BYTES or the section text over SECTION_MAX_CHARS. Ruling 6: action_id = command name (a repeated
        name gets ``.2``, ``.3`` ...), value = the rest of the command, nonce included."""
        section = card['title'] + ('\n\n' + card['text'] if card.get('text') else '')
        if len(section) > SECTION_MAX_CHARS:
            return None
        buttons, seen = [], {}
        for b in card.get('buttons') or []:
            name, _, rest = b['command'][1:].partition(' ')
            if not name or len(name) > ACTION_ID_MAX or len(rest.encode('utf-8')) > VALUE_MAX_BYTES:
                return None
            seen[name] = seen.get(name, 0) + 1
            button = {'type': 'button', 'text': {'type': 'plain_text', 'text': b['label'][:LABEL_MAX_CHARS]},
                      'action_id': name if seen[name] == 1 else name + '.' + str(seen[name])}
            if rest:
                button['value'] = rest
            if name == 'approve':
                button['style'] = 'danger'
            buttons.append(button)
        blocks = [{'type': 'section', 'text': {'type': 'plain_text', 'text': section}}]
        if buttons:
            blocks.append({'type': 'actions', 'elements': buttons})
        return blocks

    def _send_card(self, conversation, card, reply_ref):
        blocks = self._blocks(card)
        if blocks is None:  # the whole card as typed commands, nothing the user approves is cut
            return self.send_text(conversation, envelope.card_text(card), reply_ref=reply_ref, key=self.send_key)
        channel = self._channel(conversation, reply_ref)
        data = {'channel': channel, 'text': escape(card['title']), 'blocks': blocks, 'unfurl_links': False,
                'unfurl_media': False}
        if (reply_ref or {}).get('thread_ts'):
            data['thread_ts'] = str(reply_ref['thread_ts'])
        return self._api('chat.postMessage', data).get('ts')

    def _update_card(self, conversation, message_id, card):
        channel = self._channel(conversation, None)
        blocks = self._blocks(card)
        data = {'channel': channel, 'ts': str(message_id), 'unfurl_links': False, 'unfurl_media': False}
        if blocks is None:
            data.update(text=escape(envelope.card_text(card)), blocks=[])
        else:
            data.update(text=escape(card['title']), blocks=blocks)
        self._api('chat.update', data)
        return str(message_id)

    def _send_file(self, conversation, path, caption, reply_ref):
        channel = self._channel(conversation, reply_ref)
        p = Path(path)
        data = p.read_bytes()
        got = self._api('files.getUploadURLExternal', {'filename': p.name, 'length': len(data)}, form=True)
        upload_url, file_id = got.get('upload_url'), got.get('file_id')
        if not upload_url or not file_id:
            raise RuntimeError('files.getUploadURLExternal: no upload_url / file_id')
        # a pre-signed URL: the bot token is not sent there
        status, _, _ = self._request('POST', upload_url, {'Content-Type': 'application/octet-stream'}, data)
        if not 200 <= status < 300:
            raise RuntimeError('file upload: HTTP ' + str(status))
        done = {'files': json.dumps([{'id': file_id, 'title': p.name}]), 'channel_id': channel}
        if caption:
            done['initial_comment'] = caption
        if (reply_ref or {}).get('thread_ts'):
            done['thread_ts'] = str(reply_ref['thread_ts'])
        self._api('files.completeUploadExternal', done, form=True)
        return file_id

    def check(self):
        """``auth.test`` with the bot token (no message sent). Never raises."""
        try:
            got = self._api('auth.test', {}, form=True)
            return base.SendResult(ok=True, message_id=str(got.get('user_id') or '') or None)
        except Exception as exc:
            return self._fail('check', exc)

    def private_target_ok(self, conversation):
        """Ruling 4: a whitelisted user id (``U…`` / ``W…``), or a DM channel (``D…``) the store maps
        (``dmuser:<D>``) to a whitelisted user. Anything else is not a private target."""
        if not isinstance(conversation, str) or not conversation:
            return False
        if conversation[:1] in ('U', 'W'):
            return conversation in self.account.allow
        if conversation.startswith('D'):
            try:
                user = self.store.get('dmuser:' + conversation)
            except Exception:
                return False
            return bool(user) and user in self.account.allow
        return False

    # ---------------------------------------------------------------- receive (P9c): Socket Mode
    # Slack wants every frame that carries an envelope_id acknowledged within 3 s, or it redelivers. So the reader
    # thread sends the ack FIRST, before any parsing, then only queues the whole frame (no policy, no Redis).
    # poll() drains the queue through parse_inbound and the base policy; a frame leaves the queue only in _ack(),
    # once final, so a Retryable keeps it for the next poll. Residual risk (survey §0-4): a frame acked by the reader
    # but not yet handed to the fleet is lost if the actor process dies -- Slack does not redeliver an acked frame.
    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        if self.ws_factory is None:
            self._websocket()  # MissingDependency now, before any thread: sends keep working
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name='slack-socket-' + self.account.name, daemon=True)
        self._thread.start()

    def close(self):
        try:
            self._stopping.set()
            ws = self._ws
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
            with self._cond:
                self._cond.notify_all()
        except Exception:
            pass

    def _websocket(self):
        module = self.require('websocket', WS_PIP)
        if not callable(getattr(module, 'create_connection', None)):  # the unrelated PyPI package 'websocket'
            raise channels.MissingDependency(WS_PIP, "the importable 'websocket' module is not websocket-client;"
                                             ' inbound off, sending still works')
        version = tuple(int(p) for p in str(getattr(module, '__version__', '0')).split('.')[:3] if p.isdigit())
        if version and version < WS_MIN:
            raise channels.MissingDependency(WS_PIP, 'installed websocket-client ' + module.__version__
                                             + ' is too old; inbound off, sending still works')
        return module

    def _connect(self, url):
        if self.ws_factory is not None:
            return self.ws_factory(url)
        ws = self._websocket().create_connection(url, timeout=WS_CONNECT_TIMEOUT_S)
        ws.settimeout(WS_READ_TIMEOUT_S)
        return ws

    def _run(self):
        try:
            self._socket_loop()
        except Exception as exc:  # the thread must not die silently
            self._fail('socket', exc)

    def _socket_loop(self):
        """apps.connections.open -> ws_factory -> one session, until close(). Backoff through ``sleep``: 1 s after a
        session that got ``hello``, doubling up to BACKOFF_CAP_S while connecting fails; AUTH_RETRY_S when a token
        is refused (health needs_login)."""
        backoff = 1
        while not self._stopping.is_set():
            wait = None
            try:
                answer = self._api('apps.connections.open', {}, token_key='SLACK_APP_TOKEN', form=True)
                url = answer.get('url')
                if not url:
                    raise RuntimeError('apps.connections.open: no url')
                ws = self._connect(url)
                if self._socket_session(ws):
                    backoff = 1
                if not self._stopping.is_set():
                    self._state = 'reconnecting'
            except channels.ChannelError as exc:
                self._fail('socket', exc)
                if exc.kind in ('auth', 'bad_config'):
                    wait = AUTH_RETRY_S
                elif exc.retry_after:
                    wait = exc.retry_after
            except Exception as exc:
                self._fail('socket', exc)
                self._state = 'reconnecting'
            if self._stopping.is_set():
                break
            self._backoff_wait(wait if wait is not None else backoff)
            backoff = min(backoff * 2, BACKOFF_CAP_S)

    def _backoff_wait(self, seconds):
        """Reconnect backoff through the injected ``sleep`` (ruling 3). With the real ``time.sleep`` it waits on the
        stop event instead, so ``close()`` ends a 300 s needs_login wait at once instead of after it."""
        if self.sleep is time.sleep:
            self._stopping.wait(seconds)
        else:
            self.sleep(seconds)

    def _socket_session(self, ws):
        """One connection: for each frame ack first (``{"envelope_id": …}``), then ``hello`` -> health, ``disconnect``
        -> return (reconnect with a new URL), a redelivered envelope / event id -> ``channel.drop_dup``, anything
        else with an envelope id -> queued. Returns True when a ``hello`` arrived. Never raises."""
        self._ws = ws
        got_hello = idle_pinged = False
        try:
            while not self._stopping.is_set():
                try:
                    frame = ws.recv()
                except Exception as exc:
                    # A quiet DM is normal: the first read timeout sends one ping (its pong resets the read); a
                    # second timeout in a row, or any other error (closed), ends the session and the loop reconnects.
                    if _is_timeout(exc) and not idle_pinged and callable(getattr(ws, 'ping', None)):
                        idle_pinged = True
                        try:
                            ws.ping()
                            continue
                        except Exception:
                            pass
                    break
                idle_pinged = False
                if frame is None or frame == '' or frame == b'':
                    break
                try:
                    msg = json.loads(frame)
                except (TypeError, ValueError):
                    continue
                if not isinstance(msg, dict):
                    continue
                eid = msg.get('envelope_id')
                if eid:
                    try:
                        ws.send(json.dumps({'envelope_id': eid}))
                    except Exception:
                        break
                kind = msg.get('type')
                if kind == 'hello':
                    got_hello = True
                    self._hello(msg)
                    continue
                if kind == 'disconnect':
                    break
                if not eid:
                    continue
                if self._redelivered(msg):
                    self.audit('channel.drop_dup', envelope_id=str(eid), retry_attempt=msg.get('retry_attempt') or 0)
                    continue
                with self._cond:
                    self._queue.append(msg)
                    self._cond.notify_all()
        finally:
            self._ws = None
            try:
                ws.close()
            except Exception:
                pass
        return got_hello

    def _hello(self, msg):
        n = msg.get('num_connections')
        if isinstance(n, int) and not isinstance(n, bool) and n > 1:
            # ruling 9: another client holds a connection for this app token and Slack splits events between them
            self._fail('socket', channels.ChannelError(
                'conflict', str(n) + ' Socket Mode connections share this app token; events are split between them'))
        else:
            self._state = 'ok'

    def _redelivered(self, msg):
        keys = ['env:' + str(msg.get('envelope_id'))]
        if msg.get('type') == 'events_api':
            event_id = (msg.get('payload') or {}).get('event_id')
            if event_id:
                keys.append('evt:' + str(event_id))
        if any(k in self._recent for k in keys):
            return True
        for k in keys:
            self._recent[k] = True
        while len(self._recent) > RECENT_MAX:
            self._recent.popitem(last=False)
        return False

    def _poll(self, timeout):
        with self._cond:
            if not self._queue and timeout and timeout > 0 and not self._stopping.is_set():
                self._cond.wait(timeout)
            return list(self._queue)

    def _ack(self, raw):
        """The frame is final (received or deterministically dropped): take it off the queue. (Slack's own ack was
        sent by the reader long ago.) A raw item that was never queued (``inbound(raw)``) is a no-op."""
        with self._cond:
            for i, item in enumerate(self._queue):
                if item is raw:
                    del self._queue[i]
                    return

    def parse_inbound(self, raw):
        if not isinstance(raw, dict):
            raise ValueError('not a Socket Mode frame')
        payload = raw.get('payload') or {}
        if raw.get('type') == 'events_api':
            return self._parse_event(payload)
        if raw.get('type') == 'interactive':
            return self._parse_action(payload)
        self.audit('channel.drop_invalid', reason='unsupported frame type ' + str(raw.get('type'))[:40])
        return []

    def _parse_event(self, payload):
        event = payload.get('event') or {}
        if event.get('type') != 'message':
            self.audit('channel.drop_invalid', reason='unsupported event ' + str(event.get('type'))[:40])
            return []
        if event.get('bot_id'):
            self.audit('channel.drop_invalid', reason='bot message')
            return []
        subtype = event.get('subtype')
        if subtype and subtype != 'file_share':
            self.audit('channel.drop_invalid', reason='subtype ' + str(subtype)[:40])
            return []
        channel, user, ts = event.get('channel'), event.get('user'), event.get('ts')
        if not (channel and user and ts):
            raise ValueError('message without channel / user / ts')
        # §53「直连 worker」: every reply goes into the thread under the user's message (its root: the message itself,
        # or the thread it was typed in); in_thread marks a reply typed inside a thread, which may be bound to a worker
        root = str(event.get('thread_ts') or ts)
        reply_ref = {'channel': str(channel), 'thread_ts': root}
        if root != str(ts):
            reply_ref['in_thread'] = True
        rejected = None
        if subtype == 'file_share':  # ruling 10: say the file was not taken, never drop it silently
            rejected = [{'name': str(f.get('name') or f.get('title') or f.get('id') or 'file')[:200],
                         'reason': FILE_REASON} for f in (event.get('files') or []) if isinstance(f, dict)]
            rejected = rejected or [{'name': 'file', 'reason': FILE_REASON}]
        return [envelope.make(self.type, self.account.name, conversation=channel, message_id=str(channel) + ':' + str(ts),
                              sender=user, text=unescape(str(event.get('text') or '')),
                              private=event.get('channel_type') == 'im', verified=True, at=int(float(ts)),
                              reply_ref=reply_ref, rejected_files=rejected)]

    def _parse_action(self, payload):
        if payload.get('type') != 'block_actions':
            self.audit('channel.drop_invalid', reason='unsupported interaction ' + str(payload.get('type'))[:40])
            return []
        actions = payload.get('actions') or []
        if not actions or not isinstance(actions[0], dict):
            raise ValueError('block_actions without an action')
        action = actions[0]
        container = payload.get('container') or {}
        user = (payload.get('user') or {}).get('id')
        channel = (payload.get('channel') or {}).get('id') or container.get('channel_id')
        card_ts = (payload.get('message') or {}).get('ts') or container.get('message_ts')
        action_ts = action.get('action_ts')
        name = str(action.get('action_id') or '').split('.', 1)[0]  # ruling 6: '.2' suffix stripped
        value = str(action.get('value') or '')
        if not (user and channel and action_ts and name):
            raise ValueError('block_actions without user / channel / action')
        command = '/' + name + (' ' + value if value else '')
        act = {'command': command}
        if card_ts:
            act['card_message_id'] = str(card_ts)
        # ruling 7: id from action_ts (a redelivery has a new envelope_id, the same action_ts); private = a DM
        return [envelope.make(self.type, self.account.name, conversation=channel,
                              message_id='act:' + str(channel) + ':' + str(action_ts), sender=user, text=command,
                              private=str(channel).startswith('D'), verified=True, at=int(float(action_ts)),
                              reply_ref={'channel': str(channel)}, action=act)]

    def on_accepted(self, env, outcome):
        """Ruling 4: remember which whitelisted user a DM channel belongs to (private_target_ok)."""
        if env.get('private') and str(env.get('conversation', '')).startswith('D'):
            self.store.set('dmuser:' + env['conversation'], env['sender'])


ADAPTER = SlackChannel
