"""Telegram adapter protocol tests (packages P2a receive / P2b send; brief v2.1 §6.9, contracts §8 "Telegram 专项").

Account file keys (brief §2.1-1): ``BOT_TOKEN`` (required), ``PROXY`` (optional http(s) proxy url), ``API_BASE``
(optional Bot API base url), plus the shared ``ALLOW`` / ``PUSH_TO``. All values here are fakes (``123:FAKE``).

``FakeBotAPI`` is an in-process callable with the signature of ``base.http_transport`` (method, url, *, headers,
body, timeout) -> (status, body bytes): it records every request and answers from a script. Nothing connects to a
network, nothing listens. The adapter class is looked up inside each test body (never in a fixture), so a missing
or incomplete adapter makes a test FAIL, not ERROR.

``CONTRACT`` feeds the shared adapter contract rows in tests/test_channels_contract.py (``red:P2-telegram``).
"""
import email.parser
import email.policy
import json
import socket
import urllib.request
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture: the repo's private test Redis)
from cmux_fleet import channels
from cmux_fleet.channels import base, envelope

TOKEN = '123:FAKE'
API_BASE = 'https://bot-api.invalid'
USER = '4242'          # the whitelisted user; a private chat id equals the user id
OUTSIDER = '999'
ACCOUNT = 'bot1'
APPROVE = '/approve p-0123abcd'
REJECT = '/reject p-0123abcd'


# ====================================================================== fakes
class FakeBotAPI:
    """Bot API stand-in. ``updates`` are served by getUpdates from the requested offset (Telegram semantics:
    everything with update_id >= offset). ``files``: file_id -> (file_path, bytes). ``broken_files``: file ids
    whose getFile answers 502. ``fail``: method -> list of scripted answers, each (status, payload dict) or an
    exception to raise, consumed in order. ``events``: one ordered log shared with tests (requests + accepts)."""

    def __init__(self, updates=(), files=None):
        self.updates = list(updates)
        self.files = dict(files or {})
        self.broken_files = set()
        self.fail = {}
        self.calls = []
        self.events = []
        self.webhook_url = ''
        self.next_message_id = 500

    def __call__(self, method, url, *, headers=None, body=None, timeout=30):
        parts = urlsplit(url)
        headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        params = dict(parse_qsl(parts.query))
        ctype = headers.get('content-type', '')
        raw = body or b''
        if raw and 'json' in ctype:
            params.update(json.loads(raw))
        elif raw and 'x-www-form-urlencoded' in ctype:
            params.update(dict(parse_qsl(raw.decode())))
        if '/file/bot' in parts.path:
            name, path = 'download', parts.path.split('/file/bot', 1)[1].split('/', 1)[1]
        else:
            name, path = parts.path.rsplit('/', 1)[-1], ''
        call = {'name': name, 'method': method, 'url': url, 'params': params, 'headers': headers, 'body': raw,
                'timeout': timeout, 'path': path}
        self.calls.append(call)
        self.events.append(('api', name, params))
        script = self.fail.get(name)
        if script:
            answer = script.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            status, payload = answer
            return status, json.dumps(payload).encode()
        return self.answer(name, params, path)

    @staticmethod
    def ok(result):
        return 200, json.dumps({'ok': True, 'result': result}).encode()

    def answer(self, name, params, path):
        if name == 'getUpdates':
            offset = int(params.get('offset') or 0)
            return self.ok([u for u in self.updates if u['update_id'] >= offset])
        if name == 'getMe':
            return self.ok({'id': 1, 'is_bot': True, 'first_name': 'fake', 'username': 'fake_bot'})
        if name == 'getWebhookInfo':
            return self.ok({'url': self.webhook_url, 'pending_update_count': 0})
        if name == 'getFile':
            fid = params.get('file_id')
            if fid in self.broken_files:
                return 502, json.dumps({'ok': False, 'error_code': 502, 'description': 'Bad Gateway'}).encode()
            fpath, data = self.files[fid]
            return self.ok({'file_id': fid, 'file_unique_id': 'u' + fid, 'file_size': len(data), 'file_path': fpath})
        if name == 'download':
            for fpath, data in self.files.values():
                if fpath == path:
                    return 200, data
            return 404, b'not found'
        if name in ('sendMessage', 'sendDocument', 'editMessageText'):
            self.next_message_id += 1
            chat = params.get('chat_id', USER)
            return self.ok({'message_id': self.next_message_id, 'date': 1762650000,
                            'chat': {'id': int(chat) if str(chat).lstrip('-').isdigit() else chat, 'type': 'private'}})
        return self.ok(True)

    def named(self, name):
        return [c for c in self.calls if c['name'] == name]


