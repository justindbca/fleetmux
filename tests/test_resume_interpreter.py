"""A run built by release A and resumed by release B runs on B's interpreter (docs/design.md section 60).

``start --resume`` (``fleet.resume``) writes the resuming release's interpreter (``releases.python()``: that release's
own ``.venv``, else the current interpreter) into the run config and into every sub-fleet config whose daemon is not
running. The daemon, every actor started after the resume (and the hook lines it writes), the history pane (the
daemon's own interpreter) and every relaunched sub-fleet then run on it.

A and B are fake release directories in tmp with a ``.venv/bin/<python>`` symlink each (never executed: every
launch is stubbed); ``releases.ROOT`` points at B, which is how "the resuming CLI is release B" is modelled.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import test_subfleet as ts
from test_subfleet import start_tree
from fleetmux import cmux_manager, fleet, releases, subfleet

PY = Path(sys.executable).name


def release(tmp_path, name):
    root = tmp_path / 'fleetmux-releases' / name
    (root / '.venv' / 'bin').mkdir(parents=True)
    (root / '.venv' / 'bin' / PY).symlink_to(sys.executable)
    return root, str(root / '.venv' / 'bin' / PY)


@pytest.fixture
def ab(tmp_path, monkeypatch):
    _, a = release(tmp_path, 'A')
    b_root, b = release(tmp_path, 'B')
    monkeypatch.setattr(subfleet, 'default_launch', ts.no_launch)
    gen = start_tree(tmp_path, monkeypatch, python=a)
    t = next(gen)
    t.a, t.b, t.b_root = a, b, b_root
    yield t
    for _ in gen:
        pass


def finished_child(t, name):
    """A sub-fleet that ran and was collected under release A (never relaunched by a resume)."""
    d = t.f.run_dir / 'sub' / name
    d.mkdir(parents=True)
    fleet.save(d / 'config.json', {'run': str(d), 'python': t.a})
    (d / 'subfleet.json').write_text(json.dumps({'task_id': name, 'state': 'returned', 'outcome': 'completed',
                                                  'created': 1.0, 'run': str(d)}))
    return d


def resume_with_b(t, monkeypatch, running=()):
    """``start --resume`` from release B, up to the daemon launch (stubbed)."""
    monkeypatch.setattr(releases, 'ROOT', t.b_root)
    launched = []
    monkeypatch.setattr(fleet, 'manager_for', lambda cfg: SimpleNamespace(
        spawn_window=lambda *a, **k: launched.append(a) or SimpleNamespace(success=True)))
    monkeypatch.setattr(fleet, 'LATEST', t.tmp / 'latest.json')
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: True)
    monkeypatch.setattr(fleet, 'daemon_running', lambda path: Path(path) in {Path(p) for p in running})
    t.f.v2.close()
    args = SimpleNamespace(resume=str(t.f.run_dir), extend_hours=None, max_attempts=None, roster=None)
    assert fleet.resume(t.f.run_dir, args) is True and len(launched) == 1
    monkeypatch.setattr(fleet, 'manager_for', t.world.manager)  # the daemon below spawns through the fake world
    return launched[0]


def python_of(path):
    return fleet.load(Path(path) / 'config.json')['python']


def test_a_run_built_by_A_resumed_by_B_uses_B_everywhere(ab, monkeypatch, capsys):
    t = ab
    rec, _ = ts.spawn_m0(t)
    child = Path(rec['run'])
    done = finished_child(t, 'M9')
    assert python_of(t.f.run_dir) == python_of(child) == python_of(done) == t.a  # built by A
    name, command = resume_with_b(t, monkeypatch)[:2]
    assert t.b in command.split() and t.a not in command  # the daemon (and so its history pane) run on B
    assert python_of(t.f.run_dir) == t.b
    assert python_of(child) == t.b and python_of(done) == t.b  # every sub-fleet config, running or finished
    assert 'interpreter' in capsys.readouterr().out
    # the daemon resumes from that config: the actors it starts, their hook lines and the child relaunch are B
    again = fleet.Fleet(t.f.path, t.f.r, t.world.manager(fleet.load(t.f.path)), sessions=t.sessions)
    t.fleets[0] = again
    t.f = again
    again.resume_requested = True
    before = len(getattr(again.mgr, 'spawned', []))
    again.v2.resume_run()
    started = getattr(again.mgr, 'spawned', [])[before:]
    assert started and all(kw['cli_command'].startswith(t.b + ' ') for kw in started)
    cfg = fleet.load(again.path)
    argv = fleet.claude_argv(again.path, cfg, 'worker-2', 1, 'hello')
    settings = next(Path(a) for a in argv if str(a).endswith('.json') and '.fleet-settings-g' in str(a))
    text = settings.read_text()
    assert t.b in text and t.a not in text
    child_mgr = t.world.mgrs['fleetmux:M0']
    relaunch = [cmd for _, cmd in getattr(child_mgr, 'windows', []) if '--resume' in cmd]
    assert relaunch and all(t.b in cmd and t.a not in cmd for cmd in relaunch)


def test_resume_keeps_a_running_childs_config(ab, monkeypatch, capsys):
    t = ab
    rec, _ = ts.spawn_m0(t)
    child = Path(rec['run'])
    resume_with_b(t, monkeypatch, running=[child])
    assert python_of(t.f.run_dir) == t.b
    assert python_of(child) == t.a  # its live daemon would write the old value back: it switches at its own resume
    out = capsys.readouterr().out
    assert 'M0' in out and 'still running' in out


def test_a_checkout_without_a_venv_keeps_the_current_interpreter(tmp_path, monkeypatch):
    monkeypatch.setattr(releases, 'ROOT', tmp_path / 'checkout')
    assert releases.python() == sys.executable
    root, b = release(tmp_path, 'B')
    monkeypatch.setattr(releases, 'ROOT', root)
    assert releases.python() == b
