"""`start` and every `--resume auto` new-run exit with open jobs in the fleet home (docs/design.md section 17 / 58).

A service root run that holds open jobs (`<run>/jobs.json`) is continued by `start`, `start --resume auto` and
`start --resume <dir>`; when it cannot be continued, a new run takes its never-started jobs over (same ids, same
order) and its started ones get a failed result. No backend, no real fleet home: `resume` and the new-run path are
replaced by recorders.
"""
import contextlib
import io
import json
import sys
import time
from pathlib import Path

import pytest

from test_escalate import ok_check  # noqa: F401  (fixture)
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_service_escalate import result, svc  # noqa: F401
from fleetmux import fleet, fleet_config, join


class NewRun(Exception):
    pass


ROW = {'goal': 'g', 'settings': {'workers': 1, 'hours': 1, 'supervisor_profile': None, 'worker_profiles': []},
       'artifacts': None, 'budget': None, 'lifecycle': None, 'state': 'queued', 'slot': '', 'task': '', 'run': '',
       'waiting': '', 'tries': 0, 'spawn': {}, 'hot_slot': ''}


def make_run(home, name='fleet-20260101-000000-aaaa', mode='service', v2=True, stopped=True, rows=None,
             latest=True):
    run = Path(home) / 'runs' / name
    run.mkdir(parents=True)
    cfg = {'run': str(run), 'mode': mode, 'backend': 'fleetmux', 'fleet_home': str(home),
           'transport_version': 2 if v2 else 1, 'redis_runtime_dir': str(run / 'redis')}
    fleet.save(run / 'config.json', cfg)
    if stopped:
        fleet.save(run / 'result.json', {'status': 'stopped', 'reason': 'explicit fleet stop'})
    if rows is not None:
        fleet.save(run / 'jobs.json', rows)  # fleet.JOBS_FILE
    if latest:
        fleet.save(Path(home) / 'latest.json', dict(cfg, config=str(run / 'config.json')))
    return run


def rows(*ids, state='queued'):
    return {jid: dict(ROW, goal='goal of ' + jid, state=state, created=1000.0 + i) for i, jid in enumerate(ids)}


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / 'home'
    h.mkdir()
    monkeypatch.setattr(fleet, 'HOME', h)
    monkeypatch.setattr(fleet, 'LATEST', h / 'latest.json')
    calls = []

    def resume(run, args):
        calls.append(Path(run))
        return True

    def new_run():
        raise NewRun(getattr(home_ns, 'args', None))
    home_ns = type('H', (), {})()
    home_ns.path, home_ns.calls = h, calls
    monkeypatch.setattr(fleet, 'resume', resume)
    monkeypatch.setattr(join, 'attach', lambda args: False)
    def start_new(args):
        home_ns.args = args
        raise NewRun(args)
    monkeypatch.setattr(fleet, 'start_new_run', start_new, raising=False)
    monkeypatch.setattr(fleet_config, 'from_env', lambda: (_ for _ in ()).throw(NewRun(None)))
    return home_ns


START = ['start', '--lifecycle', 'service', '--supervisor-cli', 'codex', '--worker-cli', 'claude',
         '--allow-development-v2']


def test_plain_start_in_a_home_with_open_jobs_continues_and_names_unapplied_options(home, capsys):
    run = make_run(home.path, rows=rows('job-a', 'job-b'))
    fleet.main(START + ['--workers', '3', '--max-attempts', '4'])
    assert home.calls == [run]
    out = capsys.readouterr().out
    assert 'continuing ' + str(run) in out and '2 open job' in out
    assert '--workers' in out.split('not applied')[1] and '--max-attempts' not in out.split('not applied')[1]


def test_scenario2_resume_auto_after_stop_continues_the_run_with_its_job(home, capsys):
    run = make_run(home.path, rows=rows('job-a'))
    fleet.main(START + ['--resume', 'auto'])
    assert home.calls == [run]
    assert 'starting a new run' not in capsys.readouterr().out