class Audit(list):
    def __call__(self, event, **fields):
        self.append((event, fields))

    def events(self):
        return [e for e, _ in self]


class FakeIO:
    def __init__(self, lines):
        self.lines, self.printed = list(lines), []

    def print(self, text):
        self.printed.append(str(text))

    def readline(self):
        return self.lines.pop(0) if self.lines else ''


def adapter():
    return channels.adapter_class('telegram')


def make(api=None, *, values=None, store=None, audit=None, inbox=None, account=None, **kw):
    acct = account or channels.Account('telegram', ACCOUNT, [USER],
                                       dict({'BOT_TOKEN': TOKEN, 'API_BASE': API_BASE}, **(values or {})))
    return adapter()(acct, transport=api, store=store if store is not None else base.MemoryStore(),
                     audit=audit if audit is not None else Audit(), inbox_dir=inbox, **kw)


def message(update_id, mid, text='hi', *, sender=USER, chat=USER, chat_type='private', **extra):
    msg = {'message_id': mid, 'from': {'id': int(sender), 'is_bot': False, 'first_name': 'u'},
           'chat': {'id': int(chat), 'type': chat_type}, 'date': 1762650000}
    if text is not None:
        msg['text'] = text
    msg.update(extra)
    return {'update_id': update_id, 'message': msg}


def callback(update_id, cb_id, data, *, sender=USER, chat=USER, chat_type='private', card_mid=900):
    return {'update_id': update_id, 'callback_query': {
        'id': cb_id, 'from': {'id': int(sender), 'is_bot': False, 'first_name': 'u'}, 'chat_instance': 'ci',
        'message': {'message_id': card_mid, 'date': 1762650000, 'chat': {'id': int(chat), 'type': chat_type}},
        'data': data}}


def as_list(value):
    return json.loads(value) if isinstance(value, str) else value


def multipart(call):
    """{field name: (filename or None, bytes)} of a multipart/form-data request recorded by FakeBotAPI."""
    head = ('Content-Type: ' + call['headers']['content-type'] + '\r\n\r\n').encode()
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(head + call['body'])
    return {p.get_param('name', header='content-disposition'): (p.get_filename(), p.get_payload(decode=True))
            for p in msg.iter_parts()}


def contract_transport():
    return FakeBotAPI()


CONTRACT = {
    'account': {'ALLOW': USER, 'BOT_TOKEN': TOKEN, 'API_BASE': API_BASE},
    'conversation': USER,
    'transport': contract_transport,
    'inbound_ok': message(1, 11, 'hello contract'),
    'inbound_text': 'hello contract',
    'inbound_foreign': message(2, 12, 'hello contract', sender=OUTSIDER, chat=OUTSIDER),
    'inbound_group': message(3, 13, 'hello contract', chat='-100', chat_type='group'),
    # Telegram authenticates every sender: no 'inbound_unverified'.
    'inbound_button': callback(4, 'cb-ok', APPROVE),
    'inbound_button_text': APPROVE,
    'inbound_button_foreign': callback(5, 'cb-out', APPROVE, sender=OUTSIDER, chat=OUTSIDER),
    'inbound_button_group': callback(6, 'cb-grp', APPROVE, chat='-100', chat_type='group'),
}


