"""§50.1: every per-user data dir defaults under ~/.fleetmux; an explicit environment variable wins.

HOME is a temporary directory in every test (never the operator's home); the conftest env isolation is undone
here so the default resolution itself is what runs. Data under older names: tests/test_home_migrate.py.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

from fleetmux import brake, channels, dsh_adapter, fleet_config, paths, transport_inventory

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / 'launchers' / 'start-dsh.command'
ENV_NAMES = ('FLEETMUX_HOME', 'FLEETMUX_STATE', 'FLEETMUX_CONFIG', 'FLEETMUX_CONFIG_DIR', 'FLEETMUX_LAUNCHER_ENV')


@pytest.fixture
def home(monkeypatch, tmp_path):
    h = tmp_path / 'h'
    h.mkdir()
    monkeypatch.setenv('HOME', str(h))
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    return h


def test_env_wins(home, monkeypatch, tmp_path):
    (home / '.fleetmux/state').mkdir(parents=True)
    monkeypatch.setenv('FLEETMUX_HOME', str(tmp_path / 'fh'))
    monkeypatch.setenv('FLEETMUX_STATE', str(tmp_path / 'st'))
    monkeypatch.setenv('FLEETMUX_CONFIG_DIR', str(tmp_path / 'cf'))
    monkeypatch.setenv('FLEETMUX_LAUNCHER_ENV', str(tmp_path / 'l.env'))
    assert paths.home() == tmp_path / 'fh'
    assert paths.state_dir() == tmp_path / 'st'
    assert paths.config_dir() == channels.config_dir() == tmp_path / 'cf'
    assert paths.launcher_env() == tmp_path / 'l.env'


def test_defaults_are_under_the_root(home):
    assert paths.home() == home / '.fleetmux'
    assert paths.state_dir() == home / '.fleetmux/state'
    assert paths.config_dir() == home / '.fleetmux/config'
    assert paths.launcher_env() == home / '.fleetmux/launchers.env'


def test_shared_files_follow_the_default_home_not_fleetmux_home(home, monkeypatch, tmp_path):
    """A per-fleet home (FLEETMUX_HOME) keeps reading the one private fleet.yaml / quota.json / dsh-home."""
    (home / '.fleetmux').mkdir()
    monkeypatch.setenv('FLEETMUX_HOME', str(home / '.fleetmux/fleets/dev'))
    monkeypatch.delenv(fleet_config.USER_ENV, raising=False)
    monkeypatch.setattr(fleet_config, 'USER_FILE', None)
    assert fleet_config.user_path() == home / '.fleetmux/fleet.yaml'
    assert dsh_adapter.dsh_home({}) == home / '.fleetmux/dsh-home'
    (home / '.fleetmux/quota.json').write_text(json.dumps({'pools': {'p': {'at': 1}}}))
    assert brake.read_quota() == {'pools': {'p': {'at': 1}}}


def test_inventory_searches_the_new_root_and_its_fleets(home, monkeypatch):
    for base in (home / '.fleetmux', home / '.fleetmux/fleets/dev'):
        run = base / 'runs/fleet-x'
        run.mkdir(parents=True)
        (run / 'config.json').write_text(json.dumps({'run': str(run), 'redis_socket': str(run / 's'),
                                                     'workers': ['worker-1']}))
    (home / 'link').symlink_to(home / '.fleetmux/fleets/dev')   # not a root: never listed
    monkeypatch.setattr(transport_inventory.subprocess, 'run',
                        lambda args, **kw: subprocess.CompletedProcess(args, 0, '', ''))
    report = transport_inventory.inventory()
    assert sorted(i['run'] for i in report['instances']) == sorted(
        [str(home / '.fleetmux/runs/fleet-x'), str(home / '.fleetmux/fleets/dev/runs/fleet-x')])
    assert all(len(i['configs']) == 1 for i in report['instances'])


def _launch(home, **env):
    e = {k: v for k, v in os.environ.items() if not k.startswith('DSH_') and k not in ENV_NAMES}
    e.update(DSH_PROVIDER='fake', HOME=str(home), **env)
    return subprocess.run(['bash', str(LAUNCHER), '--check-only'], env=e, capture_output=True, text=True, timeout=30)


def test_launcher_reads_the_root_file_and_stops_on_the_old_place(home):
    (home / '.config/fleetmux').mkdir(parents=True)
    (home / '.config/fleetmux/launchers.env').write_text('DSH_UI=bogus\n')
    r = _launch(home)
    assert r.returncode == 2 and "run 'fleetmux home migrate'" in r.stderr, r.stderr   # old place: never read
    assert 'DSH_UI 只认 web / tui' not in r.stderr, r.stderr
    (home / '.fleetmux').mkdir()
    (home / '.fleetmux/launchers.env').write_text('DSH_UI=bogus\n')
    r = _launch(home)
    assert r.returncode == 2 and 'DSH_UI 只认 web / tui' in r.stderr, r.stderr          # the root file is read
