"""Email adapter protocol tests (package P5, mail-1; brief v2 §2.2 P5a / P5b, §6.12; contracts §8 邮件专项).

P5a: send (SMTP over ``transport('smtp')``) + the pure ``verify`` of the topmost trusted Authentication-Results.
P5b: receive (IMAP over ``transport('imap')``): UIDVALIDITY + UID cursor in the account store, attachments,
plain-text body, IDLE or polling, reconnects.

Account file keys (``channels/email-<account>.env``; none of them is a roster key): ``ALLOW`` (sender addresses,
lower case), ``ADDRESS``, ``APP_PASSWORD``, ``SMTP_HOST`` / ``SMTP_PORT`` (default ``smtp.gmail.com:465``),
``IMAP_HOST`` / ``IMAP_PORT`` (default ``imap.gmail.com:993``), ``TRUSTED_AUTHSERV`` (default ``mx.google.com``),
``IDLE`` (auto / off), ``POLL_S`` (default 60), ``MAX_FILE_BYTES``, ``MAX_FILES``, ``PUSH_TO``. No key relaxes
the DKIM + SPF check.

Injection: ``transport(kind, **kw)`` returns an SMTP (``'smtp'``) or IMAP (``'imap'``) connection object;
``transport=None`` -> real ``smtplib.SMTP_SSL`` / ``imaplib.IMAP4_SSL`` with ``ssl.create_default_context()``.
Extra constructor keyword ``sleep=time.sleep`` (I3). Subject tag from the base attributes ``fleet_name`` /
``send_ref`` that P1g / P1e assign (I2, integration migration-plan §5d option A); the adapter never falls back
to the account name.

Everything here is fake: in-process FakeSMTP / FakeIMAP, reserved example domains, a fake app password. No real
network (an autouse guard fails any socket connect), no listener, no credential, nothing deleted.
AR trust premise (the receiving server strips forged same-name AR headers) is NOT verified by these offline tests:
they prove only the parsing and the decision ("按可信收件服务器的 AR 判定，依赖其剥除伪造头").
"""
import ast
import email.message
import email.policy
import email.utils
import imaplib
import json
import mimetypes
import re
import smtplib
import socket
import ssl
import sys
import time
from pathlib import Path

import pytest

from fleetmux import channels
from fleetmux.channels import base, envelope
from fleetmux.channels import email as mailchan

ROOT = Path(__file__).resolve().parents[1]
ALLOW = 'me@example.com'
ADDRESS = 'fleet@example.com'
APP_PASSWORD = 'fake-app-password'
ACCOUNT = 'contract-1'
T0 = 1762650000.0


# ====================================================================== fakes
class FakeSMTP:
    """One SMTP connection (smtplib.SMTP_SSL shape). Server state lives on the FakeTransport."""

    def __init__(self, server, host='', port=0, *args, **kw):
        self.server, self.host, self.port = server, host, port
        self.context, self.timeout = kw.get('context'), kw.get('timeout')
        self.closed = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.quit()
        return False

    def ehlo(self, *a, **kw):
        return 250, b'ok'

    def login(self, user, password):
        self.server.logins.append(user)
        if self.server.fail_login is not None:
            raise self.server.fail_login
        return 235, b'2.7.0 Accepted'

    def _record(self, parsed, from_addr, to_addrs):
        if self.server.fail_send is not None:
            raise self.server.fail_send
        self.server.sent.append({'msg': parsed, 'from_addr': from_addr,
                                 'to_addrs': None if to_addrs is None else list(
                                     [to_addrs] if isinstance(to_addrs, str) else to_addrs)})
        return {}

    def send_message(self, msg, from_addr=None, to_addrs=None, mail_options=(), rcpt_options=()):
        return self._record(email.message_from_bytes(msg.as_bytes(), policy=email.policy.default), from_addr, to_addrs)

    def sendmail(self, from_addr, to_addrs, msg, mail_options=(), rcpt_options=()):
        raw = msg.encode() if isinstance(msg, str) else bytes(msg)
        return self._record(email.message_from_bytes(raw, policy=email.policy.default), from_addr, to_addrs)

    def noop(self):
        return 250, b'ok'

    def quit(self):
        self.closed += 1
        return 221, b'bye'

    def close(self):
        self.closed += 1


class FakeMailbox:
    """IMAP server state shared by every connection: uidvalidity, {uid: raw bytes}, capabilities, failures."""

    def __init__(self, uidvalidity=7, mails=None, with_idle=True, capabilities=('IMAP4rev1', 'IDLE')):
        self.uidvalidity = uidvalidity
        self.mails = dict(mails or {})
        self.with_idle = with_idle
        self.capabilities = tuple(capabilities)
        self.fail = {}             # (op, uid or None) -> exception, raised once
        self.fail_login = None     # raised on every login while set
        self.idle_events = []      # what one IDLE yields
        self.log = []
        self.connects = 0
        self.logins = 0
        self.idle_calls = []

    def add(self, uid, raw):
        self.mails[uid] = raw

    def ops(self, op):
        return [e for e in self.log if e[0] == op]


def _norm(value):
    return value.decode() if isinstance(value, (bytes, bytearray)) else str(value)


