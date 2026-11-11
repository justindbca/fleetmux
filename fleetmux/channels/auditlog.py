"""Append-only audit chain (package P8g; brief v2.2 §8.2 / §8.4; contracts §8 「审计链（P8g）」; sec-2 survey §9 / §10.1).

P8g module part (supervisor M101 / M113): ``canonical``, ``verify``, ``Chain`` and ``mirror`` are implemented.
``daemon_pump`` (M134) is the daemon's single-writer driver, one stage of ``Fleet.security_tick``. Off branches: no policy or
``audit_log.enabled: off`` -> 0 without building a Scanner or a Chain, reading credentials, creating a directory or
lock, or touching Redis. A Chain creates nothing until its first ``append`` / ``acquire``.

Interfaces (sec-2 test-plan §0):
- ``Chain(dir, *, clock=time.time, uappend=True, chflags=default_chflags(), audit=None)``: files
  ``chain-YYYYMMDD.jsonl``, opened ``O_WRONLY|O_APPEND|O_CREAT`` 0600, lines ``{seq, prev, record, hash}``; uappend
  (UF_APPEND) only on chain files after creating them, never on ``.writer.lock``; unsupported / failing -> one
  ``security.audit_chain reason=uappend_unsupported`` per Chain instance. ``append(record)``; ``acquire() -> bool``
  (flock single writer); ``close()`` releases
  the lock fd.
- ``canonical(record) -> str``; ``verify(path) -> (ok, first_bad_seq)`` (tamper-evident, not tamper-proof).
- ``mirror(r, run, policy, *, scanner, chain, count=500) -> int``: one bounded non-blocking batch of the run's v2 audit
  stream into the chain; cursor = hash ``fx:channel:auditlog`` field ``last_id``.
- ``daemon_pump(f) -> int``: the daemon's single-writer driver (one line in ``Fleet.security_tick``).
"""
import fcntl
import fnmatch
import hashlib
import json
import os
import stat
import time
from pathlib import Path

from fleetmux import channels
from fleetmux.channels import security
from fleetmux.message_bus import P as _BUS_PREFIX

FILE_PREFIX = 'chain-'
LOCK_NAME = '.writer.lock'


def _off(policy):
    return policy is None or (policy.get('audit_log') or {}).get('enabled') == 'off'


