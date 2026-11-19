"""CI Linux: the `start --backend fleetmux` harness must not depend on the host's keep-awake tool.

`auto_world` pretends to be Linux and records `subprocess.Popen`. `keep_awake_prefix()` (fleetmux/platform.py) uses
the real `shutil.which` / `subprocess.run` (import-time defaults), so on a Linux host with `systemd-inhibit` on PATH
its probe used to go through the recording Popen and fail (CI run 44361071214). A macOS host has no systemd-inhibit,
so the branch was never reached locally. Here a stand-in `systemd-inhibit` is put on PATH so every host walks that
path, and each `auto_world` test (tests/test_fleetmux_start_attach.py, tests/test_host_terminal_auto_view.py) must
still pass.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_fleetmux_start_attach as start_attach  # noqa: E402  (module import: its tests are not collected twice)
import test_host_terminal_auto_view as host_terminal  # noqa: E402

AUTO_WORLD_TESTS = [
    'test_auto_view_opens_the_window_in_the_background_on_a_desktop',
    'test_auto_view_attaches_without_a_desktop_or_window_half_or_over_ssh',
    'test_auto_view_stays_quiet_without_a_terminal_inside_a_pane_or_with_a_viewer',
    'test_no_view_flag_opens_nothing',
    'test_explicit_attach_still_execs_on_a_desktop',
    'test_auto_view_never_fires_on_cmux',
    'test_auto_view_raises_the_window_on_macos_only',
    'test_no_view_turns_the_daemons_window_off_for_the_run',
    'test_start_gui_opens_no_second_window',
]
HOST_TERMINAL_TESTS = [
    'test_auto_view_unchanged_in_a_host_terminal',
    'test_auto_view_over_ssh_in_a_host_terminal_still_attaches',
]


def systemd_inhibit_on_path(tmp_path, monkeypatch):
    """A Linux host's `systemd-inhibit` (an executable that would grant the inhibitor), first on PATH."""
    host_bin = tmp_path / 'host-bin'  # not tmp_path/bin: fleetmux_start creates that one itself
    host_bin.mkdir()
    tool = host_bin / 'systemd-inhibit'
    tool.write_text('#!/bin/sh\nexit 0\n')
    tool.chmod(0o755)
    monkeypatch.setenv('PATH', str(host_bin) + os.pathsep + os.environ.get('PATH', ''))
    return tool


@pytest.mark.parametrize('module, name', [(start_attach, n) for n in AUTO_WORLD_TESTS]
                         + [(host_terminal, n) for n in HOST_TERMINAL_TESTS])
def test_auto_world_tests_pass_with_systemd_inhibit_on_the_host(module, name, tmp_path, monkeypatch, capsys):
    systemd_inhibit_on_path(tmp_path, monkeypatch)
    getattr(module, name)(tmp_path / 'case', monkeypatch, capsys)
