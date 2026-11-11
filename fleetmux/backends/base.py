"""The terminal-backend interface of a fleet (docs/design.md「终端后端」).

fleet reaches the terminal exclusively through ``fleet.manager_for(cfg)``; that one object must
offer every method listed here. Three implementations ship: the original cmux one
(``backends/cmux.py``, moved there unchanged), the WezTerm one (``backends/wezterm.py``) and the
fleetmux one (``backends/fleetmux.py``, one private ``fleetmux server`` per run).

The signatures below are exactly ``CmuxSessionManager``'s (measured from the cmux implementation
plus the fakes in tests/, which are the de facto contract): changing one is a change to both
backends and to fleet. ``METHODS`` is the same list as data, so the interface-completeness test
(design §7.1) can check both classes without repeating the names.

Two capability flags, and nothing else, let fleet degrade a layout per backend (design §4.3):

* ``supports_browser`` — a real embedded browser surface for the history page (cmux yes;
  WezTerm no: the run's ``history.html`` is printed and opened with the system opener). fleetmux
  says yes, meaning "has a history pane": a terminal tab running ``fleetmux.history_view``, not a
  browser, so its ``browser_goto`` returns False and ``browser_url`` never carries a jump marker
  (fleetmux design §4.4).
* ``nested_tabs`` — tabs inside a split, i.e. worker tabs beside the supervisor in one page
  (cmux yes; WezTerm no: one tab per worker; fleetmux yes: three fixed tab groups).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol, Tuple, runtime_checkable

MISSING = object()  # sentinel of default_backend(): no backend could be picked


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


@dataclass(frozen=True)
class Capabilities:
    """What a backend can render (design §2). Read by fleet's layout and history code only."""
    browser: bool = True
    nested_tabs: bool = True


# Filled by the two implementation modules (they import this module), so the test can assert that
# every name below exists on both classes with the same signature.
METHODS = (
    # session
    'available', 'session_exists', 'get_or_create_session', 'claim_run', 'workspace_id',
    'select_workspace', 'keep_windows_open', 'owns', 'close_all',
    # spawning
    'spawn_window', 'spawn_interactive_cli',
    # reading / writing
    'send_prompt_to_idle', 'send_key', 'capture_output', 'capture_to',
    # state
    'get_pane_status', 'has_cli_process', 'process_states', 'process_cpu',
    # label / focus
    'rename', 'focus', 'kill_pane',
    # layout
    'arrange', 'dock_tab', 'dock_control', 'place_right_of', 'place_after', 'tab_into',
    'tab_panes',
    # browser cell
    'browser_goto', 'browser_url', 'reload_browser',
    # handover between fleets
    'release', 'adopt',
    # tmux views (named sub-fleets)
    'tmux_view_info', 'attach_view', 'tmux_session_alive', 'reattach_client', 'detach_view',
)

# (session_name, agents_title, control_title, state_dir) — both implementations accept these.
INIT_PARAMS = ('session_name', 'agents_title', 'control_title', 'state_dir')

BACKENDS = ('cmux', 'wezterm', 'fleetmux')


@runtime_checkable
class TerminalBackend(Protocol):
    """Typed view of :data:`METHODS`; ``isinstance(mgr, TerminalBackend)`` is a duck check only,
    the completeness test compares signatures against the cmux implementation."""

    supports_browser: bool
    nested_tabs: bool

    def session_exists(self) -> bool: ...
    def get_or_create_session(self) -> bool: ...
    def claim_run(self, run: str) -> None: ...
    def workspace_id(self) -> Optional[str]: ...
    def select_workspace(self, workspace: str) -> bool: ...
    def keep_windows_open(self, ours: List[str]) -> bool: ...
    def owns(self, sid: str) -> bool: ...
    def close_all(self) -> bool: ...
    def spawn_window(self, window_name: str, command: str, metadata: Optional[Dict[str, str]] = None,
                     start_directory: Optional[str] = None) -> SpawnResult: ...
    def spawn_interactive_cli(self, window_name: str, cli_command: str, prompt_file: Optional[str] = None,
                              output_file: Optional[str] = None, metadata: Optional[Dict[str, str]] = None,
                              start_directory: Optional[str] = None, **kwargs) -> SpawnResult: ...
    def send_prompt_to_idle(self, pane_id: str, text: str, bracketed: bool = False) -> bool: ...
    def send_key(self, pane_id: str, key: str) -> bool: ...
    def capture_output(self, pane_id: str, lines: int = 2000) -> str: ...
    def capture_to(self, pane_id: str, path: str) -> None: ...
    def get_pane_status(self, pane_id: str) -> Optional[PaneStatus]: ...
    def has_cli_process(self, pane_id: str, names: Tuple[str, ...] = ('codex',)) -> bool: ...
    def process_states(self, pane_id: str) -> List[str]: ...
    def process_cpu(self, pane_id: str) -> Optional[float]: ...
    def rename(self, pane_id: str, label: str) -> None: ...
    def focus(self, pane_id: str) -> bool: ...
    def kill_pane(self, pane_id: str) -> bool: ...
    def arrange(self, supervisor: str, workers: List[str], history_url: str) -> Optional[str]: ...
    def dock_tab(self, sid: str, history_surface: str, after: Optional[str] = None) -> bool: ...
    def dock_control(self, history_surface: str) -> bool: ...
    def place_right_of(self, pane_id: str, peer: str) -> bool: ...
    def place_after(self, workspace: Optional[str]) -> bool: ...
    def tab_into(self, pane_id: str, peer: str, before: bool = True) -> bool: ...
    def tab_panes(self) -> Dict[str, str]: ...
    def browser_goto(self, surface: str, url: str) -> bool: ...
    def browser_url(self, surface: str) -> Optional[str]: ...
    def reload_browser(self, surface: str) -> bool: ...
    def release(self, pane_id: str) -> Optional[Dict]: ...
    def adopt(self, pane_id: str, peer: str, info: Dict) -> bool: ...
    def tmux_view_info(self, sid: str) -> Optional[Dict[str, str]]: ...
    def attach_view(self, info: Dict[str, str], peer: str) -> Optional[str]: ...
    def tmux_session_alive(self, sid: str) -> Optional[bool]: ...
    def reattach_client(self, sid: str, peer: str) -> Optional[str]: ...
    def detach_view(self, sid: str) -> bool: ...


