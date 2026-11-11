"""Channel interface contract (docs/design.md §53, docs/contracts.md §8). Package P0 of the channels run.

Three groups in one file (phase A freezes them together so later packages never edit each other's files):

1. ``test_p0_*``: the frozen pieces themselves (registry, roster ``channels:`` rules, account files, envelope,
   origin, outbound item, card, command grammar, receipts, the base class policy). Green at P0.
2. ``test_adapter_*``: the adapter contract, parametrised over every adapter type plus an in-file fake. Each
   adapter package provides, in its own protocol test module ``tests/test_channel_<type>.py``, a module-level
   ``CONTRACT`` dict:

       CONTRACT = {
           'account': {'ALLOW': '<whitelisted sender id>', '<KEY>': '<fake value>', ...},  # account file values
           'conversation': '<conversation id to send to>',
           'transport': <zero-arg factory -> fake transport that answers every call successfully>,
           # receiving adapters (RECEIVES = True) only; None for exec:
           'inbound_ok': <raw platform item: a private text 'inbound_text' from the whitelisted sender>,
           'inbound_text': 'hello contract',
           'inbound_foreign': <the same message from a sender not in ALLOW>,
           'inbound_group': <a group / non-private message, or None when the platform has none>,
           'inbound_unverified': <optional: a message whose sender the platform did not authenticate>,
       }

   Rows of adapters not built yet fail with "red: <package> not landed" (ids ``red:P2-telegram`` …); select
   ``-k "not red"`` for what must be green now. Import errors fail, never skip (design §42).
3. ``test_red_p1_* / test_red_p6_* / test_red_p7_*``: red skeletons of the frozen cross-package interfaces
   (actor, lease, throttle, queue, CLI, roster, lifecycle_close compatibility, delete approval hook,
   daemon-down step, split budget). Their packages turn them green; they are append-only (test contract).

No real network, no real credentials, no listener: transports are fakes, the config dir is a temp dir.
"""
import importlib
import json
import re
import sys
import time
from pathlib import Path

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture: the repo's private test Redis)
from test_v2_codex_service import service  # noqa: F401  (fixture: a v2 service fleet on that Redis)
from fleetmux import channels
from fleetmux.channels import base, envelope

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def config_dir(monkeypatch, tmp_path):
    """Never the operator's ~/.fleetmux/config: every test gets its own."""
    d = tmp_path / 'fleetmux-config'
    monkeypatch.setenv(channels.CONFIG_ENV, str(d))
    return d


# ====================================================================== the in-file fake adapter
class FakeChannel(base.Channel):
    """A reference adapter: proves the harness and the base policy end to end. Transport: callable(op, data)."""
    type = 'fake'
    TEXT_LIMIT = 50
    SUPPORTS_CARDS = True
    SUPPORTS_BUTTONS = False
    SUPPORTS_UPDATE = True
    OPTIONAL_DEPS = ('fleetmux_contract_optional_dep',)  # never installed: start() must report it, sends work

    def start(self):
        self.require('fleetmux_contract_optional_dep', 'fleetmux-contract-optional-dep')

    def _send_text(self, conversation, text, reply_ref):
        return self.transport('send_text', {'to': conversation, 'text': text, 'reply_ref': reply_ref})

    def _send_card(self, conversation, card, reply_ref):
        return self.transport('send_card', {'to': conversation, 'card': card})

    def _update_card(self, conversation, message_id, card):
        return self.transport('update_card', {'to': conversation, 'id': message_id, 'card': card})

    def _send_file(self, conversation, path, caption, reply_ref):
        return self.transport('send_file', {'to': conversation, 'path': path, 'caption': caption})

    def parse_inbound(self, raw):
        # a button press reports who pressed it (not who got the card) and the chat it was pressed in
        action = {'command': raw['text'], 'card_message_id': str(raw['card'])} if raw.get('card') else None
        return [envelope.make('fake', self.account.name, conversation=raw['chat'], message_id=str(raw['id']) + ':'
                              + raw['chat'], sender=raw['from'], text=raw['text'], private=raw.get('private', True),
                              verified=raw.get('verified', True), at=1762650000, reply_ref={'chat': raw['chat']},
                              action=action)]

    def _poll(self, timeout):
        return self.transport('poll', {'timeout': timeout})


def _fake_transport():
    sent, inbox = [], [{'id': 7, 'chat': 'c-1', 'from': 'u-1', 'text': 'polled'}]

    def transport(op, data):
        if op == 'poll':
            out = list(inbox)
            inbox.clear()
            return out
        sent.append((op, data))
        return 'm-' + str(len(sent))
    transport.sent = sent
    return transport


FAKE_CONTRACT = {
    'account': {'ALLOW': 'u-1', 'TOKEN': 'fake-token'},
    'conversation': 'c-1',
    'transport': _fake_transport,
    'inbound_ok': {'id': 1, 'chat': 'c-1', 'from': 'u-1', 'text': 'hello contract'},
    'inbound_text': 'hello contract',
    'inbound_foreign': {'id': 2, 'chat': 'c-9', 'from': 'u-9', 'text': 'hello contract'},
    'inbound_group': {'id': 3, 'chat': 'g-1', 'from': 'u-1', 'text': 'hello contract', 'private': False},
    'inbound_unverified': {'id': 4, 'chat': 'c-1', 'from': 'u-1', 'text': 'hello contract', 'verified': False},
    'inbound_button': {'id': 5, 'chat': 'c-1', 'from': 'u-1', 'card': 9,
                       'text': '/approve p-0123456789ab 0123456789abcdef'},
    'inbound_button_text': '/approve p-0123456789ab 0123456789abcdef',
    'inbound_button_foreign': {'id': 6, 'chat': 'c-1', 'from': 'u-9', 'card': 9,
                               'text': '/approve p-0123456789ab 0123456789abcdef'},
    'inbound_button_group': {'id': 7, 'chat': 'g-1', 'from': 'u-1', 'card': 9, 'private': False,
                             'text': '/approve p-0123456789ab 0123456789abcdef'},
}

PACKAGE = {'telegram': 'P2', 'feishu': 'P3', 'weixin': 'P4', 'email': 'P5', 'exec': 'P6', 'slack': 'P9'}
ADAPTER_ROWS = [pytest.param('fake', id='P0-fake')] + [
    pytest.param(t, id='red:' + PACKAGE[t] + '-' + t) for t in channels.ADAPTER_MODULES]


@pytest.fixture
def fake_registered():
    undo = channels.register('fake', FakeChannel, 'fake')
    yield
    undo()


def harness(type_):
    """(adapter class, CONTRACT) for one type; a package that has not landed fails red, it is never skipped."""
    if type_ == 'fake':
        return FakeChannel, FAKE_CONTRACT
    try:
        cls = channels.adapter_class(type_)
    except channels.Unavailable as exc:
        pytest.fail('red: ' + PACKAGE[type_] + ' ' + type_ + ' adapter not landed (' + str(exc) + ')')
    try:
        module = importlib.import_module('test_channel_' + type_)
    except ImportError as exc:
        pytest.fail('red: ' + PACKAGE[type_] + ' tests/test_channel_' + type_ + '.py not landed (' + str(exc) + ')')
    contract = getattr(module, 'CONTRACT', None)
    if not isinstance(contract, dict):
        pytest.fail('red: tests/test_channel_' + type_ + '.py defines no CONTRACT dict')
    return cls, contract


class Audit(list):
    def __call__(self, event, **fields):
        self.append((event, fields))

    def events(self):
        return [e for e, _ in self]


def build(type_, tmp_path, transport=None):
    cls, contract = harness(type_)
    lines = '\r\n'.join(k + '=' + str(v) for k, v in contract['account'].items())  # CRLF on purpose
    account = channels.parse_account_text(type_, 'contract-1', lines)
    audit = Audit()
    ch = cls(account, transport=transport if transport is not None else contract['transport'](),
             store=base.MemoryStore(), audit=audit, inbox_dir=tmp_path / 'inbox' / 'files')
    return ch, contract, audit


def spy_text(ch):
    calls, real = [], ch._send_text

    def spy(conversation, text, reply_ref):
        calls.append((conversation, text, reply_ref))
        return real(conversation, text, reply_ref)
    ch._send_text = spy
    return calls


def boom(*args, **kwargs):
    raise OSError('contract: network down')


# ====================================================================== group 2: the adapter contract
@pytest.mark.parametrize('type_', ADAPTER_ROWS)
def test_adapter_declares_the_interface(type_, tmp_path, fake_registered):
    ch, contract, _ = build(type_, tmp_path)
    assert isinstance(ch, base.Channel) and ch.type == type_
    assert isinstance(ch.TEXT_LIMIT, int) and 100 <= ch.TEXT_LIMIT <= 100000 or type_ == 'fake'
    assert ch.account.allow, 'CONTRACT account needs an ALLOW whitelist'
    if ch.RECEIVES:
        for k in ('inbound_ok', 'inbound_text', 'inbound_foreign'):
            assert contract.get(k) is not None, 'receiving adapter CONTRACT needs ' + k


@pytest.mark.parametrize('type_', ADAPTER_ROWS)
def test_adapter_inbound_envelope_shape(type_, tmp_path, fake_registered):
    ch, contract, _ = build(type_, tmp_path)
    if not ch.RECEIVES:
        assert ch.inbound(contract.get('inbound_ok')) == [] and ch.poll(0) == []
        return
    got = ch.inbound(contract['inbound_ok'])
    assert len(got) == 1
    env = got[0]
    assert envelope.problems(env) == []
    assert env['channel'] == type_ and env['account'] == 'contract-1'
    assert env['message_id'].startswith(channels.prefix(type_) + ':')
    if type_ == 'email':  # P8a / P8b: a mail body is outside content; text holds only a typed single-line command
        assert env['text'] == '' and env.get('provenance') == [{'kind': 'email_body', 'text': contract['inbound_text']}]
    else:
        assert env['text'] == contract['inbound_text']
    assert env['sender'] in ch.account.allow and env['private'] is True and env['verified'] is True
    origin = envelope.origin_from(env)
    assert 'text' not in origin and origin['conversation'] == env['conversation']
    json.dumps(env)  # it crosses Redis and the exec stdin as JSON


@pytest.mark.parametrize('type_', ADAPTER_ROWS)
def test_adapter_inbound_is_idempotent(type_, tmp_path, fake_registered):
    ch, contract, audit = build(type_, tmp_path)
    if not ch.RECEIVES:
        return
    first = ch.inbound(contract['inbound_ok'])
    assert len(first) == 1
    assert ch.inbound(contract['inbound_ok']) == []  # a redelivery (WS reconnect, poll overlap) is dropped
    assert 'channel.drop_dup' in audit.events()
    ch.forget(first[0])  # the actor could not hand it over: a redelivery must get in again
    assert len(ch.inbound(contract['inbound_ok'])) == 1


@pytest.mark.parametrize('type_', ADAPTER_ROWS)
def test_adapter_inbound_whitelist_private_verified(type_, tmp_path, fake_registered):
    ch, contract, audit = build(type_, tmp_path)
    if not ch.RECEIVES:
        return
    assert ch.inbound(contract['inbound_foreign']) == []
    drops = [f for e, f in audit if e == 'channel.drop_policy']
    assert drops and drops[-1]['sender'] not in ch.account.allow
    assert 'hello' not in json.dumps(drops)  # the text of a stranger is not logged
    for k in ('inbound_group', 'inbound_unverified'):
        if contract.get(k) is not None:
            assert ch.inbound(contract[k]) == [], k + ' must be dropped'


