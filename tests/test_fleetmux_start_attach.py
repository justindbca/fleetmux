"""M0 py-start-attach (brief-m03 §3.5, B10): `start --backend fleetmux` tells the operator how to look at the
fleet -- one line ``fleetmux attach --runtime <runtime dir>`` -- and ``start --attach`` execs exactly that once
start has finished. ``--attach`` is fleetmux-only: any other backend is refused (exit 2) before provisioning.

``start`` runs through test_pi's ``mock_start`` (fake Redis / CLIs, nothing provisioned for real); the
fleetmux manager is a stand-in with the backend's declared ``runtime_root`` (plan §3.5), so these tests depend
on fleet.py's start path only (B10), not on the manager's bodies (B9).
"""
import os
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cmux_fleet import fleet  # noqa: E402
from cmux_fleet.backends import fleetmux as fm  # noqa: E402


def fleetmux_start(tmp_path, monkeypatch):
    """mock_start's argv + a fleetmux run: the stand-in manager, a ``fleetmux`` binary on PATH and in
    $CMUX_FLEET_FLEETMUX, and a recorder in place of ``os.execvp`` (-> list of (file, argv))."""
    from test_pi import mock_start
    args, ran = mock_start(tmp_path, monkeypatch)
    state = tmp_path / 'state'
    runtime = state / 'fleetmux' / 'cmux-fleet'
    runtime.mkdir(parents=True)
    spawned = []
    mgr = SimpleNamespace(meta_path=state / 'cmux-fleet.fleetmux.json', runtime_root=runtime,
                          session_exists=lambda: False, claim_run=lambda run: None,
                          spawn_window=lambda *a, **kw: spawned.append(a) or SimpleNamespace(success=True, error=None))
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    binary = bindir / 'fleetmux'
    binary.write_text('#!/bin/sh\nexit 0\n')
    binary.chmod(0o755)
    monkeypatch.setenv(fm.BIN_ENV, str(binary))
    monkeypatch.setenv('PATH', str(bindir) + os.pathsep + os.environ.get('PATH', ''))
    monkeypatch.setattr(fleet, 'resolve_backend', lambda requested=None, platform=None: (requested or 'fleetmux', None))
    monkeypatch.setattr(fleet, 'backends_check', lambda name: True)
    monkeypatch.setattr(fleet, 'manager_for', lambda cfg: mgr)
    execs = []

    def execvp(file, argv):
        execs.append((file, list(argv), (tmp_path / 'latest.json').exists()))
    monkeypatch.setattr(os, 'execvp', execvp)
    return args, ran, runtime, spawned, execs


def attach_lines(text, runtime):
    want = 'fleetmux attach --runtime '
    return [line for line in text.splitlines()
            if want in line and line.rstrip().endswith((want + str(runtime), want + shlex.quote(str(runtime))))]


def test_start_prints_attach_command_line(tmp_path, monkeypatch, capsys):
    args, ran, runtime, spawned, execs = fleetmux_start(tmp_path, monkeypatch)
    fleet.main(args + ['--backend', 'fleetmux'])
    out = capsys.readouterr().out
    assert spawned and spawned[0][0] == 'daemon'  # start really ran to the end
    assert len(attach_lines(out, runtime)) == 1, out
    assert execs == []  # without --attach start only says how


def test_start_attach_flag_execs_fleetmux_attach(tmp_path, monkeypatch, capsys):
    args, ran, runtime, spawned, execs = fleetmux_start(tmp_path, monkeypatch)
    fleet.main(args + ['--backend', 'fleetmux', '--attach'])
    assert len(execs) == 1, execs
    file, argv, finished = execs[0]
    assert Path(file).name == 'fleetmux' and Path(argv[0]).name == 'fleetmux'
    assert argv[1:] == ['attach', '--runtime', str(runtime)]
    assert finished and spawned and spawned[0][0] == 'daemon'  # exec only after start has done its work
    assert len(attach_lines(capsys.readouterr().out, runtime)) == 1  # the line is printed either way


