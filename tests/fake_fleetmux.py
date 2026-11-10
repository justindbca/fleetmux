#!/usr/bin/env python3.14
"""A fake ``fleetmux`` binary: the control protocol without waiting for Rust (plan.md §3.5).

Normative source: ``fleetmux/PROTOCOL.md`` (proto-1's draft, supervisor rulings R1–R12 + the three extra ones).
Same command line as the real binary:

* ``fleetmux --version`` prints ``fleetmux 0.2.0 (protocol 1)`` (R9).
* ``fleetmux server --runtime D --session S [--daemonize] [--default-size COLSxROWS] [--record DIR] [--scrollback N]
  [--prefix KEY] [--theme FILE]`` owns ``D``:
  ``ctl.sock`` (0600, dir 0700), ``server.json``, ``panes.json``, ``layout.json``, ``server.log``. Every check
  (owner / mode, socket length, "a server already answers") and the bind happen *before* the double fork, so
  exit 0 from ``--daemonize`` means the socket already answers. ``start.lock`` is the client's; never touched.
* ``fleetmux attach --runtime D [--no-mouse]`` does the view handshake, prints the server's reply line, detaches;
  exit 3 + the last 20 lines of ``server.log`` on stderr when it cannot connect (PROTOCOL.md §9.2).
* ``fleetmux reap --runtime D`` (only when no server answers) signals each process group in ``panes.json``
  whose pid's OS start time still equals the recorded one; one JSON line ``{"reaped": [...], "skipped": [...]}``
  and the §9.3 exit codes.

Exit codes follow PROTOCOL.md §9: 2 usage (unknown flag, missing / malformed value), server 3 = a server already
answers (or is starting), 4 = socket path too long, 5 = runtime dir owner / mode wrong; 3–5 also write one ``ERROR``
line to ``server.log``. ``FAKE_FM_START_FAIL`` (below) and anything else exit 1.

Control connection: one JSON object per line, ``{"id": n, "cmd": ..., ...}`` -> ``{"id": n, "ok": true, ...}`` /
``{"id": n, "ok": false, "code": ..., "error": ...}``; the top-level ``id`` is only ever the request number and a
pane is always ``pane`` (R1). Panes are real processes on real PTYs (``pty.openpty`` + ``Popen(start_new_session=
True)``, the PTY made their controlling tty). ``pane.kill`` signals the group at 0 / 2000 / 5000 ms, only the
steps at or before ``timeout_ms``, then waits at most 500 ms more without signalling (R4). Placement / move /
selection follow design §3.2 / §3.4, rectangles design §2.5. Start times are integer Unix seconds read from the
OS (sysctl on macOS, /proc on Linux) and compared exactly (R6).

Fake-only behaviour, not part of the protocol: ``pane.read`` text comes from a line model (escapes dropped,
``\\r`` / ``\\b`` / tab / auto-wrap at the pane width, blank rows padded to the pane height), not a full emulator —
cursor addressing is ignored. Test knobs (env, read when the server starts): ``FAKE_FM_VERSION`` version string;
``FAKE_FM_PROTOCOL`` protocol number; ``FAKE_FM_FAIL`` comma list of commands that answer ``internal``;
``FAKE_FM_START_FAIL`` = message: written to server.log, then exit 1 instead of starting; ``FAKE_FM_EXPECT_UID``
the uid peers must have (default: ours); ``FAKE_FM_KEYS`` path of keys.json (default
``../fleetmux/protocol/keys.json`` from this file); ``FAKE_FM_SCROLLBACK`` scrollback rows (10000);
``FAKE_FM_LOG`` path every request line is appended to. ``FAKE_FM_*`` is removed from every pane's environment.
``--theme FILE`` (§9.1): a JSON object with the optional keys ``bg`` / ``fg`` / ``cursor`` (``#rrggbb``, either
case, stored lowercase) and ``ansi`` (exactly 16 colours); it is merged over ``CMUX_THEME`` (the Python copy of
the cmux default palette), and ``hello`` reports the result as ``theme`` (§4.1). A bad file is a usage error,
exit 2, as in the Rust server.

Stage-A additions (brief-m03 §3.2 P-1…P-3): ``server.stats`` (``frames`` counts the ``0x81`` frames this fake
sends -- one first screen per viewer; ``wakeups`` is always 0; ``rss_bytes`` is the peak RSS), the optional pane
keys ``unseen`` (output arrived while the pane was not shown: not its group's selected tab, or another pane is
zoomed; cleared when it is shown) and ``scrollback`` (rows above the screen), and ``hello``'s ``viewers`` /
``prefix`` (``--prefix``, default ``C-b``), ``--record DIR`` (§7 entries, one ``<pane>.rec`` per pane, 0600,
split over 1 MiB), ``--scrollback N`` (over ``FAKE_FM_SCROLLBACK``). View input (§10): the prefix byte is intercepted
and the next key within 1 s acts -- ``1``–``9``, ``Tab`` / ``BTab``, arrows, ``z``, ``<`` ``>`` ``+`` ``-`` ``=``,
``o`` (an ``0x83`` frame to that viewer, payload = kv ``history_url`` without ``file://``), the prefix again
(written to the pane); ``[`` / ``d`` / anything else is swallowed (no page mode, no mouse handling: SGR mouse
reports are dropped, never forwarded).

M2.5 additions (brief-m25 §3.5 W4; PROTOCOL.md §4.18-§4.22, §6.3): view connections also take ``0x04`` frames. A
viewer has a web window from its ``{"event":"window","state":"open"}`` until ``closed`` or the end of its
connection; the primary window is the one that reported ``open`` last. ``web.open`` / ``web.goto`` / ``web.url`` /
``web.reload`` relay one ``0x84 {"seq","op","url"?}`` per viewer (``open``: every viewer; ``url``: the primary
window; ``goto`` / ``reload``: every window) and wait at most 3 s (``WEB_TIMEOUT_S``) for the ``0x04`` replies with
the same ``seq``; a window that does not answer -> ``web_timeout``; no window -> ``no_window`` (``goto`` / ``url``)
or ``windows: 0`` (``reload``). A viewer's ``{"op":"page","path","have"}`` gets ``0x84 {"seq","op":"page","page":
{"path","size","mtime","b64"?}}`` (``mtime`` in ns; ``b64`` only when ``have`` differs). ``web.eval`` always answers
``not_implemented``. Frame writes to one viewer are serialised (relay threads and the viewer's own thread).

M4 pages (design-1/m4-design.md v0.7 §4.2; PROTOCOL.md §4 once B1 lands): one server holds the pages of a fleet tree.
Page ``""`` is the root page; a control connection is bound to a page by its first ``hello{page}`` (a second
``hello`` with another page -> ``bad_request``), an unbound connection is a root-page connection, and every viewer
shows the one *current* page (``page.select``). ``hello`` answers ``pages: 1``, ``page`` (the connection's) and
``current``; ``run`` is that page's kv. A bound page that does not exist yet is created ``live`` (title = its id) by
the connection's first ``pane.spawn`` / ``pane.move``; until then ``pane.list`` is ``[]``, ``layout.get`` empty
groups, ``kv.get`` null. ``page.open`` / ``page.close`` / ``page.select`` / ``page.list`` (``{"pages": [{page,
title, state, panes, current}]}``, root first); ``pane.spawn{page}`` (unknown -> ``bad_request`` "no such page");
``pane.list{all}``; every pane record carries ``page``; ``pane.move`` pulls a pane of any page into the
connection's page (same group too), anchors must be in that page; ``pane.focus`` never changes the current page;
kv per page, ``sidebar`` / ``stop_request`` / ``restorable`` / ``sidebar_width`` root page only; ``layout``
groups / selection / zoom per page, ratios shared; ``server.shutdown`` from a page connection -> ``bad_request``;
``web.url`` only for the current page (a bound page also needs the window path to equal its ``history_url``
path), ``web.goto`` / ``web.reload`` of a background page relay nothing (``windows: 0``); ``page.select`` sends
``0x84 goto <history_url>`` to the web windows; a viewer's ``0x04 {"event":"jump","url"}`` selects the page whose
``history_url`` path equals the URL's ``to`` path. ``server.json``, ``server.stats`` and the view ``ok`` line carry
``pages``. ``panes.json`` rows carry ``page``; ``layout.json`` holds the root page only. At most 64 pages besides the
root. ``FAKE_FM_NO_PAGES=1`` plays an M3 server: ``page`` arguments are ignored, no ``page.*`` command, no
``pages`` / ``page`` keys anywhere.
"""
import base64
import codecs
import ctypes
import datetime
import fcntl
import json
import os
import pty
import random
import re
import resource
import select
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

