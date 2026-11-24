"""Join stub commands after a root restart (docs/design.md section 19 "Commands on a stub").

`stop` on a stub cancels a job that has no sub-fleet yet (queued / starting / suspended) through the root, records
the cancel before a running job's sub-fleet is stopped, and still records it while the root is down.
"""
import json
from pathlib import Path

from test_start_join import alive, run_main, scan, stubbed, with_job  # noqa: F401  (fixtures)
from fleetmux import fleet


def test_stub_stop_cancels_a_queued_job_with_a_result(stubbed, tmp_path, monkeypatch, capsys):
    with_job(stubbed, tmp_path, state='queued', child=False)
    stubbed.replies['task'] = (0, json.dumps({'status': 'cancelled', 'job': 'job-abc'}), '')
    assert run_main(['stop', '--config', str(stubbed.path)], monkeypatch) == 0
    tasks = [c[0] for c in stubbed.calls if c[0][0] == 'task']
    assert tasks == [['task', '--cancel', 'job-abc', '--config', str(stubbed.root)]]
    assert not [c for c in stubbed.calls if c[0][0] == 'stop']
    assert json.loads(capsys.readouterr().out)['status'] == 'cancelled'


def test_stub_stop_cancels_a_starting_job_without_requeue(stubbed, tmp_path, monkeypatch, capsys):
    with_job(stubbed, tmp_path, state='starting', child=False)
    stubbed.replies['task'] = (0, json.dumps({'status': 'cancelled', 'job': 'job-abc'}), '')
    assert run_main(['stop', '--config', str(stubbed.path)], monkeypatch) == 0
    assert [c[0][:2] for c in stubbed.calls if c[0][0] == 'task'] == [['task', '--cancel']]


def test_stub_stop_of_a_running_job_records_the_cancel_on_the_root_first(stubbed, tmp_path, monkeypatch):
    run = with_job(stubbed, tmp_path)
    stubbed.replies['stop'] = (0, '{"status": "accepted"}\n', '')
    assert run_main(['stop', '--config', str(stubbed.path)], monkeypatch) == 0
    kinds = [c[0][:2] for c in stubbed.calls if c[0][0] in ('task', 'stop')]
    assert kinds[0] == ['task', '--cancel']
    assert ['stop', '--config', str(run / 'config.json')] in [c[0] for c in stubbed.calls]


def test_stub_stop_while_the_root_is_down_records_the_cancel_for_its_restart(stubbed, tmp_path, monkeypatch, capsys):
    with_job(stubbed, tmp_path, state='queued', child=False)
    stubbed.alive.clear()  # the root's daemon is gone
    assert run_main(['stop', '--config', str(stubbed.path)], monkeypatch) == 0
    note = Path(fleet.load(stubbed.root)['run']) / fleet.JOB_CANCELS / 'job-abc.json'
    assert fleet.load(note)['by'] == 'operator'
    assert 'when the root starts again' in capsys.readouterr().out
    assert stubbed.calls == []


def test_stub_finds_the_root_running_in_its_home_after_a_new_run(stubbed, tmp_path, monkeypatch, capsys):
    from test_start_join import make_run
    with_job(stubbed, tmp_path)
    data = fleet.load(stubbed.path)
    data['join']['root_home'] = str(stubbed.scan / 'fleets' / 'svc')
    fleet.save(stubbed.path, data)
    stubbed.alive.clear()  # the old run is down; a new run of the same home is live
    new = make_run(stubbed.scan / 'fleets' / 'svc', 'fleet-20260102-000000-new0', alive=stubbed.alive)
    assert run_main(['status', '--config', str(stubbed.path)], monkeypatch) == 0
    assert [c[0] for c in stubbed.calls] == [['status', '--config', str(new)]]
    assert fleet.load(stubbed.path)['join']['root_config'] == str(new)
    assert 'now runs' in capsys.readouterr().err
