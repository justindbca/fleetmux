#!/usr/bin/env python3
"""Redact a fleetmux `--record` file before it is committed as a replay fixture (PROTOCOL.md §7.1, brief-m03 P-10).

Usage: redact_recording.py [--literal TEXT]... IN.rec OUT.rec

Rewrites the `bytes` of every entry. E-mail addresses, absolute paths that start with a user's home directory
(macOS `/Users/<name>`, Linux `/<home>/<name>`, `/root`) and token shapes (`sk-…`, `ghp_…` and the other GitHub
token prefixes, hyphenated 8-4-4-4-12 hex UUIDs in any case, runs of >= 32 hex digits) are replaced by placeholders of the same byte length, so every entry
keeps its `offset_us` and length and the file stays readable by `fleetmux_proto::record::read`. `--literal TEXT`
(repeatable) also replaces every occurrence of TEXT, e.g. a user or host name shown in a shell prompt.

Matching runs over the concatenation of all entries, seen as text: escape-sequence syntax is removed from the
view first, so a secret split across two PTY reads or styled in the middle (`ali ESC[1m ce@…`) is still found
whole, and only its text bytes are rewritten; every escape-syntax byte stays where it was. Home paths may hold
non-ASCII characters in any component, the last one included; every byte of such a character becomes one ASCII
byte, so the output is UTF-8 of the same length. Token prefixes are matched even when glued to the text before them, at the cost of an
occasional false positive, which only blanks that run. OSC payloads (titles, OSC 8 links, OSC 7 cwd) are redacted
like text. Input that cannot be redacted safely is refused, never half-redacted: non-UTF-8 bytes (including 8-bit
C1 controls), a secret-shaped match inside a DCS / APC / PM / SOS payload or an OSC 52 clipboard payload, or an
OSC 52 clipboard whose base64 holds a secret. This tool does not replace the manual privacy review.

Exit codes: 0 written; 1 malformed input (truncated entry, length 0 or over 1 MiB), refused input (above) or
I/O error, nothing written;
2 usage. The output is written to a temporary file next to OUT (mode 0600) and renamed over it.
"""

import base64
import os
import re
import struct
import sys
import tempfile

MAX_ENTRY = 1024 * 1024
HEADER = struct.Struct('>QI')
ESC = 0x1B

# Built from parts: this file must not contain a literal Linux home-directory path.
_HOMES = rb'/Users/|/' + b'home' + rb'/|/root(?=[/\s"\'):;,]|$)'
_ASCII = rb'\-A-Za-z0-9._~+@%=,'  # `-` escaped: `_ANY` appends a range
_ANY = _ASCII + rb'\x80-\xff'
# Every component of a home path, the last one included, may hold any UTF-8 character: the path runs until a
# byte outside the class (whitespace, quotes, `:;()<>|`, a control or escape syntax), so text glued to its end
# (`notes.mdé`) is taken as part of the path.
_PATH_REST = rb'[/' + _ANY + rb']*'
PATTERNS = [
    ('email', re.compile(rb'(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}')),
    ('path', re.compile(rb'(?:' + _HOMES + rb')' + _PATH_REST)),
    ('sk', re.compile(rb'sk-[A-Za-z0-9_-]{16,}')),
    ('github', re.compile(rb'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})')),
    # No boundary checks: a UUID glued to letters or more hex is still taken, so no UUID-shaped text is left.
    ('uuid', re.compile(rb'[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}')),
    ('hex', re.compile(rb'(?<![0-9A-Fa-f])[0-9A-Fa-f]{32,}(?![0-9A-Fa-f])')),
]


class RecordError(Exception):
    pass