VERSION = os.environ.get('FAKE_FM_VERSION') or '0.2.0'
PROTOCOL = int(os.environ.get('FAKE_FM_PROTOCOL') or 1)
VIEW_VERSION = 1
MAX_LINE = 4 * 1024 * 1024
MAX_FRAME = 1024 * 1024
WEB_FRAME_MAX = 32 * 1024 * 1024                 # §6.2: 0x84 alone may carry a page of up to 32 MiB
WEB_TIMEOUT_S = 3.0                              # §4.18: the wait for the replies of one relayed web.* command
MAX_QUEUE = 16 * 1024 * 1024
MAX_ID = 2 ** 53 - 1
PAGE_ID = re.compile(r'[A-Za-z0-9._-]{1,64}\Z')   # design §4.1
MAX_PAGES = 64                                   # pages besides the root (R14)
ROOT_ONLY_KV = ('sidebar', 'stop_request', 'restorable', 'sidebar_width')
NO_PAGES = os.environ.get('FAKE_FM_NO_PAGES') == '1'
SUN_MAX = 103 if sys.platform == 'darwin' else 107
GROUPS = ('supervisor', 'lower', 'workers')
MODES = ('raw', 'bracketed', 'paste')
KILL_SCHEDULE = ((0, signal.SIGHUP), (2000, signal.SIGTERM), (5000, signal.SIGKILL))
KILL_REAP_GRACE_MS = 500
CURSOR_KEYS = {b'\x1b[A', b'\x1b[B', b'\x1b[C', b'\x1b[D', b'\x1b[H', b'\x1b[F'}
PASTE_START, PASTE_END = b'\x1b[200~', b'\x1b[201~'
RECORD_MAX = 1024 * 1024                         # §7: one entry is 1 byte .. 1 MiB
PREFIX_WAIT = 1.0                                # §10: the second key must come within 1 s
SGR_MOUSE = re.compile(rb'\x1b\[<[0-9]+;[0-9]+;[0-9]+[Mm]')
EXIT_USAGE, EXIT_ANSWERS, EXIT_TOO_LONG, EXIT_PERMS = 2, 3, 4, 5
#: PROTOCOL.md §9.1: the cmux 0.65.0 palette (OSC 10/11/12/4 measured on this machine), the ``--theme`` default.
#: The Python copy of ``Palette::CMUX`` (crates/term/src/alacritty.rs) -- keep the 19 values identical.
CMUX_THEME = {
    'bg': '#1e1e1e', 'fg': '#ffffff', 'cursor': '#98989d',
    'ansi': ['#1a1a1a', '#cc372e', '#26a439', '#cdac08', '#0869cb', '#9647bf', '#479ec2', '#98989d',
             '#464646', '#ff453a', '#32d74b', '#ffd60a', '#0a84ff', '#bf5af2', '#76d6ff', '#ffffff'],
}
THEME_COLOUR = re.compile(r'#[0-9a-fA-F]{6}\Z')
THEME_KEYS = ('bg', 'fg', 'cursor', 'ansi')


class Fail(Exception):
    def __init__(self, code, error):
        super().__init__(error)
        self.code, self.error = code, error


def die(msg, code=1):
    print(f'fleetmux: {msg}', file=sys.stderr)
    sys.exit(code)


def load_theme(path, usage):
    """``--theme FILE`` (§9.1): a JSON object whose four keys are optional; colours are ``#rrggbb`` in either
    case and are stored lowercase; ``ansi`` is exactly 16 colours. Unknown key, bad value, unreadable file or a
    non-object -> usage error, exit 2 (as the Rust server, which never swallows it)."""
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        die(f'--theme {path}: {exc}\nusage: {usage}', EXIT_USAGE)
    if not isinstance(data, dict):
        die(f'--theme {path}: not a JSON object\nusage: {usage}', EXIT_USAGE)
    theme = {k: CMUX_THEME[k] for k in ('bg', 'fg', 'cursor')}
    theme['ansi'] = list(CMUX_THEME['ansi'])
    for key, value in data.items():
        if key not in THEME_KEYS:
            die(f'--theme {path}: unknown key {key!r}\nusage: {usage}', EXIT_USAGE)
        if key == 'ansi':
            if not isinstance(value, list) or len(value) != 16:
                die(f'--theme {path}: ansi wants exactly 16 colours\nusage: {usage}', EXIT_USAGE)
            if not all(isinstance(v, str) and THEME_COLOUR.match(v) for v in value):
                die(f'--theme {path}: ansi wants #rrggbb colours\nusage: {usage}', EXIT_USAGE)
            theme['ansi'] = [v.lower() for v in value]
        else:
            if not isinstance(value, str) or not THEME_COLOUR.match(value):
                die(f'--theme {path}: {key} wants a #rrggbb colour\nusage: {usage}', EXIT_USAGE)
            theme[key] = value.lower()
    return theme


def log_line(path, level, msg):
    stamp = datetime.datetime.now(datetime.UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')
    with open(path, 'a', encoding='utf-8') as fh:
        fh.write(f'{stamp} {level} {msg}\n')


def atomic_write(path, obj):
    tmp = path.with_name(path.name + '.tmp')
    with open(tmp, 'w', encoding='utf-8') as fh:
        fh.write(json.dumps(obj, indent=1) + '\n')
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def unlink(path):
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- OS facts: start time (§8.6), peer uid (§3)

def start_time(pid):
    """The OS-recorded start of ``pid`` as integer Unix seconds, or None when it cannot be read (= unverified)."""
    try:
        if sys.platform == 'darwin':
            libc = ctypes.CDLL(None, use_errno=True)
            mib = (ctypes.c_int * 4)(1, 14, 1, pid)          # CTL_KERN, KERN_PROC, KERN_PROC_PID
            buf, size = ctypes.create_string_buffer(1024), ctypes.c_size_t(1024)
            if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, ctypes.c_size_t(0)) or size.value < 8:
                return None
            return struct.unpack_from('q', buf.raw, 0)[0]   # kinfo_proc.kp_proc.p_starttime.tv_sec
        ticks = int(Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19])
        btime = next(int(line.split()[1]) for line in Path('/proc/stat').read_text().splitlines()
                     if line.startswith('btime '))
        return btime + ticks // os.sysconf('SC_CLK_TCK')
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def peer_allowed(server_uid, peer_uid):
    return server_uid == peer_uid


def peer_uid(conn):
    if sys.platform == 'darwin':
        raw = conn.getsockopt(0, 1, 76)          # SOL_LOCAL, LOCAL_PEERCRED -> struct xucred
        return struct.unpack_from('I', raw, 4)[0]
    raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
    return struct.unpack('3i', raw)[1]


def answers(sock_path):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect(str(sock_path))
        return True
    except OSError:
        return False
    finally:
        s.close()


def load_keys():
    path = os.environ.get('FAKE_FM_KEYS') or Path(__file__).resolve().parent.parent / 'fleetmux/protocol/keys.json'
    return {k: v.encode() for k, v in json.loads(Path(path).read_text())['keys'].items()}


# ---------------------------------------------------------------- screen text (what pane.read returns, §4.9)

ESC_SEQ = re.compile(rb'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[PX^_][^\x1b]*\x1b\\'
                     rb'|\x1b[ -/]*[0-OQ-WYZ\\`a-~]')
UNFINISHED = re.compile(rb'\x1b(?:\[[0-?]*[ -/]*|\][^\x07]*|[PX^_][\s\S]*|[ -/]*)\Z')
MODE_SEQ = re.compile(rb'\x1b\[\?([0-9;]+)([hl])')


