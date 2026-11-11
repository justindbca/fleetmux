"""One command name: `fleetmux` (docs/design.md §50).

What these tests pin on the Python side (the Rust dispatcher has its own tests in
rust/crates/app/tests/forward_cli.rs):

- FLEETMUX_* environment names, with their legacy CMUX_FLEET_* twins (env_names.LEGACY). Both names work; the new
  one wins; both set and different → the new one, plus one stderr warning. Every reader reads the new name:
  `fleetmux/__init__.py` gives each pair one value before any module reads the environment, so children see
  one value too.
- argparse shows the command as `fleetmux`.
- `gui` (Python) always execs `fleetmux gui --runtime <dir>`: the Rust binary forwards only a `gui` without
  `--runtime` to Python, so this form can never come back (no loop).
- scripts/install_release.sh installs into ~/fleetmux-releases/<sha> and maintains ~/fleetmux-current; the old
  paths stay usable (a missing ~/cmux-fleet-releases becomes a link to the new root; an existing old directory is
  never moved or removed; ~/cmux-fleet-current, when it is a link or missing, follows --activate as before).
"""
import io
import os
import subprocess
import sys
from pathlib import Path

import test_install_release as inst  # noqa: E402  (tests/ is on sys.path: conftest imports test_fleet)
from test_fleetmux_history import gui_world, make_run  # noqa: E402
from test_install_release import APP_SHA, OTHER_SHA, make_bin_dir, make_python, world  # noqa: E402,F401

from fleetmux import fleet  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
NEW_OLD = (('FLEETMUX_HOME', 'CMUX_FLEET_HOME'), ('FLEETMUX_STATE', 'CMUX_FLEET_STATE'),
           ('FLEETMUX_CONFIG', 'CMUX_FLEET_CONFIG'))


# ---- environment names -------------------------------------------------------------------------------------------

def _env_names():
    from fleetmux import env_names
    return env_names


def test_env_new_name_is_copied_over_the_old_one():
    env, err = {'FLEETMUX_HOME': '/new/home', 'FLEETMUX_STATE': '/new/state', 'FLEETMUX_CONFIG': '/new/f.yaml'}, io.StringIO()
    _env_names().apply(env, err)
    assert env['CMUX_FLEET_HOME'] == '/new/home' and env['CMUX_FLEET_STATE'] == '/new/state'
    assert env['CMUX_FLEET_CONFIG'] == '/new/f.yaml'
    assert err.getvalue() == ''


def test_env_old_name_alone_still_works():
    env, err = {'CMUX_FLEET_HOME': '/old/home', 'CMUX_FLEET_STATE': '/old/state'}, io.StringIO()
    _env_names().apply(env, err)
    assert env == {'CMUX_FLEET_HOME': '/old/home', 'CMUX_FLEET_STATE': '/old/state',
                   'FLEETMUX_HOME': '/old/home', 'FLEETMUX_STATE': '/old/state'}  # nothing else invented
    assert err.getvalue() == ''


def test_env_every_renamed_variable_has_a_legacy_twin():
    legacy = _env_names().LEGACY
    assert legacy['FLEETMUX_BIN'] == 'CMUX_FLEET_FLEETMUX' and legacy['FLEETMUX_THEME'] == 'CMUX_FLEET_FLEETMUX_THEME'
    assert legacy['FLEETMUX_WEB_BIN'] == 'CMUX_FLEET_FLEETMUX_WEB' and legacy['FLEETMUX_PI_SLOT'] == 'CMUX_FLEET_PI_SLOT'
    env = {'CMUX_FLEET_FLEETMUX': '/bin/fm', 'FLEETMUX_PI_SLOT': 'worker-1'}
    _env_names().apply(env, io.StringIO())
    assert env['FLEETMUX_BIN'] == '/bin/fm' and env['CMUX_FLEET_PI_SLOT'] == 'worker-1'
    assert _env_names().with_legacy({'FLEETMUX_PI_SLOT': 's', 'PI_X': '1'}) == {
        'FLEETMUX_PI_SLOT': 's', 'CMUX_FLEET_PI_SLOT': 's', 'PI_X': '1'}


def test_env_both_equal_is_silent_and_both_different_uses_the_new_one_with_one_warning():
    env = {'FLEETMUX_HOME': '/same', 'CMUX_FLEET_HOME': '/same',
           'FLEETMUX_STATE': '/new/state', 'CMUX_FLEET_STATE': '/old/state'}
    err = io.StringIO()
    _env_names().apply(env, err)
    assert env['CMUX_FLEET_HOME'] == '/same' and env['CMUX_FLEET_STATE'] == '/new/state'
    lines = err.getvalue().splitlines()
    assert len(lines) == 1, lines
    assert 'FLEETMUX_STATE' in lines[0] and 'CMUX_FLEET_STATE' in lines[0] and '/new/state' in lines[0], lines
    _env_names().apply(env, err)  # a second pass (or a child that inherits the result) finds them equal: no repeat
    assert len(err.getvalue().splitlines()) == 1


