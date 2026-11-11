"""Per-account channel lease (package P1b; docs/design.md §53, docs/contracts.md §8).

One (type, account) is driven by at most one fleet on this machine: an flock on
``channels.lease_path(type, account)`` (under ``config_dir()/leases/``, never under a run), holding a JSON
holder record ``{run_id, run, pid, host, at}``. A lock whose pid is dead and whose flock is free is reclaimed
(audit ``channel.lease_reclaimed``). The kernel drops the flock when the holding process exits.

flock is per open file description: a second ``acquire`` of the same account in this process is refused like
one from another fleet. The lock file is never deleted (deleting a locked path lets two holders lock two
different inodes); a clean ``release()`` empties it, so ``holder()`` is None until the next ``acquire``.
"""
import fcntl
import json
import os
import socket
import time

from fleetmux import channels

FIELDS = ('run_id', 'run', 'pid', 'host', 'at')


class Lease:
    """A held lease. ``release()`` drops it; calling it twice is harmless."""

    def __init__(self, type_, account, path=None, fd=None):
        self.type, self.account, self.path, self.fd = type_, account, path, fd

    def release(self):
        fd, self.fd = self.fd, None
        if fd is None:
            return
        try:
            os.ftruncate(fd, 0)  # still under the lock: nobody reads a half-cleared record
        except OSError:
            pass
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)

    def __repr__(self):
        return 'Lease(%s:%s, %s)' % (self.type, self.account, 'held' if self.fd is not None else 'released')


def _alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # exists, owned by someone else
        return True
    except OSError:
        return False
    return True


def _parse(raw):
    try:
        record = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    return record if isinstance(record, dict) and record else None


def _read_fd(fd):
    chunks = []
    offset = 0
    while True:
        chunk = os.pread(fd, 65536, offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
    return b''.join(chunks)


def _leases_dir(path):
    d = path.parent
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(d, 0o700)  # mkdir's mode is masked by umask, and an existing dir may be wider


def acquire(type_, account, *, run_id, run, audit=None):
    """A Lease, or None when another live process holds it. ``audit(event, **fields)`` gets
    ``channel.lease_reclaimed`` when a dead holder's lock is taken over."""
    path = channels.lease_path(type_, account)  # validates type / account
    _leases_dir(path)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        os.fchmod(fd, 0o600)
        previous = _parse(_read_fd(fd))
        record = {'run_id': run_id, 'run': str(run), 'pid': os.getpid(), 'host': socket.gethostname(),
                  'at': time.time()}
        data = json.dumps(record, ensure_ascii=False).encode('utf-8')
        os.ftruncate(fd, 0)
        os.pwrite(fd, data, 0)
        os.fsync(fd)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    held = Lease(type_, account, path=path, fd=fd)
    # The flock was free, so whoever wrote ``previous`` no longer holds it; a record left behind by a process
    # that is gone (killed, crashed: no clean release) is a reclaim worth an audit row.
    if previous is not None and previous.get('pid') != os.getpid() and not _alive(previous.get('pid')):
        if audit is not None:
            try:
                audit('channel.lease_reclaimed', channel=type_, account=account, run_id=run_id,
                      previous={k: previous.get(k) for k in FIELDS})
            except Exception:  # an audit sink failure never costs the lease
                pass
    return held


def holder(type_, account):
    """The holder record ``{run_id, run, pid, host, at}`` of the lock file, or None."""
    path = channels.lease_path(type_, account)
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None
    record = _parse(raw)
    if record is None:
        return None
    return {k: record.get(k) for k in FIELDS}


def acquire_all(entries, *, run_id, run, audit=None, status=None):
    """``{(type, account): Lease or None}`` for the roster ``channels:`` entries. A held one is None, audited once
    as ``channel.lease_busy`` (with the holder's run_id) and reported through ``status(type_, account, **fields)``
    with ``state='lease_busy'``; the others are unaffected."""
    got = {}
    for entry in entries:
        key = (entry['type'], entry['account'])
        if key in got:
            continue
        type_, account = key
        try:
            held = acquire(type_, account, run_id=run_id, run=run, audit=audit)
        except OSError as exc:  # one account's broken lock file stops that account only
            got[key] = None
            if audit is not None:
                try:
                    audit('channel.error', channel=type_, account=account, error='lease: ' + str(exc))
                except Exception:
                    pass
            continue
        got[key] = held
        if held is not None:
            continue
        who = holder(type_, account) or {k: None for k in FIELDS}
        if audit is not None:
            try:
                audit('channel.lease_busy', channel=type_, account=account, holder=who)
            except Exception:
                pass
        if status is not None:
            try:
                status(type_, account, state='lease_busy', holder=who.get('run_id') or '')
            except Exception:
                pass
    return got
