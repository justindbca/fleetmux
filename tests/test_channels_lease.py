"""Per-account channel lease (package P1b; brief §6.3, docs/design.md §53, docs/contracts.md §8).

An flock under ``config_dir()/leases/`` with a JSON holder record. flock is per open file description, so a second
``open`` in this process stands in for "another fleet holds it"; only the last test uses a real child process (it
is killed in ``finally``). No network, no listener; the config dir is a temp dir.
"""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture: the repo's private test Redis)
from test_channels_contract import Audit
from cmux_fleet import channels

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def config_dir(monkeypatch, tmp_path):
    d = tmp_path / 'fleetmux-config'
    monkeypatch.setenv(channels.CONFIG_ENV, str(d))
    return d


def lease_mod():
    from cmux_fleet.channels import lease
    return lease


def test_acquire_writes_holder_json_and_0700_dir(tmp_path):
    lease = lease_mod()
    run = tmp_path / 'runs' / 'r1'
    held = lease.acquire('telegram', 'fleet-bot-1', run_id='r1', run=str(run))
    try:
        assert held is not None
        h = lease.holder('telegram', 'fleet-bot-1')
        assert set(h) == {'run_id', 'run', 'pid', 'host', 'at'}
        assert (h['run_id'], h['run']) == ('r1', str(run))
        assert h['pid'] == os.getpid() and h['host'] == socket.gethostname()
        assert abs(float(h['at']) - time.time()) < 5
        path = channels.lease_path('telegram', 'fleet-bot-1')
        assert (path.parent.stat().st_mode & 0o777) == 0o700
        assert (path.stat().st_mode & 0o777) == 0o600
    finally:
        if held is not None:
            held.release()


def test_release_then_reacquire(tmp_path):
    lease = lease_mod()
    r1 = lease.acquire('telegram', 'fleet-bot-1', run_id='r1', run=str(tmp_path / 'r1'))
    assert r1 is not None
    assert lease.acquire('telegram', 'fleet-bot-1', run_id='r2', run=str(tmp_path / 'r2')) is None
    r1.release()
    r1.release()  # twice is harmless
    r2 = lease.acquire('telegram', 'fleet-bot-1', run_id='r2', run=str(tmp_path / 'r2'))
    try:
        assert r2 is not None and lease.holder('telegram', 'fleet-bot-1')['run_id'] == 'r2'
    finally:
        if r2 is not None:
            r2.release()


def test_dead_pid_lock_is_reclaimed_and_audited(tmp_path):
    lease = lease_mod()
    child = subprocess.Popen(['true'])
    child.wait()
    dead = child.pid
    path = channels.lease_path('telegram', 'fleet-bot-1')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps({'run_id': 'r-old', 'run': str(tmp_path / 'old'), 'pid': dead,
                                'host': socket.gethostname(), 'at': time.time() - 60}))  # no flock held
    audit = Audit()
    held = lease.acquire('telegram', 'fleet-bot-1', run_id='r2', run=str(tmp_path / 'r2'), audit=audit)
    try:
        assert held is not None
        reclaimed = [f for e, f in audit if e == 'channel.lease_reclaimed']
        assert len(reclaimed) == 1
        assert 'channel.lease_reclaimed' in channels.AUDIT_EVENTS  # P0b adds the name
        previous = reclaimed[0].get('previous', reclaimed[0])
        assert previous['run_id'] == 'r-old' and int(previous['pid']) == dead
        assert lease.holder('telegram', 'fleet-bot-1')['run_id'] == 'r2'
    finally:
        if held is not None:
            held.release()


def test_acquire_all_disables_only_busy_channel(tmp_path, private_redis_v2):  # noqa: F811
    lease = lease_mod()
    r = private_redis_v2
    r.flushdb()
    busy = lease.acquire('fake', 'a2', run_id='r1', run=str(tmp_path / 'r1'))
    assert busy is not None
    audit = Audit()

    def status(type_, account, **fields):
        r.hset(channels.STATUS + type_ + ':' + account, mapping={k: str(v) for k, v in fields.items()})
    push = list(channels.DEFAULT_PUSH)
    got = {}
    try:
        got = lease.acquire_all([{'type': 'fake', 'account': 'a1', 'push': push},
                                 {'type': 'fake', 'account': 'a2', 'push': push}],
                                run_id='r2', run=str(tmp_path / 'r2'), audit=audit, status=status)
        assert set(got) == {('fake', 'a1'), ('fake', 'a2')}
        assert isinstance(got[('fake', 'a1')], lease.Lease) and got[('fake', 'a2')] is None
        busy_rows = [f for e, f in audit if e == 'channel.lease_busy']
        assert len(busy_rows) == 1
        holder = busy_rows[0].get('holder', busy_rows[0])
        assert holder['run_id'] == 'r1'
        assert r.hget(channels.STATUS + 'fake:a2', 'state') == 'lease_busy'
        assert r.hget(channels.STATUS + 'fake:a1', 'state') != 'lease_busy'
    finally:
        for held in got.values():
            if held is not None:
                held.release()
        busy.release()


def test_lease_path_under_config_dir_never_run(tmp_path):
    lease = lease_mod()
    run = tmp_path / 'runs' / 'r1'
    held = lease.acquire('telegram', 'fleet-bot-1', run_id='r1', run=str(run))
    try:
        assert held is not None
        assert Path(held.path) == channels.lease_path('telegram', 'fleet-bot-1')
        assert Path(held.path).is_relative_to(channels.config_dir() / 'leases')
        assert not Path(held.path).is_relative_to(run)
    finally:
        if held is not None:
            held.release()


CHILD = ('import sys, time\n'
         'from cmux_fleet.channels import lease\n'
         'held = lease.acquire("telegram", "fleet-bot-1", run_id="r-child", run=sys.argv[1])\n'
         'print("held" if held is not None else "none", flush=True)\n'
         'time.sleep(60)\n')


def test_lease_released_when_process_exits(tmp_path, config_dir):
    lease = lease_mod()
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    env[channels.CONFIG_ENV] = str(config_dir)  # never the operator's real config dir
    p = subprocess.Popen([sys.executable, '-c', CHILD, str(tmp_path / 'child-run')], env=env, cwd=str(ROOT),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        line = ''
        deadline = time.time() + 20
        while time.time() < deadline and p.poll() is None:
            line = p.stdout.readline().strip()
            if line:
                break
        if line != 'held':
            p.kill()
            _, err = p.communicate(timeout=10)
            pytest.fail('child did not take the lease: ' + repr(line) + ' ' + err[-500:])
        assert lease.acquire('telegram', 'fleet-bot-1', run_id='r-main', run=str(tmp_path / 'main')) is None
        p.kill()
        p.wait(timeout=10)
        held = lease.acquire('telegram', 'fleet-bot-1', run_id='r-main', run=str(tmp_path / 'main'))
        assert held is not None
        held.release()
    finally:
        if p.poll() is None:
            p.kill()
            p.wait(timeout=10)
