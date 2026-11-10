"""Fleet-side wiring of the WezTerm backend (review 2026-10-28 「超范围」): only fakes here, never the
WezTerm module's internals (backends/wezterm.py is owned by another change).

1. lifecycle_close.all_gone asks the backend's public interface, not cmux's private ``_surfaces``.
2. Fleet.run() stops with the backend's reason when its session cannot be created (cmux unchanged).
3. Sub-fleets are refused on WezTerm at every entry point, with one readable error.
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cmux_fleet import fleet, lifecycle_close, subfleet  # noqa: E402


# ---- 1. all_gone is backend-neutral ------------------------------------------------------------------

class NoSurfacesMgr:
    """A registry with only the public interface (like WezTermSessionManager): no ``_surfaces``."""

    def __init__(self, live):
        self.live = live
        self.meta_path = Path('/nonexistent/registry.json')

    def _meta(self):
        return self.meta

    def session_exists(self):
        return self.live

    def close_all(self):
        return False


def test_all_gone_uses_the_public_interface():
    meta = {'workspace': 'fleet-wz'}
    assert lifecycle_close.all_gone(NoSurfacesMgr(live=False), meta, wait=0) is True
    assert lifecycle_close.all_gone(NoSurfacesMgr(live=True), meta, wait=0) is False


def test_closer_with_a_failed_close_all_on_a_backend_without_surfaces(tmp_path, monkeypatch):
    run = tmp_path / 'run'
    run.mkdir()
    fleet.save(run / 'config.json', {'run': str(run)})
    fleet.save(run / 'result.json', {'status': 'completed'})
    monkeypatch.setattr(lifecycle_close, 'all_gone', lambda mgr, meta: ALL_GONE(mgr, meta, wait=0))
    mgr = NoSurfacesMgr(live=False)
    mgr.meta = {'run': str(run), 'surfaces': {}, 'workspace': 'fleet-wz'}
    assert lifecycle_close.close_workspace(run / 'config.json', mgr=mgr) == 'closed'
    mgr = NoSurfacesMgr(live=True)
    mgr.meta = {'run': str(run), 'surfaces': {}, 'workspace': 'fleet-wz'}
    assert lifecycle_close.close_workspace(run / 'config.json', mgr=mgr).startswith('close_failed')


ALL_GONE = lifecycle_close.all_gone


# ---- 2. Fleet.run() refuses to continue without a session ---------------------------------------------

def _run_self(backend, ok, why=''):
    started, logged = [], []
    mgr = SimpleNamespace(get_or_create_session=lambda: ok, last_error=why)
    cfg = {} if backend is None else {'backend': backend}
    return SimpleNamespace(mgr=mgr, cfg=cfg, v2=None, stopping=True, start_all=lambda: started.append(1),
                           capture_terminal_theme=lambda: None, wall_chains_load=lambda: None,  # §23 chains -> Redis
                           log=lambda kind, **data: logged.append((kind, data))), started, logged


def test_run_stops_with_the_backends_reason_when_the_session_fails():
    me, started, logged = _run_self('wezterm', False, 'wezterm-mux-server exited: address in use')
    with pytest.raises(SystemExit) as exc:
        fleet.Fleet.run(me)
    assert 'wezterm' in str(exc.value) and 'address in use' in str(exc.value)
    assert started == []  # nothing spawned
    assert logged and logged[0][0] == 'backend.session_failed'


def test_run_on_cmux_keeps_going_after_a_failed_ping():
    """cmux's get_or_create_session is `cmux ping`: a production daemon survived a transient miss and still does."""
    for backend in (None, 'cmux'):
        me, started, logged = _run_self(backend, False)
        fleet.Fleet.run(me)
        assert started == [1] and logged == []


def test_run_continues_when_the_session_is_up():
    me, started, _ = _run_self('wezterm', True)
    fleet.Fleet.run(me)
    assert started == [1]


# ---- 3. sub-fleets are refused on WezTerm ---------------------------------------------------------------

def test_backend_problem_names_wezterm_only():
    assert subfleet.backend_problem({'backend': 'cmux'}) is None
    assert subfleet.backend_problem({}) is None  # an old config is cmux
    why = subfleet.backend_problem({'backend': 'wezterm'})
    assert 'wezterm' in why and 'sub-fleet' in why


def test_check_refuses_any_sub_fleet_on_wezterm():
    f = SimpleNamespace(cfg={'backend': 'wezterm', 'task_id': None})
    assert subfleet.check(f, 'worker-1', 'M1', 1) == subfleet.backend_problem(f.cfg)
    named = {'name': 'ops', 'mode': 'service'}
    assert subfleet.check(f, 'worker-1', 'ops', 1, ready=False, named=named) == subfleet.backend_problem(f.cfg)


