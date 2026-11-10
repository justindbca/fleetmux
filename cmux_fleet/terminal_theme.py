"""Terminal theme discovery via OSC 11 / OSC 10 colour queries.

The fleet daemon asks the terminal it lives in (the control tab) once, at
start-up, what its background and foreground colours are: it writes
``ESC ] 11 ; ? ESC \\`` and ``ESC ] 10 ; ? ESC \\`` to the tty, switches the
tty to raw mode, and reads the answers back with ``select`` under one short
total timeout (0.3 s by default).  The result is stored in
``<run>/terminal_theme.json``::

    {"bg": "#rrggbb", "fg": "#rrggbb"|null, "source": "osc", "at": <epoch>}

``bg`` is always present, ``fg`` may be ``null``, colours are lower-case
``#rrggbb``.  ``cmux_fleet.history.write_page`` reads that file and turns it
into the ``:root{...}`` CSS block that makes the history page follow the
terminal colours.

If the terminal does not answer (not a tty, no ``/dev/tty``, timeout, any
OSError / termios error) nothing is written at all and the page keeps its
built-in light/dark scheme; an already existing file is left untouched.

The probe is bounded on every axis: one deadline covers the raw-mode switch and
the write too, the write loop is non-blocking (and refuses to write at all when
it cannot be made non-blocking), every read goes through ``select`` with the
remaining time, and the tty is put in raw mode and back with ``TCSANOW`` plus
``tcflush(TCIOFLUSH)``.  Never ``TCSAFLUSH`` / ``TCSADRAIN``: both wait for the
pending *output* queue to drain, which never happens when the terminal does not
read our query bytes -- that was the real 20-minute test hang (``tty.setraw``
defaults to ``TCSAFLUSH``, so the switch has to say ``TCSANOW`` explicitly).  So
the probe never raises and never hangs.

Only the standard library is used.
"""

import fcntl
import json
import os
import re
import select
import termios
import time
import tty
from pathlib import Path

FILE = 'terminal_theme.json'

# Base palettes; the light/dark entries are the ones history.PAGE already uses
# for ``:root`` / ``@media (prefers-color-scheme:dark)``.
SCHEMES = {
    'light': {
        'bg': '#fbfbfa',
        'fg': '#1d1d1b',
        'muted': '#6b6b66',
        'line': '#e2e1dc',
        'panel': '#f2f1ed',
        'accent': '#2f5d8a',
        'mark': '#ffe08a',
        'cur': '#ff9f43',
    },
    'dark': {
        'bg': '#171716',
        'fg': '#e8e6e1',
        'muted': '#9a978f',
        'line': '#2e2d2a',
        'panel': '#201f1d',
        'accent': '#7fb0e0',
        'mark': '#6b5a1a',
        'cur': '#b8641c',
    },
}

# ESC ] 11 ; ? ESC \   ESC ] 10 ; ? ESC \
QUERY = b'\x1b]11;?\x1b\\\x1b]10;?\x1b\\'

_HEX6 = re.compile(r'^#[0-9a-f]{6}$')

# ESC ] <code> ; rgb:R/G/B <ST or BEL>; every component is either 16-bit
# (RRRR, of which the high 8 bits are kept) or 8-bit (RR).
_OSC_RGB = (rb'\x1b\]%d;rgb:(?:([0-9a-fA-F]{4})|([0-9a-fA-F]{2}))'
            rb'/(?:([0-9a-fA-F]{4})|([0-9a-fA-F]{2}))'
            rb'/(?:([0-9a-fA-F]{4})|([0-9a-fA-F]{2}))(?:\x1b\\|\x07)')


