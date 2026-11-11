"""Weixin channel (official ClawBot iLink only): P4a login, P4b receive / send. brief v2 §6.11, wx-1/test-plan.md.

Every protocol fact asserted here comes from the @tencent-weixin/openclaw-weixin 2.4.10 source and is NOT verified
against the real service (real-device checks are outside this run). No real network, no real credential, no QR
scan, no listener, no local WeChat data: the transport is the in-process ``FakeILink`` below (same signature as
``base.http_transport``), the config dir is a temp dir, every value is fake.

Supervisor rulings these tests pin (wx-1 test-plan §7):
* login hook = module-level ``LOGIN(account_name, io, transport)``; success returns exactly BOT_TOKEN / BASE_URL /
  BOT_ID / USER_ID / ALLOW (no SAVED_AT); ``binded_redirect`` returns {} and changes no file; a failure raises a
  redacted ``channels.ChannelError`` (credential failures kind 'auth', the 480 s limit kind 'transient');
* ``local_token_list`` holds only the BOT_TOKEN of this account's own file ([] without one): account isolation,
  approved deviation from the source, which sends every local token;
* ``get_bot_qrcode`` carries no ``base_info`` (source login-qr.ts:99-103); business POSTs do (approved exception
  to the brief's "every POST");
* phase one is text only: media are refused (``rejected_files``, reason 微信第一期只收文字), SUPPORTS_FILES False;
* cursor store key ``get_updates_buf``, committed only when the whole batch is final (base receipt protocol);
* errcode -14 -> health / status ``needs_login`` + a long pause; group / bot messages yield no envelope.

``CONTRACT`` (below) is read by tests/test_channels_contract.py (rows ``red:P4-weixin``).
"""
import base64
import inspect
import json
import re
import urllib.parse

import pytest

from fleetmux import __version__ as FLEETMUX_VERSION
from fleetmux import channels
from fleetmux.channels import base, weixin

# The protocol constant the source names (login-qr.ts:39, accounts.ts:13). Asserted as a value only: FakeILink
# answers every request in-process, nothing ever connects to it.
OFFICIAL_HOST = 'https://ilinkai.weixin.qq.com'

QR_PATH = '/ilink/bot/get_bot_qrcode'
STATUS_PATH = '/ilink/bot/get_qrcode_status'
UPD = '/ilink/bot/getupdates'
SEND = '/ilink/bot/sendmessage'
START = '/ilink/bot/msg/notifystart'
STOP = '/ilink/bot/msg/notifystop'
BUSINESS = (UPD, SEND, START, STOP)

USER = 'u_test@im.wechat'
BOT = 'bot_test@im.bot'
TOKEN = 'fake-token-wx'
POST_FIXED = {'Content-Type', 'AuthorizationType', 'X-WECHAT-UIN', 'iLink-App-Id', 'iLink-App-ClientVersion'}
GET_HEADERS = {'iLink-App-Id', 'iLink-App-ClientVersion'}
REFUSED = '微信第一期只收文字'


# ====================================================================== fakes (in-process, no socket)
class FakeClock:
    def __init__(self, t=1_000_000.0):
        self.t, self.slept = t, []

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.t += seconds

    def advance(self, seconds):
        self.t += seconds


class FakeILink:
    """In-process iLink: routes by path, records every request, answers from per-path scripts.
    Signature = base.http_transport: (method, url, *, headers=None, body=None, timeout=30) -> (status, bytes).
    An answer is a dict (200 JSON), a (status, bytes) tuple, or an exception instance to raise; a TimeoutError
    costs its full timeout on the attached clock."""
    DEFAULTS = {
        UPD: {'ret': 0, 'msgs': [], 'get_updates_buf': ''},
        SEND: {'ret': 0},
        START: {'ret': 0},
        STOP: {'ret': 0},
    }

    def __init__(self, clock=None):
        self.requests, self.script, self.clock = [], {}, clock

    def on(self, path, *answers):
        self.script.setdefault(path, []).extend(answers)
        return self

    def __call__(self, method, url, *, headers=None, body=None, timeout=30):
        u = urllib.parse.urlsplit(url)
        if isinstance(body, (bytes, bytearray)):
            body = body.decode('utf-8')
        req = {'method': method, 'url': url, 'host': u.scheme + '://' + u.netloc, 'path': u.path,
               'query': dict(urllib.parse.parse_qsl(u.query)), 'headers': dict(headers or {}),
               'json': json.loads(body) if body else None, 'timeout': timeout,
               'at': self.clock.now() if self.clock else None}
        self.requests.append(req)
        queue = self.script.get(u.path) or []
        answer = queue.pop(0) if queue else self.DEFAULTS.get(u.path, (404, b'{}'))
        if isinstance(answer, BaseException):
            if isinstance(answer, TimeoutError) and self.clock:
                self.clock.advance(timeout)
            raise answer
        if isinstance(answer, tuple):
            return answer
        return 200, json.dumps(answer).encode('utf-8')

    def calls(self, path):
        return [r for r in self.requests if r['path'] == path]


class FakeIO:
    """The ``io`` of LOGIN: print(text) / readline()."""

    def __init__(self, lines=()):
        self.lines, self.printed, self.reads = list(lines), [], 0

    def print(self, text):
        self.printed.append(str(text))

    def readline(self):
        self.reads += 1
        return self.lines.pop(0) if self.lines else ''


