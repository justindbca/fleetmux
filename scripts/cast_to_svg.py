#!/usr/bin/env python3
"""Render an asciicast v2 recording as one animated SVG image that loops (the README recording).

Usage: cast_to_svg.py [--hold S] [--width PX] IN.cast OUT.svg

The cast is replayed into a terminal screen with pyte (requirements-dev.txt). A frame is kept each time the screen
changes; events less than 50 ms apart count as one change. The frames are stacked in one group, and one CSS
animation (``steps(1, end)``, ``infinite``) moves the group by one frame height at each frame's time, so the
image shows the frames in order and starts again after the last one has been shown for ``--hold`` seconds.

The output is a plain image: no script, no link, no external font or other resource (GitHub and PyPI load it as an
<img>). Text uses the viewer's monospace font. ``--width`` (default 960) caps the display width; the drawing keeps
its proportions. Run it on a cast that already went through scripts/redact_recording.py and scripts/rec_to_cast.py:
this tool copies the text as it is.

Exit codes: 0 written; 1 unreadable or malformed input or I/O error, nothing written; 2 usage.
"""
import argparse
import json
import os
import sys
import tempfile
from xml.sax.saxutils import escape

import pyte

FONT = 'ui-monospace, SFMono-Regular, Menlo, Consolas, "DejaVu Sans Mono", "Liberation Mono", monospace'
FONT_PX = 14
CELL_W = 8.4    # 0.6 em: the advance of the common monospace fonts at 14 px
CELL_H = 17.0
PAD = 8.0
MERGE_S = 0.05  # events closer than this make one frame
BG = '#1e1e1e'
FG = '#d4d4d4'
NAMED = {'black': '#000000', 'red': '#cd3131', 'green': '#0dbc79', 'brown': '#e5e510', 'yellow': '#e5e510',
         'blue': '#2472c8', 'magenta': '#bc3fbc', 'cyan': '#11a8cd', 'white': '#e5e5e5',
         'brightblack': '#666666', 'brightred': '#f14c4c', 'brightgreen': '#23d18b', 'brightbrown': '#f5f543',
         'brightyellow': '#f5f543', 'brightblue': '#3b8eea', 'brightmagenta': '#d670d6', 'brightcyan': '#29b8db',
         'brightwhite': '#e5e5e5'}


