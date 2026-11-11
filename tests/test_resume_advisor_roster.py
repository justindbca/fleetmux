"""start --resume --advisor writes `advisor: on` into the roster and must still save config.json, not the roster.

Incident 2026-10-23 (an ops service fleet): the advisor loop in resume() rebound `path`, so the config was saved
over fleet-config/roster.yaml, the daemon was launched on it, and the next resume died on invalid YAML."""
import json
import sys
from types import SimpleNamespace

import pytest

from fleetmux import cmux_manager, fleet

WORKERS = ['memx-ops', 'tgog-ops']


def test_resume_with_advisor_keeps_config_and_roster_apart(tmp_path, monkeypatch):
    run = tmp_path / 'run'
    (run / 'fleet-config').mkdir(parents=True)
    roster_path = run / 'fleet-config' / 'roster.yaml'
    roster_path.write_text('workers:\n' + ''.join('  - name: ' + w + '\n' for w in WORKERS))
    cfg = dict(run=str(run), python=sys.executable, workers=WORKERS, lifecycle='service', mode='service',
               supervisor_cli='codex', worker_cli='claude', transport_version=2, allow_development_v2=True,
               roster=str(roster_path))
    fleet.save(run / 'config.json', cfg)
    monkeypatch.setattr(fleet, 'daemon_running', lambda path: False)
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: True)
    monkeypatch.setattr(fleet, 'connect', lambda cfg: object())
    monkeypatch.setattr(fleet, 'LATEST', tmp_path / 'latest.json')
    commands = []
    mgr = SimpleNamespace(spawn_window=lambda *a, **kw: commands.append(a) or SimpleNamespace(success=True))
    monkeypatch.setattr(fleet, 'manager_for', lambda cfg: mgr)

    assert fleet.resume(run, SimpleNamespace(max_attempts=None, roster=None, advisor=True))

    text = roster_path.read_text()
    assert 'advisor: on' in text and not text.lstrip().startswith('{')  # still a roster, now with the switch
    assert fleet.load(run / 'config.json')['workers'] == WORKERS  # the config went to config.json
    assert commands[0][1].split()[commands[0][1].split().index('daemon') + 1] == str(run / 'config.json')
    assert json.loads((tmp_path / 'latest.json').read_text())['config'] == str(run / 'config.json')
    # a second resume (the keeper's next try) must not trip on the roster
    assert fleet.resume(run, SimpleNamespace(max_attempts=None, roster=None, advisor=True))


def test_resume_keeps_old_style_names_and_checks_only_added_ones(tmp_path, monkeypatch):
    # Worker names end with -<number> (docs/design.md §3, 2026-10-28) for new slots only: a run started
    # before the rule resumes with its stored names; a --roster that adds a slot is checked for the new one.
    run = tmp_path / 'run'
    run.mkdir()
    cfg = dict(run=str(run), python=sys.executable, workers=WORKERS, lifecycle='service', mode='service',
               supervisor_cli='codex', worker_cli='claude', transport_version=2, allow_development_v2=True)
    fleet.save(run / 'config.json', cfg)
    monkeypatch.setattr(fleet, 'daemon_running', lambda path: False)
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: True)
    monkeypatch.setattr(fleet, 'connect', lambda cfg: object())
    monkeypatch.setattr(fleet, 'LATEST', tmp_path / 'latest.json')
    mgr = SimpleNamespace(spawn_window=lambda *a, **kw: SimpleNamespace(success=True))
    monkeypatch.setattr(fleet, 'manager_for', lambda cfg: mgr)
    assert fleet.resume(run, SimpleNamespace(max_attempts=None, roster=None, advisor=False))
    roster_path = tmp_path / 'roster.yaml'
    roster_path.write_text('workers:\n' + ''.join('  - name: ' + w + '\n' for w in WORKERS + ['extra']))
    with pytest.raises(SystemExit, match=r'workers\[2\] \(extra\): .*rename it extra-1'):
        fleet.resume(run, SimpleNamespace(max_attempts=None, roster=str(roster_path), advisor=False))
    roster_path.write_text('workers:\n' + ''.join('  - name: ' + w + '\n' for w in WORKERS + ['extra-1']))
    assert fleet.resume(run, SimpleNamespace(max_attempts=None, roster=str(roster_path), advisor=False))
    assert fleet.load(run / 'config.json')['workers'] == WORKERS  # the add lands at the daemon's first tick
