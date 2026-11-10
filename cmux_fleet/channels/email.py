"""Email adapter (package P5; docs/design.md §53, contracts §8 邮件专项). Ported in spirit from
``~/.cmux-fleet-channels/ref/curve_report.py`` (SMTP_SSL + app password), with every business part removed.

Account file ``channels/email-<account>.env`` (none of these is a roster key; nothing relaxes the sender check):
``ALLOW`` (sender addresses, lower case), ``ADDRESS``, ``APP_PASSWORD``, ``SMTP_HOST`` / ``SMTP_PORT`` (default
``smtp.gmail.com:465``; ``SMTP_HOST`` may carry ``:port``), ``IMAP_HOST`` / ``IMAP_PORT`` (default
``imap.gmail.com:993``), ``TRUSTED_AUTHSERV`` (default ``mx.google.com``), ``IDLE`` (auto / off), ``POLL_S``
(default 60), ``MAX_FILE_BYTES`` (default 25 MB), ``MAX_FILES`` (default 10), ``PUSH_TO``.

Transport: ``transport(kind)`` returns a connection object, ``kind`` = ``'smtp'`` (smtplib.SMTP_SSL shape) or
``'imap'`` (imaplib.IMAP4_SSL shape). ``transport=None`` -> the real classes with ``ssl.create_default_context()``
(certificate and host name checked). Nothing here listens or opens a socket of its own.

Sender check (``verify``): only the TOPMOST ``Authentication-Results`` header counts, its authserv-id must be
``TRUSTED_AUTHSERV``; DKIM pass aligned with the From domain AND SPF pass aligned with it; a ``dmarc=`` other than
pass rejects; exactly one From address, in ALLOW. AR is only a header: this holds because the trusted receiving
server strips forged same-name AR headers (RFC 8601 §5), which is not verified here ("按可信收件服务器的 AR 判定，
依赖其剥除伪造头"; never "已防伪").
"""
import email.message
import email.policy
import email.utils
import hashlib
import imaplib
import mimetypes
import os
import re
import smtplib
import ssl
import time
import uuid
from pathlib import Path

from cmux_fleet import channels
from cmux_fleet.channels import base, envelope, provenance

DEFAULT_SMTP = ('smtp.gmail.com', 465)
DEFAULT_IMAP = ('imap.gmail.com', 993)
DEFAULT_AUTHSERV = 'mx.google.com'
SEND_MAX_BYTES = 25 * 1024 * 1024  # Gmail's message size limit for one attachment send
TIMEOUT_S = 60
REQUIRED = frozenset({'dkim', 'spf'})


# ====================================================================== sender check (pure)
def _strip_comments(value):
    """RFC 5322 comments ``( … )`` (nested, with quoted-pairs) removed; quoted strings kept as they are."""
    out, depth, quoted, escaped = [], 0, False, False
    for ch in value:
        if escaped:
            escaped = False
            if depth == 0:
                out.append(ch)
            continue
        if ch == '\\':
            escaped = True
            if depth == 0:
                out.append(ch)
            continue
        if quoted:
            out.append(ch)
            if ch == '"':
                quoted = False
            continue
        if ch == '"' and depth == 0:
            quoted = True
            out.append(ch)
        elif ch == '(':
            depth += 1
        elif ch == ')' and depth:
            depth -= 1
        elif depth == 0:
            out.append(ch)
    return ''.join(out)


_PAIR = re.compile(r'([A-Za-z0-9_.\-/]+)\s*=\s*("(?:[^"\\]|\\.)*"|[^\s;]+)')


def _parse_ar(value):
    """(authserv-id lower case, [(method, result, {property: value})]) of one Authentication-Results value."""
    pieces = _strip_comments(' '.join(str(value).split())).split(';')
    head = pieces[0].split()
    authserv = head[0].lower() if head else ''
    results = []
    for piece in pieces[1:]:
        pairs = _PAIR.findall(piece)
        if not pairs:
            continue
        method, result = pairs[0]
        props = {k.lower(): v.strip('"') for k, v in pairs[1:]}
        results.append((method.split('/')[0].lower(), result.strip('"').lower(), props))
    return authserv, results