class Audit(list):
    def __call__(self, event, **fields):
        self.append((event, fields))

    def events(self):
        return [e for e, _ in self]

    def of(self, event):
        return [f for e, f in self if e == event]


class Accept:
    """The actor's accept, in memory (contracts §8 入站确认): 'persisted' / 'duplicate', or raise to keep the item
    un-final. ``fail[message_id] = exception`` raises it once for that envelope."""

    def __init__(self):
        self.got, self.fail, self.calls = {}, {}, []

    def __call__(self, env):
        mid = env['message_id']
        self.calls.append(mid)
        if mid in self.fail:
            raise self.fail.pop(mid)
        if mid in self.got:
            return 'duplicate'
        self.got[mid] = env
        return 'persisted'


def _msg(mid, frm=USER, text='hello contract', **kw):
    m = {'message_id': mid, 'from_user_id': frm, 'to_user_id': BOT, 'message_type': 1,
         'create_time_ms': 1762650000000, 'context_token': 'ctx-fake-1',
         'item_list': [{'type': 1, 'text_item': {'text': text}}]}
    if mid is None:
        del m['message_id']
    m.update(kw)
    return m


CONTRACT = {
    'account': {'ALLOW': USER, 'BOT_TOKEN': TOKEN, 'BASE_URL': 'https://acct.fake', 'BOT_ID': BOT,
                'USER_ID': USER},
    'conversation': USER,
    'transport': FakeILink,                       # zero-arg factory; its defaults answer every call ok
    'inbound_ok': _msg('7001'),
    'inbound_text': 'hello contract',
    'inbound_foreign': _msg('7002', frm='u_stranger@im.wechat'),
    'inbound_group': _msg('7003', group_id='g_test@chatroom'),
    'inbound_unverified': None,                   # iLink authenticates the sender itself
}


@pytest.fixture(autouse=True)
def config_dir(monkeypatch, tmp_path):
    """Never the operator's ~/.fleetmux/config."""
    d = tmp_path / 'fleetmux-config'
    monkeypatch.setenv(channels.CONFIG_ENV, str(d))
    return d


def _time(monkeypatch, clock):
    """LOGIN's own clock: module-level ``weixin._now`` / ``weixin._sleep`` (LOGIN takes no clock argument)."""
    monkeypatch.setattr(weixin, '_now', clock.now)
    monkeypatch.setattr(weixin, '_sleep', clock.sleep)


def _qr(n=1):
    return {'qrcode': 'qr-fake-' + str(n), 'qrcode_img_content': 'https://qr.fake/qr-fake-' + str(n)}


def _confirmed(**drop_or_set):
    c = {'status': 'confirmed', 'bot_token': TOKEN, 'ilink_bot_id': BOT, 'baseurl': 'https://acct.fake',
         'ilink_user_id': USER}
    for k, v in drop_or_set.items():
        if v is None:
            c.pop(k, None)
        else:
            c[k] = v
    return c


def _redirect_script(clock):
    return (FakeILink(clock).on(QR_PATH, _qr(1))
            .on(STATUS_PATH, {'status': 'wait'}, {'status': 'scaned'},
                {'status': 'scaned_but_redirect', 'redirect_host': 'idc2.fake'}, {'status': 'wait'}, _confirmed()))


def _account():
    lines = '\n'.join(k + '=' + str(v) for k, v in CONTRACT['account'].items())
    return channels.parse_account_text('weixin', 'contract-1', lines)


def _channel(tmp_path, il=None, clock=None, store=None):
    clock = clock or (il.clock if il is not None and il.clock else FakeClock())
    il = il if il is not None else FakeILink(clock)
    audit = Audit()
    ch = weixin.ADAPTER(_account(), transport=il, store=store if store is not None else base.MemoryStore(),
                        audit=audit, inbox_dir=tmp_path / 'inbox' / 'files', clock=clock.now)
    return ch, il, clock, audit


def _files(root):
    return sorted(str(p) for p in root.rglob('*')) if root.exists() else []


# ====================================================================== P4a: login
def test_contract_dict_and_adapter_class_registered():
    keys = {'account', 'conversation', 'transport', 'inbound_ok', 'inbound_text', 'inbound_foreign',
            'inbound_group', 'inbound_unverified'}
    assert set(CONTRACT) == keys
    assert all(CONTRACT[k] is not None for k in keys - {'inbound_unverified'}) and CONTRACT['inbound_unverified'] is None
    assert CONTRACT['account']['ALLOW'] == CONTRACT['account']['USER_ID'] == CONTRACT['conversation']
    cls = channels.adapter_class('weixin')
    assert cls is weixin.ADAPTER and issubclass(cls, base.Channel) and not inspect.isabstract(cls)
    assert cls.type == 'weixin' and channels.prefix('weixin') == 'wx'
    assert cls.TEXT_LIMIT == 4000 and cls.TEXT_UNITS == 'chars' and cls.OVERFLOW == 'split'
    assert cls.RECEIVES is True and cls.SUPPORTS_FILES is False
    assert cls.SUPPORTS_CARDS is False and cls.SUPPORTS_BUTTONS is False and cls.SUPPORTS_UPDATE is False
    assert cls.PENDING == () and cls.OPTIONAL_DEPS == ()
    assert list(inspect.signature(weixin.LOGIN).parameters) == ['account_name', 'io', 'transport']


