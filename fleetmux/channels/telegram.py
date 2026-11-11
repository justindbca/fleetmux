"""Telegram channel: Bot API over plain HTTPS client calls (package P2; contracts §8 "Telegram 专项", design §53).

Receive (P2a): ``getUpdates`` long poll (``timeout`` 50 s, HTTP 65 s, ``allowed_updates`` message + callback_query).
The cursor (``offset``) lives in the account store and moves only past final items (``_ack``, contracts §8 入站确认):
a transient failure (a download that hit a 5xx / network error) raises ``channels.Retryable`` and the update comes
back on the next poll. Private messages from the whitelisted user become envelopes ``tg:<message_id>:<chat_id>``;
inline-button presses become ``tg:cb:<callback_query.id>`` with ``action.command`` = ``callback_data``, answered
after they are received (``on_accepted``: 已收到) or with an empty answer when dropped (``on_dropped``). Documents and
the largest photo are downloaded into ``inbox_dir`` (exclusive new files ``tg-<message_id>-<name>``); files over the
Bot API's 20 MB download limit and media this channel does not take (stickers, voice, video, ...) are refused through
``rejected_files``; a message without caption that carries media gets a fixed label as text, never a file name.

Send (P2b): ``_send_text`` = one ``sendMessage`` chunk, plain text (no ``parse_mode``), ``reply_parameters`` on the
first chunk only; the base splits at 4000 UTF-16 units and this layer refuses a longer chunk. A 429 whose
``retry_after`` is <= 30 s is waited out once (injectable ``sleep``) and retried; longer -> the send fails and the
queue retries. ``_send_card`` = one message with an inline keyboard (``callback_data`` = the button's command); a
command over 64 bytes (``envelope.COMMAND_MAX_BYTES``) sends the whole card as ``card_text`` (typed commands, never
truncated) and returns ok. ``_update_card`` = ``editMessageText`` ("message is not modified" counts as success; one
message, so an over-long typed form is cut in its body). ``_send_file`` = hand-built multipart ``sendDocument``
(caption cut to 1024 UTF-16 units, > 50 MB fails without a call). ``check()`` = ``getMe`` + ``getWebhookInfo`` (a set
webhook is reported as a 409 risk, never deleted); module ``LOGIN`` reads the token and checks it with ``getMe``.

Account file keys: ``BOT_TOKEN`` (required), ``PROXY`` (optional http(s) proxy url, overrides the environment),
``API_BASE`` (optional, default the public Bot API), plus the shared ``ALLOW`` / ``PUSH_TO``.

Errors on the inbound side are ``channels.ChannelError``: 401 / 404 -> auth, 409 -> conflict (another poller or a
webhook; never deleted automatically), 429 -> rate_limited (``retry_after``), 5xx / network -> network. Every error
string is redacted by the base (the token sits in every URL).
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from fleetmux import channels
from fleetmux.channels import base, envelope, provenance

API_DEFAULT = 'https://api.telegram.org'
POLL_TIMEOUT_S = 50          # Telegram long poll (notify_bus telegram_poll POLL_TIMEOUT_S)
HTTP_SLACK_S = 15            # HTTP timeout = long-poll timeout + this (50 -> 65)
HTTP_TIMEOUT_S = 30
DOWNLOAD_TIMEOUT_S = 120
ALLOWED_UPDATES = ['message', 'callback_query']
DOWNLOAD_MAX_BYTES = 20 * 1024 * 1024   # Bot API getFile limit
SEND_FILE_MAX_BYTES = 50 * 1024 * 1024  # Bot API sendDocument limit
UPLOAD_TIMEOUT_S = 300
CAPTION_MAX = 1024           # notify_bus channels/telegram.py: caption[:1024]
RETRY_AFTER_MAX_S = 30       # a send waits out a 429 up to this long, once
# Media this channel does not take in, checked before document / photo (an animation also carries a document).
UNSUPPORTED_MEDIA = ('animation', 'sticker', 'voice', 'video', 'video_note', 'audio')
LABELS = {'photo': '[图片]', 'document': '[文件]', 'animation': '[动图]', 'sticker': '[贴纸]', 'voice': '[语音]',
          'video': '[视频]', 'video_note': '[视频]', 'audio': '[音频]'}


class BotAPIError(channels.ChannelError):
    """A Bot API answer that is not ``ok``: HTTP status, the platform's description and ``retry_after`` (429)."""

    def __init__(self, status, description='', retry_after=None):
        self.status, self.description = status, str(description or '')
        if status in (401, 404):
            kind = 'auth'            # a revoked / wrong token: getMe and every method answer 401 (404 for a bad path)
        elif status == 409:
            kind = 'conflict'        # another getUpdates consumer, or a webhook is set
        elif status == 429:
            kind = 'rate_limited'
        elif status >= 500:
            kind = 'network'
        else:
            kind = 'transient'
        super().__init__(kind, 'HTTP ' + str(status) + (': ' + self.description if self.description else ''),
                         retry_after=retry_after)


