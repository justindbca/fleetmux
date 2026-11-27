"""`fleetmux upgrade` writes the files the next release resumes from out of the snapshot taken BEFORE anything stops
(fleetmux/upgrade.py, steps backup and restore_files): Redis is read once, while the fleet still runs; what a dying
daemon does to its Redis afterwards never reaches <run>/jobs.json.
"""
import json
from pathlib import Path

from upgrade_kit import FakeOps, Tree, make_release, run_upgrade
from fleetmux import fleet, release_drift

JOBS = {'job-aaaaaaaaaa': {'state': 'running', 'slot': 'worker-1', 'task': 'M1', 'created': 1.0},
        'job-bbbbbbbbbb': {'state': 'queued', 'created': 2.0}}


def tree_and_ops(tmp_path, monkeypatch):
    old, new = make_release(tmp_path, 'aaaaaaa1'), make_release(tmp_path, 'bbbbbbb2')
    tree = Tree(tmp_path, old)
    tree.child('M1', job='job-aaaaaaaaaa')
    ops = FakeOps(tree, new, jobs=JOBS)
    monkeypatch.setattr(fleet, 'daemon_running', ops.daemon_running)
    return tree, ops, new


def test_jobs_json_comes_from_the_snapshot_before_the_stop(tmp_path, monkeypatch):
    tree, ops, new = tree_and_ops(tmp_path, monkeypatch)
    real_kill = ops.kill

    def kill(pid, sig):  # the old root drops rows while it goes down (the incident): Redis is not read again
        ops.jobs_source.clear()
        real_kill(pid, sig)
    ops.kill = kill
    code, lines = run_upgrade(tree, ops, new)
    assert code == 0, lines
    kinds = [e[0] for e in ops.events]
    first_kill, start = kinds.index('kill'), kinds.index('run')
    reads = [i for i, k in enumerate(kinds) if k == 'read_jobs']
    assert len([i for i in reads if i < first_kill]) == 1 and not [i for i in reads if first_kill < i < start]
    assert json.loads((tree.run / 'jobs.json').read_text()) == JOBS
    index = json.loads((tree.home / 'job-runs' / (tree.run.name + '.json')).read_text())
    assert index['run'] == str(tree.run)


def test_the_backup_lives_outside_the_run_directory(tmp_path, monkeypatch):
    tree, ops, new = tree_and_ops(tmp_path, monkeypatch)
    code, lines = run_upgrade(tree, ops, new)
    assert code == 0, lines
    out = json.loads(lines[-1])
    backup = Path(out['backup'])
    assert tree.run not in backup.parents and tree.home in backup.parents
    assert json.loads((backup.parent / 'fx-jobs.json').read_text()) == JOBS
    before = json.loads(backup.read_text())
    assert before['jobs'] == JOBS and before['run'] == str(tree.run)


def test_restore_files_follow_redis_down_order(tmp_path, monkeypatch):
    tree, ops, new = tree_and_ops(tmp_path, monkeypatch)
    written = []
    real = release_drift.write_jobs

    def spy(plan, jobs):
        written.append([e[0] for e in ops.events])
        return real(plan, jobs)
    monkeypatch.setattr(release_drift, 'write_jobs', spy)
    code, lines = run_upgrade(tree, ops, new)
    assert code == 0, lines
    snapshot, restore = written[0], written[1]
    assert 'kill' not in snapshot  # the snapshot, before any daemon stops
    assert restore.count('kill') == 2 and 'redis_down' not in restore  # after every stop, before Redis goes down


def test_job_mirror_of_a_live_root_is_unchanged(tmp_path):
    """write_job_mirror is the extracted body of Fleet.job_mirror: same files, same content."""
    run = tmp_path / 'runs' / 'fleet-x'
    run.mkdir(parents=True)
    cfg = {'run': str(run), 'fleet_home': str(tmp_path), 'mode': 'service'}
    fleet.write_job_mirror(cfg, run, JOBS)
    assert json.loads((run / 'jobs.json').read_text()) == JOBS
    assert (tmp_path / 'job-runs' / 'fleet-x.json').exists()
    fleet.write_job_mirror(cfg, run, {})
    assert json.loads((run / 'jobs.json').read_text()) == {}
    assert not (tmp_path / 'job-runs' / 'fleet-x.json').exists()