def test_LOGIN_uses_fixed_host_then_redirect_host_for_polling(monkeypatch):
    clock = FakeClock()
    _time(monkeypatch, clock)
    il = _redirect_script(clock)
    got = weixin.LOGIN('wx-test', FakeIO(), il)
    assert weixin.FIXED_HOST == OFFICIAL_HOST
    qr = il.calls(QR_PATH)
    assert len(qr) == 1 and qr[0]['method'] == 'POST' and qr[0]['host'] == weixin.FIXED_HOST
    assert qr[0]['query'] == {'bot_type': '3'}
    st = il.calls(STATUS_PATH)
    assert [r['method'] for r in st] == ['GET'] * 5
    assert [r['host'] for r in st] == [weixin.FIXED_HOST] * 3 + ['https://idc2.fake'] * 2
    assert all(r['query'].get('qrcode') == 'qr-fake-1' for r in st)
    assert 1 in clock.slept  # one second between status polls (login-qr.ts:486)
    assert got['BASE_URL'] == 'https://acct.fake'


def test_LOGIN_confirmed_baseurl_is_BASE_URL_not_redirect_host(monkeypatch, config_dir):
    _time(monkeypatch, FakeClock())
    got = weixin.LOGIN('wx-test', FakeIO(), _redirect_script(None))
    assert got == {'BOT_TOKEN': TOKEN, 'BASE_URL': 'https://acct.fake', 'BOT_ID': BOT, 'USER_ID': USER,
                   'ALLOW': USER}
    assert 'SAVED_AT' not in got and 'idc2' not in json.dumps(got)
    assert got['ALLOW'] == got['USER_ID']
    assert _files(config_dir) == []  # LOGIN never writes the account file (the CLI does)


def test_LOGIN_confirmed_without_baseurl_uses_default_and_without_bot_id_fails(monkeypatch):
    _time(monkeypatch, FakeClock())
    il = FakeILink().on(QR_PATH, _qr(1)).on(STATUS_PATH, _confirmed(baseurl=None))
    got = weixin.LOGIN('wx-test', FakeIO(), il)
    assert weixin.DEFAULT_BASE_URL == OFFICIAL_HOST
    assert got['BASE_URL'] == weixin.DEFAULT_BASE_URL and got['BOT_TOKEN'] == TOKEN

    il = FakeILink().on(QR_PATH, _qr(1)).on(STATUS_PATH, _confirmed(ilink_bot_id=None))
    with pytest.raises(channels.ChannelError) as exc:
        weixin.LOGIN('wx-test', FakeIO(), il)
    assert exc.value.kind == 'auth'
    assert 'ilink_bot_id' in str(exc.value) and TOKEN not in str(exc.value)


def test_LOGIN_expired_refreshes_qr_at_most_3_times(monkeypatch):
    _time(monkeypatch, FakeClock())
    io = FakeIO()
    il = (FakeILink().on(QR_PATH, _qr(1), _qr(2), _qr(3))
          .on(STATUS_PATH, {'status': 'expired'}, {'status': 'expired'}, _confirmed()))
    got = weixin.LOGIN('wx-test', io, il)
    assert got['BOT_TOKEN'] == TOKEN
    qr = il.calls(QR_PATH)
    assert len(qr) == 3 and all(r['host'] == weixin.FIXED_HOST for r in qr)  # refreshes also hit the fixed host
    assert [r['query']['qrcode'] for r in il.calls(STATUS_PATH)] == ['qr-fake-1', 'qr-fake-2', 'qr-fake-3']
    printed = '\n'.join(io.printed)
    assert all('https://qr.fake/qr-fake-' + str(n) in printed for n in (1, 2, 3))

    il = FakeILink().on(QR_PATH, *[_qr(n) for n in range(1, 7)]).on(STATUS_PATH, *[{'status': 'expired'}] * 6)
    with pytest.raises(channels.ChannelError) as exc:
        weixin.LOGIN('wx-test', FakeIO(), il)
    assert len(il.calls(QR_PATH)) == 4  # first code + 3 refreshes (MAX_QR_REFRESH_COUNT, login-qr.ts:253)
    assert exc.value.kind == 'auth' and 'expired' in str(exc.value) and TOKEN not in str(exc.value)

    # verify_code_blocked counts against the same limit
    il = (FakeILink().on(QR_PATH, *[_qr(n) for n in range(1, 7)])
          .on(STATUS_PATH, {'status': 'verify_code_blocked'}, *[{'status': 'expired'}] * 5))
    with pytest.raises(channels.ChannelError) as exc:
        weixin.LOGIN('wx-test', FakeIO(), il)
    assert len(il.calls(QR_PATH)) == 4 and exc.value.kind == 'auth'


def test_LOGIN_need_verifycode_reads_io_and_sends_verify_code(monkeypatch):
    _time(monkeypatch, FakeClock())
    io = FakeIO(['1234\n', '5678\n'])
    il = (FakeILink().on(QR_PATH, _qr(1))
          .on(STATUS_PATH, {'status': 'need_verifycode'}, {'status': 'need_verifycode'}, {'status': 'scaned'},
              _confirmed()))
    got = weixin.LOGIN('wx-test', io, il)
    assert got['USER_ID'] == USER and io.reads == 2
    st = il.calls(STATUS_PATH)
    assert len(st) == 4
    assert 'verify_code' not in st[0]['query']
    assert st[1]['query'].get('verify_code') == '1234'  # read from io, sent on the next poll
    assert st[2]['query'].get('verify_code') == '5678'  # asked again = the first one was wrong
    assert 'verify_code' not in st[3]['query']          # scaned clears the submitted code
    assert len([p for p in io.printed if 'qr.fake' not in p]) >= 1  # a prompt was printed before reading


