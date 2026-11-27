"""The release switch over a whole fleet tree (fleetmux/release_drift.py, docs/design.md section 56): the one
stop -> switch -> start -> check that the root daemon's automatic switch and the manual `fleetmux upgrade` share.

Covered here:

* stop: the root first, then the sub-fleets level by level (a level only once every parent let go of its lock, each
  daemon matched by its own config), then the applets, the job files from the snapshot, Redis down (and no longer
  answering) and the terminal server; the snapshot is also kept outside the run directory;
* switch: every config's ``python`` set to the target's interpreter before the target starts; ``FLEETMUX_BIN`` and
  ``PATH`` of the start pinned to the target;
* check: daemons, interpreter, jobs, supervisor, sub-fleets, applets, window, judged as "not worse than before the
  switch", each with its recovery commands in the outcome;
* a fleet home without start.sh: the manual entry runs the target's ``start --resume <run>``;
* a switch process that died between the stop and the start: the same command goes on from the start;
* one switch at a time: ``<run>/release-drift.lock``.

Everything is stubbed (tests/upgrade_kit.py): no real process is signalled, no Redis is reached.
"""
import fcntl
import json
from pathlib import Path

import pytest

from upgrade_kit import FakeOps, Tree, auto_plan, kills, make_release, py, run_upgrade
from fleetmux import fleet, release_drift, upgrade

JOBS = {'job-aaaaaaaaaa': {'state': 'running', 'slot': 'worker-1', 'task': 'M1', 'created': 1.0},
        'job-bbbbbbbbbb': {'state': 'queued', 'created': 2.0}}


def nested(tmp_path, monkeypatch, start_sh=True, window=False):
    old, new = make_release(tmp_path, 'aaaaaaa1'), make_release(tmp_path, 'bbbbbbb2')
    tree = Tree(tmp_path, old, start_sh=start_sh)
    tree.child('M1', job='job-aaaaaaaaaa')
    tree.child('M1.1', parent='M1')
    tree.child('M10')
    tree.child('M10.1', parent='M10')
    tree.child('M10.1.1', parent='M10.1')
    ops = FakeOps(tree, new, jobs=JOBS, window=window)
    monkeypatch.setattr(fleet, 'daemon_running', ops.daemon_running)
    return tree, ops, old, new


def depth(tree, run):
    return len([p for p in Path(run).relative_to(tree.run).parts if p == 'sub'])


# ---------------------------------------------------------------- section 2.1: the stop order
def test_switch_stops_nested_subfleets_level_by_level(tmp_path, monkeypatch):
    tree, ops, old, new = nested(tmp_path, monkeypatch)
    seen = []
    real = ops.kill

    def kill(pid, sig):
        run = next((r for r, p in ops.pid_of.items() if p == pid and r in ops.alive_runs), None)
        if run:
            parents = [str(p) for p in Path(run).parents if (p / 'config.json').exists() and str(p) != run]
            assert not any(ops.daemon_running(p) for p in parents), (run, 'a parent still holds its lock')
            seen.append(run)
        real(pid, sig)
    ops.kill = kill
    outcome = release_drift.switch(auto_plan(tree, ops, new), ops)
    assert outcome['ok'] is True, outcome
    assert seen[0] == str(tree.run)
    levels = [depth(tree, r) for r in seen[1:]]
    assert levels == sorted(levels) and levels[-1] == 3, seen
    assert sorted(seen[1:]) == sorted(str(d) for k, d in tree.nodes.items() if k != 'root')
    assert all(sig == release_drift.signal.SIGTERM for _, sig in kills(ops))