def parse_osc_color(reply: bytes, code: int) -> str | None:
    """Extract ``#rrggbb`` for OSC ``code`` (10 = fg, 11 = bg) from a reply.

    Accepts both terminators (ST ``ESC \\`` and BEL) and both component widths
    (8-bit ``RR`` and 16-bit ``RRRR``).  Returns ``None`` for anything else;
    never raises.
    """
    if not reply or not isinstance(reply, (bytes, bytearray)):
        return None
    try:
        pattern = _OSC_RGB % int(code)
    except (TypeError, ValueError):
        return None
    m = re.search(pattern, bytes(reply))
    if not m:
        return None
    parts = []
    for i in (0, 2, 4):
        wide, narrow = m.group(i + 1), m.group(i + 2)
        value = wide if wide is not None else narrow
        if value is None:
            return None
        parts.append(value[:2].decode('ascii').lower())
    return '#' + ''.join(parts)


def _write_all(fd: int, data: bytes, deadline: float) -> bool:
    """Write ``data`` to ``fd`` without ever blocking past ``deadline``.

    Returns ``False`` without writing anything when the descriptor cannot be
    put in non-blocking mode: a blocking ``os.write`` on a pty whose reader is
    gone could sit in the kernel indefinitely, and an unbounded write is worse
    than no colour probe at all.
    """
    try:
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    except (OSError, ValueError):
        return False
    try:
        view = memoryview(data)
        while view:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            try:
                written = os.write(fd, view)
            except BlockingIOError:
                try:
                    select.select([], [fd], [], remaining)
                except (OSError, ValueError):
                    return False
                continue
            except InterruptedError:
                continue
            except OSError:
                return False
            if written <= 0:
                return False
            view = view[written:]
        return True
    finally:
        if flags is not None:
            try:
                fcntl.fcntl(fd, fcntl.F_SETFL, flags)
            except OSError:
                pass


def query(fd: int, timeout: float = 0.3) -> dict | None:
    """Ask tty ``fd`` for its colours; ``None`` if unavailable. Never raises.

    One deadline covers the raw-mode switch, the query write and the reads.
    It returns as soon as *both* answers are in, or at the deadline with
    ``{'bg': ..., 'fg': None}`` when only OSC 11 came back (``fg`` is optional,
    ``bg`` is not), or ``None`` when there is no usable background answer.
    """
    saved = None
    try:
        # One deadline for the whole probe: raw-mode switch, write and reads.
        deadline = time.monotonic() + max(0.0, float(timeout))
        if not os.isatty(fd):
            return None
        saved = termios.tcgetattr(fd)
        # TCSANOW, never the tty.setraw default TCSAFLUSH: TCSAFLUSH waits for
        # the pending output queue to drain, i.e. forever when the terminal
        # never reads our query bytes (real 20-minute hang, see module docstring).
        tty.setraw(fd, termios.TCSANOW)
        if not _write_all(fd, QUERY, deadline):
            return None

        buf = b''
        bg = None
        fg = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                ready, _, _ = select.select([fd], [], [], remaining)
            except (OSError, ValueError):
                break
            if not ready:
                break
            try:
                chunk = os.read(fd, 4096)
            except (BlockingIOError, InterruptedError):
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            if bg is None:
                bg = parse_osc_color(buf, 11)
            if fg is None:
                fg = parse_osc_color(buf, 10)
            if bg is not None and fg is not None:
                break
        if bg is None:
            return None
        return {'bg': bg, 'fg': fg}
    except (OSError, termios.error, ValueError, TypeError):
        return None
    finally:
        if saved is not None:
            # Drop whatever is still queued in both directions (the query bytes
            # included: nobody is going to read them) and restore with TCSANOW.
            # TCSADRAIN would wait for the unread query bytes to drain -- i.e.
            # forever on a terminal that never answers.
            try:
                termios.tcflush(fd, termios.TCIOFLUSH)
            except (OSError, termios.error):
                pass
            try:
                termios.tcsetattr(fd, termios.TCSANOW, saved)
            except (OSError, termios.error):
                pass


