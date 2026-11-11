"""Slack adapter protocol tests (packages P9b send / P9c receive; brief v2.3 §9.3, contracts §8 "Slack 专项").

Account file keys (contracts §8, P9a): ``SLACK_BOT_TOKEN`` (``xoxb-``, Web API Bearer), ``SLACK_APP_TOKEN`` (``xapp-``,
only for ``apps.connections.open``), ``SLACK_ALLOW_USERS`` (the whitelist: a synonym of ``ALLOW`` that
``parse_account_text`` puts into ``Account.allow``; empty = reject everyone). All values here are fakes
(``xoxb-FAKE`` / ``xapp-FAKE``, ``*.invalid`` hosts).

Fakes only, nothing connects, nothing listens, no thread is started and nothing really sleeps:

* ``FakeSlackAPI``: an in-process callable with the Slack adapter's transport signature (ruling 2:
  ``(method, url, *, headers, body, timeout) -> (status, body bytes, headers)``; the adapter also accepts a 2-tuple).
  It records every request and answers from a script; ``fail[name]`` overrides answers in order.
* ``FakeWS`` / ``FakeWSFactory``: the injected ``ws_factory`` (ruling 3): ``ws_factory(url)`` -> an object with
  ``recv()`` / ``send(str)`` / ``close()``. ``recv()`` returns the scripted frames as JSON, then raises
  ``ConnectionError`` (the connection closed). ``send`` records the acks.

The adapter is driven synchronously through two methods P9c provides (survey §1.1): ``_socket_session(ws)`` (one
connection: ack every frame that has an ``envelope_id`` first, then handle ``hello`` / ``disconnect`` / queue the frame;
returns on ``disconnect`` or when ``recv`` raises) and ``_socket_loop()`` (``apps.connections.open`` -> ``ws_factory``
-> ``_socket_session`` -> injected ``sleep`` backoff, until ``close()``); then ``poll(0)`` drains the queued frames
through ``parse_inbound`` and the base class policy.

The adapter class is looked up inside each test body (never in a fixture), so a missing or incomplete adapter makes a
test FAIL, not ERROR. ``CONTRACT`` feeds the shared adapter rows in tests/test_channels_contract.py (``red:P9-slack``).
"""
import importlib
import json
import sys
from urllib.parse import parse_qsl, urlsplit

import pytest

from fleetmux import channels
from fleetmux.channels import base, envelope

TOKEN = 'xoxb-FAKE'
APP_TOKEN = 'xapp-FAKE'
USER = 'U0FAKEOWNER'      # the whitelisted user
OUTSIDER = 'U0FAKEOTHER'
DM = 'D0FAKEDM'           # the owner's DM channel (what FakeSlackAPI's conversations.open returns)
OTHER_DM = 'D0FAKEDM2'
CHAN = 'C0FAKECHAN'       # a public channel
ACCOUNT = 'bot1'
APPROVE = '/approve p-0123abcd'
NONCE = '0123456789abcdef'


# ====================================================================== fakes
class FakeSlackAPI:
    """Slack Web API stand-in. Method name = last URL path segment (``chat.postMessage`` ...); the pre-signed upload
    URL of files.getUploadURLExternal is called ``upload``. ``fail[name]``: list of scripted answers consumed in
    order, each ``(status, payload dict, headers dict)`` or an exception instance to raise. ``events``: an ordered log
    the tests may share with FakeWS."""

    def __init__(self, events=None):
        self.calls = []
        self.fail = {}
        self.events = events if events is not None else []
        self.next_ts = 0
        self.next_url = 0

    def __call__(self, method, url, *, headers=None, body=None, timeout=30):
        parts = urlsplit(url)
        headers = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
        params = dict(parse_qsl(parts.query))
        ctype = headers.get('content-type', '')
        raw = body or b''
        if isinstance(raw, str):
            raw = raw.encode()
        name = 'upload' if '/upload/' in parts.path else parts.path.rsplit('/', 1)[-1]
        if name != 'upload' and raw:
            if 'json' in ctype:
                params.update(json.loads(raw))
            elif 'x-www-form-urlencoded' in ctype:
                params.update(dict(parse_qsl(raw.decode())))
        call = {'name': name, 'method': method, 'url': url, 'params': params, 'headers': headers, 'body': raw,
                'timeout': timeout}
        self.calls.append(call)
        self.events.append(('api', name))
        script = self.fail.get(name)
        if script:
            answer = script.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            status, payload, rheaders = answer
            return status, json.dumps(payload).encode(), dict(rheaders)
        return self.answer(name, params)

    @staticmethod
    def ok(**payload):
        return 200, json.dumps(dict({'ok': True}, **payload)).encode(), {'content-type': 'application/json'}

    def answer(self, name, params):
        if name == 'auth.test':
            return self.ok(team='T0FAKE', user='test', user_id='U0FAKEBOT', bot_id='B0FAKE')
        if name == 'apps.connections.open':
            self.next_url += 1
            return self.ok(url='wss://socket.invalid/link/?n=' + str(self.next_url))
        if name == 'conversations.open':
            return self.ok(channel={'id': DM})
        if name in ('chat.postMessage', 'chat.update'):
            self.next_ts += 1
            ts = params.get('ts') if name == 'chat.update' else '1762650000.%06d' % (100 + self.next_ts)
            return self.ok(channel=params.get('channel', DM), ts=ts)
        if name == 'files.getUploadURLExternal':
            return self.ok(upload_url='https://files.invalid/upload/F0FAKE', file_id='F0FAKE')
        if name == 'upload':
            return 200, b'OK', {}
        if name == 'files.completeUploadExternal':
            return self.ok(files=[{'id': 'F0FAKE', 'title': 'r.txt'}])
        return self.ok()

    def named(self, name):
        return [c for c in self.calls if c['name'] == name]

    def names(self):
        return [c['name'] for c in self.calls]