def test_resume_explicit_dir_continues_a_stopped_run_with_open_jobs(home):
    run = make_run(home.path, rows=rows('job-a'))
    fleet.main(START + ['--resume', str(run)])
    assert home.calls == [run]


def test_scenario2_latest_json_missing_finds_the_run_through_the_home_index(home):
    run = make_run(home.path, rows=rows('job-a'), latest=False)
    fleet.save(home.path / 'job-runs' / (run.name + '.json'), {'run': str(run), 'config': str(run / 'config.json')})
    fleet.main(START + ['--resume', 'auto'])
    assert home.calls == [run]


def test_scenario2_latest_json_is_a_stub_and_the_run_is_found(home):
    run = make_run(home.path, rows=rows('job-a'))
    fleet.save(home.path / 'latest.json', {'join': {'root_config': '/elsewhere/config.json', 'jobs': []}})
    fleet.main(START + ['--resume', 'auto'])
    assert home.calls == [run]


def test_runs_scan_finds_a_run_without_an_index_entry(home):
    run = make_run(home.path, rows=rows('job-a'), latest=False)
    fleet.main(START)
    assert home.calls == [run]


def test_stopped_run_without_open_jobs_still_starts_a_new_run(home):
    make_run(home.path, rows={})
    with pytest.raises(NewRun):
        fleet.main(START + ['--resume', 'auto'])
    assert home.calls == []


def test_live_or_sub_fleet_or_task_runs_are_never_continued(home, monkeypatch):
    make_run(home.path, name='fleet-20260101-000000-task', mode='task', rows=rows('job-a'))
    with pytest.raises(NewRun):
        fleet.main(START)
    assert home.calls == []


@pytest.mark.parametrize('broken', ['not_v2', 'unreadable', 'not_continuable'])
def test_run_that_cannot_be_continued_hands_its_rows_to_the_new_run(home, monkeypatch, broken):
    run = make_run(home.path, rows=rows('job-a', 'job-b'), v2=broken != 'not_v2')
    if broken == 'unreadable':
        (run / 'config.json').write_text('{not json')
    if broken == 'not_continuable':
        def refuse(r, args):
            raise fleet.NotContinuable('cannot resume ' + str(r) + ': its Redis runtime directory is gone')
        monkeypatch.setattr(fleet, 'resume', refuse)
    with pytest.raises(NewRun) as exc:
        fleet.main(START + ['--resume', 'auto'])
    carry = exc.value.args[0].carry_from
    assert [c['run'] for c in carry] == [str(run)] and carry[0]['reason']


def test_retryable_refusal_exits_nonzero_and_leaves_the_jobs_untouched(home, monkeypatch, capsys):
    run = make_run(home.path, rows=rows('job-a'))

    def refuse(r, args):
        raise SystemExit('cannot resume ' + str(r) + ': backend fleetmux is not usable here: no server')
    monkeypatch.setattr(fleet, 'resume', refuse)
    with pytest.raises(SystemExit) as exc:
        fleet.main(START)
    assert 'not usable' in str(exc.value.code)
    assert '1 open job' in capsys.readouterr().err
    assert json.loads((run / 'jobs.json').read_text()) == rows('job-a')


def test_stub_never_overwrites_latest_json_of_a_run_with_open_jobs(tmp_path):
    h = tmp_path / 'h'
    make_run(h, rows=rows('job-a'))
    assert join._occupied(h)
    h2 = tmp_path / 'h2'
    make_run(h2, rows={})
    assert not join._occupied(h2)


# ------------------------------------------------------------------ the new run takes the rows over
def in_process_cli(monkeypatch):
    """join.fleet_cli runs `fleet.py <argv>` in this process (the real status / result commands)."""
    def cli(argv, stdin=None, timeout=None):
        out, err = io.StringIO(), io.StringIO()
        monkeypatch.setattr(sys, 'stdin', io.StringIO(stdin or ''))
        code = 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                fleet.main(list(argv))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 2)
        return code, out.getvalue(), err.getvalue()
    monkeypatch.setattr(join, 'fleet_cli', cli)


