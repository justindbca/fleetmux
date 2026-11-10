"""Opt-in project-local Redis smoke; no agents, daemon, cmux or latest.json.

python3 tests/redis_runtime_smoke.py
Only the freshly reserved instance is started/stopped. Logs and data remain in
.runtime/redis/<token>/ for diagnosis, excluded from Git locally.
"""
import argparse
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cmux_fleet import control_view, fleet, redis_runtime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--redis-server', default=shutil.which('redis-server'))
    parser.add_argument('--root', default=str(redis_runtime.DEFAULT_ROOT))
    args = parser.parse_args()
    if not args.redis_server:
        parser.error('redis-server is required')
    cfg = redis_runtime.prepare(args.root, uuid.uuid4().hex[:8])
    directory = Path(cfg['redis_runtime_dir'])
    config = directory / 'smoke-config.json'
    config.write_text(json.dumps(cfg, indent=2) + '\n')
    argv = [args.redis_server, '--port', '0', '--unixsocket', cfg['redis_socket'],
            '--unixsocketperm', '700', '--dir', str(directory), '--dbfilename', 'fleet.rdb',
            '--appendonly', 'yes', '--appendfsync', 'everysec', '--save', '',
            '--daemonize', 'no', '--pidfile', str(directory / 'redis.pid'),
            '--logfile', str(directory / 'redis.log')]
    process = client = None
    try:
        for cycle in (1, 2):
            process = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 10
            while not Path(cfg['redis_socket']).exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    log = directory / 'redis.log'
                    raise RuntimeError('private Redis did not start: ' +
                                       (log.read_text()[-3000:] if log.exists() else 'no log'))
                time.sleep(.02)
            client = fleet.connect(cfg)
            assert client.info('server')['process_id'] == process.pid
            assert client.config_get('port')['port'] == '0'
            assert stat.S_ISSOCK(Path(cfg['redis_socket']).stat().st_mode)
            assert stat.S_IMODE(Path(cfg['redis_socket']).stat().st_mode) == 0o700
            assert control_view._settings(dict(cfg, run='smoke', workers=[])).socket == cfg['redis_socket']
            if cycle == 1:
                client.set('runtime:smoke', 'project-local')
                child = subprocess.run([sys.executable, '-c',
                    'from cmux_fleet import fleet; import sys; '
                    'r=fleet.connect(fleet.load(sys.argv[1])); '
                    'assert r.get("runtime:smoke")=="project-local"; '
                    'print("PONG"); r.close(); r.connection_pool.disconnect()', str(config)],
                    cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
                    check=True, timeout=15)
                assert child.stdout.strip() == 'PONG'
                client.save()
                assert (directory / 'fleet.rdb').is_file()
                assert (directory / 'redis.pid').is_file()
                assert (directory / 'appendonlydir').is_dir()
                print('PASS project-local socket, TCP disabled, mode 700, cross-process PONG', flush=True)
            else:
                assert client.get('runtime:smoke') == 'project-local'
                print('PASS same isolated child runtime restart recovers persisted data', flush=True)
            client.shutdown()
            process.wait(timeout=10)
            client.close()
            client.connection_pool.disconnect()
            client = None
            assert not Path(cfg['redis_socket']).exists()
            assert not (directory / 'redis.pid').exists()
        print('PASS private Redis stopped; existing fleets untouched', flush=True)
        print('Runtime directory:', directory)
        print('Retained files:', ', '.join(p.name for p in sorted(directory.iterdir())))
    finally:
        if client is not None:
            client.close()
            client.connection_pool.disconnect()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()  # Only the child created above, never a PID read from config.
                process.wait(timeout=5)


if __name__ == '__main__':
    main()
