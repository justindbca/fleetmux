"""The daemon's switch-baseline check (fleetmux/baseline_check.py): every daemon compares its running switches with
this machine's baseline for its shape, restores the hot ones it has a writer for, reports the rest once (and once
more when they are back to normal), and restarts a fleet only after the user's own `fleetmux answer`.

In-process daemons on private test Redis servers under /tmp, a fake cmux and a per-test inventory file (no live
fleet, no real fleet-superpowers.yaml); the agents are the scripted hooks of tests/test_subfleet.py.
"""
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_service_split import queued_to
from test_subfleet import child_daemon, die, hook, given, no_launch, spawn_m0, step, svc, tree  # noqa: F401
from fleetmux import baseline, baseline_check, blocked_scope, cmux_manager, fleet, runtime_v2, subfleet
from fleetmux import fleet_superpowers as fs

MACHINE = 'machines:\n  box-a:\n    source: self\nmodels:\n  m1: {cli: claude}\n'
FUTURE, PAST = '2999-01-01', '2000-01-01'


@pytest.fixture(autouse=True)
def own_inventory(monkeypatch, tmp_path):
    monkeypatch.setenv(fs.ENV, str(tmp_path / 'superpowers' / 'fleet-superpowers.yaml'))


def write(text, machine=MACHINE):
    path = fs.file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    old = path.stat().st_mtime_ns if path.exists() else time.time_ns()
    path.write_text(machine + text)
    os.utime(path, ns=(old + 2_000_000_000, old + 2_000_000_000))  # a new stamp even within one clock tick
    return path


def block(shape='root_service', every=300, exceptions='', **switches):
    rows = ''.join('        %s: %s\n' % (k, v) for k, v in switches.items())
    return ('baseline:\n  version: 1\n  check_every_s: %s\n  machines:\n    box-a:\n      %s:\n%s'
            % (every, shape, rows)) + exceptions


def exception(switch, value, until, shape='root_service'):
    return ('  exceptions:\n    - {machine: box-a, shape: %s, switch: %s, value: %s, reason: not ready yet, until: %s}\n'
            % (shape, switch, value, until))


def notes(f):
    """[baseline] notices queued to this fleet's supervisor."""
    return [m['body']['text'] for m in queued_to(f, 'supervisor', '[baseline')]


def pushed(f):
    """[baseline] notices for the user (the run's outbound path: notifications.jsonl at an unmanaged root)."""
    path = f.run_dir / 'notifications.jsonl'
    rows = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
    return [r['text'] for r in rows if '[baseline' in r.get('text', '')]


def check(f, later=0):
    """One more check now (the interval is tested on its own)."""
    baseline_check.check(f, now=time.time() + later)


def errors(f, part='baseline'):
    return [x for _, x in f.r.xrange(f.v2.bus.audit_key) if x.get('event') == 'tick.error' and part in str(x)]


@pytest.fixture
def root(svc):
    svc.f.advisor_runner = lambda *a, **k: None  # no resident advisor tab in this world (one-shot runner)
    return svc


# -- 1. a hot switch is restored -------------------------------------------------------------------------------

def test_root_advisor_off_is_restored_in_one_tick_and_reported(root):
    f = root.f
    write(block(advisor='true'))
    assert not f.advisor_enabled()
    f.tick()
    assert f.advisor_enabled()
    from fleetmux import roster
    assert roster.advisor(Path(f.cfg['roster']).read_bytes()) is True  # written: the roster reload keeps it on
    f.roster_tick()
    assert f.advisor_enabled()
    n, = notes(f)
    assert 'restored' in n and 'advisor' in n and 'running false' in n and 'baseline true' in n
    assert len(pushed(f)) == 1 and 'restored' in pushed(f)[0]
    check(f)
    assert len(notes(f)) == 1  # nothing new: it is normal now
    assert not errors(f)


# -- 2. a restart switch is only reported, with the command ----------------------------------------------------

