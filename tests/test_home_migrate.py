"""`fleetmux home migrate` and the start-up guard (docs/design.md §50.1) -- migration only.

The old names below (the pre-fleetmux layout) appear only here and in fleetmux/home_migrate.py. HOME is a temporary
directory in every test, never the operator's home.
"""
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from fleetmux import fleet, fleetmux_history, home_migrate, paths

ROOT = Path(__file__).resolve().parent.parent
OLD = home_migrate.OLD
ENV_NAMES = ('FLEETMUX_HOME', 'FLEETMUX_STATE', 'FLEETMUX_CONFIG', 'FLEETMUX_CONFIG_DIR', 'FLEETMUX_LAUNCHER_ENV')


@pytest.fixture
def home(monkeypatch, tmp_path):
    h = tmp_path / 'h'
    h.mkdir()
    monkeypatch.setenv('HOME', str(h))
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(home_migrate, '_home', Path.home)  # conftest points the guard elsewhere; undo it here
    return h


def old_layout(h):
    (h / '.cmux-fleet/runs/fleet-1').mkdir(parents=True)
    (h / '.cmux-fleet/fleet.yaml').write_text('accounts: {}\n')
    (h / '.local/state/cmux-fleet/fleetmux/cmux-fleet').mkdir(parents=True)
    (h / '.local/state/cmux-fleet/fleetmux/cmux-fleet/layout.json').write_text('{}')
    (h / '.local/state/cmux-fleet/cmux-fleet.fleetmux.json').write_text('{}')
    (h / '.config/fleetmux/channels').mkdir(parents=True)
    (h / '.config/fleetmux/launchers.env').write_text('DSH_PURGE=on\n')


def old_fleet(h, name):
    home, state = h / ('.cmux-fleet-' + name), h / '.local/state' / ('cmux-fleet-' + name)
    run = home / 'runs/fleet-2'
    (run / 'sub/M0').mkdir(parents=True)
    (state / 'wezterm/cmux-fleet_M0').mkdir(parents=True)
    (state / 'cmux-fleet.json').write_text('{}')
    cfg = {'run': str(run), 'fleet_home': str(home), 'cmux_state_dir': str(state),
           'private_config': str(h / '.cmux-fleet/fleet.yaml'), 'roster': str(home / 'roster.yaml'),
           'python': str(h / 'cmux-fleet-releases/abc/.venv/bin/python'),
           'redis_socket': '/tmp/cmux-fleet-ab12.sock', 'worker_roles': {'worker-1': str(home / 'roles/doer.md')},
           'resume_env': {'CMUX_FLEET_HOME': str(home), 'CMUX_FLEET_FLEETMUX_WEB': '/x/fleetmux-web'}}
    (run / 'config.json').write_text(json.dumps(cfg))
    sub = dict(cfg, run=str(run / 'sub/M0'), session='cmux-fleet:M0', fleetmux_root_session='cmux-fleet',
               parent_workspace='fleetmux:cmux-fleet')
    (run / 'sub/M0/config.json').write_text(json.dumps(sub))
    (home / 'latest.json').write_text(json.dumps(cfg))
    return home, state


def guard(fatal=True):
    err = io.StringIO()
    try:
        home_migrate.check(fatal=fatal, err=err)
        code = 0
    except SystemExit as e:
        code = e.code
    return code, err.getvalue().splitlines()


# ------------------------------------------------------------------ start-up guard

def test_old_data_stops_the_human_commands_with_one_line(home):
    old_layout(home)
    assert paths.home() == home / '.fleetmux'          # resolution never reads the old place
    code, lines = guard()
    assert code == 2 and len(lines) == 1
    assert "run 'fleetmux home migrate'" in lines[0] and str(home / '.cmux-fleet') in lines[0]
    assert guard(fatal=False)[0] == 0                  # status / stop: the line only


def test_nothing_old_says_nothing(home):
    (home / '.fleetmux/runs').mkdir(parents=True)
    assert guard() == (0, [])


def test_a_migrated_layout_is_quiet(home):
    old_layout(home)
    assert home_migrate.migrate(out=io.StringIO(), running=lambda: []) == 0
    assert guard() == (0, [])


def test_split_home_is_a_note(home):
    (home / '.cmux-fleet/runs').mkdir(parents=True)
    (home / '.fleetmux').mkdir()
    (home / '.fleetmux/fleet.yaml').write_text('accounts: {}\n')
    code, lines = guard()
    assert code == 0 and len(lines) == 1 and 'not read (not merged)' in lines[0]


def test_old_fleet_home_names_are_refused(home, monkeypatch):
    monkeypatch.setenv('FLEETMUX_HOME', str(home / '.cmux-fleet-smoke'))
    code, lines = guard()
    assert code == 2 and 'uses the old name' in lines[0] and '~/.fleetmux/fleets/<name>' in lines[0]


