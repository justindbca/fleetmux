"""Review repro (cmux-fleet-backend-core, 41632170..1462b17a). Copy into tests/ and run with pytest."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cmux_fleet import backends, fleet, subfleet  # noqa: E402
from cmux_fleet.backends import base  # noqa: E402


class _Stop(Exception):
    pass


def test_spawn_child_registry_follows_parent_backend(tmp_path, monkeypatch):
    """A WezTerm fleet delegating a sub-fleet must build the child registry on WezTerm, not cmux."""
    seen = []

    def record(cfg):
        seen.append(dict(cfg))
        raise _Stop()
    monkeypatch.setattr(fleet, 'manager_for', record)
    monkeypatch.setattr(subfleet, 'check', lambda *a, **k: None)
    f = SimpleNamespace(cfg={'backend': 'wezterm', 'name': 'fleet', 'run': str(tmp_path), 'cmux_state_dir': str(tmp_path), 'title_path': ['fleet']},
                        service=True, run_dir=tmp_path, path=tmp_path / 'config.json')
    with pytest.raises(_Stop):
        subfleet.spawn_child(f, 'worker-1', 'M0', 'build the thing', 2)
    assert fleet.run_backend(seen[0]) == 'wezterm', seen[0]


def test_resume_of_an_old_run_stays_on_cmux(tmp_path, monkeypatch):
    """config.json without `backend` (run started before 1462b17a) resumed where cmux does not answer
    but WezTerm is usable: must stay cmux (refuse like before), never switch the run to WezTerm."""
    run = tmp_path / 'run'
    run.mkdir()
    cfg = {'run': str(run), 'codex_queue': False, 'codex_version': None, 'workers': ['worker-1']}
    monkeypatch.setattr(fleet, 'load', lambda path: dict(cfg))
    monkeypatch.setattr(fleet, 'pin_paths', lambda c: c)
    monkeypatch.setattr(fleet.runtime_v2, 'validate_config', lambda c: None)
    monkeypatch.setattr(fleet, 'daemon_running', lambda run: False)
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(base, 'unavailable_reason', lambda name: 'no cmux' if name == 'cmux' else None)
    probed = []
    monkeypatch.setattr(fleet, 'backends_check', lambda name: probed.append(name) or False)
    args = SimpleNamespace(max_attempts=None, roster=None, advisor=False)
    with pytest.raises(SystemExit):
        fleet.resume(run, args)
    assert probed in ([], ['cmux']), probed  # 1462b17a: ['wezterm'] -> the old cmux run would be resumed on WezTerm
