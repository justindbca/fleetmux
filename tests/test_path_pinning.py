"""Isolated locations reach internal processes through config.json, not the environment.

cmux starts the daemon, actors and hooks in fresh login shells; they need not inherit
CMUX_FLEET_HOME / CMUX_FLEET_STATE / CMUX_BIN, so `start` pins them and every entry point applies them."""
import json
from pathlib import Path

from cmux_fleet import cmux_manager, fleet, fleet_config


def pinned(monkeypatch):
    for mod, name in ((fleet, 'HOME'), (fleet, 'LATEST'), (cmux_manager, 'CMUX'), (cmux_manager, 'STATE_DIR'),
                      (fleet_config, '_pinned')):
        monkeypatch.setattr(mod, name, getattr(mod, name))


def test_pin_paths_makes_the_config_win_over_the_environment(tmp_path, monkeypatch):
    pinned(monkeypatch)
    monkeypatch.setenv('CMUX_FLEET_HOME', '/nonexistent/home-from-env')
    cfg = {'fleet_home': str(tmp_path / 'home'), 'cmux_bin': str(tmp_path / 'bin/cmux'),
           'cmux_state_dir': str(tmp_path / 'state')}
    assert fleet.pin_paths(cfg) is cfg
    assert fleet.HOME == tmp_path / 'home' and fleet.LATEST == tmp_path / 'home/latest.json'
    assert cmux_manager.CMUX == str(tmp_path / 'bin/cmux')
    assert cmux_manager.STATE_DIR == tmp_path / 'state'
    assert fleet.codex_home({}) == tmp_path / 'home/codex-home'


def test_pin_paths_leaves_old_configs_alone(monkeypatch):
    pinned(monkeypatch)
    before = (fleet.HOME, fleet.LATEST, cmux_manager.CMUX, cmux_manager.STATE_DIR)
    fleet.pin_paths({'run': '/x'})
    assert (fleet.HOME, fleet.LATEST, cmux_manager.CMUX, cmux_manager.STATE_DIR) == before


def test_internal_entry_points_apply_the_pins_before_running(tmp_path, monkeypatch):
    pinned(monkeypatch)
    cfg_path = tmp_path / 'config.json'
    cfg_path.write_text(json.dumps({'run': str(tmp_path), 'fleet_home': str(tmp_path / 'h'),
                                    'cmux_bin': '/iso/bin/cmux', 'cmux_state_dir': str(tmp_path / 's')}))
    seen = {}
    for cmd, fn in (('daemon', 'daemon'), ('actor', 'actor'), ('notify', 'notify')):
        monkeypatch.setattr(fleet, fn, lambda *a, _c=cmd, **k: seen.__setitem__(
            _c, (fleet.LATEST, cmux_manager.CMUX, cmux_manager.STATE_DIR)))
    fleet.main(['daemon', str(cfg_path)])
    fleet.main(['actor', str(cfg_path), 'supervisor', 'p.txt'])
    fleet.main(['notify', str(cfg_path), 'supervisor', '1', '{}'])
    want = (tmp_path / 'h/latest.json', '/iso/bin/cmux', tmp_path / 's')
    assert seen == {'daemon': want, 'actor': want, 'notify': want}


def test_unreadable_config_does_not_crash_the_pinning(tmp_path, monkeypatch):
    pinned(monkeypatch)
    calls = []
    monkeypatch.setattr(fleet, 'daemon', lambda *a, **k: calls.append(a))
    fleet.main(['daemon', str(tmp_path / 'missing.json')])
    assert calls  # the entry point itself decides how to report it


def test_start_options_pin_home_and_cmux_and_default_the_run_dirs(tmp_path, monkeypatch):
    pinned(monkeypatch)
    got = {}
    monkeypatch.setattr(fleet, 'start', lambda args: got.update(vars(args), HOME=fleet.HOME,
                                                                CMUX=cmux_manager.CMUX))
    fleet.main(['start', '--fleet-home', str(tmp_path / 'fh'), '--cmux-bin', str(tmp_path / 'cmux'),
                '--cmux-state-dir', str(tmp_path / 'st')])
    assert got['HOME'] == (tmp_path / 'fh').resolve() and got['CMUX'] == str((tmp_path / 'cmux').resolve())
    assert got['runs_dir'] == str((tmp_path / 'fh').resolve() / 'runs')
    assert got['codex_home'] == str((tmp_path / 'fh').resolve() / 'codex-home')
    assert got['cmux_state_dir'] == str(tmp_path / 'st')
