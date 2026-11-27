"""After `fleetmux upgrade --to B` everything of the tree runs B's interpreter (the switch itself is
section 60's `start --resume` code, unchanged: tests/test_resume_interpreter.py). These are the
regression tests on the upgrade side: B's interpreter starts the run, every config is switched because no daemon of
the tree is left running, and an applet that outlived its daemon is not adopted on A's interpreter.
"""
import json
import os
import signal
import subprocess
import time
from pathlib import Path

from upgrade_kit import FakeOps, Tree, make_release, py, run_upgrade
from fleetmux import applet_host, awake, fleet, release_drift, releases


def test_start_new_runs_the_new_release_with_its_interpreter_and_binaries(tmp_path, monkeypatch):
    old, new = make_release(tmp_path, 'aaaaaaa1'), make_release(tmp_path, 'bbbbbbb2')
    tree = Tree(tmp_path, old)
    ops = FakeOps(tree, new)
    monkeypatch.setattr(fleet, 'daemon_running', ops.daemon_running)
    code, lines = run_upgrade(tree, ops, new)
    assert code == 0, lines
    (argv, env, _), = [e[1:] for e in ops.events if e[0] == 'run' and 'start' in e[1]]
    assert argv[:2] == [py(new), str(new / 'fleetmux' / 'fleet.py')]
    assert argv[2:] == ['start', '--resume', str(tree.run)]
    assert env['FLEETMUX_BIN'] == str(new / 'bin' / 'fleetmux')
    assert env['PATH'].split(os.pathsep)[0] == str(new / 'bin')
    assert env['FLEETMUX_HOME'] == str(tree.home) and env['FLEETMUX_STATE'] == tree.cfg['cmux_state_dir']


def test_upgrade_switches_root_subfleets_applets_and_new_actors(tmp_path, monkeypatch):
    """With every daemon of the tree stopped, the resume switch reaches the root, every sub-fleet at any depth and the
    interpreter applets and actors are started with."""
    old, new = make_release(tmp_path, 'aaaaaaa1'), make_release(tmp_path, 'bbbbbbb2')
    tree = Tree(tmp_path, old)
    tree.child('M1', job='job-1')
    tree.child('M1.1', parent='M1')
    tree.child('N', named=True, name='N', autostart=True)
    monkeypatch.setattr(fleet, 'daemon_running', lambda run: False)  # what upgrade leaves behind
    monkeypatch.setattr(releases, 'python', lambda: py(new))
    cfg = fleet.load(tree.cfg_path)
    fleet.resume_interpreter(tree.run, cfg)
    assert cfg['python'] == py(new)
    for key in ('M1', 'M1.1', 'N'):
        assert fleet.load(tree.nodes[key] / 'config.json')['python'] == py(new), key
    assert applet_host.python(cfg) == py(new)  # actors and hook lines are built from cfg['python'] as well
    # (fleet.claude_argv, Fleet.spawn; tests/test_resume_interpreter.py covers them on the resume side)


def test_verify_fails_while_a_daemon_still_runs_the_old_interpreter(tmp_path, monkeypatch):
    old, new = make_release(tmp_path, 'aaaaaaa1'), make_release(tmp_path, 'bbbbbbb2')
    tree = Tree(tmp_path, old)
    tree.child('M1', job='job-1')
    ops = FakeOps(tree, new)
    monkeypatch.setattr(fleet, 'daemon_running', ops.daemon_running)
    def start(target):
        if target.name == 'bbbbbbb2':
            pid = ops.pid_of[str(tree.nodes['M1'])]
            ops.argv_of[pid] = [py(old)] + ops.argv_of[pid][1:]
    ops.on_start.append(start)
    code, lines = run_upgrade(tree, ops, new, extra=['--verify-timeout', '0'])
    assert code == 1
    out = json.loads(lines[-1])
    assert out['step'] == 'verify' and 'interpreter' in out['reason']
    assert out['rolled_back'] is True  # the switch went back to the release before


def _session_sleeper():
    return subprocess.Popen(['sleep', '60'], start_new_session=True, stdin=subprocess.DEVNULL)


def test_running_applet_is_not_adopted_on_the_old_interpreter(tmp_path):
    """SIGTERM ends a daemon, not its applets; the next daemon adopts a copy whose heartbeat is fresh (applets.md 2.3),
    so it would keep the old interpreter. The switch stops the copy first (release_drift.Ops.stop_applet)."""
    run = tmp_path / 'run'
    cfg = {'run': str(run), 'fleet_home': str(tmp_path / 'home')}
    spec = {'name': 'tick', 'stale_after_s': 60, 'durable_state': True}
    proc = _session_sleeper()
    try:
        (applet_host.run_dir(cfg, 'tick')).mkdir(parents=True)
        (applet_host.run_dir(cfg, 'tick') / 'pid').write_text(str(proc.pid))
        owner = applet_host.owner_path(cfg, spec)
        owner.parent.mkdir(parents=True)
        owner.write_text(json.dumps({'pid': proc.pid, 'run': str(run)}))
        now = time.time()
        h = {'hb_at': now, 'started_at': now - 5}
        assert applet_host.adoptable(cfg, spec, h, awake.from_wall(now)) is None  # the hazard: adopted as it is
        assert release_drift.Ops().applets(str(run)) == {'tick': proc.pid}
        assert release_drift.Ops().stop_applet(str(run), 'tick') == proc.pid
        proc.wait(timeout=10)
        assert applet_host.adoptable(cfg, spec, h, awake.from_wall(now)) == 'process gone'
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()


def test_upgrade_stops_every_live_applet_and_checks_the_new_ones(tmp_path, monkeypatch):
    old, new = make_release(tmp_path, 'aaaaaaa1'), make_release(tmp_path, 'bbbbbbb2')
    tree = Tree(tmp_path, old)
    ops = FakeOps(tree, new)
    ops.applet_pids = {'tick': (777, py(old)), 'relay': (778, py(old))}
    monkeypatch.setattr(fleet, 'daemon_running', ops.daemon_running)
    code, lines = run_upgrade(tree, ops, new)
    assert code == 0, lines
    assert sorted(e[1] for e in ops.events if e[0] == 'stop_applet') == ['relay', 'tick']
    kinds = [e[0] for e in ops.events]
    assert max(i for i, k in enumerate(kinds) if k == 'kill') < kinds.index('stop_applet')
