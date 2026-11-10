"""§50.1: every per-user data dir defaults under ~/.fleetmux; legacy dirs still work until `fleetmux home migrate`.

HOME is a temporary directory in every test (never the operator's home); the conftest env isolation is undone
here so the default resolution itself is what runs.
"""
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cmux_fleet import brake, channels, dsh_adapter, fleet_config, fleetmux_history, paths, transport_inventory

ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / 'launchers' / 'start-dsh.command'
ENV_NAMES = ('FLEETMUX_HOME', 'CMUX_FLEET_HOME', 'FLEETMUX_STATE', 'CMUX_FLEET_STATE', 'FLEETMUX_CONFIG',
             'CMUX_FLEET_CONFIG', 'FLEETMUX_CONFIG_DIR', 'FLEETMUX_LAUNCHER_ENV')


@pytest.fixture
def home(monkeypatch, tmp_path):
    h = tmp_path / 'h'
    h.mkdir()
    monkeypatch.setenv('HOME', str(h))
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(paths, '_warned', set())
    return h


def legacy_layout(h):
    (h / '.cmux-fleet/runs/fleet-1').mkdir(parents=True)
    (h / '.cmux-fleet/fleet.yaml').write_text('accounts: {}\n')
    (h / '.local/state/cmux-fleet').mkdir(parents=True)
    (h / '.local/state/cmux-fleet/cmux-fleet.fleetmux.json').write_text('{}')
    (h / '.config/fleetmux/channels').mkdir(parents=True)
    (h / '.config/fleetmux/launchers.env').write_text('DSH_PURGE=on\n')


def warnings():
    err = io.StringIO()
    paths.warn_legacy(err)
    return err.getvalue().splitlines()


def test_env_wins(home, monkeypatch, tmp_path):
    legacy_layout(home)
    (home / '.fleetmux/state').mkdir(parents=True)
    monkeypatch.setenv('CMUX_FLEET_HOME', str(tmp_path / 'fh'))
    monkeypatch.setenv('FLEETMUX_STATE', str(tmp_path / 'st'))
    monkeypatch.setenv('FLEETMUX_CONFIG_DIR', str(tmp_path / 'cf'))
    assert paths.home() == tmp_path / 'fh'
    assert paths.state_dir() == tmp_path / 'st'
    assert paths.config_dir() == channels.config_dir() == tmp_path / 'cf'
    assert warnings() == []


def test_neither_gives_new(home):
    assert paths.home() == home / '.fleetmux'
    assert paths.state_dir() == home / '.fleetmux/state'
    assert paths.config_dir() == home / '.fleetmux/config'
    assert paths.launcher_env() == home / '.fleetmux/launchers.env'
    assert warnings() == []


def test_new_only_gives_new(home):
    (home / '.fleetmux/runs').mkdir(parents=True)
    (home / '.fleetmux/state').mkdir()
    (home / '.fleetmux/config').mkdir()
    assert (paths.home(), paths.state_dir(), paths.config_dir()) == (
        home / '.fleetmux', home / '.fleetmux/state', home / '.fleetmux/config')
    assert warnings() == []


def test_legacy_only_gives_legacy_and_warns_once_per_item(home):
    legacy_layout(home)
    assert paths.home() == home / '.cmux-fleet'
    assert paths.state_dir() == home / '.local/state/cmux-fleet'
    assert paths.config_dir() == home / '.config/fleetmux'
    lines = warnings()
    assert len(lines) == 3
    assert lines[0] == ("fleetmux: using legacy %s; run 'fleetmux home migrate' to move it to %s"
                        % (home / '.cmux-fleet', home / '.fleetmux'))
    assert warnings() == []  # once per process


def test_board_alone_does_not_make_the_new_home(home):
    """~/.fleetmux exists only because of board/: the fleet home is still the legacy one, item by item."""
    legacy_layout(home)
    (home / '.fleetmux/board').mkdir(parents=True)
    (home / '.fleetmux/state').mkdir()        # state already new, the others still legacy
    assert paths.home() == home / '.cmux-fleet'
    assert paths.state_dir() == home / '.fleetmux/state'
    assert paths.config_dir() == home / '.config/fleetmux'
    assert len(warnings()) == 2


def test_shared_files_follow_the_default_home_not_fleetmux_home(home, monkeypatch, tmp_path):
    """A per-fleet home (FLEETMUX_HOME) keeps reading the one private fleet.yaml / quota.json / dsh-home."""
    legacy_layout(home)
    monkeypatch.setenv('CMUX_FLEET_HOME', str(tmp_path / 'per-fleet'))
    assert fleet_config.user_path() == home / '.cmux-fleet/fleet.yaml'
    assert dsh_adapter.dsh_home({}) == home / '.cmux-fleet/dsh-home'
    (home / '.cmux-fleet/quota.json').write_text(json.dumps({'pools': {'p': {'at': 1}}}))
    assert brake.read_quota() == {'pools': {'p': {'at': 1}}}


