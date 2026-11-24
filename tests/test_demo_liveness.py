"""The daemon's liveness check of an official-CLI supervisor (RuntimeV2.cli_check, every 30 s: two checks in a row
without a `claude` / `codex` process on the pane's tty restart the supervisor) must not fire on the demo's scripted
supervisor, however slow the run. The actors are slowed down so the run spans at least three checks after the
supervisor is ready; the supervisor must keep its first session and the task must still end with one rejected and
one accepted claim."""
import json
import os
import re
import shutil
import subprocess
import sys

from test_demo_e2e import ROOT, demo_root, outer, own_processes, snapshot  # noqa: F401  (outer: fixture)

TURN_S = 16  # six scripted turns after the supervisor is ready: about 100 s, i.e. at least three checks
CHECK_S = 30


def field(rec, name):
    for where in (rec, rec.get('detail'), rec.get('audit'), (rec.get('audit') or {}).get('detail')):
        if isinstance(where, dict) and where.get(name) is not None:
            return where[name]
    return None


def test_slow_run_spans_three_liveness_checks_without_restarting_the_supervisor(outer):
    fh, st, env, home = outer
    before = (snapshot(fh), snapshot(st), snapshot(home))
    env = dict(env, FLEETMUX_DEMO_TURN_S=str(TURN_S))
    done = subprocess.run([sys.executable, str(ROOT / 'fleetmux' / 'fleet.py'), 'demo', '--no-view', '--keep'],
                          env=env, capture_output=True, text=True, timeout=400)
    out = done.stdout + done.stderr
    root = demo_root(out)
    try:
        audit = [json.loads(line) for line in (root / 'audit.jsonl').read_text().splitlines()]
        events = [r.get('event') for r in audit]
        # the supervisor kept its first session: no lost-link restart, no second spawn
        assert not [e for e in events if e and e.startswith('link.')], [e for e in events if e and e.startswith('link.')]
        spawns = [field(r, 'agent') for r in audit if r.get('event') == 'spawn']
        assert spawns.count('supervisor') == 1, spawns
        assert done.returncode == 0, out
        summary = out[out.index('Acceptance record (read from the audit stream'):]
        assert re.findall(r'claimed done -> (\w+)', summary) == ['REJECTED', 'PASS']
        ready = min(float(field(r, 'at')) for r in audit
                    if r.get('event') == 'startup' and field(r, 'slot') == 'supervisor') / 1000.0
        ended = [float(field(r, 'at')) / 1000.0 for r in audit if r.get('event') == 'task.completed']
        assert ended and ended[0] - ready >= 3 * CHECK_S + 5, 'the run did not span three liveness checks'
        assert not own_processes(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)
    assert (snapshot(fh), snapshot(st), snapshot(home)) == before