# ====================================================================== P2a receive
def test_contract_dict_and_adapter_class_registered():
    required = {'account', 'conversation', 'transport', 'inbound_ok', 'inbound_text', 'inbound_foreign',
                'inbound_group', 'inbound_button', 'inbound_button_text', 'inbound_button_foreign',
                'inbound_button_group'}
    assert required <= set(CONTRACT) and callable(CONTRACT['transport'])
    cls = adapter()
    assert issubclass(cls, base.Channel) and cls.type == 'telegram'
    assert cls.TEXT_LIMIT == 4000 and cls.TEXT_UNITS == 'utf16'
    assert cls.SUPPORTS_CARDS and cls.SUPPORTS_BUTTONS and cls.SUPPORTS_UPDATE and cls.SUPPORTS_FILES


def test_private_message_envelope_fields():
    api = FakeBotAPI([message(10, 77, 'hi there')])
    ch = make(api)
    got = ch.poll(0)
    assert len(got) == 1
    env = got[0]
    assert envelope.problems(env) == []
    assert (env['channel'], env['account'], env['message_id']) == ('telegram', ACCOUNT, 'tg:77:' + USER)
    assert env['conversation'] == USER and env['sender'] == USER and env['text'] == 'hi there'
    assert env['private'] is True and env['verified'] is True
    assert set(env['reply_ref']) == {'chat_id', 'message_id'}
    assert str(env['reply_ref']['chat_id']) == USER and str(env['reply_ref']['message_id']) == '77'


def test_group_and_channel_post_are_private_false():
    group = message(11, 78, 'in a group', chat='-100', chat_type='group')
    post = {'update_id': 12, 'channel_post': {'message_id': 79, 'date': 1762650000, 'text': 'a post',
                                              'sender_chat': {'id': -1001, 'type': 'channel'},
                                              'chat': {'id': -1001, 'type': 'channel'}}}
    audit = Audit()
    ch = make(FakeBotAPI(), audit=audit)
    assert ch.inbound(group) == [] and ch.inbound(post) == []
    drops = [f for e, f in audit if e == 'channel.drop_policy']
    assert len(drops) == 2 and all(f['private'] is False for f in drops)
    assert drops[1]['sender'] == '-1001'
    assert 'channel.drop_invalid' not in audit.events()


def test_offset_advances_past_batch_even_when_one_item_raises():
    """TG-C1: the injected failure is an ordinary format error (not channels.Retryable): deterministic, final."""
    api = FakeBotAPI([message(20, 1, 'one'), message(21, 2, 'two'), message(22, 3, 'three')])
    audit = Audit()
    ch = make(api, audit=audit)
    real = ch.parse_inbound

    def malformed_second(raw):
        if isinstance(raw, dict) and raw.get('update_id') == 21:
            raise ValueError('malformed update')
        return real(raw)
    ch.parse_inbound = malformed_second
    assert [e['text'] for e in ch.poll(0)] == ['one', 'three']
    assert 'channel.drop_invalid' in audit.events()
    assert ch.store.get('offset') == '23'
    assert ch.poll(0) == []
    assert int(api.named('getUpdates')[-1]['params']['offset']) == 23


def test_retryable_item_holds_offset_then_resumes_after_retry(tmp_path):
    """TG-C1 supplement (supervisor-approved): a transient download failure (getFile 502 -> channels.Retryable)
    keeps the cursor before that update; nothing after it is acknowledged; the next poll resumes there."""
    doc = message(31, 2, None, caption='doc', document={'file_id': 'F31', 'file_unique_id': 'uF31',
                                                        'file_name': 'a.txt', 'file_size': 5})
    api = FakeBotAPI([message(30, 1, 'one'), doc, message(32, 3, 'three')], files={'F31': ('documents/a.txt', b'hello')})
    api.broken_files.add('F31')
    audit = Audit()
    ch = make(api, audit=audit, inbox=tmp_path / 'inbox')
    assert [e['text'] for e in ch.poll(0)] == ['one']
    assert ch.store.get('offset') == '31'
    assert 'channel.inbound_retrying' in audit.events() and ch.health()['state'] == 'retrying'
    api.broken_files.clear()
    got = ch.poll(0)
    assert int(api.named('getUpdates')[-1]['params']['offset']) == 31
    assert [e['message_id'] for e in got] == ['tg:2:' + USER, 'tg:3:' + USER]
    assert len(got[0]['files']) == 1 and got[1]['text'] == 'three'
    assert ch.store.get('offset') == '33'
    assert ch.health()['state'] == 'ok'  # v2 normal state (P0b moves INBOUND_STATES / health to 'ok')


