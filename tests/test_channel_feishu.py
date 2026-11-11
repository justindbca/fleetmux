"""Feishu adapter protocol tests (packages P3a send / P3b receive; brief v2.1 §6.10, design §53, contracts §8).

Also the harness of the shared adapter contract: ``tests/test_channels_contract.py`` imports this module and reads
its ``CONTRACT`` dict (the ``test_adapter_*[red:P3-feishu]`` rows).

Rules of this file:
- No real network, no real credential, no listener: HTTP goes through the injected ``transport`` (an in-process
  ``FakeFeishuAPI`` with the signature of ``base.http_transport``); the WebSocket client comes from the injected
  ``ws_factory`` (``FakeWS``); status writes go to an injected ``status`` sink (a list). Fake values only:
  ``ou_test_user`` / ``oc_test_chat`` / ``om_test_1`` / ``cli_fake_app`` / ``fake-app-secret``.
- The optional SDK (lark-oapi) is never imported at module level here and never installed by the tests. Only
  ``test_lark_dispatcher_end_to_end_without_network`` needs it (``pytest.importorskip``); every other test runs
  without it, several with it made unimportable on purpose.
- The Feishu whitelist key is the open_id (``ou_…``), and it is per app: the same person has a different open_id
  under every Feishu app, so a new fleetmux app needs its own open_id in ``ALLOW``.

Interface pieces these tests pin (feishu-1 test-plan, approved by the supervisor):
- ``ws_factory(*, app_id, app_secret, on_event, on_card, on_frame) -> client`` (``client.start()`` blocks; run in a
  thread by ``start()``); ``on_event(dict)`` / ``on_card(dict) -> toast`` take schema-2.0 event dicts,
  ``on_frame('pong' | 'data')`` marks heartbeat / activity. Without it ``start()`` imports lark-oapi.
- ``status(fields)`` (Feishu-only constructor keyword, default None): every state change and every
  heartbeat / activity timestamp update writes ``{state, heartbeat_at, activity_at, error}``; states
  ``ok / reconnecting / needs_login / missing_dependency`` (brief v2); the Redis write is P1d's.
- Button privacy: a card action is private only when this account's store has ``p2p:<chat_id>`` (written from a
  trusted ``im.message.receive_v1`` p2p event) or the chat API says ``chat_mode == 'p2p'``; any failure or
  doubt is not private.
"""
import ast
import asyncio
import contextlib
import importlib
import importlib.util
import json
import os
import socket
import sys
import threading
import time
import types
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from fleetmux import channels
from fleetmux.channels import base, envelope
from fleetmux.channels import feishu

ROOT = Path(__file__).resolve().parents[1]
API = '/open-apis'
APP_ID = 'cli_fake_app'
APP_SECRET = 'fake-app-secret'
TOKEN_PATH = API + '/auth/v3/tenant_access_token/internal'
APPROVE = '/approve p-0123abcd'
REJECT = '/reject p-0123abcd'
T0 = 1762650000.0


@pytest.fixture(autouse=True)
def config_dir(monkeypatch, tmp_path):
    """Never the operator's ~/.config/fleetmux."""
    d = tmp_path / 'fleetmux-config'
    monkeypatch.setenv(channels.CONFIG_ENV, str(d))
    return d


# ====================================================================== fakes
def _json(obj):
    return json.dumps(obj, ensure_ascii=False).encode()


