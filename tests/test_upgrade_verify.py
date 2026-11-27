"""The checks `fleetmux upgrade` runs before it ends, what it prints when one fails, and what a failure
leaves: the one switch of fleetmux/release_drift.py goes back to the release that ran before, and the same command
can be run again (fleetmux/upgrade.py is its manual entry)."""
import json
from pathlib import Path

import pytest

from upgrade_kit import FakeOps, Tree, make_release, py, run_upgrade
from fleetmux import fleet, upgrade

JOBS = {'job-aaaaaaaaaa': {'state': 'running', 'slot': 'worker-1', 'task': 'M1', 'created': 1.0},
        'job-bbbbbbbbbb': {'state': 'queued', 'created': 2.0}}


def setup(tmp_path, monkeypatch, window=False):
    old, new = make_release(tmp_path, 'aaaaaaa1'), make_release(tmp_path, 'bbbbbbb2')
    tree = Tree(tmp_path, old)
    tree.child('M1', job='job-aaaaaaaaaa')
    tree.child('N', named=True, name='N', autostart=True)
    tree.child('P', named=True, name='P', autostart=False)
    ops = FakeOps(tree, new, jobs=JOBS, window=window)
    ops.applet_pids = {'tick': (777, py(old))}
    monkeypatch.setattr(fleet, 'daemon_running', ops.daemon_running)
    return tree, ops, old, new


def test_all_checks_pass_and_the_last_line_is_json(tmp_path, monkeypatch):
    tree, ops, old, new = setup(tmp_path, monkeypatch, window=True)
    code, lines = run_upgrade(tree, ops, new)
    assert code == 0, lines
    out = json.loads(lines[-1])
    assert out['result'] == 'pass' and out['step'] == '' and out['from'] == 'aaaaaaa1' and out['to'] == 'bbbbbbb2'
    for name in ('daemons', 'interpreter', 'jobs', 'supervisor', 'subfleets', 'applets', 'window'):
        assert any(line.startswith('upgrade: verify ' + name + ': ok') for line in lines), (name, lines)


def test_a_named_subfleet_without_autostart_is_brought_up_again(tmp_path, monkeypatch):
    tree, ops, old, new = setup(tmp_path, monkeypatch)
    code, lines = run_upgrade(tree, ops, new)
    assert code == 0, lines
    (spec,) = ops.up_requests
    assert spec['op'] == 'subfleet.up' and spec['name'] == 'P' and spec['id'].startswith('upgrade-')
    (argv,) = [e[1] for e in ops.events if e[0] == 'run' and 'request' in e[1]]
    assert argv[2:] == ['request', '-', '--config', str(tree.cfg_path)]
    kinds = [e for e in ops.events if e[0] == 'run']
    assert 'start' in kinds[0][1]  # after the root is up


def _break(case, tree, ops, old, new):
    def start(target):
        if target.name != new.name:
            ops.supervisor = 'idle'
            return  # the way back to the old release works
        if case == 'daemons':
            (tree.nodes['N'] / 'release').write_text('aaaaaaa1\n')
        elif case == 'interpreter':
            cfg = fleet.load(tree.nodes['M1'] / 'config.json')
            cfg['python'] = py(old)
            fleet.save(tree.nodes['M1'] / 'config.json', cfg)
        elif case == 'jobs':
            ops.jobs_source = {'job-bbbbbbbbbb': JOBS['job-bbbbbbbbbb']}
        elif case == 'supervisor':
            ops.supervisor = 'held'
        elif case == 'subfleets':
            ops.alive_runs.discard(str(tree.nodes['N']))
        elif case == 'applets':
            ops.applet_pids['tick'] = (None, py(new))
        elif case == 'window':
            ops.window_pid, ops.on_screen = None, set()
    ops.on_start.append(start)