def test_a_new_fleet_home_not_yet_migrated_is_refused(home, monkeypatch):
    old_fleet(home, 'dev')
    monkeypatch.setenv('FLEETMUX_HOME', str(home / '.fleetmux/fleets/dev'))
    code, lines = guard()
    assert code == 2 and str(home / '.cmux-fleet-dev') in lines[0]


def test_old_environment_names_are_refused_unless_mirrored(home, monkeypatch):
    monkeypatch.setenv('CMUX_FLEET_HOME', str(home / 'x'))
    code, lines = guard()
    assert code == 2 and lines == ['fleetmux: CMUX_FLEET_HOME is not read any more; set FLEETMUX_HOME instead']
    monkeypatch.setenv('FLEETMUX_HOME', str(home / 'x'))   # an older release's pane sets both: fine
    monkeypatch.setenv('CMUX_FLEET_FLEETMUX_WEB', '/w')
    monkeypatch.setenv('FLEETMUX_WEB_BIN', '/w')
    assert guard() == (0, [])


# ------------------------------------------------------------------ migrate

def test_migrate_dry_run_changes_nothing(home):
    old_layout(home)
    old_fleet(home, 'dev')
    (home / '.fleetmux/board').mkdir(parents=True)
    out = io.StringIO()
    assert home_migrate.migrate(dry_run=True, out=out, running=lambda: []) == 0
    text = out.getvalue()
    assert 'would mv %s -> %s' % (home / '.cmux-fleet/runs', home / '.fleetmux/runs') in text
    assert 'would mv %s -> %s' % (home / '.local/state/cmux-fleet', home / '.fleetmux/state') in text
    assert 'would mv %s -> %s' % (home / '.config/fleetmux/launchers.env', home / '.fleetmux/launchers.env') in text
    assert 'would mv %s -> %s' % (home / '.config/fleetmux', home / '.fleetmux/config') in text
    assert 'would mv %s -> %s' % (home / '.cmux-fleet-dev', home / '.fleetmux/fleets/dev') in text
    assert 'would mv %s -> %s' % (home / '.local/state/cmux-fleet-dev', home / '.fleetmux/fleets/dev/state') in text
    assert 'would mv %s -> %s' % (home / '.local/state/cmux-fleet/cmux-fleet.fleetmux.json',
                                  home / '.local/state/cmux-fleet/fleetmux.fleetmux.json') in text
    sub = home / '.cmux-fleet-dev/runs/fleet-2/sub/M0/config.json'
    assert 'run record: would rewrite old names in %s' % sub in text
    assert not (home / '.fleetmux/runs').exists() and not (home / '.cmux-fleet').is_symlink()
    assert sorted(p.name for p in (home / '.fleetmux').iterdir()) == ['board']
    assert json.loads((home / '.cmux-fleet-dev/latest.json').read_text())['fleet_home'] \
        == str(home / '.cmux-fleet-dev')


def test_migrate_moves_links_and_rewrites(home):
    old_layout(home)
    (home / '.fleetmux/board').mkdir(parents=True)
    (home / '.fleetmux/board/x.json').write_text('{}')
    run = home / '.cmux-fleet/runs/fleet-1'
    old_state = str(home / '.local/state/cmux-fleet')
    (run / 'config.json').write_text(json.dumps({'run': str(run), 'fleet_home': str(home / '.cmux-fleet'),
                                                 'cmux_state_dir': old_state}))
    assert home_migrate.migrate(out=io.StringIO(), running=lambda: []) == 0
    for old, new in ((home / '.cmux-fleet', home / '.fleetmux'),
                     (home / '.local/state/cmux-fleet', home / '.fleetmux/state'),
                     (home / '.config/fleetmux', home / '.fleetmux/config')):
        assert old.is_symlink() and os.path.realpath(old) == os.path.realpath(new)
    assert (home / '.fleetmux/fleet.yaml').read_text() == 'accounts: {}\n'
    assert (home / '.fleetmux/board/x.json').is_file()
    assert (home / '.fleetmux/state/fleetmux.fleetmux.json').is_file()           # session name renamed
    assert (home / '.fleetmux/state/fleetmux/fleetmux/layout.json').is_file()     # runtime dir renamed
    assert (home / '.fleetmux/config/channels').is_dir()
    assert (home / '.fleetmux/launchers.env').read_text() == 'DSH_PURGE=on\n'
    cfg = json.loads((home / '.fleetmux/runs/fleet-1/config.json').read_text())
    assert cfg == {'run': str(home / '.fleetmux/runs/fleet-1'), 'fleet_home': str(home / '.fleetmux'),
                   'cmux_state_dir': str(home / '.fleetmux/state')}
    assert fleetmux_history._same_runtime(cfg, str(home / '.fleetmux/state'))