def test_restart_switch_deviation_is_reported_with_a_command_and_nothing_restarts(root):
    f = root.f
    write(block(supervisor_split='true'))
    f.tick()
    n, = notes(f)
    assert 'supervisor_split' in n and 'running false' in n and 'baseline true' in n
    assert 'fleetmux answer' in n and 'baseline:supervisor_split approve' in n and str(f.path) in n
    assert ' start --allow-development-v2 --resume ' + str(f.run_dir) in n  # what the approval runs
    assert len(pushed(f)) == 1 and 'baseline:supervisor_split' in pushed(f)[0]
    assert not f.stopping and not f.explicit_stop and not f.cfg.get('supervisor_split')
    assert fleet.load(f.path).get('supervisor_split') in (None, False)


def test_a_switch_that_needs_a_new_run_says_so_and_offers_no_answer_key(root):
    f = root.f
    write(block(kit_fleet='ops-kit'))
    f.tick()
    n, = notes(f)
    assert 'kit_fleet' in n and 'new run' in n and 'baseline:kit_fleet' not in n and '--kit-fleet ops-kit' in n
    assert not f.stopping


def test_max_busy_has_no_writer_and_is_only_reported(root):
    f = root.f
    write(block(max_busy=3))
    f.tick()
    n, = notes(f)
    assert 'max_busy' in n and 'running 0' in n and 'baseline 3' in n and 'restored' not in n
    assert (f.cfg.get('max_busy') or 0) == 0


# -- 3. the user's approval: daemon stop, then start --resume --------------------------------------------------

def answer(t, key, decision='approve', by='user', f=None):
    f = f or t.f
    rid = 'ans-' + str(time.time_ns())
    runtime_v2.post_event(f.v2.bus, dict(kind='escalation_answer', key=key, decision=decision, note='', by=by,
                                         request_id=rid, run_id=f.v2.bus.run_id), rid)
    for _ in range(20):
        step(t)
        ans = runtime_v2.wait_reply(f.v2.bus, rid, timeout=.2)
        if ans.get('status') != 'pending':
            break
    return ans


def resume_world(t, monkeypatch):
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: True)
    monkeypatch.setattr(fleet, 'LATEST', t.tmp / 'latest.json')
    monkeypatch.setattr(fleet, 'backends_check', lambda backend: True)
    monkeypatch.setattr(fleet, 'sweep_orphan_redis', lambda keep=None: None)
    spawned = []
    mgr = SimpleNamespace(spawn_window=lambda *a, **kw: spawned.append(a) or SimpleNamespace(success=True))
    monkeypatch.setattr(fleet, 'manager_for', lambda cfg: mgr)
    return spawned


def test_user_approval_stops_the_daemon_and_start_resume_brings_the_baseline(root, monkeypatch):
    t, f = root, root.f
    write(block(supervisor_split='true', lease_reclaim='"off"'))
    f.tick()
    assert len(notes(f)) == 2
    ans = answer(t, 'baseline:supervisor_split')
    assert ans['status'] == 'accepted', ans
    assert f.stopping and not f.explicit_stop  # the daemon-only stop: run, Redis and agents stay
    argv = ans['restart']['argv']
    assert argv[-3:] == ['--resume', str(f.run_dir)][-2:] or argv[-2:] == ['--resume', str(f.run_dir)]
    assert 'start' in argv and not (f.run_dir / 'result.json').exists()
    f.v2.close()  # the daemon loop ends (this in-process root holds no lock file)
    f.stopped = True
    spawned = resume_world(t, monkeypatch)
    ran = []

    def runner(cmd):
        ran.append(cmd)
        assert fleet.resume(Path(cmd[-1]), SimpleNamespace(max_attempts=None, roster=None, advisor=False,
                                                           resume=cmd[-1]))
        return 0
    assert baseline_check.restart_after_approval(f.cfg, ans, runner=runner, wait_s=2) == 0
    assert ran == [argv] and spawned  # one start --resume, which opened the daemon tab
    cfg = fleet.load(f.path)
    assert cfg['supervisor_split'] is True and cfg['lease_reclaim'] == 'off'  # the run config = baseline
    again = fleet.Fleet(f.path, fleet.connect(cfg), t.world.manager(cfg), sessions=t.sessions)  # the resumed daemon
    t.fleets.insert(0, again)
    t.f = again
    check(again)
    back = [n for n in notes(again) if 'back to normal' in n]
    assert len(back) == 2 and any('supervisor_split' in n for n in back)