def test_new_run_takes_over_never_started_rows_and_fails_started_ones_read_through_the_stub(
        svc, ok_check, tmp_path, monkeypatch, capsys):
    f = svc
    h = tmp_path / 'home'
    old_rows = rows('job-a', 'job-b')
    old_rows['job-run'] = dict(ROW, goal='running one', state='running', created=999.0, task='M0',
                               run=str(tmp_path / 'old' / 'sub' / 'M0'))
    old = make_run(h, name='fleet-20260101-000000-old0', rows=old_rows, latest=False)
    f.cfg['carry_from'] = [{'run': str(old), 'reason': 'its config is not a v2 config'}]
    f.cfg['fleet_home'] = str(h)
    f.stopping = True  # the rows stay queued for the assertions
    f.job_carry_in()
    assert [j for j, _ in sorted(f.job_state().items(), key=lambda x: x[1]['created'])] == ['job-a', 'job-b']
    assert f.r.hget(fleet.REQ + 'job-a', 'status') == 'job_queued'
    got = result(f, 'job-run')
    assert got['status'] == 'failed' and got['result']['outcome'] == 'not_carried' and 'M0' in got['error']
    carried = json.loads((old / fleet.JOBS_CARRIED).read_text())
    assert carried['carried_to'] == str(f.run_dir) and not (old / fleet.JOBS_FILE).exists()
    f.job_carry_in()  # once only
    assert len(f.job_state()) == 2
    # the stub still names the old run: it finds the root now running in its fleet home
    fleet.save(h / 'latest.json', dict(fleet.load(f.path), config=str(f.path), backend='fleetmux'))
    (f.run_dir / 'daemon.lock').touch()
    monkeypatch.setattr(fleet, 'daemon_running', lambda run: Path(run) == f.run_dir)
    monkeypatch.setattr(fleet, 'run_backend', lambda cfg: 'fleetmux')
    in_process_cli(monkeypatch)
    stub = tmp_path / 'stub.json'
    fleet.save(stub, {'join': {'root_config': str(old / 'config.json'), 'root_home': str(h), 'jobs': ['job-run']}})
    capsys.readouterr()
    assert join.cli(type('A', (), {'cmd': 'result', 'id': None, 'wait': 0})(), stub, fleet.load(stub)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['status'] == 'failed' and out['result']['outcome'] == 'not_carried'
    assert fleet.load(stub)['join']['root_config'] == str(f.path)  # rewritten to the live root
    data = fleet.load(stub)
    data['join']['jobs'].append('job-b')
    fleet.save(stub, data)
    assert join.cli(type('A', (), {'cmd': 'status'})(), stub, fleet.load(stub)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['job'] == 'job-b' and out['state'] == 'queued'


def test_carried_rows_are_dropped_when_the_old_run_is_resumed_later(svc, ok_check):
    f = svc
    f.stopping = True
    jid = __import__('test_service_job').submit(f)['job_id']
    fleet.save(f.run_dir / fleet.JOBS_CARRIED, {'carried_to': '/new/run', 'jobs': {jid: {}}})
    f.job_resume_reconcile()
    assert jid not in f.job_state() and not f.r.hgetall(fleet.REQ + jid)


def test_result_on_a_stub_reads_the_mirrored_result_when_the_root_is_down(tmp_path, monkeypatch, capsys):
    h = tmp_path / 'home'
    run = make_run(h, rows={})
    fleet.save(run / fleet.JOB_RESULTS / 'job-x.json', {'id': 'job-x', 'status': 'failed', 'error': 'cancelled'})
    stub = tmp_path / 'stub.json'
    fleet.save(stub, {'join': {'root_config': str(run / 'config.json'), 'root_home': str(h), 'jobs': ['job-x']}})
    monkeypatch.setattr(join, 'fleet_cli', lambda argv, stdin=None, timeout=None: pytest.fail('root is down'))
    assert join.cli(type('A', (), {'cmd': 'result', 'id': None, 'wait': 0})(), stub, fleet.load(stub)) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'failed'
