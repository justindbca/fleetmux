"""Environment variable names: ``FLEETMUX_*``, and the legacy ``CMUX_FLEET_*`` twins (docs/design.md §50).

Every reader in this package reads the ``FLEETMUX_*`` name. ``CMUX_FLEET_*`` is the legacy name, kept for
compatibility: scripts written before the rename still set it, and programs from older releases started from here
still read it. ``apply()`` runs before any submodule reads the environment (``fleetmux/__init__.py``) and gives each
pair one value in this process, so every child process inherits both names already equal:

* only the new name set -> the legacy name gets its value;
* only the legacy name set -> the new name gets its value;
* both set and different -> the new value wins and stderr gets one warning line.

An empty value counts as unset. This table is the one place the legacy names live.
"""
import os
import sys

# New-name suffix -> legacy-name suffix. Legacy names, kept for compatibility.
_SUFFIXES = {
    'HOME': 'HOME', 'STATE': 'STATE', 'CONFIG': 'CONFIG', 'BACKEND': 'BACKEND', 'PYTHON': 'PYTHON',
    'RELEASES': 'RELEASES', 'TOOL_SHA': 'TOOL_SHA', 'TMUX': 'TMUX', 'WEZTERM': 'WEZTERM',
    'WEZTERM_MUX': 'WEZTERM_MUX', 'BIN': 'FLEETMUX', 'WEB_BIN': 'FLEETMUX_WEB', 'REAL_BIN': 'FLEETMUX_REAL',
    'THEME': 'FLEETMUX_THEME', 'TG_CMD': 'TG_CMD', 'PARENT_CMD': 'PARENT_CMD', 'INITIATOR': 'INITIATOR',
    'GLOBAL_CLOCK': 'GLOBAL_CLOCK', 'DAEMON_WATCH': 'DAEMON_WATCH', 'LEGACY_LAUNCHER': 'LEGACY_LAUNCHER',
    'PI_BRIDGE': 'PI_BRIDGE', 'PI_CALLBACK': 'PI_CALLBACK', 'PI_DIR': 'PI_DIR', 'PI_MODE': 'PI_MODE',
    'PI_ROOT': 'PI_ROOT', 'PI_SLOT': 'PI_SLOT',
    'DSH_API_KEY': 'DSH_API_KEY', 'DSH_BRIDGE': 'DSH_BRIDGE', 'DSH_GEN': 'DSH_GEN', 'DSH_LABEL': 'DSH_LABEL',
    'DSH_LOG': 'DSH_LOG', 'DSH_MODE': 'DSH_MODE', 'DSH_MODEL': 'DSH_MODEL', 'DSH_SESSION_DIR': 'DSH_SESSION_DIR',
    'DSH_SLOT': 'DSH_SLOT', 'DSH_STARTUP': 'DSH_STARTUP', 'DSH_USER_HOME': 'DSH_USER_HOME',
    'DSH_WINDOW': 'DSH_WINDOW',
    'OC_BRIDGE': 'OC_BRIDGE', 'OC_CATALOG_WAIT_S': 'OC_CATALOG_WAIT_S', 'OC_GEN': 'OC_GEN', 'OC_LABEL': 'OC_LABEL',
    'OC_LAUNCH_CWD': 'OC_LAUNCH_CWD', 'OC_MODE': 'OC_MODE', 'OC_MODEL': 'OC_MODEL',
    'OC_SESSION_DIR': 'OC_SESSION_DIR', 'OC_SLOT': 'OC_SLOT', 'OC_STARTUP': 'OC_STARTUP', 'OC_WINDOW': 'OC_WINDOW',
}
LEGACY = {'FLEETMUX_' + new: 'CMUX_FLEET_' + old for new, old in _SUFFIXES.items()}
PAIRS = tuple(LEGACY.items())


def apply(environ=None, err=None):
    """Give each (new, legacy) pair one value in ``environ`` (default os.environ); see the module doc."""
    environ = os.environ if environ is None else environ
    for new, old in PAIRS:
        value, legacy = environ.get(new), environ.get(old)
        if value:
            if legacy and legacy != value:
                print('fleetmux: %s=%s and %s=%s differ; using %s' % (new, value, old, legacy, new),
                      file=err or sys.stderr)
            environ[old] = value
        elif legacy:
            environ[new] = legacy


def with_legacy(env):
    """``env`` plus the legacy twin of each new name in it: for a child that may still read the old names (an
    installed copy of an older pi extension)."""
    return dict(env, **{LEGACY[k]: v for k, v in env.items() if k in LEGACY})