def test_LOGIN_binded_redirect_is_success_without_new_values(monkeypatch, config_dir):
    _time(monkeypatch, FakeClock())
    own = channels.account_path('weixin', 'wx-test')
    other = channels.account_path('weixin', 'other')
    own.parent.mkdir(parents=True)
    own.write_text('BOT_TOKEN=fake-token-old\nUSER_ID=' + USER + '\nALLOW=' + USER + '\n', encoding='utf-8')
    other.write_text('BOT_TOKEN=fake-token-other\n', encoding='utf-8')
    before = own.read_bytes()
    il = FakeILink().on(QR_PATH, _qr(1)).on(STATUS_PATH, {'status': 'binded_redirect'})
    assert weixin.LOGIN('wx-test', FakeIO(), il) == {}
    assert own.read_bytes() == before
    # only this account's own token (supervisor-approved account isolation; the source sends every local token)
    assert il.calls(QR_PATH)[0]['json'] == {'local_token_list': ['fake-token-old']}
    assert 'fake-token-other' not in json.dumps(il.requests)

    il = FakeILink().on(QR_PATH, _qr(1)).on(STATUS_PATH, {'status': 'binded_redirect'})
    assert weixin.LOGIN('wx-new', FakeIO(), il) == {}  # no account file of its own -> empty list
    assert il.calls(QR_PATH)[0]['json'] == {'local_token_list': []}
    assert 'fake-token-other' not in json.dumps(il.requests) and 'fake-token-old' not in json.dumps(il.requests)
    assert not channels.account_path('weixin', 'wx-new').exists()


def test_LOGIN_poll_timeout_is_wait_and_total_480s_limit(monkeypatch, config_dir):
    clock = FakeClock()
    _time(monkeypatch, clock)
    t0 = clock.now()
    il = (FakeILink(clock).on(QR_PATH, _qr(1))
          .on(STATUS_PATH, TimeoutError('timed out'), (524, b''), OSError('connection reset'),
              *[TimeoutError('timed out') for _ in range(40)]))
    with pytest.raises(channels.ChannelError) as exc:
        weixin.LOGIN('wx-test', FakeIO(), il)
    assert exc.value.kind == 'transient'  # running out of time is not a credential failure
    assert 'timeout' in str(exc.value).lower() and TOKEN not in str(exc.value)
    assert len(il.calls(QR_PATH)) == 1  # timeout / 524 / network error = wait: no refresh
    st = il.calls(STATUS_PATH)
    assert len(st) > 3 and all(r['timeout'] == 35 for r in st)  # QR_LONG_POLL_TIMEOUT_MS (login-qr.ts:33)
    assert all(r['at'] - t0 < 480 for r in st)
    assert len(st) <= 480 // 36 + 5  # ~36 s per poll (35 s timeout + 1 s pause); slack for the loop's exact shape
    assert _files(config_dir) == []


def test_LOGIN_prints_qr_link_never_token(monkeypatch, tmp_path):
    _time(monkeypatch, FakeClock())
    io = FakeIO()
    before = _files(tmp_path)
    got = weixin.LOGIN('wx-test', io, _redirect_script(None))
    printed = '\n'.join(io.printed)
    assert 'https://qr.fake/qr-fake-1' in printed
    assert TOKEN not in printed and got['BOT_TOKEN'] not in printed
    assert '\x1b[' not in printed and not re.search('[▀-▟]', printed)  # a link, never a drawn code
    assert _files(tmp_path) == before  # no image, no file


def test_post_headers_five_fixed_plus_bearer_get_headers_two_body_has_base_info(monkeypatch, tmp_path):
    clock = FakeClock()
    _time(monkeypatch, clock)
    il = _redirect_script(clock)
    weixin.LOGIN('wx-test', FakeIO(), il)
    ch, _, _, _ = _channel(tmp_path, il=il, clock=clock)
    ch.poll(0)
    assert ch.send_text(USER, 'hi').ok
    ch.start()
    ch.close()

    assert weixin.CHANNEL_VERSION == '2.4.10'
    assert weixin.client_version(weixin.CHANNEL_VERSION) == '132106'
    posts = [r for r in il.requests if r['method'] == 'POST']
    login_posts = [r for r in posts if r['path'] == QR_PATH]
    business = [r for r in posts if r['path'] in BUSINESS]
    assert login_posts and {r['path'] for r in business} == set(BUSINESS)
    assert len(posts) == len(login_posts) + len(business)
    for r in login_posts:
        assert set(r['headers']) == POST_FIXED  # no Authorization while logging in
    for r in business:
        assert set(r['headers']) == POST_FIXED | {'Authorization'}
        assert r['headers']['Authorization'] == 'Bearer ' + TOKEN and r['host'] == 'https://acct.fake'
    uins = []
    for r in posts:
        h = r['headers']
        assert h['Content-Type'] == 'application/json' and h['AuthorizationType'] == 'ilink_bot_token'
        assert h['iLink-App-Id'] == 'bot' and h['iLink-App-ClientVersion'] == '132106'
        uin = base64.b64decode(h['X-WECHAT-UIN']).decode('ascii')
        assert uin.isdigit() and int(uin) < 2 ** 32
        uins.append(uin)
    assert len(set(uins)) > 1  # random per request
    gets = [r for r in il.requests if r['method'] == 'GET']
    assert gets and all(r['path'] == STATUS_PATH and set(r['headers']) == GET_HEADERS for r in gets)
    for r in business:
        info = r['json']['base_info']
        assert info == {'channel_version': '2.4.10', 'bot_agent': 'fleetmux/' + FLEETMUX_VERSION}
        assert re.fullmatch(r'fleetmux/\S+', info['bot_agent']) and 'OpenClaw' not in json.dumps(r['json'])
    # approved exception to "every POST body has base_info": the source's get_bot_qrcode sends its body as is
    # (login-qr.ts:99-103, apiPostFetch); not verified on the real service
    assert all(r['json'] == {'local_token_list': []} for r in login_posts)


