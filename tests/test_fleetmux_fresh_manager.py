"""Fresh-manager takeover (fleetmux design §5, the WezTerm review's P0-01 / P0-02 / P1-03 for fleetmux):
every fleet process builds its own manager (fleet.manager_for), so a *fresh* FleetmuxSessionManager on
the same ``(state_dir, session)`` must find, start and stop the run's one server by its path alone.

Runs against the fake and the real server through test_fleetmux_backend's ``impl`` fixture; red until M0.
"""
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_fleetmux_backend import (SESSION, _read_json, alive, ctl, impl, new_mgr, pane,  # noqa: E402,F401
                                   panes, ps_args, runtime_of, server_up, spawn, wait_for)
from fleetmux.backends import fleetmux as fm  # noqa: E402


def _servers_of(rt: Path) -> list:
    """Pids whose command line is a fleetmux server for this runtime (the fake runs as python)."""
    done = subprocess.run(['ps', '-ww', '-A', '-o', 'pid=,args='], capture_output=True, text=True)
    found = []
    for line in done.stdout.splitlines():
        pid, _, args = line.strip().partition(' ')
        if ' server ' in ' ' + args + ' ' and '--runtime ' + str(rt) in args and 'ps -ww' not in args:
            found.append(int(pid))
    return found


def test_fresh_manager_reuses_the_running_server(impl):
    """P0-01: the daemon's fresh manager must not start a second server on the run's socket."""
    mgr = new_mgr(impl)
    assert mgr.get_or_create_session() is True
    first = ctl(impl, 'hello')
    p = spawn(mgr, 'worker-1')
    daemon_mgr = fm.FleetmuxSessionManager(SESSION, state_dir=impl.state)
    assert daemon_mgr.get_or_create_session() is True
    again = ctl(impl, 'hello')
    assert (again['pid'], again['epoch']) == (first['pid'], first['epoch']), 'a second server took the socket'
    assert _servers_of(runtime_of(impl)) == [first['pid']]
    assert daemon_mgr.session_exists() is True and daemon_mgr.owns(p) is True
    assert p in [row['pane'] for row in panes(impl)]  # the daemon's view did not lose the running pane


def test_fresh_manager_spawn_window_starts_the_server(impl):
    """P0-02: `start` calls spawn_window('control') on a fresh manager (cmux contract: first spawn creates)."""
    assert not server_up(impl)
    start_mgr = fm.FleetmuxSessionManager(SESSION, state_dir=impl.state)
    res = start_mgr.spawn_window('control', 'sleep 600', metadata={'type': 'daemon'}, start_directory='/tmp')
    assert res.success, res.error
    assert server_up(impl)
    row = pane(impl, res.pane_id)
    assert row is not None and row['group'] == 'lower' and alive(row['pid'])


def test_fresh_manager_close_all_really_kills_the_panes(impl):
    """P1-03: the detached lifecycle closer / subfleet.collect close a run through a fresh manager."""
    mgr = new_mgr(impl)
    sup = spawn(mgr, 'supervisor-1', type_='supervisor')
    w = spawn(mgr, 'worker-1')
    pids = [pane(impl, x)['pid'] for x in (sup, w)]
    srv = _read_json(runtime_of(impl) / 'server.json')['pid']
    closer = fm.FleetmuxSessionManager(SESSION, state_dir=impl.state)
    assert closer.close_all() is True
    assert wait_for(lambda: not any(alive(pid) for pid in pids)), 'close_all reported success but a pane still runs'
    assert wait_for(lambda: not alive(srv)) and not server_up(impl)
    assert closer.session_exists() is False and mgr.session_exists() is False


def test_fresh_manager_stops_only_a_server_whose_cmdline_has_this_runtime(impl):
    """P1-03 identity check: with the socket not answering, a fresh closer signals the pid in server.json
    only when that process runs with ``--runtime <this runtime>``; any other pid is never signalled."""
    mgr = new_mgr(impl)
    assert mgr.get_or_create_session() is True
    rt = runtime_of(impl)
    recorded = _read_json(rt / 'server.json')
    srv = recorded['pid']
    assert str(rt) in ps_args(srv)
    other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)', '--runtime', str(rt) + '-x'])
    try:
        time.sleep(0.3)
        (rt / 'ctl.sock').rename(rt / 'ctl.sock.hidden')
        (rt / 'server.json').write_text(__import__('json').dumps(dict(recorded, pid=other.pid)))
        closer = fm.FleetmuxSessionManager(SESSION, state_dir=impl.state)
        assert closer.end_server() is False, 'an unverified pid must be reported, not claimed stopped'
        assert other.poll() is None, 'a process that is not this run\'s server was signalled'
        (rt / 'server.json').write_text(__import__('json').dumps(recorded))
        closer = fm.FleetmuxSessionManager(SESSION, state_dir=impl.state)
        assert closer.end_server() is True
        assert wait_for(lambda: not alive(srv)), 'this run\'s server was not stopped'
    finally:
        other.kill()
        other.wait()