class FakeWS:
    def __init__(self, frames, events=None):
        self.frames = list(frames)
        self.sent = []
        self.closed = False
        self.events = events if events is not None else []

    def recv(self):
        if not self.frames:
            raise ConnectionError('fake socket closed')
        return json.dumps(self.frames.pop(0))

    def send(self, text):
        msg = json.loads(text)
        self.sent.append(msg)
        self.events.append(('ack', msg.get('envelope_id')))

    def close(self):
        self.closed = True

    def acked(self):
        return [m.get('envelope_id') for m in self.sent]


class FakeWSFactory:
    """``ws_factory(url)``: hands out one FakeWS per connection from ``sessions`` (a list of frame lists)."""

    def __init__(self, sessions, events=None):
        self.sessions = [list(s) for s in sessions]
        self.urls = []
        self.sockets = []
        self.events = events if events is not None else []

    def __call__(self, url, **kwargs):
        self.urls.append(url)
        if not self.sessions:
            raise ConnectionError('fake: no more sessions')
        ws = FakeWS(self.sessions.pop(0), self.events)
        self.sockets.append(ws)
        return ws


class Audit(list):
    def __call__(self, event, **fields):
        self.append((event, fields))

    def events(self):
        return [e for e, _ in self]


# ====================================================================== frames
def hello(n=1):
    return {'type': 'hello', 'num_connections': n, 'debug_info': {'host': 'fake'}, 'connection_info': {'app_id': 'A0'}}


def disconnect(reason='refresh_requested'):
    return {'type': 'disconnect', 'reason': reason, 'debug_info': {'host': 'fake'}}


def im(ts, text='hi', *, user=USER, channel=DM, channel_type='im', event_id=None, envelope_id=None, retry_attempt=0,
       **extra):
    event = {'type': 'message', 'channel_type': channel_type, 'user': user, 'channel': channel, 'ts': ts}
    if text is not None:
        event['text'] = text
    event.update(extra)
    return {'type': 'events_api', 'envelope_id': envelope_id or 'env-' + ts, 'accepts_response_payload': False,
            'retry_attempt': retry_attempt, 'retry_reason': 'timeout' if retry_attempt else '',
            'payload': {'type': 'event_callback', 'team_id': 'T0FAKE',
                        'event_id': event_id or 'Ev' + ts.replace('.', ''), 'event_time': int(float(ts)),
                        'event': event}}


def press(command_word, value, *, user=USER, channel=DM, msg_ts='1762650000.000900', action_ts='1762650001.000001',
          envelope_id=None):
    action = {'type': 'button', 'action_id': command_word, 'block_id': 'b1', 'action_ts': action_ts,
              'text': {'type': 'plain_text', 'text': 'ok'}}
    if value is not None:
        action['value'] = value
    return {'type': 'interactive', 'envelope_id': envelope_id or 'env-act-' + action_ts,
            'accepts_response_payload': False,
            'payload': {'type': 'block_actions', 'user': {'id': user}, 'channel': {'id': channel},
                        'message': {'ts': msg_ts}, 'container': {'type': 'message', 'message_ts': msg_ts,
                                                                 'channel_id': channel},
                        'trigger_id': 'trig-' + action_ts, 'actions': [action]}}


# ====================================================================== helpers
def adapter():
    return channels.adapter_class('slack')


