"""Backend selection and inheritance (docs/design.md「终端后端」, design §5) and capability degradation (§4.3).

The rules: ``start --backend`` wins; without it cmux on macOS when cmux is usable, else WezTerm; an
explicitly chosen backend that cannot run is refused before anything is provisioned; the choice is
written into config.json and followed by --resume and every sub-fleet. ``supports_browser`` false
means no history tab: the page is announced instead of opened, and a page without links to click
skips the jump poll.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fleetmux import backends, cmux_manager, fleet  # noqa: E402
from fleetmux.backends import base  # noqa: E402


# ---- selection ------------------------------------------------------------------------------------

def test_flag_choice_is_used_and_checked(monkeypatch):
    seen = []

    def check(name):
        seen.append(name)
        return name
    monkeypatch.setattr(backends, "check_backend", check)
    assert fleet.resolve_backend('wezterm') == ('wezterm', None)
    assert seen == ['wezterm']


def test_unknown_flag_is_reported_not_crashed():
    name, why = fleet.resolve_backend('zellij')
    assert name == 'zellij' and 'cmux, wezterm' in why


def test_unusable_flag_reports_the_reason(monkeypatch):
    def refuse(name):
        raise base.InterfaceError('missing: wezterm-mux-server')
    monkeypatch.setattr(backends, "check_backend", refuse)
    name, why = fleet.resolve_backend('wezterm')
    assert name == 'wezterm' and why == 'missing: wezterm-mux-server'


def test_no_flag_follows_design_5(monkeypatch):
    monkeypatch.setattr(base, 'cmux_installed', lambda: True)  # this host's cmux binary must not decide it
    monkeypatch.setattr(base, 'unavailable_reason', lambda name: None)
    assert fleet.resolve_backend(None, platform='darwin') == ('cmux', None)
    monkeypatch.setattr(base, 'unavailable_reason', lambda name: 'no cmux' if name == 'cmux' else None)
    monkeypatch.setattr(base, 'cmux_installed', lambda: False)  # a Mac with cmux installed stays on cmux
    assert fleet.resolve_backend(None, platform='darwin') == ('wezterm', None)  # WezTerm is usable
    assert fleet.resolve_backend(None, platform='linux') == ('fleetmux', None)  # Linux never probes cmux (M3: fleetmux)


def _parse(argv):
    return fleet.cli().parse_args(argv)


def test_start_parser_accepts_only_the_two_backends():
    assert _parse(['start', '--backend', 'wezterm']).backend == 'wezterm'
    assert _parse(['start']).backend is None  # default stays None: resolve_backend decides
    with pytest.raises(SystemExit):
        _parse(['start', '--backend', 'tmux'])


def test_run_backend_and_manager_for_follow_the_config(tmp_path):
    cfg = {'backend': 'wezterm', 'session': 'cmux-fleet:M0', 'cmux_state_dir': str(tmp_path)}
    assert fleet.run_backend(cfg) == 'wezterm'
    assert fleet.run_backend({}) == 'cmux'  # a config written before the choice existed stays on cmux
    mgr = fleet.manager_for({k: v for k, v in cfg.items() if k != 'backend'})
    assert isinstance(mgr, cmux_manager.CmuxSessionManager)


def test_backends_check_asks_the_cmux_probe(monkeypatch):
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: False)
    assert fleet.backends_check('cmux') is False
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: True)
    assert fleet.backends_check('cmux') is True
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: (_ for _ in ()).throw(RuntimeError('x')))
    assert fleet.backends_check('cmux') is False  # a probe that raises means "cannot run", never a crash


def test_unavailable_text_names_what_is_missing(monkeypatch):
    monkeypatch.setattr(backends, 'unavailable_reason', lambda name: 'missing: wezterm, wezterm-mux-server')
    assert 'missing: wezterm, wezterm-mux-server' in fleet.unavailable_text('wezterm')


def test_sub_fleet_manager_inherits_the_backend(tmp_path, monkeypatch):
    """subfleet builds child registries with manager_for(child_cfg); the child config carries the backend."""
    from fleetmux import subfleet
    parent = {'backend': 'wezterm', 'run': '/parent', 'workers': ['worker-1'], 'title_path': ['fleet'],
              'session': 'cmux-fleet'}
    fake = SimpleNamespace(cfg=parent, path=Path('/parent/config.json'), runtime_cfg=lambda slot: parent,
                           agent=lambda slot: {'generation': 0},
                           mgr=SimpleNamespace(workspace_id=lambda: 'ws-parent'),
                           label=lambda slot: slot)  # §46: child_config names the supervising generation
    monkeypatch.setattr(fleet, 'agent_cli', lambda cfg, slot: 'codex')
    monkeypatch.setattr(fleet, 'agent_cwd', lambda cfg, slot: '/parent/workspace/worker-1')
    child = subfleet.child_config(fake, 'worker-1', 'M0', 2, Path('/child'), 'pane-1', None, {}, 'task')
    assert child['backend'] == 'wezterm'  # the child config carries the parent's backend field
    calls = []
    monkeypatch.setattr(backends, 'build', lambda name, session, **kw: calls.append((name, session)))
    fleet.manager_for({'backend': 'wezterm', 'session': 'cmux-fleet:M0'})
    assert calls == [('wezterm', 'cmux-fleet:M0')]


# ---- degradation: no browser surface --------------------------------------------------------------

def test_refresh_history_skips_the_browser_without_one(monkeypatch):
    f = SimpleNamespace(run_dir='/run', history_slots=lambda: [], history_links=lambda: (),
                        r=SimpleNamespace(hget=lambda *a: 'pane-h'), mgr=SimpleNamespace(supports_browser=False,
                                                                                       reloaded=[]),
                        log=lambda *a, **k: None)
    f.mgr.reload_browser = lambda pane: f.mgr.reloaded.append(pane) or True
    monkeypatch.setattr(fleet.history, 'write_page', lambda *a, **k: None)
    fleet.Fleet.refresh_history(f)
    assert f.mgr.reloaded == []  # nothing on screen to reload


def test_refresh_history_reloads_the_browser_when_there_is_one(monkeypatch):
    f = SimpleNamespace(run_dir='/run', history_slots=lambda: [], history_links=lambda: (),
                        r=SimpleNamespace(hget=lambda *a: 'pane-h'), mgr=SimpleNamespace(reloaded=[]),
                        log=lambda *a, **k: None)
    assert not hasattr(f.mgr, 'supports_browser')  # a manager without the flag counts as cmux
    f.mgr.reload_browser = lambda pane: f.mgr.reloaded.append(pane) or True
    monkeypatch.setattr(fleet.history, 'write_page', lambda *a, **k: None)
    fleet.Fleet.refresh_history(f)
    assert f.mgr.reloaded == ['pane-h']


def test_announce_history_page_prints_the_path_without_a_browser(monkeypatch, capsys):
    f = SimpleNamespace(run_dir='/run', cfg={'backend': 'wezterm'},
                        mgr=SimpleNamespace(supports_browser=False), log=lambda *a, **k: None)
    opened = []
    monkeypatch.setattr(fleet.shutil, 'which', lambda name: '/usr/bin/' + name)
    monkeypatch.setattr(fleet.subprocess, 'Popen', lambda argv, **kw: opened.append(argv))
    fleet.Fleet.announce_history_page(f)
    out = capsys.readouterr().out
    assert 'history.html' in out and '/run' in out
    assert opened and opened[0][-1].endswith('history.html')  # system opener, once


def test_announce_history_page_does_nothing_with_a_browser(monkeypatch, capsys):
    f = SimpleNamespace(run_dir='/run', cfg={}, mgr=SimpleNamespace(supports_browser=True),
                        log=lambda *a, **k: None)
    monkeypatch.setattr(fleet.subprocess, 'Popen', lambda *a, **k: pytest.fail('must not open a window'))
    fleet.Fleet.announce_history_page(f)
    assert capsys.readouterr().out == ''


def test_history_jump_poll_is_off_without_a_browser(monkeypatch):
    """subfleet.history_jump: no page on screen, so no cmux call per tick."""
    from fleetmux import subfleet
    f = SimpleNamespace(cfg={'parent_config': 'p'}, run_dir='/run', jump_next=0,
                        mgr=SimpleNamespace(supports_browser=False, browser_url=lambda p: pytest.fail('no call')))
    monkeypatch.setattr(subfleet, '_fleet', lambda: SimpleNamespace(SERVICE='svc'))
    assert subfleet.follow_jump(f) is None
