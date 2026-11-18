"""Process side of the applet SDK (docs/applet-interface.md sections 2 and 7).

Imports only the standard library, ``redis`` and ``fleetmux.awake`` (standard library only); ``yaml`` is imported
when parameters are first read.

Clocks (docs/design.md section 35): ``hb_at`` is Unix seconds (the host reads it through ``awake.from_wall``); every
local wait (``stale_after_s``, the write-back period, the lease calls) is ``awake.mono()``, which stops while the
machine sleeps; host times (``started_at``, lease ``expires_at``, ``host.draining``) are the host fleet's awake
seconds, read with ``fleet_clock``: the host's published clock (``fx:clock``) when Redis answers, else measured
here from the last read (or from the start) with the same monotonic clock.
The lease calls (``request_lease``, ``leases``, ``escalate``, ``notify``, ``audit``) are not in this module; they
use only the public attributes ``name``, ``token``, ``redis``, ``config_path``, ``state_dir``, ``run_dir`` and
``stopping``.
"""
import json
import os
import re
import signal
import sys
import threading
import time
import traceback
from pathlib import Path

INTERFACE = 1
EXIT_CONFIG = 78
PHASE_MAX = 200
ENV = ('FLEETMUX_APPLET_NAME', 'FLEETMUX_APPLET_INTERFACE', 'FLEETMUX_APPLET_CONFIG', 'FLEETMUX_APPLET_REDIS',
       'FLEETMUX_APPLET_TOKEN', 'FLEETMUX_APPLET_STATE', 'FLEETMUX_APPLET_RUN')
EVENTS_POS = 'events.pos'  # in state_dir: {"run": <run dir>, "id": <last stream id read>}
WRITEBACK_S = 300

from fleetmux import awake  # noqa: E402
from fleetmux.applet.lease import (  # noqa: E402  (lease calls, section 7; YAML lock, 2.5)
    WAITS, LeaseCalls, LeaseDenied, Rejected, file_lock)


def key(name):
    return 'fx:applet:' + name


