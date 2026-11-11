"""Fleet-side wiring of the fleetmux backend (fleetmux plan §3.5, design §4.5): green from phase 0 on.

The backend's behaviour is still ``NotImplementedError`` here; what these tests pin is the wiring that
*is* the interface: the name is selectable, the class is built lazily, the capability flags, ``start
--backend``, the unavailability reason reaching ``resolve_backend``, the sub-fleet refusal, the
dependency gate, and the default backend staying put until M3 flips it (plan §6, ``test-contract:``).
"""
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fleetmux import backends, fleet, subfleet  # noqa: E402
from fleetmux.backends import base  # noqa: E402
from fleetmux.backends import fleetmux as fm  # noqa: E402

_spec = importlib.util.spec_from_file_location('check_deps', ROOT / 'scripts/check_deps.py')
check_deps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_deps)

REASON = 'fleetmux backend not available: fleetmux not found on PATH (set FLEETMUX_BIN)'


def test_fleetmux_is_a_backend_name():
    assert 'fleetmux' in base.BACKENDS
    assert base.BACKENDS[:2] == ('cmux', 'wezterm')  # appended: existing names and their order unchanged
    assert base.normalize_backend(' FleetMux ') == 'fleetmux'
    with pytest.raises(base.InterfaceError) as exc:
        base.normalize_backend('tmux')
    assert 'cmux, wezterm, fleetmux' in str(exc.value)
    assert 'fleetmux' in check_deps.BACKENDS