class CrashBeforeOffset(base.MemoryStore):
    """A store whose first offset write dies: the process 'crashed' after receiving, before the cursor moved."""

    def __init__(self):
        super().__init__()
        self.crash = True

    def set(self, key, value, ttl=None):
        if key == 'offset' and self.crash:
            self.crash = False
            raise OSError('crash before the offset was written')
        super().set(key, value, ttl)


def test_restart_same_store_no_refetch_and_crash_before_offset_yields_same_message_id():
    api = FakeBotAPI([message(40, 5, 'first')])
    store = base.MemoryStore()
    assert [e['text'] for e in make(api, store=store).poll(0)] == ['first']
    assert store.get('offset') == '41'
    assert make(api, store=store).poll(0) == []  # a restart on the same store does not fetch it again
    assert int(api.named('getUpdates')[-1]['params']['offset']) == 41

    api2 = FakeBotAPI([message(40, 5, 'first')])
    crash = CrashBeforeOffset()
    seen, inbox = [], {}

    def accept(env):  # the actor's atomic seen + enqueue, simulated
        seen.append(env['message_id'])
        if env['message_id'] in inbox:
            return 'duplicate'
        inbox[env['message_id']] = env
        return 'persisted'
    assert len(make(api2, store=crash).poll(0, accept)) == 1
    assert crash.get('offset') is None
    assert make(api2, store=crash).poll(0, accept) == []  # redelivered, recognised as the same message
    assert seen == ['tg:5:' + USER, 'tg:5:' + USER]
    assert crash.get('offset') == '41'


def test_getupdates_params_timeout_50_http_65_allowed_updates():
    api = FakeBotAPI()
    make(api).poll(50)
    call = api.named('getUpdates')[-1]
    assert int(call['params']['timeout']) == 50
    assert call['timeout'] == 65
    assert as_list(call['params']['allowed_updates']) == ['message', 'callback_query']


def test_401_409_429_network_set_last_error_without_token_409_marked():
    cases = [
        ('auth', (401, {'ok': False, 'error_code': 401, 'description': 'Unauthorized'}), 'needs_login'),
        ('conflict', (409, {'ok': False, 'error_code': 409,
                            'description': 'Conflict: terminated by other getUpdates request'}), 'conflict'),
        ('rate', (429, {'ok': False, 'error_code': 429, 'description': 'Too Many Requests: retry after 7',
                        'parameters': {'retry_after': 7}}), 'rate_limited'),
        ('network', OSError('urlopen error for ' + API_BASE + '/bot' + TOKEN + '/getUpdates: timed out'),
         'reconnecting'),
    ]
    for name, answer, state in cases:
        api = FakeBotAPI()
        api.fail['getUpdates'] = [answer]
        audit = Audit()
        ch = make(api, audit=audit)
        assert ch.poll(0) == [], name
        assert 'channel.error' in audit.events(), name
        assert TOKEN not in ch.last_error and TOKEN not in json.dumps(audit, default=str), name
        assert ch.health()['state'] == state, name
        if name == 'conflict':
            assert 'conflict' in ch.last_error.lower()