class FakeIMAP:
    """One IMAP connection (imaplib.IMAP4_SSL return shapes). No ``idle`` attribute (see FakeIMAPIdle)."""

    def __init__(self, box, host='', port=0, *args, **kw):
        self.box, self.host, self.port = box, host, port
        self.ssl_context, self.timeout = kw.get('ssl_context'), kw.get('timeout')
        box.connects += 1
        self.capabilities = box.capabilities

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.logout()
        return False

    def _maybe_fail(self, op, uid=None):
        exc = self.box.fail.pop((op, uid), None)
        if exc is not None:
            raise exc

    def login(self, user, password):
        self.box.logins += 1
        self.box.log.append(('LOGIN', user))
        if self.box.fail_login is not None:
            raise self.box.fail_login
        self._maybe_fail('login')
        return 'OK', [b'LOGIN completed']

    def capability(self):
        return 'OK', [' '.join(self.box.capabilities).encode()]

    def select(self, mailbox='INBOX', readonly=False):
        self.box.log.append(('SELECT', _norm(mailbox), readonly))
        self._maybe_fail('select')
        return 'OK', [str(len(self.box.mails)).encode()]

    def examine(self, mailbox='INBOX'):
        return self.select(mailbox, readonly=True)

    def response(self, code):
        if code == 'UIDVALIDITY':
            return 'UIDVALIDITY', [str(self.box.uidvalidity).encode()]
        if code == 'UIDNEXT':
            return 'UIDNEXT', [str(max(self.box.mails, default=0) + 1).encode()]
        return code, [None]

    def status(self, mailbox, names):
        top = max(self.box.mails, default=0)
        return 'OK', [('"INBOX" (UIDNEXT %d UIDVALIDITY %d MESSAGES %d)'
                       % (top + 1, self.box.uidvalidity, len(self.box.mails))).encode()]

    def _uidset(self, spec):
        top = max(self.box.mails, default=0)
        out = []
        for part in _norm(spec).split(','):
            lo, _, hi = part.partition(':')
            lo = top if lo == '*' else int(lo)
            hi = lo if not _ else (top if hi == '*' else int(hi))
            out += [u for u in sorted(self.box.mails) if min(lo, hi) <= u <= max(lo, hi)]
        return out

    def uid(self, command, *args):
        cmd = _norm(command).upper()
        if cmd == 'SEARCH':
            crit = ' '.join(_norm(a) for a in args if a is not None)
            self.box.log.append(('SEARCH', ' '.join(crit.split())))
            self._maybe_fail('search')
            uids = sorted(self.box.mails)
            m = re.search(r'(\d+):\*', crit)
            if m:
                uids = [u for u in uids if u >= int(m.group(1))]
                if not uids and self.box.mails:
                    uids = [max(self.box.mails)]  # real-server quirk: n:* always matches the highest UID
            elif re.search(r'UID\s+\*', crit, re.I):
                uids = uids[-1:]
            return 'OK', [' '.join(map(str, uids)).encode()]
        if cmd == 'FETCH':
            spec, items = args[0], _norm(args[1])
            uids = self._uidset(spec)
            self.box.log.append(('FETCH', _norm(spec), items))
            for u in uids:
                self._maybe_fail('fetch', u)
            data = []
            for u in uids:
                raw = self.box.mails[u]
                data += [(('%d (UID %d BODY[] {%d}' % (u, u, len(raw))).encode(), raw), b')']
            return 'OK', data
        if cmd == 'STORE':
            self.box.log.append(('STORE',) + tuple(_norm(a) for a in args))
            return 'OK', [None]
        raise AssertionError('FakeIMAP: unexpected UID ' + cmd)

    def noop(self):
        self.box.log.append(('NOOP',))
        return 'OK', [b'NOOP completed']

    def logout(self):
        self.box.log.append(('LOGOUT',))
        return 'BYE', [b'logging out']

    def close(self):
        return 'OK', [b'closed']

    def shutdown(self):
        pass


class _Idler:
    def __init__(self, box):
        self.box = box

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter(list(self.box.idle_events))


class FakeIMAPIdle(FakeIMAP):
    """Python 3.14 shape: ``idle(duration=None)`` is a context manager yielding (type, [data]) responses."""

    def idle(self, duration=None):
        self.box.idle_calls.append(duration)
        self.box.log.append(('IDLE', duration))
        return _Idler(self.box)


class FakeTransport:
    """``transport(kind, **kw)``: 'smtp' -> new FakeSMTP, 'imap' -> new FakeIMAP bound to ``mailbox``.
    Zero-arg constructible: CONTRACT['transport'] is this class (every call succeeds)."""

    def __init__(self, mailbox=None):
        self.mailbox = mailbox if mailbox is not None else FakeMailbox()
        self.calls, self.smtp, self.imap = [], [], []
        self.sent, self.logins = [], []
        self.fail_login = None
        self.fail_send = None

    def __call__(self, kind, **kw):
        self.calls.append(kind)
        if kind == 'smtp':
            conn = FakeSMTP(self, **kw)
            self.smtp.append(conn)
            return conn
        if kind == 'imap':
            conn = (FakeIMAPIdle if self.mailbox.with_idle else FakeIMAP)(self.mailbox, **kw)
            self.imap.append(conn)
            return conn
        raise AssertionError('FakeTransport: unknown kind ' + repr(kind))


class Audit(list):
    def __call__(self, event, **fields):
        self.append((event, fields))

    def events(self):
        return [e for e, _ in self]

    def of(self, event):
        return [f for e, f in self if e == event]


# ====================================================================== test mail
def ar(authserv='mx.google.com', dkim='pass', dkim_i='example.com', dkim_d=None, spf='pass',
       mailfrom='me@example.com', dmarc='pass', from_domain='example.com', spf_comment=None):
    """One Authentication-Results value in Gmail's shape; a part set to None is left out."""
    parts = [authserv]
    if dkim is not None:
        s = 'dkim=' + dkim
        if dkim_i:
            s += ' header.i=@' + dkim_i
        if dkim_d:
            s += ' header.d=' + dkim_d
        parts.append(s + ' header.s=s1 header.b=AbCdEf12')
    if spf is not None:
        comment = spf_comment or ('google.com: domain of ' + mailfrom + ' designates 192.0.2.1 as permitted sender')
        parts.append('spf=' + spf + ' (' + comment + ') smtp.mailfrom=' + mailfrom)
    if dmarc is not None:
        parts.append('dmarc=' + dmarc + ' (p=NONE sp=NONE dis=NONE) header.from=' + from_domain)
    return '; '.join(parts)


def ar_pass(domain='example.com', local='me'):
    return ar(dkim_i=domain, mailfrom=local + '@' + domain, from_domain=domain)


def mail(frm='me@example.com', text='hello contract', ar=(), html=None, attachments=(), headers=None,
         subject='hi', to=ADDRESS, message_id='<c-1@example.com>'):
    """A raw RFC 5322 message. ``ar[0]`` is the topmost Authentication-Results. ``attachments``:
    (filename, bytes) or (filename, bytes, maintype, subtype)."""
    m = email.message.EmailMessage()
    for line in ar:
        m['Authentication-Results'] = line
    m['From'] = frm
    m['To'] = to
    m['Subject'] = subject
    m['Date'] = 'Mon, 09 Nov 2026 10:00:00 +0000'
    if message_id:
        m['Message-ID'] = message_id
    for k, v in (headers or {}).items():
        m[k] = v
    if text is not None:
        m.set_content(text)
        if html is not None:
            m.add_alternative(html, subtype='html')
    elif html is not None:
        m.set_content(html, subtype='html')
    for a in attachments:
        name, data = a[0], a[1]
        maintype, subtype = (a[2], a[3]) if len(a) > 2 else ('application', 'octet-stream')
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return m.as_bytes()


def item(uid, raw, uidvalidity=7):
    return {'uidvalidity': uidvalidity, 'uid': uid, 'raw': raw}


def parse(raw):
    return email.message_from_bytes(raw, policy=email.policy.default)


CONTRACT = {
    'account': {'ALLOW': ALLOW, 'ADDRESS': ADDRESS, 'APP_PASSWORD': APP_PASSWORD},
    'conversation': ALLOW,
    'transport': FakeTransport,  # zero-arg factory: every call succeeds
    'inbound_ok': item(1, mail(ar=[ar_pass()])),
    'inbound_text': 'hello contract',
    'inbound_foreign': item(2, mail(frm='stranger@example.org', ar=[ar_pass('example.org', 'stranger')])),
    'inbound_group': None,  # email has no groups; private is always True
    'inbound_unverified': item(3, mail(ar=['mx.google.com; dkim=none; spf=softfail smtp.mailfrom=me@example.com'])),
}


