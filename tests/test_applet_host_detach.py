"""Detach, root stop wait and move of an applet (docs/applets.md sections 2, 2.1 and 2.2, docs/applet-interface.md
sections 1, 2.1, 3.3 and 3.6).

A v2 service fleet from ``test_runtime_v2.fx`` on the repo's private test Redis. The lease side is the real
``applet_lease`` code driven through the helpers of ``test_applet_lease``; the applet process is a fake handle
(signals are recorded, never sent), except in the move test, which starts one real ``sleep`` child of its own.
"""
import hashlib
import json
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture: the repo's private test Redis)
from test_runtime_v2 import fx  # noqa: F401  (a v2 service fleet)
from test_applet_host import FakeProc
from test_applet_lease import NAME, TOKEN, ask, audits, decide, events, grant, lease_of, message, op, rec
from fleetmux import applet_host, applet_lease as al, fleet, roster


def row(base, **extra):
    out = {'name': NAME, 'command': ['{python}', 'app.py'], 'cwd': str(base), 'lease': {},
           'notify': {'max_per_hour': 2}, 'durable_state': False}
    out.update(extra)
    entries, errors = roster.applet_entries([out], base, ['worker-1', 'worker-2'])
    assert not errors
    return entries


@pytest.fixture
def det(fx, tmp_path, monkeypatch):
    """One running applet (fake process, hash as after its first heartbeat, token known to the lease helpers)."""
    f = fx
    clock = {'now': 1000.0}
    monkeypatch.setattr(al, 'clock', lambda: clock['now'])
    f.applet_clock = lambda: clock['now']
    signals, procs = [], []
    monkeypatch.setattr(applet_host, '_signal', lambda pid, sig, group=False: signals.append((pid, sig, group)))
    monkeypatch.setattr(applet_host, 'clear_stale', lambda *a, **k: None)

    def spawn(cfg, spec, token):
        procs.append(FakeProc())
        return procs[-1]
    f.applet_spawn = spawn
    for slot in f.workers:
        path = fleet.agent_cwd(f.cfg, slot) / 'AGENTS.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# Worker\n\n## 1. Rules\n- own directory only\n')
    f.row = row(tmp_path)
    f.applet_specs = list(f.row)
    applet_host.tick(f)
    f.r.hset(al.hkey(NAME), mapping={'state': 'up', 'pid': '4242',
                                     'token_sha256': hashlib.sha256(TOKEN.encode()).hexdigest()})
    f.clock, f.signals, f.procs = clock, signals, procs
    return f


def h(f):
    return f.r.hgetall(al.hkey(NAME))


def termed(f):
    return (f.procs[-1].pid, signal.SIGTERM, False) in f.signals


def detach(f):
    f.applet_specs = []
    applet_host.tick(f)


def drained(f):
    return [e for e in events(f, 'host.draining')]


def test_detach_refuses_new_leases_and_sends_host_draining(det):
    f = det
    lid = grant(f)
    detach(f)
    assert h(f)['detaching'] == '1' and h(f)['state'] == 'up' and not termed(f)
    [ev] = drained(f)
    assert ev['reason'] == 'detach' and ev['deadline_at'] == 1000 + 60
    assert ask(f, 'a.2')['reason'] == 'applet_detaching'
    al.tick(f)
    assert rec(f, lid)['state'] == 'active'  # the lease side keeps the lease while the applet may release it


def test_detach_stops_the_applet_once_it_released_its_leases(det):
    f = det
    lid = grant(f)
    detach(f)
    f.clock['now'] += 10
    out = op(f, 'applet.lease.release', 'x.1', lease=lid)
    assert out.get('lease'), out
    assert out['lease'] == {'id': lid, 'state': 'released'}
    al.tick(f)  # the lease side updates fx:applet:<name>:leases
    applet_host.tick(f)
    assert termed(f) and h(f)['state'] == 'stopping'
    f.procs[-1].code = 0
    applet_host.tick(f)
    assert not f.r.exists(al.hkey(NAME)) and not f.r.exists(al.events_key(NAME))  # keys cleared
    assert f.r.exists(al.lease_key(NAME, lid))  # the ended lease record is kept
    [row_] = audits(f, 'applet.detached')
    assert row_['applet'] == NAME and row_['reason'] == 'detach' and row_['code'] == '0'
    assert f.applet_runners == {}


def test_detach_revokes_leases_still_open_at_detach_wait_s(det):
    f = det
    lid = grant(f)
    assert message(f, 'm.1', lid) is None  # a message in flight under the lease
    detach(f)
    f.clock['now'] += 59
    applet_host.tick(f)
    assert rec(f, lid)['state'] == 'active' and not termed(f)
    f.clock['now'] += 1
    applet_host.tick(f)
    assert (rec(f, lid)['state'], rec(f, lid)['end_reason']) == ('revoked', 'applet_detached')
    assert events(f, 'lease.ended')[-1]['end_reason'] == 'applet_detached'
    assert json.loads(f.r.get(fleet.RESULT + 'm.1'))['status'] == 'lease_ended'
    assert termed(f)
    f.clock['now'] += 31
    applet_host.tick(f)
    assert (f.procs[-1].pid, signal.SIGKILL, True) in f.signals  # stop_grace_s, then the group


