"""M3 py-switch (brief-m03 §3.5, B25): Linux's default backend becomes fleetmux, WezTerm stays selectable.

Only the ``m3`` gate runs these (``@milestone(3)``). They stay red until B25 flips
``backends/base.py::default_backend`` and ``scripts/check_deps.py::DEFAULT_BACKEND`` -- which waits for the
orchestrator's "M0-M2 Mac/VM acceptance passed" and a green ``m12`` gate (brief-m03 §1, §4.6).
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _fleetmux_milestone import milestone  # noqa: E402
from cmux_fleet import fleet  # noqa: E402
from cmux_fleet.backends import base  # noqa: E402

_spec = importlib.util.spec_from_file_location('check_deps', ROOT / 'scripts/check_deps.py')
check_deps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_deps)


@milestone(3)
def test_linux_default_backend_is_fleetmux(monkeypatch):
    assert base.default_backend('linux') == 'fleetmux'
    monkeypatch.setattr(base, 'cmux_installed', lambda: True)  # a cmux binary never makes Linux a cmux host
    assert base.default_backend('linux') == 'fleetmux'
    assert fleet.resolve_backend(None, platform='linux')[0] == 'fleetmux'  # `start` without --backend


@milestone(3)
def test_wezterm_still_selectable_by_flag():
    assert fleet.resolve_backend(None, platform='linux')[0] == 'fleetmux'  # the default moved ...
    assert fleet.cli().parse_args(['start', '--backend', 'wezterm']).backend == 'wezterm'
    assert fleet.resolve_backend('wezterm', platform='linux')[0] == 'wezterm'  # ... the flag still wins


@milestone(3)
def test_check_deps_default_backend_is_fleetmux_on_linux(monkeypatch):
    monkeypatch.delenv(check_deps.BACKEND_ENV, raising=False)
    spec = {'programs': {'cmux': {'cmd': ['cmux', '--version']}}}
    assert check_deps.pick_backend(spec, plat='linux', probe_it=lambda cmd: '0.65.0') == 'fleetmux'
    assert check_deps.pick_backend(spec, plat='linux', probe_it=lambda cmd: None) == 'fleetmux'
    assert check_deps.pick_backend(spec, backend='wezterm', plat='linux') == 'wezterm'
