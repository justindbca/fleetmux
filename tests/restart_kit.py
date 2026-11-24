"""Shared helpers of the tests/test_restart_*.py files (docs/design.md section 60).

Everything runs in process on private test Redis servers under /tmp, a fake cmux and the per-test fleet home /
FLEETMUX_SUPERPOWERS of tests/conftest.py: no live fleet, no real manifest.
"""
import json
import os
import shutil
import signal
import time
import uuid
from pathlib import Path

from test_subfleet import hook, start_private, step, stop_child_like_daemon
from fleetmux import applet_host, fleet, restart_state, runtime_v2, subfleet


def operator_stop(t, final=False):
    """`fleetmux stop [--final]` on the root: the stop event, then the explicit-stop tail as RuntimeV2.run runs it
    (restart_state.on_stop first, applets, sub-fleets, result.json). The daemon is gone afterwards."""
    f = t.f
    ev = {'kind': 'stop', 'request_id': 'stop-' + uuid.uuid4().hex[:6]}
    if final:
        ev['final'] = True
    runtime_v2.post_event(f.v2.bus, ev, ev['request_id'])
    for _ in range(20):
        if f.explicit_stop:
            break
        step(t)
    assert f.explicit_stop
    restart_state.on_stop(f)
    applet_host.stop_all(f)
    subfleet.stop_children(f, wait=0)
    for x in list(t.fleets):  # in-process child daemons take the stop and exit (their lock goes with them)
        if x is not f and not getattr(x, 'stopped', False) and x.cfg.get('parent_config') == str(f.path):
            stop_child_like_daemon(x)
            lock = getattr(x, 'lock', None)
            lock and lock.close()
    subfleet.stop_children(f, wait=0)  # collected now that they are gone
    f.write_result()
    if not (f.run_dir / 'result.json').exists():
        fleet.save_result(f.run_dir, {}, 'stopped', 'explicit fleet stop')
    getattr(f, 'job_stopped', lambda: None)()
    f.stopped = True
    return f


def crash(t):
    """The root daemon dies (kill -9 / power): nothing of the stop tail runs."""
    t.f.stopped = True
    return t.f


def new_run(t, ready=True, **over):
    """A new run of the same fleet home (what `start` makes when the previous run cannot be continued): a fresh run
    directory and an empty private Redis; the config records the previous run as `start` does."""
    old = t.f
    root = Path('/tmp/cfs-' + uuid.uuid4().hex[:8])
    t.__dict__.setdefault('extra_roots', []).append(root)
    rcfg, r = start_private(root)
    run = t.tmp / ('fleet-20261124-%06d-%s' % (len(t.extra_roots), uuid.uuid4().hex[:8]))
    (run / 'workspace').mkdir(parents=True)
    keep = {k: v for k, v in old.cfg.items() if k not in ('redis_socket', 'redis_runtime_dir', 'redis_pid', 'run',
                                                         'token', 'previous_run', 'no_restore', 'restart_not_applied')}
    cfg = dict(keep, **rcfg)
    cfg.update(run=str(run), token=uuid.uuid4().hex[:8], previous_run=str(old.run_dir))
    cfg.update(over)
    path = run / 'config.json'
    fleet.save(path, cfg)
    f = fleet.Fleet(path, r, t.world.manager(cfg), sessions=t.sessions)
    t.fleets.insert(0, f)
    t.f = f
    f.start_all()
    f.r.set(f.v2.prefix + ':boot', 'running')
    if ready:
        all_ready(t)
    return f


def all_ready(t, slots=None):
    f = t.f
    for slot in slots or f.names:
        if f.agent(slot).get('state') == 'starting':
            assert hook(t, f, slot, 'READY', prompt='startup')['status'] == 'ready', slot
    step(t)


def kill_extra(t):
    for root in t.__dict__.get('extra_roots', []):
        for pid_file in root.glob('*/redis.pid'):
            try:
                os.kill(int(pid_file.read_text()), signal.SIGTERM)
            except (ProcessLookupError, ValueError):
                pass
    time.sleep(.1)
    for root in t.__dict__.get('extra_roots', []):
        shutil.rmtree(root, ignore_errors=True)


def rdir(f):
    return restart_state.fleet_dir(f)


def ledger(f):
    return restart_state.current(f)


def ledgers(f):
    d = rdir(f) / 'ledgers'
    return [json.loads(p.read_text()) for p in sorted(d.glob('L*.json'))] if d.is_dir() else []


def items(f, kind=None):
    led = ledger(f) or (ledgers(f) or [None])[-1]
    return [i for i in (led or {}).get('items', []) if kind is None or i['kind'] == kind]


def reconcile_notices(f):
    out = []
    for mid in f.v2.bus.pending('supervisor') + [f.v2.bus.slot_state('supervisor').get('message_id') or 'x']:
        m = f.v2.bus.message(mid)
        if m and '[Reconcile' in m['body'].get('text', ''):
            out.append(m['body']['text'])
    return out


def snaps(f, reason=None):
    return [s for s in restart_state.snapshots(rdir(f)) if reason is None or s[1] == reason]


def tick(t, n=1):
    for _ in range(n):
        t.f.tick()
        step(t)