class InterfaceError(ValueError):
    """A backend name that is not one of :data:`BACKENDS`, or a backend that cannot be used here."""


def normalize_backend(name) -> Optional[str]:
    """'cmux' / 'wezterm' / 'fleetmux' as given (configured names win); None stays None (let the default decide)."""
    if name is None:
        return None
    value = str(name).strip().lower()
    if not value:
        return None
    if value not in BACKENDS:
        raise InterfaceError('unknown backend ' + repr(name) + ': choose one of ' + ', '.join(BACKENDS))
    return value


def unavailable_reason(name: str) -> Optional[str]:
    """Why ``name`` cannot run here (None: it can). Each backend explains itself (design §4.4)."""
    name = normalize_backend(name) or 'cmux'
    if name == 'cmux':
        from fleetmux.backends.cmux import cmux_available
        return None if cmux_available() else (
            'cmux not available: run this inside a cmux terminal (cmux only accepts processes started '
            'in its windows)')
    if name == 'fleetmux':
        from fleetmux.backends.fleetmux import availability_problem as fleetmux_problem
        return fleetmux_problem()
    from fleetmux.backends.wezterm import availability_problem
    return availability_problem()


def cmux_installed() -> bool:
    """A cmux binary is on this machine (``CMUX_BIN`` / the cmux.app path, or ``cmux`` on PATH).
    Installed is not usable: whether it answers is ``unavailable_reason('cmux')``'s question."""
    import shutil
    from fleetmux.backends import cmux
    return bool(shutil.which(str(cmux.CMUX)) or shutil.which('cmux'))


def default_backend(platform: Optional[str] = None) -> str:
    """No ``--backend``: cmux on a Mac with cmux installed, WezTerm on a Mac without it, fleetmux off macOS
    (design §5; M3 py-switch, brief-m03 §4.6).

    A Mac with cmux installed always gets cmux, usable or not (ssh / Terminal.app, where cmux does not
    answer): ``check_backend`` then refuses with cmux's own "run this inside a cmux terminal" -- the
    message a fleet got before the choice existed -- and the run never silently becomes a WezTerm run.
    On a Mac WezTerm is the default only where cmux is not installed; even then, while the WezTerm
    backend is unusable too, the answer stays cmux so the cmux reason is what gets reported. Off macOS
    the default is fleetmux (a cmux binary never makes Linux a cmux host); ``--backend wezterm`` still
    selects WezTerm.
    """
    platform = sys.platform if platform is None else platform
    if platform == 'darwin':
        try:
            if cmux_installed():
                return 'cmux'
        except Exception:  # cannot tell: stay on cmux (its probe reports why it cannot run)
            return 'cmux'
        try:
            if unavailable_reason('wezterm') is not None:
                return 'cmux'
        except Exception:
            return 'cmux'
        return 'wezterm'
    return 'fleetmux'


def check_backend(name: str) -> str:
    """The backend to run with: refuse an unknown or unusable one with the reason (design §5)."""
    name = normalize_backend(name) or default_backend()
    problem = unavailable_reason(name)
    if problem:
        raise InterfaceError(problem)
    return name


def capabilities(name: str) -> Capabilities:
    name = normalize_backend(name) or default_backend()
    if name == 'wezterm':
        return Capabilities(browser=False, nested_tabs=False)
    if name == 'fleetmux':  # browser = the history pane (see supports_browser above)
        return Capabilities(browser=True, nested_tabs=True)
    return Capabilities(browser=True, nested_tabs=True)
