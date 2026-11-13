"""fleetmux backend (fleetmux design §4): the cmux interface on top of one ``fleetmux server`` per run.

M0 (brief-m03 B9): every method of the phase-0 skeleton (plan.md §3.5) is implemented against
rust/PROTOCOL.md v1; tests/test_backend_interface.py holds this class to ``CmuxSessionManager``'s
signatures and tests/test_fleetmux_backend.py drives it against the fake and the real server.

How it works (design §2)
------------------------
* One private unix socket ``<state dir>/fleetmux/<token>/ctl.sock`` (dir 0700, socket 0600; ``<token>`` as
  :meth:`FleetmuxSessionManager._token`, the same rule as the Rust server's), derived from
  ``(state_dir, session)`` alone, so every fresh manager (start / daemon / closer / resume) reaches the
  same server. Every call connects and says ``hello``; no "server is up" conclusion is cached.
* Control connection: one UTF-8 JSON object per line, ``{"id": n, "cmd": ..., ...}`` ->
  ``{"id": n, "ok": true, ...}`` or ``{"id": n, "ok": false, "code": ..., "error": ...}`` (PROTOCOL.md §1).
  ``id`` is only ever the request number; a pane is always named ``pane`` -- in arguments, results, the
  pane record and panes.json (ruling R1).
* Only ``get_or_create_session()`` and the first ``spawn_*`` start the server (cmux contract): take
  ``start.lock``, ``hello`` again, run ``fleetmux server --runtime D --session S --daemonize [--theme FILE]``,
  wait for ``hello`` up to 10 s, else ``last_error`` carries the tail of ``server.log``. The runtime directory is
  created here (``prepare_runtime``), never left to the server (PROTOCOL.md §9.1 does not promise it).
  ``$FLEETMUX_THEME`` (non-empty) is passed on as ``--theme FILE``: the same environment-variable
  pattern as ``FLEETMUX_BIN``, so the palette is configurable without a fleet.yaml key or a new flag.
* Registry ``<state_dir>/<session>.fleetmux.json`` (not ``<session>.json``: the cmux / wezterm registries
  must never be mixed up). Who is alive is the server's answer; who is ours is the registry's.

M4 pages (design-1/m4-design.md v0.7 §4 / §6.1): one fleet tree shares the root's server, one page per fleet.
:meth:`FleetmuxSessionManager.bind_page` binds a sub-fleet's manager to its page of the root's server: the five
runtime paths become the root's, the registry stays the child's own, every ``hello`` carries ``page`` and must be
echoed with ``pages: 1`` (else the call fails closed: an M3 server would put the child's panes in the root page),
a bound manager never starts a server, and its ``close_all`` reclaims its page instead of stopping the server.
A manager that was never bound behaves exactly as before.

``supports_browser = True`` means "has a history pane" here (a terminal tab running
``python -m fleetmux.history_view``), not an embedded browser (design §4.4).

This module never imports another backend's module at import time (backends/__init__.py lazy-import rule):
the key table is read from ``rust/protocol/keys.json``, shared with the Rust encoder tests; the tmux
helpers of the cmux backend are imported inside the tmux-view methods only (as wezterm.py does).
"""
from __future__ import annotations

import base64
import ctypes
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from fleetmux import paths
from fleetmux.backends.base import PaneStatus, SpawnResult

#: Control-protocol version this backend speaks; ``hello`` must report exactly this (PROTOCOL.md §2).
PROTOCOL = 1
#: Points at the ``fleetmux`` binary (else PATH); tests point it at tests/fake_fleetmux.py.
BIN_ENV = 'FLEETMUX_BIN'
#: A palette file handed to ``fleetmux server --theme FILE`` (PROTOCOL.md §9.1); unset = the cmux default.
THEME_ENV = 'FLEETMUX_THEME'
#: The dependency floor, same as deps.yaml's ``fleetmux`` item.
MIN_VERSION = (0, 2, 0)
#: Pane type of a sidebar bash (S1c, plan §14.3): the server spawns it as a tab of the root page's ``workers``
#: group; the root manager registers it (:meth:`FleetmuxSessionManager.adopt_shells`, user 2026-11-09) but it is no
#: agent: neither a live fleet session nor a plain close target (only an idle one goes with the root fleet).
SHELL_TYPE = 'shell'
#: sockaddr_un.sun_path includes a terminating NUL: 104 bytes on macOS, 108 on Linux (design §2.2).
SOCKET_PATH_LIMIT = 103 if sys.platform == 'darwin' else 107

#: ``fleetmux --version`` prints exactly this one line (ruling R9).
VERSION_RE = re.compile(r'fleetmux (\d+)\.(\d+)\.(\d+) \(protocol (\d+)\)')
#: PROTOCOL.md §1.4: epoch = 4 base-36 chars, new per server process; pane id = ``<epoch>-<seq>``.
EPOCH_RE = re.compile(r'[0-9a-z]{4}')
PANE_ID_RE = re.compile(r'[0-9a-z]{4}-[1-9][0-9]*')

#: M4 page commands (design §4.2); kept out of :data:`COMMANDS`, whose 22 are the M3 set pinned by its tests.
PAGE_COMMANDS = ('page.open', 'page.close', 'page.select', 'page.list')
#: A page id (design §4.1): the child config's ``fleetmux_page`` = its task id; '' is the root page.
PAGE_ID_RE = re.compile(r'[A-Za-z0-9._-]{1,64}')
#: The registry ``type`` of the read-only record tab a parent keeps in a reclaimed child's page (§5.4).
ARCHIVE_TYPE = 'archive'
#: S1 (design §4.2 compatibility rule): a bound call on a server that does not echo the page.
NO_PAGES_TEXT = ('fleetmux server on %s has no pages (it is older than M4 and would put this sub-fleet into the '
                 'root page): restart it with a fleetmux that answers hello with pages: 1')

#: Control commands, PROTOCOL.md §4: all 22 (R2's 16 + `server.stats`, A-R2; plan §3.1's "15" counts kv.set / kv.get as
#: one row; + the five M2.5 ``web.*`` commands of §4.18-§4.22, ``web.eval`` reserved).
COMMANDS = (
    'hello', 'kv.set', 'kv.get',
    'pane.spawn', 'pane.list', 'pane.get', 'pane.write', 'pane.key', 'pane.read',
    'pane.rename', 'pane.focus', 'pane.move', 'pane.kill',
    'layout.get', 'layout.set', 'server.shutdown', 'server.stats',
    'web.open', 'web.goto', 'web.url', 'web.reload', 'web.eval',
)
#: The fixed error-code set, PROTOCOL.md §5. ``unknown_type`` is for a bad ``group`` / ``mode`` only; an
#: unknown pane *type* is never an error, it lands last in ``workers`` (ruling R5).
ERROR_CODES = (
    'bad_request', 'unknown_cmd', 'no_such_pane', 'unknown_key', 'unknown_type', 'not_empty',
    'spawn_failed', 'kill_timeout', 'write_failed', 'protocol_version', 'internal',
    'no_window', 'web_timeout', 'not_implemented',          # M2.5 (web window)
)
#: ``pane.write`` modes: ``bracketed`` always wraps, ``raw`` never, ``paste`` follows the inner 2004 bit.
WRITE_MODES = ('raw', 'bracketed', 'paste')
#: The three tab groups (PROTOCOL.md §1.5) and the pane types with a placement rule (design §3.4).
GROUPS = ('supervisor', 'lower', 'workers')
PANE_TYPES = ('supervisor', 'advisor', 'worker', 'daemon', 'tab', 'history', 'placeholder')
#: ``pane.kill`` (ruling R4): default ``timeout_ms``; a step is sent only while the group lives and its time is
#: <= ``timeout_ms`` (so below 5000 there is never a SIGKILL); then only a reap grace, no further signal.
KILL_TIMEOUT_MS = 5000
KILL_SCHEDULE_MS = ((0, signal.SIGHUP), (2000, signal.SIGTERM), (5000, signal.SIGKILL))
KILL_REAP_GRACE_MS = 500
#: ``server.shutdown`` removes the live-server files and keeps the log, the layout preference and the lock
#: target (ruling R7).
REMOVED_ON_STOP = ('ctl.sock', 'server.json', 'panes.json')
KEPT_ON_STOP = ('server.log', 'layout.json', 'start.lock')

#: Packaged copy of rust/protocol/keys.json (tests/test_packaged_keys.py keeps the two identical), so an installed
#: wheel finds it without the repository.
KEYS_FILE = Path(__file__).resolve().with_name('keys.json')
#: Key names ``send_key`` knows -> the bytes they stand for (rust/protocol/keys.json, plan §3.0).
KEYS: Dict[str, str] = json.loads(KEYS_FILE.read_text(encoding='utf-8'))['keys']