RECOVER = {'daemons': 'fleetmux upgrade --to', 'interpreter': 'fleetmux upgrade --to',
           'jobs': 'fleetmux task --config', 'supervisor': 'fleetmux status --config',
           'subfleets': '"op": "subfleet.up"', 'applets': 'fleetmux applet', 'window': 'fleetmux gui'}


@pytest.mark.parametrize('case', sorted(RECOVER))
def test_each_failed_check_names_itself_and_prints_its_recovery(tmp_path, monkeypatch, case):
    tree, ops, old, new = setup(tmp_path, monkeypatch, window=True)
    _break(case, tree, ops, old, new)
    code, lines = run_upgrade(tree, ops, new, extra=['--verify-timeout', '0'])
    assert code == 1, lines
    out = json.loads(lines[-1])
    assert out['result'] == 'fail' and out['step'] == 'verify'
    assert case in out['reason']
    assert any(RECOVER[case] in cmd for cmd in out['recover']), out['recover']
    assert any(line.startswith('upgrade: verify ' + case + ': FAIL') for line in lines)
    assert out['rolled_back'] is True and ops.started == ['bbbbbbb2', 'aaaaaaa1']  # back on the release before



# ------------------------------------------------------------------ rollback_step: where the way back stopped
def test_a_passing_switch_has_no_rollback_step(tmp_path, monkeypatch):
    tree, ops, old, new = setup(tmp_path, monkeypatch)
    code, lines = run_upgrade(tree, ops, new)
    out = json.loads(lines[-1])
    assert code == 0 and 'rollback_step' in out and out['rollback_step'] is None, out


def test_a_way_back_that_worked_has_an_empty_rollback_step(tmp_path, monkeypatch):
    tree, ops, old, new = setup(tmp_path, monkeypatch)
    _break('jobs', tree, ops, old, new)
    code, lines = run_upgrade(tree, ops, new, extra=['--verify-timeout', '0'])
    out = json.loads(lines[-1])
    assert code == 1 and out['rolled_back'] is True and out['rollback_step'] == '', out


def test_a_way_back_whose_own_check_fails_names_verify(tmp_path, monkeypatch):
    """The old release is up again and only a check of the way back fails: rolled_back is false, rollback_step
    "verify" tells the caller the tree runs the release before."""
    tree, ops, old, new = setup(tmp_path, monkeypatch)
    ops.on_start.append(lambda target: setattr(ops, 'supervisor', 'held'))  # held on both releases
    code, lines = run_upgrade(tree, ops, new, extra=['--verify-timeout', '0'])
    out = json.loads(lines[-1])
    assert code == 1 and out['step'] == 'verify' and out['rolled_back'] is False, out
    assert out['rollback_step'] == 'verify', out
    assert ops.started == ['bbbbbbb2', 'aaaaaaa1']


def test_a_way_back_that_did_not_start_the_root_names_that_step(tmp_path, monkeypatch):
    tree, ops, old, new = setup(tmp_path, monkeypatch)
    ops.start_codes = {'bbbbbbb2': 1, 'aaaaaaa1': 1}
    code, lines = run_upgrade(tree, ops, new)
    out = json.loads(lines[-1])
    assert code == 4 and out['step'] == 'start_new' and out['rolled_back'] is False, out
    assert out['rollback_step'] == 'start_new', out


def test_a_refusal_before_anything_stopped_has_no_rollback_step(tmp_path, monkeypatch):
    tree, ops, old, new = setup(tmp_path, monkeypatch)
    code, lines = run_upgrade(tree, ops, old)  # already on that release: nothing to do, no way back
    out = json.loads(lines[-1])
    assert out['rollback_step'] is None, out

def test_a_lost_job_gets_a_resubmit_file_from_the_backup(tmp_path, monkeypatch):
    tree, ops, old, new = setup(tmp_path, monkeypatch)
    _break('jobs', tree, ops, old, new)
    code, lines = run_upgrade(tree, ops, new, extra=['--verify-timeout', '0'])
    out = json.loads(lines[-1])
    (cmd,) = [c for c in out['recover'] if 'fleetmux task' in c]
    path = cmd.split('< ')[-1].strip()
    assert json.loads(open(path).read())['goal'] is not None