def test_switch_never_signals_a_child_while_its_parent_holds_its_lock(tmp_path, monkeypatch):
    tree, ops, old, new = nested(tmp_path, monkeypatch)
    real = ops.kill
    stubborn = str(tree.nodes['M1'])

    def kill(pid, sig):
        if ops.pid_of.get(stubborn) == pid:
            ops.events.append(('kill', stubborn, sig))  # SIGTERM taken, the lock is kept
            return
        real(pid, sig)
    ops.kill = kill
    outcome = release_drift.switch(auto_plan(tree, ops, new, stop_wait_s=10), ops)
    signalled = [Path(r).name for r, _ in [e[1:3] for e in ops.events if e[0] == 'kill']]
    assert 'M1.1' not in signalled, signalled  # its parent never let go: the child keeps running
    assert outcome['ok'] is False and outcome['step'] == 'stop_children', outcome
    assert 'M1' in outcome['reason']
    # the way back failed as well: the run is down, so the recovery is a start of the old release, not a rerun
    assert outcome['rolled_back'] is False, outcome
    assert not any('run the same command again' in r for r in outcome['recover']), outcome['recover']
    assert 'FLEETMUX_REL=%s %s --resume %s' % (old, tree.home / 'start.sh', tree.run) in outcome['recover']


def test_a_failed_way_back_without_start_sh_names_the_old_release_start(tmp_path, monkeypatch):
    tree, ops, old, new = nested(tmp_path, monkeypatch, start_sh=False)
    ops.start_codes = {'bbbbbbb2': 1, 'aaaaaaa1': 1}  # neither release comes up
    code, lines = run_upgrade(tree, ops, new)
    out = json.loads(lines[-1])
    assert code != 0 and out['rolled_back'] is False, out
    assert not any('run the same command again' in r for r in out['recover']), out['recover']
    assert any(r.endswith('%s %s start --resume %s' % (py(old), old / 'fleetmux' / 'fleet.py', tree.run))
               and 'FLEETMUX_HOME=%s' % tree.home in r and 'FLEETMUX_REL=%s' % old in r
               for r in out['recover']), out['recover']


def test_switch_stops_applets_after_the_daemons_and_before_redis(tmp_path, monkeypatch):
    tree, ops, old, new = nested(tmp_path, monkeypatch)
    ops.applet_pids = {'tick': (777, py(old))}
    outcome = release_drift.switch(auto_plan(tree, ops, new), ops)
    assert outcome['ok'] is True, outcome
    names = [e[0] for e in ops.events]
    last_kill = max(i for i, n in enumerate(names) if n == 'kill')
    assert last_kill < names.index('stop_applet') < names.index('redis_down')
    assert ops.applet_pids['tick'][0] != 777 and ops.applet_pids['tick'][1] == py(new)


def test_switch_starts_only_after_every_redis_stopped_answering(tmp_path, monkeypatch):
    tree, ops, old, new = nested(tmp_path, monkeypatch)
    root_sock = tree.cfg['redis_socket']
    ops.redis_lag[root_sock] = 3  # it still answers three looks after its SHUTDOWN SAVE
    looks = []
    real = ops.redis_answers

    def redis_answers(sock):
        up = real(sock)
        looks.append((sock, up, len(ops.started)))
        return up
    ops.redis_answers = redis_answers
    outcome = release_drift.switch(auto_plan(tree, ops, new), ops)
    assert outcome['ok'] is True, outcome
    ups = [up for sock, up, started in looks if sock == root_sock and started == 0]
    assert ups[-1] is False and ups.count(True) >= 3, ups  # the start came only once it stopped answering


def test_a_redis_that_keeps_answering_fails_the_switch_before_any_start(tmp_path, monkeypatch):
    tree, ops, old, new = nested(tmp_path, monkeypatch)
    ops.redis_lag[tree.cfg['redis_socket']] = 10 ** 6
    outcome = release_drift.switch(auto_plan(tree, ops, new), ops)
    assert outcome['ok'] is False and outcome['step'] == 'redis_down', outcome
    assert 'bbbbbbb2' not in ops.started


def test_switch_keeps_the_job_snapshot_outside_the_run_directory(tmp_path, monkeypatch):
    tree, ops, old, new = nested(tmp_path, monkeypatch)
    plan = auto_plan(tree, ops, new)
    outcome = release_drift.switch(plan, ops)
    backup = Path(outcome['backup'])
    assert backup.is_file() and tree.run not in backup.parents and backup.parent == Path(plan['backup_dir'])
    assert json.loads((backup.parent / 'fx-jobs.json').read_text()) == JOBS
    before = json.loads(backup.read_text())
    assert before['jobs'] == JOBS and before['supervisor'] == 'idle'
    assert json.loads((tree.run / 'jobs.json').read_text()) == JOBS


