"""The private config file reaches internal processes through config.json, not CMUX_FLEET_CONFIG.

cmux starts the daemon, actors and hooks in fresh shells that need not inherit CMUX_FLEET_CONFIG,
so `start` resolves the private file once (env -> ~/.cmux-fleet/fleet.yaml), records it in config.json
(`private_config`: absolute path, or 'none' when there is no such file) and every entry point reads that."""
import io
import json
from pathlib import Path

import yaml

from cmux_fleet import fleet, fleet_config
from test_advisor import fake_start
from test_path_pinning import pinned

READS = ('accounts', 'lifecycle_modes', 'pi_context')


def private(path, tag):
    path.write_text(yaml.safe_dump({'accounts': [{'id': 'acct-' + tag}],
                                    'lifecycle_modes': {'mode-' + tag: {}},
                                    'pi_context': {'tag': tag}}))
    return path


def reads():
    return {name: fleet_config.block(name) for name in READS}


def want(tag):
    return {'accounts': [{'id': 'acct-' + tag}], 'lifecycle_modes': {'mode-' + tag: {}}, 'pi_context': {'tag': tag}}


def entry_points(tmp_path, monkeypatch, cfg, extra=()):
    """Run daemon / daemon --resume / actor / claude-prompt / pi-event / lifecycle-close on cfg; what each read."""
    pinned(monkeypatch)
    cfg_path = tmp_path / 'config.json'
    cfg_path.write_text(json.dumps(dict(cfg, run=str(tmp_path))))
    seen = {}

    def record(cmd):
        def fn(*a, **k):
            seen[cmd] = reads()
        return fn
    for name in ('daemon', 'actor', 'claude_prompt_main', 'pi_event'):
        monkeypatch.setattr(fleet, name, record(name))
    monkeypatch.setattr(fleet.lifecycle_close, 'close_workspace', record('lifecycle-close'))
    monkeypatch.setattr('sys.stdin', io.StringIO('{}'))
    runs = [['daemon', str(cfg_path)], ['actor', str(cfg_path), 'supervisor', 'p.txt'],
            ['claude-prompt', str(cfg_path), 'supervisor', '0'], ['pi-event', str(cfg_path), 'supervisor', '0'],
            ['lifecycle-close', str(cfg_path)]]
    for argv in runs:
        fleet_config.pin(None)  # each entry point is a fresh process
        fleet.main(argv)
    fleet_config.pin(None)
    monkeypatch.setattr(fleet, 'daemon', lambda cfg, resume: seen.__setitem__('daemon --resume', reads()))
    fleet.main(['daemon', str(cfg_path), '--resume'])
    return seen


def test_internal_processes_read_the_recorded_private_file_not_the_environment(tmp_path, monkeypatch):
    recorded = private(tmp_path / 'recorded.yaml', 'recorded')
    monkeypatch.setenv(fleet_config.USER_ENV, str(private(tmp_path / 'env.yaml', 'env')))
    seen = entry_points(tmp_path, monkeypatch, {'private_config': str(recorded)})
    assert set(seen) == {'daemon', 'actor', 'claude_prompt_main', 'pi_event', 'lifecycle-close', 'daemon --resume'}
    assert all(got == want('recorded') for got in seen.values()), seen


def test_recorded_none_means_no_private_file_even_when_the_environment_has_one(tmp_path, monkeypatch):
    monkeypatch.setenv(fleet_config.USER_ENV, str(private(tmp_path / 'env.yaml', 'env')))
    seen = entry_points(tmp_path, monkeypatch, {'private_config': 'none'})
    fleet_config.pin(None)
    monkeypatch.setenv(fleet_config.USER_ENV, str(tmp_path / 'missing.yaml'))
    shipped = reads()
    assert shipped != want('env')
    assert all(got == shipped for got in seen.values()), seen


def test_old_config_without_the_key_keeps_reading_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(fleet_config.USER_ENV, str(private(tmp_path / 'env.yaml', 'env')))
    seen = entry_points(tmp_path, monkeypatch, {})
    assert seen and all(got == want('env') for got in seen.values()), seen


def test_start_records_the_resolved_private_file(tmp_path, monkeypatch):
    argv = fake_start(monkeypatch, tmp_path)
    (tmp_path / 'sub').mkdir()
    (tmp_path / 'env.yaml').write_text('accounts: []\n')  # start validates lifecycle_modes: keep the shipped one
    monkeypatch.setenv(fleet_config.USER_ENV, str(tmp_path / 'sub' / '..' / 'env.yaml'))  # recorded resolved
    fleet.main(argv)
    cfg = fleet.load(fleet.load(tmp_path / 'latest.json')['config'])
    assert cfg['private_config'] == str((tmp_path / 'env.yaml').resolve())
    monkeypatch.setenv(fleet_config.USER_ENV, str(tmp_path / 'missing.yaml'))
    fleet_config.pin(None)
    fleet.main(argv)
    assert fleet.load(fleet.load(tmp_path / 'latest.json')['config'])['private_config'] == 'none'


def test_resume_keeps_the_recorded_file_over_the_callers_environment(tmp_path, monkeypatch):
    pinned(monkeypatch)
    recorded = private(tmp_path / 'recorded.yaml', 'recorded')
    monkeypatch.setenv(fleet_config.USER_ENV, str(private(tmp_path / 'env.yaml', 'env')))
    run = tmp_path / 'run'
    run.mkdir()
    (run / 'config.json').write_text(json.dumps({'run': str(run), 'private_config': str(recorded)}))
    seen = {}

    def stop(cfg):  # resume() has pinned the run's locations by the time it validates the config
        seen.update(reads())
        raise ValueError('stop here')
    monkeypatch.setattr(fleet.runtime_v2, 'validate_config', stop)
    try:
        fleet.resume(run, None)
    except SystemExit:
        pass
    assert seen == want('recorded')