class FakeFeishuAPI:
    """In-process Feishu open API: callable(method, url, *, headers, body, timeout) -> (status, body bytes)."""

    def __init__(self, chats=None, files=None, expire=7200):
        self.calls = []
        self.faults = []
        self.sent = 0
        self.issued = []  # tenant tokens handed out
        self.expire = expire
        self.chats = {'oc_test_chat': 'p2p', 'oc_test_group': 'group'} if chats is None else chats
        self.files = files or {}

    def fail(self, where, *, status=None, code=None, exc=None, times=1, method=None):
        self.faults.append({'where': where, 'status': status, 'code': code, 'exc': exc, 'times': times,
                            'method': method})

    def __call__(self, method, url, *, headers=None, body=None, timeout=30):
        parts = urlsplit(url)
        path, query = parts.path, parse_qs(parts.query)
        self.calls.append({'method': method, 'url': url, 'path': path, 'query': query,
                           'headers': dict(headers or {}), 'body': body, 'timeout': timeout})
        for f in self.faults:
            if f['times'] > 0 and f['where'] in path and f['method'] in (None, method):
                f['times'] -= 1
                if f['exc'] is not None:
                    raise f['exc']
                if f['status'] is not None:
                    return f['status'], _json({'code': 9499, 'msg': 'fake http error'})
                return 200, _json({'code': f['code'], 'msg': 'fake platform error'})
        if path == TOKEN_PATH and method == 'POST':
            token = 't-fake-tenant-' + str(len(self.issued) + 1)
            self.issued.append(token)
            return 200, _json({'code': 0, 'msg': 'ok', 'tenant_access_token': token, 'expire': self.expire})
        if path == API + '/im/v1/messages' and method == 'POST' or (
                path.startswith(API + '/im/v1/messages/') and path.endswith('/reply') and method == 'POST'):
            self.sent += 1
            return 200, _json({'code': 0, 'msg': 'ok', 'data': {'message_id': 'om_sent_' + str(self.sent)}})
        if path.startswith(API + '/im/v1/messages/') and '/resources/' in path and method == 'GET':
            key = path.rsplit('/', 1)[1]
            return 200, self.files.get(key, b'fake-bytes')
        if path.startswith(API + '/im/v1/messages/') and method == 'PATCH':
            return 200, _json({'code': 0, 'msg': 'ok'})
        if path == API + '/im/v1/files' and method == 'POST':
            return 200, _json({'code': 0, 'msg': 'ok', 'data': {'file_key': 'file_fake_1'}})
        if path.startswith(API + '/im/v1/chats/') and method == 'GET':
            chat = path.rsplit('/', 1)[1]
            if chat not in self.chats:
                return 404, _json({'code': 232011, 'msg': 'chat not found'})
            mode = self.chats[chat]
            return 200, _json({'code': 0, 'msg': 'ok', 'data': {} if mode is None else {'chat_mode': mode}})
        return 404, _json({'code': 404, 'msg': 'fake: no route ' + method + ' ' + path})

    # helpers for assertions
    def to(self, path, method=None):
        return [c for c in self.calls if c['path'] == path and (method is None or c['method'] == method)]

    def token_calls(self):
        return self.to(TOKEN_PATH, 'POST')

    def message_posts(self):
        return [c for c in self.calls if c['method'] == 'POST' and (
            c['path'] == API + '/im/v1/messages' or c['path'].endswith('/reply'))]

    def chat_calls(self):
        return [c for c in self.calls if c['path'].startswith(API + '/im/v1/chats/')]


def body(call):
    return json.loads(call['body'])


def header(call, name):
    for k, v in call['headers'].items():
        if k.lower() == name.lower():
            return v
    return None


def _msg(mid, sender, chat, chat_type, content, *, msg_type='text', mentions=None, event_id=None):
    """A schema-2.0 im.message.receive_v1 event as a dict (what the WS callback queues)."""
    return {'schema': '2.0',
            'header': {'event_id': event_id or 'ev_' + mid, 'event_type': 'im.message.receive_v1',
                       'create_time': '1762650000000', 'app_id': APP_ID, 'tenant_key': 'tk_fake', 'token': ''},
            'event': {'sender': {'sender_id': {'open_id': sender}, 'sender_type': 'user', 'tenant_key': 'tk_fake'},
                      'message': {'message_id': mid, 'chat_id': chat, 'chat_type': chat_type,
                                  'message_type': msg_type, 'content': content, 'create_time': '1762650000000',
                                  'mentions': mentions or []}}}


def _card(event_id, operator, chat, command, card_mid='om_card_1', **context):
    """A schema-2.0 card.action.trigger event as a dict (operator = who pressed)."""
    ctx = {'open_message_id': card_mid, 'open_chat_id': chat}
    ctx.update(context)
    return {'schema': '2.0',
            'header': {'event_id': event_id, 'event_type': 'card.action.trigger', 'create_time': '1762650000000',
                       'app_id': APP_ID, 'tenant_key': 'tk_fake', 'token': ''},
            'event': {'operator': {'open_id': operator, 'tenant_key': 'tk_fake'},
                      'action': {'tag': 'button', 'value': {'command': command}},
                      'context': ctx}}


CONTRACT = {
    'account': {'ALLOW': 'ou_test_user', 'APP_ID': APP_ID, 'APP_SECRET': APP_SECRET},
    'conversation': 'oc_test_chat',
    'transport': FakeFeishuAPI,
    'inbound_ok': _msg('om_test_1', 'ou_test_user', 'oc_test_chat', 'p2p', '{"text":"hello contract"}'),
    'inbound_text': 'hello contract',
    'inbound_foreign': _msg('om_test_2', 'ou_outsider', 'oc_out_chat', 'p2p', '{"text":"hello contract"}'),
    'inbound_group': _msg('om_test_3', 'ou_test_user', 'oc_test_group', 'group', '{"text":"hello contract"}'),
    'inbound_unverified': None,  # WS events are authenticated by the platform: no unverified sender on Feishu
    'inbound_button': _card('ev_b1', 'ou_test_user', 'oc_test_chat', APPROVE),
    'inbound_button_text': APPROVE,
    'inbound_button_foreign': _card('ev_b2', 'ou_outsider', 'oc_test_chat', APPROVE),
    'inbound_button_group': _card('ev_b3', 'ou_test_user', 'oc_test_group', APPROVE),
}


class Audit(list):
    def __call__(self, event, **fields):
        self.append((event, fields))

    def events(self):
        return [e for e, _ in self]


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