def test_the_job_files_are_written_from_the_snapshot_before_the_start(tmp_path, monkeypatch):
    """A stopping daemon may write its own jobs.json on its way out; the start must find the snapshot's."""
    tree, ops, old, new = nested(tmp_path, monkeypatch)
    real = ops.kill

    def kill(pid, sig):
        if ops.pid_of.get(str(tree.run)) == pid:
            (tree.run / 'jobs.json').write_text(json.dumps({'job-aaaaaaaaaa': {'state': 'cancelled'}}))
        real(pid, sig)
    ops.kill = kill
    seen = []
    ops.on_start.append(lambda target: seen.append(json.loads((tree.run / 'jobs.json').read_text())))
    assert release_drift.switch(auto_plan(tree, ops, new), ops)['ok'] is True
    assert seen[0] == JOBS


# ---------------------------------------------------------------- section 2.2: the interpreter
def test_switch_writes_the_target_interpreter_before_start(tmp_path, monkeypatch):
    tree, ops, old, new = nested(tmp_path, monkeypatch)
    ops.on_start.append(lambda target: None)
    real = ops.fake_start
    at_start = []

    def start(target=None):
        at_start.append((Path(target).name, {k: fleet.load(Path(d) / 'config.json')['python']
                                             for k, d in tree.nodes.items()}))
        if Path(target).name == 'bbbbbbb2':
            return  # the new release does not come up: the switch goes back to the old one
        real(target)
    ops.fake_start = start
    outcome = release_drift.switch(auto_plan(tree, ops, new, verify_s=5), ops)
    assert outcome['ok'] is False and outcome['rolled_back'] is True, outcome
    (first, to_new), (second, to_old) = at_start
    assert first == 'bbbbbbb2' and set(to_new.values()) == {py(new)}, to_new
    assert second == 'aaaaaaa1' and set(to_old.values()) == {py(old)}, to_old


def test_start_env_pins_the_target_terminal_binary(tmp_path):
    old, new = make_release(tmp_path, 'aaaaaaa1'), make_release(tmp_path, 'bbbbbbb2')
    plan = {'pythonpath': '', 'old': {'release': 'aaaaaaa1', 'dir': str(old)},
            'new': {'release': 'bbbbbbb2', 'dir': str(new)}}
    base = {'FLEETMUX_BIN': str(old / 'bin' / 'fleetmux'), 'PATH': str(old / 'bin') + ':/usr/bin', 'HOME': '/h'}
    env = release_drift.start_env(plan, plan['new'], base=base)
    assert env['FLEETMUX_BIN'] == str(new / 'bin' / 'fleetmux')
    assert env['PATH'].split(':')[0] == str(new / 'bin')
    assert env['FLEETMUX_REL'] == str(new) and env['HOME'] == '/h'


# ---------------------------------------------------------------- section 2.3: the checks
def test_verify_fails_on_a_daemon_left_on_the_old_interpreter_and_goes_back(tmp_path, monkeypatch):
    tree, ops, old, new = nested(tmp_path, monkeypatch)

    def keep_old(target):
        if Path(target).name == 'bbbbbbb2':  # M1.1's daemon comes back on the old release's interpreter
            pid = ops.pid_of[str(tree.nodes['M1.1'])]
            ops.argv_of[pid] = [py(old)] + ops.argv_of[pid][1:]
    ops.on_start.append(keep_old)
    outcome = release_drift.switch(auto_plan(tree, ops, new, verify_s=5), ops)
    assert outcome['ok'] is False and outcome['step'] == 'verify', outcome
    assert outcome['checks']['interpreter']['status'] == 'fail' and 'M1.1' in outcome['checks']['interpreter'][
        'detail']
    assert outcome['rolled_back'] is True and ops.started == ['bbbbbbb2', 'aaaaaaa1']


