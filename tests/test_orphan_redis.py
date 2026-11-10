"""reap_orphan_redis: a private Redis whose run was abandoned without `stop` is shut down at the
next `start`; live, young, still-referenced and parent-owned instances are left alone. Real
redis-server; ps is real too, so the abandoned run's path must not appear in any command line."""
import fcntl
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid

import pytest

from cmux_fleet import fleet, redis_runtime


@pytest.fixture
def make(tmp_path):
    binary = shutil.which('redis-server')
    if binary is None:
        pytest.skip('redis-server unavailable')
    root = Path('/tmp/cfo-' + uuid.uuid4().hex[:8])  # short: macOS Unix socket path limit
    started = []

    def _make(run, age=600):
        run = Path(run)
        run.mkdir(parents=True, exist_ok=True)
        cfg = dict(redis_runtime.prepare(root, uuid.uuid4().hex[:8]), redis_server=binary,
                   transport_version=2, run=str(run))
        client = fleet.start_redis(cfg)
        owner = Path(cfg['redis_runtime_dir']) / fleet.REDIS_OWNER
        os.utime(owner, (time.time() - age,) * 2)
        started.append(cfg)
        return cfg, client

    _make.root = root
    yield _make
    for cfg in started:
        pid_file = Path(cfg['redis_runtime_dir']) / 'redis.pid'
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass
    time.sleep(.2)
    shutil.rmtree(root, ignore_errors=True)


def up(cfg):
    try:
        fleet.connect(cfg).ping()
        return True
    except Exception:
        return False


def test_abandoned_run_is_shut_down_and_resume_reloads_it(make, tmp_path):
    cfg, client = make(tmp_path / 'runs' / 'fleet-gone')
    client.set('k', 'v')
    assert cfg['redis_socket'] in fleet.reap_orphan_redis(root=make.root)
    assert not up(cfg)
    assert fleet.start_redis(cfg).get('k') == 'v'  # lossless: what --resume does


def test_locked_run_is_kept(make, tmp_path):
    run = tmp_path / 'runs' / 'fleet-live'
    cfg, _ = make(run)
    with (run / 'daemon.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fleet.reap_orphan_redis(root=make.root)
    assert up(cfg)


def test_young_instance_is_kept(make, tmp_path):
    cfg, _ = make(tmp_path / 'runs' / 'fleet-new', age=0)
    fleet.reap_orphan_redis(root=make.root)
    assert up(cfg)


def test_run_named_by_a_live_process_is_kept(make, tmp_path):
    run = tmp_path / 'runs' / 'fleet-actor'
    cfg, _ = make(run)
    actor = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)', str(run / 'config.json')])
    try:
        fleet.reap_orphan_redis(root=make.root)
        assert up(cfg)
    finally:
        actor.kill(); actor.wait()


def test_child_of_live_parent_is_kept(make, tmp_path):
    parent = tmp_path / 'runs' / 'fleet-parent'
    cfg, _ = make(parent / 'sub' / 'memx')
    parent.mkdir(parents=True, exist_ok=True)
    with (parent / 'daemon.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fleet.reap_orphan_redis(root=make.root)
    assert up(cfg)


def test_keep_and_no_owner_are_skipped(make, tmp_path):
    run = tmp_path / 'runs' / 'fleet-resuming'
    cfg, _ = make(run)
    other, _ = make(tmp_path / 'runs' / 'fleet-legacy')
    (Path(other['redis_runtime_dir']) / fleet.REDIS_OWNER).unlink()
    fleet.reap_orphan_redis(keep=run, root=make.root)
    assert up(cfg) and up(other)