def make(api=None, *, account=None, audit=None, store=None, allow=(USER,), **kw):
    """kw: the Slack-only keywords ws_factory= / sleep= (ruling 3)."""
    acct = account or channels.Account('slack', ACCOUNT, allow, {'SLACK_BOT_TOKEN': TOKEN, 'SLACK_APP_TOKEN': APP_TOKEN})
    return adapter()(acct, transport=api if api is not None else FakeSlackAPI(),
                     store=store if store is not None else base.MemoryStore(),
                     audit=audit if audit is not None else Audit(), **kw)


def as_list(value):
    return json.loads(value) if isinstance(value, str) else value


def blocks_of(call):
    return as_list(call['params'].get('blocks')) or []


def buttons_of(call):
    return [e for b in blocks_of(call) if b.get('type') == 'actions' for e in b.get('elements', [])]


def unfurl_off(call):
    p = call['params']
    return p.get('unfurl_links') in (False, 'false') and p.get('unfurl_media') in (False, 'false')


def contract_transport():
    return FakeSlackAPI()


CONTRACT = {
    # SLACK_ALLOW_USERS is deliberately absent: the contract rows build the account through parse_account_text and
    # need ALLOW (row declares_the_interface); the synonym is pinned by test_outsider_dm_is_dropped_by_allowlist.
    'account': {'ALLOW': USER, 'SLACK_BOT_TOKEN': TOKEN, 'SLACK_APP_TOKEN': APP_TOKEN},
    'conversation': USER,   # a user id: the contract sends exercise conversations.open (proactive push)
    'transport': contract_transport,
    'inbound_ok': im('1762650000.000100', 'hello contract'),
    'inbound_text': 'hello contract',
    'inbound_foreign': im('1762650000.000200', 'hello contract', user=OUTSIDER, channel=OTHER_DM),
    'inbound_group': im('1762650000.000300', 'hello contract', channel=CHAN, channel_type='channel'),
    # Slack authenticates every sender: no 'inbound_unverified'.
    'inbound_button': press('approve', 'p-0123abcd', action_ts='1762650001.000001'),
    'inbound_button_text': APPROVE,
    'inbound_button_foreign': press('approve', 'p-0123abcd', user=OUTSIDER, channel=OTHER_DM,
                                    action_ts='1762650001.000002'),
    'inbound_button_group': press('approve', 'p-0123abcd', channel=CHAN, action_ts='1762650001.000003'),
}


# ====================================================================== P9b send
def test_contract_dict_and_adapter_class_registered():
    required = {'account', 'conversation', 'transport', 'inbound_ok', 'inbound_text', 'inbound_foreign',
                'inbound_group', 'inbound_button', 'inbound_button_text', 'inbound_button_foreign',
                'inbound_button_group'}
    assert required <= set(CONTRACT) and callable(CONTRACT['transport'])
    cls = adapter()
    assert issubclass(cls, base.Channel) and cls.type == 'slack'
    assert cls.TEXT_LIMIT == 3000 and cls.TEXT_UNITS == 'chars' and cls.OVERFLOW == 'split'
    assert cls.RECEIVES and cls.SUPPORTS_CARDS and cls.SUPPORTS_BUTTONS and cls.SUPPORTS_UPDATE and cls.SUPPORTS_FILES
    assert cls.OPTIONAL_DEPS == ('websocket',)
    assert channels.prefix('slack') == 'sk'


def test_send_text_posts_to_inbound_channel_with_unfurl_off():
    api = FakeSlackAPI()
    got = make(api).send_text(DM, 'hi https://example.invalid/x', reply_ref={'channel': DM})
    assert got.ok, got.error
    posts = api.named('chat.postMessage')
    assert len(posts) == 1 and api.named('conversations.open') == []
    p = posts[0]['params']
    assert p['channel'] == DM and p['text'] == 'hi https://example.invalid/x'
    assert p['unfurl_links'] is False and p['unfurl_media'] is False
    assert posts[0]['headers']['authorization'] == 'Bearer ' + TOKEN
    assert got.message_id == '1762650000.000101'  # the ts FakeSlackAPI answered for this post


def test_send_text_splits_at_3000_and_threads_follow_ups():
    api = FakeSlackAPI()
    ch = make(api)
    text = 'x' * 7000
    got = ch.send_text(DM, text, reply_ref={'channel': DM})
    assert got.ok, got.error
    posts = api.named('chat.postMessage')
    assert [len(c['params']['text']) for c in posts] == [3000, 3000, 1000]
    assert ''.join(c['params']['text'] for c in posts) == text
    assert 'thread_ts' not in posts[0]['params']
    first_ts = got.message_id
    assert first_ts and all(c['params'].get('thread_ts') == first_ts for c in posts[1:])
    assert all(unfurl_off(c) for c in posts)
    assert ch.send_text(DM, 'short', reply_ref={'channel': DM}).ok  # ruling 8: thread state is reset per send
    assert 'thread_ts' not in api.named('chat.postMessage')[-1]['params']