def load_cast(text):
    """(header, [(seconds, text)]) of an asciicast v2 file; only output ("o") events. ValueError when malformed."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError('empty cast')
    header = json.loads(lines[0])
    if not isinstance(header, dict) or header.get('version') != 2:
        raise ValueError('not an asciicast v2 header')
    for key in ('width', 'height'):
        if not isinstance(header.get(key), int) or header[key] <= 0:
            raise ValueError('header ' + key + ' must be a positive integer')
    events = []
    for n, line in enumerate(lines[1:], 2):
        ev = json.loads(line)
        if not (isinstance(ev, list) and len(ev) == 3 and isinstance(ev[0], (int, float)) and isinstance(ev[2], str)):
            raise ValueError('malformed event on line %d' % n)
        if ev[1] == 'o':
            events.append((float(ev[0]), ev[2]))
    return header, events


def _colour(value, default):
    if not value or value == 'default':
        return default
    if value in NAMED:
        return NAMED[value]
    if len(value) == 6 and all(c in '0123456789abcdefABCDEF' for c in value):
        return '#' + value.lower()
    return default


def _rows(screen):
    """The screen as rows of runs ``(column, text, (fg, bg, bold))``; trailing blanks dropped. A run ends where the
    style changes and after a wide character, so every run starts at its own column."""
    rows = []
    for y in range(screen.lines):
        line = screen.buffer[y]
        runs, col, start, text, style = [], 0, 0, '', None
        last = max([x for x in range(screen.columns) if line[x].data not in ('', ' ') or line[x].bg != 'default']
                   or [-1])
        for x in range(last + 1):
            ch = line[x]
            if ch.data == '':  # the right half of a wide character
                if text:
                    runs.append((start, text, style))
                text, style = '', None
                continue
            fg, bg = _colour(ch.fg, FG), _colour(ch.bg, BG)
            if ch.reverse:
                fg, bg = bg, fg
            cell = (fg, bg, bool(ch.bold))
            if cell != style:
                if text:
                    runs.append((start, text, style))
                start, text, style = x, '', cell
            text += ch.data
        if text:
            runs.append((start, text, style))
        rows.append(runs)
    return rows


def frames(header, events, merge_s=MERGE_S):
    """[(seconds, rows)]: the screen after each change (see ``_rows``), first frame at the first event."""
    screen = pyte.Screen(header['width'], header['height'])
    stream = pyte.Stream(screen)
    out = []
    for at, data in events:
        stream.feed(data)
        rows = _rows(screen)
        if out and rows == out[-1][1]:
            continue
        if out and at - out[-1][0] < merge_s:
            out[-1] = (out[-1][0], rows)
            if len(out) > 1 and out[-1][1] == out[-2][1]:
                out.pop()
            continue
        out.append((at, rows))
    return out


def _frame(rows, index, height):
    parts = ['<g class="frame" transform="translate(0 %s)">' % _num(index * height)]
    for y, runs in enumerate(rows):
        base = PAD + y * CELL_H
        for col, text, (fg, bg, bold) in runs:
            if bg != BG:
                parts.append('<rect x="%s" y="%s" width="%s" height="%s" fill="%s"/>' % (
                    _num(PAD + col * CELL_W), _num(base), _num(len(text) * CELL_W), _num(CELL_H), bg))
        spans = []
        for col, text, (fg, bg, bold) in runs:
            if not text.strip() and bg == BG:
                continue
            attrs = ' x="%s"' % _num(PAD + col * CELL_W)
            if fg != FG:
                attrs += ' fill="%s"' % fg
            if bold:
                attrs += ' font-weight="bold"'
            spans.append('<tspan%s>%s</tspan>' % (attrs, escape(text)))
        if spans:
            parts.append('<text y="%s">%s</text>' % (_num(base + CELL_H * 0.78), ''.join(spans)))
    parts.append('</g>')
    return ''.join(parts)


def _num(value):
    return ('%.2f' % value).rstrip('0').rstrip('.')


def render_svg(frame_list, cols, rows, hold=3.0, width=960):
    """The SVG text of ``frame_list`` (from ``frames``) on a ``cols`` x ``rows`` screen."""
    if not frame_list:
        raise ValueError('no frames')
    w, h = PAD * 2 + cols * CELL_W, PAD * 2 + rows * CELL_H
    total = frame_list[-1][0] + float(hold)
    shown_w = min(float(width), w)
    keys = ['%s%% { transform: translateY(%spx); }' % (_num(min(100.0, t / total * 100)), _num(-i * h))
            for i, (t, _) in enumerate(frame_list)]
    if frame_list[0][0] > 0:
        keys.insert(0, '0% { transform: translateY(0px); }')
    keys.append('100%% { transform: translateY(%spx); }' % _num(-(len(frame_list) - 1) * h))
    style = ('text { font-family: %s; font-size: %dpx; fill: %s; white-space: pre; }\n'
             '.play { animation: play %ss steps(1, end) infinite; }\n'
             '@keyframes play {\n%s\n}' % (FONT, FONT_PX, FG, _num(total),
                                           '\n'.join(keys)))
    body = ''.join(_frame(r, i, h) for i, (_, r) in enumerate(frame_list))
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="%s" height="%s" viewBox="0 0 %s %s" role="img">'
            '<title>fleetmux demo</title><style>%s</style><rect width="100%%" height="100%%" fill="%s"/>'
            '<g class="play">%s</g></svg>\n') % (_num(shown_w), _num(h * shown_w / w), _num(w), _num(h),
                                                 escape(style), BG, body)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split('\n', 1)[0])
    p.add_argument('--hold', type=float, default=3.0, help='seconds the last frame stays before the loop restarts')
    p.add_argument('--width', type=float, default=960, help='maximum display width in px')
    p.add_argument('src')
    p.add_argument('dst')
    a = p.parse_args(argv)
    try:
        with open(a.src, encoding='utf-8') as f:
            header, events = load_cast(f.read())
        frame_list = frames(header, events)
        svg = render_svg(frame_list, header['width'], header['height'], hold=a.hold, width=a.width)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(a.dst)), prefix='.cast_to_svg.')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(svg)
        os.chmod(tmp, 0o644)
        os.replace(tmp, a.dst)
    except (OSError, ValueError) as exc:
        print('cast_to_svg: ' + str(exc), file=sys.stderr)
        return 1
    print('cast_to_svg: %d frames, %.1f s loop, %d bytes -> %s' % (
        len(frame_list), frame_list[-1][0] + a.hold, len(svg.encode()), a.dst))
    return 0


if __name__ == '__main__':
    sys.exit(main())
