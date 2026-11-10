import json
from pathlib import Path
import shlex
import subprocess

from cmux_fleet import transport_inventory as inventory


def prepare(monkeypatch, tmp_path, version=1):
    home = tmp_path/'home'
    run = home/'.cmux-fleet-test/runs/fleet-fixture'
    run.mkdir(parents=True)
    cfg = run/'config.json'
    data = {'run': str(run), 'redis_socket': str(run/'private.sock'), 'workers': ['worker-1'],
            'supervisor_cli': 'pi', 'worker_cli': 'pi', 'api_key': 'SYNTHETIC_SECRET_MUST_NOT_BE_PRINTED'}
    if version is not None:
        data['transport_version'] = version
    cfg.write_text(json.dumps(data))
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    glob = Path.glob
    monkeypatch.setattr(Path, 'glob', lambda self, pattern: iter(()) if self == Path('/tmp') else glob(self, pattern))
    monkeypatch.setattr(inventory.subprocess, 'run', lambda args, **kw:
        subprocess.CompletedProcess(args, 0, '123 1 '+shlex.join(['python3','/project/cmux_fleet/fleet.py','daemon',str(cfg)])+'\n', ''))
    return home, run, cfg


def test_inventory_reports_transport_and_live_config_without_secrets(monkeypatch, tmp_path):
    _, run, cfg = prepare(monkeypatch, tmp_path, 2)
    report = inventory.inventory()
    item, = report['instances']
    assert item['transport_version'] == 2 and item['observed_running']
    assert item['configs'] == [str(cfg)] and item['run'] == str(run)
    assert item['v2_cli_supported']
    assert 'SYNTHETIC_SECRET_MUST_NOT_BE_PRINTED' not in json.dumps(report)
    assert report['coverage']['redis_connected'] is False


def test_inventory_counts_messages_without_reading_them(monkeypatch, tmp_path):
    _, run, _ = prepare(monkeypatch, tmp_path)
    path = run/'agents/supervisor/.inbox/msg-test.md'
    path.parent.mkdir(parents=True)
    path.write_text('BUSINESS_CONTENT_MUST_NOT_BE_READ')
    read = Path.read_text
    def guarded(self, *args, **kwargs):
        assert self.suffix != '.md', 'inventory opened message contents'
        return read(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'read_text', guarded)
    report = inventory.inventory()
    assert report['instances'][0]['message_file_count'] == 1
    assert 'BUSINESS_CONTENT_MUST_NOT_BE_READ' not in json.dumps(report)


def test_missing_transport_is_legacy_and_registry_is_not_liveness(monkeypatch, tmp_path):
    home, _, _ = prepare(monkeypatch, tmp_path, None)
    registry = home/'.cmux-fleet-test/state/cmux-fleet.json'
    registry.parent.mkdir(parents=True)
    registry.write_text(json.dumps({'workspace': 'old-workspace', 'surfaces': {'old': {}}}))
    monkeypatch.setattr(inventory.subprocess, 'run', lambda args, **kw: subprocess.CompletedProcess(args, 0, '', ''))
    report = inventory.inventory()
    assert report['instances'][0]['transport_version'] == 1
    assert report['instances'][0]['transport_explicit'] is False
    assert report['instances'][0]['observed_running'] is False
    assert report['registries'][0]['note'] == 'registry alone does not establish liveness'


def test_inventory_reports_unreadable_configs_and_process_coverage(monkeypatch, tmp_path):
    _, _, cfg = prepare(monkeypatch, tmp_path)
    cfg.write_text('{invalid')
    monkeypatch.setattr(inventory.subprocess, 'run', lambda args, **kw: subprocess.CompletedProcess(args, 1, '', 'not permitted'))
    report = inventory.inventory()
    assert report['coverage']['process_scan'] is False
    assert not report['instances']
    assert len(report['warnings']) == 2