def read_entries(data):
    """[(offset_us, bytes)] of a whole `.rec` file; `RecordError` for a malformed one."""
    out, pos = [], 0
    while pos < len(data):
        if len(data) - pos < HEADER.size:
            raise RecordError(f'entry header truncated at byte {pos} after {len(out)} complete entries')
        offset_us, length = HEADER.unpack_from(data, pos)
        if length == 0 or length > MAX_ENTRY:
            raise RecordError(f'entry {len(out)} at byte {pos}: length {length} (allowed 1-{MAX_ENTRY})')
        start = pos + HEADER.size
        if len(data) - start < length:
            raise RecordError(f'entry {len(out)} at byte {pos} truncated: {len(data) - start} of {length} bytes')
        out.append((offset_us, data[start:start + length]))
        pos = start + length
    return out


def write_entries(entries):
    return b''.join(HEADER.pack(offset_us, len(b)) + b for offset_us, b in entries)


def _string_end(data, i):
    """Index of the BEL or `ESC \\` ending the OSC / DCS / APC / PM / SOS payload that starts at `i` (or len)."""
    while i < len(data):
        if data[i] == 0x07 or (data[i] == ESC and i + 1 < len(data) and data[i + 1] == 0x5C):
            return i
        i += 1
    return i


def check_utf8(data):
    """`RecordError` unless `data` is UTF-8; only a character cut by the end of the recording is accepted. This also
    refuses 8-bit C1 controls (0x80-0x9F outside a character), which the redactor cannot parse safely."""
    try:
        data.decode('utf-8')
    except UnicodeDecodeError as e:
        if not (e.end == len(data) and 'unexpected end' in e.reason):
            raise RecordError(f'not UTF-8 at byte {e.start} ({e.reason}): 8-bit C1 controls and binary '
                              f'output are not supported, refusing') from None


def text_view(data):
    """(view, index, opaque): the text a reader sees, with escape-sequence syntax removed.

    `view[k]` is `data[index[k]]` for text bytes; CSI and other short sequences vanish, so text styled in the middle
    (`ali ESC[1m ce@…`) is contiguous again. The payload of an OSC / DCS / APC / PM / SOS string is text of its own:
    a NUL (`index` -1) stands for its introducer and its terminator, so no match runs into or out of it.
    `opaque` lists (start, end, what) payload ranges whose bytes must not be rewritten (DCS / APC / PM / SOS, the
    OSC 52 clipboard): a match there means the input cannot be redacted safely."""
    view, index, opaque = bytearray(), [], []
    n, i = len(data), 0

    def sep():
        if not view or view[-1] != 0:
            view.append(0)
            index.append(-1)

    while i < n:
        c = data[i]
        if c != ESC:
            view.append(c)
            index.append(i)
            i += 1
            continue
        if i + 1 >= n:
            break
        intro = data[i + 1]
        if intro == ESC:  # a lone ESC: the next one starts its own sequence
            i += 1
        elif intro == 0x5B:  # CSI: parameters 0x30-0x3F, intermediates 0x20-0x2F, one final byte 0x40-0x7E
            j = i + 2
            while j < n and 0x20 <= data[j] <= 0x3F:
                j += 1
            if j < n and 0x40 <= data[j] <= 0x7E:
                j += 1
            i = j
        elif intro in b']P_^X':  # string: introducer, redactable payload, BEL or ESC \\ terminator
            start = i + 2
            end = _string_end(data, start)
            sep()
            payload = data[start:end]
            if intro != 0x5D:
                opaque.append((start, end, 'DCS/APC/PM/SOS payload'))
            elif payload.startswith(b'52;'):
                opaque.append((start, end, 'OSC 52 clipboard payload'))
                _check_clipboard(payload, start)
            for k in range(start, end):
                view.append(data[k])
                index.append(k)
            sep()
            i = end + (2 if end < n and data[end] == ESC else 1)
        elif intro in b'()*+-./#%':  # charset designation etc.: one more byte
            i += 3
        else:
            i += 2
    return bytes(view), index, opaque


def _check_clipboard(payload, at):
    """OSC 52 data is base64: decode it and refuse when it holds anything secret-shaped."""
    parts = payload.split(b';', 2)
    data = parts[2] if len(parts) == 3 else b''
    if data in (b'', b'?'):
        return
    try:
        decoded = base64.b64decode(data, validate=True)
    except ValueError:
        raise RecordError(f'OSC 52 payload at byte {at} is not base64, refusing') from None
    for kind, pat in PATTERNS:
        if pat.search(decoded):
            raise RecordError(f'OSC 52 clipboard at byte {at} carries a {kind}; it cannot be redacted in place, '
                              f'refusing')


