"""deps.yaml / requirements.txt checker: version parsing, pins and the failing exit."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('check_deps', ROOT / 'scripts/check_deps.py')
check_deps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_deps)


def test_version_parsing_and_minimums():
    v = check_deps.version
    assert v('codex-cli 0.162.1') == (0, 162, 1) and v('tmux 3.6a') == (3, 6) and v('nothing') is None
    assert check_deps.at_least(v('cmux 0.65.0 (108)'), '0.65.0')
    assert not check_deps.at_least(v('cmux 0.64.25 (106)'), '0.65.0')
    assert not check_deps.at_least(None, '1.0')


def test_missing_program_and_wrong_pin_fail(tmp_path):
    (tmp_path / 'deps.yaml').write_text('python: {min: "3.0"}\npython_packages: req.txt\n'
                                        'programs:\n  nope: {cmd: [definitely-not-a-program-xyz], min: "1.0"}\n')
    (tmp_path / 'req.txt').write_text('PyYAML==0.0.1\n')
    rows = {r[0]: r for r in check_deps.check(tmp_path)}
    assert rows['python'][3] and not rows['PyYAML'][3] and rows['nope'][1] == 'missing' and not rows['nope'][3]


def test_manifest_names_every_program_the_code_calls():
    names = set(__import__('yaml').safe_load((ROOT / 'deps.yaml').read_text())['programs'])
    assert {'redis-server', 'codex', 'claude', 'pi', 'cmux', 'node', 'tmux', 'git'} <= names


# ---------------------------------------------------------------- backend-gated programs (design §6)
# The fleet runs one terminal backend at a time, so `cmux` is required only for backend=cmux and
# `wezterm` (+ its mux server, design §4.4) only for backend=wezterm. Items without `when:` stay
# mandatory for every backend. With no explicit choice, the backend follows the run's own rule
# (design §5): cmux only on macOS with a usable cmux, wezterm everywhere else — never the manifest
# order, and never "cmux happens to be installed here".

def _manifest(tmp_path, monkeypatch, *, cmux, wezterm, mux=True):
    """A manifest with one gated group per backend; `cmux` / `wezterm` / `wezterm-mux-server` answer
    only when the test says the binary is there. Returns the installed-version table."""
    (tmp_path / 'deps.yaml').write_text(
        'python: {min: "3.0"}\npython_packages: req.txt\n'
        'programs:\n'
        '  git: {cmd: [git, --version], min: "2.0"}\n'
        '  cmux: {cmd: [cmux, --version], min: "0.65.0", when: cmux}\n'
        '  wezterm: {cmd: [wezterm, --version], min: "20240203", when: wezterm}\n'
        '  wezterm-mux-server: {cmd: [wezterm-mux-server, --version], min: "20240203", when: wezterm}\n')
    (tmp_path / 'req.txt').write_text('')
    installed = {'git': (2, 30), 'cmux': (0, 65, 0) if cmux else None,
                 'wezterm': (20240203,) if wezterm else None,
                 'wezterm-mux-server': (20240203,) if (wezterm and mux) else None}
    monkeypatch.setattr(check_deps, 'probe', lambda cmd: installed.get(cmd[0]))
    monkeypatch.delenv(check_deps.BACKEND_ENV, raising=False)
    return installed


def _rows(tmp_path, **kw):
    return {r[0]: r for r in check_deps.check(tmp_path, **kw)}


def test_explicit_backend_requires_only_its_own_program(tmp_path, monkeypatch):
    _manifest(tmp_path, monkeypatch, cmux=False, wezterm=True)
    rows = _rows(tmp_path, backend='wezterm', plat='darwin')
    assert rows['wezterm'][3] and rows['wezterm-mux-server'][3] and rows['git'][3]
    assert rows['cmux'][3] and rows['cmux'][1].startswith('skip')
    assert all(r[3] for r in rows.values())  # a missing cmux does not fail a wezterm run


def test_explicit_backend_fails_when_its_program_is_missing(tmp_path, monkeypatch):
    _manifest(tmp_path, monkeypatch, cmux=True, wezterm=False)
    rows = _rows(tmp_path, backend='wezterm', plat='darwin')
    assert not rows['wezterm'][3] and rows['wezterm'][1] == 'missing'
    assert rows['cmux'][3]  # the other backend's program is not asked for


def test_explicit_cmux_backend_skips_the_whole_wezterm_group(tmp_path, monkeypatch):
    _manifest(tmp_path, monkeypatch, cmux=True, wezterm=False)
    rows = _rows(tmp_path, backend='cmux', plat='linux')
    assert rows['cmux'][3]
    assert rows['wezterm'][1].startswith('skip') and rows['wezterm-mux-server'][1].startswith('skip')


def test_environment_selects_the_backend(tmp_path, monkeypatch):
    _manifest(tmp_path, monkeypatch, cmux=True, wezterm=False)
    monkeypatch.setenv(check_deps.BACKEND_ENV, 'cmux')
    rows = _rows(tmp_path, plat='linux')  # the env choice wins over the platform default
    assert rows['cmux'][3] and rows['wezterm'][1].startswith('skip')


def test_darwin_with_a_usable_cmux_defaults_to_cmux(tmp_path, monkeypatch):
    """Design §5: macOS + cmux_available() -> cmux."""
    _manifest(tmp_path, monkeypatch, cmux=True, wezterm=True)
    rows = _rows(tmp_path, plat='darwin')
    assert rows['cmux'][3] and not rows['cmux'][1].startswith('skip')
    assert rows['wezterm'][1].startswith('skip')


def test_darwin_without_cmux_defaults_to_wezterm(tmp_path, monkeypatch):
    """Design §5: cmux not usable on macOS -> wezterm (and wezterm alone must be checked)."""
    _manifest(tmp_path, monkeypatch, cmux=False, wezterm=True)
    rows = _rows(tmp_path, plat='darwin')
    assert rows['wezterm'][3] and not rows['wezterm'][1].startswith('skip')
    assert rows['cmux'][1].startswith('skip')


def test_linux_defaults_to_fleetmux_even_when_cmux_is_installed(tmp_path, monkeypatch):
    """A cmux binary on a Linux box does not make Linux a cmux host (design §5 / §8); M3: fleetmux."""
    _manifest(tmp_path, monkeypatch, cmux=True, wezterm=True)
    with (tmp_path / 'deps.yaml').open('a') as f:
        f.write('  fleetmux: {cmd: [fleetmux, --version], min: "0.1.0", when: fleetmux}\n')
    assert check_deps.pick_backend(__import__('yaml').safe_load(
        (tmp_path / 'deps.yaml').read_text()), plat='linux') == 'fleetmux'
    rows = _rows(tmp_path, plat='linux')
    assert not rows['fleetmux'][1].startswith('skip')
    assert rows['wezterm'][1].startswith('skip') and rows['cmux'][1].startswith('skip')


def test_default_does_not_follow_manifest_order(tmp_path, monkeypatch):
    """wezterm's lines come first, cmux's last: the platform rule still picks cmux on macOS."""
    _manifest(tmp_path, monkeypatch, cmux=True, wezterm=True)
    assert check_deps.pick_backend(__import__('yaml').safe_load(
        (tmp_path / 'deps.yaml').read_text()), plat='darwin') == 'cmux'


def test_neither_backend_installed_fails_and_names_the_default_one(tmp_path, monkeypatch):
    """Nothing installed: the Linux default (fleetmux since M3) is what gets checked, and it fails visibly."""
    _manifest(tmp_path, monkeypatch, cmux=False, wezterm=False)
    with (tmp_path / 'deps.yaml').open('a') as f:
        f.write('  fleetmux: {cmd: [fleetmux, --version], min: "0.1.0", when: fleetmux}\n')
    rows = _rows(tmp_path, plat='linux')
    assert not rows['fleetmux'][3] and rows['fleetmux'][1] == 'missing'
    assert rows['cmux'][1].startswith('skip') and rows['wezterm'][1].startswith('skip')
    assert not all(r[3] for r in rows.values())


def test_missing_wezterm_mux_server_is_reported_clearly(tmp_path, monkeypatch):
    """Design §4.4: wezterm alone is not enough; the mux server is named with how to fix it."""
    _manifest(tmp_path, monkeypatch, cmux=False, wezterm=True, mux=False)
    rows = _rows(tmp_path, backend='wezterm', plat='linux')  # M3: Linux defaults to fleetmux, so pick wezterm
    assert rows['wezterm'][3]
    assert not rows['wezterm-mux-server'][3]
    assert rows['wezterm-mux-server'][1].startswith('missing')
    assert 'private mux server' in rows['wezterm-mux-server'][1]
    assert 'install wezterm' in rows['wezterm-mux-server'][1]


def test_unknown_backend_is_refused(tmp_path, monkeypatch, capsys):
    _manifest(tmp_path, monkeypatch, cmux=True, wezterm=True)
    real = check_deps.check
    monkeypatch.setattr(check_deps, 'check', lambda backend=None: real(tmp_path, backend, plat='linux'))
    assert check_deps.main(['--backend', 'zellij']) == 2
    assert 'unknown backend' in capsys.readouterr().err


def test_bad_usage_is_refused(capsys):
    assert check_deps.main(['--nonsense']) == 2
    assert 'usage:' in capsys.readouterr().err


def test_main_takes_backend_and_exit_code(tmp_path, monkeypatch, capsys):
    _manifest(tmp_path, monkeypatch, cmux=True, wezterm=False)
    real = check_deps.check
    monkeypatch.setattr(check_deps, 'check', lambda backend=None: real(tmp_path, backend, plat='linux'))
    assert check_deps.main(['--backend', 'cmux']) == 0
    assert check_deps.main(['--backend', 'wezterm']) == 1
    assert 'FAIL  wezterm' in capsys.readouterr().out


def test_the_shipped_manifest_gates_both_terminal_backends():
    """The real deps.yaml: both backends (and the mux server) are gated; everything else is not."""
    import yaml
    programs = yaml.safe_load((ROOT / 'deps.yaml').read_text())['programs']
    assert programs['cmux']['when'] == 'cmux'
    assert programs['wezterm']['when'] == 'wezterm'
    assert programs['wezterm-mux-server']['when'] == 'wezterm'
    assert programs['wezterm']['min'] == '20240203'
    assert programs['wezterm-mux-server']['min'] == '20240203'
    for name in ('redis-server', 'codex', 'claude', 'pi', 'opencode', 'node', 'tmux', 'git'):
        assert not programs[name].get('when'), name  # not backend specific: still mandatory


# ---------------------------------------------------------------- fleetmux version floor (brief-m03 §3.5, B19)
import os  # noqa: E402

from _fleetmux_milestone import milestone  # noqa: E402


@milestone(2)
def test_fleetmux_version_floor_is_enforced(tmp_path, monkeypatch, capsys):
    """A real `fleetmux --version` below deps.yaml's floor (through `probe`, not a stub) fails a
    backend=fleetmux check, and the row / the FAIL line name the version found and the one needed."""
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    fake = bin_dir / 'fleetmux'
    fake.write_text('#!/bin/sh\necho "fleetmux 0.0.9 (protocol 1)"\n')
    fake.chmod(0o755)
    monkeypatch.setenv('PATH', str(bin_dir) + os.pathsep + os.environ['PATH'])
    monkeypatch.delenv(check_deps.BACKEND_ENV, raising=False)
    (tmp_path / 'deps.yaml').write_text(
        'python: {min: "3.0"}\npython_packages: req.txt\n'
        'programs:\n'
        '  fleetmux: {cmd: [fleetmux, --version], min: "0.1.0", when: fleetmux}\n')
    (tmp_path / 'req.txt').write_text('')
    rows = _rows(tmp_path, backend='fleetmux', plat='linux')
    assert rows['fleetmux'][3] is False
    assert '0.0.9' in rows['fleetmux'][1] and '0.1.0' in rows['fleetmux'][2]
    real = check_deps.check
    monkeypatch.setattr(check_deps, 'check', lambda backend=None: real(tmp_path, backend, plat='linux'))
    assert check_deps.main(['--backend', 'fleetmux']) == 1
    out = capsys.readouterr().out
    assert 'FAIL  fleetmux 0.0.9' in out and '0.1.0' in out
    fake.write_text('#!/bin/sh\necho "fleetmux 0.1.0 (protocol 1)"\n')  # at the floor: passes
    assert check_deps.main(['--backend', 'fleetmux']) == 0


# ---------------------------------------------------------------- fleetmux-web (brief-m25 §3.6 W4, design §3.5.9)

@milestone(3)
def test_fleetmux_web_only_warns_without_display(tmp_path, monkeypatch):
    """``fleetmux-web`` (``desktop: true``) is needed only in a desktop session: missing on a Linux box without
    ``DISPLAY`` / ``WAYLAND_DISPLAY`` it is a passing row that says so; with a display it fails like any program."""
    (tmp_path / 'deps.yaml').write_text(
        'python: {min: "3.0"}\npython_packages: req.txt\n'
        'programs:\n'
        '  fleetmux: {cmd: [fleetmux, --version], min: "0.1.0", when: fleetmux}\n'
        '  fleetmux-web: {cmd: [fleetmux-web, --version], min: "0.1.0", when: fleetmux, desktop: true}\n')
    (tmp_path / 'req.txt').write_text('')
    monkeypatch.setattr(check_deps, 'probe', lambda cmd: {'fleetmux': (0, 1, 0)}.get(cmd[0]))
    monkeypatch.delenv(check_deps.BACKEND_ENV, raising=False)
    monkeypatch.delenv('DISPLAY', raising=False)
    monkeypatch.delenv('WAYLAND_DISPLAY', raising=False)
    rows = _rows(tmp_path, backend='fleetmux', plat='linux')
    assert rows['fleetmux'][3] is True
    assert rows['fleetmux-web'][3] is True, rows['fleetmux-web']  # warned, not failed
    assert 'desktop' in rows['fleetmux-web'][1], rows['fleetmux-web']
    monkeypatch.setenv('DISPLAY', ':0')
    rows = _rows(tmp_path, backend='fleetmux', plat='linux')
    assert rows['fleetmux-web'][3] is False and rows['fleetmux-web'][1].startswith('missing')
    monkeypatch.delenv('DISPLAY')
    rows = _rows(tmp_path, backend='wezterm', plat='linux')  # other backends skip it like fleetmux itself
    assert rows['fleetmux-web'][1].startswith('skip')


@milestone(3)
def test_shipped_fleetmux_web_matches_fleetmux_and_is_desktop_only(tmp_path, monkeypatch):
    """The real deps.yaml: fleetmux-web ships with fleetmux (same floor), only for backend=fleetmux, desktop only;
    on macOS (always a desktop session) a missing fleetmux-web fails even without DISPLAY."""
    import yaml
    programs = yaml.safe_load((ROOT / 'deps.yaml').read_text())['programs']
    web = programs['fleetmux-web']
    assert web['cmd'] == ['fleetmux-web', '--version'] and web['min'] == programs['fleetmux']['min']
    assert web['when'] == 'fleetmux' and web['desktop'] is True
    assert not programs['fleetmux'].get('desktop')
    (tmp_path / 'deps.yaml').write_text(
        'python: {min: "3.0"}\npython_packages: req.txt\n'
        'programs:\n'
        '  fleetmux-web: {cmd: [fleetmux-web, --version], min: "0.1.0", when: fleetmux, desktop: true}\n')
    (tmp_path / 'req.txt').write_text('')
    monkeypatch.setattr(check_deps, 'probe', lambda cmd: None)
    monkeypatch.delenv('DISPLAY', raising=False)
    monkeypatch.delenv('WAYLAND_DISPLAY', raising=False)
    assert _rows(tmp_path, backend='fleetmux', plat='darwin')['fleetmux-web'][3] is False
    monkeypatch.setenv('WAYLAND_DISPLAY', 'wayland-0')
    assert _rows(tmp_path, backend='fleetmux', plat='linux')['fleetmux-web'][3] is False