def test_proactive_push_opens_dm_first():
    api = FakeSlackAPI()
    ch = make(api)
    assert ch.send_text(USER, 'push').ok
    assert api.names() == ['conversations.open', 'chat.postMessage']
    assert api.named('conversations.open')[0]['params']['users'] == USER
    assert api.named('chat.postMessage')[0]['params']['channel'] == DM
    assert ch.send_text(USER, 'x' * 3500).ok
    assert len(api.named('conversations.open')) == 1  # the U -> D mapping is cached in the store
    # ruling 4: a push target must be the whitelisted user's own private chat
    assert ch.private_target_ok(USER) is True
    assert ch.private_target_ok(OUTSIDER) is False
    assert ch.private_target_ok('D0FAKEDMX') is False  # a DM channel nobody whitelisted has written from


def test_send_card_buttons_carry_command_and_pending_id():
    api = FakeSlackAPI()
    ch = make(api)

    def posts_for(card):
        before = len(api.named('chat.postMessage'))
        got = ch.send_card(DM, card, reply_ref={'channel': DM})
        assert got.ok, got.error
        return api.named('chat.postMessage')[before:]

    # card 1: native Block Kit card
    c1 = envelope.card('删除确认', 'rm -rf build/', [{'label': '批准', 'command': '/approve p-0123abcd ' + NONCE},
                                                 {'label': '拒绝', 'command': '/reject p-0123abcd'}], ref='p-0123abcd')
    (p1,) = posts_for(c1)
    assert any(b.get('type') == 'section' and b['text']['type'] == 'plain_text' for b in blocks_of(p1))
    assert [(b['action_id'], b.get('value')) for b in buttons_of(p1)] == [
        ('approve', 'p-0123abcd ' + NONCE), ('reject', 'p-0123abcd')]
    assert unfurl_off(p1) and p1['params'].get('text')

    # card 2 (ruling 5): a button value over 2000 bytes -> the whole card as typed commands, never truncated
    big = '/approve ' + 'a' * 2001
    c2 = envelope.card('删除确认', 'rm x', [{'label': '大', 'command': big}, {'label': '拒绝', 'command': '/reject p-0123abcd'}])
    p2 = posts_for(c2)
    sent2 = ''.join(c['params']['text'] for c in p2)
    assert p2 and all(not blocks_of(c) for c in p2) and all(unfurl_off(c) for c in p2)
    assert big in sent2 and '/reject p-0123abcd' in sent2

    # card 3 (ruling 5): section text over 2900 chars -> typed commands too, body not truncated
    body = 'x' * 2901
    c3 = envelope.card('删除确认', body, c1['buttons'])
    p3 = posts_for(c3)
    sent3 = ''.join(c['params']['text'] for c in p3)
    assert p3 and all(not blocks_of(c) for c in p3) and all(unfurl_off(c) for c in p3)
    assert body in sent3 and '/approve p-0123abcd ' + NONCE in sent3 and '/reject p-0123abcd' in sent3

    # card 4: just inside both limits -> still a native card
    c4 = envelope.card('删除确认', 'x' * 2890, [{'label': '大', 'command': '/approve ' + 'a' * 2000}])
    (p4,) = posts_for(c4)
    assert buttons_of(p4) and buttons_of(p4)[0]['value'] == 'a' * 2000

    # card 5 (ruling 6): two buttons with the same command name -> unique action_ids, the nonce stays in value
    c5 = envelope.card('删除确认', 'two', [{'label': 'A', 'command': '/approve p-0123abcd ' + NONCE},
                                         {'label': 'B', 'command': '/approve p-89abcdef ' + NONCE}])
    (p5,) = posts_for(c5)
    assert [(b['action_id'], b['value']) for b in buttons_of(p5)] == [
        ('approve', 'p-0123abcd ' + NONCE), ('approve.2', 'p-89abcdef ' + NONCE)]


def test_update_card_uses_chat_update():
    api = FakeSlackAPI()
    ch = make(api)
    c = envelope.card('删除确认', 'rm x', [{'label': '批准', 'command': APPROVE}])
    c2 = envelope.card('删除确认', '已答', [{'label': '拒绝', 'command': '/reject p-0123abcd'}])
    mid = ch.send_card(DM, c, reply_ref={'channel': DM}).message_id
    assert mid
    got = ch.update_card(DM, mid, c2)
    assert got.ok, got.error
    (upd,) = api.named('chat.update')
    assert upd['params']['channel'] == DM and upd['params']['ts'] == mid
    assert [b.get('value') for b in buttons_of(upd)] == ['p-0123abcd'] and buttons_of(upd)[0]['action_id'] == 'reject'
    assert unfurl_off(upd)  # contracts §8: chat.update carries the unfurl flags too
    assert len(api.named('chat.postMessage')) == 1


