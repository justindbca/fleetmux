"""cmux backend: every agent lives in a cmux tab/split that the user can close with the ✕.

cmux only accepts commands from processes started inside a cmux window, so the daemon
must run in a cmux surface. Every call is scoped by workspace UUID (refs such as
``surface:N`` renumber), and every destructive or typing call first checks that the
surface is one this manager spawned. Ownership is kept in
``~/.fleetmux/state/<session>.json`` (legacy ``~/.local/state/cmux-fleet``, cmux_fleet/paths.py).

Hard-won cmux facts (all measured, not documented):

* ``send`` interprets ``\\n`` / ``\\t`` escapes, so verbatim text goes through
  ``set-buffer`` + ``paste-buffer``.
* Do **not** wrap pasted text in bracketed-paste markers (``ESC[200~`` ... ``ESC[201~``).
  The Codex TUI treats the ESC as a bare Escape key and shows ``[200~`` literally in the
  composer (verified on codex-cli 0.156.1). Codex's own paste-burst detection already
  keeps a multi-line paste in the composer until the explicit Enter.
* Claude Code is the opposite (measured on 2.1.280): an unbracketed multi-line paste was
  once left unsubmitted and once split into two prompts at the newline. A bracketed paste
  arrives as one prompt and the following Enter submits it, so Claude panes use
  ``paste-buffer --bracketed``.
* ``new-workspace --focus false`` still switches the user's view; select the previous
  workspace back afterwards.
* ``pipe-pane`` is a one-shot snapshot, not a stream. The final screen is saved when a
  surface is closed; the real transcript is Codex's rollout file.
* Start a surface's program with ``new-surface --command`` / ``new-workspace --command``
  (cmux ≥ 0.64.25): cmux types the command into the login shell once the prompt is up.
  Typing it ourselves races the shell startup; typed-ahead text gets truncated and leaves
  the shell waiting at a quote prompt (seen on a slow login shell). The typing path is kept
  only as a fallback for older cmux, and it fails closed when no prompt appears.
* Keep the typed command short. Before the line editor is up, macOS tty input is limited to
  1024 bytes per line, and a ~1.5 kB command (an inlined PATH) was silently truncated or
  left sitting unexecuted at the prompt. Every spawn therefore writes a small launcher
  script and the surface runs ``exec bash <launcher>``.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from cmux_fleet import paths

CMUX = os.environ.get('CMUX_BIN') or '/Applications/cmux.app/Contents/Resources/bin/cmux'
STATE_DIR = paths.state_dir()  # $CMUX_FLEET_STATE, else ~/.fleetmux/state (legacy ~/.local/state/cmux-fleet)
TMUX = os.environ.get('CMUX_FLEET_TMUX') or shutil.which('tmux') or '/opt/homebrew/bin/tmux'
# A tmux server exits by itself when its last session ends; a client landing mid-shutdown reads
# 'server exited unexpectedly'. tmux_session_alive asks again this many times, 0.1 s apart.
TMUX_SHUTDOWN_RETRIES = 3
# The fleet's own tmux server (docs/design.md §19 dual display): never the user's ~/.tmux.conf, whose
# resurrect / continuum plugins would copy the user's sessions into it and overwrite their saves.
TMUX_CONF = 'set -g window-size latest\nset -g history-limit 50000\n'


@dataclass
class SpawnResult:
    success: bool
    pane_id: Optional[str] = None
    window_name: Optional[str] = None
    error: Optional[str] = None


@dataclass
class PaneStatus:
    pane_id: str
    window_name: str
    alive: bool
    exit_code: Optional[int] = None
    metadata: Dict[str, str] = field(default_factory=dict)


def _run(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, CMUX_QUIET='1')
    return subprocess.run([CMUX, *args], capture_output=True, text=True, env=env)


def _wait_until(check, timeout: float = 3.0) -> bool:
    """cmux acknowledges mutations before tree reflects them; wait, never repeat the mutation."""
    deadline = time.monotonic() + timeout
    while True:
        if check():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(.05)


def cmux_available() -> bool:
    try:
        return _run('ping').stdout.strip() == 'PONG'
    except OSError:
        return False


# -- the fleet's own tmux server (a sub-fleet supervisor_slot shown in two workspaces) -------------
def tmux_session_name(slot: str, generation) -> str:
    """``<slot>-g<generation>``: a ``.`` (pane separator) or ``:`` (window separator) breaks ``-t``."""
    safe = ''.join(c if c.isalnum() or c in '-_' else '_' for c in slot)
    return safe + '-g' + str(int(generation))


def tmux_conf(sock: str) -> Path:
    """The minimal config next to the socket (written on first use)."""
    conf = Path(sock).with_name('tmux.conf')
    if not conf.exists():
        conf.parent.mkdir(parents=True, exist_ok=True)
        conf.write_text(TMUX_CONF)
    return conf


def _tmux(sock: str, *args: str) -> subprocess.CompletedProcess:
    # Always -f: any tmux command may start a server, which would otherwise read ~/.tmux.conf.
    env = {k: v for k, v in os.environ.items() if k != 'TMUX'}
    return subprocess.run([TMUX, '-S', str(sock), '-f', str(tmux_conf(sock)), *args],
                          capture_output=True, text=True, env=env)


def tmux_attach_command(sock: str, session: str) -> str:
    """A client tab's command. ``exec``: cmux types it into a login shell; a detached client then ends the
    tab instead of dropping back to the prompt."""
    # '=name' (tmux exact match) must be quoted by hand: shlex leaves a leading '=' bare, and zsh (the macOS
    # login shell) expands '=word' to a command path -> "name not found", the tab drops to a prompt (10-14 Studio).
    return ('exec ' + shlex.join([TMUX, '-S', str(sock), '-f', str(tmux_conf(sock)), 'attach', '-t'])
            + " '=" + session.replace("'", "'\\''") + "'")


def tmux_sessions(sock: str) -> List[str]:
    if not Path(sock).exists():
        return []
    p = _tmux(sock, 'list-sessions', '-F', '#{session_name}')
    return p.stdout.split() if p.returncode == 0 else []


def tmux_clients(sock: str, session: str) -> List[str]:
    """The ttys of the clients attached to ``session`` ([] when none or tmux could not be read)."""
    p = _tmux(sock, 'list-clients', '-t', '=' + session, '-F', '#{client_tty}')
    return p.stdout.split() if p.returncode == 0 else []


def tmux_give_back(sock: str, session: str, before: List[str], timeout: float = 3.0) -> bool:
    """A client just attached to ``session`` in a tab nobody looks at (another fleetmux page, another cmux
    workspace); tmux (``window-size latest``) sized the one window to it, so the tab in view shows rows / columns
    of ``·``. Give the window back to the clients that were there before (``before``: their ttys):
    ``switch-client`` counts as their activity. Waits for the new client first; repeats until the window has the
    old client's size (less tmux's status line) or ``timeout``. False: not proven."""
    def done(tty):
        p = _tmux(sock, 'display-message', '-p', '-c', tty,
                  '#{window_width} #{window_height} #{client_width} #{client_height}')
        try:
            ww, wh, cw, ch = map(int, p.stdout.split())
        except ValueError:
            return False
        return ww == cw and ch - wh in (0, 1)
    if not _wait_until(lambda: set(tmux_clients(sock, session)) - set(before), timeout):
        return False
    old = [t for t in tmux_clients(sock, session) if t in before]

    def give():
        for tty in old:
            _tmux(sock, 'switch-client', '-c', tty, '-t', '=' + session)
        time.sleep(.1)  # the new client's own resize may still come in after this switch
        return all(done(tty) for tty in old)
    return _wait_until(give, timeout)


def tmux_kill_server(sock: str) -> bool:
    """Last step of a fleet's exit; True when no server is left on ``sock``."""
    if Path(sock).exists():
        _tmux(sock, 'kill-server')
    return not tmux_sessions(sock)