def test_putting_the_row_back_cancels_the_detach(det):
    f = det
    lid = grant(f)
    detach(f)
    assert ask(f, 'a.2')['reason'] == 'applet_detaching'
    f.applet_specs = list(f.row)
    f.clock['now'] += 30
    applet_host.tick(f)
    assert 'detaching' not in h(f) and h(f)['state'] == 'up'
    f.clock['now'] += 100
    f.r.hset(al.hkey(NAME), 'hb_at', repr(f.clock['now']))  # the applet's heartbeat
    applet_host.tick(f)
    al.tick(f)
    assert not f.signals and rec(f, lid)['state'] == 'active'
    assert ask(f, 'a.3') is None  # accepted: it now waits for the supervisor
    decide(f, {'id': 'a.3', 'decision': 'grant', 'slot': 'worker-2', 'note': 'ok'})
    assert lease_of(f, 'a.3')['slot'] == 'worker-2'


def test_detach_wait_zero_revokes_and_stops_at_once(det):
    f = det
    f.row = row(Path(f.row[0]['cwd']), detach_wait_s=0)
    f.applet_specs = list(f.row)
    applet_host.tick(f)
    lid = grant(f)
    detach(f)
    assert rec(f, lid)['end_reason'] == 'applet_detached' and termed(f)


def test_detach_of_an_applet_that_is_not_running(det):
    f = det
    f.procs[-1].code = 0
    applet_host.tick(f)
    assert h(f)['state'] == 'exited'
    detach(f)
    assert not f.r.exists(al.hkey(NAME)) and audits(f, 'applet.detached')


def test_root_stop_waits_detach_wait_s_and_parent_drain_reasons(det):
    f = det
    grant(f)
    al.tick(f)
    f.explicit_stop = f.stopping = True
    assert applet_host.stop_wait(f) is True
    [ev] = drained(f)
    assert ev['reason'] == 'stop' and ev['deadline_at'] == 1000 + 60
    f.clock['now'] += 60
    assert applet_host.stop_wait(f) is False  # the limit is the applet's detach_wait_s


def test_operator_stop_does_not_wait_or_detach(det):
    f = det
    grant(f)
    applet_host.request(f.r, NAME, 'stop', wait=0)
    applet_host.tick(f)
    assert termed(f) and drained(f) == [] and 'detaching' not in h(f)


def test_roster_detach_wait_s_default_and_bounds(tmp_path):
    assert row(tmp_path)[0]['detach_wait_s'] == 60
    assert row(tmp_path, detach_wait_s=0)[0]['detach_wait_s'] == 0
    _, errors = roster.applet_entries([{'name': NAME, 'command': ['x'], 'detach_wait_s': -1}], tmp_path, ['w-1'])
    assert errors and 'detach_wait_s' in errors[0]
    assert 'lease_wait_s' not in applet_host.SETTINGS


# ====================================================================== move: one applet, one host (2.2)
def test_move_waits_for_the_old_host_process(fx, tmp_path, monkeypatch):
    f = fx
    monkeypatch.setattr(fleet, 'HOME', tmp_path / 'home')
    procs = []

    def spawn(cfg, spec, token):
        procs.append(FakeProc())
        return procs[-1]
    f.applet_spawn = spawn
    monkeypatch.setattr(applet_host, '_signal', lambda *a, **k: None)
    spec = dict(row(tmp_path, durable_state=True)[0])
    owner = applet_host.state_dir(f.cfg, spec) / applet_host.OWNER
    owner.parent.mkdir(parents=True)
    old = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
    try:
        owner.write_text(json.dumps({'host': 'other', 'run': str(tmp_path / 'other-run'), 'pid': old.pid}))
        f.applet_specs = [spec]
        applet_host.tick(f)
        applet_host.tick(f)
        assert f.r.hget(al.hkey(NAME), 'state') == 'declared' and not procs
        assert len(audits(f, 'applet.attach_wait')) == 1
    finally:
        old.kill()
        old.wait(5)
    applet_host.tick(f)
    assert len(procs) == 1 and f.r.hget(al.hkey(NAME), 'state') == 'starting'
    record = json.loads(owner.read_text())
    assert record['run'] == f.cfg['run'] and record['pid'] == procs[-1].pid
    procs[-1].code = 0
    applet_host.tick(f)
    assert not owner.exists()  # removed once the process has exited