def test_an_approved_restart_that_did_not_bring_the_baseline_is_reported_and_asked_again(root):
    t, f = root, root.f
    write(block(supervisor_split='true'))
    f.tick()
    assert answer(t, 'baseline:supervisor_split')['status'] == 'accepted'
    assert baseline_check.waiting(f) == {}
    check(f)  # the daemon came back some other way (e.g. a plain daemon restart): still off
    texts = notes(f)
    assert len(texts) == 2 and 'did not bring' in texts[1]
    assert 'baseline:supervisor_split' in baseline_check.waiting(f)


def test_the_stopping_daemon_does_not_check_again_after_an_accepted_approve(root):
    """F1: between the accepted approve and the daemon's stop, no check and no 'did not bring' report."""
    t, f = root, root.f
    write(block(lease_reclaim='"off"'))
    f.tick()
    assert len(notes(f)) == 1 and len(pushed(f)) == 1
    assert answer(t, 'baseline:lease_reclaim')['status'] == 'accepted' and f.stopping
    baseline_check.tick(f, now=time.time() + 10_000)  # due again while it stops
    f.tick()
    assert len(notes(f)) == 1 and len(pushed(f)) == 1
    assert not any('did not bring' in n for n in notes(f) + pushed(f))
    assert 'switch:lease_reclaim' in baseline_check._state(f)  # the new daemon still says back to normal


def test_answer_to_a_fleet_that_has_ended_says_so_without_a_traceback(root, monkeypatch, capsys):
    """F2: the fleet's Redis is gone (the fleet ended): one clear sentence and a non-zero exit."""
    import redis
    f = root.f
    gone = root.root / 'gone.sock'  # never created (a short path: AF_UNIX)

    def connect(cfg, socket_timeout=5):
        redis.Redis(unix_socket_path=str(gone), socket_connect_timeout=1).ping()  # the real error
    monkeypatch.setattr(fleet, 'connect', connect)
    with pytest.raises(SystemExit) as out:
        fleet.main(['answer', '--config', str(f.path), 'baseline:lease_reclaim', 'approve'])
    assert out.value.code == 2
    text = capsys.readouterr()
    assert 'Traceback' not in text.out + text.err
    ans = json.loads(text.out.strip().splitlines()[-1])
    assert ans['status'] == 'rejected' and 'ended' in ans['reason'] and 'nothing to answer' in ans['reason']
    assert str(f.run_dir) in ans['reason']


def test_the_supervisor_takes_a_baseline_notice_with_a_plain_reply(root):
    t, f = root, root.f
    write(block(supervisor_split='true'))
    f.tick()
    step(t)
    prompt = given(t, f, 'supervisor')[-1]
    assert '[baseline]' in prompt and 'information only' in prompt
    text = json.dumps({'status': 'RUNNING', 'dispatch': [], 'skip': []})
    assert hook(t, f, 'supervisor', text, prompt=prompt)['status'] == 'committed'
    step(t)
    assert not f.paused() and not f.stopping


def test_reject_keeps_the_fleet_running_and_the_same_deviation_is_not_asked_again(root):
    t, f = root, root.f
    write(block(supervisor_split='true'))
    f.tick()
    ans = answer(t, 'baseline:supervisor_split', 'reject')
    assert ans['status'] == 'accepted' and not ans.get('restart') and not f.stopping
    check(f)
    assert len(notes(f)) == 1
    assert answer(t, 'baseline:supervisor_split')['status'] == 'rejected'  # nothing waits any more