def test_verify_wants_each_open_job_back_or_completed(tmp_path, monkeypatch):
    tree, ops, old, new = nested(tmp_path, monkeypatch)

    def lose(target):
        if Path(target).name == 'bbbbbbb2':
            ops.jobs_source = {'job-bbbbbbbbbb': JOBS['job-bbbbbbbbbb']}
    ops.on_start.append(lose)
    outcome = release_drift.switch(auto_plan(tree, ops, new, verify_s=5), ops)
    assert outcome['ok'] is False and outcome['checks']['jobs']['status'] == 'fail', outcome
    assert 'job-aaaaaaaaaa' in outcome['checks']['jobs']['detail']
    resubmit = Path(outcome['backup']).parent / 'resubmit-job-aaaaaaaaaa.json'
    assert resubmit.is_file() and any(str(resubmit) in r for r in outcome['recover'])


def test_a_supervisor_held_before_the_switch_is_not_a_failure(tmp_path, monkeypatch):
    """What did not hold before the switch is reported as such, never failed or rolled back."""
    tree, ops, old, new = nested(tmp_path, monkeypatch)
    ops.supervisor = 'held'
    outcome = release_drift.switch(auto_plan(tree, ops, new), ops)
    assert outcome['ok'] is True, outcome
    check = outcome['checks']['supervisor']
    assert check['status'] == 'n/a' and 'before the switch' in check['detail'], check
    assert ops.started == ['bbbbbbb2']


def test_a_supervisor_held_only_after_the_switch_fails_and_goes_back(tmp_path, monkeypatch):
    tree, ops, old, new = nested(tmp_path, monkeypatch)
    ops.on_start.append(lambda target: setattr(ops, 'supervisor', 'held' if target.name == 'bbbbbbb2' else 'idle'))
    outcome = release_drift.switch(auto_plan(tree, ops, new, verify_s=5), ops)
    assert outcome['ok'] is False and outcome['step'] == 'verify', outcome
    assert outcome['checks']['supervisor']['status'] == 'fail'
    assert outcome['rolled_back'] is True and ops.started == ['bbbbbbb2', 'aaaaaaa1']


def test_no_window_before_the_switch_is_na_and_a_lost_window_fails(tmp_path, monkeypatch):
    tree, ops, old, new = nested(tmp_path, monkeypatch, window=False)
    ops.tree_window = False
    outcome = release_drift.switch(auto_plan(tree, ops, new), ops)
    assert outcome['ok'] is True and outcome['checks']['window']['status'] == 'n/a', outcome
    assert 'before the switch' in outcome['checks']['window']['detail']

    tree2, ops2, old2, new2 = nested(tmp_path / 'b', monkeypatch, window=True)
    ops2.tree_window = False  # the window was there before and does not come back
    outcome = release_drift.switch(auto_plan(tree2, ops2, new2, verify_s=5), ops2)
    assert outcome['ok'] is False and outcome['checks']['window']['status'] == 'fail', outcome


def test_the_switch_brings_up_a_named_subfleet_without_autostart(tmp_path, monkeypatch):
    tree, ops, old, new = nested(tmp_path, monkeypatch)
    tree.child('P', named=True, name='P', autostart=False)
    ops.alive_runs.add(str(tree.nodes['P']))
    ops.new_daemon(tree.nodes['P'], old)
    outcome = release_drift.switch(auto_plan(tree, ops, new), ops)
    assert outcome['ok'] is True, outcome
    (spec,) = ops.up_requests
    assert spec['op'] == 'subfleet.up' and spec['name'] == 'P'
    assert outcome['checks']['subfleets']['status'] == 'ok' and 'P' in outcome['checks']['subfleets']['detail']


def test_the_outcome_carries_each_check_and_the_way_back(tmp_path, monkeypatch):
    tree, ops, old, new = nested(tmp_path, monkeypatch, window=True)
    outcome = release_drift.switch(auto_plan(tree, ops, new), ops)
    assert outcome['ok'] is True, outcome
    assert list(outcome['checks']) == list(release_drift.CHECKS)
    assert all(c['status'] in ('ok', 'n/a') for c in outcome['checks'].values()), outcome['checks']
    saved = json.loads((tree.run / release_drift.OUTCOME).read_text())
    assert saved['checks'] == outcome['checks'] and saved['step'] == ''



