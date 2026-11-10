"""Outbound secret scanning (package P8e; brief v2.2 §8.2 / §8.4; contracts §8 「出站扫描与预览」「精确值集与外发被拦」).

- ``RULES``: the builtin high-confidence rules, ``((name, compiled regex), ...)`` (11 rules, rewritten after the
  gitleaks / trufflehog high-confidence set). Order matters: on overlapping matches the earlier rule wins.
- ``Scanner(policy, *, accounts=None, env=None, read_file=None, r=None, run=None)``: builtin rules plus the exact
  value set (``secrets.values_from``: ``accounts`` = the credential keys of every account file, ``env:NAME``,
  ``file:<KEY=VALUE file>``, ``glob:<pattern>``; plus the run's canary value when ``canary.enabled`` is on).
  Values under 6 characters are not kept. Configurable encoding variants (``secrets.variants``: base64 standard
  and urlsafe without ``=``, hex, URL encoding); the always-on normalisation (whitespace and hyphens stripped from
  both the value and the scanned text) is not configurable (F5). Values live only in this object's memory: never
  in ``repr`` / ``str``, in ``errors``, in an exception, in a scan result or in an audit row.
- ``Scanner.scan(text) -> [{rule, span, redacted}]``: ``span`` indexes the original text, ``redacted`` is ``***``.
- ``guard(item, policy, *, scanner=None, notify=None, audit=None) -> item | None``: ``policy`` None -> ``item``
  itself, nothing read or written. Otherwise a hit blocks the whole item (never a partial replacement: fragments
  could be put back together), audits ``security.secret_blocked`` (``rule / item_id / kind`` = the item's event, only) and hands
  ``notify`` one fixed ``secret_blocked`` notice (same event and routing, new id) -- except for a notice itself
  (``is_notice``), which is blocked without a further notice.
- ``is_notice(item) -> bool``: recognised by the frozen receipt template, so it holds across processes.

Scanned per item: ``text``; a card's title, text and buttons; a file's ``caption``; the content of a file the fleet
generated under an ``outbox`` directory (``<run>/outbox`` when the scanner knows the run, else any directory
named ``outbox``; unreadable, not UTF-8 or over ``OUTBOX_MAX_BYTES`` -> blocked, rule ``unscannable``). Not
scanned: ``reply_ref``, ``update``, the content of a user-named file outside ``outbox``.
"""
import base64
import binascii
import glob as _glob
import os
import re
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

from cmux_fleet import channels
from cmux_fleet.channels import envelope, security

MIN_VALUE_LEN = 6
OUTBOX_MAX_BYTES = 2 * 1024 * 1024
UNSCANNABLE = 'unscannable'

RULES = (
    ('aws', re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b')),
    ('github', re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})')),
    ('slack', re.compile(r'\bxox[baprs]-[0-9A-Za-z-]{10,}')),
    ('telegram', re.compile(r'(?<![0-9])[0-9]{8,10}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])')),
    ('anthropic', re.compile(r'\bsk-ant-[A-Za-z0-9_-]{20,}')),
    ('openai', re.compile(r'\bsk-(?!ant-)(?:proj-|svcacct-|admin-)?[A-Za-z0-9_-]{20,}')),
    ('google', re.compile(r'\bAIza[0-9A-Za-z_-]{35}')),
    ('jwt', re.compile(r'\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}')),
    ('pem', re.compile(r'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----')),
    ('bearer', re.compile(r'(?i)\b(?:authorization\s*[:=]\s*)?bearer\s+[A-Za-z0-9._~+/-]{16,}=*')),
    ('generic', re.compile(r'(?i)(?:api[_-]?key|secret|token|password|passwd)["\']?\s*[:=]\s*["\']?[^\s"\']{12,}')),
)

_STRIP = re.compile(r'[\s-]+')
_NOTICE = re.compile(re.escape(envelope.RECEIPTS['secret_blocked'])
                     .replace(re.escape('{rule}'), r'[^\s（）()]+').replace(re.escape('{id}'), r'[^\s（）()]+'))


def _norm(value):
    return _STRIP.sub('', value)


def _forms(value, variants):
    """Every form of one value to look for (before normalisation): the value, its hyphen-free form and the
    configured encodings of both."""
    bases = {value, _norm(value)}
    out = set(bases)
    for b in bases:
        raw = b.encode('utf-8')
        if 'base64' in variants:
            out.add(base64.b64encode(raw).decode().rstrip('='))
            out.add(base64.urlsafe_b64encode(raw).decode().rstrip('='))
        if 'hex' in variants:
            h = binascii.hexlify(raw).decode()
            out.update((h, h.upper()))
        if 'url' in variants:
            out.add(urllib.parse.quote(b, safe=''))
    return {n for n in (_norm(f) for f in out) if len(n) >= MIN_VALUE_LEN}