def test_callback_query_answered_first_then_action_envelope():
    api = FakeBotAPI([callback(50, 'cbq1', APPROVE, card_mid=900)])

    def accept(env):
        api.events.append(('accept', env['message_id']))
        return 'persisted'
    got = make(api).poll(0, accept)
    assert len(got) == 1
    env = got[0]
    assert env['message_id'] == 'tg:cb:cbq1'
    assert env['text'] == env['action']['command'] == APPROVE
    assert '900' in str(env['action']['card_message_id'])
    order = [(e[0], e[1]) for e in api.events if e[0] == 'accept' or e[1] == 'answerCallbackQuery']
    assert order == [('accept', 'tg:cb:cbq1'), ('api', 'answerCallbackQuery')]  # answered after the receipt
    assert api.named('answerCallbackQuery')[0]['params']['callback_query_id'] == 'cbq1'

    # not received yet (the accept could not persist it): no answer may claim it was received
    api2 = FakeBotAPI([callback(51, 'cbq2', APPROVE)])

    def failing(env):
        raise channels.Retryable('inbox stream unavailable')
    assert make(api2).poll(0, failing) == []
    assert all(not c['params'].get('text') for c in api2.named('answerCallbackQuery'))


def test_callback_from_outsider_answered_and_envelope_sender_is_outsider():
    api = FakeBotAPI([callback(52, 'cbq-out', APPROVE, sender=OUTSIDER, chat=OUTSIDER)])
    audit = Audit()
    assert make(api, audit=audit).poll(0) == []
    answers = api.named('answerCallbackQuery')
    assert len(answers) == 1 and answers[0]['params']['callback_query_id'] == 'cbq-out'
    assert not answers[0]['params'].get('text')  # a refusal is an empty answer, never "received"
    drops = [f for e, f in audit if e == 'channel.drop_policy']
    assert len(drops) == 1 and drops[0]['sender'] == OUTSIDER


def test_document_and_largest_photo_downloaded_into_inbox_dir(tmp_path):
    inbox = tmp_path / 'inbox' / 'files'
    doc = message(60, 60, None, caption='see attached', document={
        'file_id': 'D1', 'file_unique_id': 'uD1', 'file_name': 'report v1.pdf', 'file_size': 11})
    photo = message(61, 61, None, photo=[
        {'file_id': 'P_small', 'file_unique_id': 'us', 'width': 90, 'height': 90, 'file_size': 3},
        {'file_id': 'P_big', 'file_unique_id': 'ub', 'width': 800, 'height': 800, 'file_size': 6}])
    api = FakeBotAPI([doc, photo], files={'D1': ('documents/file_1.pdf', b'hello world'),
                                          'P_small': ('photos/s.jpg', b'abc'), 'P_big': ('photos/b.jpg', b'BIGPIC')})
    got = make(api, inbox=inbox).poll(0)
    assert len(got) == 2
    f0, f1 = got[0]['files'][0], got[1]['files'][0]
    for env, f, mid, data in ((got[0], f0, 60, b'hello world'), (got[1], f1, 61, b'BIGPIC')):
        p = Path(f['path'])
        assert p.name.startswith('tg-' + str(mid) + '-') and p.resolve().parent == inbox.resolve()
        assert p.read_bytes() == data and f['size'] == len(data)
        assert not env.get('rejected_files')
    assert Path(f0['path']).name == 'tg-60-report_v1.pdf'
    assert [c['params']['file_id'] for c in api.named('getFile')] == ['D1', 'P_big']


def test_oversize_or_unknown_media_goes_to_rejected_files(tmp_path):
    big = message(70, 70, None, caption='big one', document={
        'file_id': 'BIG', 'file_unique_id': 'uB', 'file_name': 'huge.bin', 'file_size': 25 * 1024 * 1024})
    sticker = message(71, 71, None, sticker={'file_id': 'S1', 'file_unique_id': 'uS', 'width': 512,
                                             'height': 512, 'is_animated': False, 'is_video': False, 'type': 'regular'})
    api = FakeBotAPI([big, sticker])
    got = make(api, inbox=tmp_path / 'inbox').poll(0)
    assert len(got) == 2
    for env in got:
        assert len(env['rejected_files']) == 1 and not env.get('files')
    assert got[0]['text'] == '' and got[0]['provenance'] == [{'kind': 'caption', 'text': 'big one'}]
    assert got[1]['text'] == '[贴纸]' and got[1].get('provenance', []) == []
    assert api.named('getFile') == []