def test_send_file_uses_external_upload_two_steps(tmp_path):
    f = tmp_path / 'r.txt'
    f.write_bytes(b'z' * 600)
    api = FakeSlackAPI()
    got = make(api).send_file(DM, f, caption='结果', reply_ref={'channel': DM})
    assert got.ok, got.error
    assert [n for n in api.names() if n != 'conversations.open'] == [
        'files.getUploadURLExternal', 'upload', 'files.completeUploadExternal']
    first = api.named('files.getUploadURLExternal')[0]['params']
    assert first['filename'] == 'r.txt' and str(first['length']) == '600'
    up = api.named('upload')[0]
    assert up['url'] == 'https://files.invalid/upload/F0FAKE' and up['body'] == b'z' * 600
    assert 'authorization' not in up['headers']  # a pre-signed URL: the bot token is not sent there
    done = api.named('files.completeUploadExternal')[0]['params']
    assert done['channel_id'] == DM and done['initial_comment'] == '结果'
    assert [x['id'] for x in as_list(done['files'])] == ['F0FAKE']


def test_429_retry_after_respected_with_injected_sleep():
    def too_many(seconds):
        return (429, {'ok': False, 'error': 'ratelimited'}, {'Retry-After': str(seconds)})
    api, sleeps = FakeSlackAPI(), []
    api.fail['chat.postMessage'] = [too_many(3)]
    got = make(api, sleep=sleeps.append).send_text(DM, 'hi', reply_ref={'channel': DM})
    assert got.ok, got.error
    assert sleeps == [3] and len(api.named('chat.postMessage')) == 2

    api2, sleeps2 = FakeSlackAPI(), []
    api2.fail['chat.postMessage'] = [too_many(31)]
    got2 = make(api2, sleep=sleeps2.append).send_text(DM, 'hi', reply_ref={'channel': DM})
    assert not got2.ok and 'rate' in (got2.error or '').lower()
    assert 31 not in sleeps2 and len(api2.named('chat.postMessage')) == 1

    # ruling 2: a transport that answers a 2-tuple (no headers) works as well
    api3 = FakeSlackAPI()

    def two_tuple(method, url, **kw):
        status, body, _ = api3(method, url, **kw)
        return status, body
    assert make(two_tuple).send_text(DM, 'hi', reply_ref={'channel': DM}).ok


def test_errors_never_carry_tokens():
    cases = [
        ('not_authed', (401, {'ok': False, 'error': 'not_authed'}, {}), True),
        ('invalid_auth', (200, {'ok': False, 'error': 'invalid_auth'}, {}), True),
        ('network', OSError('POST https://slack.invalid/api/chat.postMessage Authorization: Bearer ' + TOKEN
                            + ' failed'), False),
        ('ws', OSError('wss://socket.invalid/link/?token=' + APP_TOKEN + ' closed'), False),
    ]
    for name, answer, auth in cases:
        api, audit = FakeSlackAPI(), Audit()
        api.fail['chat.postMessage'] = [answer]
        api.fail['auth.test'] = [answer]
        ch = make(api, audit=audit)
        got = ch.send_text(DM, 'hello', reply_ref={'channel': DM})
        checked = ch.check()
        assert not got.ok and got.error, name
        assert isinstance(checked, base.SendResult) and not checked.ok, name
        text = json.dumps([got.error, checked.error, ch.last_error, audit, ch.health()], ensure_ascii=False,
                          default=str)
        assert 'xoxb-' not in text and 'xapp-' not in text and TOKEN not in text and APP_TOKEN not in text, name
        assert 'channel.error' in audit.events(), name
        if auth:
            assert ch.health()['state'] == 'needs_login', name


