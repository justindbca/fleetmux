"""The local diagnostic launcher never starts Pi outside its dedicated Fleet cwd."""
import os
from pathlib import Path
import subprocess

LAUNCHER = Path(__file__).resolve().parents[1] / 'launchers/tui-audit-pi.sh'


def test_audit_launcher_rejects_unrelated_directory(tmp_path):
    result = subprocess.run(['sh', str(LAUNCHER), '--model', 'openai-codex/gpt-6-astra'],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 2
    assert 'Refusing non-audit Fleet cwd' in result.stderr


def test_audit_launcher_version_probe_only(tmp_path):
    pi = tmp_path / '.local/share/fnm/aliases/default/bin/pi'
    pi.parent.mkdir(parents=True)
    pi.write_text('#!/bin/sh\nprintf "%s\\n" "$*"\n')
    pi.chmod(0o755)
    result = subprocess.run(['sh', str(LAUNCHER), '--version'], cwd=tmp_path,
                            env=dict(os.environ, HOME=str(tmp_path)), capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.strip() == '--version'


def test_audit_launcher_keeps_bridge_arguments_and_excludes_shell_edits():
    text = LAUNCHER.read_text()
    assert '.runtime/ta/runs/*/agents/supervisor' in text
    assert '.runtime/ta/runs/*/workspace/worker-[1-5]' in text
    assert 'exec "$PI" --exclude-tools bash,edit "$@"' in text
    assert '--no-tools' not in text  # Fleet protocol tools must remain available