class Sink(list):
    """The injected ``status`` writer."""

    def __call__(self, fields):
        self.append(dict(fields))

    def states(self, start=0):
        out = []
        for f in self[start:]:
            if not out or out[-1] != f['state']:
                out.append(f['state'])
        return out


class ClientException(Exception):
    """Shape of lark_oapi's ClientException (credential rejected); the SDK is not needed to raise it."""

    def __init__(self, code, msg):
        self.code, self.msg = code, msg
        super().__init__(str(code) + ', ' + msg)


class FakeWS:
    """ws_factory: records its keyword arguments; the client blocks in start() until released."""

    def __init__(self, start_exc=None):
        self.calls = []
        self.start_exc = start_exc
        self.started = threading.Event()
        self.release = threading.Event()

    @property
    def cb(self):
        return self.calls[-1]

    def __call__(self, **kw):
        self.calls.append(kw)
        owner = self

        class Client:
            def start(self):
                owner.started.set()
                if owner.start_exc is not None:
                    raise owner.start_exc
                owner.release.wait(5)
        return Client()


def account(**values):
    v = dict(CONTRACT['account'])
    v.update(values)
    return channels.parse_account_text('feishu', 'fs-test', '\n'.join(k + '=' + str(x) for k, x in v.items()))


def make(tmp_path, api=None, *, store=None, clock=None, **kw):
    """(channel, api, audit, clock). ``kw``: the Feishu-only keywords ws_factory= / status=."""
    api = FakeFeishuAPI() if api is None else api
    audit = Audit()
    clock = Clock() if clock is None else clock
    ch = feishu.ADAPTER(account(), transport=api, store=base.MemoryStore() if store is None else store, audit=audit,
                        inbox_dir=tmp_path / 'inbox' / 'files', clock=clock, **kw)
    return ch, api, audit, clock


def sdk_unimportable(monkeypatch):
    """Make lark-oapi (and any loaded submodule) unimportable for this test, then reload the adapter module."""
    for name in [n for n in sys.modules if n == 'lark_oapi' or n.startswith('lark_oapi.')]:
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.setitem(sys.modules, 'lark_oapi', None)
    importlib.reload(feishu)