# ====================================================================== P9c receive
def test_hello_then_dm_becomes_envelope():
    audit = Audit()
    ch = make(FakeSlackAPI(), audit=audit, ws_factory=FakeWSFactory([]), sleep=lambda s: None)
    ws = FakeWS([hello(), im('1762650000.000100', 'hi', envelope_id='e1'),
                 im('1762650000.000200', 'in thread', envelope_id='e2', thread_ts='1762649999.000001')])
    ch._socket_session(ws)
    assert ws.acked() == ['e1', 'e2']
    got = ch.poll(0)
    assert len(got) == 2
    env, threaded = got
    assert envelope.problems(env) == [] and envelope.problems(threaded) == []
    assert (env['channel'], env['account'], env['message_id']) == ('slack', ACCOUNT, 'sk:' + DM + ':1762650000.000100')
    assert env['sender'] == USER and env['conversation'] == DM and env['text'] == 'hi'
    assert env['private'] is True and env['verified'] is True and env['at'] == 1762650000
    # §53「直连 worker」: replies go into the thread under the message; a reply typed in a thread is marked in_thread
    assert env['reply_ref'] == {'channel': DM, 'thread_ts': '1762650000.000100'}
    assert threaded['reply_ref'] == {'channel': DM, 'thread_ts': '1762649999.000001', 'in_thread': True}
    assert ch.health()['state'] == 'ok'
    assert ch.private_target_ok(DM) is True  # ruling 4: dmuser:<D> recorded for the whitelisted user

    # ruling 9: another consumer of the same app token -> conflict, but keep receiving
    ws2 = FakeWS([hello(2), im('1762650000.000300', 'still here', envelope_id='e3')])
    ch._socket_session(ws2)
    conflicts = [f for e, f in audit if e == 'channel.error' and f.get('kind') == 'conflict']
    assert conflicts and ch.health()['state'] == 'conflict'
    assert [e['text'] for e in ch.poll(0)] == ['still here']


def test_every_envelope_is_acked_before_processing():
    events, audit = [], Audit()
    ch = make(FakeSlackAPI(), audit=audit, ws_factory=FakeWSFactory([]), sleep=lambda s: None)
    real = ch.parse_inbound

    def logged(raw):
        eid = raw.get('envelope_id') if isinstance(raw, dict) else None
        events.append(('parse', eid))
        if eid == 'e1':
            raise ValueError('processing blew up')
        return real(raw)
    ch.parse_inbound = logged
    unknown = {'type': 'events_api', 'envelope_id': 'e2', 'payload': {'event_id': 'Ev2',
                                                                      'event': {'type': 'reaction_added'}}}
    ws = FakeWS([hello(), im('1762650000.000100', 'one', envelope_id='e1'), unknown,
                 im('1762650000.000300', 'three', envelope_id='e3')], events)
    ch._socket_session(ws)
    assert ws.acked() == ['e1', 'e2', 'e3']  # unknown event types are acked as well
    got = ch.poll(0)
    assert [e['text'] for e in got] == ['three']
    for eid in ('e1', 'e3'):
        assert events.index(('ack', eid)) < events.index(('parse', eid))
    assert 'channel.drop_invalid' in audit.events()


def test_retry_attempt_duplicate_is_dropped():
    store, audit = base.MemoryStore(), Audit()
    first = im('1762650000.000100', 'once', event_id='Ev1', envelope_id='e1')
    retry = im('1762650000.000100', 'once', event_id='Ev1', envelope_id='e2', retry_attempt=1)
    ch = make(FakeSlackAPI(), audit=audit, store=store, ws_factory=FakeWSFactory([]), sleep=lambda s: None)
    ws = FakeWS([hello(), first, retry])
    ch._socket_session(ws)
    assert ws.acked() == ['e1', 'e2']
    assert len(ch.poll(0)) == 1
    assert 'channel.drop_dup' in audit.events()
    # a fresh adapter (actor restart) with the same store: the seen key still drops the redelivery
    ch2 = make(FakeSlackAPI(), store=store, ws_factory=FakeWSFactory([]), sleep=lambda s: None)
    ws2 = FakeWS([hello(), retry])
    ch2._socket_session(ws2)
    assert ws2.acked() == ['e2'] and ch2.poll(0) == []


def test_bot_and_subtype_messages_are_not_inbound():
    audit = Audit()
    ch = make(FakeSlackAPI(), audit=audit, ws_factory=FakeWSFactory([]), sleep=lambda s: None)
    secret_text = 'do not log this'
    frames = [hello(),
              im('1762650000.000100', secret_text, envelope_id='b1', bot_id='B0FAKE'),
              im('1762650000.000200', secret_text, envelope_id='b2', subtype='message_changed'),
              im('1762650000.000300', secret_text, envelope_id='b3', subtype='message_deleted'),
              im('1762650000.000400', '', envelope_id='f1', subtype='file_share',
                 files=[{'id': 'F1', 'name': 'evil<name>.pdf', 'size': 10}])]
    ws = FakeWS(frames)
    ch._socket_session(ws)
    assert ws.acked() == ['b1', 'b2', 'b3', 'f1']
    got = ch.poll(0)
    drops = [f for e, f in audit if e == 'channel.drop_invalid']
    assert len(drops) == 3 and secret_text not in json.dumps(drops, ensure_ascii=False, default=str)
    assert 'channel.drop_policy' not in audit.events()
    # ruling 10: a file shared in the DM is an envelope that refuses the file (the actor answers file_refused)
    assert len(got) == 1
    env = got[0]
    assert envelope.problems(env) == [] and env['message_id'].startswith('sk:')
    assert env.get('files', []) == []
    assert env['rejected_files'] == [{'name': 'evil<name>.pdf', 'reason': 'Slack 第一期不收文件'}]
    assert 'evil' not in env['text']