def test_a_way_back_that_fails_only_its_checks_keeps_their_recovery(tmp_path, monkeypatch):
    """The old release is up again: a start --resume is not the way out, the failing check's own commands are."""
    tree, ops, old, new = nested(tmp_path, monkeypatch)

    def on_start(target):
        if Path(target).name == 'bbbbbbb2':  # the forward check fails ...
            pid = ops.pid_of[str(tree.nodes['M1.1'])]
            ops.argv_of[pid] = [py(old)] + ops.argv_of[pid][1:]
        else:  # ... and on the way back the supervisor comes up held
            ops.supervisor = 'held'
    ops.on_start.append(on_start)
    outcome = release_drift.switch(auto_plan(tree, ops, new, verify_s=5), ops)
    assert outcome['ok'] is False and outcome['rolled_back'] is False, outcome
    assert 'supervisor' in outcome['rollback_reason'], outcome
    assert not any('start --resume' in r or 'start.sh --resume' in r for r in outcome['recover']), outcome['recover']
    assert any(' status --config ' in r for r in outcome['recover']), outcome['recover']

def test_the_way_back_does_not_queue_again_a_job_that_completed_on_the_new_release(tmp_path, monkeypatch):
    tree, ops, old, new = nested(tmp_path, monkeypatch)
    seen = {}

    def on_new(target):
        seen[Path(target).name] = json.loads((tree.run / 'jobs.json').read_text())
        if Path(target).name == 'bbbbbbb2':
            results = tree.run / 'job-results'
            results.mkdir(exist_ok=True)
            (results / 'job-aaaaaaaaaa.json').write_text(json.dumps({'status': 'ok'}))
            ops.jobs_source = {'job-bbbbbbbbbb': JOBS['job-bbbbbbbbbb'],
                               'job-cccccccccc': {'state': 'queued', 'created': 3.0}}
            pid = ops.pid_of[str(tree.nodes['M1.1'])]  # and the check fails: back to the old release
            ops.argv_of[pid] = [py(old)] + ops.argv_of[pid][1:]
    ops.on_start.append(on_new)
    outcome = release_drift.switch(auto_plan(tree, ops, new, verify_s=5), ops)
    assert outcome['ok'] is False and outcome['rolled_back'] is True, outcome
    assert sorted(seen['aaaaaaa1']) == ['job-bbbbbbbbbb', 'job-cccccccccc'], seen  # done once is not run again


def test_no_server_before_the_switch_is_not_required_after_it(tmp_path, monkeypatch):
    tree, ops, old, new = nested(tmp_path, monkeypatch)
    ops.server = None  # the run had no fleetmux server
    ops.on_start.append(lambda target: setattr(ops, 'server', None))
    outcome = release_drift.switch(auto_plan(tree, ops, new), ops)
    assert outcome['ok'] is True, outcome
    assert 'server' not in outcome['checks']['daemons']['detail']


def test_a_server_that_ran_before_and_is_gone_after_fails_the_switch(tmp_path, monkeypatch):
    tree, ops, old, new = nested(tmp_path, monkeypatch)
    ops.on_start.append(lambda target: setattr(ops, 'server', None) if Path(target).name == 'bbbbbbb2' else None)
    outcome = release_drift.switch(auto_plan(tree, ops, new, verify_s=5), ops)
    assert outcome['ok'] is False and 'fleetmux server is not up' in outcome['checks']['daemons']['detail'], outcome
    assert outcome['rolled_back'] is True

# ---------------------------------------------------------------- section 2.4: no start.sh
def test_manual_switch_without_start_sh_runs_the_target_release_directly(tmp_path, monkeypatch):
    tree, ops, old, new = nested(tmp_path, monkeypatch, start_sh=False)
    code, lines = run_upgrade(tree, ops, new)
    assert code == 0, lines
    (argv,) = [e[1] for e in ops.events if e[0] == 'run' and 'start' in e[1]]
    assert argv == [py(new), str(new / 'fleetmux' / 'fleet.py'), 'start', '--resume', str(tree.run)]