def test_offset_persists_via_redis_store(private_redis_v2):  # noqa: F811
    """The actor's production store (P1d ``actor.RedisStore``) keeps the cursor across adapter instances.
    Skipped only by the fixture itself when redis-server is unavailable (brief §6.9 exception)."""
    from cmux_fleet.channels import actor
    r = private_redis_v2
    api = FakeBotAPI([message(80, 8, 'before')])
    first = make(api, store=actor.RedisStore(r, 'telegram', ACCOUNT))
    assert [e['text'] for e in first.poll(0)] == ['before']
    assert r.get(channels.STORE + 'telegram:' + ACCOUNT + ':offset') == '81'
    api.updates.append(message(81, 9, 'after'))
    second = make(api, store=actor.RedisStore(r, 'telegram', ACCOUNT))
    assert [e['text'] for e in second.poll(0)] == ['after']
    assert int(api.named('getUpdates')[-1]['params']['offset']) == 81
    assert r.get(channels.STORE + 'telegram:' + ACCOUNT + ':offset') == '82'


# ====================================================================== P2b send
def test_send_text_first_chunk_replies_no_parse_mode():
    api = FakeBotAPI()
    text = ('a' * 3000 + '\n') * 3
    got = make(api).send_text(USER, text, reply_ref={'chat_id': USER, 'message_id': '77'})
    assert got.ok, got.error
    sends = api.named('sendMessage')
    assert len(sends) == 3 and ''.join(c['params']['text'] for c in sends) == text
    first = as_list(sends[0]['params']['reply_parameters'])
    assert str(first['message_id']) == '77'
    assert all('reply_parameters' not in c['params'] for c in sends[1:])
    assert all('parse_mode' not in c['params'] for c in sends)
    assert all(str(c['params']['chat_id']) == USER for c in sends)


def test_chunk_over_4000_utf16_units_refused_in_hook():
    api = FakeBotAPI()
    got = make(api)._send_text(USER, '😀' * 4000, None)  # 8000 UTF-16 units handed straight to the hook
    assert isinstance(got, base.SendResult) and not got.ok
    assert 'utf-16' in got.error.lower().replace('utf16', 'utf-16')
    assert api.named('sendMessage') == []


def test_429_retry_after_respected_with_injected_sleep():
    def too_many(seconds):
        return (429, {'ok': False, 'error_code': 429, 'description': 'Too Many Requests: retry after ' + str(seconds),
                      'parameters': {'retry_after': seconds}})
    api, sleeps = FakeBotAPI(), []
    api.fail['sendMessage'] = [too_many(3)]
    got = make(api, sleep=sleeps.append).send_text(USER, 'hi')
    assert got.ok, got.error
    assert sleeps == [3] and len(api.named('sendMessage')) == 2

    api2, sleeps2 = FakeBotAPI(), []
    api2.fail['sendMessage'] = [too_many(31)]
    got2 = make(api2, sleep=sleeps2.append).send_text(USER, 'hi')
    assert not got2.ok and got2.error
    assert 31 not in sleeps2 and len(api2.named('sendMessage')) == 1


def test_send_card_inline_keyboard_callback_data_is_command():
    api = FakeBotAPI()
    c = envelope.card('删除确认', 'rm -rf build/', [{'label': '批准', 'command': APPROVE},
                                                {'label': '拒绝', 'command': REJECT}], ref='p-0123abcd')
    got = make(api).send_card(USER, c)
    assert got.ok, got.error
    sends = api.named('sendMessage')
    assert len(sends) == 1 and '删除确认' in sends[0]['params']['text']
    keyboard = as_list(sends[0]['params']['reply_markup'])['inline_keyboard']
    assert [b['callback_data'] for row in keyboard for b in row] == [APPROVE, REJECT]
    assert [b['text'] for row in keyboard for b in row] == ['批准', '拒绝']