def test_non_im_channel_is_private_false():
    audit = Audit()
    ch = make(FakeSlackAPI(), audit=audit)
    frame = im('1762650000.000100', 'in a channel', channel=CHAN, channel_type='channel')
    assert ch.inbound(frame) == []
    drops = [f for e, f in audit if e == 'channel.drop_policy']
    assert len(drops) == 1 and drops[0]['private'] is False and drops[0]['conversation'] == CHAN
    (env,) = ch.parse_inbound(frame)  # the adapter reports the fact; the base class drops it
    assert env['private'] is False and env['sender'] == USER


def test_outsider_dm_is_dropped_by_allowlist():
    keys = 'SLACK_BOT_TOKEN=' + TOKEN + '\nSLACK_APP_TOKEN=' + APP_TOKEN + '\n'
    # N1 (ruling 1, P9a ALLOW_SYNONYMS): SLACK_ALLOW_USERS is the slack ALLOW key -> Account.allow, never values
    acct = channels.parse_account_text('slack', ACCOUNT, keys + 'SLACK_ALLOW_USERS=' + USER + '\n')
    assert acct.allow == frozenset({USER}) and 'SLACK_ALLOW_USERS' not in acct.values
    assert channels.ALLOW_SYNONYMS.get('slack') == 'SLACK_ALLOW_USERS'
    assert channels.parse_account_text('slack', ACCOUNT, keys + 'SLACK_ALLOW_USERS=U1,U2\n').allow == {'U1', 'U2'}
    empty = channels.parse_account_text('slack', ACCOUNT, keys + 'SLACK_ALLOW_USERS=\n')
    assert empty.allow == frozenset() and 'SLACK_ALLOW_USERS' not in empty.values

    audit = Audit()
    ch = make(FakeSlackAPI(), account=acct, audit=audit)
    assert ch.inbound(im('1762650000.000100', 'hello from outside', user=OUTSIDER, channel=OTHER_DM)) == []
    drops = [f for e, f in audit if e == 'channel.drop_policy']
    assert drops and drops[-1]['sender'] == OUTSIDER and 'hello' not in json.dumps(drops, default=str)
    assert len(ch.inbound(im('1762650000.000200', 'hello from owner'))) == 1

    audit2 = Audit()
    ch2 = make(FakeSlackAPI(), account=empty, audit=audit2)  # empty whitelist: everyone is rejected
    assert ch2.inbound(im('1762650000.000300', 'hello from owner')) == []
    assert 'channel.drop_policy' in audit2.events()


def test_block_actions_from_owner_become_action_envelope():
    ch = make(FakeSlackAPI(), ws_factory=FakeWSFactory([]), sleep=lambda s: None)
    ws = FakeWS([hello(), press('approve', 'p-0123abcd', msg_ts='1762650000.000900', action_ts='1762650001.000001',
                                envelope_id='e9')])
    ch._socket_session(ws)
    assert ws.acked() == ['e9']
    (env,) = ch.poll(0)
    assert envelope.problems(env) == []
    assert env['text'] == APPROVE
    assert env['action'] == {'command': APPROVE, 'card_message_id': '1762650000.000900'}
    assert env['sender'] == USER and env['private'] is True and env['conversation'] == DM
    assert env['message_id'] == 'sk:act:' + DM + ':1762650001.000001'
    assert envelope.parse_command(env['text'])['kind'] == 'approve'
    # ruling 6: a duplicate action_id carries a .2 suffix that inbound strips; the nonce in value is kept
    ws2 = FakeWS([press('approve.2', 'p-89abcdef ' + NONCE, action_ts='1762650001.000002', envelope_id='e10')])
    ch._socket_session(ws2)
    (env2,) = ch.poll(0)
    assert env2['text'] == env2['action']['command'] == '/approve p-89abcdef ' + NONCE


def test_block_actions_from_outsider_are_ignored():
    api, audit = FakeSlackAPI(), Audit()
    ch = make(api, audit=audit, ws_factory=FakeWSFactory([]), sleep=lambda s: None)
    ws = FakeWS([hello(), press('approve', 'p-0123abcd', user=OUTSIDER, channel=OTHER_DM, envelope_id='e9')])
    ch._socket_session(ws)
    assert ws.acked() == ['e9']
    assert ch.poll(0) == []
    drops = [f for e, f in audit if e == 'channel.drop_policy']
    assert len(drops) == 1 and drops[0]['sender'] == OUTSIDER
    assert api.named('chat.postMessage') == []  # an outsider gets no answer


