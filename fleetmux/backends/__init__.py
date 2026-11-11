"""Terminal backends of a fleet (docs/design.md「终端后端」, design §2–§5 of the WezTerm plan).

fleet only ever touches the terminal through one object: ``fleet.manager_for(cfg)`` builds it.
``backends.base`` pins the interface, ``backends.cmux`` is the original cmux implementation
(moved there unchanged; ``fleetmux.cmux_manager`` stays as a forwarding module) and
``backends.wezterm`` is the WezTerm one built inside a private mux server per run, and
``backends.fleetmux`` drives one private ``fleetmux server`` per run.
"""
from __future__ import annotations

from fleetmux.backends.base import (BACKENDS, Capabilities, InterfaceError, MISSING, PaneStatus,
                                      SpawnResult, TerminalBackend, capabilities, check_backend,
                                      default_backend, normalize_backend, unavailable_reason)

__all__ = ['BACKENDS', 'Capabilities', 'InterfaceError', 'MISSING', 'PaneStatus', 'SpawnResult',
           'TerminalBackend', 'backend_class', 'build', 'capabilities', 'check_backend', 'default_backend',
           'normalize_backend', 'unavailable_reason']


def backend_class(name: str):
    """The manager class of one backend name (imported lazily: a cmux machine never imports WezTerm code)."""
    name = normalize_backend(name)
    if name == 'wezterm':
        from fleetmux.backends.wezterm import WezTermSessionManager
        return WezTermSessionManager
    if name == 'fleetmux':
        from fleetmux.backends.fleetmux import FleetmuxSessionManager
        return FleetmuxSessionManager
    from fleetmux.backends.cmux import CmuxSessionManager
    return CmuxSessionManager


def build(name: str, session_name: str, **extra):
    """Construct the manager of ``name``. All constructors take the same kwargs."""
    return backend_class(name)(session_name, **extra)