def test_window_check_follows_the_snapshot_before_the_stop(tmp_path, monkeypatch):
    tree, ops, old, new = setup(tmp_path, monkeypatch, window=False)
    ops.tree_window = False
    monkeypatch.setenv('SSH_CONNECTION', '1 2 3 4')
    code, lines = run_upgrade(tree, ops, new)
    assert code == 0, lines
    assert any(line.startswith('upgrade: verify window: n/a') and 'no window before the switch' in line
               for line in lines), lines


def test_window_before_and_none_after_fails_also_over_ssh(tmp_path, monkeypatch):
    tree, ops, old, new = setup(tmp_path, monkeypatch, window=True)
    ops.tree_window = False
    monkeypatch.setenv('SSH_CONNECTION', '1 2 3 4')
    code, lines = run_upgrade(tree, ops, new, extra=['--verify-timeout', '0'])
    assert code == 1 and 'window' in json.loads(lines[-1])['reason']


def test_verify_waits_for_jobs_that_are_still_being_handed_on(tmp_path, monkeypatch):
    tree, ops, old, new = setup(tmp_path, monkeypatch)
    calls = []
    real = ops.redis_get

    def redis_get(sock, key):
        raw = real(sock, key)
        if ops.started and raw:
            calls.append(1)
            if len(calls) < 3:  # suspended -> readopt: running again a few polls later
                jobs = json.loads(raw)
                jobs['job-aaaaaaaaaa'] = dict(jobs['job-aaaaaaaaaa'], state='suspended')
                return json.dumps(jobs)
        return raw
    ops.redis_get = redis_get
    code, lines = run_upgrade(tree, ops, new)
    assert code == 0, lines
    assert len(calls) >= 3


# ------------------------------------------------------------------ a failed step: back on the old release, run again
def test_a_failed_stop_step_goes_back_and_the_same_command_then_passes(tmp_path, monkeypatch):
    """No step-by-step continuation any more; a failure after the root stopped goes back to the release
    before (the same switch the root daemon uses), and the same command is then simply run again."""
    tree, ops, old, new = setup(tmp_path, monkeypatch)
    ops.fail['redis_down'] = RuntimeError('socket refused')
    code, lines = run_upgrade(tree, ops, new)
    assert code == 3
    out = json.loads(lines[-1])
    assert out['step'] == 'redis_down' and 'socket refused' in out['reason']
    assert out['rolled_back'] is True and ops.started == ['aaaaaaa1']
    assert any('fleetmux upgrade --to' in c for c in out['recover'])
    code, lines = run_upgrade(tree, ops, new)
    assert code == 0, lines
    assert ops.started == ['aaaaaaa1', 'bbbbbbb2']
    assert json.loads((tree.run / 'jobs.json').read_text()) == JOBS


def test_start_failure_exits_4_goes_back_and_a_rerun_passes(tmp_path, monkeypatch):
    tree, ops, old, new = setup(tmp_path, monkeypatch)
    ops.start_codes['bbbbbbb2'] = 1
    code, lines = run_upgrade(tree, ops, new)
    out = json.loads(lines[-1])
    assert code == 4 and out['step'] == 'start_new' and out['rolled_back'] is True, lines
    assert ops.started == ['aaaaaaa1']
    ops.start_codes = {}
    code, lines = run_upgrade(tree, ops, new)
    assert code == 0, lines


def test_stop_failure_exits_3(tmp_path, monkeypatch):
    tree, ops, old, new = setup(tmp_path, monkeypatch)
    ops.fail['kill'] = PermissionError('not allowed')
    code, lines = run_upgrade(tree, ops, new)
    assert code == 3 and json.loads(lines[-1])['step'] == 'stop_root'
    assert not ops.started  # the root never stopped: nothing else was touched