def test_client_version_2_4_10_is_132106_and_1_0_11_is_65547():
    assert weixin.client_version('2.4.10') == '132106'
    assert weixin.client_version('1.0.11') == '65547'
    # api.ts:99-104 buildClientVersion: missing parts count 0, non-numbers become 0
    assert weixin.client_version('1.2') == str((1 << 16) | (2 << 8))
    assert weixin.client_version('x.y.z') == '0'


# ====================================================================== P4b: receive / send
def test_first_getupdates_sends_empty_buf_then_persists_nonempty_cursor(tmp_path):
    ch, il, _, _ = _channel(tmp_path)
    il.on(UPD, {'ret': 0, 'msgs': [_msg('8001'), _msg('8002'), _msg('8003')], 'get_updates_buf': 'buf-1'})
    at_ack, real_ack = [], ch._ack

    def spy(raw):
        at_ack.append(ch.store.get('get_updates_buf'))
        return real_ack(raw)
    ch._ack = spy
    got = ch.poll(0)
    first = il.calls(UPD)[0]
    assert first['method'] == 'POST' and first['host'] == 'https://acct.fake'
    assert first['json']['get_updates_buf'] == ''
    assert [e['message_id'] for e in got] == ['wx:8001', 'wx:8002', 'wx:8003']
    assert at_ack == [None, None, None]  # nothing committed before the last item of the batch is final
    assert ch.store.get('get_updates_buf') == 'buf-1'
    ch.poll(0)
    assert il.calls(UPD)[1]['json']['get_updates_buf'] == 'buf-1'


def test_empty_cursor_not_overwritten_and_timeout_keeps_cursor(tmp_path):
    ch, il, _, audit = _channel(tmp_path)
    ch.store.set('get_updates_buf', 'buf-1')
    il.on(UPD, {'ret': 0, 'msgs': [], 'get_updates_buf': ''}, {'ret': 0, 'msgs': []}, TimeoutError('timed out'))
    for _ in range(3):
        assert ch.poll(0) == []
        assert ch.store.get('get_updates_buf') == 'buf-1'
    assert [r['json']['get_updates_buf'] for r in il.calls(UPD)] == ['buf-1'] * 3
    assert 'channel.error' not in audit.events()  # a client timeout is an empty batch, not an error

    batch = {'ret': 0, 'msgs': [_msg('8101'), _msg('8102'), _msg('8103')], 'get_updates_buf': 'buf-2'}
    il.on(UPD, batch, json.loads(json.dumps(batch)))
    accept = Accept()
    accept.fail['wx:8102'] = channels.Retryable('fleet did not answer')
    assert [e['message_id'] for e in ch.poll(0, accept)] == ['wx:8101']
    assert ch.store.get('get_updates_buf') == 'buf-1'  # the batch is not final: nothing committed
    assert [e['message_id'] for e in ch.poll(0, accept)] == ['wx:8102', 'wx:8103']
    assert il.calls(UPD)[-1]['json']['get_updates_buf'] == 'buf-1'  # same batch fetched again with the old cursor
    assert 'channel.drop_dup' in audit.events()
    assert ch.store.get('get_updates_buf') == 'buf-2'
    assert sorted(accept.got) == ['wx:8101', 'wx:8102', 'wx:8103']


def test_longpolling_timeout_ms_updates_next_timeout(tmp_path):
    ch, il, _, _ = _channel(tmp_path)
    il.on(UPD, {'ret': 0, 'msgs': [], 'get_updates_buf': 'b1', 'longpolling_timeout_ms': 25000},
          {'ret': 0, 'msgs': [], 'get_updates_buf': 'b2'},
          {'ret': 0, 'msgs': [], 'longpolling_timeout_ms': 0})
    for _ in range(4):
        ch.poll(0)
    # 35 s default (monitor.ts:13); then the server's value; a missing / 0 value keeps the last one
    assert [r['timeout'] for r in il.calls(UPD)] == [35, 25, 25, 25]