# tmux never unlinks its socket (kill-server, or a server ending with its last session), so the fleet's stop removes
# it (brief-m4 §7.1) -- but only on proof that no server is there. tmux_sessions() is no such proof: it answers [] for
# a live server without sessions and for any error.
TMUX_PROBE_TIMEOUT = 2.0  # one list-sessions / connect(); longer = uncertain
TMUX_GONE_SETTLE = 1.0  # kill-server may return just before the server stops listening: ask again for this long


def _tmux_probe_once(sock: str) -> Optional[bool]:
    env = {k: v for k, v in os.environ.items() if k != 'TMUX'}
    try:  # no -f: list-sessions never starts a server, and no tmux.conf is written beside a foreign path
        p = subprocess.run([TMUX, '-S', str(sock), 'list-sessions'], capture_output=True, text=True, env=env,
                           timeout=TMUX_PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode == 0:
        return False  # a server answered, with or without sessions
    if 'no server running on' not in p.stderr and '(No such file or directory)' not in p.stderr:
        return None  # e.g. 'server exited unexpectedly', a permission error
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)  # second witness: the errno itself
    s.settimeout(TMUX_PROBE_TIMEOUT)
    try:
        s.connect(str(sock))
    except (ConnectionRefusedError, FileNotFoundError):
        return True
    except OSError:  # timeout included
        return None
    finally:
        s.close()
    return False


def tmux_server_gone(sock: str, settle: Optional[float] = None) -> Optional[bool]:
    """True: no tmux server on ``sock`` ('no server running' / ECONNREFUSED / ENOENT, both from tmux and from a
    connect()); False: a server is there; None: not known (timeout, any other answer). Asks again for up to
    ``settle`` seconds (default TMUX_GONE_SETTLE) while the answer is not True."""
    deadline = time.monotonic() + (TMUX_GONE_SETTLE if settle is None else settle)
    while True:
        gone = _tmux_probe_once(sock)
        if gone is True or time.monotonic() >= deadline:
            return gone
        time.sleep(.1)


def _tree_pids(pid: int) -> List[int]:
    pids = [pid]
    for p in pids:
        result = subprocess.run(['pgrep', '-P', str(p)], capture_output=True, text=True)
        pids += [int(c) for c in result.stdout.split() if c.isdigit()]
    return pids