def test_migrate_moves_per_fleet_homes_and_resume_needs_no_link(home):
    old_layout(home)
    old_home, old_state = old_fleet(home, 'dev')
    assert home_migrate.migrate(out=io.StringIO(), running=lambda: []) == 0
    new = home / '.fleetmux/fleets/dev'
    assert old_home.is_symlink() and os.path.realpath(old_home) == os.path.realpath(new)
    assert old_state.is_symlink() and os.path.realpath(old_state) == os.path.realpath(new / 'state')
    assert (new / 'state/fleetmux.json').is_file() and (new / 'state/wezterm/fleetmux_M0').is_dir()
    for link in (old_home, old_state, home / '.cmux-fleet', home / '.local/state/cmux-fleet'):
        link.unlink()                                   # the records must not depend on the links
    cfg = fleet.load(new / 'runs/fleet-2/config.json')
    assert cfg['run'] == str(new / 'runs/fleet-2') and Path(cfg['run']).is_dir()
    assert cfg['fleet_home'] == str(new) and cfg['cmux_state_dir'] == str(new / 'state')
    assert cfg['private_config'] == str(home / '.fleetmux/fleet.yaml') and Path(cfg['private_config']).is_file()
    assert cfg['roster'] == str(new / 'roster.yaml') and cfg['worker_roles'] == {'worker-1': str(new / 'roles/doer.md')}
    assert cfg['python'] == str(home / 'cmux-fleet-releases/abc/.venv/bin/python')   # releases are not moved
    assert cfg['redis_socket'] == '/tmp/fleetmux-ab12.sock'
    assert cfg['resume_env'] == {'FLEETMUX_HOME': str(new), 'FLEETMUX_WEB_BIN': '/x/fleetmux-web'}
    sub = fleet.load(new / 'runs/fleet-2/sub/M0/config.json')
    assert (sub['session'], sub['fleetmux_root_session'], sub['parent_workspace'], sub['run']) == (
        'fleetmux:M0', 'fleetmux', 'fleetmux:fleetmux', str(new / 'runs/fleet-2/sub/M0'))
    assert fleet.load(new / 'latest.json')['fleet_home'] == str(new)
    assert 'cmux-fleet' not in json.dumps(sub).replace(str(home / 'cmux-fleet-releases'), '')


def test_records_under_new_homes_follow_an_earlier_move(home):
    """A fleet already under ~/.fleetmux/fleets pinned the old private config; it is rewritten too."""
    old_layout(home)
    run = home / '.fleetmux/fleets/new/runs/fleet-3'
    run.mkdir(parents=True)
    (run / 'config.json').write_text(json.dumps({'run': str(run),
                                                 'private_config': str(home / '.cmux-fleet/fleet.yaml')}))
    assert home_migrate.migrate(out=io.StringIO(), running=lambda: []) == 0
    assert fleet.load(run / 'config.json')['private_config'] == str(home / '.fleetmux/fleet.yaml')


def test_migrate_refuses_while_a_fleet_runs(home, capsys):
    old_layout(home)
    rc = home_migrate.migrate(out=io.StringIO(), running=lambda: ['pid 42: python fleetmux/fleet.py daemon c.json'])
    assert rc == 2
    assert 'pid 42' in capsys.readouterr().err
    assert (home / '.cmux-fleet').is_dir() and not (home / '.cmux-fleet').is_symlink()
    assert not (home / '.fleetmux').exists()


def test_migrate_skips_when_both_hold_content(home):
    old_layout(home)
    (home / '.fleetmux/state').mkdir(parents=True)
    (home / '.fleetmux/state/other.json').write_text('{}')
    (home / '.fleetmux/runs').mkdir()
    out = io.StringIO()
    assert home_migrate.migrate(out=out, running=lambda: []) == 1
    text = out.getvalue()
    assert 'home: skip' in text and 'state: skip' in text
    assert (home / '.local/state/cmux-fleet/cmux-fleet.fleetmux.json').is_file()   # untouched
    assert (home / '.cmux-fleet/fleet.yaml').is_file() and not (home / '.cmux-fleet').is_symlink()
    assert (home / '.fleetmux/config').is_symlink() is False and (home / '.config/fleetmux').is_symlink()


def test_migrate_skips_a_home_child_that_names_another_item(home):
    (home / '.cmux-fleet/state').mkdir(parents=True)
    (home / '.cmux-fleet/runs').mkdir()
    out = io.StringIO()
    assert home_migrate.migrate(out=out, running=lambda: []) == 1
    assert 'home: skip' in out.getvalue() and (home / '.cmux-fleet/runs').is_dir()


