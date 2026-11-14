"""Modules that fleet starts as script files (`python fleetmux/<name>.py`) must not let fleetmux/platform.py shadow
the stdlib `platform`: as a script, the module's own directory is sys.path[0], and Python 3.12's `uuid` calls
`platform.system()` on import (the daemon of a `uv tool install fleetmux` on Ubuntu 24.04 died at once)."""
import ast
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ['fleet.py', 'agent_bridge.py', 'session_board.py']  # started by path: pi_adapter, dsh / opencode, scope


def _guard_line(tree):
    for node in tree.body:
        if isinstance(node, ast.If) and 'sys.path' in ast.unparse(node.test) and '__file__' in ast.unparse(node.test):
            return node.lineno
    return None


@pytest.mark.parametrize('name', SCRIPTS)
def test_the_script_guard_comes_before_any_import_but_os_and_sys(name):
    tree = ast.parse((ROOT / 'fleetmux' / name).read_text())
    guard = _guard_line(tree)
    assert guard, name + ' has no sys.path guard'
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)) and node.lineno < guard:
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module]
            assert set(names) <= {'os', 'sys', '__future__'}, (name, names)


def _python312():
    found = shutil.which('python3.12')
    if found:
        return found
    uv = shutil.which('uv')
    if uv:
        done = subprocess.run([uv, 'python', 'find', '3.12'], capture_output=True, text=True)
        if done.returncode == 0 and done.stdout.strip():
            return done.stdout.strip()
    return None


@pytest.mark.skipif(_python312() is None, reason='no Python 3.12 here')
def test_fleet_py_runs_as_a_script_on_python_312(tmp_path):
    venv = tmp_path / 'venv'
    subprocess.run([_python312(), '-m', 'venv', str(venv)], check=True)
    py = venv / 'bin' / 'python'
    subprocess.run([str(py), '-m', 'pip', 'install', '-q', 'pyyaml', 'redis'], check=True, capture_output=True)
    done = subprocess.run([str(py), str(ROOT / 'fleetmux' / 'fleet.py'), '--help'], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr[-2000:]
    assert 'usage' in done.stdout
