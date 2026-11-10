"""The client-tab attach command survives zsh: '=name' is quoted (zsh expands a bare '=word')."""
import shutil
import subprocess

import pytest

from cmux_fleet import cmux_manager


def test_session_target_is_quoted(tmp_path):
    cmd = cmux_manager.tmux_attach_command(str(tmp_path / 'tmux.sock'), 'memx-ops-g0')
    assert cmd.endswith(" attach -t '=memx-ops-g0'")


@pytest.mark.skipif(not shutil.which('zsh'), reason='zsh needed')
def test_zsh_passes_the_exact_match_target_through(tmp_path):
    cmd = cmux_manager.tmux_attach_command(str(tmp_path / 'tmux.sock'), 'memx-ops-g0')
    args = cmd[len('exec '):]
    out = subprocess.run(['zsh', '-c', 'print -rl -- ' + args], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines()[-1] == '=memx-ops-g0'
