"""start / start --resume / an attaching start take the switch baseline (fleetmux/baseline.py).

The inventory file is the per-test temporary one (tests/conftest.py); no fleet, Redis or terminal is started.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from fleetmux import baseline, cmux_manager, fleet, join
from fleetmux import fleet_superpowers as fs
from test_advisor import fake_start

MACHINE = 'machines:\n  box-a:\n    source: self\n'
FUTURE, PAST = '2999-01-01', '2000-01-01'


@pytest.fixture(autouse=True)
def own_inventory(monkeypatch, tmp_path):
    """A fresh inventory file per test (the conftest one is shared by the whole session)."""
    monkeypatch.setenv(fs.ENV, str(tmp_path / 'superpowers' / 'fleet-superpowers.yaml'))


def write_baseline(text):
    path = fs.file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(MACHINE + text)
    return path


ROOT = '''baseline:
  version: 1
  machines:
    box-a:
      root_service:
        supervisor_split: true
        worker_idle_reclaim_min: 30
        lease_reclaim: "off"
        max_busy: 3
'''


def exception(switch, value, until, shape='root_service'):
    return ('  exceptions:\n    - {machine: box-a, shape: %s, switch: %s, value: %s, reason: not ready, until: %s}\n'
            % (shape, switch, value, until))


@pytest.fixture
def service_start(monkeypatch, tmp_path):
    argv = fake_start(monkeypatch, tmp_path)
    argv[argv.index('dev_task')] = 'service'
    argv[argv.index('codex')] = 'claude'
    return argv + ['--no-join']


def started(tmp_path):
    latest = fleet.load(tmp_path / 'latest.json')
    return fleet.load(latest['config']), Path(latest['run'])


def record(run):
    path = run / baseline.RECORD
    return json.loads(path.read_text()) if path.exists() else None


# -- a new run ---------------------------------------------------------------------------------------------------

def test_a_new_run_without_baseline_entries_starts_as_today(service_start, tmp_path, capsys):
    fleet.main(service_start)
    plain, run = started(tmp_path)
    write_baseline(ROOT.replace('box-a', 'box-b'))  # an entry for another machine only
    fleet.main(service_start)
    other, run2 = started(tmp_path)
    for key in ('supervisor_split', 'worker_idle_reclaim_min', 'lease_reclaim', 'max_busy'):
        assert plain.get(key, 'absent') == other.get(key, 'absent')
    assert 'worker_idle_reclaim_min' not in other
    out = capsys.readouterr().out
    assert record(run) is None and record(run2) is None and 'start: baseline' not in out and 'deviates' not in out


def test_a_new_run_takes_the_baseline_for_switches_not_typed(service_start, tmp_path, capsys):
    write_baseline(ROOT)
    fleet.main(service_start)
    cfg, run = started(tmp_path)
    assert cfg['supervisor_split'] is True and cfg['worker_idle_reclaim_min'] == 30
    assert cfg['lease_reclaim'] == 'off' and cfg['max_busy'] == 3
    assert record(run)['deviations'] == [] and 'deviates' not in capsys.readouterr().out


def test_a_contrary_flag_starts_as_typed_and_is_reported(service_start, tmp_path, capsys):
    write_baseline(ROOT)
    fleet.main(service_start + ['--worker-idle-reclaim-min', 'off', '--lease-reclaim', 'off'])
    cfg, run = started(tmp_path)
    assert cfg['worker_idle_reclaim_min'] is None  # started as typed, not refused
    out = capsys.readouterr().out
    assert 'deviates from baseline: worker_idle_reclaim_min' in out and 'lease_reclaim' not in out.split('deviates')[1]
    rows = record(run)['deviations']
    assert [r['switch'] for r in rows] == ['worker_idle_reclaim_min']  # the same value as the baseline: no report
    assert rows[0]['baseline'] == 30 and rows[0]['value'] is None and rows[0]['shape'] == 'root_service'


def test_an_exception_is_not_reported_before_until_and_is_after(service_start, tmp_path, capsys):
    write_baseline(ROOT + exception('worker_idle_reclaim_min', 'off', FUTURE))
    fleet.main(service_start)
    cfg, run = started(tmp_path)
    assert cfg['worker_idle_reclaim_min'] is None  # the exception's value is the default
    fleet.main(service_start + ['--worker-idle-reclaim-min', 'off'])
    cfg, run = started(tmp_path)
    assert record(run)['deviations'] == [] and 'deviates' not in capsys.readouterr().out
    write_baseline(ROOT + exception('worker_idle_reclaim_min', 'off', PAST))
    fleet.main(service_start + ['--worker-idle-reclaim-min', 'off'])
    cfg, run = started(tmp_path)
    assert [r['switch'] for r in record(run)['deviations']] == ['worker_idle_reclaim_min']
    assert 'deviates from baseline: worker_idle_reclaim_min' in capsys.readouterr().out


def test_a_typed_zero_is_typed(service_start, tmp_path, capsys):
    write_baseline(ROOT)
    fleet.main(service_start + ['--max-busy', '0'])
    cfg, run = started(tmp_path)
    assert cfg['max_busy'] == 0  # 0 = no cap, typed: never replaced by the baseline's 3
    assert [r['switch'] for r in record(run)['deviations']] == ['max_busy']


def test_a_broken_block_never_refuses_start(service_start, tmp_path, capsys):
    write_baseline('baseline:\n  version: 1\n  machines: {box-a: [unclosed\n')
    fleet.main(service_start)
    cfg, run = started(tmp_path)
    assert 'worker_idle_reclaim_min' not in cfg and cfg['supervisor_split'] is False  # as today
    assert 'baseline ignored' in capsys.readouterr().out
    assert record(run)['error']
    write_baseline(ROOT.replace('max_busy: 3', 'stall_timeout: 5'))  # unknown key: the whole block is off
    fleet.main(service_start)
    cfg, run = started(tmp_path)
    assert cfg['supervisor_split'] is False and 'stall_timeout' in record(run)['error']


def test_a_task_root_has_no_shape_and_is_left_alone(monkeypatch, tmp_path, capsys):
    write_baseline(ROOT)
    fleet.main(fake_start(monkeypatch, tmp_path))
    cfg, run = started(tmp_path)
    assert cfg['supervisor_split'] is False and record(run) is None


# -- start --resume ----------------------------------------------------------------------------------------------

def resumable(tmp_path, monkeypatch, drop=(), **extra):
    run = tmp_path / 'run'
    run.mkdir()
    cfg = dict(run=str(run), python=sys.executable, workers=['worker-1'], lifecycle='service', mode='service',
               supervisor_cli='codex', worker_cli='claude', transport_version=2, allow_development_v2=True,
               supervisor_split=False, worker_idle_reclaim_min=None, lease_reclaim='on', max_busy=0, name='main')
    cfg.update(extra)
    for key in drop:
        cfg.pop(key)
    fleet.save(run / 'config.json', cfg)
    monkeypatch.setattr(fleet, 'daemon_running', lambda path: False)
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: True)
    monkeypatch.setattr(fleet, 'connect', lambda cfg: object())
    monkeypatch.setattr(fleet, 'LATEST', tmp_path / 'latest.json')
    mgr = SimpleNamespace(spawn_window=lambda *a, **kw: SimpleNamespace(success=True))
    monkeypatch.setattr(fleet, 'manager_for', lambda cfg: mgr)
    return run


def resume_args(**kw):
    return SimpleNamespace(**dict(dict(max_attempts=None, roster=None, advisor=False), **kw))


def test_resume_applies_restart_switches_of_the_baseline_and_prints_each(tmp_path, monkeypatch, capsys):
    write_baseline(ROOT.replace('lease_reclaim: "off"', 'lease_reclaim: "on"') + '        kit_fleet: ops-kit\n')
    run = resumable(tmp_path, monkeypatch)
    assert fleet.resume(run, resume_args())
    cfg = fleet.load(run / 'config.json')
    assert cfg['supervisor_split'] is True and cfg['worker_idle_reclaim_min'] == 30
    assert cfg['max_busy'] == 0 and 'kit_fleet' not in cfg  # roster switch / new-run switch: not touched here
    out = capsys.readouterr().out
    assert 'baseline: supervisor_split False -> True' in out and 'baseline: worker_idle_reclaim_min None -> 30' in out
    assert 'lease_reclaim' not in out  # already as the baseline
    rec = record(run)
    assert sorted(r['switch'] for r in rec['applied']) == ['supervisor_split', 'worker_idle_reclaim_min']
    assert [(r['switch'], r['new_run']) for r in rec['deviations']] == [('kit_fleet', True)]


def test_resume_keeps_a_switch_under_an_exception(tmp_path, monkeypatch, capsys):
    write_baseline(ROOT + exception('worker_idle_reclaim_min', 'off', FUTURE))
    run = resumable(tmp_path, monkeypatch)
    fleet.resume(run, resume_args())
    assert fleet.load(run / 'config.json')['worker_idle_reclaim_min'] is None
    assert 'worker_idle_reclaim_min' not in capsys.readouterr().out
    write_baseline(ROOT + exception('worker_idle_reclaim_min', 'off', PAST))
    fleet.resume(run, resume_args())
    assert fleet.load(run / 'config.json')['worker_idle_reclaim_min'] == 30  # expired: applied again


def test_resume_typed_continue_applied_flag_wins_and_is_reported(tmp_path, monkeypatch, capsys):
    write_baseline(ROOT.replace('lease_reclaim: "off"', 'lease_reclaim: "on"'))
    run = resumable(tmp_path, monkeypatch)
    fleet.resume(run, resume_args(lease_reclaim='off', supervisor_split=False))
    cfg = fleet.load(run / 'config.json')
    assert cfg['lease_reclaim'] == 'off'  # typed and in CONTINUE_APPLIED: applied as today
    assert 'deviates from baseline: lease_reclaim' in capsys.readouterr().out
    assert [r['switch'] for r in record(run)['deviations']] == ['lease_reclaim']


def test_resume_typed_advisor_is_compared_with_the_baseline(tmp_path, monkeypatch, capsys):
    write_baseline(ROOT + '        advisor: on\n')
    run = resumable(tmp_path, monkeypatch)
    fleet.resume(run, resume_args(advisor=True))  # a keeper that passes --advisor on every resume
    assert 'deviates' not in capsys.readouterr().out
    assert [r['switch'] for r in record(run)['deviations']] == []
    write_baseline(ROOT + '        advisor: off\n')
    fleet.resume(run, resume_args(advisor=True))
    assert 'deviates from baseline: advisor' in capsys.readouterr().out


def test_resume_reads_an_absent_reclaim_key_from_the_mode_table(tmp_path, monkeypatch, capsys):
    from fleetmux import lifecycle
    shipped = lifecycle.get_mode('service')['worker_idle_reclaim_min']
    write_baseline(ROOT.replace('worker_idle_reclaim_min: 30', 'worker_idle_reclaim_min: ' + str(shipped)))
    run = resumable(tmp_path, monkeypatch, drop=('worker_idle_reclaim_min',))
    fleet.resume(run, resume_args())
    assert 'worker_idle_reclaim_min' not in fleet.load(run / 'config.json')  # already as the baseline: untouched
    assert 'worker_idle_reclaim_min' not in capsys.readouterr().out
    write_baseline(ROOT.replace('worker_idle_reclaim_min: 30', 'worker_idle_reclaim_min: "off"'))
    fleet.resume(run, resume_args())
    assert fleet.load(run / 'config.json')['worker_idle_reclaim_min'] is None
    assert 'baseline: worker_idle_reclaim_min ' + str(shipped) + ' -> None' in capsys.readouterr().out


def test_resume_without_baseline_entries_is_unchanged(tmp_path, monkeypatch, capsys):
    run = resumable(tmp_path, monkeypatch)
    before = fleet.load(run / 'config.json')
    fleet.resume(run, resume_args())
    after = fleet.load(run / 'config.json')
    for key in ('supervisor_split', 'worker_idle_reclaim_min', 'lease_reclaim', 'max_busy'):
        assert after[key] == before[key]
    out = capsys.readouterr().out
    assert record(run) is None and 'start: baseline' not in out and 'deviates' not in out


def test_resume_with_a_broken_block_is_unchanged_and_says_so(tmp_path, monkeypatch, capsys):
    write_baseline('baseline:\n  version: 1\n  machines: {box-a: [unclosed\n')
    run = resumable(tmp_path, monkeypatch)
    assert fleet.resume(run, resume_args())
    assert fleet.load(run / 'config.json')['supervisor_split'] is False
    assert 'baseline ignored' in capsys.readouterr().out


# -- an attaching start (fleetmux/join.py) -----------------------------------------------------------------------

def attach_args(*extra):
    return fleet.cli().parse_args(['start', '--lifecycle', 'service', *extra])


def test_an_attaching_service_start_takes_the_attached_shape(capsys):
    write_baseline('''baseline:
  version: 1
  machines:
    box-a:
      attached:
        lease_reclaim: "off"
        worker_idle_reclaim_min: 45
        supervisor_split: true
      root_service:
        lease_reclaim: "on"
''')
    overrides, _, _ = join.start_settings(attach_args(), service=True)
    assert overrides['lease_reclaim'] == 'off' and overrides['worker_idle_reclaim_min'] == 45
    assert overrides['supervisor_split'] is True
    overrides, _, _ = join.start_settings(attach_args('--worker-idle-reclaim-min', 'off'), service=True)
    assert overrides['worker_idle_reclaim_min'] is None  # typed wins and is reported
    assert 'deviates from baseline: worker_idle_reclaim_min' in capsys.readouterr().err


def test_an_attaching_start_without_baseline_is_unchanged():
    overrides, _, _ = join.start_settings(attach_args(), service=True)
    assert overrides == dict(join.START_RESET)
