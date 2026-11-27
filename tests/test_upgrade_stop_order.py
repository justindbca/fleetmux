"""The release switch stops the root first, then each level of sub-fleets (fleetmux/release_drift.py; `fleetmux upgrade`
is its manual entry).

Why the order matters: a parent daemon that is still alive collects a child whose daemon exited without result.json
as `cancelled` (subfleet.tick) and drops its job row. The first two tests run the same upgrade steps against an
in-process service root with a running job (private Redis, fake cmux): children first loses the job, root first keeps
it and the next release's resume hands it on. The rest pin the step order, the level order on a synthetic tree, and
the self-detach that keeps the command alive while it takes the fleet down.
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_escalate import ok_check  # noqa: F401  (fixture)
from test_job_queue_restart import continue_root, jobs, all_ready
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_service_escalate import result
from test_service_job import start_job
from test_service_split import split_tree  # noqa: F401
from test_subfleet import step
from upgrade_kit import FakeOps, Tree, kills, make_release, run_upgrade
from fleetmux import fleet, release_drift, subfleet, upgrade

pytestmark = pytest.mark.usefixtures('job_chains_as_before')

REPO = Path(__file__).resolve().parents[1]


class TreeOps(release_drift.Ops):
    """The process layer over the in-process tree of ``split_tree``: a fleet is alive while its daemon "runs"; every
    signal ends that fleet and lets each fleet still alive run its daemon tick (what a live parent does meanwhile).
    Redis is the tree's own private Redis, read through the real ``release_drift.Ops``."""

    def __init__(self, t, monkeypatch):
        self.t_tree, self.by_run = t, {str(x.run_dir): x for x in t.fleets}
        # a job sub-fleet's daemon is not in-process here (launch is stubbed): its lock is the oracle's word
        self.alive_runs = set(self.by_run) | {str(d) for d in subfleet.children_dirs(t.f.run_dir)}
        self.pid_of = {run: 2000 + i for i, run in enumerate(sorted(self.alive_runs))}
        self.events, self.t = [], 0.0
        monkeypatch.setattr(fleet, 'daemon_running', lambda run: str(Path(run)) in self.alive_runs)
        monkeypatch.setattr(subfleet, 'LAUNCH_GRACE', 0)

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s

    def ps(self):
        return [(p, '%s %s daemon %s/config.json' % (sys.executable, REPO / 'fleetmux' / 'fleet.py', run))
                for run, p in sorted(self.pid_of.items()) if run in self.alive_runs]

    def daemon_running(self, run):
        return str(Path(run)) in self.alive_runs

    def kill(self, pid, sig):
        run = next(r for r, p in self.pid_of.items() if p == pid)
        self.events.append(('kill', run, sig))
        self.alive_runs.discard(run)
        x = self.by_run.get(run)
        if x:
            x.v2.close()
            x.stopped = True
        for other, f in self.by_run.items():  # live daemons keep ticking while this one is gone
            if other in self.alive_runs:
                for _ in range(2):
                    f.tick()


def stop_tree(t, monkeypatch, tmp_path, children_first=False):
    """The switch's snapshot, its stop of the daemons (release_drift.stop_daemons) and its job files, on the tree."""
    jid, key = start_job(t)
    cfg = fleet.load(t.f.path)
    plan = {'run': str(t.f.run_dir), 'fleet_home': str(tmp_path / 'home'), 'stop_wait_s': 30}
    ops = TreeOps(t, monkeypatch)
    if children_first:  # the order of the first restart script (the incident), injected
        monkeypatch.setattr(release_drift, 'STOP_ORDER', 'children_first')
    jobs = release_drift._jobs(ops.redis_get(cfg['redis_socket'], 'fx:jobs'))
    release_drift.stop_daemons(plan, ops)
    release_drift.write_jobs(plan, jobs)
    return jid, key, ops


def test_child_first_while_root_lives_loses_the_running_job(split_tree, monkeypatch, tmp_path):
    t = split_tree
    jid, key, ops = stop_tree(t, monkeypatch, tmp_path, children_first=True)
    assert [k[0] for k in kills(ops)][0] == key  # the child went first
    gone = jid not in jobs(t.f) or (result(t.f, jid) or {}).get('status') in ('failed', 'cancelled')
    assert gone, (jobs(t.f), result(t.f, jid))