def test_start_attach_refused_on_other_backends(tmp_path, monkeypatch, capsys):
    args, ran, runtime, spawned, execs = fleetmux_start(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as exc:
        fleet.main(args + ['--backend', 'cmux', '--attach'])
    err = capsys.readouterr().err
    assert exc.value.code == 2
    assert '--attach' in err and 'fleetmux' in err and 'unrecognized arguments' not in err, err
    assert execs == [] and spawned == [] and not any('--unixsocket' in argv for argv in ran)  # nothing provisioned


# ---------------------------------------------------------------- M2.6 `fleetmux gui` (brief-m26 §3.5 W4, design §8 item 14)
# Red in W0, green in W4. A desktop = macOS, or Linux with DISPLAY / WAYLAND_DISPLAY; the window half
# (`fleetmux-web`) is found the way `fleetmux` finds it (next to the binary / PATH / $CMUX_FLEET_FLEETMUX_WEB).

from _fleetmux_milestone import milestone  # noqa: E402


def as_host(monkeypatch, plat, display=None, wayland=None):
    """Pretend to be ``plat`` ('darwin' / 'linux') with these display variables (None = unset)."""
    monkeypatch.setattr(sys, 'platform', plat)
    for var, value in (('DISPLAY', display), ('WAYLAND_DISPLAY', wayland)):
        if value is None:
            monkeypatch.delenv(var, raising=False)
        else:
            monkeypatch.setenv(var, value)


def with_web(tmp_path):
    """A ``fleetmux-web`` next to the stand-in ``fleetmux`` (same bin dir, also on PATH)."""
    web = tmp_path / 'bin' / 'fleetmux-web'
    web.write_text('#!/bin/sh\nexit 0\n')
    web.chmod(0o755)
    return web


def gui_lines(text, runtime):
    want = 'fleetmux gui --runtime '
    return [line for line in text.splitlines()
            if want in line and line.rstrip().endswith((want + str(runtime), want + shlex.quote(str(runtime))))]


def run_start(argv):
    """fleet.main(argv) -> its SystemExit code (None when it returned)."""
    try:
        fleet.main(argv)
    except SystemExit as exc:
        return exc.code
    return None


@milestone(3)
def test_start_prints_gui_line_first_on_desktop_only(tmp_path, monkeypatch, capsys):
    args, ran, runtime, spawned, execs = fleetmux_start(tmp_path, monkeypatch)
    with_web(tmp_path)
    for plat, display, wayland in (('linux', ':0', None), ('linux', None, 'wayland-0'), ('darwin', None, None)):
        as_host(monkeypatch, plat, display, wayland)
        assert run_start(args + ['--backend', 'fleetmux']) in (None, 0)
        out = capsys.readouterr().out
        lines = out.splitlines()
        gui, attach = gui_lines(out, runtime), attach_lines(out, runtime)
        assert len(gui) == 1 and len(attach) == 1, (plat, display, wayland, out)
        assert gui[0].startswith('Watch it in a window: '), gui
        assert attach[0].startswith('Watch it with: '), attach  # the attach line is unchanged
        assert lines.index(gui[0]) < lines.index(attach[0]), out  # gui first
    # No desktop: only the attach line.
    as_host(monkeypatch, 'linux')
    assert run_start(args + ['--backend', 'fleetmux']) in (None, 0)
    out = capsys.readouterr().out
    assert gui_lines(out, runtime) == [] and len(attach_lines(out, runtime)) == 1, out
    assert execs == []


@milestone(3)
def test_start_gui_execs_fleetmux_gui_and_refuses_headless(tmp_path, monkeypatch, capsys):
    args, ran, runtime, spawned, execs = fleetmux_start(tmp_path, monkeypatch)
    with_web(tmp_path)
    as_host(monkeypatch, 'linux', ':0')
    assert run_start(args + ['--backend', 'fleetmux', '--gui']) in (None, 0), capsys.readouterr().err
    assert len(execs) == 1, execs
    file, argv, finished = execs[0]
    assert Path(file).name == 'fleetmux' and Path(argv[0]).name == 'fleetmux'
    assert argv[1:] == ['gui', '--runtime', str(runtime)]
    assert finished and spawned and spawned[0][0] == 'daemon'  # exec only after start has done its work
    capsys.readouterr()
    # Headless: refused, pointing at --attach; nothing exec'd.
    del execs[:]
    as_host(monkeypatch, 'linux')
    code = run_start(args + ['--backend', 'fleetmux', '--gui'])
    err = capsys.readouterr().err
    assert code not in (None, 0), err
    assert '--attach' in err and 'unrecognized arguments' not in err, err
    assert execs == []
    # --attach and --gui together: a usage error.
    as_host(monkeypatch, 'linux', ':0')
    code = run_start(args + ['--backend', 'fleetmux', '--attach', '--gui'])
    err = capsys.readouterr().err
    assert code == 2, err
    assert '--gui' in err and 'unrecognized arguments' not in err, err
    assert execs == []


@milestone(3)
def test_start_gui_without_fleetmux_web_still_execs_fleetmux_gui(tmp_path, monkeypatch, capsys):
    """A desktop but no ``fleetmux-web``: no window line, yet ``--gui`` still execs ``fleetmux gui`` -- a missing
    window half is that command's own exit 4 (PROTOCOL.md §9.5), not a start refusal."""
    args, ran, runtime, spawned, execs = fleetmux_start(tmp_path, monkeypatch)
    monkeypatch.delenv('CMUX_FLEET_FLEETMUX_WEB', raising=False)
    monkeypatch.setenv('PATH', str(tmp_path / 'bin') + os.pathsep + os.defpath)  # no fleetmux-web anywhere
    as_host(monkeypatch, 'linux', ':0')
    assert run_start(args + ['--backend', 'fleetmux', '--gui']) in (None, 0), capsys.readouterr().err
    out = capsys.readouterr().out
    assert gui_lines(out, runtime) == [] and len(attach_lines(out, runtime)) == 1, out
    assert len(execs) == 1, execs
    file, argv, finished = execs[0]
    assert Path(argv[0]).name == 'fleetmux' and argv[1:] == ['gui', '--runtime', str(runtime)]
    assert finished and spawned and spawned[0][0] == 'daemon'
