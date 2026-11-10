"""Terminal-backend interface (design §7.1): both backends implement every method with the same signature.

The cmux implementation is the reference. Signatures are compared as ``inspect.signature`` with the
annotation strings dropped (both modules use ``from __future__ import annotations``; only names,
order and defaults are the interface) — a worker adding a keyword to fleet without adding it to the
other backend fails here instead of at run time three panes into a fleet.
"""
import importlib
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cmux_fleet import fleet  # noqa: E402
from cmux_fleet import backends  # noqa: E402
from cmux_fleet import cmux_manager  # noqa: E402
from cmux_fleet.backends import base  # noqa: E402
from cmux_fleet.backends.base import METHODS, InterfaceError  # noqa: E402


def shape(func):
    """Parameter names, kinds and defaults; annotations ignored on purpose (see module docstring)."""
    params = inspect.signature(func).parameters
    return [(p.name, p.kind, None if p.default is inspect.Parameter.empty else repr(p.default))
            for p in params.values()]


def public_methods(cls):
    return {name for name, value in inspect.getmembers(cls) if not name.startswith('_')
            and (inspect.isfunction(value) or isinstance(value, classmethod))}


def classes():
    """Every shipped backend manager whose module is real (the WezTerm one arrives on its own branch)."""
    cls = [backends.backend_class('cmux')]
    try:
        importlib.import_module('cmux_fleet.backends.wezterm')
    except ImportError:
        return cls
    wz = backends.backend_class('wezterm')
    cls += [wz] if hasattr(wz, 'supports_browser') else []
    try:
        importlib.import_module('cmux_fleet.backends.fleetmux')
    except ImportError:
        return cls
    fm = backends.backend_class('fleetmux')
    return cls + ([fm] if hasattr(fm, 'supports_browser') else [])


def test_methods_constant_lists_every_public_method():
    """METHODS is the checklist itself: no public method of the reference escapes it, none is stale."""
    cmux = backends.backend_class('cmux')
    extra = public_methods(cmux) - set(METHODS)
    assert extra == set(), 'cmux has public methods the interface does not list: ' + str(sorted(extra))
    missing = [m for m in METHODS if not callable(getattr(cmux, m, None))]
    assert missing == [], 'interface lists methods cmux does not have: ' + str(missing)


def test_capability_flags_exist():
    for cls in classes():
        assert cls.supports_browser in (True, False)
        assert cls.nested_tabs in (True, False)
        assert hasattr(cls, 'available') and callable(cls.available)


def test_cmux_capabilities():
    cls = backends.backend_class('cmux')
    assert cls.supports_browser is True and cls.nested_tabs is True  # cmux behaviour unchanged


def test_every_backend_implements_every_method():
    for cls in classes():
        missing = [m for m in METHODS if not callable(getattr(cls, m, None))]
        assert missing == [], cls.__module__ + ' misses ' + str(missing)


def test_signatures_match_the_cmux_reference():
    ref = backends.backend_class('cmux')
    for cls in classes():
        for name in METHODS:
            want = shape(getattr(ref, name))
            have = shape(getattr(cls, name))
            if isinstance(inspect.getattr_static(ref, name), classmethod):
                # a classmethod's first parameter is named after the class in one impl and cls in the other
                have = [('cls',) + tuple(h[1:]) if i == 0 else h for i, h in enumerate(have)]
                want = [('cls',) + tuple(h[1:]) if i == 0 else h for i, h in enumerate(want)]
            assert have == want, '%s.%s%s differs from cmux%s' % (
                cls.__module__, name, tuple(x[0] for x in have), tuple(x[0] for x in want))


def test_constructors_take_the_same_arguments():
    ref = shape(backends.backend_class('cmux').__init__)
    ref = [('session_name' if h[0] == 'self' else h[0],) + tuple(h[1:]) for h in ref]
    for cls in classes():
        have = shape(cls.__init__)
        have = [('session_name' if h[0] == 'self' else h[0],) + tuple(h[1:]) for h in have]
        # both take (session_name='cmux-fleet', agents_title, control_title, state_dir); a backend may
        # add trailing keyword-only options, never reorder or drop one of these four
        assert [h[0] for h in have][:4] == [h[0] for h in ref][:4], cls.__module__


def test_backend_build_constructs_the_named_one(tmp_path):
    mgr = backends.build('cmux', 'iface-test', state_dir=tmp_path)
    assert isinstance(mgr, backends.backend_class('cmux'))
    assert mgr.meta_path.parent == tmp_path.resolve()


