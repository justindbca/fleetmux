"""The conftest isolation survives a test's own ``monkeypatch.undo()`` (T-31: no test reaches a live fleet).

``monkeypatch.undo()`` in a test undoes every patch on the shared ``monkeypatch`` instance. The suite-wide guards
(tests/conftest.py ``_isolation``) use their own ``pytest.MonkeyPatch``, so after an undo the test still runs against
its per-test fleet home, join scan root and private config, and no real window can open. After the undo these tests
only compare paths and objects: nothing is called that could reach the machine if the guard were gone.
"""
import os
from pathlib import Path

from fleetmux import cmux_manager, fleet, fleet_config, gui_keep, join, paths


def _undo_something(monkeypatch):
    monkeypatch.setattr(fleet, 'interactive_terminal', lambda: True)   # any patch of the test's own
    monkeypatch.undo()


def _under(path, root):
    return Path(path).resolve().is_relative_to(Path(root).resolve())


def test_undo_keeps_the_join_scan_root(monkeypatch, tmp_path):
    _undo_something(monkeypatch)
    assert join.scan_root() != paths.root()
    assert _under(os.environ.get(join.SCAN_ENV, '/'), tmp_path)


def test_undo_keeps_the_private_config_off(monkeypatch, tmp_path_factory):
    _undo_something(monkeypatch)
    assert fleet_config._pinned is None
    assert fleet_config.user_path() != paths.default_home() / 'fleet.yaml'
    assert _under(fleet_config.user_path(), tmp_path_factory.getbasetemp())


def test_undo_keeps_the_fleet_home_temporary(monkeypatch, tmp_path, isolated_fleet_namespace):
    _undo_something(monkeypatch)
    home, state = isolated_fleet_namespace['home'], isolated_fleet_namespace['state']
    assert os.environ.get('FLEETMUX_HOME') == str(home) and fleet.HOME == home and fleet.LATEST == home / 'latest.json'
    assert os.environ.get('FLEETMUX_STATE') == str(state) and cmux_manager.STATE_DIR == state
    assert _under(home, tmp_path) and _under(state, tmp_path) and home != paths.root()
    assert os.environ.get('FLEETMUX_CONFIG') == os.environ.get(fleet_config.USER_ENV)  # the per-test private file


def test_undo_keeps_the_window_guard(monkeypatch):
    _undo_something(monkeypatch)
    assert gui_keep.spawn.__name__ == 'refuse'           # identity only: never call it
    assert gui_keep.tick.__name__ == '<lambda>'


def test_undo_keeps_the_start_and_discovery_guards(monkeypatch, tmp_path):
    _undo_something(monkeypatch)
    assert fleet.run_start.__name__ == 'refuse' and hasattr(fleet.run_start, 'real')
    assert os.environ.get('FLEETMUX_DISCOVERY') == 'off'
    assert _under(os.environ.get('FLEETMUX_CONFIG_DIR', '/'), tmp_path)
    assert fleet.sweep_orphan_redis.__name__ == '<lambda>'


def test_a_test_override_still_wins_and_its_undo_returns_to_isolation(monkeypatch, tmp_path):
    isolated = os.environ[join.SCAN_ENV]
    monkeypatch.setenv(join.SCAN_ENV, str(tmp_path / 'my-own-root'))
    assert join.scan_root() == tmp_path / 'my-own-root'
    monkeypatch.undo()
    assert os.environ.get(join.SCAN_ENV) == isolated