def test_migrate_dry_run_changes_nothing(home):
    legacy_layout(home)
    (home / '.fleetmux/board').mkdir(parents=True)
    out = io.StringIO()
    assert paths.migrate(dry_run=True, out=out, running=lambda: []) == 0
    text = out.getvalue()
    assert 'would mv %s -> %s' % (home / '.cmux-fleet/runs', home / '.fleetmux/runs') in text
    assert 'would mv %s -> %s' % (home / '.local/state/cmux-fleet', home / '.fleetmux/state') in text
    assert 'would mv %s -> %s' % (home / '.config/fleetmux/launchers.env', home / '.fleetmux/launchers.env') in text
    assert 'would mv %s -> %s' % (home / '.config/fleetmux', home / '.fleetmux/config') in text
    assert not (home / '.fleetmux/runs').exists() and not (home / '.cmux-fleet').is_symlink()
    assert sorted(p.name for p in (home / '.fleetmux').iterdir()) == ['board']


def test_migrate_moves_links_and_old_runs_keep_working(home):
    legacy_layout(home)
    (home / '.fleetmux/board').mkdir(parents=True)
    (home / '.fleetmux/board/x.json').write_text('{}')
    old_state = str(home / '.local/state/cmux-fleet')
    run = home / '.cmux-fleet/runs/fleet-1'
    cfg = {'run': str(run), 'fleet_home': str(home / '.cmux-fleet'), 'cmux_state_dir': old_state}
    (run / 'config.json').write_text(json.dumps(cfg))
    assert paths.migrate(out=io.StringIO(), running=lambda: []) == 0
    for legacy, new in ((home / '.cmux-fleet', home / '.fleetmux'),
                        (home / '.local/state/cmux-fleet', home / '.fleetmux/state'),
                        (home / '.config/fleetmux', home / '.fleetmux/config')):
        assert legacy.is_symlink() and os.path.realpath(legacy) == os.path.realpath(new)
    assert (home / '.fleetmux/runs/fleet-1/config.json').is_file()
    assert (home / '.fleetmux/fleet.yaml').read_text() == 'accounts: {}\n'
    assert (home / '.fleetmux/board/x.json').is_file()
    assert (home / '.fleetmux/state/cmux-fleet.fleetmux.json').is_file()
    assert (home / '.fleetmux/config/channels').is_dir()
    assert (home / '.fleetmux/launchers.env').read_text() == 'DSH_PURGE=on\n'
    assert (home / '.config/fleetmux/launchers.env').read_text() == 'DSH_PURGE=on\n'  # old launchers via links
    # old config.json absolute paths still resolve; nothing warns; the old run is the same runtime
    assert json.loads(Path(cfg['run'], 'config.json').read_text())['run'] == str(run)
    assert (paths.home(), paths.state_dir(), paths.config_dir()) == (
        home / '.fleetmux', home / '.fleetmux/state', home / '.fleetmux/config')
    assert warnings() == []
    assert fleetmux_history._same_runtime({'cmux_state_dir': old_state}, str(home / '.fleetmux/state'))


def test_migrate_refuses_while_a_fleet_runs(home, capsys):
    legacy_layout(home)
    rc = paths.migrate(out=io.StringIO(), running=lambda: ['pid 42: python cmux_fleet/fleet.py daemon c.json'])
    assert rc == 2
    assert 'pid 42' in capsys.readouterr().err
    assert (home / '.cmux-fleet').is_dir() and not (home / '.cmux-fleet').is_symlink()
    assert not (home / '.fleetmux').exists()


def test_migrate_skips_when_both_hold_content(home):
    legacy_layout(home)
    (home / '.fleetmux/state').mkdir(parents=True)
    (home / '.fleetmux/state/other.json').write_text('{}')
    (home / '.fleetmux/runs').mkdir()
    out = io.StringIO()
    assert paths.migrate(out=out, running=lambda: []) == 1
    text = out.getvalue()
    assert 'home: skip' in text and 'state: skip' in text
    assert (home / '.local/state/cmux-fleet/cmux-fleet.fleetmux.json').is_file()   # untouched
    assert (home / '.cmux-fleet/fleet.yaml').is_file() and not (home / '.cmux-fleet').is_symlink()
    assert (home / '.fleetmux/config').is_symlink() is False and (home / '.config/fleetmux').is_symlink()


