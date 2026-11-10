"""Regression: no test may write the operator's live fleet namespace (operator report, pytest-8260).

`pytest-8260/test_resume_keeps_the_hot_edit0` (= `test_kit_fleet.test_resume_keeps_the_hot_edited_copy_without_the_kit`)
wrote the live `~/.cmux-fleet-layout-finish/latest.json` and pointed it at a temporary redis socket: the test
process had inherited `CMUX_FLEET_HOME` from the running fleet, so `start` recorded that live home in the run's
config.json, and `start --resume` re-pinned `fleet.LATEST` from the config and saved it there.

The guard is `tests/conftest.py::isolated_fleet_namespace` (autouse).  These tests fail if that guard is removed
or if any code path saves outside the per-test namespace.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cmux_fleet import cmux_manager, fleet
from test_advisor import fake_start


def test_every_test_runs_in_its_own_fleet_namespace(isolated_fleet_namespace):
    import os

    home, state = isolated_fleet_namespace['home'], isolated_fleet_namespace['state']
    assert os.environ['CMUX_FLEET_HOME'] == str(home)
    assert os.environ['CMUX_FLEET_STATE'] == str(state)
    assert fleet.HOME == home and fleet.LATEST == home / 'latest.json'
    assert cmux_manager.STATE_DIR == state
    # the operator's real locations are never the ones a test would write
    assert fleet.HOME != Path.home() / '.cmux-fleet'
    assert fleet.HOME != Path.home() / '.fleetmux'
    assert str(fleet.LATEST).startswith(str(home.parent))


def test_start_records_the_isolated_home_and_resume_saves_only_there(monkeypatch, tmp_path,
                                                                     isolated_fleet_namespace):
    """The exact implicated flow: start (no --fleet-home) then start --resume, with save() observed."""
    argv = fake_start(monkeypatch, tmp_path)
    fleet.main(argv)
    latest = fleet.load(tmp_path / 'latest.json')
    run = Path(latest['run'])
    cfg = fleet.load(run / 'config.json')
    assert cfg['fleet_home'] == str(isolated_fleet_namespace['home']), (
        'start must record the test namespace, not an inherited live fleet home')

    # resume re-pins HOME/LATEST from that config (fleet.pin_paths) and then saves latest.json
    monkeypatch.setattr(fleet, 'daemon_running', lambda path: False)
    monkeypatch.setattr(fleet, 'connect', lambda cfg: SimpleNamespace(hgetall=lambda key: {}))
    monkeypatch.setattr(fleet, 'manager_for', lambda cfg: SimpleNamespace(
        spawn_window=lambda *a, **kw: SimpleNamespace(success=True, error=None)))
    saved = []
    real_save = fleet.save
    monkeypatch.setattr(fleet, 'save', lambda path, data: saved.append(Path(path)) or real_save(path, data))

    fleet.main(['start', '--resume', str(run)])

    base = Path(tmp_path).parent  # the pytest tmp root; every test namespace lives below it
    assert saved, 'the resume path must have saved latest.json'
    outside = [p for p in saved if not str(p).startswith(str(base))]
    assert not outside, 'resume wrote outside the test namespace: %s' % outside
    assert fleet.LATEST.parent == isolated_fleet_namespace['home']
    assert json.loads((fleet.LATEST).read_text())['run'] == str(run)