def test_env_empty_new_name_counts_as_unset():
    env = {'FLEETMUX_HOME': '', 'CMUX_FLEET_HOME': '/old'}
    _env_names().apply(env, io.StringIO())
    assert env['CMUX_FLEET_HOME'] == '/old' and env['FLEETMUX_HOME'] == '/old'


def _probe(tmp_path, extra):
    """A fresh interpreter importing the package the way every entry point does; prints what the readers saw."""
    env = {k: v for k, v in os.environ.items() if k not in {n for pair in NEW_OLD for n in pair}}
    env.update(extra)
    code = ('import sys; sys.path.insert(0, %r)\n'
            'from fleetmux import fleet, cmux_manager, fleet_config\n'
            'from fleetmux.backends.fleetmux import FleetmuxSessionManager\n'
            'print(fleet.HOME); print(cmux_manager.STATE_DIR); print(fleet_config.user_path())\n'
            'print(FleetmuxSessionManager().state_dir)\n' % str(ROOT))
    return subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, env=env, timeout=60)


def test_env_new_names_reach_every_reader_in_a_fresh_process(tmp_path):
    home, state, cfg = tmp_path / 'h', tmp_path / 's', tmp_path / 'f.yaml'
    state.mkdir()
    done = _probe(tmp_path, {'FLEETMUX_HOME': str(home), 'FLEETMUX_STATE': str(state), 'FLEETMUX_CONFIG': str(cfg),
                             'CMUX_FLEET_HOME': str(tmp_path / 'old-home')})
    assert done.returncode == 0, done.stderr
    out = done.stdout.splitlines()
    assert out[0] == str(home) and out[1] == str(state) and out[2] == str(cfg), out
    assert out[3] == str(state.resolve()), out
    warnings = [l for l in done.stderr.splitlines() if 'FLEETMUX_HOME' in l]
    assert len(warnings) == 1 and 'CMUX_FLEET_HOME' in warnings[0], done.stderr


def test_env_old_names_alone_reach_every_reader_in_a_fresh_process(tmp_path):
    home, state = tmp_path / 'h', tmp_path / 's'
    state.mkdir()
    done = _probe(tmp_path, {'CMUX_FLEET_HOME': str(home), 'CMUX_FLEET_STATE': str(state)})
    assert done.returncode == 0, done.stderr
    out = done.stdout.splitlines()
    assert out[0] == str(home) and out[1] == str(state), out
    assert 'FLEETMUX_' not in done.stderr, done.stderr


# ---- command name ------------------------------------------------------------------------------------------------