def placeholder(kind, text):
    """Same-length ASCII stand-in for `text`; idempotent (redacting a placeholder gives it back). Every byte of a
    non-ASCII character becomes one ASCII byte, so the output stays UTF-8 and keeps its length."""
    if kind == 'email':
        return bytes(c if c in b'@.' else 0x78 for c in text)  # x
    if kind == 'path':
        return bytes(c if c == 0x2F else 0x78 for c in text)  # keeps the slashes; no home prefix is left
    if kind == 'sk':
        return b'sk-' + b'X' * (len(text) - 3)
    if kind == 'github':
        cut = text.index(b'_') + 1 if not text.startswith(b'github_pat_') else len(b'github_pat_')
        return text[:cut] + b'X' * (len(text) - cut)
    if kind == 'uuid':
        return bytes(c if c == 0x2D else 0x58 for c in text)  # X keeps the dashes; X is no hex digit
    if kind == 'hex':
        return b'0' * len(text)
    return b'x' * len(text)  # literal


def redact_stream(data, literals=()):
    """(redacted bytes of the same length, {kind: count}). Only text bytes change; escape syntax never does.
    `RecordError` when the input cannot be redacted safely (see `check_utf8` / `text_view`)."""
    check_utf8(data)
    view, index, opaque = text_view(data)
    out = bytearray(data)
    counts = {}
    pats = list(PATTERNS) + [('literal', re.compile(re.escape(lit))) for lit in literals if lit]
    for kind, pat in pats:
        for m in pat.finditer(view):
            a, b = m.span()
            where = index[a:b]
            for lo, hi, what in opaque:
                if any(lo <= k < hi for k in where):
                    raise RecordError(f'{kind} inside a {what} at byte {lo}: cannot redact it safely, refusing')
            for k, c in zip(where, placeholder(kind, m.group())):
                out[k] = c
            counts[kind] = counts.get(kind, 0) + 1
    assert len(out) == len(data)
    check_utf8(bytes(out))
    return bytes(out), counts


def redact_record(data, literals=()):
    """Redact a whole `.rec` file: (new file bytes, {kind: count}). Offsets and entry lengths are unchanged."""
    entries = read_entries(data)
    stream, counts = redact_stream(b''.join(b for _, b in entries), literals)
    out, pos = [], 0
    for offset_us, b in entries:
        out.append((offset_us, stream[pos:pos + len(b)]))
        pos += len(b)
    return write_entries(out), counts


USAGE = 'usage: redact_recording.py [--literal TEXT]... IN.rec OUT.rec'


def main(argv):
    literals, paths, args = [], [], list(argv[1:])
    while args:
        a = args.pop(0)
        if a == '--literal' and args:
            literals.append(os.fsencode(args.pop(0)))
        elif a.startswith('-'):
            print(USAGE, file=sys.stderr)
            return 2
        else:
            paths.append(a)
    if len(paths) != 2:
        print(USAGE, file=sys.stderr)
        return 2
    src, dst = paths
    try:
        with open(src, 'rb') as f:
            data = f.read()
        out, counts = redact_record(data, literals)
        fd, tmp = tempfile.mkstemp(prefix='.redact-', dir=os.path.dirname(os.path.abspath(dst)))
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(out)
            os.replace(tmp, dst)
        except BaseException:
            os.unlink(tmp)
            raise
    except RecordError as e:
        print(f'redact_recording: {src}: {e}; nothing written', file=sys.stderr)
        return 1
    except OSError as e:
        print(f'redact_recording: {e}; nothing written', file=sys.stderr)
        return 1
    summary = ', '.join(f'{k} {v}' for k, v in sorted(counts.items())) or 'nothing found'
    print(f'redact_recording: {src} -> {dst}: {summary}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