def test_manual_switch_with_start_sh_uses_it_like_the_automatic_one(tmp_path, monkeypatch):
    tree, ops, old, new = nested(tmp_path, monkeypatch, start_sh=True)
    code, lines = run_upgrade(tree, ops, new)
    assert code == 0, lines
    ((argv, env),) = [(e[1], e[2]) for e in ops.events if e[0] == 'run' and '--resume' in e[1]]
    assert argv == [str(tree.home / 'start.sh'), '--resume', str(tree.run)] and env['FLEETMUX_REL'] == str(new)


# ---------------------------------------------------------------- died between the stop and the start
def test_rerun_after_the_switch_died_between_stop_and_start_goes_on_from_the_start(tmp_path, monkeypatch):
    tree, ops, old, new = nested(tmp_path, monkeypatch, start_sh=False)

    class Died(BaseException):
        pass

    def die(*a, **k):
        raise Died()
    real_start = ops.start
    ops.start = die
    with pytest.raises(Died):
        run_upgrade(tree, ops, new)
    assert not ops.daemon_running(tree.run) and (tree.run / release_drift.PLAN).is_file()
    assert not (tree.run / release_drift.OUTCOME).exists()
    kills_before, reads_before = list(kills(ops)), ops.read_calls
    ops.start = real_start
    code, lines = run_upgrade(tree, ops, new)
    assert code == 0, lines
    assert kills(ops) == kills_before and ops.read_calls >= reads_before
    assert ops.started == ['bbbbbbb2']
    assert json.loads((tree.run / 'jobs.json').read_text()) == JOBS  # from the backup of the first run
    assert any('continuing' in line for line in lines), lines


# ---------------------------------------------------------------- one switch at a time
def hold(run):
    fh = open(Path(run) / release_drift.LOCK, 'a+')
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fh


def test_manual_upgrade_exits_2_while_a_switch_holds_the_run_lock(tmp_path, monkeypatch):
    tree, ops, old, new = nested(tmp_path, monkeypatch)
    fh = hold(tree.run)
    try:
        code, lines = run_upgrade(tree, ops, new)
    finally:
        fh.close()
    assert code == 2, lines
    out = json.loads(lines[-1])
    assert out['step'] == 'preflight' and 'switch' in out['reason'], out
    assert not [e for e in ops.events if e[0] in ('kill', 'read_jobs', 'run')]
    assert not (tree.run / release_drift.PLAN).exists()


def test_the_switch_holds_the_run_lock_until_it_ends(tmp_path, monkeypatch):
    tree, ops, old, new = nested(tmp_path, monkeypatch)
    held = []
    ops.on_start.append(lambda target: held.append(release_drift.switch_running(tree.run)))
    outcome = release_drift.switch(auto_plan(tree, ops, new), ops)
    assert outcome['ok'] is True and held == [True]
    assert release_drift.switch_running(tree.run) is False


# ---------------------------------------------------------------- the manual entry is a shell over the switch
def test_upgrade_hands_its_plan_to_the_one_switch(tmp_path, monkeypatch):
    tree, ops, old, new = nested(tmp_path, monkeypatch)
    plans = []
    real = release_drift.switch

    def spy(plan, ops=None, **kw):
        plans.append(dict(plan))
        return real(plan, ops, **kw)
    monkeypatch.setattr(release_drift, 'switch', spy)
    code, lines = run_upgrade(tree, ops, new)
    assert code == 0, lines
    (plan,) = plans
    assert plan['new'] == {'release': 'bbbbbbb2', 'dir': str(new)} and plan['old']['release'] == 'aaaaaaa1'
    assert plan['manual'] is True and Path(plan['backup_dir']).parent == tree.home / 'upgrade'
    assert not hasattr(upgrade, 'Upgrade') or not hasattr(upgrade.Upgrade, 'stop_level')