class Screen:
    """Physical rows of a pane's output: escapes dropped, ``\\r`` / ``\\b`` / tab / auto-wrap honoured."""

    def __init__(self, limit, cols, rows):
        self.limit, self.cols, self.rows = limit, cols, rows
        self.lines, self.col, self.pending = [[]], 0, b''
        self.utf8 = codecs.getincrementaldecoder('utf-8')('replace')
        self.modes = {'bracketed_paste': False, 'app_cursor': False}

    def feed(self, data):
        """Returns the bytes the terminal answers (CSI 6n), the way a real parser's replies come back."""
        data, self.pending = self.pending + data, b''
        text, reply, i = [], b'', 0
        while True:
            j = data.find(b'\x1b', i)
            text.append(data[i:] if j < 0 else data[i:j])
            if j < 0:
                break
            m = ESC_SEQ.match(data, j)
            if m:
                seq, i = m.group(), m.end()
                mode = MODE_SEQ.fullmatch(seq)
                for n in mode[1].split(b';') if mode else ():
                    if n in (b'2004', b'1'):
                        self.modes['bracketed_paste' if n == b'2004' else 'app_cursor'] = mode[2] == b'h'
                if seq == b'\x1b[6n':
                    reply += b'\x1b[%d;%dR' % (min(len(self.lines), self.rows), min(self.col, self.cols - 1) + 1)
            elif len(data) - j < 4096 and UNFINISHED.match(data, j):
                self.pending = data[j:]                        # an escape split across reads
                break
            else:
                i = j + 1                                      # stray ESC: drop it
        for ch in self.utf8.decode(b''.join(text)):
            if ch == '\n':
                self.lines.append([])
                self.col = 0
            elif ch == '\r':
                self.col = 0
            elif ch == '\b':
                self.col = max(0, min(self.col, self.cols) - 1)
            elif ch == '\t':
                self.col = min(self.cols - 1, (self.col // 8 + 1) * 8)
            elif ch >= ' ' and ch != '\x7f':
                if self.col >= self.cols:                      # soft wrap: one more physical row
                    self.lines.append([])
                    self.col = 0
                line = self.lines[-1]
                line.extend(' ' * (self.col - len(line)))
                if self.col < len(line):
                    line[self.col] = ch
                else:
                    line.append(ch)
                self.col += 1
        del self.lines[:-(self.limit + self.rows)]
        return reply

    def scrollback(self):
        return max(0, len(self.lines) - self.rows)

    def text(self, n):
        rows = [''.join(line).rstrip() for line in self.lines]
        rows += [''] * (self.rows - len(rows))                # blank screen rows below the output stay
        rows = rows[-n:]
        while len(rows) > 1 and sum(len(r.encode()) + 1 for r in rows) > MAX_LINE - 4096:
            rows.pop(0)                                        # keep the reply under the 4 MiB line limit
        return '\n'.join(rows)


# ---------------------------------------------------------------- layout model (design §2.5, §3.2, §3.4)

class Layout:
    def __init__(self, ratios=None):
        self.groups = {g: [] for g in GROUPS}
        self.selected = dict.fromkeys(GROUPS)
        self.ratios = ratios if ratios is not None else {'h': 50, 'v': 60}   # M4: one dict for every page
        self.focus, self.zoom = 'supervisor', None
        self.successor = {}                      # pane -> the pane last put immediately before it

    h_ratio = property(lambda self: self.ratios['h'], lambda self, v: self.ratios.__setitem__('h', v))
    v_ratio = property(lambda self: self.ratios['v'], lambda self, v: self.ratios.__setitem__('v', v))

    def group_of(self, pid):
        return next((g for g in GROUPS if pid in self.groups[g]), None)

    def place_by_type(self, ty, types):
        if ty == 'supervisor':
            return 'supervisor', 0
        if ty == 'advisor':
            return 'supervisor', None
        if ty == 'daemon':
            hist = [n for n, p in enumerate(self.groups['lower']) if types.get(p) == 'history']
            return 'lower', hist[0] + 1 if hist else 0
        if ty == 'tab':
            return 'lower', None
        if ty == 'history':
            return 'lower', 0
        return 'workers', None                   # worker, placeholder, anything else, no type

    def add(self, pid, g, i, before=None):
        lst = self.groups[g]
        lst.insert(len(lst) if i is None else i, pid)
        if self.selected[g] is None:             # only an empty group's new pane gets selected
            self.selected[g] = pid
        if before is not None:
            self.successor[before] = pid

    def remove(self, pid):
        """Take ``pid`` out; if it was the shown tab, the pane put in before it wins, else the left neighbour."""
        g = self.group_of(pid)
        if g is None:
            return None
        lst = self.groups[g]
        i = lst.index(pid)
        lst.remove(pid)
        succ = self.successor.pop(pid, None)
        if self.selected[g] == pid:
            self.selected[g] = succ if succ in lst else lst[max(0, i - 1)] if lst else None
        if self.zoom == pid:
            self.zoom = None
        return self.selected[g]

    def move(self, pid, g, before=None, after=None):
        anchor = before or after
        if anchor is not None and (anchor == pid or anchor not in self.groups[g]):
            raise Fail('bad_request', f'reference pane {anchor} is not another pane in group {g}')
        if self.group_of(pid) == g:
            self.groups[g].remove(pid)
        else:
            self.remove(pid)                     # the old group's selection moves by the §3.2 rule
        lst = self.groups[g]
        # never takes the target group's selection; selected there only if that group was empty (ruling M5-2)
        self.add(pid, g, lst.index(before) if before else lst.index(after) + 1 if after else None, before)

    def select(self, pid):
        g = self.group_of(pid)
        self.selected[g], self.focus = pid, g

    def rects(self, cols, rows):
        """Inner (cols, rows) per group after borders, tab bars and the status line; zoom = the whole content
        area minus the status line (240x60 -> 240x59, plan §3.3)."""
        body = rows - 1
        left = cols * self.h_ratio // 100
        top = round(body * self.v_ratio / 100)
        fit = lambda c, r: (max(1, c), max(1, r))
        return {'supervisor': fit(left - 2, top - 1), 'lower': fit(left - 2, body - top - 2),
                'workers': fit(cols - left - 1, body - 2), 'zoom': fit(cols, body)}

    def obj(self):
        return {'h_ratio': self.h_ratio, 'v_ratio': self.v_ratio, 'zoom': self.zoom, 'focus': self.focus,
                'groups': {g: list(v) for g, v in self.groups.items()}, 'selected': dict(self.selected)}


class Page:
    """One fleet's page (M4): its layout (groups / selection / zoom; ratios shared), kv, title and state."""

    def __init__(self, pid, title, ratios, state='live'):
        self.id, self.title, self.state = pid, title, state
        self.layout = Layout(ratios)
        self.kv = {}


# ---------------------------------------------------------------- panes

class Pane:
    def __init__(self, server, pid_, argv, cwd, env, title, ty, size):
        self.server, self.id, self.title, self.type = server, pid_, title, ty
        self.page = ''
        self.cols, self.rows = size
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', self.rows, self.cols, 0, 0))
        self.tty = os.ttyname(slave)
        try:
            self.proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=slave, stdout=slave, stderr=slave,
                                         start_new_session=True, close_fds=True,
                                         preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0))
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            os.close(master)
            raise Fail('spawn_failed', f'{argv[0]}: {exc}')
        finally:
            os.close(slave)
        self.master, self.pid = master, self.proc.pid
        try:
            self.pgid = os.getpgid(self.pid)
        except ProcessLookupError:
            self.pgid = self.pid                 # start_new_session: the child leads its own group
        self.started = start_time(self.pid)
        self.born = time.monotonic()
        self.rec = None
        if server.record is not None:
            fd = os.open(server.record / f'{pid_}.rec', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            self.rec = os.fdopen(fd, 'ab')
        self.screen = Screen(server.scrollback, self.cols, self.rows)
        self.cv, self.out, self.queued = threading.Condition(), [], 0
        self.closing = self.killing = self.unseen = False
        for fn in (self._reader, self._writer, self._watcher):
            threading.Thread(target=fn, daemon=True).start()

    def resize(self, cols, rows):
        if (cols, rows) != (self.cols, self.rows) and not self.closing:
            self.cols, self.rows = self.screen.cols, self.screen.rows = cols, rows
            try:
                fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
            except OSError:
                pass

    def write(self, data):
        """Queue ``data``; one pane that never reads its input blocks nobody else (§4.7)."""
        if self.closing or self.proc.poll() is not None:
            raise Fail('write_failed', f'pane {self.id}: its PTY is closed')
        with self.cv:
            if self.queued + len(data) > MAX_QUEUE:
                raise Fail('write_failed', f'pane {self.id}: input queue over {MAX_QUEUE} bytes')
            self.out.append(data)
            self.queued += len(data)
            self.cv.notify()

    def _reader(self):
        while not self.closing:
            try:
                ready, _, _ = select.select([self.master], [], [], 0.2)
                if not ready:
                    continue
                data = os.read(self.master, 65536)
            except (OSError, ValueError):
                return
            if not data:
                return
            if self.rec is not None:
                us = int((time.monotonic() - self.born) * 1e6)
                for k in range(0, len(data), RECORD_MAX):
                    chunk = data[k:k + RECORD_MAX]
                    self.rec.write(struct.pack('>QI', us, len(chunk)) + chunk)
                self.rec.flush()
            with self.server.lock:
                reply = self.screen.feed(data)
                self.server.parse_bytes += len(data)
                if not self.server.shown(self.id):
                    self.unseen = True
            if reply:
                with self.cv:
                    self.out.append(reply)
                    self.cv.notify()

    def _writer(self):
        while True:
            with self.cv:
                while not self.out and not self.closing:
                    self.cv.wait()
                if self.closing:
                    return
                data = self.out.pop(0)
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(self.master, view):]
            except OSError as exc:
                self.server.log('WARN', f'write to {self.id} failed: {exc}')
            with self.cv:
                self.queued = max(0, self.queued - len(data))

    def _watcher(self):
        self.proc.wait()
        if not self.killing:                     # natural exit: the tab goes away at once (§4.0)
            time.sleep(0.1)                      # let the reader drain the last output
            self.server.drop(self)

    def close(self):
        with self.cv:
            self.closing = True
            self.cv.notify()
        time.sleep(0.05)
        try:
            os.close(self.master)
        except OSError:
            pass

    def group_gone(self):
        if self.proc.poll() is None:
            return False
        try:
            os.killpg(self.pgid, 0)
            return False
        except ProcessLookupError:
            return True
        except PermissionError:
            return False

    def fg_pgid(self):
        """The PTY's foreground process group (``tcgetpgrp`` on the master), None when it reports none."""
        try:
            g = os.tcgetpgrp(self.master)
        except OSError:
            return None
        return g if g > 0 else None

    def fg_job(self):
        """The foreground group when it is not the pane's own and its leader is in the pane's session (§4.13)."""
        g = self.fg_pgid()
        return g if g is not None and g != self.pgid and in_session(g, self.pid) else None

    def record(self, layout=None):
        """The pane record; group / index / focused by the layout of the pane's own page (F8)."""
        page = self.server.pages.get(self.page)
        layout = page.layout if page is not None else layout
        g = layout.group_of(self.id)
        rec = {'pane': self.id, 'title': self.title, 'type': self.type, 'group': g,
               'index': layout.groups[g].index(self.id) if g else None, 'pid': self.pid, 'pgid': self.pgid,
               'tty': self.tty, 'cols': self.cols, 'rows': self.rows,
               'focused': g == layout.focus and layout.selected[g] == self.id,
               'unseen': self.unseen, 'scrollback': self.screen.scrollback(), 'fg_pgid': self.fg_pgid()}
        if not NO_PAGES:
            rec['page'] = self.page
        return rec


def in_session(pid, sid):
    """``getsid(pid) == sid``: a group leader still in the pane's session (a reused pgid elsewhere never is)."""
    try:
        return os.getsid(pid) == sid
    except OSError:
        return False