def _kv_values(text):
    """KEY=VALUE lines -> [(key, value)]; comments and blanks skipped, one pair of outer quotes removed."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key, value = key.strip(), value.strip()
        if key.startswith('export '):
            key = key[len('export '):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'':
            value = value[1:-1]
        out.append((key, value))
    return out


def _default_read(path):
    return Path(path).read_text(encoding='utf-8')


class Scanner:
    def __init__(self, policy, *, accounts=None, env=None, read_file=None, r=None, run=None):
        section = dict(security.DEFAULTS['secrets'])
        section.update(((policy or {}).get('secrets') or {}))
        self._variants = frozenset(section.get('variants') or ())
        self._run = Path(run) if run else None
        self._r = r  # only for canary.trip (P8h): the pause and the alert
        self._read = read_file or _default_read
        self._env = os.environ if env is None else env
        self._values = []  # [(rule, frozenset of normalised forms)]; never exposed
        self.errors = []   # human-readable, never a value
        for source in section.get('values_from') or ():
            if source == 'accounts':
                self._load_accounts(accounts)
            elif source.startswith('env:'):
                name = source[4:]
                value = self._env.get(name)
                if value:
                    self._add('value:env:' + name, value)
            elif source.startswith('file:'):
                self._load_file(source[5:], 'value:file:')
            elif source.startswith('glob:'):
                for path in sorted(_glob.glob(os.path.expanduser(source[5:]))):
                    self._load_file(path, 'value:glob:')
        canary = (policy or {}).get('canary') or {}
        if canary.get('enabled') == 'on':
            self._load_canary(r, canary.get('path'))

    # -------------------------------------------------------------- loading
    def _add(self, rule, value):
        if isinstance(value, bytes):
            value = value.decode('utf-8', 'replace')
        if not isinstance(value, str) or len(value) < MIN_VALUE_LEN:
            return
        forms = _forms(value, self._variants)
        if forms:
            self._values.append((rule, frozenset(forms)))

    def _load_accounts(self, accounts):
        if accounts is None:
            loaded = []
            folder = channels.config_dir() / 'channels'
            for path in sorted(folder.glob('*.env')) if folder.is_dir() else ():
                type_, sep, name = path.stem.partition('-')
                if not sep:
                    continue
                try:
                    loaded.append(channels.parse_account_text(type_, name, path.read_text(encoding='utf-8')))
                except Exception as exc:  # noqa: BLE001 - name the file and the error type only, never a value
                    self.errors.append('accounts: ' + path.name + ': unreadable (' + type(exc).__name__ + ')')
            accounts = loaded
        elif isinstance(accounts, dict):
            accounts = [SimpleNamespace(name=n, values=v) if isinstance(v, dict) else v for n, v in accounts.items()]
        for account in accounts:
            values = getattr(account, 'values', None) or {}
            for key in sorted(values):
                if channels.SECRET_KEY_RE.search(key):
                    self._add('value:' + account.name, values[key])

    def _load_file(self, path, prefix):
        try:
            text = self._read(path)
        except Exception as exc:  # noqa: BLE001 - never str(exc): it may carry the very value that failed to load
            self.errors.append(prefix[len('value:'):] + str(path) + ': read failed (' + type(exc).__name__ + ')')
            return
        if isinstance(text, bytes):
            text = text.decode('utf-8', 'replace')
        for key, value in _kv_values(text or ''):
            self._add(prefix + key, value)

    def _load_canary(self, r, rel):
        value = None
        if r is not None:
            try:
                value = r.get(channels.CANARY)
            except Exception as exc:  # noqa: BLE001
                self.errors.append('canary: redis read failed (' + type(exc).__name__ + ')')
        if not value and self._run is not None and rel:
            try:
                for key, v in _kv_values(self._read(self._run / rel)):
                    if key == 'CANARY_TOKEN':
                        value = v
            except Exception as exc:  # noqa: BLE001
                self.errors.append('canary: file read failed (' + type(exc).__name__ + ')')
        if value:
            self._add('canary', value)

    # -------------------------------------------------------------- scanning
    def __repr__(self):
        return 'Scanner(rules=' + str(len(RULES)) + ', values=' + str(len(self._values)) + ', errors=' \
            + str(len(self.errors)) + ')'

    __str__ = __repr__

    def scan(self, text):
        """``[{rule, span, redacted}]`` in text order; overlapping matches reported once (exact values first,
        then RULES order)."""
        if not isinstance(text, str) or not text:
            return []
        found = []
        if self._values:
            keep = [i for i, ch in enumerate(text) if not (ch.isspace() or ch == '-')]
            flat = ''.join(text[i] for i in keep)
            for rule, forms in self._values:
                for form in forms:
                    start = flat.find(form)
                    while start >= 0:
                        found.append((rule, (keep[start], keep[start + len(form) - 1] + 1)))
                        start = flat.find(form, start + 1)
        for rule, rx in RULES:
            for m in rx.finditer(text):
                found.append((rule, m.span()))
        hits = []
        for rule, span in found:
            if any(span[0] < s[1] and s[0] < span[1] for _, s in hits):
                continue
            hits.append((rule, span))
        hits.sort(key=lambda h: h[1])
        return [{'rule': rule, 'span': span, 'redacted': '***'} for rule, span in hits]

    def scan_item(self, item):
        """The first hit's rule name for an outbound item, or ''."""
        rules = self.item_rules(item)
        return rules[0] if rules else ''

    def item_rules(self, item):
        """Every rule hit anywhere in the item, in scan order (``unscannable`` for an outbox file that cannot be
        read); [] = clean."""
        rules = []
        for text in self._item_texts(item):
            if text is None:
                rules.append(UNSCANNABLE)
                continue
            rules += [h['rule'] for h in self.scan(text)]
        return rules

    def _item_texts(self, item):
        yield item.get('text') or ''
        card = item.get('card')
        if isinstance(card, dict):
            yield card.get('title') or ''
            yield card.get('text') or ''
            for b in card.get('buttons') or ():
                if isinstance(b, dict):
                    yield str(b.get('label') or '')
                    yield str(b.get('command') or '')
        yield item.get('caption') or ''
        if item.get('file') and self._in_outbox(item['file']):
            yield self._outbox_text(item['file'])

    def _in_outbox(self, path):
        p = Path(str(path))
        if self._run is not None:
            try:
                p.resolve().relative_to((self._run / 'outbox').resolve())
                return True
            except ValueError:
                pass
        return p.parent.name == 'outbox'

    @staticmethod
    def _outbox_text(path):
        """The file's text, or None when it cannot be scanned (missing, unreadable, too big, not UTF-8)."""
        try:
            with open(str(path), 'rb') as fh:
                data = fh.read(OUTBOX_MAX_BYTES + 1)
            if len(data) > OUTBOX_MAX_BYTES:
                return None
            return data.decode('utf-8')
        except (OSError, UnicodeDecodeError):
            return None

    def mask_record(self, record):
        """``(copy, hit)``: every string (nested dicts / lists too) holding a hit becomes ``***``; on any hit the
        copy also carries ``secret_blocked: 1`` (P8g writes the line anyway, never the value)."""
        hit = [False]

        def walk(v):
            if isinstance(v, str):
                if self.scan(v):
                    hit[0] = True
                    return '***'
                return v
            if isinstance(v, dict):
                return {k: walk(x) for k, x in v.items()}
            if isinstance(v, (list, tuple)):
                return [walk(x) for x in v]
            return v
        out = walk(dict(record))
        if hit[0]:
            out['secret_blocked'] = 1
        return out, hit[0]