def test_cursor_resumes_from_store_on_new_instance(tmp_path):
    store, accept = base.MemoryStore(), Accept()
    clock = FakeClock()
    il = FakeILink(clock).on(UPD, {'ret': 0, 'msgs': [_msg('8201')], 'get_updates_buf': 'buf-1'})
    a, _, _, _ = _channel(tmp_path, il=il, store=store)
    assert len(a.poll(0, accept)) == 1
    b, _, _, _ = _channel(tmp_path, il=il, store=store)
    b.poll(0, accept)
    assert il.calls(UPD)[-1]['json']['get_updates_buf'] == 'buf-1'

    # a crash in the middle of a batch: the new instance refetches with the committed cursor; nothing lost or doubled
    batch = {'ret': 0, 'msgs': [_msg('8301'), _msg('8302'), _msg('8303')], 'get_updates_buf': 'buf-2'}
    il.on(UPD, batch, json.loads(json.dumps(batch)))
    accept.fail['wx:8302'] = ConnectionError('redis timeout')
    c, _, _, _ = _channel(tmp_path, il=il, store=store)
    assert [e['message_id'] for e in c.poll(0, accept)] == ['wx:8301']
    assert store.get('get_updates_buf') == 'buf-1'
    d, _, _, _ = _channel(tmp_path, il=il, store=store)
    assert [e['message_id'] for e in d.poll(0, accept)] == ['wx:8302', 'wx:8303']
    assert il.calls(UPD)[-1]['json']['get_updates_buf'] == 'buf-1'
    assert store.get('get_updates_buf') == 'buf-2'
    assert sorted(accept.got) == ['wx:8201', 'wx:8301', 'wx:8302', 'wx:8303']


def test_text_envelope_fields_voice_transcript_and_context_token_stored(tmp_path):
    ch, il, _, _ = _channel(tmp_path)
    [env] = ch.inbound(_msg('7001'))
    assert env['message_id'] == 'wx:7001' and env['text'] == 'hello contract'
    assert env['conversation'] == env['sender'] == USER
    assert env['private'] is True and env['verified'] is True and env['at'] == 1762650000
    assert env['reply_ref'] == {'to_user_id': USER, 'context_token': 'ctx-fake-1'}
    assert ch.store.get('ctx:' + USER) == 'ctx-fake-1'

    voice = _msg(None, context_token='ctx-fake-2',
                 item_list=[{'type': 3, 'msg_id': '8001', 'voice_item': {'text': '语音转写'}}])
    [env] = ch.inbound(voice)
    assert env['message_id'] == 'wx:8001'  # no top-level id: item_list[].msg_id (inbound.ts:202)
    assert env['text'] == '语音转写' and not env.get('rejected_files')
    assert ch.store.get('ctx:' + USER) == 'ctx-fake-2'  # the newest token per user

    [env] = ch.inbound(_msg(None, seq=42, context_token='ctx-fake-3'))
    assert env['message_id'] == 'wx:seq:42'
    assert ch.store.get('ctx:' + USER) == 'ctx-fake-3'

    bare = _msg(None, text='no ids at all')
    ids = {ch.parse_inbound(bare)[0]['message_id'], ch.parse_inbound(json.loads(json.dumps(bare)))[0]['message_id']}
    assert len(ids) == 1 and re.fullmatch(r'wx:h:[0-9a-f]{16}', ids.pop())
    assert il.requests == []


def test_media_items_go_to_rejected_files_with_label_text(tmp_path):
    ch, il, _, _ = _channel(tmp_path)
    cases = [
        (2, {'image_item': {'media': {'encrypt_query_param': 'q'}}}, '[图片]'),
        (4, {'file_item': {'file_name': 'a.pdf', 'media': {'encrypt_query_param': 'q'}}}, '[文件]'),
        (5, {'video_item': {'media': {'encrypt_query_param': 'q'}}}, '[视频]'),
        (3, {'voice_item': {'media': {'encrypt_query_param': 'q'}}}, '[语音]'),  # no transcript
    ]
    for n, (type_, item, label) in enumerate(cases):
        [env] = ch.inbound(_msg('760' + str(n), item_list=[dict({'type': type_}, **item)]))
        assert env['text'] == label
        assert len(env['rejected_files']) == 1 and env['rejected_files'][0]['reason'] == REFUSED
        assert isinstance(env['rejected_files'][0]['name'], str) and env['rejected_files'][0]['name']
        assert not env.get('files')
        if type_ == 4:
            assert env['rejected_files'][0]['name'] == 'a.pdf'
    mixed = _msg('7700', item_list=[{'type': 1, 'text_item': {'text': 'see this'}},
                                    {'type': 2, 'image_item': {'media': {'encrypt_query_param': 'q'}}}])
    [env] = ch.inbound(mixed)
    assert env['text'] == 'see this' and [f['reason'] for f in env['rejected_files']] == [REFUSED]
    assert il.requests == []  # no CDN download, no getuploadurl: phase one is text only
    assert _files(tmp_path / 'inbox') == []


def test_bot_and_group_messages_yield_no_envelope(tmp_path):
    ch, il, _, audit = _channel(tmp_path)
    bot = _msg('7101', message_type=2, text='bot echo')
    group = _msg('7102', group_id='g_test@chatroom', text='group text')
    assert ch.parse_inbound(bot) == [] and ch.parse_inbound(group) == []
    audit.clear()
    il.on(UPD, {'ret': 0, 'msgs': [bot, group, _msg('7103')], 'get_updates_buf': 'buf-g'})
    assert [e['message_id'] for e in ch.poll(0)] == ['wx:7103']
    drops = audit.of('channel.drop_policy')
    assert len(drops) == 1  # the group message; the bot's own echo is not audited
    assert drops[0].get('reason') == 'group' and drops[0].get('conversation') == 'g_test@chatroom'
    assert drops[0].get('sender') == USER
    assert 'group text' not in json.dumps(list(audit), ensure_ascii=False)
    assert 'bot echo' not in json.dumps(list(audit), ensure_ascii=False)
    assert ch.store.get('get_updates_buf') == 'buf-g'  # all three final -> committed