#: How long a fresh server gets to answer ``hello`` (design §2.3), and the wait for ``start.lock``.
START_WAIT_S = 10.0
LOCK_WAIT_S = 30.0
#: Socket timeout of one control call; ``pane.kill`` / ``server.shutdown`` get their deadline on top.
CALL_TIMEOUT_S = 30.0
#: Lines of ``server.log`` copied into ``last_error`` when a start fails.
LOG_TAIL_LINES = 20
#: The package root, prepended to the history pane's PYTHONPATH so ``-m fleetmux.history_view`` resolves.
PACKAGE_ROOT = Path(__file__).resolve().parents[2]


class FleetmuxError(RuntimeError):
    """A server / socket / protocol problem fleet must see (never silently degraded). ``code`` is one of
    :data:`ERROR_CODES` when the server answered ``ok: false``."""

    def __init__(self, message: str = '', code: str = 'internal'):
        super().__init__(message)
        self.code = code


class ServerDown(FleetmuxError):
    """Nothing answers on the run's socket (no server, or it died mid-call)."""


def fleetmux_bin() -> Optional[str]:
    """``$FLEETMUX_BIN`` or ``fleetmux`` on PATH."""
    return os.environ.get(BIN_ENV) or shutil.which('fleetmux')


def _version_of(binary: str) -> Tuple[Optional[Tuple[int, int, int, int]], str]:
    """``fleetmux 0.1.0 (protocol 1)`` -> ``((0, 1, 0, 1), line)``; ``(None, why)`` when it does not answer so."""
    try:
        done = subprocess.run([binary, '--version'], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    line = (done.stdout or '').strip()
    found = VERSION_RE.fullmatch(line)
    if done.returncode != 0 or not found:
        return None, (line or (done.stderr or '').strip() or 'exit %d' % done.returncode)[:200]
    return tuple(int(x) for x in found.groups()), line


def availability_problem() -> Optional[str]:
    """Why this machine cannot run the fleetmux backend (None: it can) — design §4.3 ``available()``:
    the binary is found, ``--version`` is at least :data:`MIN_VERSION` and speaks :data:`PROTOCOL`."""
    binary = fleetmux_bin()
    if not binary:
        return ('fleetmux backend not available: no fleetmux binary ($%s is unset and `fleetmux` is not on PATH; '
                'scripts/install_fleetmux.sh installs it)' % BIN_ENV)
    have, line = _version_of(binary)
    if have is None:
        return ('fleetmux backend not available: `%s --version` did not print "fleetmux <version> (protocol <n>)" '
                '(%s)' % (binary, line))
    want = '.'.join(map(str, MIN_VERSION))
    if have[:3] < MIN_VERSION:
        return 'fleetmux backend not available: need fleetmux >= %s, %s is %s' % (
            want, binary, '.'.join(map(str, have[:3])))
    if have[3] != PROTOCOL:
        return 'fleetmux backend not available: fleetmux version mismatch: need protocol %d, %s speaks protocol %d' % (
            PROTOCOL, binary, have[3])
    return None


def _start_time(pid: int) -> Optional[int]:
    """The OS-recorded start of ``pid`` as integer Unix seconds (PROTOCOL.md §8.6), None when unreadable."""
    try:
        if sys.platform == 'darwin':
            libc = ctypes.CDLL(None, use_errno=True)
            mib = (ctypes.c_int * 4)(1, 14, 1, int(pid))     # CTL_KERN, KERN_PROC, KERN_PROC_PID
            buf, size = ctypes.create_string_buffer(1024), ctypes.c_size_t(1024)
            if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, ctypes.c_size_t(0)) or size.value < 8:
                return None
            return struct.unpack_from('q', buf.raw, 0)[0]   # kinfo_proc.kp_proc.p_starttime.tv_sec
        ticks = int(Path('/proc/%d/stat' % pid).read_text().rsplit(')', 1)[1].split()[19])
        btime = next(int(line.split()[1]) for line in Path('/proc/stat').read_text().splitlines()
                     if line.startswith('btime '))
        return btime + ticks // os.sysconf('SC_CLK_TCK')
    except (OSError, ValueError, IndexError, StopIteration, AttributeError):
        return None


