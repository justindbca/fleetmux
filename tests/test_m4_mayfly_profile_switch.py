"""M44 (brief-m4 §7.3): the mayfly workers' profile switch on --backend fleetmux, as its own short real tree.

run2 of the E2 (world a131a9) logged SIGHUP in mayfly exit-1 / reentry-1 generation 1 at +16.4 s. The root
audit shows the only ``stop`` at +16.343 s, the finalizer's product stop after step 3 failed, and the child
retired its slots 0.1 s later (``subfleet.stop_children`` -> ``post_stop``). This test reproduces the same
world without the step-3 failure:

1. The E2's own steps 0-2 (imported, not copied), then mayfly ``up`` and the roster-profile switch of
   exit-1 / reentry-1 to generation 1 (``roster.profile_switched``).
2. Hold: no stop is sent for HOLD_S. Generation 1 of both slots must still be alive, idle and never
   signalled, and neither audit may contain a ``stop``.
3. §7.3 step 7 after the switch: a service request to exit-1 through the E2's own ``step5_mayfly_exit``. Its
   turn must reach generation 1's fake CLI.
4. The product stop (the E2's own ``stop_tree``). Generation 1 must be signalled only after the stop call.

Real processes only, as in the E2: real Redis / tmux / daemons, this worktree's fleetmux, the native fake CLI.
The world under ``~/.m4e/fm4-e2e/<id>/`` is kept. The E2's finalizer runs on every exit path.
"""
import json
import re
import signal
import time
from pathlib import Path
from types import SimpleNamespace

import test_m4_e2e_studio_ops as e2
from _fleetmux_milestone import milestone
from m4e import world as m4world
from m4e.evidence import Evidence
from m4e.inventory import Ledger, ps

pytestmark = milestone(4)
HOLD_S = 30  # run2's SIGHUP came 5.5 s after the switch; hold well past it with no stop
LIMIT_S = 900
MAYFLY_SLOTS = ('exit-1', 'reentry-1')


def gen1_logs(world, slot):
    """Fake-CLI logs of the slot's generation 1: cwd sub/mayfly/workspace/<slot>, first turn = the takeover."""
    out = []
    for p in sorted(world.ev.glob('fake-cli/claude-*.jsonl')):
        rows = [json.loads(line) for line in p.read_text().splitlines() if line.strip()]
        if not rows or not rows[0].get('cwd', '').endswith('sub/mayfly/workspace/' + slot):
            continue
        turns = [r for r in rows if r.get('event') == 'turn.start']
        if turns and turns[0].get('text', '').startswith('[Same-task takeover] You are ' + slot + '.1 '):
            out.append((p, rows))
    return out


def test_mayfly_generation_1_lives_after_the_profile_switch_until_the_product_stop():
    w = m4world.World()
    print('m4e mayfly profile-switch world: ' + str(w.dir))
    ctx = SimpleNamespace(world=w, ev=Evidence(w), ledger=Ledger(w), tree=None, kills=[], stopped=False)
    before, failed = None, None

    def on_alarm(_sig, _frame):
        raise e2.Deadline('mayfly profile-switch test exceeded %d s' % LIMIT_S)

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(LIMIT_S)
    try:
        w.build()
        before = ctx.ev.files()
        e2.step0_preconditions(ctx)
        e2.step1_start(ctx)
        e2.step2_main_fleet(ctx)
        tree = ctx.tree
        e2.wait_for('mayfly up', lambda: tree.hash('mayfly').get('state') == 'up', e2.AUTOSTART_LIMIT_S, 2)
        child, cr = tree.child('mayfly')
        for slot in MAYFLY_SLOTS:
            e2.wait_for(slot + ' profile switch to generation 1',
                        lambda: tree.events('roster.profile_switched', cfg=child, r=cr, agent=slot, generation=1),
                        300, 1)
            e2.wait_for(slot + ' generation 1 idle', lambda: tree.agent(slot, r=cr).get('state') == 'idle'
                        and tree.agent(slot, r=cr).get('generation') == '1', 300, 1)
        switched = {s: tree.events('roster.profile_switched', cfg=child, r=cr, agent=s)[-1] for s in MAYFLY_SLOTS}
        time.sleep(HOLD_S)

        procs = ps()
        alive = {}
        for slot in MAYFLY_SLOTS:
            logs = gen1_logs(w, slot)
            assert len(logs) == 1, (slot, [str(p) for p, _ in logs])
            path, rows = logs[0]
            pid = rows[0]['pid']
            bad = [r for r in rows if r.get('event') in ('signal', 'claude.exit')]
            assert not bad, '%s generation 1 was signalled / exited with no stop sent: %r' % (slot, bad)
            assert pid in procs, '%s generation 1 fake CLI pid %d is gone' % (slot, pid)
            agent = tree.agent(slot, r=cr)
            assert agent.get('generation') == '1' and agent.get('state') == 'idle', (slot, agent)
            alive[slot] = {'log': str(path), 'pid': pid, 'agent': agent}
        stops = {'root': tree.events('stop'), 'mayfly': tree.events('stop', cfg=child, r=cr)}
        assert stops == {'root': [], 'mayfly': []}, stops
        assert tree.hash('mayfly').get('state') == 'up', tree.hash('mayfly')
        ctx.ev.step(40, 'mayfly-g1-alive', {'hold_s': HOLD_S, 'switched': switched, 'alive': alive})

        # §7.3 step 7 after the switch: one service request to exit-1, answered by generation 1
        e2.step5_mayfly_exit(ctx, rid='m44-exit-g1', n=41)
        path, rows = gen1_logs(w, 'exit-1')[0]
        rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
        # The runtime moves a leading [Request <id>] into metadata (runtime_v2 "transport labels are metadata"),
        # so the id arrives as the payload's m4e_rid; accept either form (same rule as tests/m4e/fake_cli.py).
        rid_seen = re.compile(r'\[Request m44-exit-g1\]|"m4e_rid"\s*:\s*"m44-exit-g1"')
        assert any(r.get('event') == 'turn.start' and rid_seen.search(r.get('text', '')) for r in rows), \
            'm44-exit-g1 did not reach exit-1 generation 1 (' + str(path) + ')'

        stop_called = time.time()
        e2.stop_tree(ctx, tree.cfg_path)
        ctx.stopped = True
        signalled = {}
        for slot in MAYFLY_SLOTS:
            path, rows = gen1_logs(w, slot)[0]
            sigs = [r['at'] for r in rows if r.get('event') == 'signal']
            assert sigs and min(sigs) >= stop_called, (slot, stop_called, sigs)
            signalled[slot] = {'stop_called': stop_called, 'first_signal': min(sigs)}
        ctx.ev.step(42, 'mayfly-g1-stop', signalled)
    except BaseException as exc:
        failed = exc
        raise
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        problems = e2.finalize(ctx, before, failed)
        if problems and failed is not None:
            failed.add_note('m4e finalizer: ' + '\n'.join(problems))
        elif problems:
            raise AssertionError('m4e finalizer: ' + '\n'.join(problems))