def canonical(record):
    """The hashed form of a record: sorted keys, no spaces, UTF-8 kept."""
    return json.dumps(record, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def _hash(prev, record):
    return hashlib.sha256((prev + canonical(record)).encode('utf-8')).hexdigest()


def _row(raw):
    """A parsed chain line, or None when it is not one."""
    try:
        row = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if (not isinstance(row, dict) or set(row) != {'seq', 'prev', 'record', 'hash'} or type(row['seq']) is not int
            or not isinstance(row['prev'], str) or not isinstance(row['hash'], str)):
        return None
    return row


def verify(path):
    """``(True, None)`` when every line of this chain file is whole and links to the one before it, else
    ``(False, seq)`` of the first bad line (an unparsable line counts as the seq that should have been there). The
    first line's ``prev`` is taken as given (it continues the previous day's file). Tamper-evident only: a rewrite of
    the whole chain from a point on, or a cut tail, is not detected here."""
    data = Path(path).read_bytes()
    last_seq, last_hash = None, None
    for raw in data.split(b'\n'):
        if not raw.strip():
            continue
        row = _row(raw.decode('utf-8', 'replace'))
        expected = (last_seq + 1) if last_seq is not None else None
        if row is None:
            return False, (expected if expected is not None else 1)
        if last_seq is not None and (row['seq'] != expected or row['prev'] != last_hash):
            return False, row['seq']
        if row['hash'] != _hash(row['prev'], row['record']):
            return False, row['seq']
        last_seq, last_hash = row['seq'], row['hash']
    return True, None


def verify_dir(d):
    """Every ``chain-*.jsonl`` under ``d`` in day order -> ``[{file, lines, first_seq, last_seq, ok, first_bad_seq}]``
    (no record content). Each file through ``verify``; across files the first line's ``seq`` / ``prev`` must continue
    the previous file's last line (a removed day file is caught at the next file's first seq)."""
    out, last_seq, last_hash = [], None, None
    for path in sorted(Path(d).glob(FILE_PREFIX + '*.jsonl')):
        ok, bad = verify(path)
        rows = []
        for raw in path.read_bytes().split(b'\n'):
            if raw.strip():
                rows.append(_row(raw.decode('utf-8', 'replace')))
        good = [x for x in rows if x is not None]
        if ok and good and last_seq is not None and (good[0]['seq'] != last_seq + 1 or good[0]['prev'] != last_hash):
            ok, bad = False, good[0]['seq']
        out.append({'file': path.name, 'lines': len(rows), 'first_seq': good[0]['seq'] if good else None,
                    'last_seq': good[-1]['seq'] if good else None, 'ok': ok, 'first_bad_seq': bad})
        if good:
            last_seq, last_hash = good[-1]['seq'], good[-1]['hash']
    return out


_DEFAULT = object()


def default_chflags():
    """The ``chflags`` a Chain uses when none is passed, looked up at construction time (``os.chflags``, or None
    where it does not exist). One seam: the test suite stubs it so pytest temp dirs never get ``uappnd``."""
    return getattr(os, 'chflags', None)


class Chain:
    """One writer's view of ``<dir>/chain-YYYYMMDD.jsonl`` (day by the injected clock, local time). Each line is
    ``{seq, prev, record, hash}`` with ``hash = sha256(prev + canonical(record))``; ``prev`` continues across days
    and across instances (a new instance resumes from the last whole line on disk, never truncates; a torn last line
    is closed with a newline first, so verify reports it). Files are opened ``O_WRONLY|O_APPEND|O_CREAT`` mode 0600;
    a newly created chain file gets ``UF_APPEND`` (``chflags``) when ``uappend`` -- never the lock or the directory.
    No ``chflags`` (Linux) or a failing one -> writes go on, one ``security.audit_chain reason=uappend_unsupported``
    audit per instance. ``acquire()`` = flock on ``<dir>/.writer.lock`` (single writer, non-blocking)."""

    def __init__(self, dir, *, clock=time.time, uappend=True, chflags=_DEFAULT, audit=None):
        if chflags is _DEFAULT:
            chflags = default_chflags()
        self.dir, self.clock, self.uappend, self.chflags, self.audit = dir, clock, uappend, chflags, audit
        self._path = Path(dir)
        self._fd, self._fd_name, self._lock_fd = None, None, None
        self._seq, self._prev, self._loaded = 0, '', False
        self.last_record = None  # the record of the last whole line (on disk or written by this instance)
        self._unsupported_said = False

    def __repr__(self):
        return 'Chain(' + str(self._path) + ', seq=' + str(self._seq) + ')'

    # -------------------------------------------------------------- state
    def _load(self):
        if self._loaded:
            return
        self._loaded = True
        for path in sorted(self._path.glob(FILE_PREFIX + '*.jsonl'), reverse=True):
            try:
                parts = path.read_bytes().split(b'\n')
            except OSError:
                continue
            for raw in reversed(parts[:-1]):  # parts[-1] is '' or a torn tail
                row = _row(raw.decode('utf-8', 'replace')) if raw.strip() else None
                if row is not None:
                    self._seq, self._prev = row['seq'], row['hash']
                    self.last_record = row['record']
                    return

    def _name(self):
        return FILE_PREFIX + time.strftime('%Y%m%d', time.localtime(self.clock())) + '.jsonl'

    def _say_unsupported(self):
        if self._unsupported_said:
            return
        self._unsupported_said = True
        if self.audit is not None:
            try:
                self.audit('security.audit_chain', reason='uappend_unsupported')
            except Exception:  # noqa: BLE001 - an audit sink failure must not stop the chain
                pass

    def _open(self):
        name = self._name()
        if self._fd is not None and self._fd_name == name:
            return self._fd
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        self._path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._path / name
        existed = path.exists()
        torn = False
        if existed and path.stat().st_size:
            with open(path, 'rb') as fh:
                fh.seek(-1, os.SEEK_END)
                torn = fh.read(1) != b'\n'
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        if not existed:
            os.fchmod(fd, 0o600)
            if self.uappend:
                if self.chflags is None:
                    self._say_unsupported()
                else:
                    try:
                        self.chflags(str(path), stat.UF_APPEND)
                    except (OSError, NotImplementedError, AttributeError):
                        self._say_unsupported()
        if torn:
            os.write(fd, b'\n')
        self._fd, self._fd_name = fd, name
        return fd

    # -------------------------------------------------------------- api
    def append(self, record):
        """Write one record as the next chain line; returns its seq."""
        self._load()
        prev, seq = self._prev, self._seq + 1
        line = {'seq': seq, 'prev': prev, 'record': record, 'hash': _hash(prev, record)}
        data = (json.dumps(line, ensure_ascii=False) + '\n').encode('utf-8')
        os.write(self._open(), data)
        self._seq, self._prev = seq, line['hash']
        self.last_record = record
        return seq

    def acquire(self):
        """Take the single-writer lock (non-blocking). True when this instance holds it."""
        if self._lock_fd is not None:
            return True
        self._path.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(self._path / LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._lock_fd = fd
        return True

    def release(self):
        """Release the writer lock only (the chain file stays open)."""
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self._lock_fd)
                self._lock_fd = None

    def close(self):
        """Close the chain file and release the writer lock."""
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self._lock_fd)
                self._lock_fd = None