class Applet(LeaseCalls):
    """One applet process. Reads the FLEETMUX_APPLET_* environment, connects to the host Redis,
    starts the heartbeat thread and handles SIGTERM. ``stopping`` is set on SIGTERM or host.draining."""

    def __init__(self, *, name, token, redis, config_path, state_dir, run_dir, heartbeat_s=10.0, env=None,
                 clock=None, stale_after_s=60.0, stop_grace_s=30.0, waits=None):
        self.name = name
        self.token = token
        self.redis = redis
        self.config_path = Path(config_path)
        self.state_dir = Path(state_dir)
        self.run_dir = Path(run_dir)
        self.stopping = threading.Event()
        self.heartbeat_s = float(heartbeat_s)
        self.stale_after_s = float(stale_after_s)  # Redis unreachable this long: stop (exit 0 from run)
        self.stop_grace_s = float(stop_grace_s)  # SIGTERM to SIGKILL; run() returns leases within half of it
        self.waits = dict(WAITS, **(waits or {}))  # local bounds of the lease calls without wait_s (section 7)
        self._env = dict(os.environ if env is None else env)
        self._clock = clock or time.time  # hb_at: Unix seconds
        self._wait = clock or awake.mono  # local waits: stops while the machine sleeps
        self.fleet_clock = awake.local()  # the host fleet's awake time (synced on start and by the heartbeat)
        if clock is not None:  # a test clock stands for every clock
            self.fleet_clock.now = clock
        self._params = None
        self._thread = None
        self._drain_pos, self._drain_since = '0-0', None  # host.draining watch of the heartbeat thread

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_env(cls, env=None, *, start=True):
        """Build from the environment. Exits 78 when a variable is missing or the interface version differs."""
        env = dict(os.environ if env is None else env)
        missing = [k for k in ENV if not env.get(k)]
        if missing:
            _config_exit('missing environment: ' + ', '.join(missing))
        if env['FLEETMUX_APPLET_INTERFACE'] != str(INTERFACE):
            _config_exit('interface ' + env['FLEETMUX_APPLET_INTERFACE'] + ' is not supported by this SDK '
                         '(speaks ' + str(INTERFACE) + ')')
        try:
            heartbeat_s = float(env.get('FLEETMUX_APPLET_HEARTBEAT_S') or 10)
            stale_after_s = float(env.get('FLEETMUX_APPLET_STALE_AFTER_S') or 60)
            stop_grace_s = float(env.get('FLEETMUX_APPLET_STOP_GRACE_S') or 30)
            import redis
            client = redis.Redis(unix_socket_path=env['FLEETMUX_APPLET_REDIS'], decode_responses=True,
                                 socket_connect_timeout=3, socket_timeout=30)
            client.ping()
        except Exception as exc:
            _config_exit('cannot connect to the host Redis: ' + str(exc)[:200])
        waits = _waits(env.get('FLEETMUX_APPLET_WAITS'))
        app = cls(name=env['FLEETMUX_APPLET_NAME'], token=env['FLEETMUX_APPLET_TOKEN'], redis=client,
                  config_path=env['FLEETMUX_APPLET_CONFIG'], state_dir=env['FLEETMUX_APPLET_STATE'],
                  run_dir=env['FLEETMUX_APPLET_RUN'], heartbeat_s=heartbeat_s, env=env,
                  stale_after_s=stale_after_s, stop_grace_s=stop_grace_s, waits=waits)
        if start:
            app.start()
        return app

    def start(self):
        """Install the SIGTERM handler (main thread only), write the first heartbeat, start the heartbeat thread."""
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM, lambda *_: self.stopping.set())
        self.fleet_clock.sync(self.redis)
        try:  # the host's spawn time: a host.draining older than this was meant for an earlier process
            self._drain_since = float(self.redis.hget(key(self.name), 'started_at') or 0) or None
        except (TypeError, ValueError):
            pass
        self._drain_since = self._drain_since or self.fleet_clock.now()
        self.beat()
        self._thread = threading.Thread(target=self._heartbeat, name='applet-heartbeat', daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ heartbeat
    def beat(self):
        self.redis.hset(key(self.name), mapping={'pid': str(os.getpid()), 'hb_at': repr(self._clock())})

    def _heartbeat(self):
        """Beat every ``heartbeat_s``. The applet does not depend on the host daemon process (applets.md 2.3);
        only a host Redis unreachable for ``stale_after_s`` stops it (``run`` then exits 0)."""
        last_flush = last_ok = self._wait()  # start() wrote the first beat
        while not self.stopping.wait(self.heartbeat_s):
            try:
                self.beat()
                last_ok = self._wait()
                self.fleet_clock.sync(self.redis)
            except Exception as exc:
                print('applet heartbeat: ' + str(exc)[:200], file=sys.stderr, flush=True)
                if self._wait() - last_ok >= self.stale_after_s:
                    print('applet: host Redis unreachable for ' + str(self.stale_after_s) + ' s; stopping',
                          file=sys.stderr, flush=True)
                    self.stopping.set()
                    break
                continue
            try:
                self._watch_drain()
            except Exception as exc:
                print('applet drain watch: ' + str(exc)[:200], file=sys.stderr, flush=True)
            try:
                if self._params is not None and self._wait() - last_flush >= self._params.writeback_s:
                    last_flush = self._wait()
                    self._params.flush()
            except Exception as exc:  # a Redis hiccup must not kill the thread; the host sees a stale beat
                print('applet heartbeat: ' + str(exc)[:200], file=sys.stderr, flush=True)

    def _watch_drain(self):
        """Set ``stopping`` on a host.draining sent since this process was started (section 3.6), so an applet
        that never reads events() still returns its leases. Reads on its own position; events() is not moved."""
        while True:  # to the end of the stream: a restarted applet may find a long backlog
            got = self.redis.xread({key(self.name) + ':events': self._drain_pos}, count=100)
            entries = [x for _, rows in got or [] for x in rows]
            if not entries:
                return
            for sid, fields in entries:
                self._drain_pos = sid
                try:
                    event = json.loads(fields.get('data') or '{}')
                    at = float(event.get('at') or 0)
                except (ValueError, TypeError, AttributeError):
                    continue
                if event.get('type') == 'host.draining' and at >= (self._drain_since or 0):
                    self.stopping.set()

    # ------------------------------------------------------------------ status
    def set_phase(self, text: str) -> None:
        """Short free text shown in ``fleetmux status`` (what the applet is doing now)."""
        self.redis.hset(key(self.name), 'phase', str(text)[:PHASE_MAX])

    def params(self) -> "Params":
        """The applet's parameter block (section 2.5). Exits 78 when the file cannot be read or has no such block."""
        if self._params is None:
            try:
                self._params = Params(self.redis, self.name, self._env.get('FLEETMUX_APPLET_PARAMS_FILE'),
                                      self._env.get('FLEETMUX_APPLET_PARAMS_BLOCK'))
            except ValueError as exc:
                _config_exit(str(exc))
        return self._params

    # ------------------------------------------------------------------ host -> applet
    def _pos_path(self):
        return self.state_dir / EVENTS_POS

    def _pos(self):
        try:
            data = json.loads(self._pos_path().read_text())
            if data.get('run') == str(self.run_dir):
                return data['id']
        except (OSError, ValueError, KeyError, AttributeError):
            pass
        return '0-0'  # first read, or a new host run (the stream is new then)

    def _save_pos(self, sid):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._pos_path().with_suffix('.tmp')
        tmp.write_text(json.dumps({'run': str(self.run_dir), 'id': sid}))
        os.replace(tmp, self._pos_path())

    def events(self, *, block_s: float = 5.0):
        """Host events in order (section 3.6). Remembers its position in state_dir. Waits up to ``block_s`` for
        the first event, then yields what is there and returns. ``host.draining`` also sets ``stopping``."""
        pos = self._pos()
        got = self.redis.xread({key(self.name) + ':events': pos}, count=100,
                               block=max(1, int(block_s * 1000)) if block_s else None)
        for _, entries in got or []:
            for sid, fields in entries:
                try:
                    event = json.loads(fields.get('data') or '{}')
                except ValueError:
                    event = {}
                if event.get('type') == 'host.draining':
                    self.stopping.set()
                self._save_pos(sid)
                yield event

    def inbox(self, *, block_s: float = 5.0):
        """Items sent with ``fleetmux applet send``. Persist an item before acting on it. Waits up to ``block_s``
        for the first item, then yields what is queued and returns."""
        box = key(self.name) + ':inbox'
        first = self.redis.blpop([box], timeout=max(1, int(round(block_s)))) if block_s else None
        raw = first[1] if first else self.redis.lpop(box)
        while raw is not None:
            try:
                yield json.loads(raw)
            except ValueError:
                print('applet inbox: skipped an item that is not JSON', file=sys.stderr, flush=True)
            if self.stopping.is_set():
                return
            raw = self.redis.lpop(box)

    # ------------------------------------------------------------------ main loop
    def run(self, step):
        """Main loop helper: call ``step`` until ``stopping``; ``step`` returns seconds to sleep.
        Exits 0 on stop, after writing back parameters, sending queued audit records (at most a quarter of
        ``stop_grace_s``) and returning open leases (at most half of ``stop_grace_s`` waiting for the host); a
        ``stopping`` refusal (Rejected / LeaseDenied) that leaves ``step`` is a stop too. Any other uncaught
        exception exits 1 (a crash)."""
        code, stopped = 0, False
        try:
            while not self.stopping.is_set():
                delay = step(self)
                if delay:
                    self.stopping.wait(float(delay))
            stopped = True
        except SystemExit:
            raise
        except (Rejected, LeaseDenied) as exc:  # a call cut by the stop
            stopped = exc.reason == 'stopping' and self.stopping.is_set()
            if not stopped:
                traceback.print_exc()
                code = 1
        except BaseException:
            traceback.print_exc()
            code = 1
        finally:
            self.stopping.set()
            if self._params is not None:
                try:
                    self._params.flush()
                except Exception:
                    traceback.print_exc()
            left = self._flush_audits(self.stop_grace_s / 4)  # bounded: queued audit records go out first
            if left:
                self._drop_audits()
                print('applet: ' + str(left) + ' audit records not sent at exit', file=sys.stderr, flush=True)
            if stopped:  # section 2.4: release leases, then exit 0 (a crash or an exit 75 / 78 keeps them)
                try:
                    left = self._return_leases(self.stop_grace_s / 2)
                    if left:
                        print('applet: leases still open at exit: ' + ', '.join(left), file=sys.stderr, flush=True)
                except Exception:
                    traceback.print_exc()
        sys.exit(code)


class Params:
    """The applet's parameter block: hot keys live in ``fx:applet:<name>:params`` (JSON values) and are written
    back to the block on a timer; every other key is read once from the file."""

    def __init__(self, redis, name, path=None, block=None):
        self.redis, self.name, self.path, self.block = redis, name, path, block
        self.hash = key(name) + ':params'
        data = read_block(path, block) if path and block else {}
        self.values = dict(data)
        self.hot = [k for k in data.get('hot') or [] if isinstance(k, str)]
        self.writeback_s = float(data.get('writeback_s') or WRITEBACK_S)
        self.written = {k: data.get(k) for k in self.hot}
        if self.hot:
            self.redis.hset(self.hash, mapping={k: json.dumps(data.get(k)) for k in self.hot})

    def get(self, key, default=None):
        """Current value (hot keys read Redis on every call)."""
        if key in self.hot:
            raw = self.redis.hget(self.hash, key)
            if raw is None:
                return default
            value = json.loads(raw)
            return default if value is None else value
        return self.values.get(key, default)

    def set(self, key, value) -> None:
        """Change a hot key; written back to the YAML on the timer."""
        if key not in self.hot:
            raise KeyError(key + ' is not a hot key of block ' + str(self.block))
        self.redis.hset(self.hash, key, json.dumps(value))

    def flush(self) -> None:
        """Write changed hot keys back to the YAML now (atomic replace of the file)."""
        if not (self.hot and self.path and self.block):
            return
        current = {k: json.loads(v) for k, v in (self.redis.hgetall(self.hash) or {}).items() if k in self.hot}
        changed = {k: v for k, v in current.items() if v != self.written.get(k)}
        if not changed:
            return
        path = Path(self.path)
        with file_lock(path):  # one rule for every writer of the project YAML (section 2.5)
            text = set_keys(path.read_text(), self.block, changed)
            tmp = path.with_name('.' + path.name + '.tmp')
            tmp.write_text(text)
            os.replace(tmp, path)
        self.written.update(changed)


def read_block(path, block):
    """The mapping ``block`` of the YAML file ``path`` (an empty block is ``{}``). ValueError when the file cannot
    be read or parsed, or has no such block, or the block is not a mapping."""
    import yaml
    try:
        whole = yaml.safe_load(Path(path).read_text())
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError('cannot read parameters ' + str(path) + ': ' + str(exc).splitlines()[0][:200]) from None
    if not isinstance(whole, dict) or block not in whole:
        raise ValueError('parameter file ' + str(path) + ' has no block ' + block)
    data = whole[block] if whole[block] is not None else {}
    if not isinstance(data, dict):
        raise ValueError('parameter block ' + block + ' in ' + str(path) + ' is not a mapping')
    return data


def _flow(value):
    """One-line YAML for ``value``."""
    import yaml
    out = yaml.safe_dump(value, default_flow_style=True, width=float('inf'), allow_unicode=True).strip()
    if out.endswith('\n...'):  # a plain scalar ends its document
        out = out[:-4].rstrip()
    return json.dumps(value, ensure_ascii=False) if '\n' in out else out


def _indent(line):
    return len(line) - len(line.lstrip(' '))


def _note(line):
    """Blank or comment-only line."""
    return not line.strip() or line.lstrip().startswith('#')


def set_keys(text, block, changed):
    """``text`` with the direct keys ``changed`` of the top-level block-style mapping ``block`` set to new values
    (section 2.5). Each changed key becomes one line ``key: <flow value>`` that keeps its trailing comment; a key
    not in the block is added after the block's last line. Every other line stays byte for byte. Raises ValueError,
    and nothing should be written, when the result does not parse back to the old file plus the changes."""
    import yaml
    whole = yaml.safe_load(text)
    if not isinstance(whole, dict) or not isinstance(whole.get(block), dict):
        raise ValueError('parameter block ' + block + ' is missing or not a mapping')
    old = whole[block]
    lines = text.splitlines(keepends=True)
    head_re = re.compile(re.escape(block) + r'\s*:\s*(#.*)?$')
    for name, value in changed.items():
        head = next((i for i, x in enumerate(lines) if head_re.match(x.rstrip('\r\n'))), None)
        if head is None:
            raise ValueError('parameter block ' + block + ' is not block style (one key per line)')
        end = next((i for i in range(head + 1, len(lines)) if not _note(lines[i]) and _indent(lines[i]) == 0),
                   len(lines))
        body = [i for i in range(head + 1, end) if not _note(lines[i])]
        pad = ' ' * (_indent(lines[body[0]]) if body else 2)
        key_re = re.compile(re.escape(pad) + r'''(['"]?)''' + re.escape(name) + r'\1\s*:(?=\s|$)')
        at = next((i for i in body if _indent(lines[i]) == len(pad) and key_re.match(lines[i])), None)
        if at is None:
            last = body[-1] if body else head
            if not lines[last].endswith('\n'):
                lines[last] += '\n'
            lines.insert(last + 1, pad + name + ': ' + _flow(value) + '\n')
            continue
        stop = at + 1  # a multi-line value: deeper lines, and "- " items at the key's own indent
        while stop < end and (_note(lines[stop]) or _indent(lines[stop]) > len(pad)
                              or lines[stop].startswith(pad + '- ')):
            stop += 1
        while stop > at + 1 and _note(lines[stop - 1]):
            stop -= 1
        line = lines[at]
        eol = line[len(line.rstrip('\r\n')):]
        token = key_re.match(line).group(0)
        rest, comment = line[len(token):len(line) - len(eol)], ''
        if stop == at + 1:  # keep a trailing comment: the first " #" after which the old value still parses
            for pos in (p for p in range(1, len(rest)) if rest[p] == '#' and rest[p - 1] in ' \t'):
                try:
                    if yaml.safe_load(rest[:pos]) == old.get(name):
                        comment = rest[len(rest[:pos].rstrip()):]
                        break
                except yaml.YAMLError:
                    continue
        lines[at:stop] = [token + ' ' + _flow(value) + comment + (eol or '\n')]
    out = ''.join(lines)
    want = dict(whole, **{block: dict(old, **changed)})
    if yaml.safe_load(out) != want:
        raise ValueError('cannot write back block ' + block + ' without changing other lines')
    return out


def _waits(raw):
    """FLEETMUX_APPLET_WAITS: JSON object of call name to seconds > 0 (section 2.2). Exits 78 when malformed."""
    if not raw:
        return {}
    try:
        waits = json.loads(raw)
    except ValueError:
        waits = None
    if not (isinstance(waits, dict) and all(
            k in WAITS and isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0
            for k, v in waits.items())):
        _config_exit('FLEETMUX_APPLET_WAITS must be a JSON object of ' + ', '.join(WAITS) + ' to seconds > 0')
    return waits


def _config_exit(reason):
    print('applet config error: ' + reason, file=sys.stderr, flush=True)
    sys.exit(EXIT_CONFIG)