@pytest.mark.parametrize('type_', ADAPTER_ROWS)
def test_adapter_send_text_is_split_at_the_limit(type_, tmp_path, fake_registered):
    ch, contract, _ = build(type_, tmp_path)
    calls = spy_text(ch)
    text = ('line of text ' * 7 + '\n') * (3 * ch.TEXT_LIMIT // 90 + 2)
    assert ch.OVERFLOW in ('split', 'file') and ch.TEXT_UNITS in ('chars', 'utf16')
    if ch.OVERFLOW == 'file':  # email: one message, the long text attached; never chunked
        long_calls, real = [], ch._send_long_text
        ch._send_long_text = lambda c, t, r: (long_calls.append(t), real(c, t, r))[1]
        assert ch.send_text(contract['conversation'], text, key='out-1').ok and calls == [] and long_calls == [text]
        return
    got = ch.send_text(contract['conversation'], text)
    assert got.ok, got.error
    assert len(calls) >= 2
    assert all(base.text_units(t, ch.TEXT_UNITS) <= ch.TEXT_LIMIT and c == contract['conversation'] for c, t, _ in calls)
    assert ''.join(t for _, t, _ in calls) == text


@pytest.mark.parametrize('type_', ADAPTER_ROWS)
def test_adapter_card_buttons_or_typed_commands(type_, tmp_path, fake_registered):
    ch, contract, _ = build(type_, tmp_path)
    calls = spy_text(ch)
    c = envelope.card('删除确认', 'rm -rf build/', [{'label': '批准', 'command': '/approve p-0123abcd'},
                                                {'label': '拒绝', 'command': '/reject p-0123abcd'}], ref='p-0123abcd')
    got = ch.send_card(contract['conversation'], c)
    assert got.ok, got.error
    sent = ''.join(t for _, t, _ in calls)
    if not ch.CARRIES_TOKENS:  # exec: an outside command never gets an actionable command or token
        assert '/approve' not in sent and '/reject' not in sent
    elif not (ch.SUPPORTS_CARDS and ch.SUPPORTS_BUTTONS):
        assert '/approve p-0123abcd' in sent and '/reject p-0123abcd' in sent
    assert ch.update_card(contract['conversation'], got.message_id or 'm-0', c).ok


@pytest.mark.parametrize('type_', ADAPTER_ROWS)
def test_adapter_send_file(type_, tmp_path, fake_registered):
    ch, contract, _ = build(type_, tmp_path)
    missing = ch.send_file(contract['conversation'], tmp_path / 'nope.txt')
    assert not missing.ok and 'nope.txt' in missing.error
    f = tmp_path / 'result.txt'
    f.write_text('long result\n' * 50)
    got = ch.send_file(contract['conversation'], f, caption='result')
    if 'files' in ch.PENDING:  # declared as waiting for brief v2: an honest failure, never a fake success
        assert not got.ok and 'pending' in got.error
    else:
        assert got.ok, got.error


@pytest.mark.parametrize('type_', ADAPTER_ROWS)
def test_adapter_errors_never_escape(type_, tmp_path, fake_registered):
    ch, contract, audit = build(type_, tmp_path, transport=boom)
    f = tmp_path / 'a.txt'
    f.write_text('x')
    c = envelope.card('t', 'x', [{'label': 'ok', 'command': '/status'}])
    for got in (ch.send_text(contract['conversation'], 'hello'), ch.send_card(contract['conversation'], c),
                ch.update_card(contract['conversation'], 'm-1', c), ch.send_file(contract['conversation'], f)):
        assert isinstance(got, base.SendResult) and not got.ok and got.error
    assert ch.poll(0) == []
    assert ch.inbound(object()) == []  # garbage from the platform
    ch.close()
    assert 'channel.error' in audit.events() or not ch.RECEIVES


@pytest.mark.parametrize('type_', ADAPTER_ROWS)
def test_adapter_button_press_envelope(type_, tmp_path, fake_registered):
    """A pressed button is the presser's message: sender = who pressed (Telegram callback_query.from.id, Feishu
    operator open_id), private = the chat is verified one-to-one; the same whitelist / private policy applies.
    Required for adapters with SUPPORTS_BUTTONS; also run when a CONTRACT gives the samples."""
    ch, contract, audit = build(type_, tmp_path)
    if not (ch.RECEIVES and (ch.SUPPORTS_BUTTONS or contract.get('inbound_button') is not None)):
        return
    for k in ('inbound_button', 'inbound_button_text', 'inbound_button_foreign', 'inbound_button_group'):
        assert contract.get(k) is not None, 'button-capable adapter CONTRACT needs ' + k
    got = ch.inbound(contract['inbound_button'])
    assert len(got) == 1 and got[0]['action']['command'] == got[0]['text'] == contract['inbound_button_text']
    assert got[0]['sender'] in ch.account.allow and got[0]['private'] is True
    assert ch.inbound(contract['inbound_button_foreign']) == []  # pressed by someone not whitelisted
    assert ch.inbound(contract['inbound_button_group']) == []    # pressed in a group chat
    assert [e for e, _ in audit].count('channel.drop_policy') == 2


@pytest.mark.parametrize('type_', ADAPTER_ROWS)
def test_adapter_error_text_never_carries_a_secret(type_, tmp_path, fake_registered):
    """401 / 409 / 429 and network errors are reported redacted: no account value (token, secret, password)."""
    _, contract = harness(type_)
    secrets = [v for k, v in contract['account'].items() if k != 'ALLOW' and len(str(v)) >= 6]

    def leaky(*args, **kwargs):
        raise OSError('HTTP 401 for https://api.example/bot' + '/'.join(map(str, secrets)) + '/sendMessage')
    ch, contract, audit = build(type_, tmp_path, transport=leaky)
    got = ch.send_text(contract['conversation'], 'hello')
    ch.poll(0)
    text = json.dumps([got.error, ch.last_error, audit, ch.health()], ensure_ascii=False, default=str)
    assert not got.ok and all(s not in text for s in secrets)


@pytest.mark.parametrize('type_', ADAPTER_ROWS)
def test_adapter_optional_dependency_missing(type_, tmp_path, monkeypatch, fake_registered):
    """A missing optional package (feishu: lark-oapi) never breaks the import or the sends; start() says
    MissingDependency and health() reports a known state."""
    cls, _ = harness(type_)
    for dep in cls.OPTIONAL_DEPS:
        monkeypatch.setitem(sys.modules, dep, None)  # import of it now raises ImportError
    if type_ in channels.ADAPTER_MODULES:
        importlib.reload(importlib.import_module(channels.ADAPTER_MODULES[type_]))  # top level: stdlib only
    assert channels.adapter_class(type_) is not None  # the module itself imports without the optional package
    ch, contract, _ = build(type_, tmp_path)
    if ch.OPTIONAL_DEPS:
        with pytest.raises(channels.MissingDependency):
            ch.start()
    assert ch.send_text(contract['conversation'], 'still sends').ok
    assert ch.health()['state'] in channels.INBOUND_STATES
    ch.close()


# ====================================================================== group 1: P0 pieces (green)
def test_p0_registry_and_prefixes():
    assert set(channels.ADAPTER_MODULES) == {'telegram', 'feishu', 'weixin', 'email', 'exec', 'slack'}
    assert set(channels.MESSAGE_ID_PREFIX) == set(channels.ADAPTER_MODULES)
    assert len(set(channels.MESSAGE_ID_PREFIX.values())) == len(channels.MESSAGE_ID_PREFIX)
    assert channels.MESSAGE_ID_PREFIX['telegram'] == 'tg'  # telegram_poll.py's seen-key spelling
    with pytest.raises(channels.Unavailable):
        channels.adapter_class('pigeon')
    undo = channels.register('fake', FakeChannel, 'fake')
    try:
        assert channels.adapter_class('fake') is FakeChannel and 'fake' in channels.known_types()
    finally:
        undo()
    assert 'fake' not in channels.known_types()
    with pytest.raises(ValueError):
        channels.register('Bad Type', FakeChannel, 'x')


def test_p0_push_events():
    assert set(channels.DEFAULT_PUSH) <= set(channels.PUSH_EVENTS)
    assert 'escalation' in channels.PUSH_EVENTS and 'escalation' not in channels.DEFAULT_PUSH
    assert set(channels.OUT_EVENTS) == set(channels.PUSH_EVENTS) | {'reply', 'manual'}
    assert all(re.fullmatch(r'(channel|dialog|security)\.[a-z_.]+', e) for e in channels.AUDIT_EVENTS)


def test_p0_roster_channels_rules():
    entries, errors = channels.parse_channels([{'type': 'telegram', 'account': 'fleet-bot-1'},
                                               {'type': 'email', 'account': 'fleet-mail', 'push': ['task_done']}])
    assert errors == []
    assert entries == [{'type': 'telegram', 'account': 'fleet-bot-1', 'push': list(channels.DEFAULT_PUSH)},
                       {'type': 'email', 'account': 'fleet-mail', 'push': ['task_done']}]
    assert channels.parse_channels(None) == ([], [])
    assert channels.parse_channels([]) == ([], [])
    bad = [
        ('x', 'must be a list'),
        ([{'type': 'telegram', 'account': 'a', 'token': 's3cret'}], 'never in a roster'),
        ([{'type': 'pigeon', 'account': 'a'}], 'type must be one of'),
        ([{'type': 'telegram', 'account': 'Bad Name'}], 'account must match'),
        ([{'type': 'telegram', 'account': 'a', 'push': ['everything']}], 'unknown push event'),
        ([{'type': 'telegram', 'account': 'a', 'push': ['result', 'result']}], 'distinct'),
        ([{'type': 'telegram', 'account': 'a'}, {'type': 'telegram', 'account': 'a'}], 'duplicate channel'),
        ([{'type': 'telegram', 'account': 'a' + str(i)} for i in range(9)], 'at most'),
        (['telegram'], 'must be a mapping'),
    ]
    for value, words in bad:
        entries, errors = channels.parse_channels(value)
        assert entries == [] and any(words in e for e in errors), (value, errors)
    _, errors = channels.parse_channels([{'type': 'telegram', 'account': 'a', 'token': 's3cret'}])
    assert 's3cret' not in ' '.join(errors) and errors[0].startswith('channels[0]')


def test_p0_paths_live_under_the_config_dir(config_dir):
    assert channels.config_dir() == config_dir
    assert channels.account_path('telegram', 'fleet-bot-1') == config_dir / 'channels' / 'telegram-fleet-bot-1.env'
    assert channels.lease_path('telegram', 'fleet-bot-1') == config_dir / 'leases' / 'telegram-fleet-bot-1.lock'
    assert channels.state_path('weixin', 'wx-1') == config_dir / 'state' / 'weixin-wx-1.json'
    assert channels.last_path() == config_dir / 'state' / 'last.json'
    for bad in (('../x', 'a'), ('telegram', '../../etc'), ('telegram', '')):
        with pytest.raises(ValueError):
            channels.account_path(*bad)


def test_p0_account_file_strips_crlf_and_keeps_secrets_out_of_errors(config_dir):
    text = '# fleet bot\r\nBOT_TOKEN = "123:abc"\r\nALLOW=111, 222 333\r\nPUSH_TO=111\r\n\r\n'
    acct = channels.parse_account_text('telegram', 'fleet-bot-1', text)
    assert acct.get('BOT_TOKEN') == '123:abc' and acct.get('PUSH_TO') == '111'
    assert acct.allow == {'111', '222', '333'}
    assert '123:abc' not in repr(acct)
    with pytest.raises(ValueError) as exc:
        channels.parse_account_text('telegram', 'fleet-bot-1', 'BOT_TOKEN=ok\nthis is the s3cret value\n')
    assert 'line 2' in str(exc.value) and 's3cret' not in str(exc.value)
    with pytest.raises(FileNotFoundError):
        channels.load_account('telegram', 'fleet-bot-1')
    path = channels.account_path('telegram', 'fleet-bot-1')
    path.parent.mkdir(parents=True)
    path.write_text(text)
    assert channels.load_account('telegram', 'fleet-bot-1').allow == {'111', '222', '333'}


def test_p0_envelope_validation():
    env = envelope.make('telegram', 'fleet-bot-1', conversation=42, message_id='9:42', sender=111, text='hi',
                        reply_ref={'chat_id': '42'}, at=1762650000)
    assert env['message_id'] == 'tg:9:42' and env['conversation'] == '42' and env['sender'] == '111'
    assert envelope.problems(env) == []
    assert envelope.validate(env) is env
    broken = [
        (dict(env, message_id='9:42'), 'message_id must start with tg:'),
        (dict(env, channel='pigeon'), 'unknown channel'),
        (dict(env, extra=1), 'unknown key extra'),
        ({k: v for k, v in env.items() if k != 'sender'}, 'missing sender'),
        (dict(env, text=''), 'empty message'),
        (dict(env, private='yes'), 'private must be true or false'),
        (dict(env, at=True), 'at must be epoch seconds'),
        (dict(env, reply_ref={'a': [1]}), 'reply_ref'),
        (dict(env, files=[{'name': 'a.png'}]), 'files must be'),
        (dict(env, action={'value': 'x'}), 'action must be'),
        (dict(env, text='x' * 32001), 'text must be a string'),
    ]
    for value, words in broken:
        assert any(words in p for p in envelope.problems(value)), (words, envelope.problems(value))
    with pytest.raises(ValueError):
        envelope.validate(dict(env, extra=1))
    pressed = envelope.make('telegram', 'b', conversation='1', message_id='cb:5', sender='111',
                            text='/approve p-0123abcd', action={'command': '/approve p-0123abcd', 'card_message_id': '8'})
    assert envelope.problems(pressed) == []
    only_file = dict(env, text='', rejected_files=[{'name': 'big.zip', 'reason': 'over 20 MB'}])
    assert envelope.problems(only_file) == []


def test_p0_origin_and_followup_ids():
    env = envelope.make('weixin', 'wx-1', conversation='u@im.wechat', message_id='77', sender='u@im.wechat',
                        text='secret plan', reply_ref={'context_token': 'ct-1'}, at=1762650000)
    origin = envelope.origin_from(env)
    assert set(origin) == set(envelope.ORIGIN_KEYS)
    assert origin['reply_ref'] == {'context_token': 'ct-1'} and 'secret plan' not in json.dumps(origin)
    assert envelope.origin_id('ui-abc.done') == 'ui-abc' and envelope.origin_id('ui-abc') == 'ui-abc'  # C7
    assert not hasattr(envelope, 'origin_candidates')  # replaced by origin_id: no dead alias


def test_p0_outbound_items():
    item = envelope.outbound('result', target='origin', request_id='ui-1', text='done')
    assert envelope.outbound_problem(item) == '' and item['attempts'] == 0 and item['id'].startswith('out-')
    assert json.loads(envelope.dumps(item)) == item
    c = envelope.card('t', 'x', [{'label': 'ok', 'command': '/approve p-0123abcd'}], ref='p-0123abcd')
    assert envelope.outbound('prompt_wait', card=c, user_only=True)['user_only'] is True
    bad = [
        (dict(event='result', text='a', card=c), 'exactly one'),
        (dict(event='result'), 'exactly one'),
        (dict(event='weather', text='a'), 'event must be'),
        (dict(event='result', text='a', target='everyone'), 'target must be'),
        (dict(event='result', text='a', target='origin'), 'needs request_id'),
        (dict(event='manual', text='a', target='explicit', channel='telegram'), 'needs channel, account'),
        (dict(event='manual', text='  '), 'non-empty'),
    ]
    for kwargs, words in bad:
        with pytest.raises(ValueError) as exc:
            envelope.outbound(kwargs.pop('event'), **kwargs)
        assert words in str(exc.value)
    assert envelope.outbound_problem(dict(item, surprise=1)) != ''


def test_p0_card_and_its_text_form():
    c = envelope.card('删除确认', 'worker-1 g2：rm -rf build/', [{'label': '批准', 'command': '/approve p-0123abcd'},
                                                            {'label': '拒绝', 'command': '/reject p-0123abcd'}])
    text = envelope.card_text(c)
    assert text.startswith('删除确认') and '/approve p-0123abcd = 批准' in text and '/reject p-0123abcd = 拒绝' in text
    for bad in ({'title': ''}, {'title': 't', 'buttons': [{'label': 'x', 'command': 'approve'}]},
                {'title': 't', 'color': 'red'}, {'title': 't', 'buttons': [{'label': 'x', 'command': '/s'}] * 7}):
        assert envelope.card_problem(bad)
    with pytest.raises(ValueError):
        envelope.card('', 'x')


def test_p0_command_grammar():
    cases = {
        '  看一下 worker-1 在干嘛  ': {'kind': 'text', 'text': '看一下 worker-1 在干嘛'},
        '/status': {'kind': 'status'},
        '/status@fleet_bot': {'kind': 'status'},
        '/help': {'kind': 'help'},
        '/approve p-0123abcd': {'kind': 'approve', 'id': 'p-0123abcd', 'nonce': ''},  # step 1: no nonce
        '/approve p-0123abcd 0123456789abcdef': {'kind': 'approve', 'id': 'p-0123abcd', 'nonce': '0123456789abcdef'},
        '/reject p-0123abcd 不要删': {'kind': 'reject', 'id': 'p-0123abcd', 'reason': '不要删'},
        '/reject p-0123abcd': {'kind': 'reject', 'id': 'p-0123abcd', 'reason': ''},
        '/clear': {'kind': 'unknown', 'command': 'clear'},
    }
    for text, want in cases.items():
        assert envelope.parse_command(text) == want, text
    for text in ('/approve', '/approve p-0123456789ab', '/approve 123 0123456789abcdef', '/approve 123',
                 '/approve p-0123abcd nonce!', '/approve p-0123456789ab nonce!',
                 '/approve p-0123456789ab 0123456789abcdef extra', '/reject p-0123456789ab', '/reject ../x'):
        assert envelope.parse_command(text)['kind'] == 'invalid', text


def test_p0_receipts_are_fixed_texts():
    assert envelope.receipt('accepted', id='ui-1') == 'Fleet：指令已排队（ui-1），监工会按派活流程处理。'
    assert envelope.receipt('rejected', reason='x') == 'Fleet 输入被拒绝：x'
    assert envelope.receipt('unconfirmed', id='c-1').startswith('Fleet 输入未确认（c-1）')
    assert envelope.receipt('no_operator_input') == 'Fleet：这个 fleet 的监工不收中途输入。'
    fields = dict(id='p-1', reason='r', name='n', command='c', decision='d', seconds=120, resume='r',
                  action='a', rule='x', level='l', worker='dev-1', busy='')  # P8a; §53 直连 worker
    for name in envelope.RECEIPTS:
        assert envelope.receipt(name, **fields)


def test_p0_split_text():
    assert base.split_text('abc', 10) == ['abc']
    assert base.split_text('', 10) == ['']
    text = 'aaaa\nbbbb\ncccc\n' * 20
    parts = base.split_text(text, 23)
    assert ''.join(parts) == text and all(len(p) <= 23 for p in parts)
    assert all(p.endswith('\n') for p in parts[:-1])  # cut at a newline when one is in the second half
    hard = base.split_text('x' * 25, 10)
    assert hard == ['x' * 10, 'x' * 10, 'x' * 5]
    with pytest.raises(ValueError):
        base.split_text('x', 0)


def test_p0_memory_store():
    s = base.MemoryStore()
    assert s.setnx('k', 1, ttl=60) is True and s.setnx('k', 2, ttl=60) is False and s.get('k') == '1'
    s.set('t', 'v', ttl=0.01)
    time.sleep(.02)
    assert s.get('t') is None and s.setnx('t', 'w') is True
    s.delete('k')
    assert s.get('k') is None


class NativeCard(FakeChannel):
    SUPPORTS_BUTTONS = True
    SUPPORTS_FILES = False


def test_p0_base_native_cards_and_text_fallbacks(tmp_path):
    t = _fake_transport()
    ch = NativeCard(channels.Account('fake', 'a', ['u-1']), transport=t)
    c = envelope.card('t', 'x', [{'label': 'ok', 'command': '/status'}])
    assert ch.send_card('c-1', c).ok and t.sent[-1][0] == 'send_card'
    assert ch.update_card('c-1', 'm-1', c).ok and t.sent[-1][0] == 'update_card'
    f = tmp_path / 'r.txt'
    f.write_text('x')
    before = len(t.sent)
    assert ch.send_file('c-1', f, caption='cap').ok and t.sent[-1][0] == 'send_text'  # no files: path as text
    assert '文件：' + str(f) in ''.join(d['text'] for _, d in t.sent[before:])
    assert not ch.send_text('', 'x').ok and not ch.send_text('c-1', '').ok
    assert not ch.send_card('c-1', {'title': ''}).ok
    plain = FakeChannel(channels.Account('fake', 'a', ['u-1']), transport=t)
    plain.SUPPORTS_UPDATE = False
    assert plain.update_card('c-1', 'm-1', c).ok and t.sent[-1][0] == 'send_text'  # buttons spelled out


def test_p0_base_poll_applies_the_same_policy(fake_registered):
    t = _fake_transport()
    audit = Audit()
    ch = FakeChannel(channels.Account('fake', 'a', ['u-1']), transport=t, audit=audit)
    got = ch.poll(5)
    assert [e['text'] for e in got] == ['polled'] and ch.poll(5) == []
    wrong = FakeChannel(channels.Account('fake', 'b', ['u-1']), transport=t, audit=audit)
    wrong.parse_inbound = lambda raw: [dict(envelope.make('fake', 'zzz', conversation='c', message_id='1', sender='u-1',
                                                          text='x'))]
    assert wrong.inbound({}) == [] and audit[-1][0] == 'channel.drop_invalid'
    failing = FakeChannel(channels.Account('fake', 'c', ['u-1']), transport=t,
                          audit=lambda *a, **k: (_ for _ in ()).throw(RuntimeError('sink down')))
    assert failing.inbound({'id': 1}) == []  # a broken audit sink never escapes either


def test_p0_no_business_in_the_channels_package():
    """fleetmux is exported as a public repo: no group ids, open_ids, engine banners or trade wording."""
    pattern = re.compile(r'\b(ou|oc)_[0-9a-f]{12,}|【[^】]*引擎】|STTE|oc-relay|secrets\.env|futures/|tgog', re.I)
    hits = [(p.name, m.group(0)) for p in (ROOT / 'fleetmux' / 'channels').glob('*.py')
            for m in pattern.finditer(p.read_text(encoding='utf-8'))]
    assert hits == []


def test_p0_no_global_proxy_scrubbing_in_the_channels_package():
    """Proxies are scoped per client (requests trust_env=False, websockets proxy=None), never by editing the
    process environment: another channel in the same fleet may need the proxy (notify_bus feishu_ws.py:65)."""
    pattern = re.compile(r'_scrub_proxy_env|os\.environ\.(pop|clear|update|setdefault)\(|del os\.environ'
                         r'|os\.environ\[[^\]]+\]\s*=(?!=)|os\.unsetenv|os\.putenv')
    hits = [(p.name, m.group(0)) for p in (ROOT / 'fleetmux' / 'channels').glob('*.py')
            for m in pattern.finditer(p.read_text(encoding='utf-8'))]
    assert hits == []


class Server:
    """A fake platform that redelivers everything not acknowledged (Telegram getUpdates semantics)."""

    def __init__(self, items):
        self.pending, self.acked, self.flaky = list(items), [], set()


class AckChannel(FakeChannel):
    def _poll(self, timeout):
        return list(self.transport.pending)

    def parse_inbound(self, raw):
        if raw['id'] in self.transport.flaky:
            raise channels.Retryable('download of the attachment failed (HTTP 502)')
        return super().parse_inbound(raw)

    def _ack(self, raw):
        self.transport.pending.remove(raw)
        self.transport.acked.append(raw['id'])


def test_p0_cursor_moves_only_past_final_items():
    items = [{'id': 1, 'chat': 'c-1', 'from': 'u-1', 'text': 'one'},
             {'id': 2, 'chat': 'c-9', 'from': 'u-9', 'text': 'stranger'},   # deterministic reject: acked
             {'id': 3, 'chat': 'c-1', 'from': 'u-1', 'text': 'flaky'},      # transient: blocks itself and later
             {'id': 4, 'chat': 'c-1', 'from': 'u-1', 'text': 'four'}]
    server = Server(items)
    server.flaky.add(3)
    audit = Audit()
    inbox = {}

    def accept(env):  # the actor's atomic seen + enqueue, simulated
        if env['message_id'] in inbox:
            return 'duplicate'
        inbox[env['message_id']] = env
        return 'persisted'
    undo = channels.register('fake', FakeChannel, 'fake')
    try:
        ch = AckChannel(channels.Account('fake', 'a', ['u-1']), transport=server, audit=audit)
        assert [e['text'] for e in ch.poll(1, accept)] == ['one']
        assert server.acked == [1, 2] and [i['id'] for i in server.pending] == [3, 4]
        assert 'channel.inbound_retrying' in audit.events() and ch.health()['state'] == 'retrying'
        assert [e['text'] for e in ch.poll(1, accept)] == []  # still failing: nothing moves, nothing dropped
        server.flaky.clear()
        assert [e['text'] for e in ch.poll(1, accept)] == ['flaky', 'four'] and server.acked == [1, 2, 3, 4]
        assert ch.health()['state'] == 'ok'  # FS-2

        # an accept that cannot write (Redis down) is not a receipt either; the redelivery is then a duplicate
        server2, calls = Server([{'id': 5, 'chat': 'c-1', 'from': 'u-1', 'text': 'five'}]), []

        def flaky_accept(env):
            calls.append(env['message_id'])
            if len(calls) == 1:
                inbox[env['message_id']] = env  # written, but the answer was lost
                raise ConnectionError('redis timeout')
            return accept(env)
        ch2 = AckChannel(channels.Account('fake', 'a', ['u-1']), transport=server2, audit=audit)
        assert ch2.poll(1, flaky_accept) == [] and server2.acked == []
        assert ch2.poll(1, flaky_accept) == [] and server2.acked == [5]  # duplicate -> final -> acked
        assert audit.events().count('channel.drop_dup') >= 1

        # a failing ack stops the batch; the next poll sees the same items as duplicates
        server3 = Server([{'id': 6, 'chat': 'c-1', 'from': 'u-1', 'text': 'six'},
                          {'id': 7, 'chat': 'c-1', 'from': 'u-1', 'text': 'seven'}])
        ch3 = AckChannel(channels.Account('fake', 'a', ['u-1']), transport=server3, audit=audit)
        real_ack = ch3._ack
        ch3._ack = lambda raw: (_ for _ in ()).throw(OSError('state store down'))
        assert [e['text'] for e in ch3.poll(1, accept)] == ['six'] and len(server3.pending) == 2
        ch3._ack = real_ack
        assert [e['text'] for e in ch3.poll(1, accept)] == ['seven'] and server3.acked == [6, 7]
    finally:
        undo()


def test_p0_utf16_split_masking_inbox_files_and_error_kinds(tmp_path):
    text = '😀' * 3000  # 6000 UTF-16 units
    parts = base.split_text(text, 4000, 'utf16')
    assert ''.join(parts) == text and all(base.text_units(p, 'utf16') <= 4000 for p in parts) and len(parts) == 2
    assert base.text_units('a😀', 'utf16') == 3 and base.text_units('a😀') == 2
    acct = channels.Account('fake', 'a', ['u-1'], {'BOT_TOKEN': '123456:SECRET-abc', 'MODE': 'x'})
    ch = FakeChannel(acct, transport=_fake_transport(), inbox_dir=tmp_path / 'inbox')
    assert ch.redact('GET /bot123456:SECRET-abc/getMe x') == 'GET /bot***/getMe x'
    (tmp_path / 'inbox').mkdir()
    (tmp_path / 'inbox' / 'tg-1-a.png').write_bytes(b'old')
    path, fh = ch.new_inbox_file('../../a.png', prefix='tg-1-')
    fh.close()
    assert path.name == 'tg-1-a-2.png' and (tmp_path / 'inbox' / 'tg-1-a.png').read_bytes() == b'old'
    for kind, state in (('auth', 'needs_login'), ('conflict', 'conflict'), ('rate_limited', 'rate_limited'),
                        ('network', 'reconnecting')):
        def failing(op, data, kind=kind):
            raise channels.ChannelError(kind, 'HTTP ' + {'auth': '401', 'conflict': '409'}.get(kind, '429'),
                                        retry_after=3 if kind == 'rate_limited' else None)
        ch2 = FakeChannel(acct, transport=failing)
        assert ch2.poll(0) == [] and ch2.health()['state'] == state
    with pytest.raises(ValueError):
        channels.ChannelError('weird')
    assert isinstance(channels.Retryable('x'), channels.ChannelError) and channels.Retryable('x').kind == 'transient'


class MailLike(FakeChannel):
    TEXT_LIMIT = 100
    OVERFLOW = 'file'

    def _send_text(self, conversation, text, reply_ref):
        return self.transport('send_text', {'to': conversation, 'text': text, 'key': self.send_key})

    def _send_long_text(self, conversation, text, reply_ref):
        return self.transport('send_long', {'to': conversation, 'size': len(text), 'key': self.send_key})


def test_p0_overflow_to_file_and_stable_send_keys():
    t = _fake_transport()
    ch = MailLike(channels.Account('fake', 'a', ['u-1']), transport=t)
    assert ch.send_text('c-1', 'x' * 500, key='out-9').ok
    assert t.sent == [('send_long', {'to': 'c-1', 'size': 500, 'key': 'out-9'})]  # one message, not 5 chunks
    plain = FakeChannel(channels.Account('fake', 'a', ['u-1']), transport=t)
    plain._send_text = lambda c, text, r: t('send_text', {'key': plain.send_key})
    assert plain.send_text('c-1', 'y' * 120, key='out-3').ok
    assert [d['key'] for _, d in t.sent[-3:]] == ['out-3', 'out-3:1', 'out-3:2']  # a retry derives the same keys
    assert plain.send_text('c-1', 'z').ok and t.sent[-1][1]['key'] is None  # direct send: no key


def test_p0_seen_keys_are_per_account():
    undo = channels.register('fake', FakeChannel, 'fake')
    try:
        a = envelope.make('fake', 'bot-1', conversation='1', message_id='7:1', sender='u', text='x', at=1)
        b = dict(a, account='bot-2')
    finally:
        undo()
    assert channels.seen_key(a) == channels.SEEN + 'fake:bot-1:fake:7:1' and channels.seen_key(a) != channels.seen_key(b)


def test_p0_tls_verification_is_never_switched_off():
    """SMTP_SSL / IMAP4_SSL / HTTPS use ssl.create_default_context(): certificate and host name checked."""
    pattern = re.compile(r'CERT_NONE|check_hostname\s*=\s*False|_create_unverified_context|verify\s*=\s*False')
    hits = [(p.name, m.group(0)) for p in (ROOT / 'fleetmux' / 'channels').glob('*.py')
            for m in pattern.finditer(p.read_text(encoding='utf-8'))]
    assert hits == []


class MediaPending(FakeChannel):
    PENDING = ('files', 'inbound_media')


def test_p0_pending_capabilities_and_login_interface(tmp_path):
    t = _fake_transport()
    ch = MediaPending(channels.Account('fake', 'a', ['u-1']), transport=t)
    f = tmp_path / 'a.png'
    f.write_bytes(b'x')
    got = ch.send_file('c-1', f)
    assert not got.ok and 'pending a brief v2 decision' in got.error and t.sent == []
    assert set(MediaPending.PENDING) <= {'files', 'inbound_media'} and base.Channel.PENDING == ()
    with pytest.raises(NotImplementedError):
        FakeChannel.login('a', out=lambda line: None, ask=lambda prompt: '')
    r = base.LoginResult('already_bound', {}, (), 'already bound')
    assert r.status == 'already_bound' and r.allow == ()


def test_p0_push_targets_must_be_private_chats_of_whitelisted_users():
    ch = FakeChannel(channels.Account('fake', 'a', ['111'], {'PUSH_TO': '111'}), transport=_fake_transport())
    assert ch.private_target_ok('111') and not ch.private_target_ok('-100200300') and not ch.private_target_ok('')


class OutsideCommand(FakeChannel):
    """Like exec: reaches an outside command, not the user's private chat."""
    CARRIES_TOKENS = False
    SUPPORTS_BUTTONS = True


def test_p0_approval_tokens_never_reach_a_channel_that_does_not_carry_them():
    token = '0123456789abcdef'
    c = envelope.card('删除确认 worker-1', 'rm -rf build/', [{'label': '批准', 'command': '/approve p-0123abcd ' + token},
                                                         {'label': '拒绝', 'command': '/reject p-0123abcd'}],
                      ref='p-0123abcd')
    assert envelope.redact_card(c) == {'title': '删除确认 worker-1', 'text': 'rm -rf build/', 'buttons': [], 'ref': ''}
    t = _fake_transport()
    ch = OutsideCommand(channels.Account('fake', 'a', ['u-1']), transport=t)
    assert ch.send_card('c-1', c).ok and ch.update_card('c-1', 'm-1', c).ok
    assert token not in json.dumps(t.sent, ensure_ascii=False) and 'p-0123abcd' not in json.dumps(t.sent)
    assert '/approve p-0123abcd ' + token in envelope.card_text(c)  # a token-carrying channel spells it out
    longest = '/approve p-' + 'f' * 8 + ' ' + 'f' * 16  # v2: 8 hex pending id + 16 hex nonce = 36 bytes
    assert len(longest.encode()) <= envelope.COMMAND_MAX_BYTES == 64  # Telegram callback_data
    long = {'title': 't', 'text': '', 'buttons': [{'label': 'x', 'command': '/' + 'a' * 64}], 'ref': ''}
    assert envelope.card_problem(long) == ''  # TG-C2: length is the adapter's limit, not a card rule
    t2 = _fake_transport()
    nc = NativeCard(channels.Account('fake', 'a', ['u-1']), transport=t2)
    assert nc.send_card('c-1', long).ok and t2.sent[-1][0] == 'send_card'  # handed to _send_card, not refused
    assert (channels.APPROVE_CONFIRM_S, channels.APPROVE_TTL_S) == (120, 1800)
    assert channels.PENDING == channels.KEY_PREFIX + 'pending:'
    assert channels.PENDING_SLOT == channels.KEY_PREFIX + 'pending_slot:'
    assert not hasattr(channels, 'APPROVE') and not hasattr(channels, 'APPROVE_SLOT')  # no dead alias of the v1 keys


def test_p0_missing_dependency_and_origin_checks():
    exc = channels.MissingDependency('lark-oapi', 'inbound off')
    assert isinstance(exc, channels.Unavailable) and exc.package == 'lark-oapi' and 'not installed' in str(exc)
    ch = FakeChannel(channels.Account('fake', 'a', ['u-1']), transport=_fake_transport())
    with pytest.raises(channels.MissingDependency):
        ch.start()
    assert ch.require('json').dumps({}) == '{}'
    assert ch.health()['state'] == 'ok'  # FS-2: the healthy state is 'ok'
    assert set(channels.INBOUND_STATES) >= {'ok', 'reconnecting', 'needs_login', 'missing_dependency'}
    undo = channels.register('fake', FakeChannel, 'fake')
    try:
        origin = envelope.origin_from(envelope.make('fake', 'a', conversation='c', message_id='1', sender='u-1',
                                                    text='x', at=1762650000))
    finally:
        undo()
    assert envelope.origin_problem(origin) == ''
    for bad in (dict(origin, text='x'), dict(origin, conversation=''), dict(origin, reply_ref={'a': [1]}),
                dict(origin, at='now'), 'origin'):
        assert envelope.origin_problem(bad)
    assert channels.ACTOR_EXIT_CONFIG != channels.ACTOR_EXIT_LEASE
    assert channels.ACTOR == channels.KEY_PREFIX + 'actor'  # C13: one fleet, one actor hash (pid / started / beat)


def test_p0_contracts_and_design_document_the_freeze():
    contracts = (ROOT / 'docs' / 'contracts.md').read_text(encoding='utf-8')
    design = (ROOT / 'docs' / 'design.md').read_text(encoding='utf-8')
    assert '## 8. Channels' in contracts and '## 53. Channels' in design
    for word in ('fx:channel:origin:', 'fx:channel:outq', 'fx:channel:last', 'channel login', 'channel send',
                 'channel status', 'channel test', 'FLEETMUX_CONFIG_DIR', 'leases/', 'FLEETMUX_TG_CMD',
                 'fx:channel:pending:', 'CONTRACT', 'YES_CURSOR', 'approve_confirm_s',
                 'callback_data', 'CARRIES_TOKENS', 'inbound_button', 'UIDVALIDITY', 'OVERFLOW',
                 'create_default_context', 'RFC 8601', 'context_token', 'redirect_host',
                 'X-WECHAT-UIN', 'fleetmux/<version>', 'LoginResult', 'PENDING', 'PUSH_TO', 'channel.lease_busy', 'MissingDependency',
                 'optional_python_packages', 'card.action.trigger', 'trust_env=False', 'proxy=None',
                 'channel actor <config.json>', 'fx:channel:outq:<type>:<account>',
                 'channel_supervise', 'plan_processes'):
        assert word in contracts, word
    for name in channels.PUSH_EVENTS + channels.AUDIT_EVENTS:
        assert name in contracts, name
    section8 = contracts.split('## 8. Channels', 1)[1].split('\n## ', 1)[0]  # absence checks: the channel section only
    assert 'fx:channel:approve:' not in section8  # v1 key family is gone
    assert 'channel actor <config.json> <type> <account>' not in section8  # C13: no per-account actor


def test_p0_process_model_is_approved_with_configurable_supervise_defaults():
    """Supervisor-approved process model: one actor child per fleet (C13), daemon never imports an adapter."""
    import subprocess
    assert channels.SUPERVISE_DEFAULTS == {'backoff_start_s': 30, 'backoff_cap_s': 300, 'stable_reset_s': 600,
                                           'failing_after': 5, 'lease_retry_s': 300, 'config_retry_s': 600}
    assert channels.SUPERVISE_DEFAULTS['config_retry_s'] == channels.CONFIG_RETRY_S
    contracts = (ROOT / 'docs' / 'contracts.md').read_text(encoding='utf-8')
    design = (ROOT / 'docs' / 'design.md').read_text(encoding='utf-8')
    assert 'pending supervisor review' not in contracts and 'pending supervisor' not in design
    assert 'approved by the supervisor' in contracts and 'approved by the supervisor' in design
    # importing the frozen interface (what the daemon imports) loads no adapter module and no optional dependency
    probe = ('import sys, fleetmux.channels, fleetmux.channels.base, fleetmux.channels.envelope;'
             'print(sorted(m for m in sys.modules if m in set(fleetmux.channels.ADAPTER_MODULES.values())'
             ' or m.split(".")[0] in ("lark_oapi", "websockets")))')
    out = subprocess.run([sys.executable, '-c', probe], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == '[]'


def test_p0_exec_command_is_a_verifiable_argv_array():
    """C10 (brief v2 §2.1-1, supervisor ruling): exec COMMAND is split with shlex (POSIX) into argv and run with
    shell=False: no shell, no expansion; argv[0] may be a plain command found on PATH (no absolute-path rule);
    the account file strips one matching pair of outer quotes only."""
    assert channels.exec_argv('/usr/local/bin/relay --to "$HOME" ~/x "*"') == [
        '/usr/local/bin/relay', '--to', '$HOME', '~/x', '*']  # literal: no env / tilde / glob expansion
    text = 'COMMAND=/opt/relay/send --url https://relay.example/tok-secret-123\nTIMEOUT_S=12\n'
    acct = channels.parse_account_text('exec', 'a', text)
    assert channels.exec_argv(acct.values['COMMAND'])[0] == '/opt/relay/send'
    assert channels.exec_timeout(acct.values['TIMEOUT_S']) == 12
    quoted = channels.parse_account_text('exec', 'a', "COMMAND=/bin/relay --x 'a b;c'\n")
    assert channels.exec_argv(quoted.values['COMMAND']) == ['/bin/relay', '--x', 'a b;c']  # inner quotes kept
    assert channels.exec_argv('"/bin/x"') == ['/bin/x']  # a quoted word is one argument
    assert channels.exec_argv('relay --url https://relay.example/tok-secret-123') == [
        'relay', '--url', 'https://relay.example/tok-secret-123']  # a plain PATH command is allowed (v1 refused it)
    assert channels.exec_timeout(acct.values.get('NOPE')) == channels.EXEC_TIMEOUT_S == 30
    for bad in ('', '   ', '/bin/x --to "$HOME', "/bin/relay --url 'https://relay.example/tok-secret-123",
                '/bin/x a\x00b'):  # empty, unbalanced quotes, NUL
        with pytest.raises(ValueError) as err:
            channels.exec_argv(bad)
        assert 'tok-secret-123' not in str(err.value) and 'HOME' not in str(err.value)
    for bad in ('0', '-1', 'nan', 'inf', 'soon'):
        with pytest.raises(ValueError):
            channels.exec_timeout(bad)
    contracts = (ROOT / 'docs' / 'contracts.md').read_text(encoding='utf-8')
    section8 = contracts.split('## 8. Channels', 1)[1].split('\n## ', 1)[0]
    assert 'shlex' in section8 and 'channels.exec_argv' in section8 and 'shell=False' in section8


def test_p0_approval_limits_are_approved_defaults_and_approve_module_is_a_proposal():
    contracts = (ROOT / 'docs' / 'contracts.md').read_text(encoding='utf-8')
    assert 'configurable default proposal, pending P0 review' not in contracts
    assert 'configurable positive default' in contracts and 'v2 test-first' in contracts
    section8 = contracts.split('## 8. Channels', 1)[1].split('\n## ', 1)[0]  # the channel section only
    assert 'file ownership pending formal v2 assignment' not in section8
    assert (channels.APPROVE_CONFIRM_S, channels.APPROVE_TTL_S) == (120, 1800)
    # Ownership and write responsibility (P6b): the pending records belong to channels/approve.py, are registered
    # through approve.register, and the slot index is written by approve.register. Reading them is not restricted.
    rows = {line.split('|')[1].strip(): line for line in section8.splitlines() if line.startswith('| `fx:channel:pending')}
    assert 'owned by P6b, `channels/approve.py`' in rows['`fx:channel:pending:<pending id>`']
    assert '`approve.register`' in rows['`fx:channel:pending:<pending id>`']
    assert rows['`fx:channel:pending_slot:<slot>`'].rstrip().rstrip('|').strip().endswith('`approve.register`')
    assert 'calls `channels.approve.register(...)` to write `fx:channel:pending:<p-id>`' in section8


def test_p0b_errors_and_audit_mask_account_values():
    """P0b ① (brief v2 §6.1): channels.redact masks account values, `Bearer …`, `bot<token>` URL segments and the
    values of secret-named fields in every error / health / audit string; counts stay. I2 (approved plan A):
    Channel.fleet_name / send_ref default metadata, channels.fleet_label(cfg) never falls back to the account."""
    account = channels.Account('fake', 'a', ['u-1'], {'BOT_TOKEN': '123:FAKE', 'APP_SECRET': 's3cr3t'})
    audit = Audit()

    def transport(op, data):
        raise OSError('https://api/bot123:FAKE/x 401 APP_SECRET=s3cr3t')
    ch = FakeChannel(account, transport=transport, audit=audit)
    res = ch.send_text('c-1', 'hello')
    texts = [res.error, ch.health()['error'], ch.last_error] + [str(v) for _, f in audit for v in f.values()]
    assert not res.ok
    for text in texts:
        assert '123:FAKE' not in text and 's3cr3t' not in text, text
    assert '***' in res.error and '***' in ch.health()['error']
    masked = channels.redact('Authorization: Bearer abc')
    assert '***' in masked and 'abc' not in masked
    assert channels.redact({'tokens': 5}) == {'tokens': 5}  # a count is not a secret
    assert channels.redact({'api_token': 'zzz-123456'}) == {'api_token': '***'}
    assert ch.fleet_name == '' and ch.send_ref is None  # I2 defaults; the constructor is unchanged
    assert channels.fleet_label({'name': 'a', 'token': 't', 'run': '/x/r1'}) == 'a'
    assert channels.fleet_label({'token': 't', 'run': '/x/r1'}) == 't'
    assert channels.fleet_label({'run': '/x/r1'}) == 'r1'
    assert channels.fleet_label({}) == '' and channels.fleet_label(None) == ''


def test_p0b_roster_allows_one_feishu():
    """P0b ② (brief v2 §6.1, FS-3): lark keeps one WebSocket per process, so a roster has at most one feishu."""
    entries, errors = channels.parse_channels([{'type': 'feishu', 'account': 'fs-a'},
                                               {'type': 'feishu', 'account': 'fs-b'}])
    assert entries == [] and len(errors) == 1
    assert 'lark' in errors[0].lower() and 'ws' in errors[0].lower()
    entries, errors = channels.parse_channels([{'type': 'feishu', 'account': 'fs-a'},
                                               {'type': 'telegram', 'account': 'fleet-bot-1'}])
    assert errors == [] and [e['type'] for e in entries] == ['feishu', 'telegram']


def test_p0b_docs_carry_the_follow_up_decisions():
    """P0b ③ (brief v2 §6.1 + supervisor rulings): contracts §8 and design §53 carry the follow-up decisions.
    Checks read only those two channel sections (never the whole docs)."""
    contracts = (ROOT / 'docs' / 'contracts.md').read_text(encoding='utf-8')
    design = (ROOT / 'docs' / 'design.md').read_text(encoding='utf-8')
    section8 = contracts.split('## 8. Channels', 1)[1].split('\n## ', 1)[0]
    section48 = design.split('## 53. Channels', 1)[1].split('\n## ', 1)[0]
    docs = section8 + section48
    for word in ('user_approve',  # P6c registration table (6b879bd §8 Q1: proposed name of the explicit table)
                 '`p-` + 8 hex digits', '16 hex digits',  # pending id and nonce
                 'run_closed', 'stop tail',  # the daemon stop tail is the second source of run_closed
                 'ORIGIN_GRACE_S',  # the 60 s origin grace
                 'fx:escalations',  # the escalation push data source
                 'lifecycle=None', '<id>.done',  # P7: split children, close on accepted, follow-up id
                 'plain text',  # weixin sends plain text
                 'LOGIN', 'ws_factory', 'status(fields)',  # login hook, feishu ws injection and status sink
                 'sending', 'sending_at', 'drain', 'drained', 'separate thread', 'getppid'):  # actor hash, heartbeat, parent
        assert word in docs, word
    assert 'needs_login' in section8  # in the status table
    # supervisor M7 (2026-11-08): delete approval follows the reviewed 6b879bd contracts §8 (Q1 / Q2) —
    # only an explicitly registered consent key, never YES_CURSOR -> Enter, no automatic resume after the key
    assert 'Consent keys come only from explicit registration' in section8
    assert 'No automatic resume of dispatch' in section8
    assert all('never' in line for line in section8.splitlines() if 'YES_CURSOR' in line), 'YES_CURSOR only as a ban'
    assert 'prompt_only' not in section8  # no auto-resume mode in the channel section
    for name in ('channel.cursor_reset', 'channel.lease_reclaimed'):
        assert name in channels.AUDIT_EVENTS, name


# ====================================================================== P8a: the security contract (brief v2.2 §8.4)
def test_p8a_envelope_accepts_provenance_and_text_is_user_typed_only():
    """P8a ① (brief v2.2 §8.2): outside content goes to ``provenance``; ``text`` is only what the user typed."""
    assert envelope.PROVENANCE_KINDS == ('forwarded', 'quoted', 'email_body', 'attachment', 'contact_card',
                                         'location', 'caption')
    env = envelope.make('telegram', 'fleet-bot-1', conversation=42, message_id='9:42', sender=111, text='',
                        provenance=[{'kind': 'email_body', 'text': 'the mail body'}], at=1762650000)
    assert env['text'] == '' and envelope.problems(env) == []      # outside content alone is not an empty message
    assert envelope.problems(dict(env, provenance=[{'kind': 'contact_card', 'path': '/inbox/c.vcf', 'name': 'c'}])) == []
    for bad in ([{'kind': 'pigeon', 'text': 'x'}],                  # unknown kind
                [{'kind': 'quoted'}],                               # neither text nor path
                {'kind': 'quoted', 'text': 'x'},                    # not a list
                [{'kind': 'quoted', 'text': 'x', 'extra': 1}]):     # unknown field
        problems = envelope.problems(dict(env, provenance=bad))
        assert len(problems) == 1 and 'provenance' in problems[0], (bad, problems)
    empty = dict(env, provenance=[])                                # text, files, action, provenance all empty
    assert any('empty message' in p for p in envelope.problems(empty))
    plain = envelope.make('telegram', 'fleet-bot-1', conversation=42, message_id='9:43', sender=111, text='hi',
                          at=1762650000)
    assert 'provenance' not in plain and envelope.problems(plain) == []   # no outside content: the P0 shape


def test_p8a_stop_command_and_approve_third_arg():
    """P8a ② (brief v2.2 §8.2): /stop [reason]; /approve <p-id> <nonce> <6 digits> adds the TOTP code (P8l)."""
    assert 'stop' in envelope.COMMANDS
    assert envelope.parse_command('/stop 别动了') == {'kind': 'stop', 'reason': '别动了'}
    assert envelope.parse_command('/stop') == {'kind': 'stop', 'reason': ''}
    full = '/approve p-0123abcd 0123456789abcdef 123456'
    cmd = envelope.parse_command(full)
    assert cmd['kind'] == 'approve' and cmd['id'] == 'p-0123abcd' and cmd['nonce'] == '0123456789abcdef'
    assert cmd['code'] == '123456'
    assert len(full.encode()) == 43 <= envelope.COMMAND_MAX_BYTES   # brief: 36 + 7 = 43 bytes, a button still fits
    two = envelope.parse_command('/approve p-0123abcd 0123456789abcdef')
    assert two == {'kind': 'approve', 'id': 'p-0123abcd', 'nonce': '0123456789abcdef'}   # no third arg: P0 shape
    for bad in ('/approve p-0123abcd 0123456789abcdef 12345a', '/approve p-0123abcd 0123456789abcdef 1234567',
                '/approve p-0123abcd 0123456789abcdef 12345', '/approve p-0123abcd 123456',
                '/approve p-0123abcd 0123456789abcdef 123456 x'):
        assert envelope.parse_command(bad)['kind'] == 'invalid', bad


P8A_AUDIT = ('security.stranger_alert', 'security.untrusted_request', 'security.reader_summary',
             'security.reader_failed', 'security.secret_blocked', 'security.link_rewritten', 'security.canary_tripped',
             'security.action_pending', 'security.action_approved', 'security.action_denied',
             'security.action_timeout', 'security.oob_refused', 'security.stop', 'security.limit_tripped',
             'security.audit_chain', 'security.sandbox_off')   # brief v2.2 §8.2 ④, the 16 names
P8A_RECEIPTS = ('stopped', 'stop_failed', 'secret_blocked', 'rate_limited', 'confirm_action', 'action_taken',
                'oob_required', 'untrusted_notice')             # brief v2.2 §8.2 ⑤


def test_p8a_security_push_event_default_on_and_audit_names():
    """P8a ③ ④ ⑤: the ``security`` push event is on by default; 16 security.* audit names; 8 fixed receipts."""
    assert 'security' in channels.PUSH_EVENTS and 'security' in channels.DEFAULT_PUSH
    assert 'security' in channels.OUT_EVENTS
    assert len(P8A_AUDIT) == 16 and set(P8A_AUDIT) <= set(channels.AUDIT_EVENTS)
    assert len(set(channels.AUDIT_EVENTS)) == len(channels.AUDIT_EVENTS)
    fields = dict(id='p-1', reason='r', decision='d', seconds=5, resume='fleetmux resume', action='A', rule='x',
                  level='l')
    for name in P8A_RECEIPTS:
        template = envelope.RECEIPTS[name]
        assert set(re.findall(r'\{(\w+)\}', template)) <= set(fields), name   # only fixed placeholders
        text = envelope.receipt(name, **fields)
        assert text and '{' not in text, name
    assert envelope.receipt('confirm_action', action='A', seconds=1) == '确认执行？\nA\n再按一次才生效（1 秒内）'


def test_p8a_parse_security_defaults_and_rejections():
    """P8a ⑦ (brief v2.2 §8.1 table; the six user points take the notes' recommended values, all parameters)."""
    from fleetmux.channels import security
    expected = {   # brief v2.2 §8.1, row by row
        'provenance': {'spotlight': 'on'},
        'reader': {'mode': 'on', 'model': None, 'timeout_s': 120, 'max_chars': 32000, 'raw_to_supervisor': 'off'},
        'untrusted_actions': 'confirm',
        'strangers': {'alert_after': 5, 'window_s': 600, 'cooldown_s': 3600},
        'secrets': {'rules': 'builtin', 'values_from': ['accounts'], 'variants': ['base64', 'hex', 'url'],
                    'on_hit': 'block'},
        'links': {'allow': [], 'mode': 'defang', 'preview': 'off'},                        # C3 (user point 5)
        'canary': {'enabled': 'off', 'path': 'workspace/.secrets/canary.env'},
        'confirm': {'wait_s': 240, 'ticket_ttl_s': 1800,
                    'classes': {'delete': 'normal', 'send_out': 'normal', 'config': 'normal', 'install': 'normal',
                                'secret_read': 'normal', 'real_money': 'high'},           # D2 (user point 3)
                    'patterns': {}, 'high': 'desk'},                                       # A3 (user point 1)
        'stop': {'level': 'interrupt'},                                                    # E2 (user point 4)
        'limits': {'inbound_per_sender_10m': 60, 'requests_per_h': 30, 'outbound_per_h': 300,
                   'security_alerts_per_h': 10, 'tool_calls_per_delivery': 400, 'on_trip': 'pause'},  # E3 (user point 6)
        'audit_log': {'enabled': 'on', 'dir': 'audit', 'uappend': 'on',
                      'events': ['channel.*', 'security.*', 'dialog.*', 'execution.*', 'hook.*', 'service.*']},
        'sandbox': {'mode': 'off', 'allow_domains': [], 'deny_read': [], 'memory': 'off', 'codex_hooks': 'off'},  # C4 (user point 2)
    }
    assert security.DEFAULTS == expected
    for text in ('', '\n', '# nothing\n'):
        assert security.parse_security(text) == (expected, []), text
    policy, errors = security.parse_security('confirm: {high: totp}\nstop: {level: kill}\nsandbox: {mode: claude_native}\n'
                                             'links: {allow: ["*"]}\nlimits: {on_trip: alert}\n')
    assert errors == [] and policy['confirm']['high'] == 'totp' and policy['stop']['level'] == 'kill'
    assert policy['sandbox']['mode'] == 'claude_native' and policy['links']['allow'] == ['*']   # parameters, not fixed
    assert security.DEFAULTS == expected                                                     # defaults untouched
    for text, words in (('confirm: {wait_s: 300}\n', ['stall_window']),
                        ('confirm: {classes: {real_money: medium}}\n', ['off', 'normal', 'high']),
                        ('limits: {max_usd: 98765}\n', ['budget']),
                        ('sandbox: {mode: seatbelt}\n', ['off', 'claude_native', 'os'])):
        policy, errors = security.parse_security(text)
        assert policy is None and errors, text
        joined = ' '.join(errors)
        assert all(w in joined for w in words), (text, errors)
        for value in ('300', 'medium', '98765', 'seatbelt'):                                  # never echo the value
            if value in text:
                assert value not in joined, (text, errors)
    assert security.parse_security('confirm: {wait_s: 100}\n', stall_window=90)[0] is None   # the run's stall_window
    assert security.policy({}) is None and security.policy({'security': {}}) is None        # no file: as before
    assert security.policy({'security': expected}) == expected


def test_p8a_docs_carry_the_security_decisions():
    """P8a ⑧: contracts §8 and design §53 carry the security pack (checks read only those two sections)."""
    contracts = (ROOT / 'docs' / 'contracts.md').read_text(encoding='utf-8')
    design = (ROOT / 'docs' / 'design.md').read_text(encoding='utf-8')
    section8 = contracts.split('## 8. Channels', 1)[1].split('\n## ', 1)[0]
    section48 = design.split('## 53. Channels', 1)[1].split('\n## ', 1)[0]
    for word in ('security.yaml', 'provenance', '/stop', 'fx:guard:ticket', 'action_hash', 'claude-pretool',
                 'chain-', 'desk', 'second_channel', 'totp', 'a fleet without security.yaml behaves exactly as before'):
        assert word in section8, word
    assert 'security.yaml' in section48 and 'a fleet without security.yaml behaves exactly as before' in section48
    for name in P8A_AUDIT + ('security',):
        assert name in section8, name


# ====================================================================== group 3: red skeletons of later packages
WORKERS_YAML = 'workers:\n  - name: worker-1\n'


def test_red_p1_roster_accepts_channels(tmp_path):
    from fleetmux import roster
    roster.parse_entries(WORKERS_YAML + 'channels:\n  - {type: telegram, account: fleet-bot-1, push: [result]}\n',
                         tmp_path)
    with pytest.raises(ValueError) as exc:
        roster.parse_entries(WORKERS_YAML + 'channels:\n  - {type: pigeon, account: a}\n', tmp_path)
    assert 'channels[0]' in str(exc.value)


def test_red_p1_lease_is_exclusive_per_account():
    from fleetmux.channels import lease
    a = lease.acquire('telegram', 'fleet-bot-1', run_id='r1', run='/runs/r1')
    assert a is not None and channels.lease_path('telegram', 'fleet-bot-1').exists()
    assert lease.acquire('telegram', 'fleet-bot-1', run_id='r2', run='/runs/r2') is None
    assert lease.holder('telegram', 'fleet-bot-1')['run_id'] == 'r1'
    other = lease.acquire('telegram', 'fleet-bot-2', run_id='r2', run='/runs/r2')
    assert other is not None
    a.release()
    other.release()
    b = lease.acquire('telegram', 'fleet-bot-1', run_id='r2', run='/runs/r2')
    assert b is not None
    b.release()


def test_red_p1_throttle_is_the_ported_one():
    from fleetmux.channels import throttle
    assert (throttle.STORM_WINDOW, throttle.STORM_THRESHOLD, throttle.THROTTLE_WINDOW) == (120, 12, 5)
    assert throttle.KEY_PREFIX.startswith(channels.KEY_PREFIX)


def test_red_p1_queue_retries_then_dead_letters(private_redis_v2):
    from fleetmux.channels import queue
    r = private_redis_v2
    r.flushdb()
    item = envelope.outbound('manual', text='hello', target='explicit', channel='telegram', account='a',
                             conversation='1')
    queue.enqueue(r, item)
    waits = []
    out = queue.process_one(r, lambda it: base.SendResult(ok=False, error='down'), sleep=waits.append, block=1)
    assert out['status'] == 'dead' and waits == [2, 8]  # notify_bus consumer: two retries, 2 s and 8 s
    dead = json.loads(r.lindex(channels.DEAD, 0))
    assert dead['id'] == item['id'] and dead['attempts'] == 3 and 'down' in dead['dead_reason']
    queue.enqueue(r, item)
    assert queue.process_one(r, lambda it: base.SendResult(ok=True, message_id='9'), sleep=waits.append,
                             block=1)['status'] == 'sent'


def _env(text, **kw):
    return envelope.make('telegram', 'fleet-bot-1', conversation='42', message_id=str(time.time_ns()) + ':42',
                         sender='111', text=text, reply_ref={'chat_id': '42'}, **kw)


def test_red_p1_inbound_maps_to_operator_input(private_redis_v2):
    from fleetmux.channels import actor
    r = private_redis_v2
    r.flushdb()
    asked, replies = [], []

    def submit(text, origin):
        asked.append((text, origin))
        return {'status': 'accepted', 'id': 'ui-1'}
    env = _env('看一下 worker-1')
    actor.handle_inbound(r, env, submit=submit, reply=lambda e, text: replies.append(text), audit=Audit())
    assert asked == [('看一下 worker-1', envelope.origin_from(env))]  # the origin rides on the operator_input event
    assert json.loads(r.get(channels.ORIGIN + 'ui-1')) == envelope.origin_from(env)  # C8: the actor writes it
    assert 0 < r.ttl(channels.ORIGIN + 'ui-1') <= channels.ORIGIN_TTL == 7 * 86400  # 7 days from acceptance
    assert r.hget(channels.LAST, 'conversation') == '42' and r.hget(channels.LAST, 'channel') == 'telegram'
    assert replies[-1] == envelope.receipt('accepted', id='ui-1')
    actor.handle_inbound(r, _env('again'), submit=lambda t, o: {'status': 'rejected', 'reason': 'not ready'},
                         reply=lambda e, text: replies.append(text), audit=Audit())
    assert replies[-1] == envelope.receipt('rejected', reason='not ready')
    n = len(asked)
    for text, words in (('/status', ''), ('/bogus', '/bogus'), ('/help', '/status')):
        actor.handle_inbound(r, _env(text), submit=submit, reply=lambda e, t: replies.append(t), audit=Audit())
        assert words in replies[-1]
    assert len(asked) == n  # commands never become operator input
    actor.handle_inbound(r, _env('', rejected_files=[{'name': 'big.zip', 'reason': 'over 20 MB'}]),
                         submit=submit, reply=lambda e, t: replies.append(t), audit=Audit())
    assert replies[-1] == envelope.receipt('file_refused', name='big.zip', reason='over 20 MB')


def test_red_p1_operator_input_stores_the_origin_with_the_request(service, monkeypatch):
    """C8: the actor is the only origin writer (after the accepted reply). Fleet.handle accepts operator_input
    with a channel source, or with an origin field from an older client, and never writes fx:channel:origin:*."""
    from fleetmux import fleet, runtime_v2
    f = service
    monkeypatch.setattr(fleet, 'prompt_hook_slot', lambda *a, **k: True)  # stand in for a hook / pi supervisor
    monkeypatch.setattr(fleet, 'operator_channel_problem', lambda cfg: None)  # main §48: same stand-in at the gate
    info = f.agent('supervisor')
    undo = channels.register('fake', FakeChannel, 'fake')
    try:
        origin = envelope.origin_from(envelope.make('fake', 'a', conversation='c-1', message_id='1', sender='u-1',
                                                    text='hi', at=1762650000))
    finally:
        undo()

    def post(cid, **extra):
        runtime_v2.post_event(f.v2.bus, dict(kind='operator_input', agent='supervisor', generation=int(info['generation']),
                                             run_id=f.v2.bus.run_id, thread=info.get('thread'), text='hi',
                                             request_id=cid, **extra), cid)
        assert f.v2.step(10)
        return runtime_v2.wait_reply(f.v2.bus, cid, timeout=2)
    answer = post('c-1', source='channel:fake:a')
    assert answer['status'] == 'accepted' and f.r.get(channels.ORIGIN + answer['id']) is None
    old = post('c-2', origin=origin)  # an older client still sends origin: still no second writer
    assert old['status'] == 'accepted' and f.r.get(channels.ORIGIN + old['id']) is None
    plain = post('c-3')  # the supervisor's own input box: unchanged, no origin key
    assert plain['status'] == 'accepted' and f.r.get(channels.ORIGIN + plain['id']) is None


def test_red_p1_inbox_accept_is_atomic_and_replay_is_idempotent(private_redis_v2):
    """C14 (supervisor ruling; core-1 test-plan §6): no inbox stream. On the actor path actor.handle_inbound is the
    accept callback and the single owner of seen:<message_id> in the account's actor.RedisStore (SET NX,
    SEEN_TTL = 1 hour): only a claim while the fleet is asked. The platform cursor is acked only when the item is
    final: accepted, or a deterministic rejection. Unknown outcome (unconfirmed / submit error) -> forget +
    Retryable, no ack, retried later under the same request id. An accepted item replayed is a duplicate and is
    not submitted again. Message ids repeat across accounts: not a duplicate. (Name kept from v1; no
    inbox_accept / event_id API is required.)"""
    from fleetmux.channels import actor
    r = private_redis_v2
    r.flushdb()
    acked = []

    class Acking(FakeChannel):
        def _ack(self, raw):
            acked.append((self.account.name, raw['id']))

    def chan(account):
        return Acking(channels.Account('fake', account, ['u-1']), transport=_fake_transport(),
                      store=actor.RedisStore(r, 'fake', account))
    raw = {'id': 41, 'chat': 'c-1', 'from': 'u-1', 'text': 'hello'}
    calls, claimed, replies, answers = [], [], [], []

    def seen_of(account, item):  # the account store's seen key for that item's message id
        return channels.STORE + 'fake:' + account + ':seen:' + chan(account).parse_inbound(item)[0]['message_id']

    def submit(text, origin):
        calls.append(text)
        claimed.append(bool(r.exists(channels.STORE + 'fake:' + origin['account'] + ':seen:' + origin['message_id'])))
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def accept(env):
        return actor.handle_inbound(r, env, submit=submit, reply=lambda e, text: replies.append(text), audit=Audit())
    undo = channels.register('fake', FakeChannel, 'fake')
    try:
        ch, seen = chan('bot-1'), seen_of('bot-1', raw)
        answers[:] = [{'status': 'pending'}, RuntimeError('fleet unreachable'), {'status': 'accepted', 'id': 'ui-9'}]
        assert ch.inbound(raw, accept=accept) == []  # unconfirmed: claim rolled back, Retryable, cursor not acked
        assert acked == [] and not r.exists(seen) and ch.health()['state'] == 'retrying'
        unconfirmed = replies[-1]
        assert unconfirmed != envelope.receipt('unconfirmed', id='')  # the receipt names the request id
        assert ch.inbound(dict(raw), accept=accept) == [] and acked == [] and not r.exists(seen)  # submit error
        out = ch.inbound(dict(raw), accept=accept)  # third try: accepted -> final -> acked
        assert [e['text'] for e in out] == ['hello'] and acked == [('bot-1', 41)]
        assert calls == ['hello'] * 3 and claimed == [True] * 3  # one claim per try, held while the fleet is asked
        assert 0 < r.ttl(seen) <= channels.SEEN_TTL == 3600
        assert ch.inbound(dict(raw), accept=accept) == [] and len(calls) == 3  # replay: duplicate, not resubmitted
        assert acked == [('bot-1', 41)] * 2  # a duplicate is final too

        answers[:] = [{'status': 'pending'}, {'status': 'pending'}, {'status': 'accepted', 'id': 'ui-10'}]
        other = chan('bot-2')  # same message id on another account: not a duplicate, its own claim and id
        assert other.inbound(dict(raw), accept=accept) == [] and other.inbound(dict(raw), accept=accept) == []
        assert replies[-1] == replies[-2] != unconfirmed  # same request id on retry; differs across accounts
        assert [e['text'] for e in other.inbound(dict(raw), accept=accept)] == ['hello']
        assert r.exists(seen_of('bot-2', raw)) and acked[-1] == ('bot-2', 41)

        answers[:] = [{'status': 'rejected', 'reason': 'not ready'}]
        rejected = {'id': 42, 'chat': 'c-1', 'from': 'u-1', 'text': 'later'}
        ch.inbound(rejected, accept=accept)  # a deterministic rejection is final: receipt, forget, ack
        assert replies[-1] == envelope.receipt('rejected', reason='not ready')
        assert acked[-1] == ('bot-1', 42) and not r.exists(seen_of('bot-1', rejected))
    finally:
        undo()
    assert r.keys(channels.INBOX + '*') == []  # no inbox stream (C14)


def test_red_p1_results_go_back_to_their_origin_once(private_redis_v2):
    """A finished request goes back to its origin once (C8: the origin carries its 7-day expiry from acceptance;
    pushing does not delete it). A result found without an origin is waited for ORIGIN_GRACE_S, then treated as
    an outside request: marked pushed, not sent. <id>.done goes to <id>'s origin via envelope.origin_id (C7)."""
    from fleetmux.channels import actor
    r = private_redis_v2
    r.flushdb()
    origin = envelope.origin_from(_env('q'))
    r.set(channels.ORIGIN + 'ui-1', json.dumps(origin), ex=channels.ORIGIN_TTL)  # the actor writes it (C8)
    recs = [{'id': 'ui-1', 'status': 'ok', 'result': {'reply': 'answer ui-1'}},
            {'id': 'ui-2', 'status': 'ok', 'at': 0, 'result': {'reply': 'from the CLI'}},  # no origin, past grace
            {'id': 'ui-1.done', 'status': 'ok', 'result': {'reply': 'unlinked'}}]      # origin_id -> ui-1 (C7)
    for rec in recs:
        r.set('fx:result:' + rec['id'], json.dumps(rec))
        r.rpush('fx:results', json.dumps(rec))
    entries = [{'type': 'telegram', 'account': 'fleet-bot-1', 'push': list(channels.DEFAULT_PUSH)}]
    actor.outbound_scan(r, entries)
    actor.outbound_scan(r, entries)  # a restart or a second tick never pushes twice
    items = [json.loads(x) for x in reversed(r.lrange(channels.OUTQ, 0, -1))]
    assert [(i['event'], i['target'], i['request_id']) for i in items] == [('result', 'origin', 'ui-1'),
                                                                            ('result', 'origin', 'ui-1.done')]
    assert 'unlinked' in json.dumps(items[1], ensure_ascii=False)
    assert r.exists(channels.PUSHED + 'result:ui-2')  # past the grace: an outside request, marked, never sent
    assert 0 < r.ttl(channels.ORIGIN + 'ui-1') <= channels.ORIGIN_TTL  # set at acceptance; pushing keeps it


def test_red_p1_delegated_result_keeps_its_origin_for_the_followup(private_redis_v2):
    """P7 hand-off: the delegated result (status ok, result.delegated) goes to the origin like any result; the
    origin keeps its 7-day expiry from acceptance (C8), so the follow-up <id>.done still finds it through
    envelope.origin_id (C7), failed follow-ups included."""
    from fleetmux.channels import actor
    r = private_redis_v2
    r.flushdb()
    entries = [{'type': 'telegram', 'account': 'fleet-bot-1', 'push': list(channels.DEFAULT_PUSH)}]
    mine, other = envelope.origin_from(_env('big job')), dict(envelope.origin_from(_env('x')), conversation='77')
    r.set(channels.ORIGIN + 'ui-7', json.dumps(mine), ex=channels.ORIGIN_TTL)
    r.set(channels.ORIGIN + 'job', json.dumps(other), ex=channels.ORIGIN_TTL)  # job.done maps back to job
    for rec in ({'id': 'ui-7', 'status': 'ok', 'result': {'reply': '已转 task 子 fleet M0', 'delegated': 'M0'}},
                {'id': 'job.done', 'status': 'ok', 'result': {'reply': 'plain'}}):
        r.set('fx:result:' + rec['id'], json.dumps(rec))
        r.rpush('fx:results', json.dumps(rec))
    actor.outbound_scan(r, entries)
    assert 0 < r.ttl(channels.ORIGIN + 'ui-7') <= channels.ORIGIN_TTL  # delegated: origin kept, not renewed
    items = {i['request_id']: i for i in map(json.loads, r.lrange(channels.OUTQ, 0, -1))}
    assert set(items) == {'ui-7', 'job.done'} and items['job.done']['target'] == 'origin'
    done = {'id': 'ui-7.done', 'status': 'failed', 'error': 'sub-fleet M0 could not start'}  # origin_id only
    r.set('fx:result:ui-7.done', json.dumps(done))
    r.rpush('fx:results', json.dumps(done))
    actor.outbound_scan(r, entries)
    actor.outbound_scan(r, entries)
    followups = [json.loads(x) for x in r.lrange(channels.OUTQ, 0, -1) if 'could not start' in x]
    assert len(followups) == 1 and followups[0]['request_id'] == 'ui-7.done'  # failed follow-ups pushed too
    assert 0 < r.ttl(channels.ORIGIN + 'ui-7') <= channels.ORIGIN_TTL


def test_red_p1_targets_origin_then_last_then_first(private_redis_v2):
    from fleetmux.channels import actor
    r = private_redis_v2
    r.flushdb()
    entries = [{'type': 'telegram', 'account': 'fleet-bot-1', 'push': list(channels.DEFAULT_PUSH)}]
    accounts = {('telegram', 'fleet-bot-1'): channels.Account('telegram', 'fleet-bot-1', ['111'], {'PUSH_TO': '111'})}
    first = actor.resolve_target(r, envelope.outbound('task_done', text='done'), entries, accounts)
    assert (first['channel'], first['conversation']) == ('telegram', '111')  # nobody talked yet: PUSH_TO
    r.hset(channels.LAST, mapping={'channel': 'telegram', 'account': 'fleet-bot-1', 'conversation': '42',
                                   'reply_ref': '{}', 'sender': '111', 'at': '1'})
    assert actor.resolve_target(r, envelope.outbound('task_done', text='d'), entries, accounts)['conversation'] == '42'
    no_push_to = {('telegram', 'fleet-bot-1'): channels.Account('telegram', 'fleet-bot-1', ['111'])}
    r.delete(channels.LAST)
    assert actor.resolve_target(r, envelope.outbound('task_done', text='d'), entries, no_push_to, audit=Audit()) is None


def test_red_p1_supervisor_restarts_with_backoff():
    from fleetmux.channels import actor
    now = [0.0]

    class Proc:
        def __init__(self):
            self.code = None

        def poll(self):
            return self.code
    procs = []

    def spawn():
        procs.append(Proc())
        return procs[-1]
    sup = actor.Supervisor(spawn=spawn, clock=lambda: now[0])
    sup.tick()
    assert len(procs) == 1
    waits = []
    for _ in range(6):
        procs[-1].code = 1
        sup.tick()  # noticed the exit: schedules a restart
        start = now[0]
        while len(procs) == len(waits) + 1:
            now[0] += 1
            sup.tick()
        waits.append(now[0] - start)
    assert waits == [30, 60, 120, 240, 300, 300]  # notify_bus daemon.py: 30 s doubling, 5 min cap
    assert sup.failing  # keeps failing: an alarm, never a fleet stop
    now[0] += 601
    sup.tick()
    procs[-1].code = 1
    sup.tick()
    start = now[0]
    while not sup.running():
        now[0] += 1
        sup.tick()
    assert now[0] - start == 30 and not sup.failing  # stable 10 minutes resets the backoff


def test_red_p1_no_channels_means_no_actor_process(tmp_path):
    """C13: one actor child per fleet (`channel actor <config.json>`), however many channels; a run without
    channels starts none (behaviour unchanged). Supervisor settings stay optional and compatible."""
    from fleetmux.channels import actor
    config = str(tmp_path / 'config.json')
    assert actor.plan_processes({}, config) == []
    assert actor.plan_processes({'channels': []}, config) == []
    cfg = {'channels': [{'type': 'telegram', 'account': 'a'}, {'type': 'feishu', 'account': 'b'},
                        {'type': 'exec', 'account': 'c'}]}
    plans = actor.plan_processes(cfg, config)
    assert len(plans) == 1 and plans[0][-3:] == ['channel', 'actor', config]  # one argv for all three channels
    assert not {'telegram', 'feishu', 'exec', 'a', 'b', 'c'} & set(plans[0])  # no per-account arguments
    sup = actor.Supervisor(spawn=lambda: None, clock=lambda: 0.0)
    assert sup.settings == channels.SUPERVISE_DEFAULTS
    sup = actor.Supervisor(spawn=lambda: None, clock=lambda: 0.0, settings={'backoff_start_s': 5})
    assert sup.settings == dict(channels.SUPERVISE_DEFAULTS, backoff_start_s=5)


def test_red_p1_cli_has_the_channel_subcommands():
    from fleetmux import fleet
    p = fleet.cli()
    a = p.parse_args(['channel', 'send', '--config', 'c.json', '--event', 'daemon_down', '--text', 'down'])
    assert (a.cmd, a.channel_cmd, a.event, a.text) == ('channel', 'send', 'daemon_down', 'down')
    a = p.parse_args(['channel', 'send', '--channel', 'telegram:fleet-bot-1', '--to', '42', '--reply-to', '7',
                      '--file', 'r.txt'])
    assert (a.channel, a.to, a.reply_to, a.file) == ('telegram:fleet-bot-1', '42', '7', 'r.txt')
    a = p.parse_args(['channel', 'login', 'telegram', 'fleet-bot-1', '--allow', '111', '--set', 'PUSH_TO=111'])
    assert (a.type, a.account, a.allow, a.set) == ('telegram', 'fleet-bot-1', ['111'], ['PUSH_TO=111'])
    a = p.parse_args(['channel', 'test', 'telegram', 'fleet-bot-1', '--to', '42'])
    assert (a.channel_cmd, a.to) == ('test', '42')
    assert p.parse_args(['channel', 'send', '--text', '-']).text == '-'  # body from stdin (FLEETMUX_TG_CMD style)
    a = p.parse_args(['channel', 'status', '--config', 'c.json', '--json'])
    assert a.channel_cmd == 'status' and a.json


def test_red_p1_channels_take_over_from_the_tg_command(monkeypatch, tmp_path):
    from fleetmux import lifecycle_close
    from fleetmux.channels import actor
    sent = []
    monkeypatch.setattr(actor, 'send_direct', lambda cfg, item: sent.append(item) or {'status': 'sent'})
    monkeypatch.setenv(lifecycle_close.TG_ENV, 'false')

    class F:
        service = True
        cfg = {'run': str(tmp_path), 'lifecycle': 'smoke', 'initiator': None,
               'channels': [{'type': 'telegram', 'account': 'fleet-bot-1', 'push': ['run_closed']}]}

        def task(self):
            return {}

        def log(self, *a, **k):
            pass
    recs = lifecycle_close.notify(F(), 'closed', 'run closed')
    tg = [x for x in recs if x['channel'] == 'tg'][0]
    assert tg['status'] == 'sent' and sent and sent[0]['event'] == 'run_closed'  # new config wins over the env


def test_red_p6_delete_approval_hooks_exist():
    from fleetmux import watchdog
    from fleetmux import runtime_v2
    from fleetmux.channels import approve  # P6b: the module responsible for writing fx:channel:pending:*
    for name in ('register', 'card', 'handle_command', 'mark_done'):
        assert callable(getattr(approve, name, None)), name
    assert not hasattr(approve, 'answer')  # v1 entry point: no alias left behind
    assert callable(getattr(watchdog.Watchdog, 'answer_user', None))  # presses after the fresh-screen checks
    for name in ('push_user_dialog', 'user_dialog_answer'):
        assert callable(getattr(runtime_v2.RuntimeV2, name, None)), name


def test_red_p6_daemon_down_section_sends_a_channel_message(tmp_path):
    from fleetmux import fleet
    cfg = {'python': '/usr/bin/python3', 'run': str(tmp_path),
           'channels': [{'type': 'telegram', 'account': 'fleet-bot-1', 'push': list(channels.DEFAULT_PUSH)}]}
    text = fleet.daemon_down_section(cfg, True)
    assert 'channel send' in text and '--event daemon_down' in text
    assert 'channel send' not in fleet.daemon_down_section({k: v for k, v in cfg.items() if k != 'channels'}, True)


def test_red_p7_split_budget_is_a_roster_limit():
    from fleetmux import roster
    assert roster.parse_limits({'split_budget_h': 4}) == {'split_budget_h': 4}


def test_red_p7_service_prompts_carry_the_split_line():
    agents = ROOT / 'fleetmux' / 'agents'
    # brief v2.1 #3: P7 reuses the §44 escalate machinery: the worker answers with a "proposal", the
    # supervisor sees the §44 "[escalate]" wording (P7b writes both md files; red until then).
    assert '"proposal"' in (agents / 'service_worker_v2.md').read_text(encoding='utf-8')
    assert '[escalate]' in (agents / 'service_supervisor_v2.md').read_text(encoding='utf-8')


def test_p0b_mask_covers_weixin_json_tokens_and_login_url_keys():
    """M150 (owner change, wx-1 M145 / e9643d4): the shared channels.redact also masks a JSON-quoted
    "context_token" / "typing_ticket" value and the login / key-like URL query values qrcode=, verify_code=,
    qrcode_img_content= and key=, so every channel's errors and audits do (not only WeixinChannel's)."""
    raw = ('POST getconfig 400 {"context_token": "ctx-UNSEEN-987654", "typing_ticket": "tt-SECRET-4567", "ret": 1} '
           'https://ilink.example/qr?qrcode=QR-ABCDEF123456&verify_code=VC-778899 '
           'img qrcode_img_content=IMG-ZZZ-112233 https://cdn.example/f?id=7&key=K-SECRET-9988')
    masked = channels.redact(raw)
    for secret in ('ctx-UNSEEN-987654', 'tt-SECRET-4567', 'QR-ABCDEF123456', 'VC-778899', 'IMG-ZZZ-112233',
                   'K-SECRET-9988'):
        assert secret not in masked, (secret, masked)
    assert '"context_token": "***"' in masked and '"typing_ticket": "***"' in masked
    assert 'qrcode=***' in masked and 'verify_code=***' in masked and '&key=***' in masked
    assert '"ret": 1' in masked and 'id=7' in masked  # non-secret fields and parameters are kept
    assert channels.redact({'note': 'x?key=K-SECRET-9988'}) == {'note': 'x?key=***'}