def test_send_card_button_over_64_bytes_degrades_to_card_text():
    """TG-C2: a button whose command exceeds Telegram's 64-byte callback_data turns the whole card into its text
    form (every command spelled out), never truncated. Goes through the public send_card (no bypass); needs the
    P0b change that stops the shared card validation rejecting long commands up front."""
    long_command = '/approve ' + 'a' * 56
    assert len(long_command.encode()) == 65
    c = {'title': '删除确认', 'text': 'rm -rf build/', 'ref': '',
         'buttons': [{'label': '批准', 'command': long_command}, {'label': '拒绝', 'command': REJECT}]}
    api = FakeBotAPI()
    got = make(api).send_card(USER, c)
    assert got.ok, got.error
    sends = api.named('sendMessage')
    assert len(sends) == 1 and 'reply_markup' not in sends[0]['params']
    assert sends[0]['params']['text'] == envelope.card_text(c)
    assert long_command in sends[0]['params']['text'] and REJECT in sends[0]['params']['text']


def test_update_card_edit_message_text_and_not_modified_is_ok():
    api = FakeBotAPI()
    ch = make(api)
    c = envelope.card('删除确认', 'rm -rf build/', [{'label': '拒绝', 'command': REJECT}], ref='p-0123abcd')
    sent = ch.send_card(USER, c)
    assert sent.ok and sent.message_id
    sent_mid = str(api.next_message_id)  # the platform message id FakeBotAPI gave the card
    done = envelope.card('删除确认', '已答：拒绝', [], ref='p-0123abcd')
    assert ch.update_card(USER, sent.message_id, done).ok
    edit = api.named('editMessageText')[-1]['params']
    assert str(edit['chat_id']) == USER and str(edit['message_id']) == sent_mid and '已答' in edit['text']
    api.fail['editMessageText'] = [(400, {'ok': False, 'error_code': 400, 'description':
                                          'Bad Request: message is not modified: specified new message content and '
                                          'reply markup are exactly the same'})]
    assert ch.update_card(USER, sent.message_id, done).ok


def test_send_file_multipart_filename_chat_id_caption_1024(tmp_path):
    f = tmp_path / 'result.txt'
    f.write_bytes(b'long result\n' * 50)
    api = FakeBotAPI()
    got = make(api).send_file(USER, f, caption='c' * 2000)
    assert got.ok, got.error
    calls = api.named('sendDocument')
    assert len(calls) == 1 and calls[0]['method'] == 'POST'
    assert calls[0]['headers']['content-type'].startswith('multipart/form-data')
    parts = multipart(calls[0])
    assert parts['chat_id'][1].decode() == USER
    assert parts['document'] == ('result.txt', b'long result\n' * 50)
    assert len(parts['caption'][1].decode()) == 1024


def test_send_file_over_50mb_fails_without_raise(tmp_path):
    f = tmp_path / 'huge.bin'
    with open(f, 'wb') as fh:
        fh.truncate(50 * 1024 * 1024 + 1)  # sparse: nothing is really written
    api = FakeBotAPI()
    got = make(api).send_file(USER, f)
    assert isinstance(got, base.SendResult) and not got.ok and got.error
    assert api.named('sendDocument') == []


class FakeResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    def read(self, *args):
        return self.payload

    def getcode(self):
        return 200

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_proxy_env_used_PROXY_overrides_socks_rejected(monkeypatch):
    opened = []

    def fake_open(self, req, data=None, timeout=None):
        proxies = [h.proxies for h in self.handlers if isinstance(h, urllib.request.ProxyHandler)]
        opened.append(proxies[-1] if proxies else {})
        return FakeResponse(json.dumps({'ok': True, 'result': {'message_id': 1, 'date': 1762650000,
                                                                'chat': {'id': int(USER), 'type': 'private'}}}).encode())

    def no_network(*args, **kwargs):
        raise AssertionError('a test must never open a real connection')
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', fake_open)
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    monkeypatch.setenv('HTTPS_PROXY', 'http://env-proxy.invalid:3128')
    monkeypatch.delenv('NO_PROXY', raising=False)
    monkeypatch.delenv('no_proxy', raising=False)

    got = make(None).send_text(USER, 'hi')  # transport None = the adapter's real default (opener patched above)
    assert got.ok, got.error
    assert opened[-1].get('https') == 'http://env-proxy.invalid:3128'

    got = make(None, values={'PROXY': 'http://acct-proxy.invalid:8080'}).send_text(USER, 'hi')
    assert got.ok, got.error
    assert opened[-1].get('https') == 'http://acct-proxy.invalid:8080'

    before = len(opened)
    got = make(None, values={'PROXY': 'socks5://socks-proxy.invalid:1080'}).send_text(USER, 'hi')
    assert not got.ok and '需 http 代理' in got.error
    assert len(opened) == before