def test_unknown_baseline_key_is_rejected(root):
    t, f = root, root.f
    write(block(supervisor_split='true'))
    f.tick()
    ans = answer(t, 'baseline:lease_reclaim')
    assert ans['status'] == 'rejected' and not f.stopping


# -- 4. only the user approves ---------------------------------------------------------------------------------

def test_supervisor_decision_cannot_approve_a_baseline_key(root):
    t, f = root, root.f
    write(block(supervisor_split='true'))
    f.tick()
    step(t)
    prompt = given(t, f, 'supervisor')[-1]
    text = json.dumps({'status': 'RUNNING', 'dispatch': [], 'skip': [],
                       'escalations': [{'task': 'baseline:supervisor_split', 'decision': 'approve', 'note': 'ok'}]})
    hook(t, f, 'supervisor', text, prompt=prompt)
    step(t)
    assert not f.stopping
    assert 'baseline:supervisor_split' in baseline_check.waiting(f)


def test_answer_from_an_agent_pane_is_refused_also_for_a_sub_fleet_key(root, monkeypatch, tmp_path):
    f = root.f
    monkeypatch.chdir(f.run_dir / 'workspace')
    ans = blocked_scope.answer_send(f.cfg, 'baseline:supervisor_split', 'approve', wait=1)
    assert ans['status'] == 'rejected' and 'agent pane' in ans['reason']
    child_run = f.run_dir / 'sub' / 'M9'
    child_run.mkdir(parents=True)
    child = dict(f.cfg, run=str(child_run), parent_config=str(f.path))
    fleet.save(child_run / 'config.json', child)
    other = tmp_path / 'elsewhere'
    other.mkdir()
    monkeypatch.chdir(f.run_dir / 'workspace')  # a pane of the root, answering at the child's config
    ans = blocked_scope.answer_send(child, 'baseline:supervisor_split', 'approve', wait=1)
    assert ans['status'] == 'rejected' and 'agent pane' in ans['reason']


# -- 5. exceptions -----------------------------------------------------------------------------------------------

def test_live_exception_is_not_reported_and_an_expired_one_is(root):
    f = root.f
    write(block(supervisor_split='true', exceptions=exception('supervisor_split', 'false', FUTURE)))
    f.tick()
    assert notes(f) == []
    write(block(supervisor_split='true', exceptions=exception('supervisor_split', 'false', PAST)))
    check(f)
    n, = notes(f)
    assert 'supervisor_split' in n


def test_the_block_is_read_through_the_redis_cache_and_never_written_back(root):
    f = root.f
    path = write(block(supervisor_split='true'))
    before = path.read_bytes()
    f.tick()
    assert f.r.hget(baseline_check.CACHE, 'stamp')
    f.superpowers_flush()
    from fleetmux import fleet_superpowers_hot
    fleet_superpowers_hot.flush(f.r, force=True)
    assert path.read_bytes() == before
    assert 'baseline' not in fleet_superpowers_hot.HOT_BLOCKS


# -- 6. a job sub-fleet reports to its parent's supervisor -----------------------------------------------------

def job_child(t):
    rec = subfleet.spawn_child(t.f, 'worker-1', 'M0', 'write the vendor report', 1, launch=no_launch,
                               job={'id': 'job-abc123'})
    return rec, child_daemon(t, rec)


def test_job_subfleet_deviation_is_reported_to_the_root_supervisor(root):
    t, f = root, root.f
    write(block(shape='job', lease_reclaim='"off"'))
    rec, cf = job_child(t)
    assert baseline.shape_of(cf.cfg) == 'job'
    cf.tick()
    assert notes(cf) == []  # a sub-fleet's supervisor is not the one to tell
    f.tick()
    n, = notes(f)
    assert 'lease_reclaim' in n and 'fleet M0 (job' in n and 'baseline:lease_reclaim' in n and str(cf.path) in n
    assert 'information' in n.lower()  # nothing for the root supervisor to rule on
    assert len(pushed(f)) == 1  # the user, who alone can approve, is told at the root