def test_start_adopted_is_blocked_on_wezterm():
    finished = []
    f = SimpleNamespace(cfg={'backend': 'wezterm', 'adopt': {'pane': '3', 'slot': 'worker-1'}},
                        finish=lambda status, reason: finished.append((status, reason)))
    subfleet.start_adopted(f)
    assert finished == [('blocked', subfleet.backend_problem(f.cfg))]


def _roster(tmp):
    (tmp / 'sup.md').write_text('sup\n')
    (tmp / 'child.yaml').write_text('workers:\n  - name: ops-1\n')
    path = tmp / 'roster.yaml'
    path.write_text('workers:\n  - name: worker-1\n  - name: worker-2\n'
                    'subfleets:\n  - name: ops\n    supervisor_slot: worker-1\n    supervisor_role: sup.md\n'
                    '    roster: child.yaml\n    autostart: false\n    drain_s: 60\n')
    return path


def test_roster_hot_load_with_subfleets_is_rejected_on_wezterm(tmp_path):
    logged, told = [], {}
    me = SimpleNamespace(roster_digest=None, cfg={'backend': 'wezterm'}, desired=['worker-1'],
                         log=lambda kind, **data: logged.append((kind, data)),
                         r=SimpleNamespace(hset=lambda k, mapping: told.update(mapping)))
    fleet.Fleet.reload_roster(me, _roster(tmp_path))
    assert logged and logged[0][0] == 'roster.rejected'
    assert any('wezterm' in e for e in logged[0][1]['errors'])
    assert 'wezterm' in told.get('rejected', '')  # the supervisor hears of it too (§16)
    assert 'subfleets' not in me.cfg


def test_start_with_a_subfleets_roster_is_refused_on_wezterm(tmp_path, monkeypatch):
    from test_pi import mock_start
    args, ran = mock_start(tmp_path, monkeypatch)
    spawned = []
    mgr = SimpleNamespace(meta_path=tmp_path / 'state' / 'cmux-fleet.json', session_exists=lambda: False,
                          spawn_window=lambda *a, **kw: spawned.append(a) or SimpleNamespace(success=True))
    monkeypatch.setattr(fleet, 'resolve_backend', lambda requested=None, platform=None: ('wezterm', None))
    monkeypatch.setattr(fleet, 'backends_check', lambda name: True)
    monkeypatch.setattr(fleet, 'manager_for', lambda cfg: mgr)
    with pytest.raises(SystemExit) as exc:
        fleet.main(args + ['--backend', 'wezterm', '--roster', str(_roster(tmp_path))])
    assert 'wezterm' in str(exc.value) and 'sub-fleet' in str(exc.value)
    assert spawned == [] and not any('--unixsocket' in argv for argv in ran)  # nothing provisioned


# ---- 4. default backend: a Mac with cmux installed stays on cmux (core review, production safety) ------

def test_mac_with_cmux_installed_defaults_to_cmux_even_when_it_does_not_answer(monkeypatch):
    from cmux_fleet.backends import base
    monkeypatch.setattr(base, 'cmux_installed', lambda: True)
    monkeypatch.setattr(base, 'unavailable_reason', lambda name: 'cmux not available: run this inside a cmux terminal'
                        if name == 'cmux' else None)  # ssh / Terminal.app with WezTerm installed
    assert base.default_backend('darwin') == 'cmux'
    assert fleet.resolve_backend(None, platform='darwin') == (
        'cmux', 'cmux not available: run this inside a cmux terminal')


def test_wezterm_is_the_default_only_without_cmux_or_off_macos(monkeypatch):
    from cmux_fleet.backends import base
    monkeypatch.setattr(base, 'unavailable_reason', lambda name: 'no cmux' if name == 'cmux' else None)
    monkeypatch.setattr(base, 'cmux_installed', lambda: False)
    assert base.default_backend('darwin') == 'wezterm'
    monkeypatch.setattr(base, 'cmux_installed', lambda: True)
    assert base.default_backend('linux') == 'fleetmux'  # M3 py-switch: off macOS the default is fleetmux


def test_start_without_backend_where_cmux_does_not_answer_gives_the_cmux_reason(tmp_path, monkeypatch):
    from test_pi import mock_start
    from cmux_fleet import cmux_manager
    from cmux_fleet.backends import base
    args, ran = mock_start(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(base, 'cmux_installed', lambda: True)
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: False)
    with pytest.raises(SystemExit) as exc:
        fleet.main(args)
    assert str(exc.value).startswith('cmux not available') and not any('--unixsocket' in a for a in ran)