def test_token_crlf_stripped_and_masked_in_last_error():
    acct = channels.parse_account_text('telegram', ACCOUNT, 'ALLOW=' + USER + '\r\nBOT_TOKEN=' + TOKEN
                                       + '\r\nAPI_BASE=' + API_BASE + '\r\n')
    api = FakeBotAPI()
    ch = make(api, account=acct)
    assert ch.send_text(USER, 'hi').ok
    url = api.named('sendMessage')[-1]['url']
    assert '/bot' + TOKEN + '/' in url and '\r' not in url and '\n' not in url
    api.fail['sendMessage'] = [OSError('connection reset by peer for ' + API_BASE + '/bot' + TOKEN + '/sendMessage')]
    got = ch.send_text(USER, 'hi')
    assert not got.ok
    assert TOKEN not in got.error and TOKEN not in ch.last_error
    assert '***' in got.error and '***' in ch.last_error


def test_check_getme_and_webhook_conflict_and_LOGIN_validates_token():
    api = FakeBotAPI()
    api.webhook_url = 'https://hook.invalid/telegram'
    ch = make(api)
    got = ch.check()
    assert isinstance(got, base.SendResult) and not got.ok and '409' in got.error
    assert api.named('getMe') and api.named('getWebhookInfo')
    assert api.named('deleteWebhook') == []  # reported only, never changed
    api.webhook_url = ''
    assert ch.check().ok

    from cmux_fleet.channels import telegram
    login_api = FakeBotAPI()
    io = FakeIO([TOKEN + '\n'])
    assert telegram.LOGIN(ACCOUNT, io, login_api) == {'BOT_TOKEN': TOKEN}
    assert any('/bot' + TOKEN + '/getMe' in c['url'] for c in login_api.calls)
    assert all(TOKEN not in line for line in io.printed)


def test_proxy_no_proxy_and_all_proxy_env_do_not_refuse(monkeypatch):
    """NO_PROXY (a host list) and ALL_PROXY (here socks) in the environment must not make the default transport
    refuse: urllib only uses the proxy of the request's scheme (M76 regression of cc77f8f)."""
    opened = []

    def fake_open(self, req, data=None, timeout=None):
        proxies = [h.proxies for h in self.handlers if isinstance(h, urllib.request.ProxyHandler)]
        opened.append(proxies[-1] if proxies else {})
        return FakeResponse(json.dumps({'ok': True, 'result': {'message_id': 1, 'date': 1762650000,
                                                                'chat': {'id': int(USER), 'type': 'private'}}}).encode())

    def no_network(*args, **kwargs):
        raise AssertionError('a test must never open a real connection')
    monkeypatch.setattr(urllib.request.OpenerDirector, 'open', fake_open)
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    for name in ('http_proxy', 'https_proxy', 'no_proxy', 'all_proxy'):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv('HTTP_PROXY', 'http://env-proxy.invalid:3128')
    monkeypatch.setenv('HTTPS_PROXY', 'http://env-proxy.invalid:3128')
    monkeypatch.setenv('NO_PROXY', 'localhost')
    monkeypatch.setenv('ALL_PROXY', 'socks5://x.invalid:1')

    got = make(None).send_text(USER, 'hi')  # transport None = the adapter's real default (opener patched above)
    assert got.ok, got.error
    assert opened and opened[-1].get('https') == 'http://env-proxy.invalid:3128'
