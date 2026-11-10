"""start-codex-flashnext.command picks its pi provider from PI_FLASHNEXT_PROVIDER (fake pi / fake cmux)."""
import json
import os
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / 'launchers/start-codex-flashnext.command'


def _setup(tmp_path):
    agent = tmp_path / 'agent'
    agent.mkdir()
    (agent / 'models.json').write_text(json.dumps({'providers': {
        'flashnext': {'models': [{'id': 'studio-model'}]},
        'flashnext-popos': {'models': [{'id': 'popos-model'}]},
    }}))
    record = tmp_path / 'pi-args'
    pi = tmp_path / 'pi'
    pi.write_text(f'#!/bin/bash\n[ "$1" = --version ] && {{ echo 0.0.0; exit 0; }}\n'
                  f'printf "%s\\n" "PI_MODEL=$PI_MODEL" "$@" > {record}\n')
    pi.chmod(0o755)
    env = {**os.environ, 'PI_BIN': str(pi), 'PI_CODING_AGENT_DIR': str(agent), 'PI_WORKDIR': str(tmp_path)}
    for name in ('PI_MODEL', 'PI_FLASHNEXT_PROVIDER'):
        env.pop(name, None)
    return env, record, agent


def _run_terminal(tmp_path, extra_env):
    env, record, agent = _setup(tmp_path)
    env.update(extra_env)
    subprocess.run(['/bin/bash', str(LAUNCHER), '--terminal'], env=env, check=True, timeout=30)
    return record.read_text(), agent


def test_default_provider_unchanged(tmp_path):
    out, agent = _run_terminal(tmp_path, {})
    assert 'PI_MODEL=flashnext/studio-model' in out
    assert str(agent / 'sessions/flashnext') in out


def test_provider_override_selects_model_and_session_dir(tmp_path):
    out, agent = _run_terminal(tmp_path, {'PI_FLASHNEXT_PROVIDER': 'flashnext-popos'})
    assert 'PI_MODEL=flashnext-popos/popos-model' in out
    assert str(agent / 'sessions/flashnext-popos') in out


def test_unknown_provider_fails_before_pi(tmp_path):
    env, record, _ = _setup(tmp_path)
    env['PI_FLASHNEXT_PROVIDER'] = 'nope'
    proc = subprocess.run(['/bin/bash', str(LAUNCHER), '--terminal'], env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode != 0 and 'nope' in proc.stderr
    assert not record.exists()


def test_cmux_workspace_forwards_provider(tmp_path):
    env, _, _ = _setup(tmp_path)
    cmux_args = tmp_path / 'cmux-args'
    cmux = tmp_path / 'cmux'
    cmux.write_text(f'#!/bin/bash\n[ "$1" = --version ] && {{ echo 0.65.0; exit 0; }}\n[ "$1" = ping ] && exit 0\n'
                    f'printf "%s\\n" "$@" > {cmux_args}\n')
    cmux.chmod(0o755)
    env.update({'CMUX_BIN': str(cmux), 'CMUX_OPEN_BIN': '/usr/bin/true', 'PI_FLASHNEXT_PROVIDER': 'flashnext-popos'})
    subprocess.run(['/bin/bash', str(LAUNCHER)], env=env, check=True, timeout=30)
    args = cmux_args.read_text().splitlines()
    assert args[args.index('--name') + 1] == 'pi · Flash Next (flashnext-popos)'
    assert 'PI_FLASHNEXT_PROVIDER=flashnext-popos' in args[args.index('--command') + 1]
