"""The fixtures that start a private Redis stop it on every path (tests/redis_kit.py).

``start_tree`` (tests/test_subfleet.py) and ``managed`` (tests/test_resume.py) start a daemonised Redis that
pytest never reaps. Each case builds one failure right after the fixture started its Redis (setup raises, Redis
never answers, a generator is dropped, a caller raises before it drains the fixture) and checks that the Redis
the fixture started is gone and its root removed. The pids come only from what this test recorded; a safety net
stops a recorded Redis that is still alive and fails the test, so a wrong fixture leaves a red test and no orphan.
"""
import os
from pathlib import Path
import shutil
import signal
import time

import pytest

import redis_kit
import test_liveness_selfheal
from test_resume import managed  # noqa: F401  (fixture)
from test_subfleet import start_tree
from test_subfleet_named_tmux import service_tree
from test_named_subfleet_chain import named_chain_as_before  # noqa: F401  (fixture)
from fleetmux import fleet

pytestmark = pytest.mark.skipif(shutil.which('redis-server') is None, reason='redis-server unavailable')


@pytest.fixture(autouse=True)
def started(monkeypatch):
    """Records every Redis ``fleet.start_redis`` started in this test: pid, its root, alive right after the call."""
    seen = []
    real = fleet.start_redis

    def recording(cfg, *a, **kw):
        directory = Path(cfg['redis_runtime_dir'])
        try:
            return real(cfg, *a, **kw)
        finally:
            pid = redis_kit.read_pid(directory)
            if pid is not None:
                seen.append(dict(pid=pid, root=directory.parent, alive=not redis_kit.gone(pid)))
    monkeypatch.setattr(fleet, 'start_redis', recording)
    yield seen
    leaked = []
    for rec in seen:
        command = redis_kit.command_of(rec['pid'])
        if 'redis-server' in command and str(rec['root'].resolve()) + '/' in command:
            os.kill(rec['pid'], signal.SIGKILL)
            leaked.append(rec['pid'])
    for pid in leaked:
        for _ in range(100):
            if redis_kit.gone(pid):
                break
            time.sleep(.05)
    for rec in seen:
        if leaked:
            shutil.rmtree(rec['root'], ignore_errors=True)
    if leaked:
        pytest.fail('safety net had to stop test Redis %s: the fixture did not' % leaked)


def assert_stopped(seen):
    assert seen and all(rec['alive'] for rec in seen), seen  # a Redis was really up when the failure came
    for rec in seen:
        for _ in range(100):
            if redis_kit.gone(rec['pid']):
                break
            time.sleep(.05)
        assert redis_kit.gone(rec['pid']), 'Redis %s still runs' % rec['pid']
        assert not rec['root'].exists(), rec['root']


def raising(*a, **kw):
    raise RuntimeError('constructed failure')


def test_start_tree_setup_failure_stops_its_redis(started, tmp_path, monkeypatch):
    monkeypatch.setattr(fleet.Fleet, 'start_all', raising)
    gen = start_tree(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match='constructed'):
        next(gen)
    assert_stopped(started)


def test_start_tree_redis_that_never_answers_is_stopped(started, tmp_path, monkeypatch):
    monkeypatch.setattr(fleet, 'connect', raising)  # start_redis daemonises, then gives up with SystemExit
    gen = start_tree(tmp_path, monkeypatch)
    with pytest.raises(SystemExit):
        next(gen)
    assert_stopped(started)


def test_start_tree_closed_without_draining_stops_its_redis(started, tmp_path, monkeypatch):
    gen = start_tree(tmp_path, monkeypatch)
    next(gen)
    gen.close()  # what GC does to a dropped generator, made deterministic
    assert_stopped(started)


def test_start_tree_drained_after_writes_stops_its_redis(started, tmp_path, monkeypatch):
    gen = start_tree(tmp_path, monkeypatch)
    t = next(gen)
    for i in range(50):
        t.f.r.set('k%d' % i, 'v' * 100)
    for _ in gen:
        pass
    assert_stopped(started)


def test_service_tree_failing_first_tick_stops_its_redis(started, tmp_path, monkeypatch):
    monkeypatch.setattr(fleet.Fleet, 'roster_tick', raising)
    with pytest.raises(RuntimeError, match='constructed'):
        service_tree(tmp_path, monkeypatch)
    assert_stopped(started)


@pytest.mark.usefixtures('job_chains_as_before', 'named_chain_as_before')
def test_killed_up_autostart_failing_before_its_try_stops_its_redis(started, tmp_path, monkeypatch):
    monkeypatch.setattr(test_liveness_selfheal, 'adopt_view_child', raising)
    with pytest.raises(RuntimeError, match='constructed'):
        test_liveness_selfheal.killed_up_autostart(tmp_path, monkeypatch)
    assert_stopped(started)


def test_managed_redis_that_never_answers_is_stopped(started, request, monkeypatch):
    monkeypatch.setattr(fleet, 'connect', raising)
    with pytest.raises(SystemExit):
        request.getfixturevalue('managed')
    assert_stopped(started)
