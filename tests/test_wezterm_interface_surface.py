"""WezTerm-only public-surface test (not the shared interface checklist).

``tests/test_backend_interface.py`` compares both backends against ``backends/base.py``'s ``METHODS``
checklist and is owned as a shared file: this branch does not edit it, and it carries no WezTerm
exemption either. The supervisor's ruling ("``spawn_window_raise`` / ``dock_control_alone`` are cmux's
business, not the public protocol") is checked here instead, in a file that only concerns WezTerm.

Base withdrew both names from ``METHODS`` (173bbad1), so the shared checklist and this file agree.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cmux_fleet import backends  # noqa: E402


@pytest.fixture(scope='module')
def wz():
    try:
        return backends.backend_class('wezterm')
    except Exception as exc:  # pragma: no cover - the module ships on this branch
        pytest.skip('wezterm backend unavailable: %s' % exc)


def test_wezterm_does_not_carry_the_two_cmux_only_methods(wz):
    """``spawn_window_raise`` is cmux's typing-race workaround and ``dock_control_alone`` its page
    layout; neither belongs to the public protocol, so the WezTerm backend must not declare them."""
    assert not hasattr(wz, 'spawn_window_raise')
    assert not hasattr(wz, 'dock_control_alone')


def test_wezterm_keeps_the_protocol_entry_points_it_replaces(wz):
    """Dropping the two cmux-only names must not remove the real entry points behind them."""
    assert callable(getattr(wz, 'spawn_window', None))
    assert callable(getattr(wz, 'spawn_interactive_cli', None))
    assert callable(getattr(wz, 'dock_control', None))
    assert callable(getattr(wz, 'arrange', None))