def test_migrate_skips_a_home_child_that_names_another_item(home):
    (home / '.cmux-fleet/state').mkdir(parents=True)
    (home / '.cmux-fleet/runs').mkdir()
    out = io.StringIO()
    assert paths.migrate(out=out, running=lambda: []) == 1
    assert 'home: skip' in out.getvalue() and (home / '.cmux-fleet/runs').is_dir()


def test_migrate_launchers_env_alone(home):
    """Only ~/.config/fleetmux/launchers.env: the root is created before the move."""
    (home / '.config/fleetmux').mkdir(parents=True)
    (home / '.config/fleetmux/launchers.env').write_text('DSH_PURGE=on\n')
    assert paths.migrate(out=io.StringIO(), running=lambda: []) == 0
    assert (home / '.fleetmux/launchers.env').read_text() == 'DSH_PURGE=on\n'
    assert (home / '.fleetmux/config').is_dir() and (home / '.config/fleetmux').is_symlink()


def test_split_home_warns(home):
    """Data started under ~/.fleetmux while ~/.cmux-fleet still holds runs: new wins, one line says so."""
    (home / '.cmux-fleet/runs').mkdir(parents=True)
    (home / '.fleetmux').mkdir()
    (home / '.fleetmux/fleet.yaml').write_text('accounts: {}\n')
    assert paths.home() == home / '.fleetmux'
    lines = warnings()
    assert len(lines) == 1 and 'also holds fleet data' in lines[0]


def test_migrate_is_idempotent(home):
    legacy_layout(home)
    assert paths.migrate(out=io.StringIO(), running=lambda: []) == 0
    out = io.StringIO()
    assert paths.migrate(out=out, running=lambda: pytest.fail('no detector call when nothing moves')) == 0
    assert out.getvalue().startswith('nothing to do')


def test_detector_matches_fleet_processes_only():
    state = {'/h/.fleetmux/state'}
    assert paths._fleet_process(['/usr/bin/python3', '/r/cmux_fleet/fleet.py', 'daemon', '/c.json'], state)
    assert paths._fleet_process(['/r/.venv/bin/cmux-fleet', 'status'], state)
    assert paths._fleet_process(['/r/bin/fleetmux', 'server', '--runtime', '/x'], state)
    assert paths._fleet_process(['tmux', '-S', '/h/.fleetmux/state/tmux/s.sock', 'new'], state)
    assert not paths._fleet_process(['vim', '/somewhere/fleet.py'], state)
    assert not paths._fleet_process(['tmux', '-S', '/tmp/other.sock'], state)


def test_cli_dry_run_and_legacy_warning_in_a_new_process(home):
    legacy_layout(home)
    env = {k: v for k, v in os.environ.items() if k not in ENV_NAMES}
    env.update(HOME=str(home), PYTHONPATH=str(ROOT))
    r = subprocess.run([sys.executable, '-m', 'cmux_fleet.fleet', 'home', 'migrate', '--dry-run'],
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert 'would mv' in r.stdout and 'using legacy' not in r.stderr
    assert (home / '.cmux-fleet').is_dir() and not (home / '.cmux-fleet').is_symlink()
    r = subprocess.run([sys.executable, '-m', 'cmux_fleet.fleet', 'status'],
                       cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    legacy = [line for line in r.stderr.splitlines() if 'using legacy' in line]
    assert len(legacy) == 3, r.stderr


def test_inventory_searches_the_new_root_and_its_fleets(home, monkeypatch):
    for base in (home / '.fleetmux', home / '.fleetmux/fleets/dev'):
        run = base / 'runs/fleet-x'
        run.mkdir(parents=True)
        (run / 'config.json').write_text(json.dumps({'run': str(run), 'redis_socket': str(run / 's'),
                                                     'workers': ['worker-1']}))
    (home / '.cmux-fleet').symlink_to(home / '.fleetmux')   # migrated: must not be listed twice
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


def test_launcher_reads_new_default_then_legacy(home):
    (home / '.config/fleetmux').mkdir(parents=True)
    (home / '.config/fleetmux/launchers.env').write_text('DSH_UI=bogus\n')
    r = _launch(home)
    assert r.returncode == 2 and 'DSH_UI 只认 web / tui' in r.stderr, r.stderr   # legacy-only: still read
    (home / '.fleetmux').mkdir()
    (home / '.fleetmux/launchers.env').write_text('DSH_UI=web\n')
    r = _launch(home)
    assert 'DSH_UI 只认 web / tui' not in r.stderr, r.stderr                     # new wins over legacy