def wait_for(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def a_card():
    return envelope.card('删除确认', 'rm -rf build/', [{'label': '批准', 'command': APPROVE},
                                                {'label': '拒绝', 'command': REJECT}], ref='p-0123abcd')


def buttons_in(obj):
    """Every dict with a ``value`` that carries a ``command`` anywhere in a card JSON."""
    out = []
    if isinstance(obj, dict):
        if isinstance(obj.get('value'), dict) and 'command' in obj['value']:
            out.append(obj)
        for v in obj.values():
            out += buttons_in(v)
    elif isinstance(obj, list):
        for v in obj:
            out += buttons_in(v)
    return out


# ====================================================================== P3a: send (stdlib only)
def test_contract_dict_and_adapter_class_registered():
    assert set(CONTRACT) == {'account', 'conversation', 'transport', 'inbound_ok', 'inbound_text', 'inbound_foreign',
                             'inbound_group', 'inbound_unverified', 'inbound_button', 'inbound_button_text',
                             'inbound_button_foreign', 'inbound_button_group'}
    assert CONTRACT['inbound_unverified'] is None
    cls = channels.adapter_class('feishu')
    assert isinstance(cls, type) and issubclass(cls, base.Channel)
    assert cls.type == 'feishu'
    assert cls.TEXT_LIMIT == 4000
    assert cls.SUPPORTS_CARDS and cls.SUPPORTS_BUTTONS and cls.SUPPORTS_UPDATE and cls.SUPPORTS_FILES
    assert cls.RECEIVES is True
    assert tuple(cls.OPTIONAL_DEPS) == ('lark_oapi',)
    assert channels.prefix('feishu') == 'fs'


def test_first_send_fetches_tenant_token_then_reuses(tmp_path):
    ch, api, _, _ = make(tmp_path)
    assert ch.send_text('oc_test_chat', 'one').ok
    assert ch.send_text('oc_test_chat', 'two').ok
    tokens = api.token_calls()
    assert len(tokens) == 1
    assert body(tokens[0]) == {'app_id': APP_ID, 'app_secret': APP_SECRET}
    posts = api.message_posts()
    assert len(posts) == 2
    assert all(header(c, 'Authorization') == 'Bearer ' + api.issued[0] for c in posts)


def test_token_expiry_or_99991663_refreshes_once(tmp_path):
    # (a) expiry by the injected clock
    ch, api, _, clock = make(tmp_path)
    assert ch.send_text('oc_test_chat', 'a').ok
    clock.advance(60)
    assert ch.send_text('oc_test_chat', 'b').ok
    assert len(api.token_calls()) == 1
    clock.advance(7200)
    assert ch.send_text('oc_test_chat', 'c').ok
    assert len(api.token_calls()) == 2
    assert header(api.message_posts()[-1], 'Authorization') == 'Bearer ' + api.issued[1]
    # (b) 99991663 (token invalid): one refresh, one resend, ok
    ch, api, _, _ = make(tmp_path)
    api.fail(API + '/im/v1/messages', code=99991663, times=1, method='POST')
    got = ch.send_text('oc_test_chat', 'x')
    assert got.ok, got.error
    assert len(api.token_calls()) == 2 and len(api.message_posts()) == 2
    # (c) 99991661 every time: one refresh only, then a failure (no loop)
    ch, api, _, _ = make(tmp_path)
    api.fail(API + '/im/v1/messages', code=99991661, times=5, method='POST')
    got = ch.send_text('oc_test_chat', 'y')
    assert not got.ok and got.error
    assert len(api.token_calls()) == 2 and len(api.message_posts()) == 2


def test_reply_ref_uses_reply_endpoint_else_create_by_chat_id(tmp_path):
    ch, api, _, _ = make(tmp_path)
    got = ch.send_text('oc_test_chat', 'hi', reply_ref={'message_id': 'om_test_1', 'chat_id': 'oc_test_chat'})
    assert got.ok, got.error
    call = api.message_posts()[-1]
    assert call['path'] == API + '/im/v1/messages/om_test_1/reply'
    assert body(call)['msg_type'] == 'text' and json.loads(body(call)['content']) == {'text': 'hi'}
    assert got.message_id.startswith('om_sent_')
    got = ch.send_text('oc_test_chat', 'push')
    assert got.ok, got.error
    call = api.message_posts()[-1]
    assert call['path'] == API + '/im/v1/messages' and call['query'].get('receive_id_type') == ['chat_id']
    b = body(call)
    assert b['receive_id'] == 'oc_test_chat' and b['msg_type'] == 'text'
    assert json.loads(b['content']) == {'text': 'push'}
    assert got.message_id == 'om_sent_2'


def test_send_card_interactive_with_button_values_as_commands(tmp_path):
    ch, api, _, _ = make(tmp_path)
    got = ch.send_card('oc_test_chat', a_card())
    assert got.ok, got.error
    b = body(api.message_posts()[-1])
    assert b['msg_type'] == 'interactive'
    content = json.loads(b['content'])
    assert '删除确认' in json.dumps(content, ensure_ascii=False)
    buttons = buttons_in(content)
    assert [x['value'] for x in buttons] == [{'command': APPROVE}, {'command': REJECT}]
    labels = json.dumps(buttons, ensure_ascii=False)
    assert '批准' in labels and '拒绝' in labels
    assert got.message_id.startswith('om_sent_')


def test_update_card_patches_message(tmp_path):
    ch, api, _, _ = make(tmp_path)
    got = ch.update_card('oc_test_chat', 'om_card_1', a_card())
    assert got.ok, got.error
    patches = api.to(API + '/im/v1/messages/om_card_1', 'PATCH')
    assert len(patches) == 1
    assert '删除确认' in json.dumps(json.loads(body(patches[0])['content']), ensure_ascii=False)
    assert header(patches[0], 'Authorization') == 'Bearer ' + api.issued[0]


def test_send_file_uploads_then_sends_to_same_conversation(tmp_path):
    ch, api, _, _ = make(tmp_path)
    f = tmp_path / 'result.txt'
    f.write_text('long result\n' * 10)
    got = ch.send_file('oc_test_chat', f, caption='result caption')
    assert got.ok, got.error
    order = [(c['method'], c['path']) for c in api.calls if c['path'] != TOKEN_PATH]
    upload_at = order.index(('POST', API + '/im/v1/files'))
    upload = api.to(API + '/im/v1/files', 'POST')[0]
    assert 'multipart/form-data' in (header(upload, 'Content-Type') or '')
    assert b'result.txt' in upload['body'] and b'long result' in upload['body'] and b'stream' in upload['body']
    posts = api.message_posts()
    assert len(posts) == 2
    file_msg, caption_msg = (body(c) for c in posts)
    assert api.calls.index(posts[0]) > api.calls.index(upload) and upload_at >= 0
    assert file_msg['msg_type'] == 'file' and json.loads(file_msg['content']) == {'file_key': 'file_fake_1'}
    assert file_msg['receive_id'] == 'oc_test_chat'
    assert caption_msg['receive_id'] == 'oc_test_chat' and caption_msg['msg_type'] == 'text'
    assert json.loads(caption_msg['content']) == {'text': 'result caption'}


def test_http_errors_and_nonzero_code_fail_without_secret(tmp_path):
    leak = 'https://open.fake/x?app_secret=' + APP_SECRET + '&app_id=' + APP_ID
    cases = [
        ('http 400', dict(where=API + '/im/v1/messages', status=400)),
        ('http 500', dict(where=API + '/im/v1/messages', status=500)),
        ('code 230001', dict(where=API + '/im/v1/messages', code=230001)),
        ('timeout', dict(where=API + '/im/v1/messages', exc=socket.timeout('timed out ' + leak))),
        ('network', dict(where=API + '/im/v1/messages', exc=OSError('unreachable ' + leak))),
        ('token endpoint', dict(where=TOKEN_PATH, code=10003)),
    ]
    for name, fault in cases:
        ch, api, audit, _ = make(tmp_path)
        if fault['where'] != TOKEN_PATH:
            assert ch.send_text('oc_test_chat', 'prime').ok, name  # a tenant token is known to the adapter now
        api.fail(fault.pop('where'), times=99, **fault)
        got = ch.send_text('oc_test_chat', 'hello')
        assert isinstance(got, base.SendResult) and not got.ok and got.error, name
        dump = json.dumps([got.error, ch.last_error, audit, ch.health()], ensure_ascii=False, default=str)
        for secret in [APP_SECRET, APP_ID] + api.issued:
            assert secret not in dump, (name, secret)


def test_send_path_imports_no_lark(tmp_path, monkeypatch):
    sdk_unimportable(monkeypatch)
    ch, api, _, _ = make(tmp_path)
    f = tmp_path / 'a.txt'
    f.write_text('x')
    assert ch.send_text('oc_test_chat', 'still sends').ok
    sent = ch.send_card('oc_test_chat', a_card())
    assert sent.ok
    assert ch.update_card('oc_test_chat', sent.message_id or 'om_card_1', a_card()).ok
    assert ch.send_file('oc_test_chat', f).ok
    tree = ast.parse(Path(feishu.__file__).read_text())
    for node in tree.body:
        if isinstance(node, ast.Import):
            roots = [a.name.split('.')[0] for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                continue
            roots = [(node.module or '').split('.')[0]]
        else:
            continue
        for r in roots:
            assert r in sys.stdlib_module_names or r == 'fleetmux', 'top-level import ' + r


# ====================================================================== P3b: receive
def test_start_without_lark_raises_missing_dependency_sends_still_work(tmp_path, monkeypatch):
    sdk_unimportable(monkeypatch)
    sink = Sink()
    ch, _, _, _ = make(tmp_path, status=sink)
    with pytest.raises(channels.MissingDependency) as e:
        ch.start()
    assert e.value.package == 'lark-oapi'
    assert ch.health()['state'] == 'missing_dependency'
    assert sink and sink[-1]['state'] == 'missing_dependency'
    assert ch.send_text('oc_test_chat', 'outbound still works').ok
    ch.close()
    # an injected ws_factory needs no SDK: start() does not import lark-oapi
    ws = FakeWS()
    ch2, _, _, _ = make(tmp_path, ws_factory=ws)
    try:
        ch2.start()
        assert ws.started.wait(2)
        assert len(ws.calls) == 1
        assert ws.cb['app_id'] == APP_ID and ws.cb['app_secret'] == APP_SECRET
        assert callable(ws.cb['on_event']) and callable(ws.cb['on_card']) and callable(ws.cb['on_frame'])
    finally:
        ws.release.set()
        ch2.close()


def test_envelope_from_private_text_strips_mention_placeholder(tmp_path):
    ch, _, _, _ = make(tmp_path)
    raw = _msg('om_test_1', 'ou_test_user', 'oc_test_chat', 'p2p', '{"text":"@_user_1 你好"}',
               mentions=[{'key': '@_user_1', 'id': {'open_id': 'ou_fake_bot'}, 'name': 'bot'}])
    got = ch.inbound(raw)
    assert len(got) == 1
    env = got[0]
    assert envelope.problems(env) == []
    assert env['channel'] == 'feishu' and env['account'] == 'fs-test'
    assert env['text'] == '你好'
    assert env['message_id'] == 'fs:om_test_1'
    assert env['conversation'] == 'oc_test_chat' and env['sender'] == 'ou_test_user'
    assert env['private'] is True and env['verified'] is True
    assert env['reply_ref'] == {'message_id': 'om_test_1', 'chat_id': 'oc_test_chat'}


def test_group_message_is_private_false_bad_content_json_is_dropped_invalid(tmp_path):
    ch, _, audit, _ = make(tmp_path)
    group = CONTRACT['inbound_group']
    parsed = ch.parse_inbound(group)
    assert len(parsed) == 1 and parsed[0]['private'] is False
    assert ch.inbound(group) == []
    assert 'channel.drop_policy' in audit.events()
    bad = _msg('om_test_9', 'ou_test_user', 'oc_test_chat', 'p2p', '{bad')
    assert ch.parse_inbound(bad) == []
    assert 'channel.error' in audit.events()


def test_image_file_message_fetched_via_resources_api_into_inbox_dir(tmp_path):
    api = FakeFeishuAPI(files={'img_k': b'\x89PNG fake image', 'file_k': b'hello file body'})
    ch, api, _, _ = make(tmp_path, api)
    inbox = (tmp_path / 'inbox' / 'files').resolve()
    image = _msg('om_test_4', 'ou_test_user', 'oc_test_chat', 'p2p', '{"image_key":"img_k"}', msg_type='image')
    got = ch.inbound(image)
    assert len(got) == 1
    call = [c for c in api.calls if c['path'] == API + '/im/v1/messages/om_test_4/resources/img_k'][0]
    assert call['method'] == 'GET' and call['query'].get('type') == ['image']
    assert header(call, 'Authorization') == 'Bearer ' + api.issued[0]
    f = got[0]['files'][0]
    p = Path(f['path']).resolve()
    assert p.parent == inbox and p.name.startswith('om_test_4-') and p.read_bytes() == b'\x89PNG fake image'
    assert f['size'] == len(b'\x89PNG fake image')
    assert got[0]['text'].startswith('[图片]')
    doc = _msg('om_test_5', 'ou_test_user', 'oc_test_chat', 'p2p', '{"file_key":"file_k","file_name":"a b.txt"}',
               msg_type='file')
    got = ch.inbound(doc)
    call = [c for c in api.calls if c['path'] == API + '/im/v1/messages/om_test_5/resources/file_k'][0]
    assert call['query'].get('type') == ['file']
    p = Path(got[0]['files'][0]['path']).resolve()
    assert p.parent == inbox and p.name == 'om_test_5-a_b.txt' and p.read_bytes() == b'hello file body'
    assert '[文件]' in got[0]['text'] and 'a b.txt' in got[0]['text']
    # too large: not written, refused with a reason
    ch.MAX_FILE_BYTES = 4
    before = sorted(inbox.iterdir())
    big = _msg('om_test_6', 'ou_test_user', 'oc_test_chat', 'p2p', '{"file_key":"file_k","file_name":"big.bin"}',
               msg_type='file')
    got = ch.inbound(big)
    assert len(got) == 1 and got[0]['files'] == [] and len(got[0]['rejected_files']) == 1
    assert got[0]['rejected_files'][0]['name'] == 'big.bin' and got[0]['rejected_files'][0]['reason']
    assert sorted(inbox.iterdir()) == before


def test_action_from_card_sets_sender_to_operator_and_action_command(tmp_path):
    # the press itself: operator is the sender, the command is the text
    ch, api, _, _ = make(tmp_path)
    got = ch.inbound(CONTRACT['inbound_button'])
    assert len(got) == 1
    env = got[0]
    assert envelope.problems(env) == []
    assert env['sender'] == 'ou_test_user'
    assert env['action'] == {'command': APPROVE, 'card_message_id': 'om_card_1'}
    assert env['text'] == APPROVE
    assert env['conversation'] == 'oc_test_chat'
    assert env['message_id'] == 'fs:cb:ev_b1'
    # (b) cache miss -> the chat API said p2p -> private, and written back: a second press asks nobody
    assert env['private'] is True and len(api.chat_calls()) == 1
    assert ch.parse_inbound(_card('ev_b9', 'ou_test_user', 'oc_test_chat', APPROVE))[0]['private'] is True
    assert len(api.chat_calls()) == 1
    # (a) a trusted p2p message event of this account fills the cache: no chat API call at all
    ch, api, _, _ = make(tmp_path)
    assert len(ch.inbound(CONTRACT['inbound_ok'])) == 1
    assert ch.store.get('p2p:oc_test_chat')
    assert ch.parse_inbound(_card('ev_b4', 'ou_test_user', 'oc_test_chat', APPROVE))[0]['private'] is True
    assert api.chat_calls() == []
    # (c) the chat API says group -> not private
    ch, api, _, _ = make(tmp_path)
    assert ch.parse_inbound(CONTRACT['inbound_button_group'])[0]['private'] is False
    # (d) failure or doubt -> not private, nothing cached
    for name, setup in [('http 500', lambda a: a.fail(API + '/im/v1/chats/', status=500)),
                        ('network', lambda a: a.fail(API + '/im/v1/chats/', exc=OSError('down'))),
                        ('code != 0', lambda a: a.fail(API + '/im/v1/chats/', code=99991400)),
                        ('no chat_mode', lambda a: a.chats.update({'oc_x': None})),
                        ('topic', lambda a: a.chats.update({'oc_x': 'topic'}))]:
        api = FakeFeishuAPI()
        setup(api)
        api.chats.setdefault('oc_x', 'p2p')
        ch, api, _, _ = make(tmp_path, api)
        assert ch.parse_inbound(_card('ev_d', 'ou_test_user', 'oc_x', APPROVE))[0]['private'] is False, name
        assert not ch.store.get('p2p:oc_x'), name
    # (e) what a card event claims about its own chat is not trusted
    api = FakeFeishuAPI(chats={})
    api.fail(API + '/im/v1/chats/', status=500, times=9)
    ch, api, _, _ = make(tmp_path, api)
    claim = _card('ev_e', 'ou_test_user', 'oc_y', APPROVE, chat_type='p2p', open_chat_type='p2p')
    assert ch.parse_inbound(claim)[0]['private'] is False
    assert not ch.store.get('p2p:oc_y')


def fake_requests_module():
    m = types.ModuleType('fake_requests')
    m.sessions = []

    class Session:
        def __init__(self):
            self.trust_env = True
            self.calls = []
            m.sessions.append(self)

        def get(self, url, **kw):
            self.calls.append(('GET', url, kw, self.trust_env))
            return 'fake-get'

        def post(self, url, **kw):
            self.calls.append(('POST', url, kw, self.trust_env))
            return 'fake-post'

    def direct(*a, **kw):
        raise AssertionError('module-level requests call: the proxy environment would apply')
    m.Session = Session
    m.get = m.post = direct
    return m


def fake_websockets_module():
    m = types.ModuleType('fake_websockets')
    m.received = []
    m.exceptions = types.SimpleNamespace(ConnectionClosed=type('ConnectionClosed', (Exception,), {}))

    async def connect(url, **kw):
        m.received.append((url, kw))
        return 'fake-conn'
    m.connect = connect
    return m


def test_scope_direct_patches_requests_and_websockets_without_env_change(monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.fake:1')
    monkeypatch.setenv('ALL_PROXY', 'socks5://proxy.fake:2')
    snapshot = dict(os.environ)
    orig_requests, orig_ws = fake_requests_module(), fake_websockets_module()
    ws_mod = types.SimpleNamespace(requests=orig_requests, websockets=orig_ws)
    feishu._scope_direct(ws_mod)
    assert ws_mod.requests.post('https://open.fake/callback/ws/endpoint', json={'AppID': 'x'}) == 'fake-post'
    calls = [c for s in orig_requests.sessions for c in s.calls]
    assert calls and all(c[3] is False for c in calls)
    assert asyncio.run(ws_mod.websockets.connect('wss://ws.fake/x', a=1)) == 'fake-conn'
    assert orig_ws.received == [('wss://ws.fake/x', {'proxy': None, 'a': 1})]
    assert ws_mod.websockets.exceptions is orig_ws.exceptions
    assert dict(os.environ) == snapshot


def test_scope_direct_idempotent():
    orig_requests, orig_ws = fake_requests_module(), fake_websockets_module()
    ws_mod = types.SimpleNamespace(requests=orig_requests, websockets=orig_ws)
    feishu._scope_direct(ws_mod)
    r1, w1 = ws_mod.requests, ws_mod.websockets
    assert r1 is not orig_requests and w1 is not orig_ws
    feishu._scope_direct(ws_mod)
    assert ws_mod.requests is r1 and ws_mod.websockets is w1
    asyncio.run(ws_mod.websockets.connect('wss://ws.fake/y'))
    assert orig_ws.received == [('wss://ws.fake/y', {'proxy': None})]


def test_status_hash_heartbeat_180_activity_1800(tmp_path):
    sink, ws = Sink(), FakeWS()
    ch, _, _, clock = make(tmp_path, ws_factory=ws, status=sink)
    try:
        ch.start()
        assert ws.started.wait(2)
        on_frame = ws.cb['on_frame']
        mark = len(sink)
        on_frame('pong')
        on_frame('data')
        assert sink[-1]['state'] == 'ok'
        assert sink[-1]['heartbeat_at'] == T0 and sink[-1]['activity_at'] == T0
        # same state, new timestamps: each is written (not a duplicate-state error)
        n = len(sink)
        clock.advance(30)
        on_frame('pong')
        assert len(sink) == n + 1 and sink[-1]['state'] == 'ok'
        assert sink[-1]['heartbeat_at'] == T0 + 30 and sink[-1]['activity_at'] == T0
        clock.advance(10)
        on_frame('data')
        assert len(sink) == n + 2 and sink[-1]['state'] == 'ok' and sink[-1]['activity_at'] == T0 + 40
        # heartbeat stale > 180 s -> reconnecting (found by the next poll)
        clock.advance(181)
        assert ch.poll(0) == []
        assert sink[-1]['state'] == 'reconnecting' and ch.health()['state'] == 'reconnecting'
        on_frame('pong')
        on_frame('data')
        assert sink[-1]['state'] == 'ok'
        # heartbeat fresh, activity stale > 1800 s -> reconnecting
        for _ in range(19):
            clock.advance(100)
            on_frame('pong')
        ch.poll(0)
        assert sink[-1]['state'] == 'reconnecting'
        on_frame('data')
        assert sink[-1]['state'] == 'ok'
        assert sink.states(mark) == ['ok', 'reconnecting', 'ok', 'reconnecting', 'ok']
        assert all(set(f) == {'state', 'heartbeat_at', 'activity_at', 'error'} for f in sink)
        assert ch.health() == sink[-1]
    finally:
        ws.release.set()
        ch.close()


def test_client_exception_recorded_once_no_reconnect_loop(tmp_path):
    sink = Sink()
    ws = FakeWS(start_exc=ClientException(1000040343, 'app secret invalid: ' + APP_SECRET))
    ch, _, audit, clock = make(tmp_path, ws_factory=ws, status=sink)
    try:
        with contextlib.suppress(channels.ChannelError):
            ch.start()
        assert ws.started.wait(2)
        assert wait_for(lambda: ch.health()['state'] == 'needs_login')
        assert audit.events().count('channel.error') == 1
        assert 'credential' in ch.last_error.lower() or '凭证' in ch.last_error
        assert APP_SECRET not in ch.last_error
        assert sink.states().count('needs_login') == 1
        assert all(APP_SECRET not in f['error'] for f in sink)
        clock.advance(3600)
        for _ in range(3):
            assert ch.poll(0) == []
        assert len(ws.calls) == 1
        assert audit.events().count('channel.error') == 1
        assert ch.send_text('oc_test_chat', 'outbound unaffected').ok
    finally:
        ws.release.set()
        ch.close()


def test_lark_dispatcher_end_to_end_without_network(tmp_path, monkeypatch):
    lark = pytest.importorskip('lark_oapi')

    def no_network(*a, **kw):
        raise AssertionError('network used')
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    ws = FakeWS()
    ch, _, _, _ = make(tmp_path, ws_factory=ws)
    try:
        ch.start()
        assert ws.started.wait(2)
        dispatcher = feishu.build_dispatcher(ws.cb['on_event'], ws.cb['on_card'])
        dispatcher.do_without_validation(json.dumps(CONTRACT['inbound_ok']).encode())
        got = ch.poll(0)
        assert len(got) == 1
        env = got[0]
        assert env['message_id'] == 'fs:om_test_1' and env['text'] == 'hello contract'
        assert env['sender'] == 'ou_test_user' and env['private'] is True
        resp = dispatcher.do_without_validation(json.dumps(CONTRACT['inbound_button']).encode())
        assert '已收到' in lark.JSON.marshal(resp)
        got = ch.poll(0)
        assert len(got) == 1 and got[0]['action'] == {'command': APPROVE, 'card_message_id': 'om_card_1'}
    finally:
        ws.release.set()
        ch.close()


def test_deps_yaml_optional_key_does_not_break_check_deps():
    import yaml
    spec = yaml.safe_load((ROOT / 'deps.yaml').read_text())
    assert (spec['optional_python_packages']['lark-oapi']['channel'] == 'feishu'
            and spec['optional_python_packages']['lark-oapi']['min'] == '1.5.3')  # contracts §8 schema {<pip>: {min, channel, why}}
    assert 'lark' not in (ROOT / spec['python_packages']).read_text().lower()
    loader = importlib.util.spec_from_file_location('check_deps_feishu', ROOT / 'scripts/check_deps.py')
    check_deps = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(check_deps)
    rows = check_deps.check(root=ROOT, backend='fleetmux', plat='linux', probe_it=lambda cmd: (99999999,))
    assert rows and not any('lark' in str(r[0]).lower() for r in rows)


def test_downloaded_file_starting_with_brace_is_saved_not_taken_for_an_api_error(tmp_path):
    """M89: a file whose bytes start with '{' (a .json upload) is saved. The adapter tells a file from an API answer
    by Content-Type when the transport gives headers, else by the HTTP status; never by the first byte."""
    payload = b'{"code": 99991663, "msg": "this is file content, not an API answer"}\n'
    # (a) a transport without headers (status, body): 200 -> the bytes are the file
    api = FakeFeishuAPI(files={'json_k': payload})
    ch, api, _, _ = make(tmp_path, api)
    inbox = (tmp_path / 'inbox' / 'files').resolve()
    doc = _msg('om_json_1', 'ou_test_user', 'oc_test_chat', 'p2p', '{"file_key":"json_k","file_name":"data.json"}',
               msg_type='file')
    got = ch.inbound(doc)
    assert len(got) == 1 and got[0]['rejected_files'] == [], got
    p = Path(got[0]['files'][0]['path']).resolve()
    assert p.parent == inbox and p.name == 'om_json_1-data.json' and p.read_bytes() == payload
    assert len(api.token_calls()) == 1  # not mistaken for an expired-token answer

    # (b) a transport with headers (status, body, headers): Content-Type decides
    class WithHeaders(FakeFeishuAPI):
        expired_once = True

        def __call__(self, method, url, **kw):
            status, data = super().__call__(method, url, **kw)
            if '/resources/' in url:
                if self.expired_once:  # a real API answer on 200, as JSON: refresh the token, retry
                    self.expired_once = False
                    return 200, _json({'code': 99991663, 'msg': 'token expired'}), {
                        'Content-Type': 'application/json; charset=utf-8'}
                return status, data, {'content-type': 'application/octet-stream'}
            return status, data, {'Content-Type': 'application/json; charset=utf-8'}
    api = WithHeaders(files={'json_k': payload})
    ch, api, _, _ = make(tmp_path, api)
    got = ch.inbound(_msg('om_json_2', 'ou_test_user', 'oc_test_chat', 'p2p',
                          '{"file_key":"json_k","file_name":"data.json"}', msg_type='file'))
    assert len(got) == 1 and got[0]['rejected_files'] == [], got
    assert Path(got[0]['files'][0]['path']).read_bytes() == payload
    assert len(api.token_calls()) == 2  # the JSON answer was an API error: one refresh, one retry
    assert ch.send_text('oc_test_chat', 'sends read 3-tuples too').ok