def test_upgrade_stops_root_first_and_keeps_every_job(split_tree, monkeypatch, tmp_path):
    t = split_tree
    jid, key, ops = stop_tree(t, monkeypatch, tmp_path)
    assert [k[0] for k in kills(ops)] == [t.f.run_dir.name, key]
    assert all(sig == signal.SIGTERM for _, sig in kills(ops))
    assert jid in jobs(t.f) and result(t.f, jid) is None
    mirror = json.loads((t.f.run_dir / 'jobs.json').read_text())
    assert mirror[jid]['state'] == 'running' and mirror == jobs(t.f)
    # the next release continues the run the way a reboot leaves it: the job is handed on, never failed
    subfleet.shutdown_redis(fleet.load(t.f.run_dir / 'sub' / key / 'config.json'))
    ops.alive_runs.add(str(t.f.run_dir))
    f = continue_root(t, kill=True, dead_sessions=True)
    slot = jobs(f)[jid]['slot']
    if f.agent(slot).get('state') == 'delegated':
        f.v2.tab_missing(slot, f.agent(slot))
    all_ready(t)
    for _ in range(4):
        f.tick()
        step(t)
    assert result(f, jid) is None
    assert jobs(f)[jid]['state'] in ('starting', 'running')


def test_steps_order_is_fixed():
    assert upgrade.STEPS == ('preflight', 'backup', 'stop_root', 'stop_children', 'stop_applets', 'restore_files',
                             'redis_down', 'terminal_down', 'start_new', 'verify')
    assert release_drift.STEPS == upgrade.STEPS  # the CLI names the switch's own steps
    assert release_drift.STOP_ORDER == 'root_first'


def test_each_level_is_signalled_only_after_its_parents_are_down(tmp_path, monkeypatch):
    old, new = make_release(tmp_path, 'aaaaaaa1'), make_release(tmp_path, 'bbbbbbb2')
    tree = Tree(tmp_path, old)
    tree.child('M1', job='job-1')
    tree.child('M1.1', parent='M1')
    tree.child('N', named=True, name='N', autostart=True)
    tree.child('M2', job='job-2')
    tree.child('M2.1', parent='M2')
    tree.child('M2.1.1', parent='M2.1')
    ops = FakeOps(tree, new)
    monkeypatch.setattr(fleet, 'daemon_running', ops.daemon_running)
    seen = []

    def kill(pid, sig):
        run = next((r for r, p in ops.pid_of.items() if p == pid and r in ops.alive_runs), None)
        if run:
            parents = [str(p) for p in Path(run).parents if (p / 'config.json').exists() and str(p) != run]
            assert not any(ops.daemon_running(p) for p in parents), (run, 'a parent still runs')
            seen.append(Path(run).name)
        FakeOps.kill(ops, pid, sig)
    ops.kill = kill
    code, lines = run_upgrade(tree, ops, new)
    assert code == 0, lines
    depth = {'M1': 1, 'N': 1, 'M2': 1, 'M1.1': 2, 'M2.1': 2, 'M2.1.1': 3}
    assert seen[0] == tree.run.name
    assert [depth[n] for n in seen[1:]] == sorted(depth[n] for n in seen[1:])
    names = [e[0] for e in ops.events if e[0] in ('read_jobs', 'kill', 'stop_applet', 'redis_down', 'terminal_down',
                                                     'run')]
    assert names.index('read_jobs') < names.index('kill')
    assert max(i for i, n in enumerate(names) if n == 'kill') < names.index('redis_down')
    assert max(i for i, n in enumerate(names) if n == 'redis_down') < names.index('terminal_down')
    assert names.index('terminal_down') < names.index('run')


def test_the_command_leaves_the_process_group_it_is_about_to_stop(tmp_path):
    """Started by an applet (detached) or in a fleet pane, the command is in a process group the stop ends; it moves
    into a session of its own first and finishes even when its original group is killed."""
    marks = tmp_path / 'marks'
    script = ('import os, sys, time\n'
              'from fleetmux import upgrade\n'
              'marks = sys.argv[1]\n'
              'upgrade.detach()\n'
              'open(marks + ".detached", "w").write("%d %d" % (os.getpid(), os.getsid(0)))\n'
              'time.sleep(1.5)\n'
              'open(marks + ".survived", "w").write("yes")\n')
    proc = subprocess.Popen([sys.executable, '-c', script, str(marks)], cwd=str(REPO), process_group=0,
                            env=dict(os.environ, PYTHONPATH=str(REPO)), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    deadline = time.time() + 20
    detached = Path(str(marks) + '.detached')
    while not detached.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert detached.exists()
    time.sleep(0.1)
    pid, sid = map(int, detached.read_text().split())
    assert sid == pid and os.getpgid(pid) != proc.pid  # a new session, out of the original group
    os.killpg(proc.pid, signal.SIGTERM)  # what a stop does to the group the command was started in
    proc.wait(timeout=10)
    survived = Path(str(marks) + '.survived')
    while not survived.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert survived.read_text() == 'yes'


def test_detach_is_the_first_thing_the_real_command_does(tmp_path):
    old, new = make_release(tmp_path, 'aaaaaaa1'), make_release(tmp_path, 'bbbbbbb2')
    tree = Tree(tmp_path, old)
    ops = FakeOps(tree, new)
    code, lines = run_upgrade(tree, ops, new)
    assert code == 0, lines
    assert ops.events[0] == ('detach',)