STREAM_ID = 'stream_id'  # the v2 audit stream entry id, kept in each chain record (dedupe after a crash)


def _stream_key(run):
    """The run's v2 audit stream, as runtime_v2.bus_for names it: run_id from ``<run>/config.json``, else the run
    directory's name."""
    run_id = ''
    try:
        cfg = json.loads((Path(run) / 'config.json').read_text(encoding='utf-8'))
        run_id = str(cfg.get('run_id') or '') if isinstance(cfg, dict) else ''
    except (OSError, ValueError):
        pass
    return _BUS_PREFIX + ':run:' + (run_id or Path(run).name) + ':audit'


def _sid(value):
    try:
        ms, _, n = str(value).partition('-')
        return int(ms), int(n or 0)
    except ValueError:
        return (-1, -1)


def _wanted(event, patterns):
    return any(fnmatch.fnmatchcase(event, p) for p in patterns)


def mirror(r, run, policy, *, scanner, chain, count=500, stream=None):
    """One bounded, non-blocking batch: read up to ``count`` entries of the run's v2 audit stream after the cursor
    (hash ``fx:channel:auditlog`` field ``last_id``), write those whose event matches ``audit_log.events`` to the
    chain (each record through ``scanner.mask_record`` first), then move the cursor. Returns the number of stream
    entries consumed (0 = caught up, or another writer holds the chain). Single writer: the chain's lock is taken
    for the call (and released again unless the caller already held it). A crash between the chain write and the
    cursor never duplicates a line: entries at or before the chain's last ``stream_id`` are skipped."""
    if _off(policy):
        return 0
    held = chain._lock_fd is not None
    if not chain.acquire():
        return 0
    try:
        key = stream or _stream_key(run)
        cursor = r.hget(channels.AUDITLOG, 'last_id') or '0-0'
        rows = r.xrange(key, min='(' + cursor, count=int(count))
        if not rows:
            return 0
        patterns = list((policy.get('audit_log') or {}).get('events') or security.DEFAULTS['audit_log']['events'])
        chain._load()
        done = _sid((chain.last_record or {}).get(STREAM_ID, '0-0')) if isinstance(chain.last_record, dict) \
            else (0, 0)
        for sid, fields in rows:
            if _sid(sid) <= done or not _wanted(str(fields.get('event') or ''), patterns):
                continue
            record = dict(fields)
            record[STREAM_ID] = sid
            if scanner is not None:
                record, _ = scanner.mask_record(record)
            chain.append(record)
        r.hset(channels.AUDITLOG, 'last_id', rows[-1][0])
        return len(rows)
    finally:
        if not held:
            chain.release()


def daemon_pump(f):
    """The daemon's single writer (E1-M (a)): one ``mirror`` batch per tick into ``<run>/<audit_log.dir>``. The
    Chain (holding ``.writer.lock`` for the daemon's life) and the Scanner are built at the first call and kept on
    ``f``; the stream is the run's own ``f.v2.bus.audit_key``. Off (no policy / ``audit_log.enabled: off``) -> 0,
    nothing built, read or created. Returns the number of stream entries consumed."""
    policy = security.policy(getattr(f, 'cfg', None))
    if _off(policy):
        return 0
    from fleetmux.channels import secrets
    run = Path(f.cfg['run'])
    state = getattr(f, '_audit_pump', None)
    if state is None:
        log = getattr(f, 'log', None)
        section = policy.get('audit_log') or {}
        chain = Chain(run / (section.get('dir') or security.DEFAULTS['audit_log']['dir']),
                      uappend=(section.get('uappend') or security.DEFAULTS['audit_log']['uappend']) == 'on',  # F-1
                      audit=(lambda event, **fields: log(event, **fields)) if log else None)
        state = f._audit_pump = {'chain': chain, 'scanner': secrets.Scanner(policy, r=f.r, run=run)}
    if not state['chain'].acquire():
        return 0
    v2 = getattr(f, 'v2', None)
    stream = getattr(getattr(v2, 'bus', None), 'audit_key', None)
    return mirror(f.r, run, policy, scanner=state['scanner'], chain=state['chain'], stream=stream)
