"""start-dsh.command reads the operator's private launcher settings (FLEETMUX_LAUNCHER_ENV), explicit env wins."""
import os
import subprocess
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parent.parent / 'launchers' / 'start-dsh.command'


def run(tmp_path, file_text, **env):
    envf = tmp_path / 'launchers.env'
    envf.write_text(file_text)
    e = {k: v for k, v in os.environ.items() if not k.startswith('DSH_')}
    e.update(FLEETMUX_LAUNCHER_ENV=str(envf), DSH_PROVIDER='fake', HOME=str(tmp_path), **env)
    return subprocess.run(['bash', str(LAUNCHER), '--check-only'], env=e, capture_output=True, text=True, timeout=30)


def test_private_file_value_is_used(tmp_path):
    r = run(tmp_path, '# comment\nDSH_UI=bogus\n')
    assert r.returncode == 2 and 'DSH_UI 只认 web / tui' in r.stderr, r.stderr


def test_explicit_env_beats_private_file(tmp_path):
    r = run(tmp_path, 'DSH_UI=bogus\n', DSH_UI='web')
    assert 'DSH_UI 只认 web / tui' not in r.stderr, r.stderr


def test_bad_keys_are_ignored(tmp_path):
    r = run(tmp_path, 'not a key\nlower=x\n=empty\n')
    assert 'DSH_UI 只认 web / tui' not in r.stderr and 'syntax error' not in r.stderr, r.stderr