def kill_group(pgid, gone, timeout_ms, log, fg=None):
    """R4: SIGHUP@0, SIGTERM@2000, SIGKILL@5000 ms, a step only if its time <= ``timeout_ms`` and the group is
    still there; then wait for the reap until ``timeout_ms`` + 500 ms, never signalling during that grace.
    ``fg()``: the pane's foreground job group while it is alive and still in the pane's session, else None; it gets
    the same steps and is waited for until the deadline, but only the pane's own group makes a kill_timeout."""
    t0 = time.monotonic()
    elapsed = lambda: (time.monotonic() - t0) * 1000
    job = lambda: fg() if fg is not None else None
    done = lambda: gone() and job() is None
    for at, sig in KILL_SCHEDULE:
        if at > timeout_ms:
            break
        while elapsed() < at:
            if done():
                return True
            time.sleep(0.02)
        if done():
            return True
        for g in ([] if gone() else [pgid]) + [g for g in [job()] if g is not None]:
            try:
                os.killpg(g, sig)
                log('INFO', f'sent {sig.name} to process group {g}')
            except ProcessLookupError:
                pass
            except PermissionError:
                # macOS answers EPERM for a group whose members have all exited but are not reaped yet (a tmux
                # client that just detached). Not success: only ``gone()`` (ESRCH after the reap) is; a group that
                # stays EPERM or alive runs into the same bounded wait and is reported as kill_timeout.
                log('WARN', f'{sig.name} to process group {g}: permission denied; waiting for the reap')
    while elapsed() < timeout_ms + KILL_REAP_GRACE_MS:
        if done():
            return True
        time.sleep(0.02)
    return gone()


# ---------------------------------------------------------------- server

