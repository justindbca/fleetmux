"""`start`'s automatic view (docs/design.md section 33.6) does not read fleet.yaml#bare.host_terminal_env: in a host
terminal a `start` on a desktop still opens the window in the background and returns, exactly as before.

Harness: `auto_world` of tests/test_fleetmux_start_attach.py (mock start, Popen and execvp recorded).
"""
from pathlib import Path

from fleetmux import fleet  # noqa: E402
from test_fleetmux_start_attach import auto_world, run_start  # noqa: E402


def test_auto_view_unchanged_in_a_host_terminal(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('HC_TERMINAL_ID', 't1')
    args, ran, runtime, spawned, execs, popens = auto_world(tmp_path, monkeypatch)
    assert fleet.window_here(fleet.os.environ.get('FLEETMUX_BIN')), 'the desktop rule ignores the host list'
    assert run_start(args + ['--backend', 'fleetmux']) in (None, 0)
    assert execs == [], 'start does not attach in a host terminal: %r' % (execs,)
    assert len(popens) == 1, popens
    argv, kw = popens[0]
    assert Path(argv[0]).name == 'fleetmux' and argv[1:] == ['gui', '--runtime', str(runtime)]
    assert 'Opened a window' in capsys.readouterr().out


def test_auto_view_over_ssh_in_a_host_terminal_still_attaches(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('HC_TERMINAL_ID', 't1')
    args, ran, runtime, spawned, execs, popens = auto_world(tmp_path, monkeypatch, ssh=True)
    assert run_start(args + ['--backend', 'fleetmux']) in (None, 0)
    assert popens == [] and len(execs) == 1 and execs[0][1][1:] == ['attach', '--runtime', str(runtime)], execs