def test_sendmessage_body_shape_with_context_token_from_reply_ref_else_store(tmp_path):
    ch, il, _, _ = _channel(tmp_path)
    il.on(SEND, {'ret': 0, 'message_id': 'srv-1'})
    got = ch.send_text(USER, 'hi', reply_ref={'to_user_id': USER, 'context_token': 'ctx-ref'})
    assert got.ok and got.message_id == 'srv-1'
    [r] = il.calls(SEND)
    assert r['method'] == 'POST' and r['host'] == 'https://acct.fake'
    msg = dict(r['json']['msg'])
    assert re.fullmatch(r'fleetmux-weixin:\d+-[0-9a-f]{8}', msg.pop('client_id'))
    assert msg == {'from_user_id': '', 'to_user_id': USER, 'message_type': 2, 'message_state': 2,
                   'item_list': [{'type': 1, 'text_item': {'text': 'hi'}}], 'context_token': 'ctx-ref'}
    assert r['json']['base_info'] == {'channel_version': '2.4.10', 'bot_agent': 'fleetmux/' + FLEETMUX_VERSION}

    ch.store.set('ctx:' + USER, 'ctx-store')
    assert ch.send_text(USER, 'again').ok
    assert il.calls(SEND)[-1]['json']['msg']['context_token'] == 'ctx-store'

    ch2, il2, _, _ = _channel(tmp_path)
    got = ch2.send_text(USER, 'no token known')
    [r] = il2.calls(SEND)
    assert got.ok and 'context_token' not in r['json']['msg']  # send.ts:109 only warns
    assert got.message_id == r['json']['msg']['client_id']  # no server id -> the client id


def test_send_9000_chars_three_requests(tmp_path):
    ch, il, _, _ = _channel(tmp_path)
    ch.inbound(_msg('7201'))  # the store now knows ctx-fake-1 for this user
    text = 'x' * 9000
    assert ch.send_text(USER, text, reply_ref={'to_user_id': USER, 'context_token': 'ctx-fake-1'}).ok
    sends = il.calls(SEND)
    parts = [r['json']['msg']['item_list'][0]['text_item']['text'] for r in sends]
    assert [len(p) for p in parts] == [4000, 4000, 1000] and ''.join(parts) == text
    assert all(r['json']['msg']['to_user_id'] == USER for r in sends)
    assert all(r['json']['msg'].get('context_token') == 'ctx-fake-1' for r in sends)  # later chunks: from the store
    assert len({r['json']['msg']['client_id'] for r in sends}) == 3


def test_ret_nonzero_and_http_500_fail_without_raise(tmp_path):
    ch, il, _, audit = _channel(tmp_path)
    il.on(SEND, {'ret': -1, 'errmsg': 'bad'}, (500, b'oops'), OSError('connection reset'))
    results = [ch.send_text(USER, 'hi') for _ in range(3)]
    assert all(isinstance(g, base.SendResult) and not g.ok for g in results)
    assert '-1' in results[0].error and '500' in results[1].error and 'reset' in results[2].error
    assert audit.events().count('channel.error') == 3
    assert all(TOKEN not in g.error for g in results) and TOKEN not in ch.last_error


def test_errcode_minus_14_sets_needs_login_and_long_backoff(tmp_path):
    ch, il, clock, audit = _channel(tmp_path)
    il.on(UPD, {'ret': -14, 'errcode': -14, 'errmsg': 'session timeout'})
    assert ch.poll(0) == []
    errors = audit.of('channel.error')
    assert errors and errors[-1].get('kind') == 'auth'
    assert '-14' in ch.last_error and 'channel login weixin' in ch.last_error and TOKEN not in ch.last_error
    assert ch.health()['state'] == 'needs_login'  # supervisor ruling: health and status say needs_login
    calls = len(il.calls(UPD))
    clock.advance(3599)
    assert ch.poll(0) == [] and len(il.calls(UPD)) == calls  # paused (session-guard.ts:3, 60 minutes)
    clock.advance(1)
    ch.poll(0)
    assert len(il.calls(UPD)) == calls + 1
    assert ch.store.get('get_updates_buf') is None


def test_three_consecutive_api_errors_backoff_30s_else_2s(tmp_path):
    ch, il, clock, audit = _channel(tmp_path)
    bad, ok = {'ret': -1, 'errmsg': 'busy'}, {'ret': 0, 'msgs': [], 'get_updates_buf': ''}
    il.on(UPD, bad, bad, bad, ok, bad, ok)

    def polled():
        ch.poll(0)
        return len(il.calls(UPD))
    assert polled() == 1                      # error 1
    clock.advance(1)
    assert polled() == 1                      # 2 s back-off
    clock.advance(1)
    assert polled() == 2                      # error 2
    clock.advance(1)
    assert polled() == 2
    clock.advance(1)
    assert polled() == 3                      # error 3: third in a row
    clock.advance(29)
    assert polled() == 3                      # 30 s back-off (monitor.ts:129-148)
    clock.advance(1)
    assert polled() == 4                      # ok: the count resets
    assert polled() == 5                      # error again, the first of a new run
    clock.advance(1)
    assert polled() == 5
    clock.advance(1)
    assert polled() == 6
    kinds = [f.get('kind') for f in audit.of('channel.error')]
    assert kinds == ['transient'] * 4
    assert ch.store.get('get_updates_buf') is None