class Server:
    def __init__(self, runtime, session, size, listener, dirlock, prefix='C-b', scrollback=None, record=None,
                 theme=None):
        self.runtime, self.session, self.listener, self.dirlock = runtime, session, listener, dirlock
        self.prefix, self.prefix_byte, self.record = prefix, prefix_byte(prefix), record
        self.theme = theme if theme is not None else {**CMUX_THEME, 'ansi': list(CMUX_THEME['ansi'])}
        self.frames, self.parse_bytes = 0, 0
        self.cols, self.rows = size
        self.lock = threading.RLock()
        self.epoch = ''.join(random.choice('0123456789abcdefghijklmnopqrstuvwxyz') for _ in range(4))
        self.seq, self.panes = 0, {}
        self.ratios = {'h': 50, 'v': 60}
        self.pages = {'': Page('', session, self.ratios)}   # M4: page id -> Page; '' = root, always there
        self.current = ''                                    # the page every viewer shows
        self.tl = threading.local()                          # .page: this control connection's page
        self.views, self.active_view = {}, None     # view conn -> (cols, rows); whose size the PTYs follow
        self.pending = {}                           # view conn -> monotonic time its prefix byte arrived
        self.wlocks = {}                            # view conn -> lock serialising its frame writes
        self.web = threading.Condition()            # guards the four web fields below
        self.windows = []                           # view conns with a web window, the primary one last
        self.web_seq, self.web_wait, self.web_owner = 0, {}, {}   # seq -> reply (None: waiting) / its viewer
        self.keys = load_keys()
        self.scrollback = scrollback if scrollback is not None else int(os.environ.get('FAKE_FM_SCROLLBACK') or 10000)
        self.fail = set(filter(None, (os.environ.get('FAKE_FM_FAIL') or '').split(',')))
        self.expect_uid = int(os.environ.get('FAKE_FM_EXPECT_UID') or os.geteuid())
        self.req_log = os.environ.get('FAKE_FM_LOG')
        self.shutting_down = False
        try:
            saved = json.loads((runtime / 'layout.json').read_text())
            h, v = saved['h_ratio'], saved['v_ratio']
            if not all(type(r) is int and 10 <= r <= 90 for r in (h, v)):
                raise ValueError(f'ratios {h!r} / {v!r}')
            self.layout.h_ratio, self.layout.v_ratio = h, v      # pane lists name an old epoch: dropped
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.log('WARN', f'layout.json ignored ({exc}); default ratios')

    def log(self, level, msg):
        log_line(self.runtime / 'server.log', level, msg)

    # -- pages (M4)
    def conn_page(self):
        """The page the running request works on: a control connection's own page ('' = unbound / root);
        a viewer thread (or the server itself) works on the current page."""
        page = getattr(self.tl, 'page', None)
        return self.current if page is None else page

    @property
    def layout(self):
        page = self.pages.get(self.conn_page())
        return page.layout if page is not None else Layout(self.ratios)   # not created yet: empty groups

    @property
    def kv(self):
        page = self.pages.get(self.conn_page())
        return page.kv if page is not None else {}

    def ensure_page(self, pid):
        """The connection's page, created ``live`` (title = its id) when it does not exist yet."""
        page = self.pages.get(pid)
        if page is None:
            if len(self.pages) - 1 >= MAX_PAGES:
                raise Fail('bad_request', f'at most {MAX_PAGES} pages besides the root')
            page = self.pages[pid] = Page(pid, pid, self.ratios)
            self.log('INFO', f'page {pid!r} created live')
        return page

    def page_panes(self, pid):
        page = self.pages.get(pid)
        if page is None:
            return []
        return [self.panes[i] for g in GROUPS for i in page.layout.groups[g] if i in self.panes]

    # -- ledgers
    def save(self):
        with self.lock:
            keys = ('pane', 'pid', 'pgid', 'start_time', 'tty', 'group', 'index', 'title', 'type') + (
                () if NO_PAGES else ('page',))
            atomic_write(self.runtime / 'panes.json', {'epoch': self.epoch, 'panes': [
                {k: r[k] for k in keys}
                for r in ({**p.record(), 'start_time': p.started} for p in self.ordered())]})
            atomic_write(self.runtime / 'layout.json', {**self.pages[''].layout.obj(), 'version': 1})   # R11
            for p in self.panes.values():
                p.resize(*self.rect_of(p.id))
                if self.shown(p.id):
                    p.unseen = False

    def layout_of(self, pid):
        """The layout of the page the pane is in (M4)."""
        p = self.panes.get(pid)
        page = self.pages.get(p.page if p is not None else self.conn_page())
        return page.layout if page is not None else Layout(self.ratios)

    def shown(self, pid):
        """On screen for a viewer: on the current page, its group's selected tab, and no other pane zoomed."""
        p = self.panes.get(pid)
        if p is not None and p.page != self.current:
            return False
        lay = self.layout_of(pid)
        g = lay.group_of(pid)
        return g is not None and lay.selected[g] == pid and lay.zoom in (None, pid)

    def rect_of(self, pid, group=None, layout=None):
        lay = layout if layout is not None else self.layout_of(pid)
        rects = lay.rects(self.cols, self.rows)
        return rects['zoom' if lay.zoom == pid else group or lay.group_of(pid)]

    def ordered(self):
        """Every pane of every page: the root page first, then the other pages in creation order."""
        return [p for pid in self.pages for p in self.page_panes(pid)]

    def drop(self, pane):
        with self.lock:
            if self.panes.pop(pane.id, None) is None:
                return
            page = self.pages.get(pane.page)
            if page is not None:
                page.layout.remove(pane.id)
            self.save()
        pane.close()
        self.log('INFO', f'pane {pane.id} (pid {pane.pid}) gone, exit status {pane.proc.returncode}')

    def pane(self, req, name='pane'):
        ref = arg(req, name, str)
        p = self.panes.get(ref)
        if p is None:
            raise Fail('no_such_pane', f'no pane {ref}')
        return p

    # -- connections
    def serve(self):
        info = {'pid': os.getpid(), 'start_time': start_time(os.getpid()), 'version': VERSION, 'protocol': PROTOCOL,
                'epoch': self.epoch}
        if not NO_PAGES:
            info['pages'] = 1                    # M4 route marker (design §4.2)
        atomic_write(self.runtime / 'server.json', info)
        self.save()
        self.log('INFO', f'server up: epoch {self.epoch} session {self.session!r} pid {os.getpid()}')
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                time.sleep(3600)                 # listener closed by shutdown; finish() exits the process
                continue
            if self.shutting_down:
                conn.close()
                continue
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()

    def handle(self, conn):
        try:
            uid = peer_uid(conn)
            if not peer_allowed(self.expect_uid, uid):
                self.log('WARN', f'dropped a connection from uid {uid}')
                return
            rfile = conn.makefile('rb')
            first = True
            self.tl.page = None                  # viewer threads keep None: they work on the current page
            ctx = {'page': None}                 # this control connection's page binding (M4)
            while True:
                line = rfile.readline(MAX_LINE + 1)
                if not line:
                    return
                if len(line.rstrip(b'\r\n')) > MAX_LINE:
                    conn.sendall(reply(None, Fail('bad_request', 'request line over 4 MiB')))
                    return
                req = parse(line)
                if first and isinstance(req, dict) and 'view' in req:
                    return self.view(conn, rfile, req)
                first = False
                self.tl.ctx = ctx
                self.tl.page = ctx['page'] or ''
                out, after = self.dispatch(line, req)
                conn.sendall(out)
                if after:
                    after()
        except OSError:
            pass
        finally:
            conn.close()

    def dispatch(self, line, req):
        """-> (reply line, callable to run after the reply is sent or None)."""
        if self.req_log:
            with open(self.req_log, 'ab') as fh:
                fh.write(line.rstrip(b'\r\n') + b'\n')
        rid = None
        try:
            if isinstance(req, Fail):
                raise req
            if not isinstance(req, dict):
                raise Fail('bad_request', 'a request is a JSON object')
            rid = req.get('id')
            if type(rid) is not int or not 0 <= rid <= MAX_ID:
                rid = None
                raise Fail('bad_request', 'request needs an integer "id" in 0..2^53-1')
            cmd = req.get('cmd')
            if not isinstance(cmd, str):
                raise Fail('bad_request', 'request needs a string "cmd"')
            fn = HANDLERS.get(cmd)
            if fn is None:
                raise Fail('unknown_cmd', f'unknown command {cmd}')
            if cmd in self.fail:
                raise Fail('internal', f'{cmd} failed (FAKE_FM_FAIL)')
            body = fn(self, req) or {}
            after = body.pop('_after', None)
            return reply(rid, body), after
        except Fail as exc:
            return reply(rid, exc), None
        except Exception as exc:                 # noqa: BLE001 - the protocol has a code for it
            self.log('ERROR', f'internal error: {exc!r}')
            return reply(rid, Fail('internal', repr(exc))), None

    def view(self, conn, rfile, hello):
        """§6: handshake check, ``0x81`` first screen, then 0x01 input / 0x02 size / 0x03 detach."""
        bad = []
        for k in ('cols', 'rows'):
            if type(hello.get(k)) is not int or not 1 <= hello[k] <= 65535:
                bad.append(k)
        bad += [k for k in ('term', 'colorterm') if not isinstance(hello.get(k), str)]
        bad += ['cmd'] if 'cmd' in hello else []
        bad += ['view'] if type(hello['view']) is not int else []
        if bad or hello['view'] != VIEW_VERSION:
            code = 'bad_request' if bad else 'protocol_version'
            msg = f'bad view handshake: {", ".join(bad)}' if bad else f'view {hello["view"]} != {VIEW_VERSION}'
            conn.sendall(json.dumps({'ok': False, 'code': code, 'error': msg}, separators=(',', ':')).encode() + b'\n')
            return
        ok = {'ok': True, 'epoch': self.epoch, 'protocol': PROTOCOL}
        if not NO_PAGES:
            ok['pages'] = 1                      # M4: this server understands pages and the 0x04 jump event
        conn.sendall(json.dumps(ok, separators=(',', ':')).encode() + b'\n')
        self.wlocks[conn] = threading.Lock()
        with self.lock:
            self.views[conn] = (hello['cols'], hello['rows'])
            self.follow(conn)
            sel = self.layout.selected[self.layout.focus]
            screen = self.panes[sel].screen.text(self.rows) if sel in self.panes else ''
            self.frames += 1
        self.send(conn, frame(0x81, b'\x1b[H\x1b[2J' + screen.replace('\n', '\r\n').encode()[:MAX_FRAME - 16]))
        try:
            while True:
                head = rfile.read(5)
                if len(head) < 5:
                    return
                kind, size = head[0], struct.unpack('>I', head[1:])[0]
                if size > MAX_FRAME or kind not in (1, 2, 3, 4) or kind == 2 and size != 4 or kind == 3 and size:
                    self.log('WARN', f'view protocol error: frame type {kind:#x} length {size}; closing')
                    return
                body = rfile.read(size)
                if len(body) < size or kind == 3:
                    return
                if kind == 4:
                    try:
                        for f in self.web_in(conn, body):
                            self.send(conn, f)
                    except ValueError as exc:
                        self.log('WARN', f'view protocol error: bad 0x04 payload ({exc}); closing')
                        return
                    continue
                with self.lock:
                    if kind == 2:
                        cols, rows = struct.unpack('>HH', body)
                        if not cols or not rows:
                            return
                        self.views[conn] = (cols, rows)
                        if self.active_view is conn:
                            self.follow(conn)
                    else:
                        self.follow(conn)
                        body, out = self.keys_in(conn, SGR_MOUSE.sub(b'', body))
                        sel = self.layout.selected[self.layout.focus]
                        if body and sel in self.panes:
                            self.panes[sel].write(body)
                for f in out if kind == 1 else ():
                    self.send(conn, f)
        except (OSError, Fail):
            pass
        finally:
            self.viewer_gone(conn)
            with self.lock:
                self.views.pop(conn, None)
                self.pending.pop(conn, None)
                if self.active_view is conn:
                    self.active_view = None

    def send(self, conn, data):
        """One frame to a viewer; writes from different threads never interleave."""
        lock = self.wlocks.get(conn)
        if lock is None:
            conn.sendall(data)
            return
        with lock:
            conn.sendall(data)

    def web_in(self, conn, body):
        """One ``0x04`` (§6.3): a window event, a page request or the reply to a relayed ``0x84`` -> frames to send.
        ValueError (not UTF-8 / not a JSON object) is a protocol error."""
        msg = json.loads(body.decode('utf-8'))
        if not isinstance(msg, dict):
            raise ValueError('0x04 payload is not a JSON object')
        if msg.get('event') == 'window':
            with self.web:
                if conn in self.windows:
                    self.windows.remove(conn)
                if msg.get('state') == 'open':
                    self.windows.append(conn)
                self.web.notify_all()
            return []
        if msg.get('op') == 'page':
            return [web_frame(self.page_for(msg))]
        if msg.get('event') == 'jump' and not NO_PAGES:
            self.jump(msg.get('url'))
            return []
        seq = msg.get('seq')
        with self.web:
            if type(seq) is int and seq in self.web_wait and self.web_owner.get(seq) is conn:
                self.web_wait[seq] = msg
                self.web.notify_all()
        return []

    def page_for(self, msg):
        """The answer to ``{"op":"page","path","have"}`` (§4.21): the page's size / mtime, ``b64`` only when the
        viewer's copy differs."""
        out = {'op': 'page'}
        if type(msg.get('seq')) is int:
            out['seq'] = msg['seq']
        path = msg.get('path')
        try:
            if not isinstance(path, str) or not path:
                raise OSError('page request without a path')
            st = os.stat(path)
            page = {'path': path, 'size': st.st_size, 'mtime': st.st_mtime_ns}
            if msg.get('have') != {'size': page['size'], 'mtime': page['mtime']}:
                page['b64'] = base64.b64encode(Path(path).read_bytes()).decode()
                if len(page['b64']) > WEB_FRAME_MAX - 1024:
                    raise OSError(f'page {path} is over the 32 MiB 0x84 limit')
            out['page'] = page
        except OSError as exc:
            out['page'], out['error'] = None, str(exc)
        return out

    def jump(self, url):
        """R13: ``0x04 {"event":"jump","url"}`` -> select the page whose kv ``history_url`` path equals the path of
        the URL's ``to`` parameter; no match -> INFO, nothing else."""
        to = (parse_qs(urlparse(url).query).get('to') or [''])[0] if isinstance(url, str) else ''
        want = unquote(urlparse(to).path) if to else ''
        with self.lock:
            hit = next((pid for pid, page in self.pages.items()
                        if want and unquote(urlparse(page.kv.get('history_url') or '').path) == want), None)
        if hit is None:
            self.log('INFO', f'jump to {to!r}: no page has that history page')
            return
        select_page(self, hit)

    def relay(self, conns, op, url=None):
        """``0x84 {"seq","op","url"?}`` to each of ``conns``; -> [(conn, its 0x04 reply or None)] after every reply
        arrived or ``WEB_TIMEOUT_S`` passed."""
        waits = []
        with self.web:
            for c in conns:
                self.web_seq += 1
                self.web_wait[self.web_seq], self.web_owner[self.web_seq] = None, c
                waits.append((c, self.web_seq))
        for c, seq in waits:
            body = {'seq': seq, 'op': op} if url is None else {'seq': seq, 'op': op, 'url': url}
            try:
                self.send(c, web_frame(body))
            except OSError:
                pass
        deadline = time.monotonic() + WEB_TIMEOUT_S
        with self.web:
            while any(self.web_wait[seq] is None for _, seq in waits):
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self.web.wait(left)
            out = [(c, self.web_wait.pop(seq)) for c, seq in waits]
            for _, seq in waits:
                self.web_owner.pop(seq, None)
        return out

    def viewer_gone(self, conn):
        with self.web:
            if conn in self.windows:
                self.windows.remove(conn)
            for seq, owner in self.web_owner.items():
                if owner is conn and self.web_wait.get(seq, 0) is None:
                    self.web_wait[seq] = {'seq': seq, 'ok': False, 'error': 'viewer went away'}
            self.web.notify_all()

    def keys_in(self, conn, data):
        """§10 prefix handling for one ``0x01`` body -> (bytes for the focused pane, frames for this viewer)."""
        keep, out, i, pre = bytearray(), [], 0, self.prefix_byte
        while i < len(data):
            since = self.pending.pop(conn, None)
            if since is None or time.monotonic() - since > PREFIX_WAIT:
                j = data.find(pre, i)
                keep += data[i:] if j < 0 else data[i:j]
                if j < 0:
                    break
                self.pending[conn], i = time.monotonic(), j + 1
                continue
            m = ESC_SEQ.match(data, i)
            key = (data[i:i + 3] if data[i:i + 2] == b'\x1bO' else       # SS3 arrows (outer app-cursor mode)
                   m.group() if m and data[i:i + 1] == b'\x1b' else data[i:i + 1])
            i += len(key)
            if key == bytes([pre]):
                keep += key
            else:
                out += self.prefix_key(key)
        return bytes(keep), out

    def prefix_key(self, key):
        lay, f = self.layout, self.layout.focus
        tabs = lay.groups[f]
        cur = tabs.index(lay.selected[f]) if lay.selected[f] in tabs else 0
        pick = None
        if key in b'123456789' and len(key) == 1 and int(key) <= len(tabs):
            pick = tabs[int(key) - 1]
        elif key in (b'\t', b'\x1b[Z') and tabs:
            pick = tabs[(cur + (1 if key == b'\t' else -1)) % len(tabs)]
        elif key in (b'\x1b[C', b'\x1b[D', b'\x1bOC', b'\x1bOD'):
            lay.focus = 'workers' if key[-1:] == b'C' else 'supervisor' if f == 'workers' else f
        elif key in (b'\x1b[A', b'\x1b[B', b'\x1bOA', b'\x1bOB') and f != 'workers':
            lay.focus = 'supervisor' if key[-1:] == b'A' else 'lower'
        elif key == b'z' and lay.selected[f] is not None:
            lay.zoom = None if lay.zoom == lay.selected[f] else lay.selected[f]
        elif key in (b'<', b'>'):
            lay.h_ratio = clamp(lay.h_ratio + (5 if key == b'>' else -5))
        elif key in (b'+', b'-'):
            lay.v_ratio = clamp(lay.v_ratio + (5 if key == b'+' else -5))
        elif key == b'=':
            lay.h_ratio, lay.v_ratio = 50, 60
        elif key == b'o':
            url = self.pages[self.current].kv.get('history_url') or ''   # the server reads the current page's
            return [frame(0x83, (url[len('file://'):] if url.startswith('file://') else url).encode())]
        else:
            return []                            # [ (page mode), d, anything else: swallowed
        if pick is not None:
            lay.select(pick)
        self.save()
        return []

    def follow(self, conn):
        """PTY sizes follow the viewer that sent input last (before any input: the one that connected last)."""
        self.active_view = conn
        if (self.cols, self.rows) != self.views[conn]:
            self.cols, self.rows = self.views[conn]
            self.save()

    def finish(self, live=False):
        """Clean exit: 0x82 to the viewers, drop ctl.sock / server.json (/ panes.json unless panes may live)."""
        for conn in list(self.views):
            try:
                self.send(conn, frame(0x82, b'server shutting down'))
            except OSError:
                pass
        for name in ('ctl.sock', 'server.json') + (() if live else ('panes.json',)):
            unlink(self.runtime / name)
        self.log('INFO', 'server exits')
        os._exit(0)