def probe(timeout: float = 0.3) -> dict | None:
    """Open ``/dev/tty`` and query it; ``None`` when there is no terminal."""
    fd = None
    try:
        fd = os.open('/dev/tty', os.O_RDWR | os.O_NOCTTY)
    except (OSError, ValueError):
        return None
    try:
        return query(fd, timeout)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def capture(run_dir, timeout: float = 0.3) -> dict | None:
    """Probe the terminal and atomically store the result in ``<run>/``.

    Returns the written dict, or ``None`` when the terminal did not answer (in
    which case an existing ``terminal_theme.json`` is kept as it is).
    """
    theme = probe(timeout)
    if theme is None or not theme.get('bg'):
        return None
    data = {
        'bg': theme['bg'],
        'fg': theme.get('fg'),
        'source': 'osc',
        'at': time.time(),
    }
    path = Path(run_dir) / FILE
    tmp = path.with_name(path.name + '.tmp')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, 'w') as fh:
            fh.write(json.dumps(data))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return None
    return data


def read(run_dir) -> dict | None:
    """Read ``<run>/terminal_theme.json``; ``None`` when absent or invalid.

    Strict on purpose (the file is written by another process and consumed as
    CSS): ``bg`` must match ``^#[0-9a-f]{6}$`` exactly -- no strip, no case
    folding, ``fullmatch`` so a trailing newline or any whitespace is rejected.
    ``fg`` may be absent or ``null``; any other ``fg`` that is not a strictly
    matching lower-case colour invalidates the whole file.  Unreadable,
    undecodable (bad UTF-8), non-JSON or non-object content is ``None`` too.
    """
    try:
        raw = (Path(run_dir) / FILE).read_text()
    except (OSError, ValueError):
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    bg = data.get('bg')
    if not isinstance(bg, str):
        return None
    if not _HEX6.fullmatch(bg):
        return None
    fg = data.get('fg')
    if fg is None:
        return {'bg': bg, 'fg': None}
    if not isinstance(fg, str) or not _HEX6.fullmatch(fg):
        return None
    return {'bg': bg, 'fg': fg}


def _rgb(colour: str) -> tuple[int, int, int]:
    return int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16)


def _hex(rgb) -> str:
    return '#%02x%02x%02x' % tuple(max(0, min(255, int(round(c)))) for c in rgb)


def _mix(bg: str, fg: str, k: float) -> str:
    b = _rgb(bg)
    f = _rgb(fg)
    return _hex(b[i] + (f[i] - b[i]) * k for i in range(3))


def css(theme) -> str:
    """Turn a theme dict into the single-line ``:root{...}`` CSS override.

    ``--bg`` is the terminal background, ``--fg`` the terminal foreground (or
    the scheme default), ``--panel`` / ``--line`` / ``--muted`` are the
    background mixed 6 % / 12 % / 55 % towards the foreground, and
    ``--accent`` / ``--mark`` / ``--cur`` come from the light or dark scheme
    picked by the background's relative luminance.  ``color-scheme`` is always
    written so native widgets (input, button) follow too.  The input is
    validated with the same strict rule as :func:`read` (exact lower-case
    ``#rrggbb``, ``fullmatch``, ``fg`` absent or ``null``); anything else
    returns ``''`` so callers can splice the result in unconditionally.
    """
    if not isinstance(theme, dict):
        return ''
    bg = theme.get('bg')
    if not isinstance(bg, str) or not _HEX6.fullmatch(bg):
        return ''
    r, g, b = _rgb(bg)
    dark = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0 < 0.5
    scheme = SCHEMES['dark' if dark else 'light']
    fg = theme.get('fg')
    if fg is None:
        fg = scheme['fg']
    elif not isinstance(fg, str) or not _HEX6.fullmatch(fg):
        return ''
    return (
        ':root{color-scheme:%s;--bg:%s;--fg:%s;--muted:%s;--line:%s;--panel:%s;'
        '--accent:%s;--mark:%s;--cur:%s}'
        % (
            'dark' if dark else 'light',
            bg,
            fg,
            _mix(bg, fg, 0.55),
            _mix(bg, fg, 0.12),
            _mix(bg, fg, 0.06),
            scheme['accent'],
            scheme['mark'],
            scheme['cur'],
        )
    )