def test_a_sub_fleet_approval_keeps_the_parent_from_collecting_it_while_it_restarts(root):
    t, f = root, root.f
    write(block(shape='job', lease_reclaim='"off"'))
    rec, cf = job_child(t)
    cf.tick()
    ans = answer(t, 'baseline:lease_reclaim', f=cf)
    assert ans['status'] == 'accepted' and ans['restart']['run'] == rec['run'], ans
    assert cf.stopping and not cf.explicit_stop
    assert ans['restart']['argv'][-2:] == ['--resume', rec['run']]
    die(cf)  # the child daemon is gone, start --resume has not run yet
    subfleet.tick(f)
    assert subfleet.record(Path(rec['run']))['state'] == 'running'  # not collected as ended
    assert not (Path(rec['run']) / 'result.json').exists()


def test_start_resume_of_a_sub_fleet_run_takes_the_baseline_and_leaves_latest_alone(root, monkeypatch):
    t, f = root, root.f
    write(block(shape='job', lease_reclaim='"off"'))
    rec, cf = job_child(t)
    cf.tick()
    ans = answer(t, 'baseline:lease_reclaim', f=cf)
    die(cf)
    spawned = resume_world(t, monkeypatch)
    (t.tmp / 'latest.json').write_text(json.dumps({'run': str(f.run_dir), 'config': str(f.path)}))
    code = baseline_check.restart_after_approval(cf.cfg, ans, wait_s=2, runner=lambda cmd: 0 if fleet.resume(
        Path(cmd[-1]), SimpleNamespace(max_attempts=None, roster=None, advisor=False, resume=cmd[-1])) else 1)
    assert code == 0 and spawned
    assert fleet.load(Path(rec['run']) / 'config.json')['lease_reclaim'] == 'off'
    assert json.loads((t.tmp / 'latest.json').read_text())['run'] == str(f.run_dir)  # still the root


def test_an_exception_names_a_sub_fleet_by_its_own_name(root):
    t = root
    write(block(shape='job', lease_reclaim='"off"', exceptions=exception('lease_reclaim', 'on', FUTURE, shape='job')
                .replace('reason:', 'fleet: M0, reason:')))
    rec, cf = job_child(t)
    cf.tick()
    t.f.tick()
    assert notes(t.f) == []


# -- 7. dedupe and back to normal ------------------------------------------------------------------------------

def test_same_deviation_is_reported_once_and_back_to_normal_once(root):
    f = root.f
    write(block(supervisor_split='true'))
    f.tick()
    check(f)
    check(f, 1)
    assert len(notes(f)) == 1
    f.cfg['supervisor_split'] = True  # what a restart that took the baseline leaves
    check(f, 2)
    check(f, 3)
    texts = notes(f)
    assert len(texts) == 2 and 'back to normal' in texts[1] and 'supervisor_split' in texts[1]
    f.cfg['supervisor_split'] = False
    check(f, 4)
    assert len(notes(f)) == 3  # a new deviation is reported again


def test_the_interval_comes_from_the_block(root):
    f = root.f
    write(block(supervisor_split='true', every=600))
    f.tick()
    assert len(notes(f)) == 1
    f.cfg['supervisor_split'] = True
    baseline_check.tick(f, now=time.time() + 60)  # inside the interval: not checked
    assert len(notes(f)) == 1
    baseline_check.tick(f, now=time.time() + 700)
    assert len(notes(f)) == 2


# -- 8. a broken block and a machine without baseline ----------------------------------------------------------

@pytest.mark.parametrize('bad', ['baseline:\n  version: 1\n  machines: [oops\n',
                                 'baseline:\n  version: 1\n  colour: red\n',
                                 'baseline:\n  version: 1\n  exceptions:\n    - {machine: box-a, shape: job, '
                                 'switch: advisor, value: on, reason: x}\n'])
def test_broken_block_is_reported_once_nothing_is_checked_and_the_tick_goes_on(root, bad):
    f = root.f
    write(bad)
    f.tick()
    check(f)
    n, = notes(f)
    assert 'baseline block' in n and ('broken' in n or 'rejected' in n or 'not valid' in n)
    assert len(pushed(f)) == 1
    assert not errors(f) and fs.read().get('models')  # the other blocks still load
    assert not f.advisor_enabled()