def prefix_byte(name):
    """``C-b`` -> 0x02 (any ``C-<x>`` with x in ``@a-z[\\]^_``); None for anything else."""
    m = re.fullmatch(r'C-([@a-z\[\\\]^_])', name or '')
    return ord(m[1].upper()) & 0x1f if m else None


def frame(kind, payload):
    return struct.pack('>BI', kind, len(payload)) + payload


def web_frame(obj):
    """``0x84``: one JSON object, no newline (§6.3)."""
    return frame(0x84, json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode())


def parse(line):
    try:
        return json.loads(line.decode('utf-8'))
    except UnicodeDecodeError:
        return Fail('bad_request', 'request line is not UTF-8')
    except ValueError as exc:
        return Fail('bad_request', f'bad JSON: {exc}')


def reply(rid, body):
    if isinstance(body, Fail):
        out = {'id': rid, 'ok': False, 'code': body.code, 'error': body.error}
    else:
        out = {'id': rid, 'ok': True, **body}
    return json.dumps(out, ensure_ascii=False, separators=(',', ':')).encode() + b'\n'


def arg(req, name, kind, default=..., ok=None, why='out of range'):
    """A typed argument: missing (no default) / wrong JSON type / ``ok`` false -> bad_request."""
    v = req.get(name, default)
    if v is ... or (kind is not None and (not isinstance(v, kind) or type(v) is bool and kind is not bool)):
        raise Fail('bad_request', f'{req.get("cmd")}: missing or ill-typed {name!r}')
    if ok and not ok(v):
        raise Fail('bad_request', f'{req.get("cmd")}: {name!r} {why}')
    return v


def opt(req, name, kind, **kw):
    return None if req.get(name) is None else arg(req, name, kind, **kw)


def clamp(v):
    return min(90, max(10, v))


# ---------------------------------------------------------------- commands (PROTOCOL.md §4)

def check_page_id(pid):
    if not isinstance(pid, str) or not PAGE_ID.match(pid):
        raise Fail('bad_request', f'bad page id {pid!r} (want [A-Za-z0-9._-]{{1,64}})')
    return pid


def known_page(s, pid):
    if pid not in s.pages:
        raise Fail('bad_request', f'no such page: {pid}')
    return s.pages[pid]


def bound(s):
    """True on a connection bound to a page other than the root (M4)."""
    return not NO_PAGES and s.conn_page() != ''


def c_hello(s, req):
    want = opt(req, 'protocol', int)
    if want is not None and want != PROTOCOL:
        raise Fail('protocol_version', f'server speaks protocol {PROTOCOL}, client asked for {want}')
    ctx = getattr(s.tl, 'ctx', None)
    page = None if NO_PAGES else opt(req, 'page', str)  # an M3 server ignores the key (F9)
    if page is not None and ctx is not None:
        if page != '':
            check_page_id(page)
        if ctx['page'] is not None and ctx['page'] != page:
            raise Fail('bad_request', f'this connection is bound to page {ctx["page"]!r}, not {page!r}')
        ctx['page'] = page
        s.tl.page = page
    with s.lock:
        viewers = len(s.views)
        out = {'uid': os.geteuid(), 'pid': os.getpid(), 'epoch': s.epoch, 'version': VERSION, 'protocol': PROTOCOL,
               'run': s.kv.get('run'), 'viewers': viewers, 'prefix': s.prefix, 'theme': s.theme}
        if not NO_PAGES:
            out.update(pages=1, page=s.conn_page(), current=s.current)
    return out


def kv_key(s, req):
    key = arg(req, 'key', str, ok=lambda k: 1 <= len(k.encode()) <= 256, why='must be 1-256 bytes')
    if key in ROOT_ONLY_KV and bound(s):
        raise Fail('bad_request', f'kv key {key!r} is the root page\'s only')
    return key


def c_kv_set(s, req):
    key = kv_key(s, req)
    value = arg(req, 'value', str, ok=lambda v: len(v.encode()) <= 64 * 1024, why='over 64 KiB')
    with s.lock:
        if s.conn_page() not in s.pages:
            raise Fail('bad_request', f'no such page: {s.conn_page()}')
        s.kv[key] = value


def c_kv_get(s, req):
    key = kv_key(s, req)
    with s.lock:
        return {'value': s.kv.get(key)}


def c_spawn(s, req):
    argv = arg(req, 'argv', list, ok=lambda v: v and all(isinstance(a, str) for a in v), why='must be non-empty strings')
    cwd = opt(req, 'cwd', str, ok=os.path.isabs, why='must be absolute')
    extra = arg(req, 'env', dict, {}, ok=lambda v: all(isinstance(x, str) for kv in v.items() for x in kv),
                why='must map strings to strings')
    title = arg(req, 'title', str, '')
    ty = arg(req, 'type', str, '')
    target = None if NO_PAGES else opt(req, 'page', str)   # M4: an explicit page (the parent's record pages)
    group = opt(req, 'group', str)
    if group is not None and group not in GROUPS:
        raise Fail('unknown_type', f'unknown group {group}')
    anchor = opt(req, 'anchor', str)
    before = arg(req, 'before', bool, False)
    if req.get('before') is not None and anchor is None:
        raise Fail('bad_request', 'pane.spawn: before without anchor')
    if cwd is not None and not os.path.isdir(cwd):
        raise Fail('spawn_failed', f'cwd {cwd} is not a directory')
    env = {k: v for k, v in os.environ.items() if not k.startswith('FAKE_FM_')}
    env.update({'TERM': 'xterm-256color', 'COLORTERM': 'truecolor', **extra})
    with s.lock:
        page = known_page(s, target) if target is not None else s.ensure_page(s.conn_page())
        lay = page.layout
        if anchor is not None:
            if anchor not in s.panes:
                raise Fail('no_such_pane', f'no anchor pane {anchor}')
            if s.panes[anchor].page != page.id:
                raise Fail('bad_request', f'anchor pane {anchor} is not in page {page.id!r}')
            g = lay.group_of(anchor)
            if group is not None and group != g:
                raise Fail('bad_request', f'pane.spawn: group {group} differs from the anchor\'s group {g}')
            i = lay.groups[g].index(anchor) + (0 if before else 1)
        elif group is not None:
            g, i = group, None
        else:
            g, i = lay.place_by_type(ty, {k: p.type for k, p in s.panes.items()})
        s.seq += 1
        pid_ = f'{s.epoch}-{s.seq}'
        pane = Pane(s, pid_, argv, cwd, env, title, ty, s.rect_of(pid_, g, lay))
        pane.page = page.id
        s.panes[pid_] = pane
        lay.add(pid_, g, i, anchor if before else None)
        if page.state == 'reclaimed' and ty != 'history':
            page.state = 'live'                  # a record pane keeps a page reclaimed; anything else reopens it
        s.save()
    s.log('INFO', f'spawned {pid_} pid {pane.pid} tty {pane.tty} type {ty!r}: {argv!r}')
    return {'pane': pid_, 'pid': pane.pid, 'pgid': pane.pgid, 'tty': pane.tty}


def c_list(s, req):
    every = False if NO_PAGES else arg(req, 'all', bool, False)
    with s.lock:
        panes = s.ordered() if every or NO_PAGES else s.page_panes(s.conn_page())
        return {'panes': [p.record() for p in panes]}


def c_get(s, req):
    with s.lock:
        return s.pane(req).record()


def c_write(s, req):
    p = s.pane(req)
    try:
        data = base64.b64decode(arg(req, 'data', str), validate=True)
    except ValueError:
        raise Fail('bad_request', 'pane.write: data is not standard padded base64')
    mode = arg(req, 'mode', str)
    if mode not in MODES:
        raise Fail('unknown_type', f'unknown write mode {mode}')
    on = p.screen.modes['bracketed_paste']
    if mode == 'bracketed' and not on:
        s.log('WARN', f'bracketed paste sent but pane {p.id} has 2004 off')
    p.write(PASTE_START + data + PASTE_END if mode == 'bracketed' or mode == 'paste' and on else data)
    return {'written': len(data)}