def test_send_file_degrades_to_path_text_SUPPORTS_FILES_false(tmp_path):
    ch, il, _, _ = _channel(tmp_path)
    f = tmp_path / 'r.txt'
    f.write_text('result')
    got = ch.send_file(USER, f, caption='结果')
    assert got.ok and ch.PENDING == () and ch.SUPPORTS_FILES is False
    assert [r['path'] for r in il.requests] == [SEND]  # no upload, no CDN
    assert il.calls(SEND)[0]['json']['msg']['item_list'] == [{'type': 1, 'text_item': {'text': '结果\n文件：' + str(f)}}]


def test_notifystart_on_start_notifystop_on_close_failure_only_audits(tmp_path):
    ch, il, _, _ = _channel(tmp_path)
    ch.start()
    [r] = il.calls(START)
    assert r['method'] == 'POST' and r['host'] == 'https://acct.fake' and r['timeout'] == 10
    assert set(r['json']) == {'base_info'}
    ch.close()
    [r] = il.calls(STOP)
    assert r['method'] == 'POST' and set(r['json']) == {'base_info'} and r['timeout'] == 10

    ch, il, _, audit = _channel(tmp_path)
    il.on(START, OSError('down')).on(STOP, (500, b''))
    ch.start()   # must not raise
    ch.poll(0)
    assert len(il.calls(UPD)) == 1  # receiving goes on
    ch.close()   # must not raise
    errors = json.dumps(audit.of('channel.error'))
    assert 'notifystart' in errors and 'notifystop' in errors


def test_tokens_masked_in_last_error_and_audit(tmp_path):
    ch, il, _, audit = _channel(tmp_path)
    ch.inbound(_msg('7301'))  # the adapter now holds ctx-fake-1 for this user
    il.on(UPD, OSError('401 https://acct.fake/x Authorization: Bearer ' + TOKEN + ' ctx=ctx-fake-1'))
    il.on(SEND, (500, json.dumps({'context_token': 'ctx-fake-1', 'token': TOKEN}).encode()))
    ch.poll(0)
    got = ch.send_text(USER, 'hi', reply_ref={'to_user_id': USER, 'context_token': 'ctx-fake-1'})
    assert not got.ok
    blob = json.dumps([ch.last_error, got.error, list(audit), ch.health()], ensure_ascii=False, default=str)
    assert TOKEN not in blob and 'ctx-fake-1' not in blob
    assert '***' in blob
    assert 'reply_ref' not in json.dumps([f for _, f in audit])  # reply_ref never reaches an audit (contracts §8)


def test_large_message_id_preserved_as_string(tmp_path):
    ch, il, _, _ = _channel(tmp_path)
    raw = (b'{"ret":0,"get_updates_buf":"buf-big","msgs":[{"message_id":123456789012345678901234567890,'
           b'"from_user_id":"u_test@im.wechat","to_user_id":"bot_test@im.bot","message_type":1,'
           b'"create_time_ms":1762650000000,"seq":9007199254740993,"context_token":"ctx-fake-1",'
           b'"item_list":[{"type":1,"msg_id":987654321098765432109876543210,"text_item":{"text":"big"}}]}]}')
    il.on(UPD, (200, raw))
    [env] = ch.poll(0)
    assert env['message_id'] == 'wx:123456789012345678901234567890'
    il.on(SEND, (200, b'{"ret":0,"message_id":123456789012345678901234567891}'))
    got = ch.send_text(USER, 'ok')
    assert got.ok and got.message_id == '123456789012345678901234567891'


def test_audit_masks_unseen_context_token_json_and_qr_url_key(tmp_path):
    """M145: error text that reaches an audit may carry a context_token this instance never saw (JSON form) or a
    QR / login URL with its key; both are masked, as are the bearer token and every account value."""
    leak = ('HTTP 401 https://ilinkai.weixin.qq.com/ilink/bot/get_qrcode_status?qrcode=qr-secret-1&verify_code=4321&key=qr-key-2 '
            '{"context_token": "ctx-unseen-1", "typing_ticket": "tkt-unseen-1"} qrcode_img_content=https://qr.fake/x '
            'Authorization: Bearer ' + TOKEN)

    def leaky(*args, **kwargs):
        raise OSError(leak)
    ch, _, _, audit = _channel(tmp_path, il=leaky, clock=FakeClock())
    ch.poll(0)
    got = ch.send_text(USER, 'hi')
    ch.start()
    ch.close()
    assert not got.ok and audit.of('channel.error')
    blob = json.dumps([list(audit), ch.last_error, got.error, ch.health()], ensure_ascii=False, default=str)
    for secret in (TOKEN, 'ctx-unseen-1', 'tkt-unseen-1', 'qr-secret-1', '4321', 'qr-key-2',
                   'https://qr.fake/x'):
        assert secret not in blob, secret
    assert 'get_qrcode_status' in blob and '***' in blob  # still says what failed