def test_help_shows_the_command_as_fleetmux():
    done = subprocess.run([sys.executable, str(ROOT / 'fleetmux' / 'fleet.py'), '--help'], capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith('usage: fleetmux '), done.stdout[:200]
    assert 'usage: cmux-fleet' not in done.stdout


def test_subcommand_usage_errors_name_fleetmux():
    done = subprocess.run([sys.executable, str(ROOT / 'fleetmux' / 'fleet.py'), 'no-such-command'],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 2
    assert done.stderr.startswith('usage: fleetmux '), done.stderr[:200]


# ---- gui: never a loop --------------------------------------------------------------------------------------------

def test_python_gui_execs_the_rust_form_with_runtime_so_it_is_never_forwarded_back(tmp_path, monkeypatch):
    """Rust forwards `fleetmux gui` without --runtime to Python; Python's gui must exec the --runtime form only."""
    mgr, execs = gui_world(tmp_path, monkeypatch)
    make_run(tmp_path, 'fleet-old', closed_at=1790000000)
    monkeypatch.setattr(fleet, 'daemon_running', lambda run: False)
    fleet.main(['gui'])
    assert len(execs) == 1, execs
    _, argv = execs[0]
    assert argv[1:3] == ['gui', '--runtime'] and argv[3] == str(mgr.runtime_root), argv


# ---- install_release.sh: new paths, old paths kept ----------------------------------------------------------------

def run_default(world, args, env_extra=None):
    """install_release.sh with its default releases root (no FLEETMUX_RELEASES / CMUX_FLEET_RELEASES)."""
    env = {k: v for k, v in os.environ.items()
           if k not in ('FLEETMUX_PYTHON', 'FLEETMUX_RELEASES', 'FLEETMUX_TOOL_SHA',
                        'CMUX_FLEET_PYTHON', 'CMUX_FLEET_RELEASES', 'CMUX_FLEET_TOOL_SHA')}
    env.update(HOME=str(world['home']), FAKE_PY_LOG=str(world['log']), FAKE_REAL_PY=sys.executable)
    env.update(env_extra or {})
    env['PATH'] = str(world['bin']) + os.pathsep + '/usr/bin:/bin'
    return subprocess.run(['bash', str(inst.SCRIPT)] + [str(a) for a in args], capture_output=True, text=True,
                          env=env, timeout=120)


def test_install_defaults_to_fleetmux_releases_and_links_the_missing_old_paths(world):
    make_python(world['bin'])
    home = world['home']
    done = run_default(world, [APP_SHA, world['tarball'], '--bin-dir', make_bin_dir(world['tmp'] / 'b'),
                               '--activate'])
    assert done.returncode == 0, done.stderr
    rel = home / 'fleetmux-releases' / APP_SHA
    assert (rel / 'bin' / 'fleetmux').is_file() and not (rel / 'bin' / 'fleetmux').is_symlink()
    old_root = home / 'cmux-fleet-releases'
    assert old_root.is_symlink() and Path(os.readlink(old_root)) == home / 'fleetmux-releases'
    assert (old_root / APP_SHA / 'bin' / 'fleetmux').is_file()  # the old path reaches the same release
    assert Path(os.readlink(home / 'fleetmux-current')) == rel
    assert Path(os.readlink(home / 'cmux-fleet-current')) == rel  # old name follows --activate, as before


def test_install_never_moves_an_existing_old_releases_dir_and_moves_the_old_current_link(world):
    make_python(world['bin'])
    home = world['home']
    old_root = home / 'cmux-fleet-releases'
    old_rel = old_root / OTHER_SHA
    old_rel.mkdir(parents=True)
    (old_rel / 'marker').write_text('running fleet pins this path\n')
    (home / 'cmux-fleet-current').symlink_to(old_rel)
    done = run_default(world, [APP_SHA, world['tarball'], '--bin-dir', make_bin_dir(world['tmp'] / 'b'),
                               '--activate'])
    assert done.returncode == 0, done.stderr
    rel = home / 'fleetmux-releases' / APP_SHA
    assert rel.is_dir()
    assert old_root.is_dir() and not old_root.is_symlink()  # not moved, not replaced by a link
    assert (old_rel / 'marker').read_text() == 'running fleet pins this path\n'
    assert sorted(p.name for p in old_root.iterdir()) == [OTHER_SHA]  # nothing new written into the old root
    assert Path(os.readlink(home / 'fleetmux-current')) == rel
    assert Path(os.readlink(home / 'cmux-fleet-current')) == rel


def test_install_without_activate_moves_neither_current(world):
    make_python(world['bin'])
    home = world['home']
    done = run_default(world, [APP_SHA, world['tarball']])
    assert done.returncode == 0, done.stderr
    assert (home / 'fleetmux-releases' / APP_SHA).is_dir()
    assert not os.path.lexists(home / 'fleetmux-current') and not os.path.lexists(home / 'cmux-fleet-current')


def test_install_leaves_an_old_current_that_is_a_real_directory_alone(world):
    make_python(world['bin'])
    home = world['home']
    real = home / 'cmux-fleet-current'
    real.mkdir()
    (real / 'marker').write_text('keep\n')
    done = run_default(world, [APP_SHA, world['tarball'], '--activate'])
    assert done.returncode == 0, done.stderr
    assert real.is_dir() and not real.is_symlink() and (real / 'marker').read_text() == 'keep\n'
    assert Path(os.readlink(home / 'fleetmux-current')) == home / 'fleetmux-releases' / APP_SHA
    assert 'cmux-fleet-current' in done.stdout + done.stderr  # says why it was left


def test_install_fleetmux_releases_env_wins_over_the_old_name(world):
    make_python(world['bin'])
    new_root, old_root = world['tmp'] / 'new-root', world['tmp'] / 'old-root'
    done = run_default(world, [APP_SHA, world['tarball']],
                       {'FLEETMUX_RELEASES': str(new_root), 'CMUX_FLEET_RELEASES': str(old_root)})
    assert done.returncode == 0, done.stderr
    assert (new_root / APP_SHA).is_dir() and not old_root.exists()
    # an explicit root is the operator's own layout: no links next to HOME
    assert not os.path.lexists(world['home'] / 'cmux-fleet-releases')
    assert not os.path.lexists(world['home'] / 'fleetmux-releases')