def c_key(s, req):
    p = s.pane(req)
    name = arg(req, 'key', str)
    data = s.keys.get(name)
    if data is None:
        raise Fail('unknown_key', f'unknown key name {name}')
    if p.screen.modes['app_cursor'] and data in CURSOR_KEYS:
        data = b'\x1bO' + data[2:]
    p.write(data)


def c_read(s, req):
    p = s.pane(req)
    n = arg(req, 'lines', int, ok=lambda v: v >= 1, why='must be >= 1')
    with s.lock:
        return {'text': p.screen.text(n)}


def c_rename(s, req):
    with s.lock:
        p = s.pane(req)
        p.title = arg(req, 'title', str)
        s.save()


def c_focus(s, req):
    with s.lock:
        p = s.pane(req)
        s.layout_of(p.id).select(p.id)          # in the pane's own page; the current page never changes
        s.save()


def c_move(s, req):
    with s.lock:
        p = s.pane(req)
        g = arg(req, 'group', str)
        if g not in GROUPS:
            raise Fail('unknown_type', f'unknown group {g}')
        before, after = opt(req, 'before', str), opt(req, 'after', str)
        for ref in (before, after):
            if ref is not None and ref not in s.panes:
                raise Fail('no_such_pane', f'no reference pane {ref}')
        if before is not None and after is not None:
            raise Fail('bad_request', 'pane.move: before and after are exclusive')
        here = s.conn_page()
        for ref in (before, after):
            if ref is not None and s.panes[ref].page != here:
                raise Fail('bad_request', f'reference pane {ref} is not in page {here!r}')
        page = s.ensure_page(here)
        if p.page != here:                       # M4: pull a pane of another page into this one, same group too
            old = s.pages.get(p.page)
            if old is not None:
                old.layout.remove(p.id)
            p.page = here
            if page.state == 'reclaimed' and p.type != 'history':
                page.state = 'live'
        page.layout.move(p.id, g, before=before, after=after)
        s.save()


def c_kill(s, req):
    p = s.pane(req)
    timeout = arg(req, 'timeout_ms', int, 5000, ok=lambda v: 0 <= v <= 600000, why='must be 0-600000')
    kill_pane(s, p, timeout)


def kill_pane(s, p, timeout):
    p.killing = True
    job = p.fg_job()                             # read before the first signal (§4.13)

    def fg():
        if job is None or not in_session(job, p.pid):
            return None
        try:
            os.killpg(job, 0)
            return job
        except ProcessLookupError:
            return None
        except PermissionError:
            return job

    if not kill_group(p.pgid, p.group_gone, timeout, s.log, fg):
        p.killing = False
        raise Fail('kill_timeout', f'process group {p.pgid} of {p.id} still alive after {timeout} ms (+grace)')
    s.drop(p)


def c_layout_get(s, req):
    with s.lock:
        return s.layout.obj()


def c_layout_set(s, req):
    with s.lock:
        lay = s.layout                           # ratios are one dict for every page; zoom is this page's
        h, v = opt(req, 'h_ratio', int), opt(req, 'v_ratio', int)
        zoom = req.get('zoom', ...)
        if zoom is not ... and zoom is not None:
            if not isinstance(zoom, str):
                raise Fail('bad_request', 'layout.set: zoom is a pane id or null')
            if zoom not in s.panes:
                raise Fail('no_such_pane', f'no pane {zoom} to zoom')
        if h is not None:
            lay.h_ratio = clamp(h)
        if v is not None:
            lay.v_ratio = clamp(v)
        if zoom is not ...:
            lay.zoom = zoom
        s.save()
        return lay.obj()


def c_stats(s, req):
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == 'darwin' else 1024)
    with s.lock:
        out = {'panes': len(s.panes), 'viewers': len(s.views), 'frames': s.frames, 'parse_bytes': s.parse_bytes,
               'wakeups': 0, 'rss_bytes': rss,
               'scrollback_rows': sum(p.screen.scrollback() for p in s.panes.values())}
        if not NO_PAGES:
            out['pages'] = len(s.pages)
        return out


def c_shutdown(s, req):
    if bound(s):                                 # second guard (§0 item 5): a child never stops the tree's server
        raise Fail('bad_request', 'server.shutdown is refused on a page connection')
    force = arg(req, 'force', bool, False)
    with s.lock:
        panes = s.ordered()
        if panes and not force:
            raise Fail('not_empty', f'{len(panes)} panes still running')
        s.shutting_down = True                   # stop accepting
    try:
        for p in panes:
            kill_pane(s, p, 5000)
    except Fail:
        s.shutting_down = False                  # a pane survived: stay up and say so
        raise
    try:
        s.listener.close()
    except OSError:
        pass
    return {'_after': s.finish}                  # reply ok first, then remove the live files and exit


def c_web_open(s, req):
    url = opt(req, 'url', str)
    with s.lock:
        url = s.kv.get('history_url') if url is None else url
        conns = list(s.views)
    replies = s.relay(conns, 'open', url)
    with s.web:
        windows = list(s.windows)
    if any(r is None and c in windows for c, r in replies):
        raise Fail('web_timeout', f'a web window did not answer web.open within {WEB_TIMEOUT_S:g} s')
    return {'windows': len(windows)}


def _relayed(s, cmd, conns, op, url=None):
    replies = s.relay(conns, op, url)
    if any(r is None for _, r in replies):
        raise Fail('web_timeout', f'a web window did not answer {cmd} within {WEB_TIMEOUT_S:g} s')
    return [r for _, r in replies]


def background(s):
    """M4: this connection's page is not the current one (its daemon must not drive the one web window)."""
    return not NO_PAGES and s.conn_page() != s.current


def c_web_goto(s, req):
    url = arg(req, 'url', str)
    if background(s):
        return {'windows': 0}
    with s.web:
        conns = list(s.windows)
    if not conns:
        raise Fail('no_window', 'no viewer has a web window')
    return {'windows': sum(r.get('ok') is True for r in _relayed(s, 'web.goto', conns, 'goto', url))}


def c_web_url(s, req):
    if background(s):
        raise Fail('no_window', 'this page is not shown: no web window for it')
    with s.web:
        primary = s.windows[-1] if s.windows else None
    if primary is None:
        raise Fail('no_window', 'no viewer has a web window')
    r, = _relayed(s, 'web.url', [primary], 'url')
    if r.get('ok') is not True or not isinstance(r.get('url'), str):
        raise Fail('internal', f'web window: {r.get("error") or "no url in the reply"}')
    if bound(s):
        mine = urlparse(s.kv.get('history_url') or '').path
        if urlparse(r['url']).path != mine:
            raise Fail('no_window', 'the web window shows another page than this page\'s history')
    return {'url': r['url']}


def c_web_reload(s, req):
    if background(s):
        return {'windows': 0}
    with s.web:
        conns = list(s.windows)
    if not conns:
        return {'windows': 0}                    # nobody to reload: not an error (§4.21)
    return {'windows': sum(r.get('ok') is True for r in _relayed(s, 'web.reload', conns, 'reload'))}


def c_web_eval(s, req):
    arg(req, 'js', str)
    raise Fail('not_implemented', 'web.eval is reserved and not implemented')


def c_page_open(s, req):
    pid = check_page_id(arg(req, 'page', str))
    title = arg(req, 'title', str, pid)
    reclaimed = arg(req, 'reclaimed', bool, False)
    url = opt(req, 'history_url', str)
    with s.lock:
        page = s.ensure_page(pid)
        page.title = title
        if reclaimed:
            page.state = 'reclaimed'
            if url is not None:
                page.kv['history_url'] = url
        else:
            page.state = 'live'
            if url is not None:
                page.kv['history_url'] = url
    return {'page': pid, 'state': page.state}


def c_page_close(s, req):
    pid = arg(req, 'page', str)
    keep = arg(req, 'keep', bool, True)
    with s.lock:
        if pid == '':
            raise Fail('bad_request', 'the root page cannot be closed')
        page = known_page(s, pid)
        left = s.page_panes(pid)
        if left:
            raise Fail('not_empty', f'page {pid!r} still has {len(left)} pane(s)')
        if keep:
            page.state = 'reclaimed'
        else:
            del s.pages[pid]
            if s.current == pid:
                s.current = ''
        s.save()


def select_page(s, pid):
    """Make ``pid`` the current page; every web window is sent to that page's history page."""
    with s.lock:
        known_page(s, pid)
        s.current = pid
        url = s.pages[pid].kv.get('history_url')
        s.save()
    with s.web:
        conns = list(s.windows)
    if url and conns:
        threading.Thread(target=s.relay, args=(conns, 'goto', url), daemon=True).start()


def c_page_select(s, req):
    select_page(s, arg(req, 'page', str))


def c_page_list(s, req):
    with s.lock:
        return {'pages': [{'page': pid, 'title': page.title, 'state': page.state,
                           'panes': len(s.page_panes(pid)), 'current': pid == s.current}
                          for pid, page in s.pages.items()]}


PAGE_HANDLERS = {} if NO_PAGES else {'page.open': c_page_open, 'page.close': c_page_close,
                                     'page.select': c_page_select, 'page.list': c_page_list}

