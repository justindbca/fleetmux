"""``fleetmux upgrade status --all-running --to <release>``: rollout's restart is detached, so its health
command decides whether the upgrade worked. Health protocol: the last stdout line is JSON with ``result`` pass / fail /
defer, exit 0 / 1 / 2 (defer carries ``next_check_at``). Every upgrade leaves ``<fleet home>/upgrade/last.json`` (its
own last line); ``--all-running`` leaves the machine's list of root trees, so a tree whose daemons are down mid-upgrade
is still checked. Roots and processes are injected; nothing running on this machine is read."""
import fcntl
import json
import time
from pathlib import Path

from test_upgrade_all_running import machine, run_all  # noqa: F401  (fixture)
from upgrade_kit import run_upgrade
from fleetmux import release_drift, upgrade


def status(m, *extra, roots=None):
    lines = []
    code = upgrade.main(['status', '--all-running', '--to', str(m['new'])] + list(extra), ops=m['ops']['A'],
                        out=lines.append, roots=roots or (lambda: [m['trees'][k].cfg_path for k in ('A', 'B')]),
                        ops_for=lambda cfg: m['by_cfg'][str(Path(cfg))])
    return code, json.loads(lines[-1]), lines


def test_every_upgrade_leaves_its_last_line_in_last_json(machine):
    t, ops = machine['trees']['A'], machine['ops']['A']
    code, lines = run_upgrade(t, ops, machine['new'])
    assert code == 0, lines
    last = json.loads((t.home / 'upgrade' / 'last.json').read_text())
    assert {k: last[k] for k in ('result', 'step', 'to')} == {'result': 'pass', 'step': '', 'to': 'bbbbbbb2'}
    assert last['config'] == str(t.cfg_path) and last['at']


def test_status_passes_when_every_daemon_runs_the_target(machine):
    assert run_all(machine)[0] == 0
    code, out, _ = status(machine)
    assert code == 0 and out['result'] == 'pass', out
    assert [t['result'] for t in out['trees']] == ['pass', 'pass'], out


def test_status_defers_while_an_upgrade_holds_the_lock(machine):
    assert run_all(machine)[0] == 0
    base = machine['trees']['B'].home / 'upgrade'
    with open(base / '.lock', 'a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # an upgrade of tree B is running
        code, out, _ = status(machine)
    assert code == 2 and out['result'] == 'defer' and out['next_check_at'], out
    assert 'in progress' in out['trees'][1]['reason'], out


def test_status_defers_before_the_upgrade_to_the_target_has_finished(machine):
    """Right after rollout started the detached restart: no upgrade to <to> is on record yet."""
    code, out, _ = status(machine)
    assert code == 2 and out['result'] == 'defer', out
    assert 'no finished upgrade to bbbbbbb2' in out['trees'][0]['reason'], out


def test_status_fails_with_the_step_of_the_last_failed_upgrade(machine):
    machine['ops']['A'].fail['redis_down'] = RuntimeError('socket refused')
    assert run_all(machine)[0] == 3
    code, out, _ = status(machine)
    assert code == 1 and out['result'] == 'fail' and out['step'] == 'redis_down', out
    assert out['trees'][0]['result'] == 'fail' and out['trees'][1]['result'] == 'pass', out


def test_status_fails_when_a_daemon_left_the_target_after_a_passing_upgrade(machine):
    assert run_all(machine)[0] == 0
    (machine['trees']['B'].nodes['M1'] / 'release').write_text('aaaaaaa1\n')
    code, out, _ = status(machine)
    assert code == 1 and out['result'] == 'fail' and 'aaaaaaa1' in out['reason'], out


def test_status_still_checks_a_tree_whose_root_is_down(machine):
    """A root that ps no longer lists (stopped) is still checked: the machine's record of the last --all-running keeps
    it in the list. Here its upgrade passed but its root daemon is down now, no upgrade holds the lock -> fail."""
    assert run_all(machine)[0] == 0
    machine['ops']['B'].alive_runs.discard(str(machine['trees']['B'].run))
    code, out, _ = status(machine, roots=lambda: [machine['trees']['A'].cfg_path])
    assert [t['config'] for t in out['trees']] == [str(machine['trees'][k].cfg_path) for k in ('A', 'B')], out
    assert code == 1 and out['trees'][1]['result'] == 'fail' and 'root' in out['trees'][1]['reason'], out


# ---------------------------------------------------------------- the root daemon's own switch got there first
def daemons_switched(tree, name, ok=True):
    """What the root daemon's automatic switch leaves behind: every daemon on ``name`` and its outcome file."""
    for d in tree.nodes.values():
        (d / 'release').write_text(name + '\n')
    (tree.run / release_drift.OUTCOME).write_text(json.dumps({
        'ok': ok, 'from': 'aaaaaaa1', 'to': name, 'step': None if ok else 'verify',
        'reason': None if ok else 'daemons: M1 is down', 'at': time.time() + 1, 'reported': time.time() + 2}))


def test_status_passes_when_the_daemons_own_switch_beat_a_refused_manual_upgrade(machine):
    """After ~/fleetmux-current moved, the root daemon may switch its tree itself while rollout's detached restart
    runs `upgrade --all-running`: that tree's manual upgrade is refused (the switch holds the run lock, nothing
    changed). Its last.json then says preflight fail for the same target; the switch that did the work decides."""
    a = machine['trees']['A']
    held = release_drift.acquire(a.run)                         # the daemon's switch of tree A is running
    try:
        code, lines = run_all(machine)
        assert json.loads((a.home / 'upgrade' / 'last.json').read_text())['step'] == 'preflight'
        assert status(machine)[1]['trees'][0]['result'] == 'defer'   # still switching
    finally:
        held.close()
    daemons_switched(a, 'bbbbbbb2')
    code, out, _ = status(machine)
    assert code == 0 and out['result'] == 'pass', out
    assert [t['result'] for t in out['trees']] == ['pass', 'pass'], out


def test_status_fails_with_the_daemons_own_switch_when_it_failed(machine):
    a = machine['trees']['A']
    held = release_drift.acquire(a.run)
    try:
        run_all(machine)
    finally:
        held.close()
    daemons_switched(a, 'bbbbbbb2', ok=False)
    code, out, _ = status(machine)
    assert code == 1 and out['trees'][0]['step'] == 'verify' and 'M1 is down' in out['trees'][0]['reason'], out


def test_a_refused_upgrade_with_no_switch_behind_it_still_fails(machine):
    a = machine['trees']['A']
    held = release_drift.acquire(a.run)
    try:
        run_all(machine)
    finally:
        held.close()
    code, out, _ = status(machine)                               # nobody switched tree A
    assert code == 1 and out['trees'][0]['step'] == 'preflight', out
