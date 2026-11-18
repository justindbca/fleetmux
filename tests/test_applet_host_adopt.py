"""The applet outlives its host daemon and is adopted on resume (docs/applets.md sections 2.2 and 2.3,
docs/applet-interface.md section 2.1).

The repo's private test Redis, ``tmp_path`` and the real fixture applet ``tests/fixtures/applet_minimal``. The
"daemon that dies" is a separate short-lived Python process that runs one ``applet_host.tick`` and exits, so the
applet is really re-parented. Every test kills what it started (process group) and checks with ``pgrep``.
"""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture: the repo's private test Redis)
from test_runtime_v2 import fx  # noqa: F401  (a v2 service fleet)
from test_applet_host import FIXTURE, WORKERS, alive, audits, wait_for, write_roster
from fleetmux import applet_host, roster

ROOT = Path(__file__).resolve().parent.parent
NAME = 'applet-minimal-1'
KEY = applet_host.key(NAME)
DAEMON = '''
import json, sys, types
from fleetmux import applet_host, fleet
cfg = fleet.load(sys.argv[1])
f = types.SimpleNamespace(cfg=cfg, r=fleet.connect(cfg), log=lambda *a, **k: None,
                          applet_specs=[json.loads(sys.argv[2])])
applet_host.tick(f)
'''


def row(**extra):
    out = {'name': NAME, 'command': ['{python}', 'minimal_applet.py'], 'cwd': str(FIXTURE),
           'heartbeat_s': 0.5, 'stale_after_s': 10, 'stop_grace_s': 5, 'durable_state': False}
    out.update(extra)
    return out


def first_daemon(f, **extra):
    """Start the applet from a separate daemon process that exits at once; return (applet pid, child pid)."""
    spec = roster.applet_entries([row(**extra)], FIXTURE, list(f.workers))[0][0]
    subprocess.run([sys.executable, '-c', DAEMON, str(Path(f.cfg['run']) / 'config.json'), json.dumps(spec)],
                   cwd=ROOT, check=True, timeout=30)
    rd = Path(f.cfg['run']) / 'applets' / NAME
    assert wait_for(lambda: (rd / 'child.pid').exists() and f.r.hget(KEY, 'hb_at'))
    pid = int((rd / 'pid').read_text())
    return pid, int((rd / 'child.pid').read_text())


@pytest.fixture
def procs():
    """Pids the test must not leave behind: killed (whole group) at the end, then checked."""
    seen = []
    yield seen
    for pid in seen:
        for kill in (os.killpg, os.kill):
            try:
                kill(pid, signal.SIGKILL)
            except OSError:
                pass
    assert wait_for(lambda: not any(alive(p) for p in seen), timeout=10)
    out = subprocess.run(['pgrep', '-f', 'minimal_applet.py'], capture_output=True, text=True).stdout.split()
    assert not [p for p in out if int(p) in seen]


def resume(f, text):
    """The daemon comes back: the fleet of the same run reads its roster again on its tick."""
    write_roster(f, WORKERS + text)
    f.roster_tick()


def test_applet_survives_its_daemon_and_is_adopted_on_resume(fx, procs):
    f = fx
    pid, child = first_daemon(f)
    procs += [pid, child]
    time.sleep(1.5)  # three heartbeats with the daemon gone
    assert alive(pid) and alive(child) and os.getppid() != pid
    owner = json.loads((Path(f.cfg['run']) / 'applets' / NAME / 'state' / 'host.json').read_text())
    assert owner['run'] == f.cfg['run'] and owner['pid'] == pid
    resume(f, 'applets:\n  - ' + json.dumps(row()) + '\n')
    f.tick()
    assert f.applet_runners[NAME].proc.pid == pid
    assert [a['pid'] for a in audits(f, 'applet.adopted')] == [str(pid)]
    assert wait_for(lambda: f.r.hget(KEY, 'state') == 'up', tick=f.tick)
    assert f.r.hget(KEY, 'restarts') == '0' and json.loads(f.r.hget(KEY, 'crashes')) == []
    assert not audits(f, 'applet.restarted')
    # the fleet stop still stops the adopted applet and its child
    f.v2.close()
    f.v2.reader = f.v2.bus.blocking_client(1000)
    f.stopping = f.explicit_stop = True
    f.run()
    assert wait_for(lambda: not alive(pid) and not alive(child))
    assert not (Path(f.cfg['run']) / 'applets' / NAME / 'state' / 'host.json').exists()


@pytest.mark.parametrize('why', ['stale heartbeat', 'no owner record'])
def test_resume_restarts_an_applet_it_cannot_adopt_and_counts_a_crash(fx, procs, why):
    f = fx
    pid, child = first_daemon(f, heartbeat_s=5, stale_after_s=6)
    procs += [pid, child]
    if why == 'stale heartbeat':
        f.r.hset(KEY, 'hb_at', repr(time.time() - 100))
    else:
        (Path(f.cfg['run']) / 'applets' / NAME / 'state' / 'host.json').unlink()
    resume(f, 'applets:\n  - ' + json.dumps(row(heartbeat_s=5, stale_after_s=6)) + '\n')
    f.tick()
    new = f.applet_runners[NAME].proc.pid
    procs.append(new)
    assert new != pid and not audits(f, 'applet.adopted')
    assert f.r.hget(KEY, 'restarts') == '1' and len(json.loads(f.r.hget(KEY, 'crashes'))) == 1
    assert why in audits(f, 'applet.restarted')[-1]['reason']
    assert wait_for(lambda: not alive(pid) and not alive(child))  # the old copy was stopped first
    rd = Path(f.cfg['run']) / 'applets' / NAME
    assert wait_for(lambda: f.r.hget(KEY, 'state') == 'up' and int((rd / 'child.pid').read_text()) != child,
                    tick=f.tick)
    procs.append(int((rd / 'child.pid').read_text()))
    f.v2.close()
    f.v2.reader = f.v2.bus.blocking_client(1000)
    f.stopping = f.explicit_stop = True
    f.run()
    assert wait_for(lambda: not any(alive(p) for p in procs))


def test_adopted_process_exit_is_seen_as_a_crash(fx, procs):
    f = fx
    pid, child = first_daemon(f)
    procs += [pid, child]
    resume(f, 'applets:\n  - ' + json.dumps(row()) + '\n')
    f.tick()
    os.killpg(pid, signal.SIGKILL)  # the adopted applet dies: no exit code is known for a process not ours
    assert wait_for(lambda: f.applet_runners[NAME].proc is not None and f.applet_runners[NAME].proc.pid != pid,
                    tick=f.tick)
    procs.append(f.applet_runners[NAME].proc.pid)
    assert audits(f, 'applet.exited')[-1]['code'] == 'unknown' and f.r.hget(KEY, 'restarts') == '1'
    rd = Path(f.cfg['run']) / 'applets' / NAME
    assert wait_for(lambda: f.r.hget(KEY, 'state') == 'up', tick=f.tick)
    assert wait_for(lambda: (rd / 'child.pid').exists() and int((rd / 'child.pid').read_text()) != child)
    procs.append(int((rd / 'child.pid').read_text()))
    f.v2.close()
    f.v2.reader = f.v2.bus.blocking_client(1000)
    f.stopping = f.explicit_stop = True
    f.run()
    assert wait_for(lambda: not any(alive(p) for p in procs))
