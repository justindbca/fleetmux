"""Stop the private test Redis a fixture started, and only that one.

``fleet.start_redis`` daemonises Redis, so it is never a child of pytest: a missed stop leaves a process with
parent pid 1 for good (the product's orphan sweep skips instances without ``owner.json``, and the suite turns the
sweep off). The fixtures (``start_tree`` in tests/test_subfleet.py, ``managed`` in tests/test_resume.py) call
``stop_test_redis`` from a ``finally`` with their own unique root: it acts only on the ``<root>/<id>/redis.pid``
files under that root, checks that the pid is still a Redis serving a socket under that root, kills it without a
save (a test keeps no data), waits until it is gone and only then removes the root. A Redis that survives makes
the test fail instead of leaving a silent orphan.
"""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time

import pytest

PID_WAIT = 2.0   # seconds a live socket may wait for its pid file (a Redis that never answered)
STOP_WAIT = 5.0  # seconds a killed Redis has to be gone


def command_of(pid):
    """The command line of one pid (``ps -p <pid>``), '' when there is no such process."""
    out = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True)
    return out.stdout.strip()


def gone(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:  # the pid exists (reused by a process of another user)
        return False
    return False


def read_pid(instance):
    """The pid in ``<instance>/redis.pid``; waits up to PID_WAIT when the socket is there but the file is not."""
    pid_file, sock = instance / 'redis.pid', instance / 'redis.sock'
    deadline = time.monotonic() + (PID_WAIT if sock.exists() else 0)
    while True:
        try:
            return int(pid_file.read_text().strip())
        except (FileNotFoundError, ValueError):
            if time.monotonic() >= deadline:
                return None
            time.sleep(.05)


def own_pids(root):
    """Pids of the Redis instances under this root that are still running: (pids, instances without a pid)."""
    root = Path(root)
    if not root.is_dir():
        return [], []
    names = {str(root), str(root.resolve())}  # /tmp is /private/tmp on macOS; redis_runtime resolves the root
    pids, unknown = [], []
    for instance in sorted(p for p in root.iterdir() if p.is_dir()):
        pid = read_pid(instance)
        if pid is None:
            if (instance / 'redis.sock').exists():
                unknown.append(str(instance))
            continue  # a clean shutdown removes the pid file and the socket
        command = command_of(pid)
        if 'redis-server' in command and any(name + '/' in command for name in names):
            pids.append(pid)
    return pids, unknown


def stop_test_redis(root):
    """Kill every Redis this fixture started under ``root``, wait until each is gone, then remove ``root``."""
    root = Path(root)
    pids, unknown = own_pids(root)
    survivors = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            survivors.append(pid)
    deadline = time.monotonic() + STOP_WAIT
    waiting = [p for p in pids if p not in survivors]
    while waiting and time.monotonic() < deadline:
        waiting = [p for p in waiting if not gone(p)]
        if waiting:
            time.sleep(.05)
    survivors += waiting
    if survivors or unknown:
        pytest.fail('test Redis survived teardown under %s: pids %s, live sockets without a pid file %s'
                    % (root, survivors, unknown))
    shutil.rmtree(root, ignore_errors=True)
