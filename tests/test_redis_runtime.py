"""Private Redis path/provisioning regressions. No model, cmux or live fleet."""
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace

import pytest
import redis

from cmux_fleet import control_view, fleet, redis_runtime
from test_pi import mock_start


@pytest.fixture
def short_root():
    # pytest's long per-test path can exceed macOS sockaddr_un even in mocked starts.
    with tempfile.TemporaryDirectory(prefix='fx-r-') as directory:
        yield Path(directory).resolve() / 'redis'


def test_default_is_checkout_local_not_cwd_or_tmp(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    root = Path(redis_runtime.__file__).resolve().parents[1] / '.runtime' / 'redis'
    assert redis_runtime.DEFAULT_ROOT == root
    cfg = redis_runtime.paths(root, 'abcdef12')
    assert cfg['redis_runtime_dir'] == str(root / 'abcdef12')
    assert cfg['redis_socket'] == str(root / 'abcdef12' / 'redis.sock')
    assert not str(cfg['redis_socket']).startswith('/tmp/')
    assert not (root / 'abcdef12').exists()  # paths() has no side effects


def test_prepare_reserves_unique_owner_only_instance(short_root):
    a = redis_runtime.prepare(short_root, 'abcdef12')
    b = redis_runtime.prepare(short_root, 'abcdef34')
    for cfg in (a, b):
        for field in ('redis_runtime_root', 'redis_runtime_dir'):
            path = Path(cfg[field])
            assert stat.S_IMODE(path.stat().st_mode) == 0o700
            assert path.stat().st_uid == os.getuid()
        assert redis_runtime.validate_socket(cfg) == Path(cfg['redis_socket'])
    assert a['redis_socket'] != b['redis_socket']
    sentinel = Path(a['redis_runtime_dir']) / 'fleet.rdb'
    sentinel.write_text('keep the existing run')
    with pytest.raises(FileExistsError):
        redis_runtime.prepare(short_root, 'abcdef12')
    assert sentinel.read_text() == 'keep the existing run'


@pytest.mark.parametrize('instance_id', ['', '../abc12345', 'ABCDEF12', '1', 'zzzzzzzz', None])
def test_invalid_instance_is_rejected_before_mkdir(short_root, instance_id):
    with pytest.raises(ValueError, match='instance id'):
        redis_runtime.prepare(short_root, instance_id)
    assert not short_root.exists()


@pytest.mark.parametrize('field,value', [
    ('redis_socket', 'localhost:6379'),
    ('redis_socket', '/tmp/cmux-fleet-legacy.sock'),
    ('redis_socket', '/var/run/redis.sock'),
    ('redis_socket', 'redis.sock'),
    ('redis_runtime_root', None),
    ('redis_runtime_dir', None),
    ('redis_instance_id', None),
    ('redis_instance_id', '12345678'),
])
def test_managed_config_cannot_downgrade_or_redirect(short_root, field, value):
    cfg = redis_runtime.paths(short_root, 'abcdef12')
    cfg[field] = value
    with pytest.raises(ValueError, match='private Redis'):
        redis_runtime.validate_socket(cfg)
    with pytest.raises(ValueError, match='private Redis'):
        control_view._settings(dict(cfg, run='run-test', workers=['worker-1']))


def test_neighbor_run_socket_is_rejected(short_root):
    a = redis_runtime.paths(short_root, 'abcdef12')
    b = redis_runtime.paths(short_root, 'abcdef34')
    with pytest.raises(ValueError, match='instance directory'):
        redis_runtime.validate_socket(dict(a, redis_socket=b['redis_socket']))


def test_parent_traversal_is_rejected(short_root):
    cfg = redis_runtime.paths(short_root, 'abcdef12')
    cfg['redis_socket'] = str(short_root) + '/abcdef12/../abcdef34/redis.sock'
    with pytest.raises(ValueError, match='traversal'):
        redis_runtime.validate_socket(cfg)


def test_symlink_escape_is_rejected(short_root):
    cfg = redis_runtime.prepare(short_root, 'abcdef12')
    outside = short_root.parent / 'outside'
    outside.mkdir()
    socket = Path(cfg['redis_socket'])
    socket.symlink_to(outside / 'redis.sock')
    with pytest.raises(ValueError, match='symlink'):
        redis_runtime.validate_socket(cfg)
    socket.unlink()
    directory = Path(cfg['redis_runtime_dir'])
    directory.rmdir()
    directory.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        redis_runtime.validate_socket(cfg)
    link = short_root.parent / 'linked-root'
    link.symlink_to(short_root, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        redis_runtime.prepare(link, 'abcdef34')


def test_existing_shared_root_is_refused_without_chmod(short_root):
    short_root.mkdir()
    short_root.chmod(0o755)
    with pytest.raises(ValueError, match='owner-only'):
        redis_runtime.prepare(short_root, 'abcdef12')
    assert stat.S_IMODE(short_root.stat().st_mode) == 0o755
    assert not (short_root / 'abcdef12').exists()


def test_wrong_directory_owner_is_refused(short_root, monkeypatch):
    short_root.mkdir(mode=0o700)
    monkeypatch.setattr(redis_runtime.os, 'getuid', lambda: -1)
    with pytest.raises(ValueError, match='current user'):
        redis_runtime.prepare(short_root, 'abcdef12')
    assert not (short_root / 'abcdef12').exists()


def test_socket_limit_is_utf8_bytes_and_checked_before_creation(short_root, monkeypatch):
    root = short_root / ('长' * 10)
    size = len(os.fsencode(root / 'abcdef12' / 'redis.sock'))
    assert size > len(str(root / 'abcdef12' / 'redis.sock'))
    monkeypatch.setattr(redis_runtime, 'SOCKET_PATH_LIMIT', size - 1)
    with pytest.raises(ValueError, match='too long.*no /tmp fallback'):
        redis_runtime.prepare(root, 'abcdef12')
    assert not short_root.exists()
    monkeypatch.setattr(redis_runtime, 'SOCKET_PATH_LIMIT', size)
    assert redis_runtime.paths(root, 'abcdef12')['redis_socket'].endswith('redis.sock')


@pytest.mark.parametrize('socket', [
    '/tmp/redis.sock', '/tmp/cmux-fleet-x/../redis.sock',
    '/tmp/cmux-fleet-x.sock/other', '/tmp/cmux-fleet-.sock', '127.0.0.1:6379',
])
def test_unmanaged_external_socket_is_rejected(socket):
    with pytest.raises(ValueError, match='private'):
        fleet.connect({'redis_socket': socket})


def test_legacy_config_remains_explicitly_compatible(monkeypatch):
    socket = '/tmp/cmux-fleet-old-run.sock'
    calls = []
    client = SimpleNamespace(ping=lambda: calls.append('ping'))
    monkeypatch.setattr(redis, 'Redis', lambda **kwargs: calls.append(kwargs) or client)
    assert fleet.connect({'redis_socket': socket}) is client
    assert calls[0]['unix_socket_path'] == socket and calls[0]['db'] == 0
    assert 'host' not in calls[0] and 'port' not in calls[0]
    assert calls[1] == 'ping'


def test_fleet_and_control_use_identical_managed_endpoint(short_root, monkeypatch):
    cfg = dict(redis_runtime.paths(short_root, 'abcdef12'), run='run-test', workers=['worker-1'])
    calls = []
    client = SimpleNamespace(ping=lambda: True)
    monkeypatch.setattr(redis, 'Redis', lambda **kwargs: calls.append(kwargs) or client)
    assert fleet.connect(cfg) is client
    assert calls[0]['unix_socket_path'] == control_view._settings(cfg).socket
    assert 'host' not in calls[0] and 'port' not in calls[0]


def test_unreachable_managed_socket_does_not_fallback(short_root, monkeypatch):
    cfg = redis_runtime.paths(short_root, 'abcdef12')
    calls = []
    def fail():
        raise redis.ConnectionError('private socket unavailable')
    monkeypatch.setattr(redis, 'Redis', lambda **kwargs: calls.append(kwargs) or SimpleNamespace(ping=fail))
    with pytest.raises(redis.ConnectionError):
        fleet.connect(cfg)
    assert len(calls) == 1 and calls[0]['unix_socket_path'] == cfg['redis_socket']


@pytest.mark.parametrize('transport', [2])
def test_start_pins_project_redis_paths_independently_of_run(tmp_path, monkeypatch, short_root, transport):
    args, ran = mock_start(tmp_path, monkeypatch)
    # Restore real directory provisioning; fake subprocess/cmux means no fleet starts.
    monkeypatch.setattr(redis_runtime, 'prepare', prepare_real)
    args += ['--redis-runtime-dir', str(short_root), '--transport-version', str(transport)]
    if transport == 2:
        args.append('--allow-development-v2')
    fleet.main(args)
    cfg = fleet.load(tmp_path / 'latest.json')
    runtime = Path(cfg['redis_runtime_dir'])
    assert runtime.parent == short_root
    assert cfg['redis_instance_id'] == Path(cfg['run']).name.rsplit('-', 1)[1]
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o700
    command = next(command for command in ran if '--unixsocket' in command)
    def flag(name):
        return command[command.index(name) + 1]
    assert flag('--unixsocket') == str(runtime / 'redis.sock')
    assert flag('--dir') == str(runtime)
    assert flag('--pidfile') == str(runtime / 'redis.pid')
    assert flag('--logfile') == str(runtime / 'redis.log')
    assert flag('--dbfilename') == 'fleet.rdb'
    assert flag('--port') == '0' and flag('--unixsocketperm') == '700'
    assert flag('--appendonly') == ('yes' if transport == 2 else 'no')
    snapshot = fleet.load(Path(cfg['run']) / 'config.json')
    for field in (*redis_runtime.MANAGED_FIELDS, 'redis_socket'):
        assert snapshot[field] == cfg[field]
    assert Path(cfg['run']) != runtime


def test_start_refuses_long_root_before_redis_spawn(tmp_path, monkeypatch, short_root):
    args, ran = mock_start(tmp_path, monkeypatch)
    monkeypatch.setattr(redis_runtime, 'prepare', prepare_real)
    long_root = short_root / ('x' * 120)
    with pytest.raises(SystemExit, match='too long'):
        fleet.main(args + ['--redis-runtime-dir', str(long_root)])
    assert not short_root.exists()
    assert not any('--unixsocket' in command for command in ran)
    assert not (tmp_path / 'latest.json').exists()


# Capture before mock_start replaces prepare() in each test.
prepare_real = redis_runtime.prepare
