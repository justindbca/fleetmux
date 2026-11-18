"""Applet tabs: every applet of a fleet is a tab in that fleet's daemon area (docs/applet-interface.md section 2.1).

An applet is a peer of the daemon, so it is shown where the daemon is shown: the tab row of the lower-left group
(history, control, then the applet tabs). The host daemon keeps one tab per applet row it watches
(``f.applet_runners``): opened when the row is declared, closed when the row is gone (unloaded / detached).

- **Tab title**: ``<name> <state>``, for example ``applet-relay-1 up``. An abnormal state (stalled, exited,
  crashloop, config_error) gets a leading ``! `` so it is seen even when the row is crowded and the end of the
  title is cut. The daemon renames the tab when the state changes.
- **Tab page**: a read-only viewer (``python -m fleetmux.applet_view <config.json> <name>``). A fixed header at the
  top shows the state, the phase text the applet publishes, the heartbeat age, restarts / crashes, the open lease
  count and when the state last changed; it is redrawn every second. Below it runs a live tail of ``applet.log``.
  The viewer draws on the terminal's alternate screen: a clear or a resize there never pushes the header into the
  scrollback, so the tab's text (``pane.read``) holds the header once, at the top.
- **Read-only**: the viewer only reads ``fx:applet:<name>``, ``fx:applet:<name>:leases`` and the log file. Keys
  typed into the tab are read and dropped; the applet's own stdin is ``/dev/null`` (applet_host.spawn).

Tabs are program-managed, not roster ``tabs:`` (design.md section 31): they carry the metadata ``applet: <name>``
and are found by it, never by title (the title changes with the state).
"""
import argparse
import json
import os
import select
import shlex
import shutil
import signal
import sys
import time
from pathlib import Path

from fleetmux import applet_host, awake

ABNORMAL = ('stalled', 'exited', 'crashloop', 'config_error')
CHECK_S = 5.0          # how often the daemon checks that its applet tabs are still shown
RETRY_S = 10.0         # a tab that did not open is tried again after this long
REFRESH_S = 1.0        # the viewer redraws its header this often
BACKLOG = 64 * 1024    # the viewer starts with at most this much of the end of applet.log
HEADER_ROWS = 3        # two header lines and a rule


# ------------------------------------------------------------------ pure parts (tested without a terminal)
def title(name, state):
    """The tab title: ``<name> <state>``, with a leading ``! `` for an abnormal state."""
    state = state or 'declared'
    return ('! ' if state in ABNORMAL else '') + name + ' ' + state


