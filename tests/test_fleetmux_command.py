"""One command name: `fleetmux` (docs/design.md §50).

What these tests pin on the Python side (the Rust dispatcher has its own tests in
rust/crates/app/tests/forward_cli.rs):

- FLEETMUX_* environment names reach every reader, in a fresh process too.
- argparse shows the command as `fleetmux`.
- `gui` (Python) always execs `fleetmux gui --runtime <dir>`: the Rust binary forwards only a `gui` without
  `--runtime` to Python, so this form can never come back (no loop).
- scripts/install_release.sh installs into ~/fleetmux-releases/<sha> and maintains ~/fleetmux-current.
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
ENV_NAMES = ('FLEETMUX_HOME', 'FLEETMUX_STATE', 'FLEETMUX_CONFIG')


# ---- environment names -------------------------------------------------------------------------------------------

def _probe(tmp_path, extra):
    """A fresh interpreter importing the package the way every entry point does; prints what the readers saw."""
    env = {k: v for k, v in os.environ.items() if k not in ENV_NAMES}
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
    done = _probe(tmp_path, {'FLEETMUX_HOME': str(home), 'FLEETMUX_STATE': str(state), 'FLEETMUX_CONFIG': str(cfg)})
    assert done.returncode == 0, done.stderr
    out = done.stdout.splitlines()
    assert out[0] == str(home) and out[1] == str(state) and out[2] == str(cfg), out
    assert out[3] == str(state.resolve()), out


# ---- command name ------------------------------------------------------------------------------------------------

def test_help_shows_the_command_as_fleetmux():
    done = subprocess.run([sys.executable, str(ROOT / 'fleetmux' / 'fleet.py'), '--help'], capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith('usage: fleetmux '), done.stdout[:200]


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


# ---- install_release.sh: release root and current link ----------------------------------------------------------------

def run_default(world, args, env_extra=None):
    """install_release.sh with its default releases root (no FLEETMUX_RELEASES)."""
    env = {k: v for k, v in os.environ.items() if k not in ('FLEETMUX_PYTHON', 'FLEETMUX_RELEASES', 'FLEETMUX_TOOL_SHA')}
    env.update(HOME=str(world['home']), FAKE_PY_LOG=str(world['log']), FAKE_REAL_PY=sys.executable)
    env.update(env_extra or {})
    env['PATH'] = str(world['bin']) + os.pathsep + '/usr/bin:/bin'
    return subprocess.run(['bash', str(inst.SCRIPT)] + [str(a) for a in args], capture_output=True, text=True,
                          env=env, timeout=120)


def test_install_defaults_to_fleetmux_releases_and_activates_fleetmux_current(world):
    make_python(world['bin'])
    home = world['home']
    done = run_default(world, [APP_SHA, world['tarball'], '--bin-dir', make_bin_dir(world['tmp'] / 'b'),
                               '--activate'])
    assert done.returncode == 0, done.stderr
    rel = home / 'fleetmux-releases' / APP_SHA
    assert (rel / 'bin' / 'fleetmux').is_file() and not (rel / 'bin' / 'fleetmux').is_symlink()
    assert Path(os.readlink(home / 'fleetmux-current')) == rel


def test_install_without_activate_leaves_current_alone(world):
    make_python(world['bin'])
    home = world['home']
    done = run_default(world, [APP_SHA, world['tarball']])
    assert done.returncode == 0, done.stderr
    assert (home / 'fleetmux-releases' / APP_SHA).is_dir()
    assert not os.path.lexists(home / 'fleetmux-current')


def test_install_fleetmux_releases_env_is_the_root(world):
    make_python(world['bin'])
    new_root = world['tmp'] / 'new-root'
    done = run_default(world, [APP_SHA, world['tarball']], {'FLEETMUX_RELEASES': str(new_root)})
    assert done.returncode == 0, done.stderr
    assert (new_root / APP_SHA).is_dir()
    assert not os.path.lexists(world['home'] / 'fleetmux-releases')
