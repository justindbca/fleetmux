"""Finder-like PATH contracts for the shared Pi launcher (no actual GUI/model)."""
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / 'launchers/start-pi-full.command'


def make_executable(path, content):
    path.write_text(content)
    path.chmod(0o700)
    return path


def test_gui_path_uses_node_next_to_pi(tmp_path):
    binaries = tmp_path / 'fnm with spaces' / 'bin'
    binaries.mkdir(parents=True)
    # An interpreter fixture: env resolves node, and this stub interprets the
    # fixture's shell body. No Node from the test runner's PATH is needed.
    make_executable(binaries / 'node', '#!/bin/bash\nexport SIBLING_NODE=found\nexec /bin/bash "$@"\n')
    pi = make_executable(binaries / 'pi', '''#!/usr/bin/env node
if [ "${1:-}" = --version ]; then echo '1.3.0'; exit 0; fi
printf 'NODE=%s\nCWD=%s\n' "$SIBLING_NODE" "$PWD"
printf 'ARG=%s\n' "$@"
''')
    workdir = tmp_path / 'business project'
    workdir.mkdir()
    env = {'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
           'PI_BIN': str(pi), 'PI_WORKDIR': str(workdir), 'PI_MODEL': 'opencode-cli/deepseek-v4.1-flash-free'}
    result = subprocess.run(['/bin/bash', str(LAUNCHER), '--no-session'], env=env,
                            stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            start_new_session=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert 'NODE=found' in result.stdout
    assert f'CWD={workdir.resolve()}' in result.stdout
    assert 'ARG=--no-session' in result.stdout
    assert '(1.3.0)' in result.stdout


def test_pi_version_failure_does_not_continue_to_session(tmp_path):
    log = tmp_path / 'calls.txt'
    pi = make_executable(tmp_path / 'bad-pi', '''#!/bin/bash
printf '%s\n' "$*" >> "$TEST_CALL_LOG"
echo 'broken runtime' >&2
exit 2
''')
    env = {**os.environ, 'HOME': str(tmp_path), 'PI_BIN': str(pi), 'PI_WORKDIR': str(tmp_path),
           'PI_MODEL': 'opencode-cli/deepseek-v4.1-flash-free', 'TEST_CALL_LOG': str(log)}
    result = subprocess.run(['/bin/bash', str(LAUNCHER)], env=env, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, start_new_session=True, timeout=10)
    assert result.returncode == 127
    assert 'Pi 无法启动' in result.stderr
    assert log.read_text().splitlines() == ['--version']
