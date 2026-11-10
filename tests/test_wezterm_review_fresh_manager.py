"""Review 2026-10-28: every fleet process builds its own manager (fleet.manager_for), so a *fresh*
WezTermSessionManager on the same state dir must see / start / stop the run's one private server."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_wezterm_backend import _fake_server, fake_cli, mgr, panes  # noqa: E402,F401
from cmux_fleet.backends import wezterm as wz  # noqa: E402


def test_fresh_manager_reuses_the_running_server(mgr, fake_cli, monkeypatch):
    """P0-01: the daemon's fresh manager must not start a second server on the run's socket."""
    started = []

    def counting(self):
        started.append(self)
        return _fake_server(fake_cli, self)

    monkeypatch.setattr(wz.WezTermSessionManager, '_spawn_mux_server', counting)
    daemon_mgr = wz.WezTermSessionManager('wz-test', state_dir=fake_cli['root'])
    assert daemon_mgr.get_or_create_session() is True
    assert started == [], 'a second private mux server was started over the running one'


def test_fresh_manager_spawn_window_starts_the_server(fake_cli, monkeypatch):
    """P0-02: `start` calls spawn_window('control') on a fresh manager (cmux contract: first spawn creates)."""
    monkeypatch.setattr(wz.WezTermSessionManager, '_spawn_mux_server', lambda self: _fake_server(fake_cli, self))
    start_mgr = wz.WezTermSessionManager('wz-test', state_dir=fake_cli['root'])
    res = start_mgr.spawn_window('control', 'sleep 60', metadata={'type': 'daemon'}, start_directory='/tmp')
    assert res.success, res.error


def test_fresh_manager_close_all_really_kills_the_panes(mgr, fake_cli):
    """P1-03: the detached lifecycle closer / subfleet.collect close a run through a fresh manager."""
    sup = mgr.spawn_window('supervisor', 'sleep 60', metadata={'type': 'supervisor'}, start_directory='/tmp')
    assert sup.success
    closer = wz.WezTermSessionManager('wz-test', state_dir=fake_cli['root'])
    assert closer.close_all() is True
    assert sup.pane_id not in [str(p) for p in panes(fake_cli)], 'close_all reported success but the pane still runs'


def test_fresh_manager_stops_only_a_server_whose_command_line_is_this_runs(fake_cli):
    """P1-03 identity check: the closer claims the pid in mux.pid only when that process runs with this
    run's own ``--config-file``; a pid whose command line does not carry it is never signalled."""
    import subprocess
    import time
    closer = wz.WezTermSessionManager('wz-test', state_dir=fake_cli['root'])
    closer.prepare_runtime()
    sleeper = [sys.executable, '-c', 'import time; time.sleep(60)']
    ours = subprocess.Popen(sleeper + ['--config-file', closer.mux_config])
    other = subprocess.Popen(sleeper + ['--config-file', closer.mux_config + '.other'])
    try:
        time.sleep(0.3)
        Path(closer.mux_pid_file).write_text(str(other.pid))
        assert closer.end_mux_server() is True
        assert other.poll() is None, 'a process that is not this run\'s server was signalled'
        Path(closer.mux_pid_file).write_text(str(ours.pid))
        assert closer.end_mux_server() is True
        assert ours.wait(timeout=10) is not None, 'this run\'s server was not stopped'
    finally:
        for proc in (ours, other):
            proc.kill()
            proc.wait()