def kill_process_tree(pid: int, term_timeout: float = 3.0) -> None:
    """SIGTERM a process and all its descendants, then SIGKILL whatever survives."""
    pids: List[int] = []

    def collect(parent: int) -> None:
        try:
            result = subprocess.run(['pgrep', '-P', str(parent)], capture_output=True, text=True)
        except OSError:
            result = None
        if result and result.returncode == 0:
            for child in result.stdout.splitlines():
                try:
                    collect(int(child))
                except ValueError:
                    continue
        pids.append(parent)

    collect(pid)
    for p in pids:
        try:
            os.kill(p, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + term_timeout
    alive = pids
    while alive and time.monotonic() < deadline:
        time.sleep(0.05)
        alive = []
        for p in pids:
            try:
                os.kill(p, 0)
            except ProcessLookupError:
                continue
            alive.append(p)
    for p in alive:
        try:
            os.kill(p, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _exe(comm: str) -> str:
    """Executable basename without ``.exe`` (the native Claude installer runs ``.../bin/claude.exe``)."""
    name = Path(comm.strip()).name
    return name[:-4] if name.endswith('.exe') else name


def _node_runs(args: str, names: Tuple[str, ...]) -> bool:
    """``node <script>`` whose script path names one of the CLIs (npm installs); not any node process."""
    words = args.split()
    return len(words) > 1 and _exe(words[0]) == 'node' and any(n in words[1] for n in names)


class CmuxSessionManager:
    """Agents in one workspace; dock the daemon into an inactive history-pane tab."""

    # Terminal backend capabilities (cmux_fleet.backends.base.Capabilities): cmux renders a real
    # browser surface for history.html and can put tabs inside a split (design §2, §4.3).
    supports_browser = True
    nested_tabs = True

    # Interface method (design §2): can this backend run here? For cmux that is ``cmux_available()``
    # (cmux only accepts commands from processes started inside a cmux window). A plain function, not a
    # classmethod, so ``monkeypatch.setattr(CmuxSessionManager, 'available', ...)`` keeps working like the
    # module-level function it stands for; ``mgr.available()`` reads the class attribute, not the function.
    available = staticmethod(cmux_available)

    def __init__(self, session_name: str = 'cmux-fleet', agents_title: str = 'Fleet agents',
                 control_title: str = 'Fleet daemon', state_dir: Optional[Path] = None):
        self.session_name = session_name
        self.agents_title = agents_title
        self.control_title = control_title
        self.meta_path = (Path(state_dir) if state_dir is not None else STATE_DIR).expanduser().resolve() / (session_name + '.json')

    # -- ownership registry -------------------------------------------------
    def _meta(self) -> Dict:
        try:
            return json.loads(self.meta_path.read_text())
        except (OSError, ValueError):
            return {'workspace': None, 'control_workspace': None, 'surfaces': {}}

    def _save_meta(self, meta: Dict) -> None:
        self.meta_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.meta_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        tmp.replace(self.meta_path)

    def _tag(self, sid: str, **info: str) -> None:
        meta = self._meta()
        meta['surfaces'].setdefault(sid, {}).update(info)
        self._save_meta(meta)

    def _surfaces(self, workspace: Optional[str]) -> Dict[str, Dict]:
        """Live terminal surfaces of a workspace: {surface_id: {pane_id, tty, title}}."""
        if not workspace:
            return {}
        p = _run('--json', '--id-format', 'uuids', 'tree', '--workspace', workspace)
        if p.returncode:
            return {}
        found = {}
        for window in json.loads(p.stdout).get('windows', []):
            for ws in window.get('workspaces', []):
                if ws.get('id') != workspace:
                    continue
                for pane in ws.get('panes', []):
                    for s in pane.get('surfaces', []):
                        found[s['id']] = {'pane_id': s.get('pane_id'), 'tty': s.get('tty'), 'title': s.get('title')}
        return found

    def _windows(self) -> Optional[List[Dict]]:
        """All windows with their workspaces (``tree --all``); None when cmux did not answer in JSON."""
        p = _run('--json', '--id-format', 'uuids', 'tree', '--all')
        try:
            windows = json.loads(p.stdout).get('windows') if p.returncode == 0 else None
        except (ValueError, AttributeError):
            return None
        return windows if isinstance(windows, list) else None

    def keep_windows_open(self, ours: List[str]) -> bool:
        """Before closing our workspaces: no window may be left with none. cmux 0.64.25 closes a
        window together with its last workspace, and the workspace goes as soon as its last surface does
        (2026-10-13 macmini: the driver closed the lone leftover workspace, the window went with it, and
        every later start failed until someone ran new-window). A window holding only workspaces of
        ``ours`` gets an empty shell workspace first. False if that could not be confirmed.

        An unreadable topology (cmux not answering JSON) is let through, as ``_surfaces`` treats a
        failed tree as "nothing live"; real cmux answers ``tree --all``."""
        ours = {ws for ws in ours if ws}
        windows = self._windows()
        if windows is None:
            return True
        for window in windows:
            ids = {ws.get('id') for ws in window.get('workspaces', [])}
            if not ids & ours or ids - ours:
                continue
            p = _run('--id-format', 'uuids', 'new-workspace', '--window', window.get('id', ''), '--focus', 'false')
            again = {w.get('id'): w for w in self._windows() or []}.get(window.get('id'))
            if p.returncode or not again or not {ws.get('id') for ws in again.get('workspaces', [])} - ours:
                return False
        return True

    def _ensure_window(self) -> bool:
        """new-workspace needs a window: with none open (after the last one closed) cmux answers
        "TabManager not available". Open one; an unreadable answer is let through as before."""
        def listed():
            p = _run('--json', '--id-format', 'uuids', 'list-windows')
            if p.returncode == 0 and p.stdout.strip() == 'No windows':  # the plain answer seen on 0.64.25
                return []
            try:
                return json.loads(p.stdout) if p.returncode == 0 else None
            except ValueError:
                return None
        if listed() != []:
            return True
        if _run('new-window').returncode:
            return False
        return _wait_until(lambda: bool(listed()))

    def _where(self, sid: str) -> Optional[str]:
        """Workspace holding an owned, live surface; None if not ours or gone."""
        meta = self._meta()
        if sid not in meta['surfaces']:
            return None
        for ws in (meta.get('workspace'), meta.get('control_workspace')):
            if sid in self._surfaces(ws):
                return ws
        return None

    def owns(self, sid: str) -> bool:
        return self._where(sid) is not None

    def _tmux_of(self, sid: str) -> Optional[Dict[str, str]]:
        """tmux tag of a surface that is a client of the fleet's tmux server (owner: the slot's own tab,
        whose retirement ends the session; view: a second client, e.g. in a sub-fleet's workspace)."""
        info = self._meta()['surfaces'].get(sid) or {}
        return info if info.get('tmux_socket') and info.get('tmux_session') else None

    # -- session ------------------------------------------------------------
    def session_exists(self) -> bool:
        meta = self._meta()
        return bool(self._surfaces(meta.get('workspace')) or self._surfaces(meta.get('control_workspace')))

    def get_or_create_session(self) -> bool:
        return cmux_available()  # workspaces are created on first spawn

    def _new_surface(self, control: bool = False, command: Optional[str] = None) -> Tuple[Optional[str], bool]:
        """A fresh shell tab in the agents workspace (created if closed) or the control one.

        Returns (surface_id, ran): ``ran`` is True when cmux itself started ``command`` in the
        new shell, False when the caller still has to type it (older cmux without --command).
        """
        meta = self._meta()
        key = 'control_workspace' if control else 'workspace'
        live = self._surfaces(meta.get(key))
        if live:
            anchor = next(iter(live.values()))['pane_id']
            base = ['--id-format', 'uuids', 'new-surface', '--workspace', meta[key], '--pane', anchor, '--focus', 'false']
            ran = False
            p = _run(*base, '--command', command) if command else _run(*base)
            if command and p.returncode:  # cmux too old for new-surface --command: create, then type
                p = _run(*base)
            else:
                ran = bool(command)
            parts = p.stdout.split()
            if p.returncode or len(parts) < 2:
                return None, False
            return parts[1], ran
        name = self.control_title if control else self.agents_title
        if not self._ensure_window():
            self.last_error = 'cmux has no window open and new-window failed'
            return None, False
        before = _run('--id-format', 'uuids', 'current-workspace').stdout.strip()
        extra = ['--command', command] if command else []
        p = _run('--id-format', 'uuids', 'new-workspace', '--name', name, '--focus', 'false', *extra)
        if before:  # --focus false still switches the view; give the user their workspace back
            _run('select-workspace', '--workspace', before)
        if p.returncode:
            self.last_error = 'new-workspace failed: ' + ((p.stderr or p.stdout or '').strip()[:200] or 'no output')
            return None, False
        ref = p.stdout.split()[-1]  # prints a ref (workspace:N); pin the UUID right away
        t = _run('--json', '--id-format', 'uuids', 'tree', '--workspace', ref)
        if t.returncode:
            return None, False
        ids = [ws['id'] for w in json.loads(t.stdout).get('windows', []) for ws in w.get('workspaces', [])
               if ws.get('title') == name]
        if len(ids) != 1:
            return None, False
        ws = ids[0]
        meta[key] = ws
        self._save_meta(meta)
        live = self._surfaces(ws)
        return (next(iter(live)), bool(command)) if live else (None, False)

    def _spawn_error(self) -> str:
        why, self.last_error = getattr(self, 'last_error', ''), ''
        return why or 'run inside cmux'

    def _launcher(self, name: str, script: str, folder: Optional[Path] = None) -> str:
        """Write ``script`` to a launcher file and return the short command that runs it."""
        folder = folder or (self.meta_path.parent / 'launch')
        folder.mkdir(parents=True, exist_ok=True)
        safe = ''.join(c if c.isalnum() or c in '-_.' else '_' for c in name)
        path = folder / (safe + '-' + uuid.uuid4().hex[:6] + '.sh')
        path.write_text('#!/bin/bash\n' + script + '\n')
        path.chmod(0o700)
        return 'exec /bin/bash ' + shlex.quote(str(path))

    def _type_command(self, sid: str, command: str) -> bool:
        ws = self._where(sid)
        if not ws:
            return False
        for _ in range(300):  # wait for the login shell prompt itself, not just "Last login"
            screen = _run('read-screen', '--workspace', ws, '--surface', sid, '--lines', '5')
            lines = [line.rstrip() for line in screen.stdout.splitlines() if line.strip()]
            if screen.returncode == 0 and lines and lines[-1].endswith(('%', '$', '#')):
                break
            time.sleep(.2)
        else:
            return False  # never type ahead of the prompt: the text gets truncated
        return self._paste(ws, sid, command)

    def _paste(self, ws: str, sid: str, text: str, bracketed: bool = False) -> bool:
        if not self._paste_text(ws, sid, text, bracketed):
            return False
        time.sleep(1)  # Codex paste-burst detection otherwise swallows the Enter.
        return _run('send-key', '--workspace', ws, '--surface', sid, 'Enter').returncode == 0

    def _paste_text(self, ws: str, sid: str, text: str, bracketed: bool = False) -> bool:
        # Verbatim delivery; no bracketed-paste markers by default (see module docstring).
        name = 'fleet-' + uuid.uuid4().hex
        if _run('set-buffer', '--name', name, text).returncode:
            return False
        if _run('paste-buffer', '--name', name, *(['--bracketed'] if bracketed else []),
                '--workspace', ws, '--surface', sid).returncode:
            return False
        return True

    def spawn_window(self, window_name: str, command: str, metadata: Optional[Dict[str, str]] = None,
                     start_directory: Optional[str] = None) -> SpawnResult:
        control = (metadata or {}).get('type') == 'daemon'
        full = self._launcher(window_name, 'cd ' + shlex.quote(start_directory or '.') + ' && exec ' + command)
        sid, ran = self._new_surface(control=control, command=full)
        if not sid:
            return SpawnResult(success=False, error='cmux surface creation failed (' + self._spawn_error() + ')')
        self._tag(sid, **dict({k: v for k, v in (metadata or {}).items() if isinstance(v, str)},
                              type=(metadata or {}).get('type', ''), label=window_name))
        if not _wait_until(lambda: self._where(sid)):
            return SpawnResult(success=False, pane_id=sid, error='cmux surface did not become visible')
        if not ran and not self._type_command(sid, full):
            return SpawnResult(success=False, error='cmux command delivery failed')
        self.rename(sid, window_name)
        return SpawnResult(success=True, pane_id=sid, window_name=window_name)

    # -- companion tabs (roster ``tabs:``, docs/design.md §31) -------------------
    def tab_panes(self) -> Dict[str, str]:
        """{surface id: tab name} of the companion tabs this registry owns that cmux still shows."""
        return {sid: info['tab'] for sid, info in self._meta()['surfaces'].items()
                if info.get('type') == 'tab' and info.get('tab') and self._where(sid)}

    def dock_tab(self, sid: str, history_surface: str, after: Optional[str] = None) -> bool:
        """Move a companion tab into the history / control pane: right after ``after`` (the tab opened before it)
        when that is there, else after the control tab, else after history. ``--focus false``."""
        ws = self._where(history_surface)
        if not ws or self._where(sid) != ws:
            return False
        live = self._surfaces(ws)
        pane = live[history_surface]['pane_id']
        controls = [s for s, info in self._meta()['surfaces'].items()
                    if info.get('type') == 'daemon' and live.get(s, {}).get('pane_id') == pane]
        anchor = after if after and live.get(after, {}).get('pane_id') == pane else (
            controls[0] if controls else history_surface)
        if _run('move-surface', '--workspace', ws, '--surface', sid, '--pane', pane, '--after', anchor,
                '--focus', 'false').returncode:
            return False
        return _wait_until(lambda: self._surfaces(ws).get(sid, {}).get('pane_id') == pane)

    def spawn_interactive_cli(self, window_name: str, cli_command: str, prompt_file: Optional[str] = None,
                              output_file: Optional[str] = None, metadata: Optional[Dict[str, str]] = None,
                              start_directory: Optional[str] = None, auto_close: bool = True,
                              env_vars: Optional[Dict[str, str]] = None, tmux_socket: Optional[str] = None,
                              tmux_session: Optional[str] = None, **_) -> SpawnResult:
        inner = cli_command
        if prompt_file:
            inner = f'{cli_command} "$(<{prompt_file})"'
        if not auto_close:
            inner = f'{inner}; exec {os.environ.get("SHELL") or "/bin/zsh"} -l'
        env = ' '.join(shlex.quote(k + '=' + v) for k, v in (env_vars or {}).items() if v is not None)
        command = ('env ' + env + ' ' if env else '') + 'bash -lc ' + shlex.quote(inner)
        full = self._launcher(window_name, 'cd ' + shlex.quote(start_directory or '.') + ' && exec ' + command,
                              Path(output_file).parent if output_file else None)
        tag = {}
        if tmux_socket and tmux_session:
            # The CLI runs in the fleet's tmux server (same launcher string); the tab is only its client.
            self._reap_tmux(tmux_socket, tmux_session)
            p = _tmux(tmux_socket, 'new-session', '-d', '-s', tmux_session, '-c', start_directory or '.', full)
            if p.returncode:
                return SpawnResult(success=False, error='tmux new-session failed: ' + (p.stderr or p.stdout).strip())
            tag = {'tmux_socket': str(tmux_socket), 'tmux_session': tmux_session, 'tmux_role': 'owner'}
            full = tmux_attach_command(tmux_socket, tmux_session)
        sid, ran = self._new_surface(command=full)
        if not sid:
            if tag:
                _tmux(tmux_socket, 'kill-session', '-t', '=' + tmux_session)  # no session without its tab
            return SpawnResult(success=False, error='cmux surface creation failed (' + self._spawn_error() + ')')
        self._tag(sid, type=(metadata or {}).get('type', ''), label=window_name, output_file=output_file or '', **tag)
        if not _wait_until(lambda: self._where(sid)):
            return SpawnResult(success=False, pane_id=sid, error='cmux surface did not become visible')
        if not ran and not self._type_command(sid, full):
            return SpawnResult(success=False, pane_id=sid, error='cmux command delivery failed')
        self.rename(sid, window_name)
        return SpawnResult(success=True, pane_id=sid, window_name=window_name)

    def _reap_tmux(self, sock: str, session: str) -> None:
        """Before ``<slot>-g<n>`` starts: end the slot's other generations whose tab is gone (a resume after
        the tab was closed), saving their history first. A generation still shown in a tab of ours (the
        predecessor during a handoff) is left to its own retirement."""
        prefix = session.rsplit('-g', 1)[0] + '-g'
        surfaces = self._meta()['surfaces']
        for name in tmux_sessions(sock):
            if name == session or not name.startswith(prefix) or not name[len(prefix):].isdigit():
                continue
            tabs = [sid for sid, info in surfaces.items() if info.get('tmux_session') == name]
            if any(self._where(sid) for sid in tabs):
                continue
            for sid in tabs:
                if surfaces[sid].get('output_file'):
                    with open(surfaces[sid]['output_file'], 'a', encoding='utf-8') as fh:
                        fh.write(self._tmux_history(sock, name))
            _tmux(sock, 'kill-session', '-t', '=' + name)

    def tmux_view_info(self, sid: str) -> Optional[Dict[str, str]]:
        """What another fleet needs to open a second client of this surface's session (None: not tmux)."""
        tm = self._tmux_of(sid)
        if not tm:
            return None
        return {k: v for k, v in tm.items() if k in ('type', 'label', 'tmux_socket', 'tmux_session')}

    def attach_view(self, info: Dict[str, str], peer: str) -> Optional[str]:
        """Open a second client of ``info``'s session as a tab just before ``peer`` and own that tab.
        The session and the other client are untouched. Returns the new surface (None on failure)."""
        sock, session = info.get('tmux_socket'), info.get('tmux_session')
        if not sock or not session or not self._where(peer) or session not in tmux_sessions(sock):
            return None
        command = tmux_attach_command(sock, session)
        before = tmux_clients(sock, session)
        sid, ran = self._new_surface(command=command)
        if not sid:
            return None
        self._tag(sid, **{k: v for k, v in info.items() if isinstance(v, str) and k != 'output_file'},
                  tmux_role='view')
        if not _wait_until(lambda: self._where(sid)) or (not ran and not self._type_command(sid, command)):
            self.kill_pane(sid)
            return None
        if not self.tab_into(sid, peer):
            self.kill_pane(sid)
            return None
        if before and self._where(sid) != _run('--id-format', 'uuids', 'current-workspace').stdout.strip():
            tmux_give_back(sock, session, before)  # attached out of view: the tab in view keeps the window size
        return sid

    def tmux_session_alive(self, sid: str) -> Optional[bool]:
        """Whether the tmux session behind ``sid`` (a client tab of the fleet's tmux, live or closed) still
        exists: True; False only when tmux proves it gone (its server answered without it, no server runs
        on the socket, or the socket is gone); None when ``sid`` is no tmux tab or tmux could not be read."""
        tm = self._tmux_of(sid)
        if not tm:
            return None
        if not Path(tm['tmux_socket']).exists():
            return False
        for attempt in range(TMUX_SHUTDOWN_RETRIES + 1):
            try:
                p = _tmux(tm['tmux_socket'], 'list-sessions', '-F', '#{session_name}')
            except OSError:
                return None
            if 'server exited unexpectedly' not in p.stderr or attempt == TMUX_SHUTDOWN_RETRIES:
                break
            time.sleep(0.1)  # the server is shutting down: its next answer is 'no server running'
        if p.returncode == 0:
            return tm['tmux_session'] in p.stdout.split()
        return False if 'no server running' in p.stderr + p.stdout else None

    def reattach_client(self, sid: str, peer: str) -> Optional[str]:
        """``sid`` (a client tab of the fleet's tmux) was closed while its session runs on: open a new client
        tab of that session just before ``peer``. It takes over ``sid``'s registry entry (owner / view role,
        output file, label); ``sid`` is forgotten, never killed (that would end the session). None: not done
        (not a tmux tab, the tab is still there, cmux does not show ``peer``, or the session is gone)."""
        tm = self._tmux_of(sid)
        if not tm or not self._where(peer) or self._where(sid):
            return None
        new = self.attach_view(self.tmux_view_info(sid), peer)
        if not new:
            return None
        meta = self._meta()
        meta['surfaces'][new] = meta['surfaces'].pop(sid, None) or tm
        self._save_meta(meta)
        if tm.get('label'):
            self.rename(new, tm['label'])
        return new

    def _client_tty(self, sid: str) -> Optional[str]:
        ws = self._where(sid)
        tty = self._surfaces(ws).get(sid, {}).get('tty') if ws else None
        return '/dev/' + os.path.basename(tty) if tty else None

    def detach_view(self, sid: str) -> bool:
        """Disconnect this tab's tmux client (the session keeps running) and forget the tab, which ends by
        itself (exec). Never ``close-surface --force``: a confirmation dialog belongs to the user."""
        tm = self._tmux_of(sid)
        if not tm:
            return False
        ws = self._where(sid)
        if ws:
            tty = self._client_tty(sid)
            if tty:
                _tmux(tm['tmux_socket'], 'detach-client', '-t', tty)
            if not _wait_until(lambda: sid not in self._surfaces(ws)):
                _run('close-surface', '--workspace', ws, '--surface', sid)
                if not _wait_until(lambda: sid not in self._surfaces(ws)):
                    return False
        meta = self._meta()
        meta['surfaces'].pop(sid, None)
        self._save_meta(meta)
        return True

    def _tmux_history(self, sock: str, session: str) -> str:
        # pane target: ``=<session>:`` (a bare ``=<session>`` is read as a pane name and fails)
        p = _tmux(sock, 'capture-pane', '-p', '-S', '-', '-t', '=' + session + ':')
        return p.stdout if p.returncode == 0 else ''

    def _tmux_pids(self, tm: Dict[str, str], fmt: str) -> List[str]:
        p = _tmux(tm['tmux_socket'], 'list-panes', '-s', '-t', '=' + tm['tmux_session'], '-F', fmt)
        return [line.strip() for line in p.stdout.splitlines() if line.strip()] if p.returncode == 0 else []

    # -- status / io ----------------------------------------------------------
    def get_pane_status(self, pane_id: str) -> Optional[PaneStatus]:
        if not self.owns(pane_id):
            return None
        info = self._meta()['surfaces'][pane_id]
        return PaneStatus(pane_id=pane_id, window_name=info.get('label', ''), alive=True,
                          metadata={k: v for k, v in info.items() if isinstance(v, str)})

    def has_cli_process(self, pane_id: str, names: Tuple[str, ...] = ('codex',)) -> bool:
        """A live surface may only contain a shell after its CLI process (default: Codex) exits."""
        ws = self._where(pane_id)
        if not ws:
            return False
        tm = self._tmux_of(pane_id)
        if tm:  # the tab shows only a tmux client; the CLI runs under the tmux server's pane
            pids = [str(p) for pane in self._tmux_pids(tm, '#{pane_pid}') if pane.isdigit() for p in _tree_pids(int(pane))]
            if not pids:
                return False
            sel = ['-p', ','.join(pids)]
        else:
            tty = self._surfaces(ws)[pane_id].get('tty')
            if not tty:
                return False
            sel = ['-t', tty]
        # comm: one executable path per line (may contain spaces); a bare shell never matches.
        result = subprocess.run(['ps', *sel, '-o', 'comm='], capture_output=True, text=True)
        if result.returncode != 0:
            return False
        exes = [_exe(line) for line in result.stdout.splitlines() if line.strip()]
        if any(e in names for e in exes):
            return True
        if 'node' not in exes:
            return False
        result = subprocess.run(['ps', *sel, '-o', 'args='], capture_output=True, text=True)
        return result.returncode == 0 and any(_node_runs(line, names) for line in result.stdout.splitlines())

    def process_states(self, pane_id: str) -> List[str]:
        """`ps` STAT of the processes on an owned surface's tty (watchdog frozen check)."""
        from cmux_fleet.watchdog import pane_tty_stats
        ws = self._where(pane_id)
        tm = self._tmux_of(pane_id) if ws else None
        if tm:
            return [st for tty in self._tmux_pids(tm, '#{pane_tty}') for st in pane_tty_stats(tty)]
        return pane_tty_stats(self._surfaces(ws).get(pane_id, {}).get('tty')) if ws else []

    def process_cpu(self, pane_id: str) -> Optional[float]:
        """CPU seconds of an owned surface's process tree (watchdog stall window); None when unreadable."""
        from cmux_fleet.watchdog import pane_tty_cpu
        ws = self._where(pane_id)
        if not ws:
            return None
        tm = self._tmux_of(pane_id)
        if tm:
            return pane_tty_cpu(self._tmux_pids(tm, '#{pane_tty}'))
        return pane_tty_cpu([self._surfaces(ws).get(pane_id, {}).get('tty')])

    def capture_output(self, pane_id: str, lines: int = 2000) -> str:
        ws = self._where(pane_id)
        if not ws:
            return ''
        tm = self._tmux_of(pane_id)
        if tm:  # whole scrollback of the session, not the client's current screen
            return self._tmux_history(tm['tmux_socket'], tm['tmux_session'])
        p = _run('read-screen', '--workspace', ws, '--surface', pane_id, '--scrollback', '--lines', str(lines))
        return p.stdout if p.returncode == 0 else ''

    def send_prompt_to_idle(self, pane_id: str, text: str, bracketed: bool = False) -> bool:
        from cmux_fleet import claude_input
        ws = self._where(pane_id)
        if ws and claude_input.required(self, pane_id):
            return claude_input.send(self, pane_id, text,
                lambda: self._paste_text(ws, pane_id, text, bracketed or text != '/clear'))
        return bool(ws) and self._paste(ws, pane_id, text, bracketed)

    def send_key(self, pane_id: str, key: str) -> bool:
        ws = self._where(pane_id)
        return bool(ws) and _run('send-key', '--workspace', ws, '--surface', pane_id, key).returncode == 0

    def rename(self, pane_id: str, label: str) -> None:
        ws = self._where(pane_id)
        if ws:
            _run('rename-tab', '--workspace', ws, '--surface', pane_id, label)
        self._tag(pane_id, label=label)

    def focus(self, pane_id: str) -> bool:
        ws = self._where(pane_id)
        if not ws:
            return False
        _run('select-workspace', '--workspace', ws)
        pane = self._surfaces(ws)[pane_id]['pane_id']
        return _run('focus-pane', '--workspace', ws, '--pane', pane).returncode == 0

    # -- lifecycle ------------------------------------------------------------
    def kill_pane(self, pane_id: str) -> bool:
        meta = self._meta()
        if pane_id not in meta['surfaces']:
            return False  # never touch a surface this fleet did not spawn
        ws = self._where(pane_id)
        tm = self._tmux_of(pane_id)
        if not ws:  # already closed (e.g. by the ✕)
            if tm and tm.get('tmux_role') == 'owner':  # its session must not outlive it as an orphan
                if tm.get('output_file'):
                    with open(tm['output_file'], 'a', encoding='utf-8') as f:
                        f.write(self._tmux_history(tm['tmux_socket'], tm['tmux_session']))
                _tmux(tm['tmux_socket'], 'kill-session', '-t', '=' + tm['tmux_session'])
            meta['surfaces'].pop(pane_id)
            self._save_meta(meta)
            return True
        info = self._surfaces(ws)[pane_id]
        output_file = self._meta()['surfaces'][pane_id].get('output_file')
        if output_file:  # cmux has no streaming pipe-pane; keep the final screen instead
            with open(output_file, 'a', encoding='utf-8') as f:
                f.write(self.capture_output(pane_id))
        if tm:
            # The tab holds only a tmux client: killing its tty would orphan the CLI in the tmux server.
            # Owner: end the session; view: disconnect this client only. Either way the tab exits (exec).
            if tm.get('tmux_role') == 'owner':
                _tmux(tm['tmux_socket'], 'kill-session', '-t', '=' + tm['tmux_session'])
            elif info.get('tty'):
                _tmux(tm['tmux_socket'], 'detach-client', '-t', '/dev/' + os.path.basename(info['tty']))
            if _wait_until(lambda: pane_id not in self._surfaces(ws)):
                meta = self._meta()
                meta['surfaces'].pop(pane_id, None)
                self._save_meta(meta)
                return True
        if info.get('tty'):
            ps = subprocess.run(['ps', '-t', info['tty'], '-o', 'pid=,ppid='], capture_output=True, text=True)
            rows = [line.split() for line in ps.stdout.splitlines() if line.strip()]
            pids = {int(r[0]) for r in rows}
            for pid, ppid in ((int(r[0]), int(r[1])) for r in rows):
                if ppid not in pids:  # tree roots on this tty
                    kill_process_tree(pid)
        p = _run('close-surface', '--workspace', ws, '--surface', pane_id)
        if p.returncode and 'confirmation_required' in p.stdout + p.stderr:
            # cmux >= 0.65 refuses to close a surface that still shows a running process; this
            # surface is ours and its process tree was just killed above.
            _run('close-surface', '--workspace', ws, '--surface', pane_id, '--force')
        if not _wait_until(lambda: pane_id not in self._surfaces(ws)):
            return False  # keep ownership so a failed/late close can be retried safely
        meta = self._meta()
        meta['surfaces'].pop(pane_id, None)
        self._save_meta(meta)
        return True

    def place_right_of(self, pane_id: str, peer: str) -> bool:
        """Put an agent in its own split to the right of its peer's pane."""
        ws = self._where(pane_id)
        if not ws or self._where(peer) != ws:
            return False
        live = self._surfaces(ws)
        if live[pane_id]['pane_id'] != live[peer]['pane_id']:
            return True  # already side by side
        return self._split_surface(ws, pane_id, live[peer]['pane_id'], 'right')

    def _split_surface(self, ws: str, sid: str, source_pane: str, direction: str) -> bool:
        if _run('split-off', '--workspace', ws, '--surface', sid, direction, '--focus', 'false').returncode:
            return False
        return _wait_until(lambda: self._surfaces(ws).get(sid, {}).get('pane_id') not in (None, source_pane))

    def capture_to(self, pane_id: str, path: str) -> None:
        """Save a retiring agent's whole screen (with scrollback) for the history page."""
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(self.capture_output(pane_id, 100000), encoding='utf-8')

    def dock_control(self, history_surface: str) -> bool:
        """Keep control beside history as an inactive tab, without restarting the daemon."""
        ws = self._where(history_surface)
        meta = self._meta()
        source = meta.get('control_workspace')
        if not ws or not source:
            return False
        controls = [sid for sid, info in meta['surfaces'].items()
                    if info.get('type') == 'daemon' and self._where(sid) == source]
        if not controls:
            return False
        pane = self._surfaces(ws)[history_surface]['pane_id']
        for sid in controls:
            p = _run('move-surface', '--surface', sid, '--workspace', ws, '--pane', pane,
                     '--after', history_surface, '--focus', 'false')
            if p.returncode or not _wait_until(lambda: self._surfaces(ws).get(sid, {}).get('pane_id') == pane):
                return False  # leave the existing control workspace alive on failure
        meta = self._meta()
        meta['control_workspace'] = ws
        self._save_meta(meta)
        if source != ws and not self._surfaces(source) and self.keep_windows_open([source]):
            _run('close-workspace', '--workspace', source)  # empty only; never --force
        # move-surface can select the moved tab even with --focus false. Select the
        # history surface explicitly, not just its pane (which could show control).
        return _run('focus-panel', '--workspace', ws, '--panel', history_surface).returncode == 0

    def arrange(self, supervisor: str, workers: List[str], history_url: str) -> Optional[str]:
        """One page: supervisor top-left, the history browser below it, the workers as tabs of one pane
        on the right (worker-1, worker-2, ... in order).

        Returns the history browser surface (None if it could not be created).
        """
        ws = self._where(supervisor)
        if not ws:
            return None
        # Re-entry must not create another browser/split. A different run's history
        # is not ours to reuse or remove; refuse rather than nesting into its layout.
        live = self._surfaces(ws)
        histories = [(sid, info) for sid, info in self._meta()['surfaces'].items()
                     if info.get('type') == 'history' and sid in live]
        if len(histories) > 1 or any(info.get('url') != history_url for _, info in histories):
            return None
        if any(self._where(w) != ws for w in workers):
            return None
        first = None
        for w in workers:
            if first is None:
                if not self.place_right_of(w, supervisor):
                    return None
                first = w
            elif not self.tab_into(w, first, before=False):
                return None
        pane = self._surfaces(ws)[supervisor]['pane_id']
        if histories:
            sid = histories[0][0]
        else:
            p = _run('--id-format', 'uuids', 'new-surface', '--type', 'browser', '--workspace', ws, '--pane', pane,
                     '--url', history_url, '--focus', 'false')
            parts = p.stdout.split()
            if p.returncode or len(parts) < 2:
                return None
            sid = parts[1]
            self._tag(sid, type='history', label='history', url=history_url)
        if not _wait_until(lambda: sid in self._surfaces(ws)):
            return None
        if self._surfaces(ws)[sid]['pane_id'] == pane:
            if not self._split_surface(ws, sid, pane, 'down'):
                return None
        self.rename(sid, 'history')
        self.dock_control(sid)
        _run('focus-panel', '--workspace', ws, '--panel', supervisor)
        return sid

    def reload_browser(self, surface: str) -> bool:
        return bool(self._where(surface)) and _run('browser', '--surface', surface, 'reload').returncode == 0

    def browser_url(self, surface: str) -> Optional[str]:
        """The URL a browser surface of ours shows now (None: not ours / cmux failed)."""
        if not self._where(surface):
            return None
        p = _run('browser', '--surface', surface, 'url')
        return (p.stdout.strip() or None) if p.returncode == 0 else None

    def browser_goto(self, surface: str, url: str) -> bool:
        return bool(self._where(surface)) and _run('browser', '--surface', surface, 'goto', url).returncode == 0

    def select_workspace(self, workspace: str) -> bool:
        """Switch the user's view to ``workspace`` (sub-fleet page links, docs/design.md §19)."""
        return bool(workspace) and _run('select-workspace', '--workspace', workspace).returncode == 0

    # -- handover between fleets (sub-fleets, docs/design.md §19) ----------------
    def claim_run(self, run: str) -> None:
        """Tag this registry with the run that owns it (the lifecycle closer checks it before closing)."""
        meta = self._meta()
        meta['run'] = run
        self._save_meta(meta)

    def workspace_id(self) -> Optional[str]:
        return self._meta().get('workspace')

    def release(self, pane_id: str) -> Optional[Dict]:
        """Give up ownership of a live surface without closing it, so another fleet's
        registry can adopt it. Returns its registry entry (None if not ours)."""
        meta = self._meta()
        info = meta['surfaces'].pop(pane_id, None)
        if info is not None:
            self._save_meta(meta)
        return info

    def adopt(self, pane_id: str, peer: str, info: Dict) -> bool:
        """Move a surface released by another fleet into ``peer``'s pane (just before it) and own it.

        Same cross-workspace ``move-surface`` that docks the control tab. On failure the surface
        stays where it was and is not registered here."""
        ws = self._where(peer)
        if not ws:
            return False
        pane = self._surfaces(ws)[peer]['pane_id']
        if _run('move-surface', '--surface', pane_id, '--workspace', ws, '--pane', pane, '--before', peer,
                '--focus', 'false').returncode:
            return False
        if not _wait_until(lambda: self._surfaces(ws).get(pane_id, {}).get('pane_id') == pane):
            return False
        self._tag(pane_id, **{k: v for k, v in (info or {}).items() if isinstance(v, str)})
        return True

    def place_after(self, workspace: Optional[str]) -> bool:
        """Sidebar order: put this fleet's agents workspace right after ``workspace``."""
        own = self.workspace_id()
        if not own or not workspace or own == workspace:
            return False
        return _run('reorder-workspace', '--workspace', own, '--after', workspace).returncode == 0

    def close_all(self) -> bool:
        """Close every surface this registry owns, then its now-empty workspaces (history files
        stay on disk). False if anything could not be confirmed closed.

        First make sure no window is left empty (keep_windows_open): killing the last terminal of a
        workspace closes the workspace too, so the check comes before any kill. If a window cannot be
        kept open, nothing is touched (False): a leftover tab is better than a cmux with no window."""
        meta = self._meta()
        if not self.keep_windows_open([meta.get('workspace'), meta.get('control_workspace')]):
            return False
        failed = {sid for sid in list(meta['surfaces']) if not self.kill_pane(sid)}
        ok = True
        meta = self._meta()
        for ws in dict.fromkeys(meta.get(k) for k in ('workspace', 'control_workspace')):
            if not ws:
                continue
            live = self._surfaces(ws)
            if not live:
                _run('close-workspace', '--workspace', ws)  # empty only; never --force
            elif set(live) <= set(meta['surfaces']):
                # cmux refuses to close a workspace's last surface ("invalid_state: Cannot close the last
                # surface"; real e2e 2026-10-13: the history tab stayed, close_failed). Its processes were
                # stopped by kill_pane above and nothing else lives here: close the workspace as a whole.
                _run('close-workspace', '--workspace', ws)
                if _wait_until(lambda: not self._surfaces(ws)):
                    meta = self._meta()
                    for sid in live:
                        meta['surfaces'].pop(sid, None)
                    self._save_meta(meta)
                    failed -= set(live)  # gone with their workspace
                else:
                    ok = False
            else:
                ok = False  # someone else's surface lives here: never close it
        return ok and not failed

    def tab_into(self, pane_id: str, peer: str, before: bool = True) -> bool:
        """Move a surface into the pane holding ``peer`` as another tab: just before ``peer`` (a successor
        takes its predecessor's place in the tab row) or, with ``before=False``, at the end of the row."""
        ws = self._where(pane_id)
        if not ws or self._where(peer) != ws:
            return False
        target = self._surfaces(ws)[peer]['pane_id']
        place = ['--before', peer] if before else []
        if _run('move-surface', '--workspace', ws, '--surface', pane_id, '--pane', target, *place,
                '--focus', 'false').returncode:
            return False
        return _wait_until(lambda: self._surfaces(ws).get(pane_id, {}).get('pane_id') == target)

