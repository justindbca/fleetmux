"""Shared helpers of the M4 red tests (design-1/m4-design.md v0.7 §8.2 / §8.2b / §8.3).

Rules these helpers enforce (supervisor [M11]; design §8.4 item 7-8; py-1 m4-tmux-liveness.md §2-§3):

- Every test gets a **new** evidence root ``~/.m4e/fm4-py/<id>/`` (``$FM4_EVIDENCE_ROOT``, else ``$M4E_ROOT`` overrides
  the base). An
  existing root fails the test. Nothing here ever deletes it: no ``tmp_path`` for sockets / state, no
  ``shutil.rmtree``, no ``pkill`` / ``killall`` / ``killpg``. The root and the test id go to stdout and
  ``EVIDENCE.txt``.
- A process is only ever signalled when it is in this test's ledger (``pids.json``) **and** its ``lstart`` and full
  ``args`` still equal what was recorded. Teardown first runs the product's own stop paths; the identity-checked
  fallback runs after that, and when it actually signalled anything the teardown fails (the product stop did not
  clean up, and that must be visible).
- fleetmux is either ``tests/fake_fleetmux.py`` (``fake``) or this worktree's own binary (``real``:
  ``$FLEETMUX_REAL_BIN``, default ``rust/target/debug/fleetmux``). A missing real binary is a failure,
  never a skip (design §8.4 item 6).
- An M4 API that does not exist yet fails the test body through :func:`need` (a clear behaviour failure); module
  import and collection never reference anything M4 adds.
"""
import json
import os
import re
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fleetmux.backends import fleetmux as fm  # noqa: E402

FAKE = ROOT / 'tests' / 'fake_fleetmux.py'
REAL = Path(os.environ.get('FLEETMUX_REAL_BIN') or ROOT / 'rust/target/debug/fleetmux')
# bound before any HOME change. FM4_EVIDENCE_ROOT wins; else M4E_ROOT, the kept root the m4 gate exports for the
# integration slot (scripts/fleetmux_gate.sh: $HOME/.m4e/fm4-integ); else py-1's own slot root.
BASE = Path(os.environ.get('FM4_EVIDENCE_ROOT') or os.environ.get('M4E_ROOT') or Path.home() / '.m4e' / 'fm4-py')
ROOT_SESSION = 'fleetmux'
PAGE_ID = re.compile(r'[A-Za-z0-9._-]{1,64}\Z')  # design §4.1
SOCK_LIMIT = 103


# ---------------------------------------------------------------- evidence root

def new_root(tag: str, request=None, kind: str = '') -> Path:
    """A fresh ``<BASE>/<tag>-<hex>`` (short: sockets live under it). Already there -> the test fails."""
    BASE.mkdir(parents=True, exist_ok=True)
    root = BASE / ('%s-%s' % (tag, secrets.token_hex(4)))
    try:
        root.mkdir()
    except FileExistsError:
        pytest.fail('evidence root already exists (never reused): %s' % root)
    sha = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
    node = getattr(getattr(request, 'node', None), 'nodeid', '') if request is not None else ''
    lines = ['root: %s' % root, 'test: %s' % node, 'impl: %s' % kind, 'worktree: %s' % ROOT, 'sha: %s' % sha,
             'real binary: %s' % REAL, 'fake server: %s' % FAKE, 'started: %s' % time.strftime('%Y-%m-%d %H:%M:%S')]
    (root / 'EVIDENCE.txt').write_text('\n'.join(lines) + '\n')
    print('M4 evidence root (kept): %s  [%s]' % (root, node))
    return root


def note(root: Path, line: str) -> None:
    with (root / 'EVIDENCE.txt').open('a') as fh:
        fh.write(line.rstrip('\n') + '\n')


# ---------------------------------------------------------------- pid ledger