def _domain(value):
    value = (value or '').strip().strip('<>').lower()
    return value.rpartition('@')[2] if '@' in value else value


def verify(msg, allow, trusted_authserv, require=('dkim', 'spf')):
    """(ok, reason) for one parsed message: reason '' when it passes, else one of from_count / from_not_allowed /
    no_trusted_ar / dkim_fail / dkim_unaligned / spf_fail / spf_unaligned / dmarc_fail. ``require`` exists only to
    be refused: anything but both DKIM and SPF raises ValueError (the check cannot be relaxed)."""
    if not isinstance(require, (tuple, list, set, frozenset)) or set(require) != REQUIRED or len(require) != 2:
        raise ValueError('email verify requires both dkim and spf; it cannot be relaxed')
    froms = [str(h) for h in (msg.get_all('From') or [])]
    addresses = [a for _, a in email.utils.getaddresses(froms) if a] if len(froms) == 1 else []
    if len(froms) != 1 or len(addresses) != 1 or '@' not in addresses[0]:
        return False, 'from_count'
    sender = addresses[0].lower()
    if sender not in {str(a).strip().lower() for a in allow}:
        return False, 'from_not_allowed'
    from_domain = _domain(sender)
    ars = msg.get_all('Authentication-Results') or []
    if not ars:
        return False, 'no_trusted_ar'
    authserv, results = _parse_ar(ars[0])  # the topmost one only
    if not authserv or authserv != str(trusted_authserv or '').strip().lower():
        return False, 'no_trusted_ar'
    dkim = [p for m, r, p in results if m == 'dkim' and r == 'pass']
    if not dkim:
        return False, 'dkim_fail'
    if not any(_domain(p.get('header.d') or p.get('header.i')) == from_domain for p in dkim):
        return False, 'dkim_unaligned'
    spf = [p for m, r, p in results if m == 'spf' and r == 'pass']
    if not spf:
        return False, 'spf_fail'
    if not any(_domain(p.get('smtp.mailfrom')) == from_domain for p in spf):
        return False, 'spf_unaligned'
    if any(m == 'dmarc' and r != 'pass' for m, r, _ in results):
        return False, 'dmarc_fail'
    return True, ''


# ====================================================================== the adapter
def _host_port(account, key, default):
    host, port = account.get(key + '_HOST', '') or default[0], default[1]
    if ':' in host:
        host, _, p = host.rpartition(':')
        port = int(p) if p.isdigit() else port
    p = account.get(key + '_PORT', '')
    if str(p).isdigit():
        port = int(p)
    return host, port


def _int(value, default):
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def _one_line(value):
    return ' '.join(str(value or '').split())


def _text(value):
    return value.decode('utf-8', 'replace') if isinstance(value, (bytes, bytearray)) else str(value)


_QUOTE_HEAD = re.compile(
    r'^[ \t]*(?:On\s[^\n]*(?:\n[^\n]*)?\swrote:'          # Gmail / Apple (may wrap onto a second line)
    r'|在[^\n]*(?:\n[^\n]*)?写道[:：]'                    # Chinese clients
    r'|-{2,}\s*(?:Original Message|原始邮件)\s*-{2,}'       # Outlook
    r')[ \t]*$', re.M)


def strip_quotes(text):
    """The text the sender typed: cut at the first reply attribution ("On … wrote:", "在 … 写道：", "-----Original
    Message-----") or signature separator ("-- "), drop ``>`` quoted lines, trim."""
    m = _QUOTE_HEAD.search(text)
    if m:
        text = text[:m.start()]
    lines = []
    for line in text.split('\n'):
        if line.rstrip() == '--':  # RFC 3676 "-- " signature separator (some clients drop the space)
            break
        if line.lstrip().startswith('>'):
            continue
        lines.append(line.rstrip())
    return '\n'.join(lines).strip()