def _decode(status, raw):
    """Bot API JSON answer -> result; anything else -> BotAPIError."""
    try:
        data = json.loads(raw.decode('utf-8') if isinstance(raw, (bytes, bytearray)) else raw)
    except (ValueError, UnicodeDecodeError, AttributeError):
        data = None
    if not isinstance(data, dict):
        raise BotAPIError(status, 'answer is not JSON')
    if status == 200 and data.get('ok') is True:
        return data.get('result')
    params = data.get('parameters') if isinstance(data.get('parameters'), dict) else {}
    return_status = data.get('error_code') if isinstance(data.get('error_code'), int) else status
    raise BotAPIError(return_status if status == 200 else status, data.get('description', ''),
                      retry_after=params.get('retry_after'))


class TelegramChannel(base.Channel):
    type = 'telegram'
    TEXT_LIMIT = 4000
    TEXT_UNITS = 'utf16'
    SUPPORTS_CARDS = True
    SUPPORTS_BUTTONS = True
    SUPPORTS_UPDATE = True
    SUPPORTS_FILES = True

    def __init__(self, account, *, sleep=time.sleep, **kwargs):
        """``sleep``: how a send waits out a 429 ``retry_after`` (tests inject a recorder)."""
        super().__init__(account, **kwargs)
        self.sleep = sleep
        self.token = str(account.get('BOT_TOKEN', '')).strip()
        self.api_base = (str(account.get('API_BASE', '')).strip() or API_DEFAULT).rstrip('/')

    # ---------------------------------------------------------------- HTTP
    def _http(self, method, url, *, headers=None, body=None, timeout=HTTP_TIMEOUT_S):
        """Default transport (``base.http_transport`` shape) with this account's proxy: ``PROXY`` from the account
        file, else HTTPS_PROXY / NO_PROXY from the environment. Only http(s) proxies; the environment is never
        changed. Certificates are verified (urllib's default context)."""
        proxy = str(self.account.get('PROXY', '')).strip()
        handler = urllib.request.ProxyHandler({'http': proxy, 'https': proxy} if proxy else None)
        # Only the proxy urllib will use for this URL's scheme is checked: the environment also yields 'no'
        # (NO_PROXY: a host list) and 'all' (ALL_PROXY), which urllib never dispatches on.
        value = handler.proxies.get(urllib.parse.urlsplit(url).scheme)
        if value and not str(value).lower().startswith(('http://', 'https://')):
            raise channels.ChannelError('bad_config', '需 http 代理：Telegram 只支持 http:// 或 https:// 代理，'
                                        '不支持 socks')
        opener = urllib.request.build_opener(handler)
        req = urllib.request.Request(url, data=body, headers=dict(headers or {}), method=method)
        try:
            resp = opener.open(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        with resp:
            return getattr(resp, 'status', None) or resp.getcode(), resp.read()

    def _transport(self):
        return self.transport if self.transport is not None else self._http

    def _request(self, method, url, *, headers=None, body=None, timeout=HTTP_TIMEOUT_S):
        try:
            return self._transport()(method, url, headers=headers, body=body, timeout=timeout)
        except channels.ChannelError:
            raise
        except OSError as exc:
            raise channels.ChannelError('network', self.redact(type(exc).__name__ + ': ' + str(exc))) from None

    def _call(self, name, params=None, *, timeout=HTTP_TIMEOUT_S):
        """POST one Bot API method with a JSON body -> its ``result``."""
        status, raw = self._request('POST', self.api_base + '/bot' + self.token + '/' + name,
                                    headers={'Content-Type': 'application/json'},
                                    body=json.dumps(params or {}, ensure_ascii=False).encode('utf-8'), timeout=timeout)
        return _decode(status, raw)

    # ---------------------------------------------------------------- receive
    def _poll(self, timeout):
        wait = POLL_TIMEOUT_S if timeout is None else max(0, min(int(timeout), POLL_TIMEOUT_S))
        params = {'timeout': wait, 'allowed_updates': ALLOWED_UPDATES}
        offset = self.store.get('offset')
        if offset:
            params['offset'] = int(offset)
        result = self._call('getUpdates', params, timeout=wait + HTTP_SLACK_S)
        return list(result or [])

    def _ack(self, raw):
        """Move the offset past this update (never backwards)."""
        update_id = raw['update_id']
        if not isinstance(update_id, int) or isinstance(update_id, bool):
            raise ValueError('update without an integer update_id')
        current = self.store.get('offset')
        if current is None or int(current) <= update_id:
            self.store.set('offset', update_id + 1)

    def parse_inbound(self, raw):
        if not isinstance(raw, dict) or not isinstance(raw.get('update_id'), int):
            raise ValueError('not a Telegram update')
        if isinstance(raw.get('callback_query'), dict):
            return [self._callback(raw['callback_query'])]
        msg = raw.get('message') if isinstance(raw.get('message'), dict) else raw.get('channel_post')
        if not isinstance(msg, dict):
            return []  # an update kind outside allowed_updates: nothing to receive (final)
        return [self._message(msg)]

    def _message(self, msg):
        chat = msg['chat']
        who = msg.get('from') if isinstance(msg.get('from'), dict) else msg.get('sender_chat') or {}
        sender = str(who.get('id', ''))
        private = chat.get('type') == 'private'
        mid, chat_id = msg['message_id'], chat['id']
        text, prov = provenance.telegram_provenance(msg)  # P8b: forwarded / caption / quoted / contact / location
        files, rejected, label = [], [], ''
        for kind in UNSUPPORTED_MEDIA:
            if msg.get(kind):
                label = LABELS[kind]
                rejected.append({'name': kind, 'reason': 'Telegram ' + kind + ' 不收（只收文字、文件和图片）'})
                break
        else:
            media = self._media(msg)
            if media:
                label = LABELS[media[0]]
                # download only what the policy will let in: never write a stranger's or a group's file to disk
                if private and sender in self.account.allow:
                    got = self._download(mid, *media[1:])
                    (files if 'path' in got else rejected).append(got)
        return envelope.make(self.type, self.account.name, conversation=str(chat_id), message_id=f'{mid}:{chat_id}',
                             sender=sender, text=text or ('' if prov else label), private=private, verified=True,
                             at=msg.get('date') or self.clock(), sender_name=str(who.get('first_name', '')) or None,
                             files=files or None, rejected_files=rejected or None, provenance=prov or None,
                             reply_ref={'chat_id': str(chat_id), 'message_id': str(mid)})

    @staticmethod
    def _media(msg):
        """('document' | 'photo', file_id, display name, declared size) or None."""
        doc = msg.get('document')
        if isinstance(doc, dict) and doc.get('file_id'):
            return 'document', doc['file_id'], str(doc.get('file_name') or 'document'), doc.get('file_size')
        photos = [p for p in msg.get('photo') or [] if isinstance(p, dict) and p.get('file_id')]
        if photos:
            best = max(photos, key=lambda p: (p.get('width', 0) * p.get('height', 0), p.get('file_size') or 0))
            return 'photo', best['file_id'], 'photo', best.get('file_size')
        return None

    def _download(self, mid, file_id, name, size):
        """-> {name, path, size} when the file is on disk, else {name, reason} (deterministic refusal).
        5xx / network / 429 -> channels.Retryable: the update is not acknowledged and comes back."""
        too_big = {'name': name, 'reason': '文件超过 20 MB（Telegram Bot API 下载上限）'}
        if self.inbox_dir is None:
            return {'name': name, 'reason': '本通道未开启收文件'}
        if isinstance(size, int) and size > min(self.MAX_FILE_BYTES, DOWNLOAD_MAX_BYTES):
            return too_big
        try:
            info = self._call('getFile', {'file_id': file_id})
        except BotAPIError as exc:
            if exc.status >= 500 or exc.status == 429:
                raise channels.Retryable('getFile ' + str(exc), retry_after=exc.retry_after) from None
            return {'name': name, 'reason': 'Telegram 拒绝取文件（HTTP ' + str(exc.status) + '）'}
        except channels.ChannelError as exc:
            raise channels.Retryable('getFile ' + str(exc)) from None
        path = (info or {}).get('file_path')
        expected = (info or {}).get('file_size', size)
        if not path:
            return {'name': name, 'reason': 'Telegram 没给出文件路径'}
        if isinstance(expected, int) and expected > min(self.MAX_FILE_BYTES, DOWNLOAD_MAX_BYTES):
            return too_big
        try:
            status, data = self._request('GET', self.api_base + '/file/bot' + self.token + '/' + path,
                                         timeout=DOWNLOAD_TIMEOUT_S)
        except channels.ChannelError as exc:
            raise channels.Retryable('download ' + str(exc)) from None
        if status >= 500 or status == 429:
            raise channels.Retryable('download HTTP ' + str(status))
        if status != 200:
            return {'name': name, 'reason': '下载失败（HTTP ' + str(status) + '）'}
        if isinstance(expected, int) and len(data) != expected:
            raise channels.Retryable('download incomplete')  # nothing written: a short read is not a file
        if len(data) > self.MAX_FILE_BYTES:
            return too_big
        if name == 'photo':
            suffix = path.rsplit('.', 1)[-1] if '.' in path.rsplit('/', 1)[-1] else 'jpg'
            name = 'photo.' + suffix
        target, fh = self.new_inbox_file(name, prefix='tg-' + str(mid) + '-')
        with fh:
            fh.write(data)
        return {'name': name, 'path': str(target), 'size': len(data)}

    def _callback(self, cq):
        msg = cq.get('message') if isinstance(cq.get('message'), dict) else {}
        chat = msg.get('chat') if isinstance(msg.get('chat'), dict) else {}
        presser = cq['from']['id']
        chat_id = chat.get('id', presser)
        data = cq.get('data') if isinstance(cq.get('data'), str) else ''
        action = {'command': data}
        if msg.get('message_id') is not None:
            action['card_message_id'] = str(chat_id) + ':' + str(msg['message_id'])
        return envelope.make(self.type, self.account.name, conversation=str(chat_id), message_id='cb:' + str(cq['id']),
                             sender=str(presser), text=data, private=chat.get('type') == 'private', verified=True,
                             at=msg.get('date') or self.clock(), action=action,
                             reply_ref={'chat_id': str(chat_id), 'message_id': str(msg.get('message_id', ''))})

    def _answer_callback(self, env, text):
        prefix = channels.prefix(self.type) + ':cb:'
        if env['message_id'].startswith(prefix):
            params = {'callback_query_id': env['message_id'][len(prefix):]}
            if text:
                params['text'] = text
            self._call('answerCallbackQuery', params)

    def on_accepted(self, env, outcome):
        self._answer_callback(env, '已收到')  # only after the press is received (persisted / duplicate)

    def on_dropped(self, env, reason):
        self._answer_callback(env, '')  # stop the button spinner; claim nothing

    # ---------------------------------------------------------------- send
    def _send(self, name, params=None, *, body=None, headers=None, timeout=HTTP_TIMEOUT_S):
        """One send-side Bot API call; a 429 with ``retry_after`` <= 30 s is waited out once (``self.sleep``) and
        retried, a longer one fails the send (the queue retries later)."""
        params = dict(params or {}, link_preview_options={'is_disabled': True}) if self.link_preview is False and name in ('sendMessage', 'editMessageText') else params  # P8f: links.preview off
        for attempt in (1, 2):
            try:
                if body is None:
                    return self._call(name, params, timeout=timeout)
                status, raw = self._request('POST', self.api_base + '/bot' + self.token + '/' + name,
                                            headers=headers, body=body, timeout=timeout)
                return _decode(status, raw)
            except BotAPIError as exc:
                wait = exc.retry_after
                if (exc.status != 429 or attempt == 2 or not isinstance(wait, (int, float))
                        or isinstance(wait, bool) or wait < 0 or wait > RETRY_AFTER_MAX_S):
                    raise
                self.sleep(wait)

    @staticmethod
    def _reply_parameters(reply_ref):
        if isinstance(reply_ref, dict) and str(reply_ref.get('message_id', '')).strip().lstrip('-').isdigit():
            return {'message_id': int(reply_ref['message_id']), 'allow_sending_without_reply': True}
        return None

    @staticmethod
    def _id(conversation, msg):
        return str(conversation) + ':' + str((msg or {}).get('message_id', ''))

    def _send_text(self, conversation, text, reply_ref):
        """One chunk, plain text (no parse_mode). The base splits by UTF-16 units; this guards the hook anyway."""
        if base.text_units(text, 'utf16') > self.TEXT_LIMIT:
            return base.SendResult(ok=False, error='chunk over ' + str(self.TEXT_LIMIT)
                                   + ' UTF-16 units (Telegram counts UTF-16)')
        params = {'chat_id': conversation, 'text': text}
        reply = self._reply_parameters(reply_ref)
        if reply:
            params['reply_parameters'] = reply
        return self._id(conversation, self._send('sendMessage', params))

    @staticmethod
    def _too_long(card):
        return any(len(b['command'].encode('utf-8')) > envelope.COMMAND_MAX_BYTES for b in card.get('buttons') or [])

    def _card_body(self, card):
        """Title + text as one message (a card is never split: the buttons must sit on one message). Over the limit
        -> cut with '…'."""
        body = card['title'] + ('\n\n' + card['text'] if card.get('text') else '')
        if base.text_units(body, 'utf16') <= self.TEXT_LIMIT:
            return body
        return base.split_text(body, self.TEXT_LIMIT - 1, 'utf16')[0] + '…'

    def _typed_card(self, card):
        """``card_text`` as ONE message (an edit cannot be split): over the limit, the card's ``text`` body is cut
        with '…' so the title and the typed ``回复 /<command>`` lines (the point of the fallback) stay whole."""
        full = envelope.card_text(card)
        if base.text_units(full, 'utf16') <= self.TEXT_LIMIT:
            return full
        room = self.TEXT_LIMIT - base.text_units(envelope.card_text(dict(card, text='')), 'utf16') - 3  # '\n\n' + '…'
        if room >= 1 and card.get('text'):
            return envelope.card_text(dict(card, text=base.split_text(card['text'], room, 'utf16')[0] + '…'))
        return base.split_text(full, self.TEXT_LIMIT - 1, 'utf16')[0] + '…'

    @staticmethod
    def _keyboard(card):
        buttons = [{'text': b['label'], 'callback_data': b['command']} for b in card.get('buttons') or []]
        return {'inline_keyboard': [buttons[i:i + 2] for i in range(0, len(buttons), 2)]}

    def _send_card(self, conversation, card, reply_ref):
        if self._too_long(card):
            # callback_data holds at most 64 bytes: the whole card goes out as its typed form, never truncated
            first = None
            for i, chunk in enumerate(base.split_text(envelope.card_text(card), self.TEXT_LIMIT, 'utf16')):
                got = self._send_text(conversation, chunk, reply_ref if i == 0 else None)
                if isinstance(got, base.SendResult):
                    return got
                first = first or got
            return first
        params = {'chat_id': conversation, 'text': self._card_body(card)}
        if card.get('buttons'):
            params['reply_markup'] = self._keyboard(card)
        reply = self._reply_parameters(reply_ref)
        if reply:
            params['reply_parameters'] = reply
        return self._id(conversation, self._send('sendMessage', params))

    def _update_card(self, conversation, message_id, card):
        chat, _, mid = str(message_id).rpartition(':')
        params = {'chat_id': chat or conversation, 'message_id': int(mid)}
        if self._too_long(card):
            params['text'] = self._typed_card(card)
        else:
            params['text'] = self._card_body(card)
            if card.get('buttons'):
                params['reply_markup'] = self._keyboard(card)
        try:
            self._send('editMessageText', params)
        except BotAPIError as exc:
            if 'message is not modified' not in exc.description:
                raise
        return str(message_id)

    def _send_file(self, conversation, path, caption, reply_ref):
        size = os.path.getsize(path)
        if size > SEND_FILE_MAX_BYTES:
            return base.SendResult(ok=False, error='file over 50 MB (Telegram sendDocument limit)')
        boundary = 'fleetmux-' + uuid.uuid4().hex
        fields = [('chat_id', str(conversation))]
        if caption:
            fields.append(('caption', base.split_text(caption, CAPTION_MAX, 'utf16')[0]))  # Telegram counts UTF-16
        reply = self._reply_parameters(reply_ref)
        if reply:
            fields.append(('reply_parameters', json.dumps(reply)))
        with open(path, 'rb') as fh:
            data = fh.read()
        body = b''.join(
            ('--' + boundary + '\r\nContent-Disposition: form-data; name="' + k + '"\r\n\r\n').encode('utf-8')
            + v.encode('utf-8') + b'\r\n' for k, v in fields)
        filename = os.path.basename(path).replace('"', '_').replace('\r', '_').replace('\n', '_')
        body += ('--' + boundary + '\r\nContent-Disposition: form-data; name="document"; filename="' + filename
                 + '"\r\nContent-Type: application/octet-stream\r\n\r\n').encode('utf-8') + data + b'\r\n'
        body += ('--' + boundary + '--\r\n').encode('utf-8')
        msg = self._send('sendDocument', body=body, timeout=UPLOAD_TIMEOUT_S,
                         headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
        return self._id(conversation, msg)

    def check(self):
        """`channel test` without sending: getMe (the token works) + getWebhookInfo (a webhook would make every
        getUpdates answer 409; reported, never deleted)."""
        try:
            self._call('getMe')
            hook = self._call('getWebhookInfo')
        except Exception as exc:
            return self._fail('check', exc)
        if isinstance(hook, dict) and hook.get('url'):
            return base.SendResult(ok=False, error='a webhook is set on this bot: getUpdates would fail with HTTP 409 '
                                   'conflict; remove it where it was set (fleetmux never deletes it)')
        return base.SendResult(ok=True)


def LOGIN(account_name, io, transport=None):
    """`fleetmux channel login telegram <account>` (contracts §8, brief v2 §2.1-2): read the bot token from the
    terminal, check it with getMe, return the account values to write. The token is never printed."""
    io.print('Telegram bot token for ' + str(account_name) + ' (from @BotFather), then Enter:')
    token = str(io.readline() or '').strip()
    if not token:
        raise ValueError('no token given')
    call = transport or base.http_transport
    try:
        status, raw = call('POST', API_DEFAULT + '/bot' + token + '/getMe', headers={'Content-Type': 'application/json'},
                           body=b'{}', timeout=HTTP_TIMEOUT_S)
        me = _decode(status, raw) or {}
    except Exception as exc:
        raise ValueError('getMe failed: ' + channels.redact(str(exc), {'BOT_TOKEN': token})) from None
    io.print('token ok: bot @' + str(me.get('username', '?')))
    return {'BOT_TOKEN': token}


ADAPTER = TelegramChannel