def test_unknown_backend_is_refused():
    with pytest.raises(InterfaceError) as exc:
        base.normalize_backend('tmux')
    assert 'cmux, wezterm' in str(exc.value)
    with pytest.raises(InterfaceError):
        base.check_backend('zellij')


def test_names_are_normalized():
    assert base.normalize_backend(' WezTerm ') == 'wezterm'
    assert base.normalize_backend(None) is None


def test_default_backend_on_macos_prefers_cmux(monkeypatch):
    monkeypatch.setattr(base, 'cmux_installed', lambda: True)
    monkeypatch.setattr(base, 'unavailable_reason', lambda name: None)
    assert base.default_backend('darwin') == 'cmux'
    # cmux installed but not answering (ssh / Terminal.app), WezTerm usable: still cmux, refused by its probe
    monkeypatch.setattr(base, 'unavailable_reason',
                        lambda name: 'no cmux here' if name == 'cmux' else None)
    assert base.default_backend('darwin') == 'cmux'
    # no cmux installed and no usable WezTerm either: fall through to cmux, whose own probe then reports why
    monkeypatch.setattr(base, 'cmux_installed', lambda: False)
    monkeypatch.setattr(base, 'unavailable_reason', lambda name: 'no cmux here')
    assert base.default_backend('darwin') == 'cmux'
    # no cmux installed, WezTerm usable: WezTerm
    monkeypatch.setattr(base, 'unavailable_reason',
                        lambda name: 'no cmux here' if name == 'cmux' else None)
    assert base.default_backend('darwin') == 'wezterm'
    assert base.default_backend('linux') == 'fleetmux'  # M3 py-switch


def test_default_backend_survives_a_broken_cmux(monkeypatch):
    monkeypatch.setattr(base, 'cmux_installed', lambda: (_ for _ in ()).throw(OSError('cmux binary exploded')))
    monkeypatch.setattr(base, 'unavailable_reason', lambda name: None)
    assert base.default_backend('darwin') == 'cmux'  # cannot tell: stay on cmux, never crash


def test_check_backend_refuses_an_unusable_one(monkeypatch):
    monkeypatch.setattr(base, 'unavailable_reason', lambda name: 'missing: wezterm')
    with pytest.raises(InterfaceError) as exc:
        base.check_backend('wezterm')
    assert 'missing: wezterm' in str(exc.value)


def test_capabilities_per_backend():
    cm = base.capabilities('cmux')
    assert (cm.browser, cm.nested_tabs) == (True, True)
    wz = base.capabilities('wezterm')
    assert (wz.browser, wz.nested_tabs) == (False, False)  # design §4.3 degradations


def test_forwarding_module_exports_the_moved_names():
    """cmux_manager stays a forwarding module: imports, class identity and monkeypatch targets hold."""
    from cmux_fleet import cmux_manager as fwd
    from cmux_fleet.backends import cmux as real
    assert fwd.CmuxSessionManager is real.CmuxSessionManager
    assert fwd.SpawnResult is real.SpawnResult and fwd.PaneStatus is real.PaneStatus
    for name in ('cmux_available', 'tmux_session_name', 'tmux_attach_command', 'tmux_sessions',
                 'tmux_kill_server', 'tmux_conf', 'kill_process_tree'):
        assert callable(getattr(fwd, name)), name
    for name in ('CMUX', 'STATE_DIR', 'TMUX', 'TMUX_CONF', '_run', '_wait_until', '_tmux'):
        assert hasattr(fwd, name), name


def test_forwarding_module_picks_up_monkeypatched_functions(monkeypatch, tmp_path):
    """Tests patch ``cmux_manager.cmux_available``; the manager fleet builds must see that patch."""
    from cmux_fleet import cmux_manager as fwd
    monkeypatch.setattr(fwd, 'cmux_available', lambda: True)
    monkeypatch.setattr(fleet, 'manager_for', lambda cfg: fwd.CmuxSessionManager('fwd-test', state_dir=tmp_path))
    assert fwd.CmuxSessionManager('fwd-test', state_dir=tmp_path).get_or_create_session() is True


def test_manager_for_still_builds_the_cmux_registry(tmp_path):
    """fleet's single construction point keeps its cmux behaviour (design §1, §5)."""
    cfg = {'session': 'cmux-fleet:iface', 'cmux_state_dir': str(tmp_path)}
    mgr = fleet.manager_for(cfg)
    assert isinstance(mgr, backends.backend_class('cmux'))
    assert mgr.session_name == 'cmux-fleet:iface'
    assert mgr.meta_path == (tmp_path / 'cmux-fleet:iface.json').resolve()