def test_disconnect_frame_reconnects_with_new_url():
    api = FakeSlackAPI()
    factory = FakeWSFactory([[hello(), disconnect()], [hello(), im('1762650000.000100', 'after reconnect')]])
    sleeps = []
    holder = {}

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            holder['ch'].close()
        if len(sleeps) > 10:
            raise RuntimeError('fake sleep: the loop did not stop')
    ch = holder['ch'] = make(api, ws_factory=factory, sleep=sleep)
    ch._socket_loop()
    opens = api.named('apps.connections.open')
    assert len(opens) >= 2
    assert all(c['headers']['authorization'] == 'Bearer ' + APP_TOKEN for c in opens)
    assert len(factory.urls) >= 2 and factory.urls[0] != factory.urls[1]
    assert sleeps and all(isinstance(s, (int, float)) for s in sleeps) and sleeps == sorted(sleeps)
    assert [e['text'] for e in ch.poll(0)] == ['after reconnect']

    # the app token is refused -> needs_login, and the error carries no token
    api2, audit2, holder2 = FakeSlackAPI(), Audit(), {}
    api2.fail['apps.connections.open'] = [(200, {'ok': False, 'error': 'invalid_auth'}, {})] * 3

    def sleep2(seconds):
        holder2['ch'].close()
    ch2 = holder2['ch'] = make(api2, audit=audit2, ws_factory=FakeWSFactory([]), sleep=sleep2)
    ch2._socket_loop()
    assert ch2.health()['state'] == 'needs_login'
    assert 'xapp-' not in ch2.last_error and 'xapp-' not in json.dumps(audit2, default=str)


def test_missing_websocket_client_is_missing_dependency(monkeypatch):
    monkeypatch.setitem(sys.modules, 'websocket', None)  # importing it now raises ImportError
    importlib.reload(importlib.import_module(channels.ADAPTER_MODULES['slack']))  # top level: stdlib only
    assert channels.adapter_class('slack') is not None
    ch = make(FakeSlackAPI())  # no ws_factory: start() must import websocket-client
    with pytest.raises(channels.MissingDependency) as info:
        ch.start()
    assert 'websocket-client' in info.value.package
    assert ch.send_text(DM, 'still sends', reply_ref={'channel': DM}).ok
    assert ch.health()['state'] in channels.INBOUND_STATES
    assert channels.adapter_class('telegram') is not None  # the other channels are untouched
    ch.close()


# ====================================================================== P9c review of wip 4cc04f3 (added tests)
class IdleWS(FakeWS):
    """recv() raises a read timeout for every ``None`` in the script (a quiet connection); records pings."""

    def __init__(self, frames, events=None):
        super().__init__(frames, events)
        self.pings = 0

    def recv(self):
        if self.frames and self.frames[0] is None:
            self.frames.pop(0)
            raise TimeoutError('fake read timeout')
        return super().recv()

    def ping(self, payload=''):
        self.pings += 1


def test_idle_read_timeout_pings_once_then_keeps_receiving():
    ch = make(FakeSlackAPI(), ws_factory=FakeWSFactory([]), sleep=lambda s: None)
    ws = IdleWS([hello(), None, im('1762650000.000100', 'after a quiet minute', envelope_id='e1'), None,
                 im('1762650000.000200', 'and another', envelope_id='e2')])
    assert ch._socket_session(ws) is True
    assert ws.pings == 2 and ws.acked() == ['e1', 'e2']
    assert [e['text'] for e in ch.poll(0)] == ['after a quiet minute', 'and another']
    # two timeouts in a row: the connection is dead, the session ends (the loop reconnects)
    dead = IdleWS([hello(), None, None, im('1762650000.000300', 'never read', envelope_id='e3')])
    ch._socket_session(dead)
    assert dead.pings == 1 and dead.acked() == [] and dead.closed


def test_close_ends_a_real_backoff_wait_at_once():
    import time as _time
    ch = make(FakeSlackAPI(), ws_factory=FakeWSFactory([]), sleep=_time.sleep)
    ch.close()
    started = _time.monotonic()
    ch._backoff_wait(300)  # the needs_login wait: must not block a stopped adapter
    assert _time.monotonic() - started < 5


def test_unrelated_websocket_package_is_missing_dependency(monkeypatch):
    import types
    monkeypatch.setitem(sys.modules, 'websocket', types.ModuleType('websocket'))  # PyPI 'websocket', no client
    ch = make(FakeSlackAPI())
    with pytest.raises(channels.MissingDependency) as info:
        ch.start()
    assert 'websocket-client' in info.value.package
    ch.close()