def age(seconds):
    """``3s`` / ``4m05s`` / ``2h03m`` (never negative)."""
    s = max(0, int(seconds))
    if s < 60:
        return '%ds' % s
    if s < 3600:
        return '%dm%02ds' % (s // 60, s % 60)
    return '%dh%02dm' % (s // 3600, s % 3600 // 60)


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def header(name, h, leases, now, error='', clock=None):
    """The two header lines of the viewer from the applet hash ``h`` (``fx:applet:<name>``) and the open lease
    count ``leases``; ``now`` is wall time, like ``hb_at`` and ``state_at``. ``started_at`` is the host fleet's awake
    time: ``clock`` (``awake.of``) converts ``hb_at`` to it to tell a beat of this start."""
    if error:
        return [name + '  (fleet bus unreachable)', error[:300]]
    if not h:
        return [name + '  (no record: not declared on this host, or detached)', '']
    state = h.get('state') or 'declared'
    first = ('! ' if state in ABNORMAL else '') + name + '  ' + state.upper() + '  phase: ' + (h.get('phase') or '-')
    hb, started = _float(h.get('hb_at')), _float(h.get('started_at'))
    if hb is not None and (started is None or (clock.from_wall(hb) if clock else hb) >= started):
        beat = 'heartbeat ' + age(now - hb) + ' ago'
    else:
        beat = 'no heartbeat yet'
    try:
        crashes = len(json.loads(h.get('crashes') or '[]'))
    except (TypeError, ValueError):
        crashes = 0
    parts = [beat, 'pid ' + h['pid'] if h.get('pid') else '', 'restarts ' + (h.get('restarts') or '0'),
             'crashes ' + str(crashes), 'leases ' + str(leases) + ' open']
    since = _float(h.get('state_at'))
    if since is not None:
        parts.append(state + ' since ' + time.strftime('%H:%M:%S', time.localtime(since)) + ' ('
                     + age(now - since) + ' ago)')
    return [first, ' · '.join(p for p in parts if p)]


def read_state(r, name):
    """(hash, open lease count): read calls only."""
    return r.hgetall(applet_host.key(name)), r.scard(applet_host.key(name) + ':leases')


class Tail:
    """New bytes of a log file; starts with its last ``backlog`` bytes (from a line start), follows truncation."""

    def __init__(self, path, backlog=BACKLOG):
        self.path, self.pos, self.backlog = Path(path), None, backlog

    def read(self, limit=1 << 20):
        try:
            size = self.path.stat().st_size
        except OSError:
            return b''
        skip = False
        if self.pos is None:
            self.pos = max(0, size - self.backlog)
            skip = self.pos > 0
        elif size < self.pos:
            self.pos = 0  # truncated or replaced: read it again from the start
        if size == self.pos:
            return b''
        try:
            with open(self.path, 'rb') as fh:
                fh.seek(self.pos)
                data = fh.read(min(size - self.pos, limit))
        except OSError:
            return b''
        self.pos += len(data)
        if skip:
            data = data[data.find(b'\n') + 1:] if b'\n' in data else b''
        return data


# ------------------------------------------------------------------ daemon side
def command(cfg, name):
    """The tab's program: the viewer of ``name`` on this run, with this release's interpreter and package."""
    argv = [applet_host.python(cfg), '-m', 'fleetmux.applet_view', str(Path(cfg['run']) / 'config.json'), name]
    return 'env PYTHONPATH=' + shlex.quote(applet_host.sdk_dir()) + ' ' + ' '.join(shlex.quote(a) for a in argv)


def _views(f):
    views = getattr(f, 'applet_views', None)
    if views is None:
        views = f.applet_views = {}
    return views


def _applet_of(mgr, sid):
    st = mgr.get_pane_status(sid)
    return ((st.metadata or {}).get('applet') or '') if st is not None else ''


def _check(f, mgr, views):
    """Forget tabs that are no longer shown (they are opened again) and close applet tabs nobody owns (a previous
    daemon's)."""
    shown = set(mgr.tab_panes())
    for name, v in list(views.items()):
        if v['pane'] not in shown:
            views.pop(name)
            f.log('applet.view_gone', applet=name, pane=v['pane'])
    mine = {v['pane'] for v in views.values()}
    for sid in shown - mine:
        if _applet_of(mgr, sid):
            mgr.kill_pane(sid)


def tick(f, now=None):
    """``Fleet.tick`` part after ``applets``: one tab per applet row in the daemon area, titled with its state."""
    runners = getattr(f, 'applet_runners', None) or {}
    if not runners and not getattr(f, 'applet_views', None):
        return
    mgr = getattr(f, 'mgr', None)
    if mgr is None or getattr(f, 'stopping', False) or getattr(f, 'explicit_stop', False):
        return
    if getattr(f, 'tab_specs', None) is not None and not getattr(f, 'tabs_opened', True):
        return  # tabs.open_all closes a previous daemon's tabs first (it runs earlier in the same tick)
    from fleetmux.fleet import SERVICE
    history = f.r.hget(SERVICE, 'history_pane')  # empty: no history pane to dock into (WezTerm), as tabs.start_one
    now = time.time() if now is None else now
    views = _views(f)
    if now >= getattr(f, 'applet_view_check', 0):
        f.applet_view_check = now + CHECK_S
        _check(f, mgr, views)
    for name in [n for n in views if n not in runners]:
        v = views.pop(name)
        mgr.kill_pane(v['pane'])
        f.log('applet.view_closed', applet=name, pane=v['pane'])
    retry = getattr(f, 'applet_view_retry', None)
    if retry is None:
        retry = f.applet_view_retry = {}
    prev = None
    for name in runners:  # roster order
        label = title(name, f.r.hget(applet_host.key(name), 'state'))
        v = views.get(name)
        if v is None:
            if now < retry.get(name, 0):
                continue
            pane = open_one(f, mgr, name, label, history, prev)
            if not pane:
                retry[name] = now + RETRY_S
                continue
            retry.pop(name, None)
            v = views[name] = {'pane': pane, 'title': label}
        elif v['title'] != label:
            mgr.rename(v['pane'], label)
            v['title'] = label
        prev = v['pane']


def open_one(f, mgr, name, label, history, after):
    """Open the viewer tab of ``name`` and dock it in the daemon area (after ``after``, else after control); without
    a history pane the tab stays where the backend put it (``tabs.start_one`` does the same)."""
    res = mgr.spawn_window(label, command(f.cfg, name), metadata={'type': 'tab', 'tab': name, 'applet': name},
                           start_directory=str(f.cfg['run']))
    if not res.success:
        f.log('applet.view_failed', level='error', applet=name, error=res.error or 'no surface')
        return None
    if history and not mgr.dock_tab(res.pane_id, history, after=after):
        f.log('applet.view_dock_failed', level='warn', applet=name, pane=res.pane_id)
    f.log('applet.view_opened', applet=name, pane=res.pane_id)
    return res.pane_id


# ------------------------------------------------------------------ the viewer program
class Screen:
    """A fixed header above a scrolling region on the terminal's alternate screen; plain lines when ``out`` is not a
    terminal. On the main screen a clear (``2J``) or a row shrink pushes the header into the scrollback, one copy per
    resize; the alternate screen has no scrollback."""

    def __init__(self, out):
        self.out = out
        self.tty = out.isatty()
        self.shown = None
        self.resized = True
        self.alt = False

    def size(self):
        size = shutil.get_terminal_size((100, 30))
        return max(size.lines, HEADER_ROWS + 2), max(size.columns, 20)

    def header(self, lines):
        if not self.tty:
            if lines != self.shown:
                self.out.write(('\n'.join(lines) + '\n').encode())
                self.shown = lines
            return
        rows, cols = self.size()
        text = b''
        if not self.alt:  # entering the alternate screen clears it
            text, self.alt = b'\x1b[?1049h', True
        if self.resized:  # (re)set the scrolling region below the header, cursor at its bottom; no clear
            text += b'\x1b[%d;%dr\x1b[%d;1H' % (HEADER_ROWS + 1, rows, rows)
            self.resized, self.shown = False, None
        if lines != self.shown:
            body = b'\x1b7'
            for i, line in enumerate(lines + ['─' * cols]):
                body += b'\x1b[%d;1H\x1b[2K' % (i + 1) + line[:cols].encode()
            text += body + b'\x1b8'
            self.shown = lines
        if text:
            self.out.write(text)

    def log(self, data):
        if data:
            self.out.write(data)

    def flush(self):
        self.out.flush()

    def reset(self):
        if self.tty:
            self.out.write(b'\x1b[r' + (b'\x1b[?1049l' if self.alt else b''))
            self.out.flush()


def _quiet(fd):
    """No echo, no line editing, no Ctrl-C on the tab's terminal (keys are dropped); returns the restore callable."""
    try:
        import termios
        old = termios.tcgetattr(fd)
        new = termios.tcgetattr(fd)
        new[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)
        termios.tcsetattr(fd, termios.TCSANOW, new)
        return lambda: termios.tcsetattr(fd, termios.TCSANOW, old)
    except Exception:
        return lambda: None


def drain(fd, wait):
    """Wait up to ``wait`` seconds; whatever was typed into the tab is read and dropped. Returns False once the
    input is closed (then the caller only sleeps)."""
    try:
        ready, _, _ = select.select([fd], [], [], wait)
    except (OSError, ValueError):
        time.sleep(wait)
        return False
    if ready:
        try:
            return bool(os.read(fd, 4096))
        except OSError:
            return False
    return True


def step(name, connect, tail, screen, now=None):
    """One redraw: header from Redis (a failed read shows the error and reconnects next time), new log bytes."""
    now = time.time() if now is None else now
    error, h, leases, clock = '', {}, 0, None
    try:
        r = connect()
        h, leases = read_state(r, name)
        clock = awake.of(r)
    except Exception as exc:
        error = str(exc)
    screen.header(header(name, h, leases, now, error, clock))
    screen.log(tail.read())
    screen.flush()
    return not error


def main(argv=None):
    p = argparse.ArgumentParser(prog='fleetmux.applet_view', description='Read-only view of one applet.')
    p.add_argument('config', help="the host fleet's config.json")
    p.add_argument('name', help='applet name')
    p.add_argument('--interval', type=float, default=REFRESH_S)
    args = p.parse_args(argv)
    from fleetmux import fleet
    cfg = fleet.load(args.config)
    cfg.setdefault('run', str(Path(args.config).resolve().parent))  # a run's config.json sits in the run
    client = [None]

    def connect():
        if client[0] is None:
            client[0] = fleet.connect(cfg)
        return client[0]

    screen = Screen(sys.stdout.buffer)
    tail = Tail(applet_host.run_dir(cfg, args.name) / 'applet.log')
    signal.signal(signal.SIGWINCH, lambda *_: setattr(screen, 'resized', True))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    fd = sys.stdin.fileno() if sys.stdin is not None else -1
    restore = _quiet(fd) if fd >= 0 and os.isatty(fd) else (lambda: None)
    reading = fd >= 0
    try:
        while True:
            if not step(args.name, connect, tail, screen):
                client[0] = None
            if reading:
                reading = drain(fd, args.interval)
            else:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        restore()
        screen.reset()


if __name__ == '__main__':
    main()