class EmailChannel(base.Channel):
    type = 'email'
    TEXT_LIMIT = 100000
    TEXT_UNITS = 'chars'
    OVERFLOW = 'split'      # D2: several mails, one per chunk, same subject, same thread
    RECEIVES = True
    SUPPORTS_CARDS = False  # a card goes out as text (commands spelled out)
    SUPPORTS_BUTTONS = False
    SUPPORTS_UPDATE = False
    SUPPORTS_FILES = True
    MAX_FILE_BYTES = 25 * 1024 * 1024
    MAX_FILES = 10
    POLL_S = 60
    AUTH_RETRY_S = 300      # after a refused login, no new login attempt before this
    OPTIONAL_DEPS = ()

    def __init__(self, account, *, transport=None, store=None, audit=None, inbox_dir=None, clock=time.time,
                 sleep=time.sleep):
        super().__init__(account, transport=transport, store=store, audit=audit, inbox_dir=inbox_dir, clock=clock)
        self.sleep = sleep
        self.address = account.get('ADDRESS', '').strip()
        self.smtp_host = _host_port(account, 'SMTP', DEFAULT_SMTP)
        self.imap_host = _host_port(account, 'IMAP', DEFAULT_IMAP)
        self.trusted_authserv = account.get('TRUSTED_AUTHSERV', '') or DEFAULT_AUTHSERV
        self.max_file_bytes = _int(account.get('MAX_FILE_BYTES', ''), self.MAX_FILE_BYTES)
        self.max_files = _int(account.get('MAX_FILES', ''), self.MAX_FILES)
        self._first = None  # the first chunk of the current long send: {key, message_id, subject, references}
        self._imap = None   # the open IMAP connection (inbound)
        self.poll_s = _int(account.get('POLL_S', ''), self.POLL_S)
        self.idle_off = str(account.get('IDLE', '')).strip().lower() in ('off', '0', 'no', 'false')
        self._idle = False      # the open connection supports IDLE (and IDLE is not off)
        self._due = 0.0         # polling mode: next SEARCH not before this
        self._auth_until = 0.0  # after a refused login: no new login before this
        self._heartbeat = 0     # last successful SEARCH
        self._activity = 0      # last poll that fetched mail

    # ------------------------------------------------------------------ connections
    def _connect(self, kind):
        if self.transport is not None:
            return self.transport(kind)
        context = ssl.create_default_context()
        if kind == 'smtp':
            return smtplib.SMTP_SSL(self.smtp_host[0], self.smtp_host[1], context=context, timeout=TIMEOUT_S)
        return imaplib.IMAP4_SSL(self.imap_host[0], self.imap_host[1], ssl_context=context, timeout=TIMEOUT_S)

    @staticmethod
    def _close_smtp(conn):
        try:
            conn.quit()
        except Exception:
            try:
                conn.close()
            except Exception:
                pass

    def _deliver(self, msg, conversation):
        conn = self._connect('smtp')
        try:
            try:
                conn.login(self.address, self.account.get('APP_PASSWORD', ''))
            except smtplib.SMTPAuthenticationError as exc:
                raise channels.ChannelError('auth', self.redact(str(exc))) from None
            conn.send_message(msg, from_addr=self.address, to_addrs=[conversation])
        finally:
            self._close_smtp(conn)
        return str(msg['Message-ID'])

    # ------------------------------------------------------------------ outbound
    def _message_id(self, key):
        domain = self.address.rpartition('@')[2] or 'fleetmux.invalid'
        if key:
            digest = hashlib.sha256((self.account.name + '\0' + str(key)).encode()).hexdigest()[:32]
        else:
            digest = uuid.uuid4().hex
        return '<' + digest + '.fleetmux@' + domain + '>'

    def _subject(self, text):
        name = _one_line(self.fleet_name)
        tag = ''
        if name:  # never the account name or the address instead (I2 option A)
            ref = _one_line(self.send_ref) if self.send_ref is not None else ''
            tag = '[fleet:' + name + ('#' + ref if ref else '') + ']'
        first = next((_one_line(line) for line in str(text).splitlines() if line.strip()), '')
        if len(first) > 60:
            first = first[:57] + '...'
        return (tag + ' ' + first).strip() or 'fleetmux'

    def _build(self, conversation, reply_ref, body_text):
        """(EmailMessage with headers set, the first-chunk record of this send or None)."""
        key = self.send_key
        first = self._first
        cont = (reply_ref is None and key and first and first['key']
                and str(key).startswith(first['key'] + ':') and str(key)[len(first['key']) + 1:].isdigit())
        msg = email.message.EmailMessage()
        msg['From'] = self.address
        msg['To'] = conversation
        msg['Date'] = email.utils.formatdate(self.clock(), usegmt=True)
        msg['Message-ID'] = self._message_id(key)
        if cont:  # a later chunk of one long send: same subject, same thread as its first chunk
            msg['Subject'] = first['subject']
            msg['In-Reply-To'] = first['message_id']
            msg['References'] = (first['references'] + ' ' + first['message_id']).strip()
        else:
            ref = reply_ref if isinstance(reply_ref, dict) else {}
            original = _one_line(ref.get('subject', ''))
            if original:  # a reply follows the original subject: one Re:, no second tag
                subject = original if re.match(r'\s*re\s*:', original, re.I) else 'Re: ' + original
            else:
                subject = self._subject(body_text)
            msg['Subject'] = subject
            parent = _one_line(ref.get('message_id', ''))
            references = _one_line(ref.get('references', ''))
            if parent:
                msg['In-Reply-To'] = parent
                references = (references + ' ' + parent).strip()
            if references:
                msg['References'] = references
            self._first = {'key': str(key) if key else '', 'message_id': str(msg['Message-ID']),
                           'subject': subject, 'references': references}
        return msg

    def _recipient_refused(self, conversation):
        if self.private_target_ok(conversation):
            return None
        return base.SendResult(ok=False, error='email: the recipient is not a whitelisted address (ALLOW)')

    def _send_text(self, conversation, text, reply_ref):
        refused = self._recipient_refused(conversation)
        if refused:
            return refused
        msg = self._build(conversation, reply_ref, text)
        msg.set_content(text)
        return self._deliver(msg, conversation)

    def _send_file(self, conversation, path, caption, reply_ref):
        refused = self._recipient_refused(conversation)
        if refused:
            return refused
        p = Path(path)
        size = os.path.getsize(p)
        if size > SEND_MAX_BYTES:
            return base.SendResult(ok=False, error='email: file is over 25 MB (' + str(size) + ' bytes); not sent')
        msg = self._build(conversation, reply_ref, caption or p.name)
        msg.set_content(caption or p.name)
        mime = mimetypes.guess_type(p.name)[0] or 'application/octet-stream'
        maintype, _, subtype = mime.partition('/')
        msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name)
        return self._deliver(msg, conversation)

    def check(self):
        conn = None
        try:
            conn = self._connect('smtp')
            conn.login(self.address, self.account.get('APP_PASSWORD', ''))
            return base.SendResult(ok=True)
        except Exception as exc:
            return base.SendResult(ok=False, error=self.redact(type(exc).__name__ + ': ' + str(exc))[:500])
        finally:
            if conn is not None:
                self._close_smtp(conn)

    # ------------------------------------------------------------------ inbound
    def _open_imap(self):
        """The IMAP connection, logged in (kept open between polls; ``transport('imap')`` or the real IMAP4_SSL)."""
        if self._imap is None:
            conn = self._connect('imap')
            try:
                conn.login(self.address, self.account.get('APP_PASSWORD', ''))
            except imaplib.IMAP4.abort as exc:
                self._drop_imap(conn)
                raise channels.ChannelError('network', self.redact(str(exc))) from None
            except imaplib.IMAP4.error as exc:
                self._drop_imap(conn)
                self._auth_until = self.clock() + self.AUTH_RETRY_S  # no busy retry of a refused password
                raise channels.ChannelError('auth', self.redact(str(exc))) from None
            except Exception as exc:
                self._drop_imap(conn)
                raise channels.ChannelError('network', self.redact(type(exc).__name__ + ': ' + str(exc))) from None
            self._idle = not self.idle_off and hasattr(conn, 'idle') and 'IDLE' in self._capabilities(conn)
            self._imap = conn
        return self._imap

    @staticmethod
    def _capabilities(conn):
        caps = {_text(c).upper() for c in (getattr(conn, 'capabilities', None) or ())}
        if 'IDLE' not in caps and hasattr(conn, 'capability'):
            try:
                typ, data = conn.capability()
                if typ == 'OK':
                    caps |= {c.upper() for d in data or () for c in _text(d).split()}
            except Exception:
                pass
        return caps

    def _drop_imap(self, conn=None):
        conn = conn if conn is not None else self._imap
        self._imap = None
        if conn is None:
            return
        try:
            conn.logout()
        except Exception:
            try:
                conn.shutdown()
            except Exception:
                pass

    def _wait(self, timeout, until):
        """Sleep at most ``timeout`` s, and not past ``until`` (the injected ``sleep``; nothing when timeout is 0)."""
        left = min(float(timeout or 0), until - self.clock())
        if left > 0:
            self.sleep(left)

    def _poll(self, timeout):
        """New mail as raw items {uidvalidity, uid, raw}, in UID order. IDLE when the server has it (Python 3.14
        ``idle``), else one SEARCH every POLL_S seconds. A network error drops the connection and raises (the
        cursor stays; the next poll reconnects). A refused login waits AUTH_RETRY_S before the next one."""
        if self.clock() < self._auth_until:
            self._wait(timeout, self._auth_until)
            return []
        if self._imap is None or not self._idle:  # polling mode (or not connected yet): only when due
            if self.clock() < self._due:
                self._wait(timeout, self._due)
                if self.clock() < self._due:
                    return []
            self._due = self.clock() + self.poll_s
        fresh = self._imap is None
        conn = self._open_imap()
        blocking = bool(timeout) and timeout > 0
        try:
            raws = []
            if not self._idle or not blocking or fresh:  # polling mode, a non-blocking call, or a new connection
                raws = self._check(conn)
                if self._idle:
                    self._due = self.clock() + self.poll_s
            if not raws and self._idle and blocking:
                event = self._idle_wait(conn, timeout)
                # M93: SEARCH after IDLE only when IDLE reported something, after the first IDLE of a connection,
                # or as the POLL_S safety net (a silently stale IDLE then loses at most POLL_S); not on every call
                if event or fresh or self.clock() >= self._due:
                    raws = self._check(conn)
                    self._due = self.clock() + self.poll_s
        except channels.ChannelError:
            self._drop_imap()
            raise
        except Exception as exc:  # abort / socket / ssl / a server BAD: reconnect next time, cursor kept
            self._drop_imap()
            raise channels.ChannelError('network', self.redact(type(exc).__name__ + ': ' + str(exc))[:500]) from None
        self._heartbeat = int(self.clock())
        if getattr(self, '_state', '') in ('needs_login', 'reconnecting'):
            self._state = ''
            self.last_error = ''
        if raws:
            self._activity = self._heartbeat
        return raws

    IDLE_MAX_S = 25 * 60  # RFC 2177: re-issue IDLE at least every 29 minutes

    def _idle_wait(self, conn, timeout):
        """IDLE for at most ``timeout`` s; True when the server said something (EXISTS ...), which ends the wait."""
        with conn.idle(duration=max(0.001, min(float(timeout), self.IDLE_MAX_S))) as idler:
            for _ in idler:
                return True
        return False

    def _uidvalidity(self, conn):
        typ, data = conn.select('INBOX', readonly=True)
        if typ != 'OK':
            raise channels.ChannelError('network', 'SELECT INBOX: ' + self.redact(_text(data[0] if data else ''))[:200])
        _, data = conn.response('UIDVALIDITY')
        for d in data or ():
            if d is not None and _text(d).strip().isdigit():
                return int(_text(d).strip())
        typ, data = conn.status('INBOX', '(UIDVALIDITY)')
        m = re.search(r'UIDVALIDITY\s+(\d+)', ' '.join(_text(d) for d in data or () if d is not None))
        if typ != 'OK' or not m:
            raise channels.ChannelError('network', 'no UIDVALIDITY for INBOX')
        return int(m.group(1))

    def _search(self, conn, criteria):
        typ, data = conn.uid('SEARCH', None, criteria)
        if typ != 'OK':
            raise channels.ChannelError('network', 'UID SEARCH failed')
        return sorted({int(u) for d in data or () if d is not None for u in _text(d).split() if u.isdigit()})

    def _check(self, conn):
        uv = self._uidvalidity(conn)
        cursor = self._cursor()
        if cursor is None or cursor[0] != uv:
            # D3: a first start does not eat the backlog; a new UIDVALIDITY resets without replay. Both audited.
            uids = self._search(conn, 'ALL')
            top = uids[-1] if uids else 0
            self.store.set('cursor', str(uv) + ':' + str(top))
            self.audit('channel.cursor_reset', reason='first_start' if cursor is None else 'uidvalidity_changed',
                       old='' if cursor is None else str(cursor[0]), new=str(uv), skipped=len(uids))
            return []
        last = cursor[1]
        new = [u for u in self._search(conn, 'UID ' + str(last + 1) + ':*') if u > last]  # n:* always has the top
        raws = []
        for uid in new[:self.FETCH_BATCH]:
            raw = self._fetch(conn, uid)
            if raw is not None:
                raws.append({'uidvalidity': uv, 'uid': uid, 'raw': raw})
        if len(new) > self.FETCH_BATCH:
            self._due = 0.0  # the rest on the next poll, without waiting POLL_S
        return raws

    FETCH_BATCH = 50

    @staticmethod
    def _fetch(conn, uid):
        typ, data = conn.uid('FETCH', str(uid), '(BODY.PEEK[])')  # PEEK: never sets the Seen flag
        if typ != 'OK':
            raise channels.ChannelError('network', 'UID FETCH ' + str(uid) + ' failed')
        for d in data or ():
            if isinstance(d, tuple) and len(d) > 1 and isinstance(d[1], (bytes, bytearray)):
                return bytes(d[1])
        return None  # expunged between SEARCH and FETCH

    def health(self):
        h = super().health()
        h['heartbeat_at'], h['activity_at'] = self._heartbeat, self._activity
        return h

    def close(self):
        self._drop_imap()

    def _body(self, msg):
        """(plain text with quotes and signature removed, html_only?)."""
        part = msg.get_body(preferencelist=('plain',))
        if part is None:
            return '', msg.get_body(preferencelist=('html',)) is not None
        try:
            text = str(part.get_content())
        except (LookupError, ValueError):  # an unknown charset
            text = (part.get_payload(decode=True) or b'').decode('utf-8', 'replace')
        return strip_quotes(text.replace('\r\n', '\n')), False

    def _attachments(self, msg, uv, uid):
        """(files, rejected_files): each attachment written exclusively (O_EXCL: never over an existing name or
        through a symlink) as ``em-<uv>-<uid>-<safe name>`` inside inbox_dir. A write failure -> Retryable."""
        files, rejected = [], []
        for part in msg.iter_attachments():
            name = _one_line(part.get_filename() or '') or 'attachment'
            data = part.get_payload(decode=True) or b''
            if len(files) >= self.max_files:
                rejected.append({'name': name, 'reason': 'more than ' + str(self.max_files) + ' attachments'})
                continue
            if len(data) > self.max_file_bytes:
                rejected.append({'name': name, 'reason': 'over %d MB (%d bytes)'
                                 % (self.max_file_bytes // (1024 * 1024), len(data))})
                continue
            if self.inbox_dir is None:
                rejected.append({'name': name, 'reason': 'no inbox directory'})
                continue
            try:
                path, fh = self.new_inbox_file(name, prefix='em-%d-%d-' % (uv, uid))
                with fh:
                    fh.write(data)
                if path.resolve().parent != self.inbox_dir.resolve():
                    raise OSError('inbox file left the inbox directory: ' + path.name)
            except channels.Retryable:
                raise
            except OSError as exc:
                raise channels.Retryable('attachment write failed: ' + type(exc).__name__ + ': ' + str(exc))
            files.append({'name': name, 'path': str(path.absolute()), 'size': len(data)})
        return files, rejected

    def parse_inbound(self, raw):
        if not isinstance(raw, dict) or not isinstance(raw.get('raw'), (bytes, bytearray)):
            raise ValueError('not an IMAP item {uidvalidity, uid, raw}')
        uv, uid = int(raw['uidvalidity']), int(raw['uid'])
        msg = email.message_from_bytes(bytes(raw['raw']), policy=email.policy.default)
        ok, reason = verify(msg, self.account.allow, self.trusted_authserv)
        froms = [a for _, a in email.utils.getaddresses([str(h) for h in (msg.get_all('From') or [])]) if a]
        sender = froms[0].strip().lower() if froms else ''
        names = [n for n, _ in email.utils.getaddresses([str(h) for h in (msg.get_all('From') or [])])]
        common = dict(conversation=sender or '(unknown)', message_id=self.account.name + ':' + str(uv) + ':' + str(uid),
                      sender=sender or '(unknown)', private=True, verified=ok, at=int(self.clock()))
        if not ok:
            # Not accepted: no body is read, no file is written; the base drops it by policy and logs who.
            self.audit('channel.drop_auth', reason=reason, uid=uid, uidvalidity=uv)
            return [envelope.make('email', self.account.name, text='',
                                  rejected_files=[{'name': '(邮件)', 'reason': 'sender not verified: ' + reason}],
                                  **common)]
        reply_ref = {'message_id': _one_line(msg.get('Message-ID', '')),
                     'references': _one_line(msg.get('References', '')),
                     'subject': _one_line(msg.get('Subject', ''))}
        text, html_only = self._body(msg)
        if len(text) > envelope.TEXT_MAX:  # D4: no envelope, no file written; the cursor still moves past it
            self.audit('channel.drop_size', uid=uid, uidvalidity=uv, chars=len(text), limit=envelope.TEXT_MAX)
            return []
        files, rejected = self._attachments(msg, uv, uid)
        text, prov = provenance.email_provenance(text)  # P8b D-1: one '/' line stays a command, else email_body
        if html_only:
            rejected.insert(0, {'name': '(正文)', 'reason': 'html only'})
        if not text and not prov and not files and not rejected:
            rejected.append({'name': '(正文)', 'reason': 'empty mail'})
        return [envelope.make('email', self.account.name, text=text, reply_ref=reply_ref,
                              sender_name=_one_line(names[0]) if names and names[0] else None,
                              files=files or None, rejected_files=rejected or None, provenance=prov or None,
                              **common)]

    def _ack(self, raw):
        if not isinstance(raw, dict) or 'uid' not in raw or 'uidvalidity' not in raw:
            return
        uv, uid = int(raw['uidvalidity']), int(raw['uid'])
        current = self._cursor()
        if current and current[0] == uv and current[1] >= uid:
            return
        self.store.set('cursor', str(uv) + ':' + str(uid))

    def _cursor(self):
        value = self.store.get('cursor')
        if not value:
            return None
        uv, _, last = str(value).partition(':')
        try:
            return int(uv), int(last)
        except ValueError:
            return None


ADAPTER = EmailChannel