def test_refusals_change_nothing_and_exit_2(tmp_path, monkeypatch):
    tree, ops, old, new = setup(tmp_path, monkeypatch)
    code, lines = run_upgrade(tree, ops, tmp_path / 'no-such-release')
    assert code == 2 and json.loads(lines[-1])['step'] == 'preflight'
    assert not [e for e in ops.events if e[0] in ('kill', 'read_jobs')]
    ops.pid_of.pop(str(tree.nodes['M1']))  # a daemon holds its lock but no process is found for it
    code, lines = run_upgrade(tree, ops, new)
    assert code == 2 and 'M1' in json.loads(lines[-1])['reason']
    assert not [e for e in ops.events if e[0] == 'kill']


def test_already_on_the_release_is_nothing_to_do(tmp_path, monkeypatch):
    tree, ops, old, new = setup(tmp_path, monkeypatch)
    code, lines = run_upgrade(tree, ops, new)
    assert code == 0
    n = len(ops.events)
    code, lines = run_upgrade(tree, ops, new)
    assert code == 0 and json.loads(lines[-1])['result'] == 'pass'
    assert 'already' in json.loads(lines[-1])['reason']
    assert not [e for e in ops.events[n:] if e[0] in ('kill', 'redis_down', 'run')]


def test_the_printed_way_back_names_the_release_before(tmp_path, monkeypatch):
    tree, ops, old, new = setup(tmp_path, monkeypatch)
    ops.start_codes['bbbbbbb2'] = 1
    code, lines = run_upgrade(tree, ops, new)
    assert code == 4
    back = [c for c in json.loads(lines[-1])['recover'] if '--to ' + str(old) in c]
    assert back, lines[-1]
    argv = back[0].split('#')[0].split()
    assert argv[1:3] == ['upgrade', '--to'] and argv[3] == str(old)
    code, lines = run_upgrade(tree, ops, old, extra=argv[4:])  # the switch already went back: nothing to do
    assert code == 0 and 'already' in json.loads(lines[-1])['reason'], lines


def test_a_subfleet_that_ran_before_and_did_not_come_back_fails(tmp_path, monkeypatch):
    tree, ops, old, new = setup(tmp_path, monkeypatch)
    lost = tree.child('M1.1', parent='M1')
    ops.alive_runs.add(str(lost))
    ops.new_daemon(lost, old)
    real = ops.revive
    ops.revive = lambda d, target=None: None if Path(d) == lost and Path(target).name == 'bbbbbbb2' else real(d,
                                                                                                           target)
    code, lines = run_upgrade(tree, ops, new, extra=['--verify-timeout', '0'])
    assert code == 1, lines
    out = json.loads(lines[-1])
    assert 'daemons' in out['reason'] and 'M1.1' in out['reason']


def test_a_subfleet_that_finished_meanwhile_is_not_a_failure(tmp_path, monkeypatch):
    tree, ops, old, new = setup(tmp_path, monkeypatch)
    done = tree.child('M1.1', parent='M1')
    ops.alive_runs.add(str(done))
    ops.new_daemon(done, old)
    real = ops.revive

    def revive(d, target=None):
        if Path(d) == done:  # its work ended while the tree was down: collected, not running
            fleet.save(done / 'subfleet.json', dict(json.loads((done / 'subfleet.json').read_text()), state='returned'))
            return
        real(d, target)
    ops.revive = revive
    code, lines = run_upgrade(tree, ops, new)
    assert code == 0, lines


# ------------------------------------------------------------------ jobs that ended during the upgrade
def _job_ended(tree, ops, jid, status):
    """job ``jid`` ended while the tree was down or during verify: no row in fx:jobs, its result on disk, its job
    sub-fleet M1 returned."""
    def start(target):
        if target.name != 'bbbbbbb2':
            return
        ops.jobs_source = {j: dict(r, state='running') for j, r in JOBS.items() if j != jid}
        results = tree.run / 'job-results'
        results.mkdir(exist_ok=True)
        (results / (jid + '.json')).write_text(json.dumps({'id': jid, 'status': status}))
        rec = json.loads((tree.nodes['M1'] / 'subfleet.json').read_text())
        rec['state'] = 'returned'
        (tree.nodes['M1'] / 'subfleet.json').write_text(json.dumps(rec))
    ops.on_start.append(start)


