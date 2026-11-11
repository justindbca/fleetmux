"""Outbound link rewriting and preview off (package P8f; brief v2.2 §8.2 / §8.4; contracts §8 「出站扫描与预览」).

- ``rewrite(text, policy) -> (text, hits)``: every URL whose host is not allowed -> ``defang`` (``host[.]tld/path``,
  scheme dropped) or ``strip`` (``[链接已移除：host[.]tld]``); bare hosts with or without a path, ``www.`` hosts;
  markdown images removed whatever the allow list says; ``*.x`` = subdomains of x only, an exact host never implies
  its subdomains; host match ignores case; idempotent (a rewritten text is left alone). ``hits``: host names only.
  No policy, or ``links.allow`` containing ``'*'`` -> ``(text, [])``.
- ``rewrite_item(item, policy, *, audit=None) -> item``: text, card title / text, caption; never button commands,
  reply_ref or file paths; nothing rewritten -> the item itself; else a copy, and one ``security.link_rewritten``
  audit row (``item_id``, ``kind`` = the item's event, ``hosts``: host names only, never a path or query).

What counts as a bare host (survey §6.1 L1): dot-separated labels whose last label is letters (2+; a ``www.`` host
any letter label), not glued to a preceding ``/``, word character, ``.``, ``-`` or ``@`` (part of a longer path,
identifier or mail address). So local paths (``docs/design.md``) and numeric versions (``2.4.10``) stay; a lone
``README.md`` may be rewritten (conservative). ``hxxp`` spellings are not URLs. No TLD table.
Telegram preview: ``base.Channel.link_preview`` (False when ``links.preview == 'off'``), read by the Telegram adapter.
"""
import re

_LABEL = r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
_HOST = (r'(?:[Ww][Ww][Ww]\.(?:' + _LABEL + r'\.)*[A-Za-z][A-Za-z0-9-]*'
         r'|(?:' + _LABEL + r'\.)+[A-Za-z]{2,63})')
_PATH_CHARS = r'[^\s<>"\'`()\[\]{}]'
_TRAIL = '.,;:!?…'

_IMAGE = r'(?P<image>!\[[^\]\n]*\]\([^)\s]*(?:\s+"[^"\n]*")?\))'
_URL = r'(?P<url>(?i:\b(?:https?|ftp)://)[^\s/?#<>"\'`()\[\]{}]+(?:[/?#]' + _PATH_CHARS + r'*)?)'
_BARE = (r'(?P<bare>(?<![/\w.@-])' + _HOST + r'(?![A-Za-z0-9_-])(?:[/?#]' + _PATH_CHARS + r'*)?)')
_ANY = re.compile(_IMAGE + '|' + _URL + '|' + _BARE)
_BARE_ONLY = re.compile(_BARE)

MARKER = '[链接已移除：{host}]'


def _unchanged(policy):
    if policy is None:
        return True
    allow = ((policy.get('links') or {}).get('allow')) or []
    return '*' in allow


def _allowed(host, allow):
    host = host.lower().rstrip('.')
    for entry in allow:
        entry = str(entry).lower().strip()
        if entry.startswith('*.'):
            if host.endswith(entry[1:]) and host != entry[2:]:
                return True
        elif host == entry:
            return True
    return False


def _defang(host):
    return host.replace('.', '[.]')


def _split_trail(s):
    """(url, trailing sentence punctuation)."""
    end = len(s)
    while end and s[end - 1] in _TRAIL:
        end -= 1
    return s[:end], s[end:]


def _split_host(rest):
    """``rest`` = the URL without its scheme -> (userinfo@, host, :port, path)."""
    m = re.match(r'([^/?#]*)(.*)', rest, re.S)
    authority, path = m.group(1), m.group(2)
    userinfo, _, hostport = authority.rpartition('@')
    host, colon, port = hostport.partition(':')
    return (userinfo + '@' if userinfo else ''), host, (colon + port), path


class _Rewriter:
    def __init__(self, policy):
        links = policy.get('links') or {}
        self.allow = list(links.get('allow') or [])
        self.mode = links.get('mode') or 'defang'
        self.hits = []

    def path(self, path):
        """A rewritten URL's path: bare hosts inside it are defanged too, so a second pass finds nothing."""
        return _BARE_ONLY.sub(lambda m: self.bare(m.group('bare'), in_path=True), path)

    def url(self, raw):
        url, trail = _split_trail(raw)
        rest = url.split('://', 1)[1]
        userinfo, host, port, path = _split_host(rest)
        if not host or _allowed(host, self.allow):
            return raw
        self.hits.append(host.lower())
        if self.mode == 'strip':
            return MARKER.format(host=_defang(host)) + trail
        return userinfo + _defang(host) + port + self.path(path) + trail

    def bare(self, raw, *, in_path=False):
        url, trail = _split_trail(raw)
        m = re.match(_HOST, url)
        host, path = m.group(0), url[m.end():]
        if _allowed(host, self.allow):
            return raw
        if not in_path:
            self.hits.append(host.lower())
        if self.mode == 'strip' and not in_path:
            return MARKER.format(host=_defang(host)) + trail
        return _defang(host) + self.path(path) + trail

    def sub(self, m):
        if m.group('image') is not None:
            return ''
        if m.group('url') is not None:
            return self.url(m.group('url'))
        return self.bare(m.group('bare'))


def rewrite(text, policy):
    if _unchanged(policy) or not isinstance(text, str) or not text:
        return text, []
    rw = _Rewriter(policy)
    return _ANY.sub(rw.sub, text), rw.hits


def rewrite_item(item, policy, *, audit=None):
    if _unchanged(policy):
        return item
    hosts, out = [], dict(item)

    def fix(value):
        new, hits = rewrite(value, policy)
        hosts.extend(hits)
        return new
    if isinstance(item.get('text'), str):
        out['text'] = fix(item['text'])
    card = item.get('card')
    if isinstance(card, dict):
        out['card'] = dict(card, title=fix(card.get('title') or ''), text=fix(card.get('text') or ''))
    if isinstance(item.get('caption'), str) and item['caption']:
        out['caption'] = fix(item['caption'])
    if out == item:
        return item
    if audit is not None:
        try:
            audit('security.link_rewritten', item_id=item.get('id') or '', kind=item.get('event') or '',
                  hosts=sorted(set(hosts)))
        except Exception:  # noqa: BLE001 - an audit sink failure must not stop the send
            pass
    return out