# ====================================================================== fixtures / helpers
@pytest.fixture(autouse=True)
def config_dir(monkeypatch, tmp_path):
    d = tmp_path / 'fleetmux-config'
    monkeypatch.setenv(channels.CONFIG_ENV, str(d))
    return d


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Any real connect fails loudly (and is recorded: a send would swallow the AssertionError)."""
    hits = []

    def refuse(*args, **kwargs):
        hits.append(args[1:2] or kwargs)
        raise AssertionError('real network')
    monkeypatch.setattr(socket.socket, 'connect', refuse)
    monkeypatch.setattr(socket.socket, 'connect_ex', refuse)
    monkeypatch.setattr(socket, 'create_connection', refuse)
    monkeypatch.setattr(ssl.SSLContext, 'wrap_socket', refuse)
    return hits


def account(**extra):
    values = dict(CONTRACT['account'])
    values.update({k: str(v) for k, v in extra.items()})
    return channels.parse_account_text('email', ACCOUNT, '\n'.join(k + '=' + v for k, v in values.items()))


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


class Sleep:
    """Records sleeps and advances the fake clock (an endless sleep loop fails instead of hanging)."""

    def __init__(self, clock):
        self.clock, self.calls = clock, []

    def __call__(self, seconds):
        self.calls.append(seconds)
        assert len(self.calls) < 1000, 'sleep loop'
        self.clock.now += max(0, seconds)


def make(tmp_path, transport=None, store=None, clock=None, sleep=None, inbox_dir=None, **extra):
    """(adapter, transport, audit). Builds through the registry like the actor does."""
    cls = channels.adapter_class('email')
    t = transport if transport is not None else FakeTransport()
    audit = Audit()
    kw = {'sleep': sleep} if sleep is not None else {}
    ch = cls(account(**extra), transport=t, store=store if store is not None else base.MemoryStore(), audit=audit,
             inbox_dir=inbox_dir if inbox_dir is not None else tmp_path / 'inbox' / 'files',
             clock=clock if clock is not None else Clock(), **kw)
    return ch, t, audit


def sent(t):
    return [s['msg'] for s in t.sent]


def addr(value):
    return email.utils.parseaddr(str(value))[1]


def flat(value):
    return ' '.join(str(value).split())


def verify(raw, allow=(ALLOW,), require=None):
    kw = {} if require is None else {'require': require}
    return mailchan.verify(parse(raw), set(allow), 'mx.google.com', **kw)


# ====================================================================== P5a: send + verify
def test_contract_dict_and_adapter_class_registered(tmp_path):
    assert {'account', 'conversation', 'transport', 'inbound_ok', 'inbound_text', 'inbound_foreign',
            'inbound_group', 'inbound_unverified'} <= set(CONTRACT)
    assert CONTRACT['inbound_group'] is None and CONTRACT['transport']().calls == []
    cls = channels.adapter_class('email')
    assert issubclass(cls, base.Channel)
    assert cls.type == 'email'
    assert cls.TEXT_LIMIT == 100000 and cls.TEXT_UNITS == 'chars'
    assert cls.OVERFLOW == 'split'  # D2: long text goes out as several mails, never as an attachment
    assert cls.SUPPORTS_CARDS is False and cls.SUPPORTS_BUTTONS is False and cls.SUPPORTS_UPDATE is False
    assert cls.SUPPORTS_FILES is True and cls.RECEIVES is True and cls.OPTIONAL_DEPS == ()
    assert channels.prefix('email') == 'em'
    ch = cls(account(), transport=FakeTransport(), store=base.MemoryStore(), audit=Audit(),
             inbox_dir=tmp_path / 'inbox', clock=Clock(), sleep=time.sleep)  # I3: extra keyword
    assert isinstance(ch, base.Channel) and ch.health()['state'] in channels.INBOUND_STATES


def test_default_transport_uses_ssl_default_context_cert_required(tmp_path, monkeypatch):
    made, box = [], FakeMailbox(with_idle=False)

    def smtp_ssl(*args, **kw):
        made.append(('smtp', args, kw))
        return FakeSMTP(holder, *args, **kw)

    def imap_ssl(*args, **kw):
        made.append(('imap', args, kw))
        return FakeIMAP(box, *args, **kw)
    holder = FakeTransport()
    monkeypatch.setattr(smtplib, 'SMTP_SSL', smtp_ssl)
    monkeypatch.setattr(imaplib, 'IMAP4_SSL', imap_ssl)

    def host_port(args, kw):
        return (args[0] if args else kw.get('host')), (args[1] if len(args) > 1 else kw.get('port'))

    def checked(ctx):
        return isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True

    cls = channels.adapter_class('email')
    ch = cls(account(), transport=None, store=base.MemoryStore(), audit=Audit(), inbox_dir=tmp_path / 'inbox',
             clock=Clock())
    assert ch.send_text(ALLOW, 'hello').ok
    ch.poll(0)
    smtp = [(a, k) for kind, a, k in made if kind == 'smtp']
    imap = [(a, k) for kind, a, k in made if kind == 'imap']
    assert smtp and imap
    assert host_port(*smtp[0]) == ('smtp.gmail.com', 465) and checked(smtp[0][1].get('context'))
    assert host_port(*imap[0]) == ('imap.gmail.com', 993) and checked(imap[0][1].get('ssl_context'))
    assert len(holder.sent) == 1

    made.clear()
    ch2 = cls(account(SMTP_HOST='smtp.example.com:2465', IMAP_HOST='imap.example.com', IMAP_PORT='1993'),
              transport=None, store=base.MemoryStore(), audit=Audit(), inbox_dir=tmp_path / 'inbox', clock=Clock())
    assert ch2.send_text(ALLOW, 'hello').ok
    ch2.poll(0)
    assert host_port(*[(a, k) for kind, a, k in made if kind == 'smtp'][0]) == ('smtp.example.com', 2465)
    assert host_port(*[(a, k) for kind, a, k in made if kind == 'imap'][0]) == ('imap.example.com', 1993)

    src = (ROOT / 'fleetmux' / 'channels' / 'email.py').read_text(encoding='utf-8')
    assert not re.search(r'CERT_NONE|check_hostname\s*=\s*False|_create_unverified_context|verify\s*=\s*False', src)


def test_subject_tag_message_id_and_threading_without_re_stacking(tmp_path):
    ch, t, _ = make(tmp_path)
    # I2 option A: P1g / P1e assign these before a send; here the test assigns them directly.
    ch.fleet_name, ch.send_ref = 'fleet-x', 'ui-1'
    # (1) both set
    r1 = ch.send_text(ALLOW, 'first', key='out-1')
    assert r1.ok, r1.error
    m1 = sent(t)[-1]
    assert m1['Subject'].startswith('[fleet:fleet-x#ui-1]')
    assert m1['Message-ID'] and m1['Date'] and addr(m1['From']) == ADDRESS and addr(m1['To']) == ALLOW
    assert r1.message_id == str(m1['Message-ID'])
    assert m1.get_content_type() == 'text/plain' and m1.get_content_charset() == 'utf-8'
    # (2) same key -> same Message-ID (at-least-once delivery deduplicates on the receiving side)
    assert ch.send_text(ALLOW, 'first', key='out-1').ok
    assert str(sent(t)[-1]['Message-ID']) == str(m1['Message-ID'])
    # (3) no key -> a fresh Message-ID each time
    ch.send_text(ALLOW, 'a')
    ch.send_text(ALLOW, 'b')
    ids = [str(m['Message-ID']) for m in sent(t)[-2:]]
    assert ids[0] != ids[1] and str(m1['Message-ID']) not in ids
    # (4) a .done follow-up keeps its ref
    ch.send_ref = 'ui-1.done'
    assert ch.send_text(ALLOW, 'done').ok
    assert '[fleet:fleet-x#ui-1.done]' in sent(t)[-1]['Subject']
    # (5) name only
    ch.send_ref = None
    assert ch.send_text(ALLOW, 'push').ok
    s5 = sent(t)[-1]['Subject']
    assert '[fleet:fleet-x]' in s5 and '#' not in s5
    # (6) no name -> no tag at all, never the account name or the address instead
    ch2, t2, _ = make(tmp_path)
    assert ch2.fleet_name == '' and ch2.send_ref is None  # the base defaults; the adapter adds none of its own
    assert ch2.send_text(ALLOW, 'untagged').ok
    ch2.send_ref = 'ui-1'
    assert ch2.send_text(ALLOW, 'untagged ref').ok
    for m in sent(t2):
        assert '[fleet:' not in m['Subject'] and ACCOUNT not in m['Subject'] and ADDRESS not in m['Subject']
    # (7) replies follow reply_ref.subject: one Re:, no second tag, no Re: Re:
    ch.send_ref = 'ui-1'
    ref = {'message_id': '<orig@example.com>', 'references': '<a@example.com>',
           'subject': 'Re: [fleet:fleet-x#ui-0] hi'}
    assert ch.send_text(ALLOW, 'reply', reply_ref=ref, key='out-r').ok
    m7 = sent(t)[-1]
    assert m7['Subject'] == 'Re: [fleet:fleet-x#ui-0] hi'
    assert str(m7['In-Reply-To']) == '<orig@example.com>'
    assert flat(m7['References']) == '<a@example.com> <orig@example.com>'
    assert ch.send_text(ALLOW, 'reply', reply_ref={'message_id': '<o2@example.com>', 'subject': 'hi'}).ok
    assert sent(t)[-1]['Subject'] == 'Re: hi'
    assert ch.send_text(ALLOW, 'reply', reply_ref={'message_id': '<o3@example.com>', 'subject': 'RE: x'}).ok
    assert sent(t)[-1]['Subject'] == 'RE: x'
    # (8) D2: over TEXT_LIMIT -> several mails, same tag, same thread
    text = ('y' * 99 + '\n') * 2500
    chunks = base.split_text(text, 100000)
    assert len(chunks) >= 3
    before = len(t.sent)
    assert ch.send_text(ALLOW, text, key='out-2').ok
    parts = sent(t)[before:]
    assert len(parts) == len(chunks)
    bodies = [m.get_content() for m in parts]
    assert all(len(b) <= 100000 for b in bodies) and ''.join(bodies) == text
    assert all(m['Subject'].startswith('[fleet:fleet-x#ui-1]') for m in parts)
    pids = [str(m['Message-ID']) for m in parts]
    assert len(set(pids)) == len(pids)
    before = len(t.sent)
    assert ch.send_text(ALLOW, text, key='out-2').ok
    assert [str(m['Message-ID']) for m in sent(t)[before:]] == pids  # derived from out-2, out-2:1, ...
    before = len(t.sent)
    assert ch.send_text(ALLOW, text, reply_ref=ref, key='out-3').ok
    parts = sent(t)[before:]
    assert len(parts) == len(chunks)
    assert all(m['Subject'] == 'Re: [fleet:fleet-x#ui-0] hi' for m in parts)
    first_id = str(parts[0]['Message-ID'])
    assert str(parts[0]['In-Reply-To']) == '<orig@example.com>'
    assert flat(parts[0]['References']) == '<a@example.com> <orig@example.com>'
    for m in parts[1:]:
        assert str(m['In-Reply-To']) == first_id
        assert '<orig@example.com>' in str(m['References']) and first_id in str(m['References'])


def test_login_failure_fails_without_password_in_error(tmp_path):
    ch, t, audit = make(tmp_path)
    t.fail_login = smtplib.SMTPAuthenticationError(
        535, ('5.7.8 Username and Password not accepted for ' + ADDRESS + ' / ' + APP_PASSWORD).encode())
    got = ch.send_text(ALLOW, 'hello')
    assert isinstance(got, base.SendResult) and got.ok is False and got.error
    dump = json.dumps([got.error, ch.last_error, audit, ch.health()], ensure_ascii=False, default=str)
    assert APP_PASSWORD not in dump
    errors = audit.of('channel.error')
    assert len(errors) == 1 and errors[0]['kind'] == 'auth'
    assert t.sent == [] and t.smtp and all(c.closed >= 1 for c in t.smtp)


def test_send_file_over_25mb_fails_without_raise(tmp_path):
    ch, t, _ = make(tmp_path)
    big = tmp_path / 'big.bin'
    with open(big, 'wb') as fh:
        fh.truncate(25 * 1024 * 1024 + 1)  # sparse
    got = ch.send_file(ALLOW, big, caption='too big')
    assert isinstance(got, base.SendResult) and got.ok is False and '25' in (got.error or '')
    assert t.sent == []
    small = tmp_path / 'result.txt'
    small.write_bytes(b'r' * 600)
    got = ch.send_file(ALLOW, small, caption='see attached')
    assert got.ok, got.error
    assert len(t.sent) == 1
    m = sent(t)[0]
    atts = list(m.iter_attachments())
    assert len(atts) == 1 and atts[0].get_filename() == 'result.txt'
    assert atts[0].get_content_type() == mimetypes.guess_type('result.txt')[0]
    assert atts[0].get_payload(decode=True) == b'r' * 600
    assert 'see attached' in m.get_body(('plain',)).get_content()


def test_recipient_is_conversation_not_reply_to_header(tmp_path):
    ch, t, _ = make(tmp_path)
    raw = mail(ar=[ar_pass()], headers={'Reply-To': 'attacker@evil.test', 'Cc': 'x@evil.test'})
    got = ch.inbound(item(1, raw))
    assert len(got) == 1
    env = got[0]
    assert env['conversation'] == ALLOW
    assert set(env['reply_ref']) <= {'message_id', 'references', 'subject'}
    assert 'evil.test' not in json.dumps(env['reply_ref'])
    assert ch.send_text(env['conversation'], 'ok', reply_ref=env['reply_ref']).ok
    s = t.sent[-1]
    assert s['to_addrs'] in (None, [ALLOW])
    assert addr(s['msg']['To']) == ALLOW and s['msg']['Cc'] is None and s['msg']['Bcc'] is None
    assert 'evil.test' not in s['msg'].as_string()
    smtp_before = t.calls.count('smtp')
    got = ch.send_text('stranger@example.org', 'x')
    assert got.ok is False and t.calls.count('smtp') == smtp_before


def test_verify_rejects_outsider_and_multiple_from():
    assert verify(mail(frm='stranger@example.org', ar=[ar_pass('example.org', 'stranger')])) == (False, 'from_not_allowed')
    raw = mail(ar=[ar_pass()])
    two_headers = b'From: other@example.com\n' + raw
    assert verify(two_headers) == (False, 'from_count')
    assert verify(mail(frm='me@example.com, other@example.com', ar=[ar_pass()])) == (False, 'from_count')
    assert verify(mail(frm='"Me" <ME@Example.COM>', ar=[ar_pass()])) == (True, '')


def test_verify_requires_first_ar_from_trusted_authserv():
    assert verify(mail()) == (False, 'no_trusted_ar')
    assert verify(mail(ar=[ar_pass().replace('mx.google.com', 'evil.test', 1), ar_pass()])) == (False, 'no_trusted_ar')
    assert verify(mail(ar=[ar(dkim='fail'), ar_pass()])) == (False, 'dkim_fail')  # judged by the topmost only
    assert verify(mail(ar=[ar_pass().replace('mx.google.com', 'mx.google.com 1', 1)])) == (True, '')


def test_verify_requires_dkim_pass_aligned():
    for state in ('fail', 'none'):
        assert verify(mail(ar=[ar(dkim=state)])) == (False, 'dkim_fail')
    assert verify(mail(ar=[ar(dkim=None)])) == (False, 'dkim_fail')
    assert verify(mail(ar=[ar(dkim_i=None, dkim_d='evil.test')])) == (False, 'dkim_unaligned')
    assert verify(mail(ar=[ar(dkim_i=None, dkim_d='mail.example.com')])) == (False, 'dkim_unaligned')  # strict
    assert verify(mail(ar=[ar(dkim_i='example.com', dkim_d=None)])) == (True, '')
    assert verify(mail(ar=[ar(dkim_i=None, dkim_d='example.com')])) == (True, '')
    two = ('mx.google.com; dkim=pass header.d=evil.test header.s=s2 header.b=Zz; '
           'dkim=pass header.d=example.com header.s=s1 header.b=AbCd; '
           'spf=pass smtp.mailfrom=me@example.com; dmarc=pass header.from=example.com')
    assert verify(mail(ar=[two])) == (True, '')


def test_verify_requires_spf_pass_aligned():
    for state in ('softfail', 'neutral'):
        assert verify(mail(ar=[ar(spf=state)])) == (False, 'spf_fail')
    assert verify(mail(ar=[ar(spf=None)])) == (False, 'spf_fail')
    assert verify(mail(ar=[ar(mailfrom='bounce@evil.test')])) == (False, 'spf_unaligned')
    assert verify(mail(ar=[ar(mailfrom='example.com')])) == (True, '')
    commented = ar(spf_comment='google.com: spf=fail smtp.mailfrom=bounce@evil.test was an old record')
    assert verify(mail(ar=[commented])) == (True, '')  # a comment is not a result


def test_verify_dmarc_fail_rejected_all_pass_accepted():
    assert verify(mail(ar=[ar(dmarc='fail')])) == (False, 'dmarc_fail')
    assert verify(mail(ar=[ar(dmarc=None)])) == (True, '')
    assert verify(mail(ar=[ar(dmarc='pass')])) == (True, '')


def test_verify_cannot_be_relaxed_to_spf_only(tmp_path):
    msg = parse(mail(ar=[ar_pass()]))
    for require in (('spf',), ('dkim',), ()):
        with pytest.raises(ValueError):
            mailchan.verify(msg, {ALLOW}, 'mx.google.com', require=require)
    for raw in (mail(ar=[ar_pass()]), mail(ar=[ar(spf='softfail')]), mail(ar=[ar(dkim='fail')])):
        assert verify(raw, require=('spf', 'dkim')) == verify(raw)
    spf_only = item(1, mail(ar=[ar(dkim='fail')]))
    for extra in ({'REQUIRE': 'spf'}, {'VERIFY': 'spf'}):
        ch, _, _ = make(tmp_path, **extra)
        envs = ch.parse_inbound(spf_only)
        assert envs and envs[0]['verified'] is False  # no switch relaxes the check
        assert ch.inbound(spf_only) == []


def test_verified_flag_in_envelope_equals_verify_result(tmp_path):
    ch, _, audit = make(tmp_path)
    cases = [
        (mail(ar=[ar_pass()], text='secret body 1'), ''),
        (mail(ar=[ar(dkim='fail')], text='secret body 2'), 'dkim_fail'),
        (mail(ar=[ar(mailfrom='bounce@evil.test')], text='secret body 3'), 'spf_unaligned'),
        (mail(ar=[ar(dmarc='fail')], text='secret body 4'), 'dmarc_fail'),
        (mail(ar=[ar_pass().replace('mx.google.com', 'evil.test', 1)], text='secret body 5'), 'no_trusted_ar'),
    ]
    for uid, (raw, reason) in enumerate(cases, 1):
        ok, why = verify(raw)
        assert (ok, why) == (reason == '', reason)
        envs = ch.parse_inbound(item(uid, raw))
        assert len(envs) == 1 and envs[0]['verified'] is ok
    audit.clear()
    for uid, (raw, reason) in enumerate(cases, 1):
        got = ch.inbound(item(uid, raw))
        assert len(got) == (1 if reason == '' else 0)
        assert ch.store.get('cursor') == '7:' + str(uid)  # a deterministic rejection still advances
    policy = audit.of('channel.drop_policy')
    assert len(policy) == 4 and all(f['verified'] is False for f in policy)
    assert sorted(f['reason'] for f in audit.of('channel.drop_auth')) == sorted(r for _, r in cases if r)
    assert 'secret body' not in json.dumps(audit, ensure_ascii=False, default=str)


# ====================================================================== P5b: receive
def test_new_mail_envelope_with_em_prefix_and_reply_ref(tmp_path):
    box = FakeMailbox(uidvalidity=7)
    ch, _, _ = make(tmp_path, transport=FakeTransport(box))
    assert ch.poll(0) == []
    assert ch.store.get('cursor') == '7:0'
    box.add(1, mail(frm='Me <ME@example.com>', ar=[ar_pass()], message_id='<orig@example.com>',
                    headers={'References': '<a@example.com>'}, subject='hi'))
    got = ch.poll(0)
    assert len(got) == 1
    env = got[0]
    assert envelope.problems(env) == []
    assert env['message_id'] == 'em:' + ACCOUNT + ':7:1'  # D1
    assert env['conversation'] == env['sender'] == 'me@example.com'
    assert env['reply_ref'] == {'message_id': '<orig@example.com>', 'references': '<a@example.com>', 'subject': 'hi'}
    assert env['private'] is True and env['verified'] is True and env['text'] == ''
    assert env.get('provenance') == [{'kind': 'email_body', 'text': 'hello contract'}]
    assert ('SEARCH', 'UID 1:*') in box.log
    fetches = box.ops('FETCH')
    assert fetches
    for _, _, items in fetches:
        tokens = items.strip('()').upper().split()
        assert 'BODY.PEEK[]' in tokens and 'BODY[]' not in tokens and 'RFC822' not in tokens
    assert ch.store.get('cursor') == '7:1'


def test_same_uid_not_reemitted_and_search_star_quirk_filtered(tmp_path):
    box = FakeMailbox(uidvalidity=7, mails={u: mail(ar=[ar_pass()]) for u in (1, 2, 3)})
    store = base.MemoryStore()
    store.set('cursor', '7:3')
    ch, _, audit = make(tmp_path, transport=FakeTransport(box), store=store)
    assert ch.poll(0) == []
    assert ch.poll(0) == []  # 'UID 4:*' answers [3]: filtered, not fetched again
    assert box.ops('SEARCH') and box.ops('FETCH') == []
    assert store.get('cursor') == '7:3'
    store.set('seen:em:' + ACCOUNT + ':7:3', '1')  # uid 3 was received before
    assert ch.inbound(item(3, box.mails[3])) == []
    assert 'channel.drop_dup' in audit.events()


def test_uidvalidity_change_resets_cursor_without_replay_and_audits_cursor_reset(tmp_path):
    box = FakeMailbox(uidvalidity=8, mails={u: mail(ar=[ar_pass()]) for u in (1, 2, 3)})
    store = base.MemoryStore()
    store.set('cursor', '7:5')
    ch, _, audit = make(tmp_path, transport=FakeTransport(box), store=store)
    assert ch.poll(0) == []
    assert box.ops('FETCH') == []  # no replay
    assert store.get('cursor') == '8:3'
    resets = audit.of('channel.cursor_reset')
    assert len(resets) == 1
    r = resets[0]
    assert (r['reason'], str(r['old']), str(r['new']), int(r['skipped'])) == ('uidvalidity_changed', '7', '8', 3)
    box.add(4, mail(ar=[ar_pass()]))
    got = ch.poll(0)
    assert [e['message_id'] for e in got] == ['em:' + ACCOUNT + ':8:4']


def test_first_start_skips_backlog(tmp_path):
    box = FakeMailbox(uidvalidity=7, mails={u: mail(ar=[ar_pass()]) for u in (1, 2, 3)})
    store = base.MemoryStore()
    ch, t, audit = make(tmp_path, transport=FakeTransport(box), store=store)
    assert ch.poll(0) == []
    assert box.ops('FETCH') == []
    assert store.get('cursor') == '7:3'
    resets = audit.of('channel.cursor_reset')
    assert len(resets) == 1 and resets[0]['reason'] == 'first_start' and int(resets[0]['skipped']) == 3
    box.add(4, mail(ar=[ar_pass()]))
    assert [e['message_id'] for e in ch.poll(0)] == ['em:' + ACCOUNT + ':7:4']
    ch.close()
    # same store, new instance (actor restart / --resume on the same Redis): a restart is not a first start
    box.add(5, mail(ar=[ar_pass()]))
    box.add(6, mail(ar=[ar_pass()]))
    ch2, _, audit2 = make(tmp_path, transport=FakeTransport(box), store=store)
    assert [e['message_id'] for e in ch2.poll(0)] == ['em:' + ACCOUNT + ':7:5', 'em:' + ACCOUNT + ':7:6']
    assert audit2.of('channel.cursor_reset') == []
    # an empty mailbox on first start
    ch3, _, audit3 = make(tmp_path, transport=FakeTransport(FakeMailbox(uidvalidity=7)))
    assert ch3.poll(0) == [] and ch3.store.get('cursor') == '7:0'
    resets = audit3.of('channel.cursor_reset')
    assert len(resets) == 1 and resets[0]['reason'] == 'first_start' and int(resets[0]['skipped']) == 0


def test_attachment_names_sanitized_inside_inbox_dir_no_symlink_follow(tmp_path):
    inbox = tmp_path / 'inbox' / 'files'
    inbox.mkdir(parents=True)
    outside = tmp_path / 'outside.txt'
    outside.write_text('keep')
    trap = inbox / 'em-7-1-passwd'
    trap.symlink_to(outside)  # a name the first attachment would take
    names = ['../../etc/passwd', 'a/b\\c.txt', 'x\x00y\x07.txt', '报告.pdf', '.hidden']
    datas = [('content-%d-' % i).encode() * (i + 3) for i in range(len(names))]
    raw = mail(ar=[ar_pass()], text='files', attachments=list(zip(names, datas)))
    ch, _, _ = make(tmp_path, inbox_dir=inbox)
    got = ch.inbound(item(1, raw))
    assert len(got) == 1
    files = got[0]['files']
    assert len(files) == len(names)
    for f in files:
        p = Path(f['path'])
        assert p.is_absolute() and not p.is_symlink() and p.resolve().parent == inbox.resolve()
        assert re.fullmatch(r'[A-Za-z0-9._-]+', p.name) and not p.name.startswith('.')
        assert p.name.startswith('em-7-1-')
        assert int(f['size']) == p.stat().st_size
    assert sorted(Path(f['path']).read_bytes() for f in files) == sorted(datas)
    assert outside.read_text() == 'keep' and trap.is_symlink() and trap.resolve() == outside.resolve()
    assert any(Path(f['path']).name == 'em-7-1-passwd-2' for f in files)  # O_EXCL: the taken name is skipped


def test_too_many_or_too_large_attachments_go_to_rejected_files(tmp_path):
    ch, _, _ = make(tmp_path)
    eleven = [('f%02d.txt' % i, b'x' * 10, 'text', 'plain') for i in range(11)]
    got = ch.inbound(item(1, mail(ar=[ar_pass()], text='many', attachments=eleven)))
    assert len(got) == 1
    assert len(got[0]['files']) == 10 and len(got[0]['rejected_files']) == 1
    assert '10' in got[0]['rejected_files'][0]['reason']
    assert ch.store.get('cursor') == '7:1'
    big = [('big.bin', b'\0' * (26 * 1024 * 1024)), ('small.txt', b'small', 'text', 'plain')]
    got = ch.inbound(item(2, mail(ar=[ar_pass()], text='big', attachments=big)))
    assert len(got) == 1
    assert [Path(f['path']).read_bytes() for f in got[0]['files']] == [b'small']
    rejected = got[0]['rejected_files']
    assert len(rejected) == 1 and rejected[0]['name'] == 'big.bin'
    assert re.search(r'MB|byte|size|大', rejected[0]['reason'])
    assert ch.store.get('cursor') == '7:2'


def test_html_only_body_goes_to_rejected_files_reason_html_only(tmp_path):
    ch, _, _ = make(tmp_path)
    got = ch.inbound(item(1, mail(ar=[ar_pass()], text=None, html='<p>do it</p>')))
    assert len(got) == 1
    env = got[0]
    assert env['text'] == '' and env['rejected_files'] == [{'name': '(正文)', 'reason': 'html only'}]
    assert envelope.problems(env) == []
    assert ch.store.get('cursor') == '7:1'
    got = ch.inbound(item(2, mail(ar=[ar_pass()], text='plain part', html='<p>html part</p>')))
    assert len(got) == 1 and got[0]['text'] == '' and got[0].get('rejected_files', []) == []
    assert got[0].get('provenance') == [{'kind': 'email_body', 'text': 'plain part'}]


def test_quote_and_signature_stripped_text_over_32000_rejected(tmp_path):
    ch, _, audit = make(tmp_path)
    bodies = [
        'do it\n\nOn Mon, 9 Nov 2026 at 10:00, Fleet <fleet@example.com> wrote:\n> old text\n> more\n',
        'do it\n\n在 2026年11月9日 10:00，Fleet <fleet@example.com> 写道：\n> 旧内容\n',
        'do it\n-- \nMe\nPhone 555-0100\n',
    ]
    for uid, body in enumerate(bodies, 1):
        got = ch.inbound(item(uid, mail(ar=[ar_pass()], text=body)))
        assert len(got) == 1 and got[0]['text'] == ''
        assert got[0].get('provenance') == [{'kind': 'email_body', 'text': 'do it'}]
    got = ch.inbound(item(4, mail(ar=[ar_pass()], text='a' * 32000)))
    assert len(got) == 1 and got[0]['text'] == ''
    assert got[0].get('provenance') == [{'kind': 'email_body', 'text': 'a' * 32000}]
    over = mail(ar=[ar_pass()], text='b' * 32001)
    assert ch.parse_inbound(item(5, over)) == []  # D4: no envelope at all
    assert 'channel.drop_size' in audit.events()
    box = FakeMailbox(uidvalidity=7, mails={1: over, 2: mail(ar=[ar_pass()], text='next one')})
    store = base.MemoryStore()
    store.set('cursor', '7:0')
    ch2, _, audit2 = make(tmp_path, transport=FakeTransport(box), store=store)
    got = ch2.poll(0)
    assert [(e['text'], e.get('provenance')) for e in got] == [('', [{'kind': 'email_body', 'text': 'next one'}])]
    assert 'channel.drop_size' in audit2.events() and store.get('cursor') == '7:2'
    assert 'b' * 100 not in json.dumps(audit2, default=str)


def _poll_mode(tmp_path, box, **extra):
    clock = Clock()
    sleep = Sleep(clock)
    store = base.MemoryStore()
    store.set('cursor', '7:0')
    ch, _, _ = make(tmp_path, transport=FakeTransport(box), store=store, clock=clock, sleep=sleep, **extra)
    return ch, clock, sleep


def test_idle_when_supported_else_poll_60s(tmp_path):
    # (1) idle() present and CAPABILITY says IDLE -> IDLE up to the timeout, no sleeping, then a SEARCH
    box = FakeMailbox(with_idle=True)
    ch, clock, sleep = _poll_mode(tmp_path, box)
    ch.poll(50)
    assert box.idle_calls and all(d is not None and 0 < d <= 50 for d in box.idle_calls)
    assert sleep.calls == []
    kinds = [e[0] for e in box.log]
    assert 'SEARCH' in kinds[kinds.index('IDLE') + 1:]
    # (2) no idle attribute, (3) idle() but no IDLE capability, (4) IDLE=off -> poll every 60 s with sleep
    cases = [(FakeMailbox(with_idle=False), {}),
             (FakeMailbox(with_idle=True, capabilities=('IMAP4rev1',)), {}),
             (FakeMailbox(with_idle=True), {'IDLE': 'off'})]
    for box, extra in cases:
        ch, clock, sleep = _poll_mode(tmp_path, box, **extra)
        ch.poll(50)
        assert len(box.ops('SEARCH')) == 1 and box.idle_calls == []  # the first poll searches at once
        sleep.calls.clear()
        clock.now = T0 + 10
        ch.poll(5)
        assert len(box.ops('SEARCH')) == 1  # not due yet
        assert sleep.calls and all(0 < s <= 5 for s in sleep.calls)
        clock.now = T0 + 60
        ch.poll(5)
        assert len(box.ops('SEARCH')) == 2 and box.idle_calls == []
    # (5) POLL_S=5
    box = FakeMailbox(with_idle=False)
    ch, clock, sleep = _poll_mode(tmp_path, box, POLL_S=5)
    ch.poll(1)
    assert len(box.ops('SEARCH')) == 1
    clock.now = T0 + 3
    ch.poll(1)
    assert len(box.ops('SEARCH')) == 1
    clock.now = T0 + 5
    ch.poll(1)
    assert len(box.ops('SEARCH')) == 2


def test_imap_abort_reconnects_keeping_cursor_auth_failure_no_busy_retry(tmp_path):
    # (1) the connection drops mid-FETCH: cursor kept, next poll reconnects and gets the mail
    box = FakeMailbox(uidvalidity=7, with_idle=False, mails={1: mail(ar=[ar_pass()]), 2: mail(ar=[ar_pass()])})
    box.fail[('fetch', 2)] = imaplib.IMAP4.abort('socket error: EOF')
    store = base.MemoryStore()
    store.set('cursor', '7:1')
    clock = Clock()
    ch, _, audit = make(tmp_path, transport=FakeTransport(box), store=store, clock=clock, sleep=Sleep(clock))
    assert ch.poll(0) == []
    assert store.get('cursor') == '7:1' and 'channel.error' in audit.events()
    clock.now += 61
    got = ch.poll(0)
    assert box.connects == 2
    assert [e['message_id'] for e in got] == ['em:' + ACCOUNT + ':7:2'] and store.get('cursor') == '7:2'
    # (2) AUTHENTICATIONFAILED: needs_login from the adapter itself, no busy retry, no password anywhere
    box = FakeMailbox(uidvalidity=7, with_idle=False)
    box.fail_login = imaplib.IMAP4.error('[AUTHENTICATIONFAILED] Invalid credentials (Failure) ' + APP_PASSWORD)
    clock = Clock()
    ch, _, audit = make(tmp_path, transport=FakeTransport(box), clock=clock, sleep=Sleep(clock))
    assert ch.poll(0) == []
    assert ch.health()['state'] == 'needs_login'
    assert [f['kind'] for f in audit.of('channel.error')] == ['auth']
    dump = json.dumps([ch.last_error, audit, ch.health()], ensure_ascii=False, default=str)
    assert APP_PASSWORD not in dump
    assert ch.poll(0) == [] and ch.poll(0) == []
    assert box.logins == 1
    clock.now += 300
    ch.poll(0)
    assert box.logins == 2


def test_processed_including_unverified_advances_cursor_network_error_does_not(tmp_path):
    box = FakeMailbox(uidvalidity=7, with_idle=False, mails={
        1: mail(ar=[ar(dkim='fail')]),                                           # forged
        2: mail(frm='stranger@example.org', ar=[ar_pass('example.org', 'stranger')]),  # not whitelisted
        3: mail(ar=[ar_pass()], text='legit'),
    })
    store = base.MemoryStore()
    store.set('cursor', '7:0')
    clock = Clock()
    ch, _, audit = make(tmp_path, transport=FakeTransport(box), store=store, clock=clock, sleep=Sleep(clock))
    got = ch.poll(0)
    assert [(e['text'], e.get('provenance')) for e in got] == [('', [{'kind': 'email_body', 'text': 'legit'}])]
    assert store.get('cursor') == '7:3'
    box.add(4, mail(ar=[ar_pass()], text='four'))
    box.fail[('fetch', 4)] = OSError('connection reset by peer')
    clock.now += 61
    assert ch.poll(0) == [] and store.get('cursor') == '7:3'
    clock.now += 61
    got = ch.poll(0)
    assert [(e['text'], e.get('provenance')) for e in got] == [('', [{'kind': 'email_body', 'text': 'four'}])]
    assert store.get('cursor') == '7:4'
    # the actor's accept could not persist it: not received, cursor stays, comes back once
    box.add(5, mail(ar=[ar_pass()], text='five'))

    def refuse(env):
        raise channels.Retryable('redis down')
    clock.now += 61
    assert ch.poll(0, accept=refuse) == [] and store.get('cursor') == '7:4'
    assert 'channel.inbound_retrying' in audit.events()
    clock.now += 61
    got = ch.poll(0)
    assert [(e['text'], e.get('provenance')) for e in got] == [('', [{'kind': 'email_body', 'text': 'five'}])]
    assert store.get('cursor') == '7:5'
    clock.now += 61
    assert ch.poll(0) == []
    # an attachment that cannot be written: Retryable, cursor stays, then received exactly once
    blocked = tmp_path / 'not-a-dir'
    blocked.write_text('a file where the inbox dir should be')
    box2 = FakeMailbox(uidvalidity=7, with_idle=False,
                       mails={1: mail(ar=[ar_pass()], text='with file', attachments=[('r.txt', b'r', 'text', 'plain')])})
    store2 = base.MemoryStore()
    store2.set('cursor', '7:0')
    clock2 = Clock()
    ch2, _, audit2 = make(tmp_path, transport=FakeTransport(box2), store=store2, clock=clock2, sleep=Sleep(clock2),
                          inbox_dir=blocked / 'files')
    assert ch2.poll(0) == [] and store2.get('cursor') == '7:0'
    assert 'channel.inbound_retrying' in audit2.events()
    ch2.inbox_dir = tmp_path / 'inbox2'
    clock2.now += 61
    got = ch2.poll(0)
    assert [(e['text'], e.get('provenance')) for e in got] == [('', [{'kind': 'email_body', 'text': 'with file'}])]
    assert store2.get('cursor') == '7:1'
    clock2.now += 61
    assert ch2.poll(0) == []


def test_no_real_socket_connect_guard(tmp_path, _no_network):
    box = FakeMailbox(uidvalidity=7, mails={1: mail(ar=[ar_pass()])})
    ch, t, _ = make(tmp_path, transport=FakeTransport(box))
    f = tmp_path / 'r.txt'
    f.write_text('r')
    c = envelope.card('删除确认', 'rm -rf build/', [{'label': '批准', 'command': '/approve p-0123abcd'}], ref='p-0123abcd')
    assert ch.send_text(ALLOW, 'hello').ok
    assert ch.send_file(ALLOW, f, caption='file').ok
    assert ch.send_card(ALLOW, c).ok
    assert ch.update_card(ALLOW, 'm-1', c).ok
    ch.poll(0)
    box.add(2, mail(ar=[ar_pass()]))
    ch.poll(0)
    ch.close()
    assert _no_network == []
    assert t.calls and set(t.calls) <= {'smtp', 'imap'}
    src_path = ROOT / 'fleetmux' / 'channels' / 'email.py'
    src = src_path.read_text(encoding='utf-8')
    for banned in ('.bind(', '.listen(', 'socket.socket(', 'serve_forever'):
        assert banned not in src
    top = set()
    for node in ast.parse(src).body:
        if isinstance(node, ast.Import):
            top |= {a.name.split('.')[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            top.add(node.module.split('.')[0])
    assert top <= set(sys.stdlib_module_names) | {'fleetmux', '__future__'}
    assert 'smtplib' in top and 'imaplib' in top  # the real defaults, referenced as module attributes


def test_idle_poll_20_waits_in_idle_and_does_not_search_every_call(tmp_path):
    # M93: actor.inbound_loop always calls poll(20). In IDLE mode a quiet call IDLEs up to 20 s and runs no SEARCH;
    # SEARCH only on a new connection, when IDLE reports something, or every POLL_S as a safety net.
    box = FakeMailbox(with_idle=True)
    ch, clock, sleep = _poll_mode(tmp_path, box)
    ch.poll(20)  # a new connection catches up
    for _ in range(3):
        before = len(box.ops('SEARCH'))
        assert ch.poll(20) == []
        assert len(box.ops('SEARCH')) == before and 0 < box.idle_calls[-1] <= 20
    assert sleep.calls == []
    box.add(1, mail(ar=[ar_pass()], text='arrived'))
    box.idle_events.append((b'EXISTS', [b'1']))
    got = ch.poll(20)
    assert [(e['text'], e.get('provenance')) for e in got] == [('', [{'kind': 'email_body', 'text': 'arrived'}])]
    assert ch.store.get('cursor') == '7:1'
    box.idle_events.clear()
    clock.now += 60
    before = len(box.ops('SEARCH'))
    assert ch.poll(20) == [] and len(box.ops('SEARCH')) == before + 1