def is_notice(item):
    """True for a ``secret_blocked`` notice: a text item whose text is exactly the frozen receipt."""
    if not isinstance(item, dict) or item.get('card') is not None or item.get('file') is not None:
        return False
    text = item.get('text')
    return isinstance(text, str) and _NOTICE.fullmatch(text) is not None


def _notice(item, rule):
    return envelope.outbound(
        item.get('event') or 'result', target=item.get('target') or 'last',
        text=envelope.receipt('secret_blocked', rule=rule, id=item.get('id') or ''),
        request_id=item.get('request_id') or '', channel=item.get('channel') or '',
        account=item.get('account') or '', conversation=item.get('conversation') or '',
        reply_ref=item.get('reply_ref') or None, user_only=bool(item.get('user_only')))


def guard(item, policy, *, scanner=None, notify=None, audit=None):
    if policy is None:  # no security.yaml: unchanged (brief §8.7)
        return item
    if scanner is None:
        scanner = Scanner(policy)
    canary_hit = False
    if hasattr(scanner, 'item_rules'):
        rules = scanner.item_rules(item)
        rule = rules[0] if rules else ''
        canary_hit = 'canary' in rules
    elif hasattr(scanner, 'scan_item'):
        rule = scanner.scan_item(item)
        canary_hit = rule == 'canary'
    else:  # a bare scan()-only scanner: text, card and caption
        card = item.get('card') if isinstance(item.get('card'), dict) else {}
        hits = scanner.scan('\n'.join(t for t in (item.get('text'), card.get('title'), card.get('text'),
                                                  item.get('caption')) if t))
        rule = hits[0]['rule'] if hits else ''
    if not rule:
        return item
    if audit is not None:
        # the item's event goes in as ``kind`` (queue._audit's convention): ``event`` is audit()'s own first argument
        try:
            audit('security.secret_blocked', rule=rule, item_id=item.get('id') or '', kind=item.get('event') or '')
        except Exception:  # noqa: BLE001 - an audit sink failure must not let the item through
            pass
    if notify is not None and not is_notice(item):
        notify(_notice(item, rule))
    if canary_hit:  # P8h: the decoy left the box -> alert + limits.on_trip (after the block and its notice)
        from cmux_fleet.channels import canary
        canary.trip(getattr(scanner, '_r', None), item.get('id') or '', policy, audit=audit, notify=notify)
    return None