def test_a_job_that_succeeded_during_the_upgrade_is_ok_and_listed(tmp_path, monkeypatch):
    """A job that finished successfully meanwhile is not lost; no resubmit file (that would ask
    to redo finished work), and the output names it apart as completed during the switch."""
    tree, ops, old, new = setup(tmp_path, monkeypatch)
    _job_ended(tree, ops, 'job-aaaaaaaaaa', 'ok')
    code, lines = run_upgrade(tree, ops, new, extra=['--verify-timeout', '0'])
    assert code == 0, lines
    (line,) = [x for x in lines if x.startswith('upgrade: verify jobs: ok')]
    assert 'completed during the switch: job-aaaaaaaaaa' in line, line
    assert not list(Path(json.loads(lines[-1])['backup']).parent.glob('resubmit-*.json'))


@pytest.mark.parametrize('status', ['failed', 'blocked', 'cancelled', None])
def test_a_job_that_failed_during_the_upgrade_still_fails(tmp_path, monkeypatch, status):
    """Only a successful result counts as done: a failed / blocked / cancelled one, or none at all, fails the jobs check
    and gets its resubmit file."""
    tree, ops, old, new = setup(tmp_path, monkeypatch)
    _job_ended(tree, ops, 'job-aaaaaaaaaa', status)
    if status is None:
        ops.on_start.append(lambda target: (tree.run / 'job-results' / 'job-aaaaaaaaaa.json').unlink(missing_ok=True))
    code, lines = run_upgrade(tree, ops, new, extra=['--verify-timeout', '0'])
    assert code == 1, lines
    out = json.loads(lines[-1])
    assert 'jobs' in out['reason'] and 'job-aaaaaaaaaa' in out['reason'], out
    assert any('fleetmux task --config' in c for c in out['recover']), out['recover']
    assert not any('completed during the switch' in x for x in lines)


# ------------------------------------------------------------------ --to version:<v>
def _version_index(tmp_path, monkeypatch, new, version):
    """A temporary FLEETMUX_RELEASES whose versions/<v> link names release ``new`` (the installer's side of the index)."""
    import os
    root = Path(new).parent
    monkeypatch.setenv('FLEETMUX_RELEASES', str(root))
    (Path(new) / 'bin' / 'installed.json').write_text(json.dumps({'release_sha': Path(new).name, 'version': version}))
    (root / 'versions').mkdir(exist_ok=True)
    os.symlink('../' + Path(new).name, root / 'versions' / version)


def test_to_accepts_a_version_prefix(tmp_path, monkeypatch):
    tree, ops, old, new = setup(tmp_path, monkeypatch)
    _version_index(tmp_path, monkeypatch, new, '0.3.2')
    lines = []
    code = upgrade.main(['--to', 'version:0.3.2', '--config', str(tree.cfg_path)], ops=ops, out=lines.append)
    assert code == 0, lines
    out = json.loads(lines[-1])
    assert out['result'] == 'pass' and out['to'] == 'bbbbbbb2', out


def test_an_unknown_version_is_refused_before_anything_stops(tmp_path, monkeypatch):
    tree, ops, old, new = setup(tmp_path, monkeypatch)
    _version_index(tmp_path, monkeypatch, new, '0.3.2')
    lines = []
    code = upgrade.main(['--to', 'version:0.9.9', '--config', str(tree.cfg_path)], ops=ops, out=lines.append)
    assert code == 2, lines
    out = json.loads(lines[-1])
    assert out['result'] == 'fail' and out['step'] == 'preflight' and '0.9.9' in out['reason'], out
    assert not [e for e in ops.events if e[0] == 'kill'], ops.events
