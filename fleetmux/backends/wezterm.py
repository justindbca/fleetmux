"""WezTerm backend (design §4): the cmux interface on top of a private mux server per run.

Why a private mux server
------------------------
WezTerm multiplexes every pane through a ``wezterm-mux-server``. The operator's own WezTerm already
runs one, and a second one with the default state directory cannot start: it locks
``~/.local/share/wezterm/pid`` (measured, impl-wezterm/probe-report.md §1::

    ERROR wezterm_mux_server > unable to lock pid file ~/.local/share/wezterm/pid

``WEZTERM_RUNTIME_DIR`` does not exist as a knob and ``XDG_DATA_HOME`` is ignored. Only two things put
that pid lock anywhere else, and the second one is the truth (M2 second operator correction, which
retracts the earlier "``HOME`` is the only isolation" conclusion):

* ``HOME=<fake>`` — what the first iteration did. It does move the lock, but it also leaks into every
  spawned pane, so an agent loses ``~/.claude`` / ``~/.codex`` / ``~/.gitconfig``. **Rejected**.
* **``--daemonize`` is what takes the operator's pid file at all.** Without ``--daemonize`` the server does
  not touch ``~/.local/share/wezterm/pid``, so no ``HOME`` override is needed. **This is what the backend
  does now**: the server is a plain ``Popen`` in a new session, its pid is recorded in ``<run dir>/mux.pid``
  by us, and only that pid is ever signalled.

So a run needs no ``HOME`` trick: its own ``wezterm.lua`` (one ``unix_domains`` entry, ``default_domain``),
its own socket, and ``WEZTERM_UNIX_SOCKET=<that socket>`` plus ``--no-auto-start`` on every ``wezterm cli``
call. That is enough to never reach, or start, the operator's WezTerm (verified: two private servers plus
the operator's run side by side, ``probe-report.md`` §1). ``HOME`` is inherited untouched, so panes run as
the operator.

Layout (§4.3)
-------------
WezTerm cannot put tabs inside a split, so there is no cmux-style page. One window holds:

* the supervisor's tab, with the control (daemon) pane split below it,
* one tab per worker, one tab per companion tab.

``tab_into`` moves a pane into the target pane's tab; ``dock_tab`` / ``dock_control`` / ``place_after``
are no-ops that succeed (there is no second history pane to dock into). ``supports_browser`` is False:
``browser_*`` return False / None and ``arrange`` returns None so fleet prints the history path instead.

Keys (§4.2)
-----------
``send_text`` is a paste. With ``--no-paste`` it writes the bytes verbatim, which the measured probe
shows is enough to deliver Esc (0x1b), Ctrl-C (0x03) and arrow keys (ESC[A …) as real keystrokes to a
raw-mode foreground program. So ``send_key`` really sends, and returns False for a name it does not know
rather than typing the name as text. A newline inside ``send-text`` arrives as LF, never CR: Enter is
always a separate ``send-text --no-paste $'\\r'``.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fleetmux import paths

# The dependency floor (design §4.4 / deps.yaml): ``wezterm cli send-text --no-paste`` and the
# ``list --format json`` fields this backend reads are not older than this.
MIN_VERSION = (20240203,)
VERSION_RE = re.compile(r'wezterm\s+(\d{8})')
# sockaddr_un.sun_path includes a terminating NUL: 104 bytes on macOS, 108 on Linux (as redis_runtime).
SOCKET_PATH_LIMIT = 103 if sys.platform == 'darwin' else 107
TOKEN_RE = re.compile(r'[A-Za-z0-9_.:-]{1,64}\Z')
#: Key names ``send_key`` knows, and the bytes ``send-text --no-paste`` must carry for each. Only the
#: keys a watchdog needs to answer a dialog: Esc / Ctrl-C to abort, arrows to move a selection.
KEYS = {
    'Escape': '\x1b', 'Esc': '\x1b',
    'Enter': '\r', 'Return': '\r',
    'Tab': '\t', 'BTab': '\x1b[Z',
    'Space': ' ', 'BSpace': '\x7f', 'Backspace': '\x7f',
    'Up': '\x1b[A', 'Down': '\x1b[B', 'Right': '\x1b[C', 'Left': '\x1b[D',
    'Home': '\x1b[H', 'End': '\x1b[F',
    'PageUp': '\x1b[5~', 'PageDown': '\x1b[6~',
    'Delete': '\x1b[3~', 'Insert': '\x1b[2~',
    'C-c': '\x03', 'Ctrl-C': '\x03', 'C-d': '\x04', 'Ctrl-D': '\x04',
    'C-a': '\x01', 'C-e': '\x05', 'C-u': '\x15', 'C-z': '\x1a',
}

_MUX_BIN = 'wezterm-mux-server'


class WezTermError(RuntimeError):
    """A private mux server / socket / CLI problem that fleet must see (never silently degraded)."""


def wezterm_bin() -> Optional[str]:
    return os.environ.get('FLEETMUX_WEZTERM') or shutil.which('wezterm')


def mux_bin() -> Optional[str]:
    return os.environ.get('FLEETMUX_WEZTERM_MUX') or shutil.which(_MUX_BIN)


WEZTERM_BIN: Optional[str] = None  # tests pin these; None means "look on PATH"
MUX_BIN: Optional[str] = None


def _bin() -> Optional[str]:
    return WEZTERM_BIN or wezterm_bin()


def _mux() -> Optional[str]:
    return MUX_BIN or mux_bin()


def version_of(binary: str) -> Optional[Tuple[int, ...]]:
    """``wezterm 20240203-110809-5046fc22`` -> ``(20240203,)``; None when it does not answer."""
    try:
        p = subprocess.run([binary, '--version'], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    found = VERSION_RE.search(p.stdout or '')
    return (int(found.group(1)),) if found else None


def availability_problem() -> Optional[str]:
    """Why this machine cannot run the WezTerm backend (None: it can) — design §4.4.

    Both binaries must be on PATH and ``wezterm --version`` at least :data:`MIN_VERSION`."""
    missing = [name for name, path in (('wezterm', _bin()), (_MUX_BIN, _mux())) if not path]
    if missing:
        return 'wezterm backend not available: ' + ', '.join(missing) + ' not found on PATH (apt install wezterm)'
    have = version_of(_bin())
    if have is None:
        return 'wezterm backend not available: `%s --version` did not answer' % _bin()
    if have < MIN_VERSION:
        return ('wezterm backend not available: need wezterm >= %d, found %d (send-text --no-paste and '
                'list --format json are older there)' % (MIN_VERSION[0], have[0]))
    return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _exe(comm: str) -> str:
    """Executable basename without ``.exe`` (the native Claude installer runs ``.../bin/claude.exe``)."""
    name = os.path.basename(comm.strip())
    return name[:-4] if name.endswith('.exe') else name


def _node_runs(args: str, names: Tuple[str, ...]) -> bool:
    """A node process whose script is one of ``names`` (same rule as the cmux backend)."""
    return any(name in args for name in names)


class WezTermSessionManager:
    """Every agent of one fleet inside one private mux server; one window, one tab per worker."""

    # Terminal backend capabilities (fleetmux.backends.base.Capabilities, design §4.3).
    supports_browser = False
    nested_tabs = False

    @classmethod
    def available(cls) -> bool:
        return availability_problem() is None

    def __init__(self, session_name: str = 'fleetmux', agents_title: str = 'Fleet agents',
                 control_title: str = 'Fleet daemon', state_dir: Optional[Path] = None):
        self.session_name = session_name
        self.agents_title = agents_title
        self.control_title = control_title
        # Registry pinned in config by fleet (same file name as the cmux backend's).
        self.meta_path = (Path(state_dir) if state_dir is not None else
                          paths.state_dir()).expanduser().resolve() / (session_name + '.json')
        self.runtime_root = None  # set by prepare_runtime(); derived from meta_path's parent + token
        self.mux_socket: Optional[str] = None
        self.mux_pid: Optional[int] = None
        self.mux_pid_file: Optional[str] = None
        self.mux_log: Optional[str] = None
        self.mux_proc: Optional[subprocess.Popen] = None
        self.mux_config: Optional[str] = None
        self.mux_workspace = self._workspace_name()
        self.last_error = ''

    # -- identity / runtime paths -------------------------------------------
    def _workspace_name(self) -> str:
        safe = ''.join(c if c.isalnum() or c in '-_' else '_' for c in self.session_name)
        return ('fleet-' + safe)[:60]

    def _token(self) -> str:
        safe = ''.join(c if c.isalnum() or c in '-_' else '_' for c in self.session_name)[:40]
        return safe or 'fleet'

    def prepare_runtime(self) -> Dict[str, str]:
        """Describe (and create) this run's private runtime: ``<state dir>/wezterm/<token>/``.

        Raises :class:`WezTermError` when the socket would be too long — no ``/tmp`` fallback (design §4.1,
        same rule as the private Redis). Idempotent."""
        if self.runtime_root:
            return {'root': self.runtime_root, 'socket': self.mux_socket}
        base = self.meta_path.parent / 'wezterm' / self._token()
        if '..' in Path(base).parts:
            raise WezTermError('private WezTerm runtime path must not contain "..": ' + str(base))
        base = base.absolute()
        socket = str(base / 'mux.sock')
        size = len(os.fsencode(socket))
        if size > SOCKET_PATH_LIMIT:
            raise WezTermError(
                'private WezTerm Unix socket path is too long (%d bytes; limit %d): %s — '
                'choose a shorter fleet state directory; no /tmp fallback' % (size, SOCKET_PATH_LIMIT, socket))
        self.runtime_root = str(base)
        self.mux_socket = socket
        self.mux_pid_file = str(base / 'mux.pid')
        self.mux_log = str(base / 'mux.log')
        self.mux_config = str(base / 'wezterm.lua')
        # The run directory itself is all we need: no fake HOME, no server pid lock of the operator's.
        # Owner-only: wezterm-mux-server refuses a runtime dir others can write to, and a plain mkdir
        # under Ubuntu's default umask (002) gives 0775.
        base.mkdir(parents=True, exist_ok=True)
        base.chmod(0o700)
        return {'root': self.runtime_root, 'socket': socket}

    def cli_env(self) -> Dict[str, str]:
        """The inherited environment plus our private socket — nothing else is rewritten.

        ``HOME`` is deliberately **not** touched: the operator's real HOME is what panes must inherit
        (credentials, profile, ``~/.gitconfig``), and no fake HOME is needed any more because the server
        is not daemonized (see ``_spawn_mux_server``). ``WEZTERM_UNIX_SOCKET`` pins every call to our own
        socket, and every ``wezterm cli`` call also carries ``--no-auto-start``, so a call can never
        reach — or start — the operator's WezTerm."""
        env = {k: v for k, v in os.environ.items() if k not in ('WEZTERM_PANE',)}
        env['WEZTERM_UNIX_SOCKET'] = self.mux_socket or ''
        env.pop('WEZTERM_CONFIG_FILE', None)  # a stale pointer into the operator's config
        env['WEZTERM_LOG'] = 'error'
        return env

    def _write_config(self) -> str:
        """The minimal config: one unix domain with our socket as the default domain."""
        conf = (
            '-- written by fleetmux: one private mux server per run (design §4.1)\n'
            'return {\n'
            '  unix_domains = {\n'
            '    { name = %s, socket_path = %s },\n'
            '  },\n'
            '  default_domain = %s,\n'
            '  default_workspace = %s,\n'
            '  check_for_updates = false,\n'
            '  automatically_reload_config = false,\n'
            '}\n' % (_lua(self.mux_workspace), _lua(self.mux_socket), _lua(self.mux_workspace), _lua(self.mux_workspace))
        )
        Path(self.mux_config).write_text(conf)
        return conf

    # -- private mux server --------------------------------------------------
    def _server_pid(self) -> Optional[int]:
        """The pid of the server this run started, as recorded in our own pid file.

        The server is a plain child of ours (``subprocess.Popen``) — not daemonized — so the pid we write
        is the Popen pid, inside the run directory. There is no ``$HOME`` trick any more: only
        ``--daemonize`` would take the operator's ``~/.local/share/wezterm/pid``, and we never daemonize.
        A pid file that does not parse or names a dead process yields ``None``."""
        if not self.mux_pid_file:
            return None
        try:
            pid = int(Path(self.mux_pid_file).read_text().strip())
        except (OSError, ValueError):
            return None
        return pid if pid > 1 else None

    def _claim_pid(self) -> Optional[int]:
        """The recorded server pid, but only when that process *is* this run's server.

        Every fleet process (start, daemon, the detached closer, a sub-fleet) builds its own manager, so a
        fresh instance learns the pid from ``mux.pid``. Before anything signals it, its command line must
        carry this run's own ``--config-file <run dir>/wezterm.lua``: a reused pid, or a pid file naming
        somebody else's process, is never claimed."""
        pid = self._server_pid()
        if pid is None or not self.mux_config:
            return None
        try:
            p = subprocess.run(['ps', '-ww', '-p', str(pid), '-o', 'command='], capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        own = re.compile(r'(?:^|\s)--config-file\s+' + re.escape(self.mux_config) + r'(?:\s|$)')
        if p.returncode != 0 or not own.search((p.stdout or '').strip()):
            return None
        return pid

    def server_running(self) -> bool:
        try:
            self.prepare_runtime()  # a fresh manager derives the run's socket / pid file first
        except WezTermError:
            return False
        pid = self.mux_pid or self._server_pid()
        return bool(pid and _pid_alive(pid) and self.mux_socket and Path(self.mux_socket).exists())

    def _spawn_mux_server(self) -> int:
        """Start ``wezterm-mux-server --config-file <ours>`` as a detached child and return its pid.

        **No ``--daemonize``**: that re-execs with ``--pid-file-fd`` and takes the pid file under
        ``$HOME/.local/share/wezterm/`` — which is the operator's own WezTerm lock. Instead the server is a
        plain ``Popen`` in a new session (``start_new_session=True``) with stdin from ``/dev/null`` and its
        output appended to ``<run dir>/mux.log``, so it survives the fleet daemon exiting (as a daemonized
        server did) but its pid is one we created and recorded ourselves. Only that pid is ever signalled."""
        self.prepare_runtime()
        self._write_config()
        binary = _mux()
        if not binary:
            raise WezTermError(_MUX_BIN + ' not found on PATH')
        cmd = [binary, '--config-file', self.mux_config]
        log = open(self.mux_log, 'ab', buffering=0)
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                    start_new_session=True, env=self.cli_env(), close_fds=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise WezTermError('could not start the private WezTerm mux server: %s' % exc) from exc
        finally:
            log.close()  # the child keeps its own descriptor
        self.mux_pid = proc.pid
        self.mux_proc = proc
        Path(self.mux_pid_file).write_text(str(proc.pid))
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise WezTermError('private WezTerm mux server exited with status %s: %s' % (
                    proc.returncode, self._log_tail()))
            if Path(self.mux_socket).exists():
                return proc.pid
            time.sleep(0.1)
        raise WezTermError('private WezTerm mux server did not come up (no socket at %s): %s' % (
            self.mux_socket, self._log_tail()))

    def _log_tail(self, limit: int = 300) -> str:
        """Last lines of this run's server log, for an error message (never another process's log)."""
        try:
            return (Path(self.mux_log).read_text(errors='replace').strip()[-limit:]) or 'no output'
        except OSError:
            return 'no log'

    def _start_mux_server(self) -> int:
        """``_spawn_mux_server`` unless the run's server is already up (started by another fleet process:
        ``start``, the daemon, a resume — each builds its own manager on the same run dir)."""
        self.prepare_runtime()
        if self.server_running():
            self.mux_pid = self.mux_pid or self._claim_pid()
            # A server started by another process: no Popen handle here, so the reap-on-stop step finds
            # nothing to wait for and relies on signals alone (and only on a claimed pid).
            return self.mux_pid or 0
        return self._spawn_mux_server()

    def end_mux_server(self) -> bool:
        """Stop the mux server this manager started: only its recorded pid, SIGTERM then SIGKILL.

        The server is our child (no ``--daemonize``), so after signalling we also **reap** it: an
        unreaped SIGTERMed child lingers as a zombie and ``kill(pid, 0)`` keeps reporting it alive.
        Only the pid we recorded is ever signalled — never anything of the operator's. The runtime
        directory is left on disk, as redis_runtime does.

        A fresh manager (the detached closer, ``subfleet.collect``) claims the pid from ``mux.pid`` only
        after :meth:`_claim_pid` has verified it is this run's server."""
        try:
            self.prepare_runtime()
        except WezTermError:
            pass
        pid = self.mux_pid or self._claim_pid()
        if pid is None:
            return True
        if not _pid_alive(pid):
            self._reap()
            self.mux_pid = None
            return True
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError):
            self._reap()
            self.mux_pid = None
            return True
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self._reap() is not None or not _pid_alive(pid):
                break
            time.sleep(0.05)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            time.sleep(0.1)
            self._reap()
        self.mux_pid = None
        self.mux_proc = None
        self._clear_server_files()
        return True

    def _reap(self, wait: float = 0.0) -> Optional[int]:
        """Reap our own child if it has exited (never another process's); return its exit status."""
        if self.mux_proc is None:
            return None
        try:
            return self.mux_proc.wait(timeout=wait) if wait else self.mux_proc.poll()
        except subprocess.TimeoutExpired:
            return None
        except (OSError, ChildProcessError):
            return None

    def _clear_server_files(self) -> None:
        """Remove this run's own pid files and socket (never a directory)."""
        for path in (self.mux_pid_file, self.mux_socket):
            try:
                Path(path).unlink()
            except (OSError, TypeError):
                pass

    # -- CLI -------------------------------------------------------------------
    def _cli(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        """Run ``wezterm cli --no-auto-start <args>`` against our private socket."""
        binary = _bin()
        if not self.mux_socket:
            self.prepare_runtime()  # a fresh manager: the run's socket is derived, never remembered
        if not binary or not self.mux_socket:
            raise WezTermError('private WezTerm mux server is not running')
        cmd = [binary, 'cli', '--no-auto-start', *args]
        return subprocess.run(cmd, capture_output=True, text=True, env=self.cli_env(), timeout=timeout)

    def _cli_ok(self, *args: str, timeout: float = 60) -> bool:
        try:
            return self._cli(*args, timeout=timeout).returncode == 0
        except (OSError, subprocess.SubprocessError, WezTermError):
            return False

    def _list(self) -> List[Dict]:
        """Every live pane of our server (``list --format json``); [] when it cannot be read."""
        try:
            p = self._cli('list', '--format', 'json')
        except (OSError, subprocess.SubprocessError, WezTermError):
            return []
        if p.returncode != 0:
            return []
        try:
            rows = json.loads(p.stdout or '[]')
        except ValueError:
            return []
        return rows if isinstance(rows, list) else []

    def _row(self, pane_id) -> Optional[Dict]:
        for row in self._list():
            if str(row.get('pane_id')) == str(pane_id):
                return row
        return None

    def _workspace_of(self, pane_id) -> Optional[str]:
        row = self._row(pane_id)
        return row.get('workspace') if row else None

    def _surfaces(self, workspace: Optional[str]) -> Dict[str, Dict]:
        """{pane id: row} of the live panes in ``workspace`` (what lifecycle_close.all_gone re-checks)."""
        if not workspace:
            return {}
        return {str(row.get('pane_id')): row for row in self._list() if row.get('workspace') == workspace}

    def _in_run(self, pane_id) -> Optional[Dict]:
        """The pane's row if it is live *and* in this run's workspace (else None: not ours)."""
        row = self._row(pane_id)
        if not row or row.get('workspace') != self.mux_workspace:
            return None
        return row

    # -- ownership registry -------------------------------------------------------
    def _meta(self) -> Dict:
        try:
            return json.loads(self.meta_path.read_text())
        except (OSError, ValueError):
            return {'workspace': self.mux_workspace, 'control_workspace': None, 'surfaces': {}}

    def _save_meta(self, meta: Dict) -> None:
        self.meta_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.meta_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        tmp.replace(self.meta_path)

    def _tag(self, sid: str, **info) -> None:
        meta = self._meta()
        entry = meta.setdefault('surfaces', {}).setdefault(str(sid), {})
        entry.update({k: v for k, v in info.items() if isinstance(v, str)})
        meta.setdefault('workspace', self.mux_workspace)
        self._save_meta(meta)

    def _forget(self, sid: str) -> None:
        meta = self._meta()
        if meta.get('surfaces', {}).pop(str(sid), None) is not None:
            self._save_meta(meta)

    def owns(self, sid: str) -> bool:
        """A pane this registry spawned and the server still shows in our workspace."""
        if str(sid) not in (self._meta().get('surfaces') or {}):
            return False
        return self._in_run(sid) is not None

    # -- session (design §2) ---------------------------------------------------------
    def session_exists(self) -> bool:
        return self.server_running() and bool(self._list())

    def get_or_create_session(self) -> bool:
        """Start the run's private mux server; False (with ``last_error``) when it cannot come up."""
        problem = availability_problem()
        if problem:
            self.last_error = problem
            return False
        try:
            self._start_mux_server()
        except WezTermError as exc:
            self.last_error = str(exc)
            return False
        self.mux_workspace = self._workspace_name()
        meta = self._meta()
        meta['workspace'] = self.mux_workspace
        self._save_meta(meta)
        return True

    def claim_run(self, run: str) -> None:
        meta = self._meta()
        meta['run'] = run
        self._save_meta(meta)

    def workspace_id(self) -> Optional[str]:
        return self._meta().get('workspace') or self.mux_workspace

    def select_workspace(self, workspace: str) -> bool:
        """WezTerm has no view to switch for the operator (no GUI client); succeed when ours exists."""
        return bool(workspace) and workspace in [row.get('workspace') for row in self._list()]

    def keep_windows_open(self, ours: List[str]) -> bool:
        """Nothing to do: killing our panes never closes a window the operator needs (our server is ours)."""
        return True

    # -- spawning --------------------------------------------------------------------
    def _launcher(self, name: str, script: str, folder: Optional[Path] = None) -> str:
        """Write ``script`` to a launcher file and return the short command that runs it (cmux's rule:
        keep the argv the terminal receives short)."""
        folder = folder or (self.meta_path.parent / 'launch')
        folder.mkdir(parents=True, exist_ok=True)
        safe = ''.join(c if c.isalnum() or c in '-_.' else '_' for c in name) or 'agent'
        path = folder / (safe + '-' + uuid.uuid4().hex[:6] + '.sh')
        path.write_text('#!/bin/bash\n' + script + '\n')
        path.chmod(0o700)
        return 'exec /bin/bash ' + shlex.quote(str(path))

    def _spawn(self, window_name: str, command: str, metadata: Optional[Dict[str, str]],
               start_directory: Optional[str], new_window: bool, anchor: Optional[str] = None) -> 'object':
        from fleetmux.backends.base import SpawnResult
        try:
            self.prepare_runtime()
        except WezTermError as exc:
            self.last_error = str(exc)
            return SpawnResult(success=False, error=str(exc))
        try:
            self._start_mux_server()  # cmux parity: the run's first window creates the session
        except WezTermError as exc:
            self.last_error = str(exc)
            return SpawnResult(success=False, error=str(exc))
        full = self._launcher(window_name, 'cd ' + shlex.quote(start_directory or '.') + ' && exec ' + command)
        args = ['spawn']
        if new_window:
            args += ['--new-window', '--workspace', self.mux_workspace]
        else:
            peer = anchor or self._any_pane()
            if peer is None:
                args += ['--new-window', '--workspace', self.mux_workspace]
            else:
                args += ['--pane-id', str(peer)]
        args += ['--cwd', str(start_directory or os.getcwd()), '--', 'bash', '-lc', full]
        try:
            p = self._cli(*args)
        except (OSError, subprocess.SubprocessError, WezTermError) as exc:
            self.last_error = 'wezterm cli spawn failed: %s' % exc
            return SpawnResult(success=False, error=self.last_error)
        pane = (p.stdout or '').strip().splitlines()
        pane_id = pane[-1].strip() if pane else ''
        if p.returncode != 0 or not pane_id.isdigit():
            self.last_error = 'wezterm cli spawn failed: ' + ((p.stderr or p.stdout or '').strip()[:300] or 'no pane id')
            return SpawnResult(success=False, error=self.last_error)
        self._tag(pane_id, type=(metadata or {}).get('type', ''), label=window_name,
                  window_name=window_name, workspace=self.mux_workspace)
        if not _wait(lambda: self._in_run(pane_id) is not None, 10):
            return SpawnResult(success=False, pane_id=pane_id, error='pane did not appear in the run workspace')
        self.rename(pane_id, window_name)
        return SpawnResult(success=True, pane_id=pane_id, window_name=window_name)

    def _any_pane(self) -> Optional[str]:
        """A live pane of ours to anchor a new tab on: the supervisor's tab first, else any pane."""
        rows = [row for row in self._list() if row.get('workspace') == self.mux_workspace]
        if not rows:
            return None
        surfaces = self._meta().get('surfaces') or {}
        for preference in ('supervisor', 'daemon'):
            for row in rows:
                info = surfaces.get(str(row.get('pane_id'))) or {}
                if info.get('type') == preference or info.get('window_name', '').startswith(preference):
                    return str(row.get('pane_id'))
        for row in rows:
            if str(row.get('pane_id')) in surfaces:
                return str(row.get('pane_id'))
        return str(rows[0].get('pane_id'))

    def spawn_window(self, window_name: str, command: str, metadata: Optional[Dict[str, str]] = None,
                     start_directory: Optional[str] = None):
        """First pane of the run opens the window (in the run's workspace); the rest are tabs in it."""
        new_window = self._any_pane() is None
        return self._spawn(window_name, command, metadata, start_directory, new_window)

    def spawn_interactive_cli(self, window_name: str, cli_command: str, prompt_file: Optional[str] = None,
                              output_file: Optional[str] = None, metadata: Optional[Dict[str, str]] = None,
                              start_directory: Optional[str] = None, auto_close: bool = True,
                              env_vars: Optional[Dict[str, str]] = None, tmux_socket: Optional[str] = None,
                              tmux_session: Optional[str] = None, **_) -> 'object':
        """Same command shape as the cmux backend; tmux views are not built here (no client tab to attach)."""
        from fleetmux.backends.base import SpawnResult
        if tmux_socket and tmux_session:
            # A named sub-fleet's tmux client: supported through the same spawn path, without cmux's
            # owner/view bookkeeping (WezTerm tabs are plain panes of our server).
            from fleetmux.backends.cmux import tmux_attach_command, tmux_sessions, _tmux
            if tmux_session not in tmux_sessions(tmux_socket):
                p = _tmux(tmux_socket, 'new-session', '-d', '-s', tmux_session, '-c', start_directory or '.',
                          'bash', '-lc', cli_command)
                if p.returncode:
                    return SpawnResult(success=False, error='tmux new-session failed: ' + (p.stderr or p.stdout).strip())
            res = self._spawn(window_name, tmux_attach_command(tmux_socket, tmux_session), metadata,
                              start_directory, self._any_pane() is None)
            if res.success:
                self._tag(res.pane_id, tmux_socket=str(tmux_socket), tmux_session=tmux_session, tmux_role='owner',
                          output_file=output_file or '')
            return res
        inner = cli_command
        if prompt_file:
            inner = f'{cli_command} "$(<{prompt_file})"'
        if not auto_close:
            inner = f'{inner}; exec {os.environ.get("SHELL") or "/bin/bash"} -l'
        env = ' '.join(shlex.quote(k + '=' + str(v)) for k, v in (env_vars or {}).items() if v is not None)
        command = ('env ' + env + ' ' if env else '') + 'bash -lc ' + shlex.quote(inner)
        res = self._spawn(window_name, command, metadata, start_directory, self._any_pane() is None)
        if res.success:
            self._tag(res.pane_id, output_file=output_file or '')
        return res

    # -- reading / writing ---------------------------------------------------------------
    def _send(self, pane_id: str, text: str, no_paste: bool) -> bool:
        if self._in_run(pane_id) is None:
            return False
        args = ['send-text', '--pane-id', str(pane_id)]
        if no_paste:
            args.append('--no-paste')
        return self._cli_ok(*args, '--', text)  # '--': a text starting with '-' is not an option

    def send_prompt_to_idle(self, pane_id: str, text: str, bracketed: bool = False) -> bool:
        """Text, then Enter as its own CR (a newline inside send-text is LF, never CR — probe §2.2)."""
        from fleetmux import claude_input
        if claude_input.required(self, pane_id):
            return claude_input.send(self, pane_id, text,
                lambda: self._send(pane_id, text, no_paste=not (bracketed or text != '/clear')))
        if not self._send(pane_id, text, no_paste=not bracketed):
            return False
        time.sleep(1 if bracketed else 0.2)  # paste-burst detection needs the gap before Enter
        return self._send(pane_id, '\r', no_paste=True)

    def send_key(self, pane_id: str, key: str) -> bool:
        """A real keystroke: ``send-text --no-paste`` with the key's bytes (Esc / Ctrl-C / arrows measured
        to arrive byte-exact). An unknown key returns False and types nothing."""
        payload = KEYS.get(key)
        if payload is None:
            if len(key) == 1:  # a single character is unambiguous
                payload = key
            elif re.fullmatch(r'(C|Ctrl)-[a-zA-Z]', key):
                payload = chr(ord(key[-1].lower()) - ord('a') + 1)
            else:
                return False
        return self._send(pane_id, payload, no_paste=True)

    def capture_output(self, pane_id: str, lines: int = 2000) -> str:
        if self._in_run(pane_id) is None:
            return ''
        try:
            p = self._cli('get-text', '--pane-id', str(pane_id), '--start-line', '-' + str(int(lines)))
        except (OSError, subprocess.SubprocessError, WezTermError):
            return ''
        return p.stdout if p.returncode == 0 else ''

    def capture_to(self, pane_id: str, path: str) -> None:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(self.capture_output(pane_id, 100000), encoding='utf-8')

    # -- state -----------------------------------------------------------------------
    def _tty(self, pane_id) -> Optional[str]:
        row = self._in_run(pane_id)
        return row.get('tty_name') if row else None

    def get_pane_status(self, pane_id: str):
        from fleetmux.backends.base import PaneStatus
        row = self._in_run(pane_id)
        if row is None:
            return None
        info = self._meta().get('surfaces', {}).get(str(pane_id)) or {}
        meta = {k: v for k, v in info.items() if isinstance(v, str)}
        meta.update({'tty_name': row.get('tty_name') or '', 'workspace': row.get('workspace') or '',
                     'tab_id': str(row.get('tab_id', '')), 'window_id': str(row.get('window_id', '')),
                     'tab_title': row.get('tab_title') or '', 'cwd': row.get('cwd') or ''})
        return PaneStatus(pane_id=str(pane_id), window_name=info.get('label') or info.get('window_name') or '',
                          alive=True, metadata=meta)

    def _ps(self, tty: Optional[str], fmt: str) -> List[str]:
        if not tty:
            return []
        try:
            p = subprocess.run(['ps', '-ww', '-t', os.path.basename(tty), '-o', fmt],
                               capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return []
        return [line for line in p.stdout.splitlines() if line.strip()] if p.returncode == 0 else []

    def has_cli_process(self, pane_id: str, names: Tuple[str, ...] = ('codex',)) -> bool:
        """Is one of ``names`` running on the pane's tty? A bare shell never matches (cmux's rule)."""
        tty = self._tty(pane_id)
        if not tty:
            return False
        exes = [_exe(line) for line in self._ps(tty, 'comm=')]
        if any(e in names for e in exes):
            return True
        if 'node' not in exes:
            return False
        return any(_node_runs(line, names) for line in self._ps(tty, 'args='))

    def process_states(self, pane_id: str) -> List[str]:
        """``ps`` STAT of the processes on the pane's tty (watchdog frozen check)."""
        return [line.strip() for line in self._ps(self._tty(pane_id), 'stat=')]

    def process_cpu(self, pane_id: str) -> Optional[float]:
        """Cumulative CPU seconds of the pane's tty tree; None when unreadable."""
        from fleetmux.watchdog import pane_tty_cpu
        tty = self._tty(pane_id)
        return pane_tty_cpu([tty]) if tty else None

    # -- label / focus --------------------------------------------------------------
    def rename(self, pane_id: str, label: str) -> None:
        if self._in_run(pane_id) is not None:
            self._cli_ok('set-tab-title', '--pane-id', str(pane_id), label)
        self._tag(str(pane_id), label=label)

    def focus(self, pane_id: str) -> bool:
        if self._in_run(pane_id) is None:
            return False
        return self._cli_ok('activate-pane', '--pane-id', str(pane_id))

    def kill_pane(self, pane_id: str) -> bool:
        """Kill a pane we spawned (never one we did not); save its final screen when an output file is set."""
        meta = self._meta()
        info = (meta.get('surfaces') or {}).get(str(pane_id))
        if info is None:
            return False
        if self._in_run(pane_id) is None:  # already gone (kill-pane by hand / server gone)
            self._forget(pane_id)
            return True
        if info.get('output_file'):
            try:
                with open(info['output_file'], 'a', encoding='utf-8') as fh:
                    fh.write(self.capture_output(pane_id, 100000))
            except OSError:
                pass
        if not self._cli_ok('kill-pane', '--pane-id', str(pane_id)):
            return False  # keep ownership so a failed kill can be retried
        if not _wait(lambda: self._row(pane_id) is None, 5):
            return False
        self._forget(pane_id)
        return True

    # -- layout (§4.3) ---------------------------------------------------------------
    def place_right_of(self, pane_id: str, peer: str) -> bool:
        """Put an agent beside its peer in the same tab (a right-hand split)."""
        row, peer_row = self._in_run(pane_id), self._in_run(peer)
        if row is None or peer_row is None:
            return False
        if _same_tab(row, peer_row) and row.get('left_col', 0) != peer_row.get('left_col', 0):
            return True  # already side by side
        tid = self._move_to_new_tab(pane_id, peer_row.get('window_id'))
        if not tid:
            return False
        return self._cli_ok('split-pane', '--pane-id', str(peer), '--right', '--percent', '50',
                            '--move-pane-id', str(pane_id))

    def place_below(self, pane_id: str, peer: str) -> bool:
        """Put a pane below its peer in the peer's tab (the control pane's place)."""
        peer_row = self._in_run(peer)
        if self._in_run(pane_id) is None or peer_row is None:
            return False
        return self._cli_ok('split-pane', '--pane-id', str(peer), '--bottom', '--percent', '40',
                            '--move-pane-id', str(pane_id))

    def _move_to_new_tab(self, pane_id: str, window_id) -> Optional[str]:
        """Move a pane into a tab of its own (the only way WezTerm has to re-place a pane), return the tab."""
        try:
            p = self._cli('move-pane-to-new-tab', '--pane-id', str(pane_id), '--window-id', str(window_id))
        except (OSError, subprocess.SubprocessError, WezTermError):
            return None
        if p.returncode != 0:
            return None
        return (p.stdout or '').strip() or '1'

    def tab_into(self, pane_id: str, peer: str, before: bool = True) -> bool:
        """WezTerm has no tabs-in-a-split: put the pane into the peer's tab (``before`` only picks whether it
        is moved to a new tab first and split in beside the peer, or joined as the peer's tab-mate)."""
        row, peer_row = self._in_run(pane_id), self._in_run(peer)
        if row is None or peer_row is None:
            return False
        if _same_tab(row, peer_row):
            return True
        if before:
            # A successor takes its predecessor's place: same tab as the peer, side by side.
            moved = self._move_to_new_tab(pane_id, peer_row.get('window_id'))
            if not moved:
                return False
            return self._cli_ok('split-pane', '--pane-id', str(peer), '--right', '--percent', '50',
                                '--move-pane-id', str(pane_id))
        # before=False: the API has no "move pane into an existing tab"; a tab of its own is the
        # closest honest rendering, and the caller only needs the panes to be distinct tabs side by side.
        return bool(self._move_to_new_tab(pane_id, peer_row.get('window_id')))

    def tab_panes(self) -> Dict[str, str]:
        """{pane id: tab title} of the companion tabs this registry owns and the server still shows."""
        return {sid: row.get('tab_title') or '' for sid, info in (self._meta().get('surfaces') or {}).items()
                if info.get('type') == 'tab' and (row := self._in_run(sid)) is not None}

    def dock_tab(self, sid: str, history_surface: str, after: Optional[str] = None) -> bool:
        """No history pane to dock into (§4.3): keep the companion tab as its own tab. Success."""
        _ = (history_surface, after)
        if not sid:
            return False
        if self._in_run(sid) is not None:
            self._tag(str(sid), type='tab')
        return True

    def dock_control(self, history_surface: str) -> bool:
        """Put the control (daemon) pane below the supervisor in its tab — the degraded page of §4.3."""
        sup = self._supervisor_pane()
        controls = [sid for sid, info in (self._meta().get('surfaces') or {}).items()
                    if info.get('type') == 'daemon' and self._in_run(sid) is not None]
        if sup is None or not controls:
            return True  # nothing to dock yet: not an error
        for sid in controls:
            row, sup_row = self._in_run(sid), self._in_run(sup)
            if _same_tab(row, sup_row) and row.get('top_row', 0) > sup_row.get('top_row', 0):
                continue  # already below
            if not self.place_below(sid, sup):
                return False
        _ = history_surface
        return True

    def _supervisor_pane(self) -> Optional[str]:
        surfaces = self._meta().get('surfaces') or {}
        for sid, info in surfaces.items():
            if info.get('type') == 'supervisor' and self._in_run(sid) is not None:
                return sid
        for sid, info in surfaces.items():
            if str(info.get('window_name', '')).startswith('supervisor') and self._in_run(sid) is not None:
                return sid
        return None

    def arrange(self, supervisor: str, workers: List[str], history_url: str) -> Optional[str]:
        """One window, the supervisor's tab, one tab per worker, control below the supervisor (§4.3).

        Returns None: there is no history surface to show (``supports_browser`` False), which tells fleet
        to print ``history.html``'s path instead of a browser tab."""
        _ = history_url
        sup_row = self._in_run(supervisor)
        if sup_row is None:
            return None
        if any(self._in_run(w) is None for w in workers):
            return None
        # One window: bring every agent into the supervisor's window.
        for pane_id in list(workers) + [sid for sid, info in (self._meta().get('surfaces') or {}).items()
                                        if info.get('type') == 'daemon' and self._in_run(sid) is not None]:
            row = self._in_run(pane_id)
            if row and row.get('window_id') != sup_row.get('window_id'):
                moved = self._move_to_new_tab(pane_id, sup_row.get('window_id'))
                if not moved:
                    return None
        for worker in workers:
            row = self._in_run(worker)
            if _same_tab(row, sup_row):  # a worker must not share the supervisor's tab
                moved = self._move_to_new_tab(worker, sup_row.get('window_id'))
                if not moved:
                    return None
            self.rename(worker, (self._meta()['surfaces'].get(worker) or {}).get('window_name') or
                        (self._meta()['surfaces'].get(worker) or {}).get('label') or 'worker')
        surfaces = self._meta().get('surfaces') or {}
        for sid, info in surfaces.items():
            if info.get('type') in ('worker', 'tab', 'supervisor') and self._in_run(sid) is not None:
                label = info.get('label') or info.get('window_name')
                if label:
                    self._cli_ok('set-tab-title', '--pane-id', sid, label)
        self.focus(supervisor)
        self.dock_control(supervisor)
        return None

    def place_after(self, workspace: Optional[str]) -> bool:
        """Workspace order is a sidebar in cmux; there is none here (§4.3): success, nothing to do."""
        _ = workspace
        return True

    def close_all(self) -> bool:
        """Kill every pane we spawned, then stop our private mux server (its pid, only)."""
        meta = self._meta()
        ok = True
        for sid in list((meta.get('surfaces') or {})):
            if not self.kill_pane(sid):
                ok = False
        return self.end_mux_server() and ok

    # -- history page (no browser surface, §4.3) --------------------------------------
    def browser_goto(self, surface: str, url: str) -> bool:
        _ = (surface, url)
        return False

    def browser_url(self, surface: str) -> Optional[str]:
        _ = surface
        return None

    def reload_browser(self, surface: str) -> bool:
        _ = surface
        return False

    # -- handover between fleets -------------------------------------------------------
    def release(self, pane_id: str) -> Optional[Dict]:
        """Give up ownership of a live pane without closing it (another fleet's registry adopts it)."""
        meta = self._meta()
        info = (meta.get('surfaces') or {}).pop(str(pane_id), None)
        if info is not None:
            self._save_meta(meta)
        return info

    def adopt(self, pane_id: str, peer: str, info: Dict) -> bool:
        """Take over a released pane and put it in ``peer``'s tab (its window)."""
        peer_row = self._in_run(peer)
        if peer_row is None or self._in_run(pane_id) is None:
            return False
        row = self._in_run(pane_id)
        if not _same_tab(row, peer_row):
            if not self._move_to_new_tab(pane_id, peer_row.get('window_id')):
                return False
        self._tag(str(pane_id), **{k: v for k, v in (info or {}).items() if isinstance(v, str)})
        return True

    # -- tmux views (named sub-fleets) --------------------------------------------------
    def _tmux_of(self, sid: str) -> Optional[Dict[str, str]]:
        info = (self._meta().get('surfaces') or {}).get(str(sid)) or {}
        return info if info.get('tmux_socket') and info.get('tmux_session') else None

    def tmux_view_info(self, sid: str) -> Optional[Dict[str, str]]:
        tm = self._tmux_of(sid)
        if not tm:
            return None
        return {k: v for k, v in tm.items() if k in ('type', 'label', 'tmux_socket', 'tmux_session')}

    def attach_view(self, info: Dict[str, str], peer: str) -> Optional[str]:
        """A second tmux client in a tab of ours (same tmux server, same session)."""
        from fleetmux.backends.cmux import tmux_attach_command, tmux_sessions
        sock, session = (info or {}).get('tmux_socket'), (info or {}).get('tmux_session')
        if not sock or not session or self._in_run(peer) is None or session not in tmux_sessions(sock):
            return None
        res = self._spawn('view-' + session, tmux_attach_command(sock, session), None, None, False, anchor=peer)
        if not res.success:
            return None
        self._tag(res.pane_id, **{k: v for k, v in info.items() if isinstance(v, str) and k != 'output_file'},
                  tmux_role='view')
        if not self.tab_into(res.pane_id, peer):
            self.kill_pane(res.pane_id)
            return None
        return res.pane_id

    def tmux_session_alive(self, sid: str) -> Optional[bool]:
        from fleetmux.backends.cmux import _tmux
        tm = self._tmux_of(sid)
        if not tm:
            return None
        if not Path(tm['tmux_socket']).exists():
            return False
        try:
            p = _tmux(tm['tmux_socket'], 'list-sessions', '-F', '#{session_name}')
        except OSError:
            return None
        if p.returncode == 0:
            return tm['tmux_session'] in p.stdout.split()
        return False if 'no server running' in p.stderr + p.stdout else None

    def reattach_client(self, sid: str, peer: str) -> Optional[str]:
        """``sid``'s tab was closed while its tmux session runs on: open a new client tab, take over its entry."""
        tm = self._tmux_of(sid)
        if not tm or self._in_run(peer) is None or self._in_run(sid) is not None:
            return None
        new = self.attach_view(self.tmux_view_info(sid), peer)
        if not new:
            return None
        meta = self._meta()
        if sid in meta['surfaces'] and new in meta['surfaces']:
            meta['surfaces'][new] = meta['surfaces'].pop(sid)
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
        row = self._in_run(sid)
        if row is not None:
            tty = row.get('tty_name')
            if tty:
                _tmux(tm['tmux_socket'], 'detach-client', '-t', tty)
            if not self._cli_ok('kill-pane', '--pane-id', str(sid)):
                return False
            if not _wait(lambda: self._row(sid) is None, 5):
                return False
        self._forget(sid)
        return True

    # -- lifecycle helpers ---------------------------------------------------------------
    def stop(self) -> bool:
        """What the daemon's finally block calls: close our panes and stop our private mux server."""
        return self.close_all()


def _lua(value: Optional[str]) -> str:
    return "'" + str(value or '').replace('\\', '\\\\').replace("'", "\\'") + "'"


def _same_tab(a: Optional[Dict], b: Optional[Dict]) -> bool:
    return bool(a and b and a.get('tab_id') == b.get('tab_id') and a.get('window_id') == b.get('window_id'))


def _wait(check, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if check():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
