#!/usr/bin/env python3
"""Convert one fleetmux `--record` file (PROTOCOL.md §7) into an asciicast v2 file for players and GIF / SVG tools.

Usage: rec_to_cast.py --cols N --rows N [--title TEXT] [--idle-limit S] IN.rec OUT.cast

Run it on a recording that already went through scripts/redact_recording.py: this tool copies the bytes as they
are. Each entry (``u64 offset_us`` + ``u32 length`` + bytes, big-endian) becomes one ``[seconds, "o", text]``
event; UTF-8 split across two entries is joined. ``--idle-limit`` caps every pause at S seconds (also written as
the header's ``idle_time_limit``). The header has no timestamp and no environment, so the cast names no machine.

Exit codes: 0 written; 1 malformed input (truncated entry, length 0 or over 1 MiB) or I/O error, nothing written;
2 usage.
"""
import argparse
import codecs
import json
import os
import struct
import sys
import tempfile

HEADER = struct.Struct('>QI')
MAX_ENTRY = 1024 * 1024


def entries(data):
    """(offset_us, bytes) of every entry; ValueError on a malformed file (PROTOCOL.md §7)."""
    at = 0
    while at < len(data):
        if at + HEADER.size > len(data):
            raise ValueError('truncated entry header at byte %d' % at)
        offset, length = HEADER.unpack_from(data, at)
        if not 0 < length <= MAX_ENTRY:
            raise ValueError('entry length %d at byte %d is out of range' % (length, at))
        at += HEADER.size
        if at + length > len(data):
            raise ValueError('truncated entry at byte %d' % at)
        yield offset, data[at:at + length]
        at += length


def convert(data, cols, rows, title=None, idle_limit=None):
    """The asciicast v2 text of one recording."""
    header = {'version': 2, 'width': cols, 'height': rows}
    if title:
        header['title'] = title
    if idle_limit:
        header['idle_time_limit'] = idle_limit
    lines = [json.dumps(header)]
    dec = codecs.getincrementaldecoder('utf-8')('replace')
    clock, last = 0.0, None
    for offset, chunk in entries(data):
        now = offset / 1e6
        step = 0.0 if last is None else max(0.0, now - last)
        if idle_limit:
            step = min(step, idle_limit)
        clock, last = clock + step, now
        text = dec.decode(chunk)
        if text:
            lines.append(json.dumps([round(clock, 6), 'o', text], ensure_ascii=False))
    tail = dec.decode(b'', final=True)
    if tail:
        lines.append(json.dumps([round(clock, 6), 'o', tail], ensure_ascii=False))
    return '\n'.join(lines) + '\n'


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n', 1)[0])
    p.add_argument('--cols', type=int, required=True)
    p.add_argument('--rows', type=int, required=True)
    p.add_argument('--title')
    p.add_argument('--idle-limit', type=float)
    p.add_argument('src')
    p.add_argument('dst')
    a = p.parse_args(argv)
    try:
        with open(a.src, 'rb') as f:
            text = convert(f.read(), a.cols, a.rows, a.title, a.idle_limit)
        out_dir = os.path.dirname(os.path.abspath(a.dst))
        fd, tmp = tempfile.mkstemp(dir=out_dir, prefix='.rec_to_cast.')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, a.dst)
    except (OSError, ValueError) as exc:
        print('rec_to_cast: ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