def test_backend_class_returns_fleetmux_manager(tmp_path, monkeypatch):
    cls = backends.backend_class('fleetmux')
    assert cls is fm.FleetmuxSessionManager
    # build() / fleet.manager_for() reach this class with the config's arguments. The constructor itself
    # is backend behaviour (a phase-0 skeleton), so it is replaced by a recorder here.
    seen = []
    monkeypatch.setattr(cls, '__init__', lambda self, session_name='cmux-fleet', **kw: seen.append((session_name, kw)))
    assert isinstance(backends.build('fleetmux', 'wiring-test', state_dir=tmp_path), cls)
    assert seen[-1] == ('wiring-test', {'state_dir': tmp_path})
    cfg = {'session': 'wiring-test', 'cmux_state_dir': str(tmp_path), 'backend': 'fleetmux', 'agents_title': 'A'}
    assert isinstance(fleet.manager_for(cfg), cls)
    assert seen[-1] == ('wiring-test', {'agents_title': 'A', 'state_dir': str(tmp_path)})
    # module declarations of plan §3.5
    assert fm.PROTOCOL == 1 and fm.BIN_ENV == 'FLEETMUX_BIN' and callable(fm.availability_problem)
    assert len(fm.COMMANDS) == len(set(fm.COMMANDS)) == 22 and len(fm.ERROR_CODES) == 14  # PROTOCOL.md §4, §5 (A-R2, M2.5)
    assert fm.VERSION_RE.fullmatch('fleetmux 0.1.0 (protocol 1)')  # the one --version line (R9)
    shared = yaml.safe_load((ROOT / 'rust/protocol/keys.json').read_text())['keys']
    assert fm.KEYS == shared and fm.KEYS['Enter'] == '\r' and fm.KEYS['C-c'] == '\x03'
    # lazy-import rule (backends/__init__.py): no backend module drags in another one
    probe = ('import sys; sys.path.insert(0, %r)\n'
             'from fleetmux import backends\n'
             'backends.backend_class("cmux")\n'
             'assert "fleetmux.backends.fleetmux" not in sys.modules, "cmux imported fleetmux"\n'
             'backends.backend_class("fleetmux")\n'
             'assert "fleetmux.backends.wezterm" not in sys.modules, "fleetmux imported wezterm"\n') % str(ROOT)
    done = subprocess.run([sys.executable, '-c', probe], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr


def test_capabilities_fleetmux_browser_true_nested_true():
    caps = base.capabilities('fleetmux')
    assert (caps.browser, caps.nested_tabs) == (True, True)  # browser = "has a history pane" (design §4.4)
    cls = backends.backend_class('fleetmux')
    assert cls.supports_browser is True and cls.nested_tabs is True
    assert 'history pane' in base.__doc__  # the flag's fleetmux meaning is written down where it is defined
    wz = base.capabilities('wezterm')
    assert (wz.browser, wz.nested_tabs) == (False, False)  # the other backends unchanged


def test_start_backend_choice_accepts_fleetmux():
    assert fleet.cli().parse_args(['start', '--backend', 'fleetmux']).backend == 'fleetmux'
    assert fleet.cli().parse_args(['start', '--backend', 'wezterm']).backend == 'wezterm'
    with pytest.raises(SystemExit):
        fleet.cli().parse_args(['start', '--backend', 'zellij'])


def test_resolve_backend_reports_fleetmux_unavailability(monkeypatch):
    """An explicit ``--backend fleetmux`` is checked by the fleetmux module's own probe, and its reason
    is what `start` refuses with (before anything is provisioned)."""
    asked = []

    def problem():
        asked.append(1)
        return REASON
    monkeypatch.setattr(fm, 'availability_problem', problem)
    assert base.unavailable_reason('fleetmux') == REASON
    assert fleet.resolve_backend('fleetmux') == ('fleetmux', REASON)
    assert fleet.resolve_backend('fleetmux', platform='linux') == ('fleetmux', REASON)
    assert REASON in fleet.unavailable_text('fleetmux')
    assert fleet.backends_check('fleetmux') is False  # never raises, whatever the backend does
    assert asked
    monkeypatch.setattr(fm, 'availability_problem', lambda: None)
    assert fleet.resolve_backend('fleetmux') == ('fleetmux', None)


def test_subfleet_allowed_on_fleetmux_refused_on_wezterm():
    # M4 (design m4 §12): fleetmux hosts the whole tree on the root's server, one page per fleet.
    assert subfleet.backend_problem({'backend': 'fleetmux'}) is None
    assert subfleet.backend_problem({'backend': 'cmux'}) is None
    assert subfleet.backend_problem({}) is None  # an old config is cmux
    wz = subfleet.backend_problem({'backend': 'wezterm'})
    assert 'wezterm' in wz and 'fleetmux' not in wz
    assert 'sub-fleet' in wz and '--backend cmux' in wz  # the one refusal left keeps its reason and its way out
    # The run-time gate: a server without pages (pages_problem) refuses with its own reason.
    why = 'fleetmux server has no pages (restart it with fleetmux >= 0.2.0)'
    f = SimpleNamespace(cfg={'backend': 'fleetmux', 'task_id': None}, mgr=SimpleNamespace(pages_problem=lambda: why))
    assert subfleet.check(f, 'worker-1', 'M1', 1) == why
    named = {'name': 'ops', 'mode': 'service'}
    assert subfleet.check(f, 'worker-1', 'ops', 1, ready=False, named=named) == why


def test_deps_manifest_gates_fleetmux_on_backend():
    spec = yaml.safe_load((ROOT / 'deps.yaml').read_text())
    item = spec['programs']['fleetmux']
    assert item['cmd'] == ['fleetmux', '--version'] and item['min'] == '0.2.0' and item['when'] == 'fleetmux'
    assert fm.MIN_VERSION == check_deps.version(item['min'])  # the backend's floor is the manifest's
    assert check_deps.pick_backend(spec, 'fleetmux') == 'fleetmux'

    def probe(cmd):
        return None if cmd[0] == 'fleetmux' else (999,)  # everything installed except fleetmux
    rows = {r[0]: r for r in check_deps.check(ROOT, backend='fleetmux', probe_it=probe)}
    assert rows['fleetmux'][1] == 'missing' and rows['fleetmux'][3] is False
    assert rows['cmux'][1].startswith('skip') and rows['wezterm'][1].startswith('skip')
    for other in ('cmux', 'wezterm'):
        rows = {r[0]: r for r in check_deps.check(ROOT, backend=other, probe_it=probe)}
        assert rows['fleetmux'][1] == 'skip (backend=fleetmux)' and rows['fleetmux'][3] is True


def test_default_backend_is_fleetmux_after_m3(monkeypatch):
    """M3 flipped Linux to fleetmux via ``test-contract:`` (plan §6); a Mac with cmux keeps cmux, without it WezTerm."""
    monkeypatch.setattr(base, 'unavailable_reason', lambda name: None)  # every backend usable
    monkeypatch.setattr(base, 'cmux_installed', lambda: True)
    assert base.default_backend('linux') == 'fleetmux'
    assert base.default_backend('darwin') == 'cmux'
    monkeypatch.setattr(base, 'cmux_installed', lambda: False)
    assert base.default_backend('darwin') == 'wezterm'
    assert fleet.resolve_backend(None, platform='linux') == ('fleetmux', None)
    assert check_deps.DEFAULT_BACKEND == 'fleetmux'
    monkeypatch.delenv(check_deps.BACKEND_ENV, raising=False)
    spec = {'programs': {'cmux': {'cmd': ['cmux', '--version']}}}
    assert check_deps.pick_backend(spec, plat='linux', probe_it=lambda cmd: (1,)) == 'fleetmux'
    assert fleet.cli().parse_args(['start']).backend is None  # no flag: resolve_backend decides