def test_migrate_skips_a_per_fleet_dir_that_is_no_fleet_home_or_already_exists(home):
    (home / '.cmux-fleet-notes').mkdir()
    (home / '.cmux-fleet-notes/x.txt').write_text('x')
    old_fleet(home, 'dev')
    (home / '.fleetmux/fleets/dev/runs').mkdir(parents=True)
    out = io.StringIO()
    assert home_migrate.migrate(out=out, running=lambda: []) == 1
    text = out.getvalue()
    assert 'fleet notes: skip' in text and 'not a fleet home' in text
    assert 'fleet dev: skip' in text and (home / '.cmux-fleet-dev/runs/fleet-2').is_dir()
    assert 'fleet dev state: skip' in text and (home / '.local/state/cmux-fleet-dev').is_dir()  # follows its home
    assert not (home / '.fleetmux/fleets/dev/state').exists()


def test_a_per_fleet_state_dir_never_merges_into_a_home_state_child(home):
    old_home, old_state = old_fleet(home, 'dev')
    (old_home / 'state').mkdir()
    (old_home / 'state/x.json').write_text('{}')
    out = io.StringIO()
    assert home_migrate.migrate(out=out, running=lambda: []) == 1
    assert 'fleet dev state: skip' in out.getvalue()
    assert (home / '.fleetmux/fleets/dev/state/x.json').is_file()       # the home moved with its own state/
    assert old_state.is_dir() and not old_state.is_symlink() and (old_state / 'cmux-fleet.json').is_file()


def test_migrate_launchers_env_alone(home):
    (home / '.config/fleetmux').mkdir(parents=True)
    (home / '.config/fleetmux/launchers.env').write_text('DSH_PURGE=on\n')
    assert home_migrate.migrate(out=io.StringIO(), running=lambda: []) == 0
    assert (home / '.fleetmux/launchers.env').read_text() == 'DSH_PURGE=on\n'
    assert (home / '.fleetmux/config').is_dir() and (home / '.config/fleetmux').is_symlink()


def test_migrate_is_idempotent(home):
    old_layout(home)
    old_fleet(home, 'dev')
    assert home_migrate.migrate(out=io.StringIO(), running=lambda: []) == 0
    out = io.StringIO()
    assert home_migrate.migrate(out=out, running=lambda: pytest.fail('no detector call when nothing moves')) == 0
    assert out.getvalue().startswith('nothing to do')


def test_detector_matches_fleet_processes_only():
    dirs = {'/h/.cmux-fleet-dev', '/h/.local/state/cmux-fleet'}
    py = '/usr/bin/python3'
    assert home_migrate._fleet_process([py, '/r/fleetmux/fleet.py', 'daemon', '/c.json'], dirs)
    assert home_migrate._fleet_process([py, '/r/cmux_fleet/fleet.py', 'daemon', '/c.json'], dirs)  # older release
    assert home_migrate._fleet_process([py, '-m', 'cmux_fleet.fleet', 'daemon', '/c.json'], dirs)
    assert home_migrate._fleet_process([py, '-m', 'fleetmux.fleet', 'daemon', '/c.json'], dirs)
    assert home_migrate._fleet_process(['/r/.venv/bin/cmux-fleet', 'status'], dirs)
    assert home_migrate._fleet_process(['/r/.venv/bin/fleetmux-py', 'status'], dirs)
    assert home_migrate._fleet_process(['/r/bin/fleetmux', 'server', '--runtime', '/x'], dirs)
    assert home_migrate._fleet_process(['tmux', '-S', '/h/.local/state/cmux-fleet/tmux/s.sock', 'new'], dirs)
    assert home_migrate._fleet_process(['/bin/bash', '/h/.cmux-fleet-dev/start.sh'], dirs)
    assert not home_migrate._fleet_process(['vim', '/somewhere/fleet.py'], dirs)
    assert not home_migrate._fleet_process(['tmux', '-S', '/tmp/other.sock'], dirs)
    assert not home_migrate._fleet_process(['cat', '/h/.cmux-fleet-devx/a'], dirs)


def test_cli_in_a_new_process(home):
    old_layout(home)
    env = {k: v for k, v in os.environ.items() if k not in ENV_NAMES and not k.startswith(home_migrate.OLD_ENV)}
    env.update(HOME=str(home), PYTHONPATH=str(ROOT), FLEETMUX_DISCOVERY='off')

    def cli(*args):
        return subprocess.run([sys.executable, '-m', 'fleetmux.fleet', *args], cwd=ROOT, env=env,
                              capture_output=True, text=True, timeout=60)
    r = cli('home', 'migrate', '--dry-run')
    assert r.returncode == 0 and 'would mv' in r.stdout, r.stderr
    assert (home / '.cmux-fleet').is_dir() and not (home / '.cmux-fleet').is_symlink()
    r = cli('setup')
    assert r.returncode == 2 and "run 'fleetmux home migrate'" in r.stderr, r.stderr
    r = cli('status')
    assert len([line for line in r.stderr.splitlines() if 'fleetmux home migrate' in line]) == 1, r.stderr
