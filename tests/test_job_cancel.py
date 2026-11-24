"""`fleetmux cancel <job id>` (fleetmux/job_cancel.py): stop one job of a root fleet, never the root.

The root is stubbed (join._root / job_view / root_result / fleet_cli): no test reaches a running fleet.
"""
import json

import pytest

from fleetmux import fleet, job_cancel, join

JID = 'job-0123456789'


@pytest.fixture
def root(tmp_path, monkeypatch):
    """A root config and a child sub-fleet run of it; join's root calls recorded."""
    rconf = tmp_path / 'root' / 'config.json'
    rconf.parent.mkdir()
    rconf.write_text(json.dumps({'run': str(rconf.parent)}))
    child = tmp_path / 'root' / 'sub' / 'M1'
    child.mkdir(parents=True)
    (child / 'config.json').write_text(json.dumps({'run': str(child), 'parent_config': str(rconf)}))
    state = {'row': {'id': JID, 'state': 'running', 'subfleet': 'M1'}, 'item': {'state': 'running', 'run': str(child)},
             'result': (3, {}), 'argv': [], 'stop_code': 0}

    monkeypatch.setattr(join, '_root', lambda stub: {'run': str(rconf.parent)})
    monkeypatch.setattr(join, 'job_view', lambda stub, j: (state['row'], state['item']))
    monkeypatch.setattr(join, 'root_result', lambda stub, j, wait=0: state['result'])

    def fleet_cli(argv, stdin=None, timeout=None):
        state['argv'].append(list(argv))
        return state['stop_code'], json.dumps({'status': 'accepted'}), ''
    monkeypatch.setattr(join, 'fleet_cli', fleet_cli)
    state.update(config=rconf, child=child)
    return state


def never_the_root(state):
    root = str(state['config'])
    assert not any(a[:1] == ['stop'] and root in a for a in state['argv'])


def test_running_job_stops_its_own_sub_fleet_only(root):
    out = job_cancel.cancel(root['config'], JID)
    assert out['state'] == 'stopping' and out['stopped'] is True and out['job'] == JID
    assert root['argv'] == [['stop', '--config', str(root['child'] / 'config.json')]]
    never_the_root(root)


def test_a_sub_fleet_of_another_root_is_not_stopped(root, tmp_path):
    (root['child'] / 'config.json').write_text(json.dumps({'parent_config': str(tmp_path / 'other.json')}))
    with pytest.raises(join.JoinError, match='not a sub-fleet'):
        job_cancel.cancel(root['config'], JID)
    assert root['argv'] == []


@pytest.mark.parametrize('state', ['queued', 'starting'])
def test_a_job_without_a_sub_fleet_is_reported_pending_and_nothing_is_stopped(root, state):
    root['row'], root['item'] = {'id': JID, 'state': state}, None
    out = job_cancel.cancel(root['config'], JID)
    assert out['state'] == state and out['stopped'] is False and out['pending'] is True
    assert 'fleetmux cancel ' + JID in out['reason']
    assert root['argv'] == []


def test_an_ended_job_is_left_alone(root):
    root['row'], root['item'], root['result'] = None, None, (0, {'status': 'ok'})
    out = job_cancel.cancel(root['config'], JID)
    assert out['state'] == 'ended' and out['stopped'] is False and out['status'] == 'ok'
    assert root['argv'] == []


def test_an_unknown_job_is_an_error(root):
    root['row'], root['item'], root['result'] = None, None, (2, {})
    with pytest.raises(join.JoinError, match='no open job'):
        job_cancel.cancel(root['config'], JID)


@pytest.mark.parametrize('bad', ['', 'job-', 'job-XYZ', 'M1', 'job-0123456789; rm -rf /', '../job-0123456789'])
def test_only_a_job_id_is_accepted(root, bad):
    with pytest.raises(join.JoinError, match='job id'):
        job_cancel.cancel(root['config'], bad)
    assert root['argv'] == []


def test_a_failed_child_stop_is_reported(root):
    root['stop_code'] = 1
    out = job_cancel.cancel(root['config'], JID)
    assert out['stopped'] is False and out['state'] == 'running'


# ------------------------------------------------------------------ the command line
def run_main(argv, capsys):
    code = job_cancel.main(argv)
    out = capsys.readouterr()
    return code, (json.loads(out.out) if out.out.strip() else None), out.err


def test_main_with_the_root_config(root, capsys):
    code, out, _ = run_main([JID, '--config', str(root['config'])], capsys)
    assert code == 0 and out['state'] == 'stopping'
    never_the_root(root)


def test_main_with_a_join_stub_uses_its_root(root, capsys, tmp_path):
    stub = tmp_path / 'stub.json'
    stub.write_text(json.dumps({'join': {'root_config': str(root['config']), 'jobs': []}}))
    code, out, _ = run_main([JID, '--config', str(stub)], capsys)
    assert code == 0 and out['job'] == JID
    never_the_root(root)


def test_main_without_config_finds_the_running_root(root, capsys, monkeypatch):
    monkeypatch.setattr(join, 'find_root', lambda home: {'config': str(root['config'])})
    code, out, _ = run_main([JID], capsys)
    assert code == 0 and out['state'] == 'stopping'


def test_main_without_a_running_root(root, capsys, monkeypatch):
    monkeypatch.setattr(join, 'find_root', lambda home: None)
    code, out, err = run_main([JID], capsys)
    assert code == 2 and out is None and 'no running root fleet' in err


def test_main_pending_exits_3(root, capsys):
    root['row'], root['item'] = {'id': JID, 'state': 'queued'}, None
    code, out, _ = run_main([JID, '--config', str(root['config'])], capsys)
    assert code == 3 and out['pending'] is True


def test_main_wait_retries_until_the_sub_fleet_can_be_stopped(root, capsys, monkeypatch):
    views = [({'id': JID, 'state': 'queued'}, None)] * 2 + [(root['row'], root['item'])]
    monkeypatch.setattr(join, 'job_view', lambda stub, j: views.pop(0) if len(views) > 1 else views[0])
    sleeps = []
    monkeypatch.setattr(job_cancel.time, 'sleep', sleeps.append)
    code, out, _ = run_main([JID, '--config', str(root['config']), '--wait', '60'], capsys)
    assert code == 0 and out['state'] == 'stopping'
    assert sleeps == [job_cancel.RETRY_S, job_cancel.RETRY_S]
    never_the_root(root)


def test_main_bad_id_exits_2(root, capsys):
    code, out, err = run_main(['nope', '--config', str(root['config'])], capsys)
    assert code == 2 and 'job id' in err and root['argv'] == []


def test_fleet_main_dispatches_cancel_and_acp(monkeypatch):
    seen = []
    monkeypatch.setattr(job_cancel, 'main', lambda argv: seen.append(('cancel', argv)) or 0)
    from fleetmux import acp_server
    monkeypatch.setattr(acp_server, 'main', lambda argv: seen.append(('acp', argv)) or 0)
    for argv in (['cancel', JID], ['acp']):
        with pytest.raises(SystemExit) as exc:
            fleet.main(argv)
        assert exc.value.code == 0
    assert seen == [('cancel', [JID]), ('acp', [])]