def identity(pid: int):
    """(lstart, args) of a live, non-zombie pid; None when it is gone or unreadable."""
    try:
        stat = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True, timeout=10)
        lstart = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart='], capture_output=True, text=True, timeout=10)
        args = subprocess.run(['ps', '-ww', '-p', str(pid), '-o', 'args='], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if not stat.stdout.strip() or stat.stdout.strip().startswith('Z'):
        return None
    if not lstart.stdout.strip() or not args.stdout.strip():
        return None
    return lstart.stdout.strip(), args.stdout.strip()


class Ledger:
    """The processes this test started and checked (``pids.json``); the only ones teardown may signal."""

    def __init__(self, root: Path):
        self.root = root
        self.rows = []

    def _save(self):
        (self.root / 'pids.json').write_text(json.dumps(self.rows, indent=2, ensure_ascii=False))

    def register(self, role: str, pid) -> bool:
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False
        if pid <= 1 or pid == os.getpid():
            return False
        ident = identity(pid)
        if ident is None:
            return False
        if any(r['pid'] == pid and r['lstart'] == ident[0] for r in self.rows):
            return True
        self.rows.append({'role': role, 'pid': pid, 'lstart': ident[0], 'args': ident[1]})
        self._save()
        return True

    def row(self, role: str):
        return next((r for r in self.rows if r['role'] == role), None)

    def alive(self, role: str) -> bool:
        """Is the registered process of ``role`` still the very same process (identity check)?"""
        r = self.row(role)
        return bool(r) and identity(r['pid']) == (r['lstart'], r['args'])

    def fallback(self) -> list:
        """SIGTERM / SIGKILL only what still matches its recorded identity; returns what was signalled."""
        signalled, log = [], []
        for r in self.rows:
            want = (r['lstart'], r['args'])
            now = identity(r['pid'])
            if now is None:
                log.append(dict(r, action='gone'))
                continue
            if now != want:
                log.append(dict(r, action='identity mismatch, not signalled', now=list(now)))
                continue
            try:
                os.kill(r['pid'], signal.SIGTERM)
            except OSError as exc:
                log.append(dict(r, action='SIGTERM failed: %s' % exc))
                continue
            action = 'SIGTERM'
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and identity(r['pid']) == want:
                time.sleep(0.1)
            if identity(r['pid']) == want:
                try:
                    os.kill(r['pid'], signal.SIGKILL)
                    action = 'SIGTERM+SIGKILL'
                except OSError as exc:
                    action = 'SIGTERM; SIGKILL failed: %s' % exc
            signalled.append(dict(r, action=action))
            log.append(dict(r, action=action))
        (self.root / 'cleanup.json').write_text(json.dumps({'fallback': log}, indent=2, ensure_ascii=False))
        return signalled


def fail_if_signalled(root: Path, signalled: list, stop_log: list) -> None:
    """Teardown verdict: the product stop must have left nothing for the fallback."""
    note(root, 'product stop: %s' % json.dumps(stop_log, ensure_ascii=False))
    note(root, 'fallback signalled: %s' % json.dumps(signalled, ensure_ascii=False))
    print('M4 evidence root (kept): %s; fallback signalled %d process(es)' % (root, len(signalled)))
    if signalled:
        raise AssertionError('product stop left processes the identity-checked fallback had to signal: %s (evidence %s)'
                             % (json.dumps(signalled, ensure_ascii=False), root))


# ---------------------------------------------------------------- fleetmux server under test

def install_fleetmux(monkeypatch, root: Path, kind: str) -> Path:
    """Put a ``fleetmux`` launcher for ``kind`` on PATH / in ``$FLEETMUX_BIN``; returns it."""
    if kind == 'real':
        if not (REAL.is_file() and os.access(REAL, os.X_OK)):
            pytest.fail('missing dependency: real fleetmux binary %s (cargo build -p fleetmux in this worktree, '
                        'or set FLEETMUX_REAL_BIN)' % REAL)
        target = [str(REAL)]
    else:
        target = [sys.executable, str(FAKE)]
    bindir = root / 'bin'
    bindir.mkdir(exist_ok=True)
    launcher = bindir / 'fleetmux'
    launcher.write_text('#!/bin/sh\nexec ' + shlex.join(target) + ' "$@"\n')
    launcher.chmod(0o755)
    monkeypatch.setenv(fm.BIN_ENV, str(launcher))
    monkeypatch.setenv('PATH', str(bindir) + os.pathsep + os.environ.get('PATH', ''))
    note(root, 'fleetmux launcher: %s -> %s' % (launcher, ' '.join(target)))
    return launcher


def token(session: str) -> str:
    return re.sub(r'[^A-Za-z0-9_-]', '_', session, flags=re.ASCII)[:40] or 'fleet'


def runtime(state: Path, session: str = ROOT_SESSION) -> Path:
    return Path(state).resolve() / 'fleetmux' / token(session)


def ctl_sock(state: Path, session: str = ROOT_SESSION) -> Path:
    sock = runtime(state, session) / 'ctl.sock'
    assert len(os.fsencode(str(sock))) <= SOCK_LIMIT, sock
    return sock


def rpc(sock: Path, *requests, timeout: float = 10) -> list:
    """Send ``requests`` in order on ONE control connection; one reply per request (a page binds a connection)."""
    replies = []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(timeout)
        conn.connect(str(sock))
        stream = conn.makefile('rwb')
        for n, req in enumerate(requests, 1):
            stream.write((json.dumps(dict(req, id=n)) + '\n').encode())
            stream.flush()
            line = stream.readline()
            if not line:
                replies.append({'ok': False, 'code': 'closed', 'error': 'connection closed'})
                break
            replies.append(json.loads(line))
    return replies


def ctl(sock: Path, cmd: str, **args) -> dict:
    """One request on a fresh root (unbound) connection, bypassing every manager."""
    return rpc(sock, {'cmd': cmd, **args})[0]


def on_page(sock: Path, page: str, cmd: str, **args):
    """``hello{page}`` then ``cmd`` on the same connection: (hello reply, command reply)."""
    hello, *rest = rpc(sock, {'cmd': 'hello', 'protocol': fm.PROTOCOL, 'page': page}, {'cmd': cmd, **args})
    return hello, (rest[0] if rest else {'ok': False, 'code': 'closed'})


def server_up(sock: Path) -> bool:
    try:
        return ctl(sock, 'hello', protocol=fm.PROTOCOL).get('ok') is True
    except (OSError, ValueError):
        return False


def all_panes(sock: Path) -> list:
    reply = ctl(sock, 'pane.list', all=True)
    assert reply.get('ok'), reply
    return reply.get('panes') or []


def record(sock: Path, pane) -> dict:
    """``pane.get`` record (top-level keys), {} when the server does not know the pane."""
    reply = ctl(sock, 'pane.get', pane=str(pane))
    return {k: v for k, v in reply.items() if k not in ('id', 'ok')} if reply.get('ok') else {}


def page_of(sock: Path, pane):
    """The ``page`` key of the pane's record: '' = root page; None when the server gives none (an M3 server)."""
    return record(sock, pane).get('page')


def pages(sock: Path) -> dict:
    """{page id: page.list row}; fails the test when the server has no ``page.list`` (M3)."""
    reply = ctl(sock, 'page.list')
    assert reply.get('ok'), 'page.list failed (no M4 pages on this server): %r' % reply
    rows = reply.get('pages')
    assert isinstance(rows, list), reply
    return {row.get('page'): row for row in rows}


def current_page(sock: Path):
    return ctl(sock, 'hello', protocol=fm.PROTOCOL).get('current')


def wait_for(check, timeout: float = 10.0, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if check():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(step)


def need(obj, name: str):
    """The M4 method ``name`` of ``obj``; a clear test failure (not an error) while it does not exist."""
    attr = getattr(obj, name, None)
    if not callable(attr):
        pytest.fail('M4 API missing: %s.%s (design-1/m4-design.md §6.1)' % (type(obj).__name__, name))
    return attr


class Recorder:
    """Wraps a manager's ``_exchange`` to log every control request it sends (what crossed the socket)."""

    def __init__(self, mgr):
        self.requests = []
        real = mgr._exchange

        def exchange(stream, request):
            self.requests.append(dict(request))
            return real(stream, request)
        mgr._exchange = exchange


# ---------------------------------------------------------------- managers and product stop

class Servers:
    """The managers one test built on one state dir, plus the ledger and the product stop for them."""

    def __init__(self, root: Path, kind: str, state: Path):
        self.root, self.kind, self.state = root, kind, state
        self.ledger = Ledger(root)
        self.mgrs = []

    @property
    def sock(self) -> Path:
        return ctl_sock(self.state)

    def track(self, mgr):
        self.mgrs.append(mgr)
        return mgr

    def root_mgr(self, session: str = ROOT_SESSION):
        return self.track(fm.FleetmuxSessionManager(session, state_dir=self.state))

    def child_mgr(self, page: str, root_session: str = ROOT_SESSION):
        """``FleetmuxSessionManager('fleetmux:<page>')`` bound to ``page`` of the root's server (design §6.1 S0)."""
        mgr = self.track(fm.FleetmuxSessionManager(root_session + ':' + page, state_dir=self.state))
        need(mgr, 'bind_page')(root_session, page)
        return mgr

    def register_servers(self):
        """Every fleetmux server under this state dir, and the processes of every pane it lists."""
        base = Path(self.state) / 'fleetmux'
        for sj in sorted(base.glob('*/server.json')) if base.is_dir() else []:
            try:
                pid = json.loads(sj.read_text()).get('pid')
            except (OSError, ValueError):
                continue
            self.ledger.register('fleetmux-server:' + sj.parent.name, pid)
            try:
                rows = ctl(sj.parent / 'ctl.sock', 'pane.list', all=True).get('panes') or []
            except (OSError, ValueError):
                rows = []
            for row in rows:
                self.ledger.register('pane:' + str(row.get('pane')), row.get('pid'))

    def stop(self, extra=None) -> list:
        """Product stop: ``kill_pane`` every registered pane of every manager (children first), ``extra`` (e.g.
        ``subfleet.stop_tmux``), then ``end_server()`` of each manager. Returns the step log."""
        log = []
        for mgr in reversed(self.mgrs):
            for sid in list(getattr(mgr, '_surfaces', lambda: {})()):
                try:
                    log.append(('kill_pane', mgr.session_name, sid, bool(mgr.kill_pane(sid))))
                except Exception as exc:  # noqa: BLE001 - teardown must go on
                    log.append(('kill_pane', mgr.session_name, sid, 'error: %s' % exc))
        for step in extra or ():
            try:
                log.append((getattr(step, '__name__', 'extra'), step()))
            except Exception as exc:  # noqa: BLE001
                log.append((getattr(step, '__name__', 'extra'), 'error: %s' % exc))
        for mgr in reversed(self.mgrs):
            try:
                log.append(('end_server', mgr.session_name, bool(mgr.end_server()), mgr.last_error))
            except Exception as exc:  # noqa: BLE001
                log.append(('end_server', mgr.session_name, 'error: %s' % exc))
        root = fm.FleetmuxSessionManager(ROOT_SESSION, state_dir=self.state)
        if root.ctl_socket.exists():  # a server nobody here owns any more: the root's own stop
            try:
                log.append(('end_server', 'root-final', bool(root.end_server()), root.last_error))
            except Exception as exc:  # noqa: BLE001
                log.append(('end_server', 'root-final', 'error: %s' % exc))
        return [list(map(str, row)) for row in log]

    def close(self, extra=None):
        self.register_servers()
        stop_log = self.stop(extra)
        wait_for(lambda: not any(self.ledger.alive(r['role']) for r in self.ledger.rows), timeout=5)
        fail_if_signalled(self.root, self.ledger.fallback(), stop_log)


def make_run(root: Path, name: str) -> tuple:
    """A run dir with a ``history.html``; (run, its file:// URL)."""
    run = root / name
    run.mkdir(parents=True, exist_ok=True)
    (run / 'history.html').write_text('<html><body>%s</body></html>' % name)
    return run, (run / 'history.html').as_uri()


def sub_run(parent_run: Path, dirname: str, task_id: str, state: str = 'returned', attempt: int = 1, **extra) -> Path:
    """``<parent>/sub/<dirname>`` with its ``subfleet.json`` record and a ``history.html`` (no daemon, no Redis)."""
    run = Path(parent_run) / 'sub' / dirname
    run.mkdir(parents=True, exist_ok=True)
    (run / 'history.html').write_text('<html><body>%s</body></html>' % dirname)
    rec = dict({'task_id': task_id, 'slot': 'worker-1', 'run': str(run), 'state': state, 'attempt': attempt,
                'workers': 1, 'title': task_id}, **extra)
    (run / 'subfleet.json').write_text(json.dumps(rec))
    (run / 'config.json').write_text(json.dumps({'run': str(run), 'task_id': task_id}))
    return run