def _cmdline(pid: int) -> Optional[List[str]]:
    """The process's argv (Linux: exact; macOS: ``ps -ww`` split on blanks), None when unreadable."""
    try:
        if sys.platform.startswith('linux'):
            raw = Path('/proc/%d/cmdline' % pid).read_bytes()
            return [a.decode('utf-8', 'replace') for a in raw.split(b'\0') if a] or None
        done = subprocess.run(['ps', '-ww', '-p', str(pid), '-o', 'command='], capture_output=True, text=True,
                              timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.split() if done.returncode == 0 and done.stdout.strip() else None


def _pid_alive(pid: int) -> bool:
    """Running and not a zombie."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    try:
        done = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True, timeout=10)
        return not done.stdout.strip().startswith('Z')
    except (OSError, subprocess.SubprocessError):
        return True


def _is_shell_row(row) -> bool:
    """A ``pane.list`` row of a bash page (S1c, plan §14.3); anything that is not a dict is not one."""
    return isinstance(row, dict) and row.get('type') == SHELL_TYPE


def _shell_idle(row) -> bool:
    """The bash page's shell sits at its prompt: the PTY's foreground group (``fg_pgid``, PROTOCOL.md §4.0) is the
    shell's own ``pgid``. No ``fg_pgid`` (an older server) or ``null`` (the PTY reports no group) is never idle."""
    fg, pgid = row.get('fg_pgid'), row.get('pgid')
    return (isinstance(fg, int) and not isinstance(fg, bool) and isinstance(pgid, int)
            and not isinstance(pgid, bool) and fg == pgid)


def _wait(check, timeout: float = 5.0, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if check():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(step)


def _exe(comm: str) -> str:
    """Executable basename without a login shell's ``-`` or ``.exe`` (the native Claude installer runs
    ``.../bin/claude.exe``)."""
    name = os.path.basename(comm.strip()).lstrip('-')
    return name[:-4] if name.endswith('.exe') else name


class FleetmuxSessionManager:
    """Every agent of one fleet inside one private ``fleetmux server``: three fixed tab groups."""

    # Terminal backend capabilities (fleetmux.backends.base.Capabilities, design §4.4):
    # browser = "has a history pane" (history_view tab), nested_tabs = tabs inside the three groups.
    supports_browser = True
    nested_tabs = True

    # Instance fields the constructor sets:
    session_name: str        # default 'fleetmux'
    agents_title: str        # default 'Fleet agents'
    control_title: str       # default 'Fleet daemon'
    state_dir: Path          # resolved; default $FLEETMUX_STATE or paths.state_dir()
    last_error: str          # '' until a start / spawn fails; then the reason (server.log tail)

    @classmethod
    def available(cls) -> bool:
        return availability_problem() is None

    def __init__(self, session_name: str = 'fleetmux', agents_title: str = 'Fleet agents',
                 control_title: str = 'Fleet daemon', state_dir: Optional[Path] = None):
        """Keeps the four arguments, resolves ``state_dir`` (default ``$FLEETMUX_STATE`` or
        ``paths.state_dir()``, as the other backends) and sets ``last_error = ''``. Nothing is
        started or created here: the runtime appears on ``prepare_runtime()``."""
        self.session_name = session_name
        self.agents_title = agents_title
        self.control_title = control_title
        base = Path(state_dir) if state_dir is not None else paths.state_dir()
        self.state_dir = base.expanduser().resolve()
        self.last_error = ''
        self._req = 0
        self.page = None                 # M4: the page this manager is bound to (None = unbound, as before)
        self._runtime_session = None     # M4: the root's session whose server this manager uses
        self.kept_bash_pages = []        # titles of the bash pages the last close_panes kept (busy / not idle)

    def bind_page(self, root_session: str, page: str) -> None:
        """M4 S0: use ``root_session``'s server (runtime paths) and act on ``page`` there; the registry stays
        ``<state_dir>/<own session>.fleetmux.json``. Not in ``METHODS`` (backend-specific, like ``kv_get``)."""
        if not isinstance(page, str) or not PAGE_ID_RE.fullmatch(page):
            raise FleetmuxError('bad page id %r (want [A-Za-z0-9._-]{1,64})' % (page,), code='bad_request')
        self.page = page
        self._runtime_session = str(root_session)

    def _runtime_token(self) -> str:
        """The token of the session whose server this manager talks to (the root's once bound)."""
        name = self._runtime_session if self._runtime_session is not None else self.session_name
        return re.sub(r'[^A-Za-z0-9_-]', '_', name, flags=re.ASCII)[:40] or 'fleet'

    # -- registry (design §4.2: fleet reads these private members directly) ---------------------
    @property
    def meta_path(self) -> Path:
        """``<state_dir>/<session>.fleetmux.json``."""
        return self.state_dir / (self.session_name + '.fleetmux.json')

    def _meta(self) -> Dict:
        try:
            meta = json.loads(self.meta_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            meta = None
        if not isinstance(meta, dict):
            meta = {'workspace': self.workspace_id(), 'control_workspace': None, 'surfaces': {}}
        if not isinstance(meta.get('surfaces'), dict):
            meta['surfaces'] = {}
        return meta

    def _save_meta(self, meta: Dict) -> None:
        self.meta_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.meta_path.with_name(self.meta_path.name + '.tmp')
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(self.meta_path)

    def _tag(self, sid: str, **info: str) -> None:
        meta = self._meta()
        entry = meta['surfaces'].setdefault(str(sid), {})
        entry.update({k: v for k, v in info.items() if isinstance(v, str)})
        meta.setdefault('workspace', self.workspace_id())
        self._save_meta(meta)

    def _forget(self, sid: str) -> None:
        meta = self._meta()
        if meta['surfaces'].pop(str(sid), None) is not None:
            self._save_meta(meta)

    def _surfaces(self) -> Dict[str, Dict]:
        return self._meta()['surfaces']

    # -- runtime paths (design §2.2) ------------------------------------------------------------
    @property
    def runtime_root(self) -> Path:
        """``<state_dir>/fleetmux/<token>/`` (bound: the root session's token, design §6.1 S0)."""
        return self.state_dir / 'fleetmux' / self._runtime_token()

    @property
    def ctl_socket(self) -> Path:
        return self.runtime_root / 'ctl.sock'

    @property
    def server_json(self) -> Path:
        return self.runtime_root / 'server.json'

    @property
    def start_lock(self) -> Path:
        return self.runtime_root / 'start.lock'

    @property
    def panes_json(self) -> Path:
        return self.runtime_root / 'panes.json'

    @property
    def server_log(self) -> Path:
        return self.runtime_root / 'server.log'

    def _token(self) -> str:
        """The runtime dir name, identical to the Rust ``paths::token`` (cross-language path ruling):
        every character that is not ASCII ``[A-Za-z0-9_-]`` becomes one ``_`` (so ``é``, ``工`` or a
        full-width ``１`` are replaced -- never ``str.isalnum()``, which accepts non-ASCII letters and
        digits), then cut to 40 characters; an empty session name gives ``fleet``."""
        return re.sub(r'[^A-Za-z0-9_-]', '_', self.session_name, flags=re.ASCII)[:40] or 'fleet'

    def prepare_runtime(self) -> Dict[str, str]:
        """Create the 0700 runtime dir; refuse a socket path over :data:`SOCKET_PATH_LIMIT` (no /tmp fallback)."""
        sock = str(self.ctl_socket)
        if len(os.fsencode(sock)) > SOCKET_PATH_LIMIT:
            raise FleetmuxError('fleetmux socket path is %d bytes, over the %d-byte unix socket limit: %s '
                                '(use a shorter state dir or session name; there is no /tmp fallback)'
                                % (len(os.fsencode(sock)), SOCKET_PATH_LIMIT, sock), code='bad_request')
        rt = self.runtime_root
        for folder in (rt.parent, rt):
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(folder, 0o700)  # the umask may have widened a fresh dir; an old one is narrowed
        st = rt.stat()
        if st.st_uid != os.getuid():
            raise FleetmuxError('fleetmux runtime %s is owned by uid %d, not %d' % (rt, st.st_uid, os.getuid()))
        return {'runtime': str(rt), 'socket': sock, 'server_json': str(self.server_json),
                'start_lock': str(self.start_lock), 'panes_json': str(self.panes_json),
                'server_log': str(self.server_log)}

    # -- server connection / lifecycle (design §2.3) --------------------------------------------
    def _connect(self, timeout: float = CALL_TIMEOUT_S):
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(timeout)
        try:
            conn.connect(str(self.ctl_socket))
        except OSError:
            conn.close()
            raise
        return conn

    def _exchange(self, stream, request: Dict) -> Dict:
        self._req += 1
        stream.write((json.dumps(dict(request, id=self._req), ensure_ascii=False) + '\n').encode('utf-8'))
        stream.flush()
        line = stream.readline()
        if not line:
            raise ServerDown('fleetmux server closed the connection (%s)' % request.get('cmd'))
        try:
            reply = json.loads(line)
        except ValueError as exc:
            raise FleetmuxError('fleetmux server sent a bad reply line: %s' % exc) from exc
        if not isinstance(reply, dict):
            raise FleetmuxError('fleetmux server sent a non-object reply')
        if reply.get('ok') is not True:
            raise FleetmuxError(str(reply.get('error') or 'request failed'), code=str(reply.get('code') or 'internal'))
        return reply

    def _call(self, cmd: str, **args) -> Dict:
        """One request on a fresh control connection; raises :class:`FleetmuxError` on ``ok: false``.
        A pane is addressed as ``pane=<id>`` (``id`` is the request number, ruling R1). Every connection
        starts with ``hello`` and checks the server's uid and protocol; a bound manager's ``hello`` carries its
        page and must be echoed with ``pages: 1`` and that page (S1, checked on every call, never cached)."""
        return self._request(cmd, True, **args)

    def _request(self, cmd: str, check_pages: bool, **args) -> Dict:
        timeout = CALL_TIMEOUT_S
        if cmd == 'pane.kill':
            timeout += int(args.get('timeout_ms', KILL_TIMEOUT_MS)) / 1000.0
        elif cmd == 'server.shutdown':
            timeout += 60.0
        try:
            conn = self._connect(timeout)
        except OSError as exc:
            raise ServerDown('no fleetmux server answers on %s (%s)' % (self.ctl_socket, exc)) from exc
        try:
            with conn, conn.makefile('rwb') as stream:
                greet = {'cmd': 'hello', 'protocol': PROTOCOL}
                if self.page is not None:
                    greet['page'] = self.page
                hello = self._exchange(stream, greet)
                if hello.get('uid') != os.getuid() or hello.get('protocol') != PROTOCOL:
                    raise FleetmuxError('the server on %s is not ours (uid %s, protocol %s)'
                                        % (self.ctl_socket, hello.get('uid'), hello.get('protocol')),
                                        code='protocol_version')
                if (self.page is not None and check_pages
                        and (hello.get('pages') != 1 or hello.get('page') != self.page)):
                    raise FleetmuxError(NO_PAGES_TEXT % self.ctl_socket, code='no_pages')
                if cmd == 'hello':
                    return hello
                return self._exchange(stream, dict(args, cmd=cmd))
        except socket.timeout as exc:
            raise FleetmuxError('fleetmux %s timed out after %.0f s' % (cmd, timeout)) from exc
        except (ConnectionError, BrokenPipeError) as exc:
            raise ServerDown('fleetmux server went away during %s (%s)' % (cmd, exc)) from exc
        except OSError as exc:
            raise FleetmuxError('fleetmux %s failed: %s' % (cmd, exc)) from exc

    def _hello(self) -> Optional[Dict]:
        """The server's ``hello`` reply; None when nothing answers. A server that is not ours raises."""
        try:
            return self._call('hello')
        except ServerDown:
            return None

    def _get(self, sid) -> Optional[Dict]:
        """The pane record; None only when a live server answers ``no_such_pane``. A silent socket, a failed
        ``hello`` or any other error raises: a failed read is not "no pane" (design §4.3 / §5)."""
        try:
            reply = self._call('pane.get', pane=str(sid))
        except FleetmuxError as exc:
            if exc.code == 'no_such_pane':
                return None
            raise
        return {k: v for k, v in reply.items() if k not in ('id', 'ok')}

    def _row(self, sid) -> Optional[Dict]:
        """The pane record, None when it is not live or cannot be read."""
        try:
            return self._get(sid)
        except FleetmuxError:
            return None

    def _list(self) -> List[Dict]:
        """``pane.list``: this connection's page (unbound: the root page)."""
        return list(self._call('pane.list').get('panes') or [])

    def _list_all(self) -> List[Dict]:
        """M4: every page's panes -- this page's rows (:meth:`_list`) plus the other pages' from
        ``pane.list{all: true}``, by pane id (an M3 server ignores ``all`` and lists the same rows again)."""
        rows = self._list()
        seen = {str(r.get('pane')) for r in rows}
        extra = self._call('pane.list', all=True).get('panes') or []
        return rows + [r for r in extra if isinstance(r, dict) and str(r.get('pane')) not in seen]

    def _page_row(self) -> Optional[Dict]:
        """This bound manager's ``page.list`` entry; None when the page does not exist (yet)."""
        rows = self._call('page.list').get('pages') or []
        return next((r for r in rows if isinstance(r, dict) and r.get('page') == self.page), None)

    def _groups(self) -> Dict[str, List[str]]:
        groups = self._call('layout.get').get('groups') or {}
        return {g: list(groups.get(g) or []) for g in GROUPS}

    def _move(self, sid: str, group: str, before: Optional[str] = None, after: Optional[str] = None) -> bool:
        args = {'pane': str(sid), 'group': group}
        if before is not None:
            args['before'] = str(before)
        elif after is not None:
            args['after'] = str(after)
        try:
            self._call('pane.move', **args)
        except FleetmuxError as exc:
            self.last_error = str(exc)
            return False
        return True

    def _log_tail(self) -> str:
        try:
            lines = self.server_log.read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            return '(no server.log at %s)' % self.server_log
        return '\n'.join(lines[-LOG_TAIL_LINES:])[-4000:]

    def _ensure_server(self) -> bool:
        if self.page is not None:  # S2: only the root starts the tree's server; a child would orphan the root's panes
            if self._hello() is None:
                raise FleetmuxError('root fleetmux server for %s is not running (%s does not answer); a sub-fleet '
                                    'never starts one' % (self._runtime_session, self.ctl_socket))
            return True
        self.prepare_runtime()
        if self._hello() is None:
            before = self._server_pid()
            if before is not None and _pid_alive(before) and self._pid_is_ours(before):
                # our recorded server lives but does not answer: a second server would orphan its panes
                raise FleetmuxError('fleetmux server %d is still running for %s but %s does not answer; '
                                    'not starting a second server' % (before, self.runtime_root, self.ctl_socket))
            self._spawn_server()
        return True

    def _forget_dead_epochs(self, hello: Optional[Dict]) -> None:
        """Resume after a crash (design §2.4, PROTOCOL.md §1.4): our socket's live server answered ``hello``
        with its epoch, and a new server always has a new epoch, so a registry id ``<other epoch>-<seq>``
        names a pane of a dead server that no server can list or kill again -- forget it. Only a confirmed
        answer prunes: no answer, or an epoch we cannot read, leaves the registry alone (kill_pane's rule).
        Called by ``arrange`` only, so resume's ``kill_pane`` of the old panes still finds them registered."""
        epoch = hello.get('epoch') if hello else None
        if not isinstance(epoch, str) or not EPOCH_RE.fullmatch(epoch):
            return
        meta = self._meta()
        dead = [sid for sid in meta['surfaces'] if PANE_ID_RE.fullmatch(sid) and sid.split('-', 1)[0] != epoch]
        if dead:
            for sid in dead:
                del meta['surfaces'][sid]
            self._save_meta(meta)

    def _spawn_server(self) -> bool:
        binary = fleetmux_bin()
        if not binary:
            raise FleetmuxError(availability_problem() or 'no fleetmux binary')
        fd = os.open(str(self.start_lock), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            deadline = time.monotonic() + LOCK_WAIT_S
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise FleetmuxError('%s is held by another starter for over %.0f s'
                                            % (self.start_lock, LOCK_WAIT_S))
                    time.sleep(0.05)
            if self._hello() is not None:  # someone else started it while we waited
                return True
            if self.ctl_socket.exists() or self.ctl_socket.is_symlink():
                self.ctl_socket.unlink()  # nobody answers on it: a dead server's socket
            argv = [binary, 'server', '--runtime', str(self.runtime_root), '--session', self.session_name,
                    '--daemonize']
            theme = os.environ.get(THEME_ENV)
            if theme:
                argv += ['--theme', theme]
            with tempfile.TemporaryFile() as err:
                try:
                    done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err,
                                          timeout=START_WAIT_S + 5)
                    code = done.returncode
                except subprocess.TimeoutExpired:
                    code = None
                except OSError as exc:
                    raise FleetmuxError('cannot run %s: %s' % (binary, exc)) from exc
                err.seek(0)
                stderr = err.read().decode('utf-8', 'replace').strip()
            if code in (0, None, 3) and _wait(lambda: self._hello() is not None, START_WAIT_S):
                return True  # 3: "the socket already answers" -- someone else's start won
            raise FleetmuxError('fleetmux server did not come up (%s); server.log:\n%s%s' % (
                'exit %s' % code if code is not None else 'no exit after %.0f s' % (START_WAIT_S + 5),
                self._log_tail(), ('\nstderr: ' + stderr[-1000:]) if stderr else ''))
        finally:
            os.close(fd)  # releases the lock

    def _server_pid(self) -> Optional[int]:
        try:
            pid = json.loads(self.server_json.read_text()).get('pid')
            return int(pid) if pid else None
        except (OSError, ValueError, TypeError, AttributeError):
            return None

    def end_server(self) -> bool:
        """``server.shutdown``; signal fallback only for a pid whose command line carries this runtime. A bound
        manager sends nothing and returns False: a sub-fleet never stops the tree's server (§0 item 5)."""
        if self.page is not None:
            self.last_error = 'sub-fleet page %s never stops the tree\'s fleetmux server' % self.page
            return False
        pid = self._server_pid()
        try:
            answered = self._hello() is not None
        except FleetmuxError as exc:
            self.last_error = str(exc)
            return False
        if answered:
            try:
                self._call('server.shutdown', force=True)
            except ServerDown:
                pass  # it closed on us while exiting
            except FleetmuxError as exc:
                self.last_error = 'fleetmux server.shutdown failed: %s' % exc
                return False
            if pid is not None and not _wait(lambda: not _pid_alive(pid), START_WAIT_S):
                self.last_error = 'fleetmux server %d did not exit after server.shutdown' % pid
                return False
            return True
        if pid is None or not _pid_alive(pid):
            return True  # nothing is running
        if not self._pid_is_ours(pid):
            self.last_error = ('fleetmux server.json names pid %d, but its command line / start time do not match '
                               'this runtime (%s); not signalled' % (pid, self.runtime_root))
            return False
        try:
            os.kill(pid, signal.SIGTERM)
            if not _wait(lambda: not _pid_alive(pid), 3.0) and self._pid_is_ours(pid):
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as exc:
            self.last_error = 'cannot signal fleetmux server %d: %s' % (pid, exc)
            return False
        if not _wait(lambda: not _pid_alive(pid), 3.0):
            self.last_error = 'fleetmux server %d survived SIGTERM / SIGKILL' % pid
            return False
        for name in ('ctl.sock', 'server.json'):  # a killed server cannot remove them itself
            try:
                (self.runtime_root / name).unlink()
            except OSError:
                pass
        return True

    def _pid_is_ours(self, pid: int) -> bool:
        """Command line carries ``--runtime <this runtime>`` and the OS start time, as integer Unix seconds,
        equals server.json's ``start_time`` exactly (PROTOCOL.md §8.6, ruling R6: no tolerance; unreadable
        means not ours, so nothing is signalled)."""
        try:
            recorded = json.loads(self.server_json.read_text()).get('start_time')
        except (OSError, ValueError, AttributeError):
            return False
        if not isinstance(recorded, int) or isinstance(recorded, bool):
            return False
        argv = _cmdline(int(pid))
        if not argv:
            return False
        rt = str(self.runtime_root)
        if not any(a == '--runtime' and b == rt for a, b in zip(argv, argv[1:])) and '--runtime=' + rt not in argv:
            return False
        now = _start_time(int(pid))
        return now is not None and now == recorded

    # -- session ----------------------------------------------------------------------------------
    def _fleet_panes(self) -> List[Dict]:
        """``pane.list`` rows that are the fleet's own agents. A bash tab (``type == 'shell'``, S1c) is no agent:
        on its own it does not make a live fleet session (plan §14.5-7)."""
        return [row for row in self._list() if not _is_shell_row(row)]

    def session_exists(self) -> bool:
        """Root: a fleet pane on any page (``lifecycle_close.all_gone`` counts the whole tree). Bound: a fleet
        pane on its own page, and the page is not ``reclaimed`` (a re-up must not be refused by a record page)."""
        try:
            if self.page is None:
                return any(not _is_shell_row(row) for row in self._list_all())
            rows = self._fleet_panes()
            if not rows:
                return False
            page = self._page_row()
            return page is not None and page.get('state') != 'reclaimed'
        except FleetmuxError:
            return False

    def get_or_create_session(self) -> bool:
        problem = availability_problem()
        if problem:
            self.last_error = problem
            return False
        try:
            self._ensure_server()
        except FleetmuxError as exc:
            self.last_error = str(exc)
            return False
        except OSError as exc:
            self.last_error = 'fleetmux runtime %s: %s' % (self.runtime_root, exc)
            return False
        if self.page is not None:  # the child daemon names its page (it usually exists: the daemon tab built it)
            try:
                self._call('page.open', page=self.page, title=self.agents_title)
            except FleetmuxError as exc:
                self.last_error = 'fleetmux page.open %s: %s' % (self.page, exc)
                return False
        meta = self._meta()
        meta['workspace'] = self.workspace_id()
        self._save_meta(meta)
        if meta.get('run'):
            self._kv_set('run', str(meta['run']))
        return True

    def _kv_set(self, key: str, value: str) -> bool:
        try:
            self._call('kv.set', key=key, value=value)
        except FleetmuxError:
            return False
        return True

    def kv_get(self, key: str) -> Optional[str]:
        """kv.get: the value, or None when unset or when no server answers (never raises)."""
        try:
            value = self._call('kv.get', key=key).get('value')
        except FleetmuxError:
            return None
        return value if isinstance(value, str) else None

    def claim_run(self, run: str) -> None:
        meta = self._meta()
        meta['run'] = run
        self._save_meta(meta)
        self._kv_set('run', str(run))  # no server yet: get_or_create_session copies it later

    def workspace_id(self) -> Optional[str]:
        """``fleetmux:<token>``; a bound manager ``fleetmux:<root token>/<page>`` (row 5)."""
        if self.page is not None:
            return 'fleetmux:' + self._runtime_token() + '/' + self.page
        return 'fleetmux:' + self._token()

    def select_workspace(self, workspace: str) -> bool:
        """Row 6: the server's root id -> True (with a server, also ``page.select ''``; never starts one); one of
        its page ids -> ``page.select`` (True when it worked); anything else -> False."""
        if not workspace:
            return False
        root = 'fleetmux:' + self._runtime_token()
        if workspace == root:
            try:
                self._call('page.select', page='')
            except FleetmuxError:
                pass  # no server, or an M3 server: nothing to switch, still our workspace
            return True
        page = workspace[len(root) + 1:] if workspace.startswith(root + '/') else ''
        if not PAGE_ID_RE.fullmatch(page):
            return False
        try:
            self._call('page.select', page=page)
        except FleetmuxError as exc:
            self.last_error = str(exc)
            return False
        return True

    def keep_windows_open(self, ours: List[str]) -> bool:
        """Our server holds no window of the operator's: nothing to keep open."""
        _ = ours
        return True

    def owns(self, sid: str) -> bool:
        """In our registry and still listed by our server."""
        return str(sid) in self._surfaces() and self._row(sid) is not None

    def close_panes(self) -> bool:
        """Kill every registered pane (each confirmed) and keep the server (S2b: the sidebar's ↺ resumes
        inside it). A bash tab (``type == 'shell'``, S1c; registered or not) is no plain close target: the root
        manager kills each one whose shell is idle at its prompt (user 2026-11-08), forgetting it, and keeps the busy
        ones (a program in the foreground, or no ``fg_pgid`` to tell), listed in ``kept_bash_pages``; a bound
        manager never touches one. False when the pane table cannot be read or a registered pane's kill is unconfirmed
        (a bash page whose kill is unconfirmed is only kept)."""
        self.kept_bash_pages = []
        try:
            answered = self._hello() is not None
            rows = self._list_all() if answered else []  # "could not list" is not "nothing left"
        except FleetmuxError as exc:
            self.last_error = 'fleetmux close_panes: cannot list panes: %s' % exc
            return False
        shells = [row for row in rows if _is_shell_row(row)]
        ok = True
        for sid in list(self._surfaces()):
            if str(sid) in {str(row.get('pane')) for row in shells}:
                continue
            if not self.kill_pane(sid):
                ok = False
        if self.page is None:
            for row in shells:
                if not self._kill_idle_shell(str(row.get('pane'))):
                    self.kept_bash_pages.append(str(row.get('title') or row.get('pane')))
        return ok

    def _kill_idle_shell(self, pane_id: str) -> bool:
        """``pane.kill`` a bash page whose shell is idle right now (re-read just before the kill); True once the
        server confirms it gone, False when it is busy / unknown or the kill is unconfirmed (it is kept)."""
        try:
            row = self._get(pane_id)
            if row is None:
                self._forget(pane_id)
                return True
            if not _shell_idle(row):
                return False
            self._call('pane.kill', pane=pane_id, timeout_ms=KILL_TIMEOUT_MS)
        except FleetmuxError as exc:
            if exc.code != 'no_such_pane':
                return False
        try:
            gone = self._get(pane_id) is None
        except FleetmuxError:
            return False
        if gone:
            self._forget(pane_id)
        return gone

    def adopt_shells(self) -> bool:
        """User 2026-11-09: "在fleetmux侧边栏双击打开的任意新的bash窗口，都在主fleet的worker区域有映射，都纳入主fleet的管理".
        Take every sidebar bash of the root page (``type == 'shell'``) into this registry (``type: shell``, labelled
        with its title) and into the ``workers`` group (at its end) when it sits elsewhere; forget the registered
        ones the server no longer lists. It stays no agent: no slot, no work, not counted as busy. Root manager only
        (a sub-fleet never takes one). True when the registry changed; no server or a failed read is no change."""
        if self.page is not None:
            return False
        try:
            rows = self._list()
        except FleetmuxError:
            return False
        live = {str(r.get('pane')): r for r in rows if _is_shell_row(r)}
        meta = self._meta()
        surfaces = meta['surfaces']
        changed = False
        for sid in [s for s, info in surfaces.items() if info.get('type') == SHELL_TYPE and s not in live]:
            del surfaces[sid]
            changed = True
        for sid, row in live.items():
            title = str(row.get('title') or sid)
            entry = surfaces.setdefault(sid, {})
            if entry.get('type') != SHELL_TYPE or entry.get('label') != title:
                entry.update(type=SHELL_TYPE, label=title, window_name=title, workspace=str(self.workspace_id()))
                changed = True
            if row.get('group') != 'workers':
                self._move(sid, 'workers')
        if changed:
            self._save_meta(meta)
        return changed

    def shell_slots(self) -> List[Tuple[str, str, str]]:
        """The registered bash tabs as history-page slots ``(pane id, title, title)``, in creation order. Reads the
        registry only (:meth:`adopt_shells` keeps it current)."""
        def seq(sid: str) -> int:
            tail = sid.rsplit('-', 1)[-1]
            return int(tail) if tail.isdigit() else 0
        shells = [(sid, str(info.get('label') or sid)) for sid, info in self._surfaces().items()
                  if info.get('type') == SHELL_TYPE]
        return [(sid, title, title) for sid, title in sorted(shells, key=lambda s: seq(s[0]))]

    def close_all(self) -> bool:
        """Kill every registered pane (each confirmed) and every idle bash page (:meth:`close_panes`), then stop our
        server (pid fallback checks identity). While a busy bash page lives the server is kept (S1c, plan §14.5-7):
        it exits by itself per §13.4.
        Bound (row 9): kill this page's registered panes, then ``page.close{keep: true}`` (the page turns
        ``reclaimed``); the server is never stopped."""
        if not self.close_panes():
            return False
        if self.page is not None:
            try:
                if self._page_row() is None:
                    return True  # the page was never built: nothing to reclaim
                self._call('page.close', page=self.page, keep=True)
            except FleetmuxError as exc:
                self.last_error = 'fleetmux page.close %s: %s' % (self.page, exc)
                return False
            return True
        try:
            rows = self._list_all()  # a bash page lives on a page of its own (user 2026-11-08)
        except FleetmuxError as exc:
            self.last_error = 'fleetmux close_all: cannot list panes: %s' % exc
            return False
        if any(_is_shell_row(row) for row in rows):
            return True
        return self.end_server()

    def mark_closed(self, info: Dict) -> bool:
        """kv ``restorable``: the closed run's ``start --resume`` argv / cwd / env (S2b: this one run;
        S2c appends the history list, format in plan §18.3)."""
        value = {'v': 1, 'runs': [dict(info)]}
        return self._kv_set('restorable', json.dumps(value, ensure_ascii=False, separators=(',', ':')))

    # -- spawning ---------------------------------------------------------------------------------
    def _spawn(self, window_name: str, script: str, metadata: Optional[Dict[str, str]],
               start_directory: Optional[str], env: Optional[Dict[str, str]] = None,
               argv: Optional[List[str]] = None) -> SpawnResult:
        """Make sure the server runs (the first spawn creates it), then ``pane.spawn`` one argv."""
        try:
            self._ensure_server()
        except (FleetmuxError, OSError) as exc:
            self.last_error = str(exc)
            return SpawnResult(success=False, error=self.last_error)
        metadata = dict(metadata or {})
        cwd = os.path.abspath(os.path.expanduser(start_directory)) if start_directory else None
        if argv is None:
            body = script if re.match(r'exec\s', script) else 'exec ' + script
            argv = ['bash', '-lc', ('cd ' + shlex.quote(cwd) + ' && ' if cwd else '') + body]
        request = {'argv': argv, 'title': window_name}
        if cwd:
            request['cwd'] = cwd
        if env:
            request['env'] = {str(k): str(v) for k, v in env.items() if v is not None}
        if metadata.get('type'):
            request['type'] = str(metadata['type'])
        try:
            reply = self._call('pane.spawn', **request)
        except FleetmuxError as exc:
            self.last_error = 'fleetmux pane.spawn failed: %s' % exc
            return SpawnResult(success=False, error=self.last_error)
        sid = str(reply.get('pane') or '')
        info = {k: v for k, v in metadata.items() if isinstance(v, str)}
        info.update(type=str(metadata.get('type') or ''), label=str(metadata.get('label') or window_name),
                    window_name=window_name, workspace=str(self.workspace_id()))
        self._tag(sid, **info)
        return SpawnResult(success=True, pane_id=sid, window_name=window_name)

    def spawn_window(self, window_name: str, command: str, metadata: Optional[Dict[str, str]] = None,
                     start_directory: Optional[str] = None) -> SpawnResult:
        """``bash -lc 'cd <cwd> && exec <command>'`` as one argv (never typed), placed by its type."""
        return self._spawn(window_name, command, metadata, start_directory)

    def spawn_interactive_cli(self, window_name: str, cli_command: str, prompt_file: Optional[str] = None,
                              output_file: Optional[str] = None, metadata: Optional[Dict[str, str]] = None,
                              start_directory: Optional[str] = None, auto_close: bool = True,
                              env_vars: Optional[Dict[str, str]] = None, tmux_socket: Optional[str] = None,
                              tmux_session: Optional[str] = None, **_) -> SpawnResult:
        """The cmux command shape: ``prompt_file`` -> ``"$(<file)"``, ``auto_close=False`` -> a login shell
        after the CLI, ``env_vars`` as structured env; tmux fields -> build the session, the pane attaches."""
        inner = cli_command
        if prompt_file:
            inner = '%s "$(<%s)"' % (cli_command, shlex.quote(str(prompt_file)))
        if not auto_close:
            inner = '%s; exec %s -l' % (inner, shlex.quote(os.environ.get('SHELL') or '/bin/bash'))
        if tmux_socket and tmux_session:
            from fleetmux.backends.cmux import _tmux, tmux_attach_command, tmux_sessions
            if tmux_session not in tmux_sessions(tmux_socket):
                # the CLI runs in the tmux session, so it gets the inner command and env_vars there (cmux shape:
                # ``env K=V ... bash -lc <inner>``); the attach pane below is only its client
                env = [str(k) + '=' + str(v) for k, v in (env_vars or {}).items() if v is not None]
                done = _tmux(tmux_socket, 'new-session', '-d', '-s', tmux_session, '-c', start_directory or '.',
                             *(['env', *env] if env else []), 'bash', '-lc', inner)
                if done.returncode:
                    self.last_error = 'tmux new-session failed: ' + (done.stderr or done.stdout).strip()
                    return SpawnResult(success=False, error=self.last_error)
            res = self._spawn(window_name, tmux_attach_command(tmux_socket, tmux_session), metadata,
                              start_directory, env=env_vars)
            if res.success:
                self._tag(res.pane_id, tmux_socket=str(tmux_socket), tmux_session=tmux_session, tmux_role='owner',
                          output_file=output_file or '')
            return res
        res = self._spawn(window_name, 'bash -lc ' + shlex.quote(inner), metadata, start_directory, env=env_vars)
        if res.success:
            self._tag(res.pane_id, output_file=output_file or '')
        return res

    # -- reading / writing ------------------------------------------------------------------------
    def send_prompt_to_idle(self, pane_id: str, text: str, bracketed: bool = False) -> bool:
        """``pane.write`` (``bracketed`` -> always wrapped, else raw), then Enter on its own after 1 s / 0.2 s."""
        from fleetmux import claude_input
        if not self.owns(pane_id):
            return False
        if claude_input.required(self, pane_id):
            def paste():
                self._call('pane.write', pane=str(pane_id), data=base64.b64encode(text.encode('utf-8')).decode('ascii'),
                           mode='bracketed' if bracketed or text != '/clear' else 'raw')
                return True
            return claude_input.send(self, pane_id, text, paste)
        data = base64.b64encode(text.encode('utf-8')).decode('ascii')
        try:
            self._call('pane.write', pane=str(pane_id), data=data, mode='bracketed' if bracketed else 'raw')
            time.sleep(1.0 if bracketed else 0.2)  # paste-burst detection needs the gap before Enter
            self._call('pane.key', pane=str(pane_id), key='Enter')
        except FleetmuxError as exc:
            self.last_error = str(exc)
            return False
        return True

    def send_key(self, pane_id: str, key: str) -> bool:
        """``pane.key``: the server encodes for the inner program's modes; an unknown name writes nothing."""
        if not self.owns(pane_id):
            return False
        try:
            self._call('pane.key', pane=str(pane_id), key=str(key))
        except FleetmuxError as exc:
            self.last_error = str(exc)
            return False
        return True

    def capture_output(self, pane_id: str, lines: int = 2000) -> str:
        if str(pane_id) not in self._surfaces():
            return ''
        try:
            return str(self._call('pane.read', pane=str(pane_id), lines=max(1, int(lines))).get('text') or '')
        except FleetmuxError:
            return ''

    def capture_to(self, pane_id: str, path: str) -> None:
        """Written by this process from ``pane.read``: the server never writes an arbitrary path."""
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(self.capture_output(pane_id, 100000), encoding='utf-8')

    # -- state ------------------------------------------------------------------------------------
    def _owned_row(self, pane_id) -> Optional[Dict]:
        if str(pane_id) not in self._surfaces():
            return None
        return self._row(pane_id)

    def get_pane_status(self, pane_id: str) -> Optional[PaneStatus]:
        row = self._owned_row(pane_id)
        if row is None:
            return None
        info = self._surfaces().get(str(pane_id)) or {}
        meta = {k: v for k, v in info.items() if isinstance(v, str)}
        meta.update({k: str(row.get(k, '')) for k in ('tty', 'group', 'cols', 'rows', 'index', 'pid', 'pgid')})
        if 'page' in row:  # M4: the server's page of the pane
            meta['page'] = str(row.get('page') or '')
        meta['title'] = str(row.get('title') or '')
        return PaneStatus(pane_id=str(pane_id), window_name=info.get('label') or info.get('window_name') or '',
                          alive=True, metadata=meta)

    def _tty(self, pane_id) -> Optional[str]:
        row = self._owned_row(pane_id)
        return str(row.get('tty') or '') or None if row else None

    def _ps(self, tty: Optional[str], fmt: str) -> List[str]:
        if not tty:
            return []
        name = tty[len('/dev/'):] if tty.startswith('/dev/') else tty
        try:
            done = subprocess.run(['ps', '-ww', '-t', name, '-o', fmt], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return []
        return [line for line in done.stdout.splitlines() if line.strip()] if done.returncode == 0 else []

    def _tmux_cli_sel(self, tm: Dict[str, str]) -> List[str]:
        """``ps`` selector of the process trees under a tmux session's panes (cmux ``_tmux_pids`` + ``_tree_pids``);
        [] when the session is gone or its panes cannot be read."""
        from fleetmux.backends.cmux import _tmux, _tree_pids
        try:
            done = _tmux(tm['tmux_socket'], 'list-panes', '-s', '-t', '=' + tm['tmux_session'], '-F', '#{pane_pid}')
        except (OSError, subprocess.SubprocessError):
            return []
        if done.returncode:
            return []
        pids = [str(p) for line in done.stdout.split() if line.isdigit() for p in _tree_pids(int(line))]
        return ['-p', ','.join(pids)] if pids else []

    def _ps_sel(self, sel: List[str], fmt: str) -> List[str]:
        try:
            done = subprocess.run(['ps', '-ww', *sel, '-o', fmt], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return []
        return [line for line in done.stdout.splitlines() if line.strip()] if done.returncode == 0 else []

    def has_cli_process(self, pane_id: str, names: Tuple[str, ...] = ('codex',)) -> bool:
        """Is one of ``names`` running on the pane's tty? A bare shell never matches (cmux's rule). A tmux slot's
        tab (owner or view) only runs a tmux client: the CLI is looked for in the tmux session's process tree
        (row 17, the cmux tmux branch); session gone or unreadable -> False."""
        tm = self._tmux_of(pane_id)
        if tm:
            sel = self._tmux_cli_sel(tm)
            if not sel:
                return False
            exes = [_exe(line) for line in self._ps_sel(sel, 'comm=')]
            if any(e in names for e in exes):
                return True
            if 'node' not in exes:
                return False
            return any(any(name in line for name in names) for line in self._ps_sel(sel, 'args='))
        tty = self._tty(pane_id)
        if not tty:
            return False
        exes = [_exe(line) for line in self._ps(tty, 'comm=')]
        if any(e in names for e in exes):
            return True
        if 'node' not in exes:
            return False
        return any(any(name in line for name in names) for line in self._ps(tty, 'args='))

    def process_states(self, pane_id: str) -> List[str]:
        """``ps`` STAT of the processes on the pane's tty (watchdog frozen check)."""
        return [line.strip() for line in self._ps(self._tty(pane_id), 'stat=')]

    def process_cpu(self, pane_id: str) -> Optional[float]:
        """Cumulative CPU seconds of the pane's tty tree; None when unreadable."""
        from fleetmux.watchdog import pane_tty_cpu
        tty = self._tty(pane_id)
        return pane_tty_cpu([tty]) if tty else None

    # -- label / focus ----------------------------------------------------------------------------
    def rename(self, pane_id: str, label: str) -> None:
        live = self._row(pane_id) is not None
        if live:
            try:
                self._call('pane.rename', pane=str(pane_id), title=label)
            except FleetmuxError as exc:
                self.last_error = str(exc)
        if live or str(pane_id) in self._surfaces():
            self._tag(str(pane_id), label=label)

    def focus(self, pane_id: str) -> bool:
        if not self.owns(pane_id):
            return False
        try:
            self._call('pane.focus', pane=str(pane_id))
        except FleetmuxError as exc:
            self.last_error = str(exc)
            return False
        return True

    def kill_pane(self, pane_id: str) -> bool:
        """Not registered -> False; a live server answers ``no_such_pane`` -> forget, True; else append the
        screen to ``output_file``, ``pane.kill`` and forget only once the server confirms the pane is gone.
        Anything unconfirmed -- kill_timeout, a silent socket, a failed hello / ``pane.get`` -> False, and
        the registry entry (output_file included) is kept so fleet can retry.
        A tmux slot's owner tab (row 22): its tmux session's history goes to ``output_file`` and the session is
        ended (else the old CLI would run on in the tmux server), also when the tab is already gone; a view tab
        only ends its own client, never the session."""
        info = self._surfaces().get(str(pane_id))
        if info is None:
            return False
        tm = self._tmux_of(pane_id)
        owner = bool(tm) and tm.get('tmux_role') == 'owner'
        try:
            row = self._get(pane_id)
        except FleetmuxError as exc:
            self.last_error = str(exc)
            return False
        if row is None:
            if owner:
                self._end_tmux_session(tm, info)
            self._forget(pane_id)
            return True
        if owner:
            self._end_tmux_session(tm, info)  # the client in the tab exits by itself once its session is gone
        elif info.get('output_file'):
            try:
                text = self.capture_output(pane_id, 100000)
                with open(info['output_file'], 'a', encoding='utf-8') as fh:
                    fh.write(text if text.endswith('\n') else text + '\n')
            except OSError:
                pass
        try:
            self._call('pane.kill', pane=str(pane_id), timeout_ms=KILL_TIMEOUT_MS)
        except FleetmuxError as exc:
            if exc.code != 'no_such_pane':
                self.last_error = 'fleetmux pane.kill %s: %s' % (pane_id, exc)
                return False  # keep ownership so fleet can retry
        try:
            if self._get(pane_id) is not None:
                return False
        except FleetmuxError:
            return False
        self._forget(pane_id)
        return True

    def _end_tmux_session(self, tm: Dict[str, str], info: Dict[str, str]) -> None:
        """Append the tmux session's whole history to ``output_file`` (cmux ``_tmux_history``), then
        ``kill-session`` it; a session already gone is nothing to do."""
        from fleetmux.backends.cmux import _tmux
        sock, session = tm['tmux_socket'], tm['tmux_session']
        try:
            if info.get('output_file'):
                hist = _tmux(sock, 'capture-pane', '-p', '-S', '-', '-t', '=' + session + ':')
                if hist.returncode == 0:
                    with open(info['output_file'], 'a', encoding='utf-8') as fh:
                        fh.write(hist.stdout if hist.stdout.endswith('\n') else hist.stdout + '\n')
            _tmux(sock, 'kill-session', '-t', '=' + session)
        except (OSError, subprocess.SubprocessError) as exc:
            self.last_error = 'tmux session %s: %s' % (session, exc)

    # -- layout -----------------------------------------------------------------------------------
    def _history_command(self, run_dir: Path) -> List[str]:
        """The history pane's argv (design §4.3): fleet's own interpreter runs the viewer on the run dir."""
        return [sys.executable, '-m', 'fleetmux.history_view', str(run_dir)]

    def _live_of_type(self, kind: str, rows: Optional[Dict[str, Dict]] = None) -> List[str]:
        if rows is None:
            rows = {r['pane']: r for r in self._list()}
        return [sid for sid, info in self._surfaces().items() if info.get('type') == kind and sid in rows]

    def arrange(self, supervisor: str, workers: List[str], history_url: str) -> Optional[str]:
        """Supervisor first in its group, workers in the given order on the right, the history pane first
        in ``lower`` (spawned when missing) and shown there, control behind it, focus on the supervisor. Returns the
        history pane's real id; None when a pane cannot be placed or the history belongs to another run."""
        try:
            rows = {r['pane']: r for r in self._list()}
            self._forget_dead_epochs(self._hello())
        except FleetmuxError as exc:
            self.last_error = str(exc)
            return None
        self.adopt_shells()  # a bash opened while no fleet ran is taken in when the next one arranges (user 2026-11-09)
        surfaces = self._surfaces()
        if str(supervisor) not in rows or any(str(w) not in rows for w in workers):
            return None
        hist = None
        for sid in self._live_of_type('history', rows):
            if surfaces[sid].get('url') != history_url:
                self.last_error = 'the history pane %s shows another run (%s)' % (sid, surfaces[sid].get('url'))
                return None
            hist = sid
        if hist is None:
            parsed = urlparse(history_url)
            run_dir = Path(unquote(parsed.path)).parent if parsed.scheme == 'file' else Path(history_url).parent
            env = {'PYTHONPATH': os.pathsep.join(p for p in (str(PACKAGE_ROOT), os.environ.get('PYTHONPATH')) if p)}
            res = self._spawn('history', '', {'type': 'history', 'label': 'history'}, str(run_dir), env=env,
                              argv=self._history_command(run_dir))
            if not res.success:
                return None
            hist = res.pane_id
        self._tag(hist, type='history', url=history_url)
        try:
            groups = self._groups()
        except FleetmuxError as exc:
            self.last_error = str(exc)
            return None
        if groups['supervisor'][:1] != [str(supervisor)]:
            first = groups['supervisor'][0] if groups['supervisor'] else None
            if not self._move(supervisor, 'supervisor', before=first if first != str(supervisor) else None):
                return None
        for w in workers:
            if not self._move(w, 'workers'):
                return None
        for sid in self._live_of_type(SHELL_TYPE, rows):  # the bash tabs stay behind the workers (never fatal)
            self._move(sid, 'workers')
        if groups['lower'][:1] != [hist]:
            first = groups['lower'][0] if groups['lower'] else None
            if not self._move(hist, 'lower', before=first if first not in (None, hist) else None):
                return None
        if not self.dock_control(hist):
            return None
        self._kv_set('history_url', history_url)  # what prefix `o` opens (PROTOCOL.md §10)
        try:
            # The control (daemon) pane is usually spawned first and so was lower's selected tab; show history
            # there instead (user 2026-11-09), then hand focus back to the supervisor.
            self._call('pane.focus', pane=str(hist))
            self._call('pane.focus', pane=str(supervisor))
        except FleetmuxError as exc:
            self.last_error = str(exc)
            return None
        return hist

    def dock_tab(self, sid: str, history_surface: str, after: Optional[str] = None) -> bool:
        """Into ``lower``: right after ``after`` -> else after the control pane -> else after history."""
        try:
            rows = {r['pane']: r for r in self._list()}
        except FleetmuxError:
            return False
        if str(sid) not in rows or str(sid) not in self._surfaces() or str(history_surface) not in rows:
            return False
        if after and str(after) in rows and rows[str(after)]['group'] == 'lower' and str(after) != str(sid):
            anchor = str(after)
        else:
            lower = [r['pane'] for r in sorted(rows.values(), key=lambda r: r['index']) if r['group'] == 'lower']
            controls = [c for c in self._live_of_type('daemon', rows) if c in lower and c != str(sid)]
            anchor = max(controls, key=lower.index) if controls else str(history_surface)
        if anchor == str(sid):
            return True
        return self._move(sid, 'lower', after=anchor)

    def dock_control(self, history_surface: str) -> bool:
        """Every control (daemon) pane right behind the history pane; moved, never killed."""
        try:
            rows = {r['pane']: r for r in self._list()}
        except FleetmuxError:
            return False
        if str(history_surface) not in rows:
            return False
        anchor = str(history_surface)
        for sid in self._live_of_type('daemon', rows):
            if not self._move(sid, 'lower', after=anchor):
                return False
            anchor = sid
        return True

    def place_right_of(self, pane_id: str, peer: str) -> bool:
        """Only for "the first worker beside the supervisor": a pane in its peer's left group goes to the end
        of ``workers``; already in another group -> nothing to do."""
        row, peer_row = self._owned_row(pane_id), self._row(peer)
        if row is None or peer_row is None:
            return False
        if row['group'] != peer_row['group'] or row['group'] == 'workers':
            return True
        return self._move(pane_id, 'workers')

    def place_after(self, workspace: Optional[str]) -> bool:
        """Workspace order is a sidebar in cmux; there is none here: success, nothing to do."""
        _ = workspace
        return True

    def tab_into(self, pane_id: str, peer: str, before: bool = True) -> bool:
        """Into ``peer``'s group: right before it (a successor takes its predecessor's slot) or at the end."""
        row, peer_row = self._owned_row(pane_id), self._row(peer)
        if row is None or peer_row is None:
            return False
        if str(pane_id) == str(peer):
            return True
        return self._move(pane_id, peer_row['group'], before=str(peer) if before else None)

    def tab_panes(self) -> Dict[str, str]:
        """{pane id: title} of the companion tabs this registry owns and the server still lists."""
        try:
            rows = {r['pane']: r for r in self._list()}
        except FleetmuxError:
            return {}
        return {sid: str(rows[sid].get('title') or '') for sid in self._live_of_type('tab', rows)}

    # -- browser cell (= the history pane here) ---------------------------------------------------
    def _web(self, cmd: str, **args) -> Optional[Dict]:
        """One ``web.*`` call (M2.5, PROTOCOL.md §4.18-§4.21); None on any failure -- ``no_window``,
        ``web_timeout``, an older server's ``unknown_cmd``, no server -- so the callers keep today's answer."""
        try:
            return self._call(cmd, **args)
        except FleetmuxError:
            return None

    @staticmethod
    def _windows(reply: Optional[Dict]) -> int:
        n = (reply or {}).get('windows')
        return n if type(n) is int else 0

    def browser_goto(self, surface: str, url: str) -> bool:
        """``web.goto`` to every viewer's web window: True only when at least one window loaded ``url``. Without a
        window (or on any ``web.*`` error) False, as before: a terminal tab has nothing to navigate."""
        _ = surface
        return self._windows(self._web('web.goto', url=str(url))) >= 1

    def browser_url(self, surface: str) -> Optional[str]:
        """The primary web window's URL (``web.url``); without a window or on any error the registered url."""
        registered = (self._surfaces().get(str(surface)) or {}).get('url') or None
        if registered is None:
            return None
        url = (self._web('web.url') or {}).get('url')
        return url if isinstance(url, str) and url else registered

    def reload_browser(self, surface: str) -> bool:
        """True while the pane lives; with a web window it is also told to reload (``web.reload``). history_view
        watches its own file, so a failed ``web.reload`` does not change the answer."""
        if self._row(surface) is None:
            return False
        self._web('web.reload')
        return True

    # -- handover between fleets ------------------------------------------------------------------
    def release(self, pane_id: str) -> Optional[Dict]:
        meta = self._meta()
        info = meta['surfaces'].pop(str(pane_id), None)
        if info is not None:
            self._save_meta(meta)
        return info

    def adopt(self, pane_id: str, peer: str, info: Dict) -> bool:
        """Only a pane of this same server: move it right before ``peer`` in ``peer``'s group and register it.
        Always moved (row 34), same group too: on a page connection that is what pulls a pane of another page
        (the parent's worker) into this page, with its PTY and process unchanged."""
        row, peer_row = self._row(pane_id), self._row(peer)
        if row is None or peer_row is None:
            return False
        if str(pane_id) != str(peer) and not self._move(pane_id, peer_row['group'], before=str(peer)):
            return False
        self._tag(str(pane_id), **{k: v for k, v in (info or {}).items() if isinstance(v, str)})
        return True

    # -- tmux views (named sub-fleets) ------------------------------------------------------------
    def _tmux_of(self, sid: str) -> Optional[Dict[str, str]]:
        info = self._surfaces().get(str(sid)) or {}
        return info if info.get('tmux_socket') and info.get('tmux_session') else None

    def tmux_view_info(self, sid: str) -> Optional[Dict[str, str]]:
        tm = self._tmux_of(sid)
        if not tm:
            return None
        return {k: v for k, v in tm.items() if k in ('type', 'label', 'tmux_socket', 'tmux_session')}

    def attach_view(self, info: Dict[str, str], peer: str) -> Optional[str]:
        """A second tmux client in a tab of ours (same tmux server, same session), tabbed into ``peer``'s group."""
        from fleetmux.backends.cmux import tmux_attach_command, tmux_sessions
        sock, session = (info or {}).get('tmux_socket'), (info or {}).get('tmux_session')
        if not sock or not session or self._row(peer) is None or session not in tmux_sessions(sock):
            return None
        res = self._spawn('view-' + session, tmux_attach_command(sock, session), None, None)
        if not res.success:
            return None
        self._tag(res.pane_id, **{k: v for k, v in info.items() if isinstance(v, str) and k != 'output_file'},
                  tmux_role='view')
        if not self.tab_into(res.pane_id, peer):
            self.kill_pane(res.pane_id)
            return None
        # Which client sizes the shared window is the server's call: it knows the page in view (its tmux watch
        # nudges the client in view after this one attached or was moved, fleetmux design §44).
        return res.pane_id

    def tmux_session_alive(self, sid: str) -> Optional[bool]:
        from fleetmux.backends.cmux import _tmux
        tm = self._tmux_of(sid)
        if not tm:
            return None
        if not Path(tm['tmux_socket']).exists():
            return False
        try:
            done = _tmux(tm['tmux_socket'], 'list-sessions', '-F', '#{session_name}')
        except OSError:
            return None
        if done.returncode == 0:
            return tm['tmux_session'] in done.stdout.split()
        return False if 'no server running' in done.stderr + done.stdout else None

    def reattach_client(self, sid: str, peer: str) -> Optional[str]:
        """``sid``'s tab was closed while its tmux session runs on: open a new client tab, take over its entry."""
        tm = self._tmux_of(sid)
        if not tm or self._row(peer) is None or self._row(sid) is not None:
            return None
        new = self.attach_view(self.tmux_view_info(sid), peer)
        if not new:
            return None
        meta = self._meta()
        if str(sid) in meta['surfaces'] and new in meta['surfaces']:
            meta['surfaces'][new] = meta['surfaces'].pop(str(sid))
        self._save_meta(meta)
        if tm.get('label'):
            self.rename(new, tm['label'])
        return new

    def detach_view(self, sid: str) -> bool:
        """Disconnect this tab's tmux client (the session keeps running), then kill our pane."""
        from fleetmux.backends.cmux import _tmux
        tm = self._tmux_of(sid)
        if not tm:
            return False
        row = self._row(sid)
        if row is not None and row.get('tty'):
            _tmux(tm['tmux_socket'], 'detach-client', '-t', str(row['tty']))
        return self.kill_pane(sid)

    # -- pages (M4, design §4.2 / §5.4) ------------------------------------------------------------
    def pages_problem(self) -> Optional[str]:
        """Why this tree cannot run sub-fleets on the server that answers: it has no pages (an M3 server). None
        when it has, and when no server answers (the version is the starting root's business)."""
        try:
            hello = self._request('hello', False)
        except ServerDown:
            return None
        except FleetmuxError as exc:
            return 'fleetmux server on %s: %s' % (self.ctl_socket, exc)
        if hello.get('pages') != 1:
            return NO_PAGES_TEXT % self.ctl_socket
        return None

    def server_has_pages(self) -> bool:
        """A server answers and says ``pages: 1`` (the sidebar puts ``page`` on its cards only then)."""
        try:
            return self._request('hello', False).get('pages') == 1
        except FleetmuxError:
            return False

    def _archive_one(self, page: str, run: Path, title: str) -> bool:
        """One reclaimed page with a ``history_view`` record tab in it, registered here as ``type: archive``."""
        url = (Path(run) / 'history.html').as_uri()
        live = {str(r.get('pane')): r for r in self._list_all()}
        for sid, info in self._surfaces().items():
            if info.get('type') == ARCHIVE_TYPE and info.get('page') == page and sid in live:
                if live[sid].get('page') == page and info.get('url') == url:
                    return True  # idempotent: the page's record tab is already there
        self._call('page.open', page=page, title=title, reclaimed=True, history_url=url)
        env = {'PYTHONPATH': os.pathsep.join(p for p in (str(PACKAGE_ROOT), os.environ.get('PYTHONPATH')) if p)}
        reply = self._call('pane.spawn', page=page, type='history', title='history', cwd=str(run), env=env,
                           argv=self._history_command(Path(run)))
        sid = str(reply.get('pane') or '')
        self._tag(sid, type=ARCHIVE_TYPE, label='history', window_name='history', page=page, url=url,
                  run=str(run), workspace=str(self.workspace_id()))
        return True

    def archive_page(self, page: str, run, title: str) -> bool:
        """§5.4 items 2 / 5 / 6: keep the reclaimed sub-fleet ``page`` readable -- a reclaimed page whose
        ``history_url`` is ``<run>/history.html``, with a ``history_view <run>`` tab registered in THIS registry --
        and the same for every reclaimed descendant run under ``run`` (grandchildren...), so the live fleet holds
        every record below it. Idempotent. Failure -> ``last_error``, False (the collect goes on)."""
        from fleetmux import subfleet
        try:
            if not PAGE_ID_RE.fullmatch(str(page)):
                raise FleetmuxError('bad page id %r' % (page,), code='bad_request')
            self._archive_one(str(page), Path(run), str(title or page))
            todo = list(subfleet.children_dirs(run))
            while todo:
                d = todo.pop(0)
                rec = subfleet.record(d)
                todo.extend(subfleet.children_dirs(d))
                sub = rec.get('task_id')
                if rec.get('state') in subfleet.ACTIVE or not isinstance(sub, str) or not PAGE_ID_RE.fullmatch(sub):
                    continue
                self._archive_one(sub, Path(d), str(rec.get('title') or sub))
        except FleetmuxError as exc:
            self.last_error = 'fleetmux archive_page %s: %s' % (page, exc)
            return False
        return True

    def unarchive_page(self, page: str) -> bool:
        """§5.4 item 4: before ``page`` is used again, kill this registry's record tabs of ``page`` and of its
        descendants (``page + '.'`` prefix). Nothing to kill -> True."""
        ok = True
        for sid, info in list(self._surfaces().items()):
            mine = info.get('page')
            if info.get('type') == ARCHIVE_TYPE and isinstance(mine, str) and (
                    mine == page or mine.startswith(str(page) + '.')):
                if not self.kill_pane(sid):
                    ok = False
        return ok