HANDLERS = {'hello': c_hello, 'kv.set': c_kv_set, 'kv.get': c_kv_get, 'pane.spawn': c_spawn,
            'pane.list': c_list, 'pane.get': c_get, 'pane.write': c_write, 'pane.key': c_key,
            'pane.read': c_read, 'pane.rename': c_rename, 'pane.focus': c_focus, 'pane.move': c_move,
            'pane.kill': c_kill, 'layout.get': c_layout_get, 'layout.set': c_layout_set,
            'server.shutdown': c_shutdown, 'server.stats': c_stats,
            'web.open': c_web_open, 'web.goto': c_web_goto, 'web.url': c_web_url, 'web.reload': c_web_reload,
            'web.eval': c_web_eval, **PAGE_HANDLERS}


# ---------------------------------------------------------------- command line

def opts(args, usage, flags=(), values=()):
    """Only the subcommand's own flags; anything else, or a flag without its value, is a usage error (§9)."""
    out, rest = {}, list(args)
    while rest:
        a = rest.pop(0)
        if a in flags:
            out[a] = True
        elif a in values and rest:
            out[a] = rest.pop(0)
        else:
            die(f'unexpected argument {a!r}\nusage: {usage}', EXIT_USAGE)
    return out


def prepare_runtime(runtime):
    """0700 runtime dir owned by us (fix a loose mode, refuse a foreign owner); socket path within sun_path."""
    os.umask(0o077)
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    st = runtime.stat()
    if st.st_uid != os.geteuid():
        fatal(runtime, f'runtime {runtime} is owned by uid {st.st_uid}, not {os.geteuid()}; refusing', EXIT_PERMS)
    if st.st_mode & 0o077:
        try:
            os.chmod(runtime, 0o700)
        except OSError as exc:
            fatal(runtime, f'runtime {runtime} has mode {st.st_mode & 0o777:o} and chmod failed: {exc}', EXIT_PERMS)
    sock = runtime / 'ctl.sock'
    n = len(os.fsencode(str(sock)))
    if n > SUN_MAX:
        fatal(runtime, f'socket path {sock} is {n} bytes, over {SUN_MAX}; no /tmp fallback', EXIT_TOO_LONG)
    return sock


def fatal(runtime, msg, code=1):
    try:
        log_line(runtime / 'server.log', 'ERROR', msg)
    except OSError:
        pass                                     # e.g. a foreign-owned runtime: stderr still says why
    die(msg, code)


def cmd_server(args):
    usage = ('fleetmux server --runtime D --session S [--daemonize] [--default-size COLSxROWS] [--record DIR] '
             '[--scrollback N] [--prefix KEY] [--theme FILE]')
    o = opts(args, usage, ('--daemonize',),
             ('--runtime', '--session', '--default-size', '--record', '--scrollback', '--prefix', '--theme'))
    if '--runtime' not in o or '--session' not in o:
        die('usage: ' + usage, EXIT_USAGE)
    m = re.fullmatch(r'([1-9]\d*)x([1-9]\d*)', o.get('--default-size', '240x60'))
    if not m or not all(int(v) <= 65535 for v in m.groups()):
        die('--default-size wants COLSxROWS\nusage: ' + usage, EXIT_USAGE)
    if not re.fullmatch(r'\d+', o.get('--scrollback', '10000')):
        die('--scrollback wants a row count\nusage: ' + usage, EXIT_USAGE)
    if prefix_byte(o.get('--prefix', 'C-b')) is None:
        die('--prefix wants a key name like C-b\nusage: ' + usage, EXIT_USAGE)
    theme = load_theme(o['--theme'], usage) if '--theme' in o else None
    record = Path(o['--record']).absolute() if '--record' in o else None
    runtime = Path(o['--runtime']).absolute()
    sock = prepare_runtime(runtime)
    if record is not None:
        try:
            record.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            fatal(runtime, f'--record {record}: {exc}')
    if os.environ.get('FAKE_FM_START_FAIL'):
        fatal(runtime, f'start failed: {os.environ["FAKE_FM_START_FAIL"]}')
    if answers(sock):
        fatal(runtime, f'a server already answers on {sock}; refusing to start a second one', EXIT_ANSWERS)
    dirlock = os.open(runtime, os.O_RDONLY)
    try:
        fcntl.flock(dirlock, fcntl.LOCK_EX | fcntl.LOCK_NB)   # held for the server's lifetime
    except BlockingIOError:
        fatal(runtime, f'another server is starting in {runtime}; refusing', EXIT_ANSWERS)
    unlink(sock)                                               # stale: nobody answered
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(sock))
    os.chmod(sock, 0o600)
    listener.listen(64)
    if '--daemonize' in o:
        if os.fork():
            os._exit(0)                                        # caller wait()s for this; socket answers
        os.setsid()
        if os.fork():
            os._exit(0)
        null = os.open(os.devnull, os.O_RDWR)
        logfd = os.open(runtime / 'server.log', os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.dup2(null, 0)
        os.dup2(logfd, 1)
        os.dup2(logfd, 2)
    server = Server(runtime, o['--session'], (int(m[1]), int(m[2])), listener, dirlock, o.get('--prefix', 'C-b'),
                    int(o['--scrollback']) if '--scrollback' in o else None, record, theme)
    signal.signal(signal.SIGTERM, lambda *_: server.finish(live=True))   # panes may outlive: keep panes.json
    signal.signal(signal.SIGINT, lambda *_: server.finish(live=True))
    server.serve()


def cmd_attach(args):
    usage = 'fleetmux attach --runtime D [--no-mouse]'
    o = opts(args, usage, ('--no-mouse',), ('--runtime',))
    if '--runtime' not in o:
        die('usage: ' + usage, EXIT_USAGE)
    sock = Path(o['--runtime']) / 'ctl.sock'
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(5)
    try:
        s.connect(str(sock))
        size = os.get_terminal_size() if sys.stdout.isatty() else os.terminal_size((240, 60))
        s.sendall(json.dumps({'view': VIEW_VERSION, 'cols': size.columns, 'rows': size.lines,
                              'term': os.environ.get('TERM', ''), 'colorterm': os.environ.get('COLORTERM', '')}
                             ).encode() + b'\n')
        line = s.makefile('rb').readline()
        if not line:
            raise OSError('server closed the connection')
        print(line.decode().rstrip('\n'))
        if not json.loads(line).get('ok'):
            sys.exit(EXIT_ANSWERS)
        s.sendall(frame(0x03, b''))
    except OSError as exc:
        log = Path(o['--runtime']) / 'server.log'
        try:
            tail = ''.join(log.read_text(encoding='utf-8', errors='replace').splitlines(True)[-20:])
        except OSError:
            tail = f'({log} is missing)\n'
        sys.stderr.write(tail)
        die(f'cannot attach to {sock}: {exc}', EXIT_ANSWERS)
    finally:
        s.close()


def cmd_reap(args):
    usage = 'fleetmux reap --runtime D'
    o = opts(args, usage, (), ('--runtime',))
    if '--runtime' not in o:
        die('usage: ' + usage, EXIT_USAGE)
    runtime = Path(o['--runtime'])
    if answers(runtime / 'ctl.sock'):
        die(f'a server still answers on {runtime / "ctl.sock"}; use server.shutdown, not reap')
    try:
        ledger = json.loads((runtime / 'panes.json').read_text())
        if not isinstance(ledger, dict) or not isinstance(ledger.get('panes', []), list):
            raise ValueError('not a panes.json object')
    except (OSError, ValueError) as exc:
        die(f'{runtime / "panes.json"}: {exc}', 3)
    left, reaped, skipped, failed = [], [], [], False
    reasons = {'unverified': 'unverifiable', 'skipped': 'start_time_mismatch', 'gone': 'gone'}
    for p in ledger.get('panes', []):
        pid, pgid, was = p.get('pid'), p.get('pgid'), p.get('start_time')
        if type(pid) is not int or type(pgid) is not int:
            action = 'unverified'
        else:
            try:
                os.kill(pid, 0)
                now = start_time(pid)
            except ProcessLookupError:
                now = 'gone'
            except PermissionError:
                now = None
            if now == 'gone':
                action = 'gone'
            elif now is None or type(was) is not int:
                action = 'unverified'                          # §8.6: cannot verify -> never signal
            elif now != was:
                action = 'skipped'                             # pid reused by someone else: never touch it
            else:
                def gone(pid=pid, pgid=pgid):
                    for fn, target in ((os.kill, pid), (os.killpg, pgid)):
                        try:
                            fn(target, 0)
                            return False
                        except ProcessLookupError:
                            pass
                        except PermissionError:
                            return False
                    return True
                action = 'killed' if kill_group(pgid, gone, 5000, lambda *_: None) else 'kill_timeout'
        if action in ('unverified', 'kill_timeout'):
            left.append(p)
        if action in ('killed', 'kill_timeout'):
            reaped.append(pgid)
            failed = failed or action == 'kill_timeout'
        else:
            skipped.append({'pane': p.get('pane'), 'pid': pid, 'reason': reasons[action]})
    print(json.dumps({'reaped': reaped, 'skipped': skipped}, separators=(',', ':')))
    atomic_write(runtime / 'panes.json', {**ledger, 'panes': left})
    unlink(runtime / 'ctl.sock')
    sys.exit(1 if failed or any(x['reason'] == 'unverifiable' for x in skipped) else 0)


def main(argv):
    if argv[1:2] == ['--version']:
        print(f'fleetmux {VERSION} (protocol {PROTOCOL})')
        return
    cmds = {'server': cmd_server, 'attach': cmd_attach, 'reap': cmd_reap}
    if len(argv) < 2 or argv[1] not in cmds:
        die('usage: fleetmux --version | server | attach | reap', 2)
    cmds[argv[1]](argv[2:])


if __name__ == '__main__':
    main(sys.argv)