def test_a_fixed_block_is_checked_on_the_next_tick_not_after_the_default_interval(root):
    """F3: a broken block left the next check at the default 300 s; a new file stamp is checked at once."""
    f = root.f
    write('baseline:\n  version: 1\n  machines: [oops\n')
    baseline_check.tick(f)
    assert len(notes(f)) == 1 and 'broken' in notes(f)[0]
    baseline_check.tick(f, now=time.time() + 5)  # same file: inside the interval, nothing new
    assert len(notes(f)) == 1
    write(block(supervisor_split='true', every=3))
    baseline_check.tick(f, now=time.time() + 5)
    assert len(notes(f)) == 2 and 'supervisor_split' in notes(f)[1]
    f.cfg['supervisor_split'] = True
    baseline_check.tick(f, now=time.time() + 10)  # the block's own interval from now on
    assert len(notes(f)) == 3 and 'back to normal' in notes(f)[2]


def test_a_machine_without_baseline_entry_is_reported_once(root):
    f = root.f
    write(block(supervisor_split='true').replace('box-a:\n      root', 'box-b:\n      root'))
    f.tick()
    check(f)
    n, = notes(f)
    assert 'no baseline' in n and 'box-a' in n
    assert not f.cfg.get('supervisor_split')


def audited(f, event):
    return [x for _, x in f.r.xrange(f.v2.bus.audit_key) if x.get('event') == event]


def test_no_baseline_block_at_all_tells_the_user_once_per_root_service_run(root):
    f = root.f
    write('')
    f.tick()
    check(f)
    check(f, 1)
    assert notes(f) == [] and not errors(f)  # nothing for the supervisor: only the user can define one
    p, = pushed(f)
    assert 'no baseline' in p and str(fs.file_path()) in p and 'baseline:' in p
    assert len(audited(f, 'baseline.none')) == 1


def test_no_baseline_block_is_not_reported_by_a_task_root(tree):
    write('')
    t = tree
    t.f.tick()
    assert pushed(t.f) == [] and notes(t.f) == [] and len(audited(t.f, 'baseline.none')) == 1


def test_no_baseline_block_is_not_reported_by_a_sub_fleet(root):
    write('')
    rec, cf = job_child(root)
    cf.tick()
    assert not list((root.f.run_dir / 'subfleet-notices').glob('baseline-*'))
    assert len(audited(cf, 'baseline.none')) == 1


def test_no_inventory_file_at_all_is_only_audited(root):
    f = root.f
    assert not fs.file_path().exists()
    f.tick()
    assert pushed(f) == [] and notes(f) == [] and len(audited(f, 'baseline.none')) == 1


def test_a_failing_check_never_takes_the_tick_down(root, monkeypatch):
    f = root.f
    write(block(supervisor_split='true'))
    monkeypatch.setattr(baseline_check, 'check', lambda f, now=None: 1 / 0)
    f.tick()  # the part is isolated: logged, the other parts ran
    assert errors(f)


# -- 9. split sub-fleets are told apart ------------------------------------------------------------------------

def test_split_subfleet_is_recognised_even_under_a_job_parent(tree):
    t = tree
    rec, _ = spawn_m0(t)
    cfg = fleet.load(Path(rec['run']) / 'config.json')
    assert cfg['baseline_shape'] == 'split' and baseline.shape_of(cfg) == 'split'
    t.f.cfg['job'] = 'job-parent1'  # a job sub-fleet's own config carries `job`; its split children inherit keys
    t.f.cfg['named'] = {'name': 'x', 'autostart': False}
    rec2 = subfleet.spawn_child(t.f, 'worker-2', 'M1', 'another split', 1, launch=no_launch)
    cfg2 = fleet.load(Path(rec2['run']) / 'config.json')
    assert baseline.shape_of(cfg2) == 'split'
