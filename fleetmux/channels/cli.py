"""`fleetmux channel login|test|send|status|actor` (package P1g; docs/design.md §53, docs/contracts.md §8).

One actor process per fleet (`channel actor <config.json>`): it takes the leases (lease.acquire_all), starts one
inbound thread per leased receiving channel, sends outbound in its main loop through P1e, writes
``fx:channel:actor`` (pid, started, and ``beat`` from its own thread every ``beat_every`` s), and returns 0 within
5 s of the parent going away, leases released. Drain (``fx:channel:actor.drain`` set): stop inbound, route and send
what is queued through P1e's routing / send wrapper (non-blocking takes; P1e's wrapper owns ``sending`` /
``sending_at``), then ``drained=1``, release the leases, return 0. Each adapter instance gets
``ch.fleet_name = channels.fleet_label(cfg)`` after construction (never a constructor argument).

This module imports only the frozen interface at the top (``fleet.cli()`` builds the ``channel`` parser from
``add_parser`` here); the actor, Redis and adapters load inside the commands that need them.
"""
import argparse
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

from fleetmux import channels

BEAT_FRESH_S = 30  # `channel send` queues when fx:channel:actor.beat is younger than this, else sends directly.
                   # Independent of the daemon's actor restart threshold (P1f).
STEP_S = 0.2       # the actor's main loop: one outbound round, then this pause (busy: BUSY_STEP_S)
BUSY_STEP_S = 0.02
SCAN_EVERY_S = 1.0  # outbound_scan (fx:results / fx:task / fx:escalations) at most this often
TEST_TEXT = 'fleetmux channel test'
_KEY = re.compile(r'[A-Z][A-Z0-9_]{0,63}')


# ------------------------------------------------------------------ parser (fleet.cli() calls add_parser)
def add_parser(sub):
    """Add ``channel`` with login / test / send / status / actor to the top-level subparsers ``sub``."""
    ch = sub.add_parser('channel', help='message channels: login / test / send / status (actor: internal)')
    cs = ch.add_subparsers(dest='channel_cmd', required=True)
    add_approve_parser(cs)   # P8l: channel approve (separate function: other packages add theirs the same way)
    p = cs.add_parser('login', help='write the account file <config>/channels/<type>-<account>.env (0600); '
                                    '`login totp` sets up the high-tier TOTP secret (P8l)')
    p.add_argument('type')
    p.add_argument('account', nargs='?')  # required for every type except totp (checked in run)
    p.add_argument('--allow', action='append', metavar='ID', help='whitelist entry (repeatable; replaces ALLOW)')
    p.add_argument('--set', action='append', metavar='KEY=VALUE', help='account key (repeatable; merged)')
    p.add_argument('--from-stdin', action='store_true', help='read KEY=VALUE lines from stdin (not shell history)')
    p = cs.add_parser('test', help='check the credentials, or send one message with --to')
    p.add_argument('type')
    p.add_argument('account')
    p.add_argument('--to', metavar='CONVERSATION')
    p.add_argument('--text', metavar='T')
    p = cs.add_parser('send', help='send one message: queued for the fleet\'s actor, or sent directly')
    p.add_argument('--config', help='the run\'s config.json')
    p.add_argument('--channel', metavar='TYPE:ACCOUNT')
    p.add_argument('--to', metavar='CONVERSATION')
    p.add_argument('--reply-to', metavar='REF', help='JSON object = reply_ref as is; a bare string = message_id')
    p.add_argument('--text', metavar='T', help='the text; - reads stdin')
    p.add_argument('--text-file', metavar='F', help='the text from a file; - reads stdin')
    p.add_argument('--file', metavar='PATH', help='send this file')
    p.add_argument('--event', default='manual', help='manual (default), daemon_down, run_closed, ...')
    p = cs.add_parser('status', help='per channel: state, lease holder, last in / out, queue, dead letters')
    p.add_argument('--config', help='the run\'s config.json (without: this machine\'s account files and leases)')
    p.add_argument('--json', action='store_true')
    p = cs.add_parser('actor', help='the fleet\'s channel actor process (internal; started by the daemon)')
    p.add_argument('cfg')
    _add_audit_parser(cs)  # P8g
    return ch


def _add_audit_parser(cs):
    """P8g: ``channel audit --config <cfg> [--verify]`` (contracts §8 「审计链」)."""
    p = cs.add_parser('audit', help='the run\'s append-only audit chain: list the chain files, --verify the hashes')
    p.add_argument('--config', required=True, help='the run\'s config.json')
    p.add_argument('--verify', action='store_true', help='check every line\'s hash and the links between files')
    return p


def _parser():
    p = argparse.ArgumentParser(prog='fleetmux')
    add_parser(p.add_subparsers(dest='cmd', required=True))
    return p


# ------------------------------------------------------------------ shared helpers
def connect(cfg):
    """The actor's own Redis client for ``cfg['redis_socket']`` (no socket_timeout on a connection that blocks).
    Module level so tests can wrap it."""
    import redis
    return redis.Redis(unix_socket_path=str(cfg['redis_socket']), db=0, decode_responses=True,
                       socket_connect_timeout=5)


def _client(cfg):
    """A short-timeout client for the one-shot commands, or None when the run's Redis is not reachable."""
    path = cfg.get('redis_socket') if isinstance(cfg, dict) else None
    if not path:
        return None
    import redis
    try:
        r = redis.Redis(unix_socket_path=str(path), db=0, decode_responses=True, socket_connect_timeout=2,
                        socket_timeout=5)
        r.ping()
        return r
    except (redis.RedisError, OSError):
        return None


def _load_cfg(path):
    cfg = json.loads(Path(path).expanduser().read_text(encoding='utf-8'))
    if not isinstance(cfg, dict):
        raise ValueError('config is not a JSON object')
    return cfg


def _accounts(entries):
    out = []
    for e in entries:
        try:
            out.append(channels.load_account(e['type'], e['account']))
        except Exception:
            continue
    return out


def _mask(text, accounts=()):
    text = channels.redact(str(text))
    for account in accounts:
        text = channels.redact(text, account)
    return text


def _say(stream, obj):
    stream.write(json.dumps(obj, ensure_ascii=False) + '\n')
    stream.flush()


def _err(stream, text):
    stream.write(text.rstrip('\n') + '\n')
    stream.flush()


def _io(stdin, stdout, stderr):
    return stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr


# ------------------------------------------------------------------ entry
def run(argv_or_args, *, stdin=None, stdout=None, stderr=None) -> int:
    """Entry for ``fleetmux channel ...``: an argv list or the parsed argparse namespace. Returns the exit code
    (0 ok, 1 failed, 2 usage)."""
    stdin, stdout, stderr = _io(stdin, stdout, stderr)
    if isinstance(argv_or_args, argparse.Namespace):
        args = argv_or_args
    else:
        argv = list(argv_or_args)
        if not argv or argv[0] != 'channel':
            argv = ['channel'] + argv
        args = _parser().parse_args(argv)
    cmd = args.channel_cmd
    if cmd == 'audit':  # P8g
        return audit(args, stdin=stdin, stdout=stdout, stderr=stderr)
    if cmd == 'actor':
        return actor_main(args.cfg)
    if cmd in P8L_COMMANDS or (cmd == 'login' and getattr(args, 'type', None) == 'totp'):
        return run_p8l(args, stdin=stdin, stdout=stdout, stderr=stderr)
    if cmd == 'login' and not getattr(args, 'account', None):
        _err(stderr, 'fleetmux channel login: the account name is required (only `login totp` takes none)')
        return 2
    fn = {'login': login, 'test': test, 'send': send, 'status': status}[cmd]
    return fn(args, stdin=stdin, stdout=stdout, stderr=stderr)


# ------------------------------------------------------------------ channel audit (P8g)
def audit(args, *, stdin=None, stdout=None, stderr=None) -> int:
    """List the run's chain files (one JSON line each: file, lines, first_seq, last_seq) or, with ``--verify``, also
    ok / first_bad_seq; then one summary line. Exit 0 = listed / every file verified, 1 = a chain fails ``--verify``,
    2 = the config cannot be read. Never prints a record. Reads only the files (tamper-evident, not tamper-proof)."""
    from . import auditlog, security
    stdin, stdout, stderr = _io(stdin, stdout, stderr)
    try:
        cfg = _load_cfg(args.config)
    except (OSError, ValueError) as exc:
        _err(stderr, 'channel audit: cannot read config: ' + type(exc).__name__)
        return 2
    policy = security.policy(cfg)
    rel = ((policy or {}).get('audit_log') or {}).get('dir') or security.DEFAULTS['audit_log']['dir']
    run = Path(cfg.get('run') or Path(args.config).expanduser().parent)
    d = run / rel
    if not d.is_dir():
        _say(stdout, {'dir': str(d), 'files': 0, 'note': 'no audit chain (no security.yaml or audit_log off)'})
        return 0
    rows = auditlog.verify_dir(d)
    for row in rows:
        _say(stdout, row if args.verify else {k: row[k] for k in ('file', 'lines', 'first_seq', 'last_seq')})
    bad = [row for row in rows if not row['ok']]
    summary = {'dir': str(d), 'files': len(rows), 'lines': sum(row['lines'] for row in rows)}
    if args.verify:
        summary['ok'] = not bad
        if bad:
            summary.update(first_bad_file=bad[0]['file'], first_bad_seq=bad[0]['first_bad_seq'])
    _say(stdout, summary)
    return 1 if args.verify and bad else 0


# ------------------------------------------------------------------ channel login
class _LoginIO:
    """The ``io`` a module-level ``LOGIN`` gets: ``print`` goes to stderr (stdout stays the JSON answer),
    ``readline`` reads the terminal / stdin."""

    def __init__(self, stdin, stderr):
        self._in, self._err = stdin, stderr

    def print(self, text=''):
        _err(self._err, str(text))

    def readline(self):
        return self._in.readline()


def login_hook(type_):
    """The adapter module's ``LOGIN(account_name, io, transport) -> dict``, or None when it defines none."""
    import importlib
    try:
        cls = channels.adapter_class(type_)
    except channels.Unavailable:
        return None
    module = sys.modules.get(cls.__module__)
    if module is None:
        try:
            module = importlib.import_module(cls.__module__)
        except ImportError:
            return None
    hook = getattr(module, 'LOGIN', None)
    return hook if callable(hook) else None


def _allow_key(type_, key):
    return key == 'ALLOW' or key == channels.ALLOW_SYNONYMS.get(type_)


def _split_allow(value):
    return [a for a in re.split(r'[,\s]+', str(value)) if a]


def _uniq(items):
    out = []
    for x in items:
        x = str(x).strip()
        if x and x not in out:
            out.append(x)
    return out


def _value_line(key, value):
    value = str(value)
    if any(c in value for c in '\r\n\0'):
        raise ValueError(key + ': a value may not contain a line break or NUL')
    if value != value.strip() or (len(value) >= 2 and value[0] == value[-1] and value[0] in '"\'') or \
            (value[:1] in '"\'' and value[-1:] in '"\''):
        value = '"' + value + '"'  # parse_account_text strips exactly one outer pair, so this round-trips
    return key + '=' + value


def write_account_file(type_, account, values, allow):
    """Write the account file atomically: dir 0700, file 0600 (temp file + rename), ALLOW first."""
    path = channels.account_path(type_, account)
    lines = []
    if allow:
        lines.append('ALLOW=' + ','.join(allow))
    for key, value in values.items():
        lines.append(_value_line(key, value))
    data = ('\n'.join(lines) + '\n').encode('utf-8')
    d = path.parent
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(d, 0o700)
    tmp = d / ('.' + path.name + '.tmp-' + str(os.getpid()) + '-' + os.urandom(4).hex())
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    return path


def login(args, *, stdin=None, stdout=None, stderr=None) -> int:
    stdin, stdout, stderr = _io(stdin, stdout, stderr)
    type_, name = args.type, args.account
    if type_ not in channels.known_types():
        _err(stderr, 'channel login: unknown channel type ' + repr(type_))
        return 2
    try:
        path = channels.account_path(type_, name)
    except ValueError as exc:
        _err(stderr, 'channel login: ' + str(exc))
        return 2
    sets = {}
    set_allow = None
    for n, pair in enumerate(args.set or [], 1):
        key, sep, value = pair.partition('=')
        key = key.strip()
        if not sep or not _KEY.fullmatch(key):
            _err(stderr, 'channel login: --set #' + str(n) + ' is not KEY=VALUE (KEY upper case)')
            return 2
        if _allow_key(type_, key):
            set_allow = _split_allow(value)
        else:
            sets[key] = value.strip()
    from_stdin = None
    if args.from_stdin:
        try:
            from_stdin = channels.parse_account_text(type_, name, stdin.read())
        except ValueError as exc:
            _err(stderr, 'channel login: stdin: ' + str(exc))
            return 2
    allow_flag = _uniq(args.allow) if args.allow else None
    if path.exists():
        try:
            old = channels.load_account(type_, name)
        except (OSError, ValueError) as exc:
            _err(stderr, 'channel login: the account file cannot be read: ' + _mask(exc))
            return 1
        values, allow = dict(old.values), _uniq(old.allow)
    else:
        values, allow = {}, []
    if sets or from_stdin is not None:  # R2: --set / --from-stdin never call LOGIN
        if from_stdin is not None:
            values.update(from_stdin.values)
            if from_stdin.allow:
                allow = _uniq(from_stdin.allow)
        values.update(sets)
        if set_allow is not None:
            allow = _uniq(set_allow)
        if allow_flag is not None:
            allow = allow_flag
        status_ = 'ok'
    else:
        hook = login_hook(type_)
        if hook is None:
            if allow_flag is None:
                _err(stderr, 'channel login: ' + type_ + ' has no interactive login; give --set KEY=VALUE, '
                     '--from-stdin or --allow ID')
                return 2
            allow, status_ = allow_flag, 'ok'
        else:
            try:
                got = hook(name, _LoginIO(stdin, stderr), None)
            except Exception as exc:  # noqa: BLE001  (a platform refusal, an expired code, a broken adapter)
                kind = getattr(exc, 'kind', '') or type(exc).__name__
                _err(stderr, 'channel login: ' + kind + ': ' + _mask(getattr(exc, 'detail', '') or exc))
                return 1
            if not isinstance(got, dict):
                _err(stderr, 'channel login: LOGIN returned ' + type(got).__name__ + ', not a dict')
                return 1
            if not got:  # already bound: success, nothing written (not even --allow)
                _say(stdout, {'status': 'already_bound', 'path': str(path)})
                return 0
            got_allow = None
            for key, value in got.items():
                key = str(key)
                if not _KEY.fullmatch(key):
                    _err(stderr, 'channel login: LOGIN returned a bad key ' + repr(key[:40]))
                    return 1
                if _allow_key(type_, key):
                    got_allow = _split_allow(value) if not isinstance(value, (list, tuple)) else list(value)
                else:
                    values[key] = '' if value is None else str(value)
            if got_allow is not None:
                allow = _uniq(got_allow)
            if allow_flag is not None:
                allow = allow_flag  # --allow wins over LOGIN's ALLOW
            status_ = 'ok'
    try:
        write_account_file(type_, name, values, allow)
    except (OSError, ValueError) as exc:
        _err(stderr, 'channel login: not written: ' + _mask(exc))
        return 1
    _say(stdout, {'status': status_, 'path': str(path)})
    return 0


# ------------------------------------------------------------------ channel test
def _missing_deps(cls):
    import importlib.util
    missing = []
    for module in getattr(cls, 'OPTIONAL_DEPS', ()) or ():
        try:
            if importlib.util.find_spec(module) is None:
                missing.append(module)
        except (ImportError, ValueError):
            missing.append(module)
    return missing


def test(args, *, stdin=None, stdout=None, stderr=None) -> int:
    stdin, stdout, stderr = _io(stdin, stdout, stderr)
    type_, name = args.type, args.account
    if type_ not in channels.known_types():
        _err(stderr, 'channel test: unknown channel type ' + repr(type_))
        return 2
    try:
        account = channels.load_account(type_, name)
    except FileNotFoundError:
        _err(stderr, 'needs_login: no account file; run channel login ' + type_ + ' ' + name)
        return 1
    except (OSError, ValueError) as exc:
        _err(stderr, 'needs_login: account file unreadable (' + _mask(exc) + '); 需重新 channel login ' + type_
             + ' ' + name)
        return 1
    try:
        cls = channels.adapter_class(type_)
    except channels.Unavailable as exc:
        _err(stderr, 'unavailable: ' + _mask(exc, [account]))
        return 1
    for module in _missing_deps(cls):
        _err(stdout, '入站：未装 ' + module + '（pip install …）；发送可用')
    ch = None
    try:
        ch = cls(account)
        if args.to:
            res = ch.send_text(args.to, args.text or TEST_TEXT)
        else:
            res = ch.check()
    except channels.ChannelError as exc:
        state = 'needs_login' if exc.kind in ('auth', 'bad_config') else exc.kind
        hint = '; 需重新 channel login ' + type_ + ' ' + name if state == 'needs_login' else ''
        _err(stderr, state + ': ' + _mask(exc.detail or exc.kind, [account]) + hint)
        return 1
    except Exception as exc:  # noqa: BLE001
        _err(stderr, 'failed: ' + type(exc).__name__ + ': ' + _mask(exc, [account]))
        return 1
    finally:
        if ch is not None:
            try:
                ch.close()
            except Exception:
                pass
    ok = bool(getattr(res, 'ok', False))
    _say(stdout, {'status': ('sent' if args.to else 'ok') if ok else 'failed'})
    if not ok:
        _err(stderr, 'failed: ' + _mask(getattr(res, 'error', '') or 'check failed', [account]))
    return 0 if ok else 1


test.__test__ = False  # not a pytest test


# ------------------------------------------------------------------ channel send
def _reply_ref(value):
    if value is None:
        return {}
    try:
        parsed = json.loads(value)
    except ValueError:
        parsed = None
    return dict(parsed) if isinstance(parsed, dict) else {'message_id': value}


def _beat_fresh(r, beat_fresh_s):
    try:
        beat = r.hget(channels.ACTOR, 'beat')
        age = time.time() - float(beat)
    except Exception:
        return False
    return -beat_fresh_s < age < beat_fresh_s


def send(args, *, stdin=None, stdout=None, stderr=None, beat_fresh_s=BEAT_FRESH_S) -> int:
    stdin, stdout, stderr = _io(stdin, stdout, stderr)
    from . import envelope

    def usage(text):
        _err(stderr, 'channel send: ' + text)
        return 2
    bodies = [x for x in (args.text, args.text_file, args.file) if x is not None]
    if len(bodies) != 1:
        return usage('give exactly one of --text, --text-file, --file')
    if args.event not in channels.OUT_EVENTS:
        return usage('--event must be one of ' + ', '.join(channels.OUT_EVENTS))
    if not args.config and not (args.channel and args.to):
        return usage('give --config <run config.json>, or --channel TYPE:ACCOUNT with --to')
    if args.config:
        try:
            cfg = _load_cfg(args.config)
        except (OSError, ValueError) as exc:
            return usage('config not readable: ' + type(exc).__name__)
    else:
        cfg = {}
    entries = list(cfg.get('channels') or [])
    text = file_ = None
    if args.text is not None:
        text = stdin.read() if args.text == '-' else args.text
    elif args.text_file is not None:
        try:
            text = stdin.read() if args.text_file == '-' else Path(args.text_file).expanduser().read_text(
                encoding='utf-8')
        except (OSError, UnicodeDecodeError) as exc:
            return usage('--text-file not readable: ' + type(exc).__name__)
    else:
        path = Path(args.file).expanduser().resolve()
        if not path.is_file():
            return usage('--file is not a file: ' + str(path))
        file_ = str(path)
    target, type_, name, conversation = 'last', '', '', ''
    if args.channel:
        type_, _, name = args.channel.partition(':')
        try:
            channels.account_path(type_, name)
        except ValueError as exc:
            return usage('--channel: ' + str(exc))
        conversation = args.to or ''
        if not conversation:
            try:
                conversation = channels.load_account(type_, name).values.get('PUSH_TO', '')
            except (OSError, ValueError):
                conversation = ''
            if not conversation:
                return usage('--channel without --to needs PUSH_TO in the account file')
        target = 'explicit'
    elif args.to:
        if not entries:
            return usage('--to without --channel needs a config with channels:')
        type_, name, conversation, target = entries[0]['type'], entries[0]['account'], args.to, 'explicit'
    if target == 'explicit' and not any((e.get('type'), e.get('account')) == (type_, name) for e in entries):
        entries.append({'type': type_, 'account': name, 'push': list(channels.DEFAULT_PUSH)})
        cfg = dict(cfg, channels=entries)
    try:
        item = envelope.outbound(args.event, target=target, text=text, file=file_, channel=type_, account=name,
                                 conversation=conversation, reply_ref=_reply_ref(args.reply_to))
    except ValueError as exc:
        return usage(str(exc))
    r = _client(cfg)
    if r is not None:
        try:
            if _beat_fresh(r, beat_fresh_s):
                from . import queue
                queue.enqueue(r, item)
                _say(stdout, {'status': 'queued'})
                return 0
        except Exception as exc:  # noqa: BLE001  (Redis went away between ping and push: send directly)
            _err(stderr, 'channel send: not queued (' + type(exc).__name__ + '), sending directly')
        finally:
            try:
                r.close()
            except Exception:
                pass
    from . import actor
    try:
        res = actor.send_direct(cfg, item)
    except Exception as exc:  # noqa: BLE001
        res = {'status': 'failed', 'error': type(exc).__name__ + ': ' + str(exc)}
    res = res if isinstance(res, dict) else {'status': 'failed', 'error': 'send_direct returned ' + repr(res)[:80]}
    if res.get('status') == 'sent':
        _say(stdout, {'status': 'sent'})
        return 0
    _say(stdout, {'status': 'failed'})
    reason = res.get('error') or res.get('reason') or res.get('status') or 'send failed'
    _err(stderr, 'channel send: ' + _mask(reason, _accounts(entries)))
    return 1


# ------------------------------------------------------------------ channel status
def _local_entries():
    d = channels.config_dir() / 'channels'
    out = []
    try:
        files = sorted(d.glob('*.env'))
    except OSError:
        files = []
    for f in files:
        type_, sep, name = f.stem.partition('-')
        if not sep:
            continue
        try:
            channels.account_path(type_, name)
        except ValueError:
            continue
        out.append({'type': type_, 'account': name})
    return out


def _dead_counts(r):
    counts = {}
    try:
        raws = r.lrange(channels.DEAD, 0, -1)
    except Exception:
        return counts
    for raw in raws:
        try:
            rec = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(rec, dict):
            key = (rec.get('channel'), rec.get('account'))
            counts[key] = counts.get(key, 0) + 1
    return counts


def _int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _rows(entries, r):
    from . import lease
    dead = _dead_counts(r) if r is not None else {}
    rows = []
    for e in entries:
        type_, name = e['type'], e['account']
        try:
            has_file = channels.account_path(type_, name).exists()
        except ValueError:
            has_file = False
        row = {'type': type_, 'account': name, 'account_file': has_file, 'state': None,
               'holder': lease.holder(type_, name), 'last_in': None, 'last_out': None, 'last_error': None,
               'restarts': None, 'queue': None, 'dead': None, 'heartbeat_at': None, 'activity_at': None,
               'error': None}
        if r is not None:
            try:
                h = r.hgetall(channels.STATUS + type_ + ':' + name)
                row.update({'state': h.get('state'), 'last_in': h.get('last_in'), 'last_out': h.get('last_out'),
                            'last_error': h.get('last_error'), 'restarts': _int(h.get('restarts')),
                            'heartbeat_at': h.get('heartbeat_at'), 'activity_at': h.get('activity_at'),
                            'error': h.get('error')})
                row['queue'] = r.llen(channels.OUTQ_ACCOUNT + type_ + ':' + name)
                row['dead'] = dead.get((type_, name), 0)
            except Exception:
                pass
        rows.append(row)
    return rows


def _text_rows(rows, actor_info):
    lines = []
    if actor_info is not None:
        lines.append('actor: ' + ('pid ' + str(actor_info.get('pid')) if actor_info.get('pid') else 'not running')
                     + (', beat fresh' if actor_info.get('fresh') else ', no fresh beat'))
    if not rows:
        lines.append('no channels')
    for row in rows:
        holder = row['holder']
        who = (str(holder.get('run_id')) + ' (pid ' + str(holder.get('pid')) + ')') if holder else '-'
        lines.append(row['type'] + ':' + row['account'] + '  state=' + str(row['state'] or '-') + '  holder=' + who
                     + '  last_in=' + str(row['last_in'] or '-') + '  last_out=' + str(row['last_out'] or '-')
                     + '  queue=' + str('-' if row['queue'] is None else row['queue'])
                     + '  dead=' + str('-' if row['dead'] is None else row['dead'])
                     + '  restarts=' + str('-' if row['restarts'] is None else row['restarts']))
        if row['last_error'] or row['error']:
            lines.append('  error: ' + str(row['last_error'] or row['error'])[:300])
        if not row['account_file']:
            lines.append('  无账号文件：channel login ' + row['type'] + ' ' + row['account'])
        if row['state'] == 'needs_login':
            lines.append('  需重新 channel login ' + row['type'] + ' ' + row['account'])
        elif row['state'] == 'missing_dependency':
            lines.append('  入站：未装依赖（' + str(row['error'] or '见 channel test') + '）；发送可用')
        elif row['state'] == 'lease_busy':
            lines.append('  入站停：租约在 ' + who + ' 手里')
    return '\n'.join(lines) + '\n'


def status(args, *, stdin=None, stdout=None, stderr=None) -> int:
    stdin, stdout, stderr = _io(stdin, stdout, stderr)
    r, actor_info = None, None
    if args.config:
        try:
            cfg = _load_cfg(args.config)
        except (OSError, ValueError) as exc:
            _err(stderr, 'channel status: config not readable: ' + type(exc).__name__)
            return 2
        entries = [e for e in (cfg.get('channels') or []) if isinstance(e, dict)]
        r = _client(cfg)
        if r is not None:
            try:
                h = r.hgetall(channels.ACTOR)
                actor_info = {'pid': h.get('pid'), 'started': h.get('started'), 'beat': h.get('beat'),
                              'fresh': _beat_fresh(r, BEAT_FRESH_S), 'drained': h.get('drained')}
            except Exception:
                actor_info = None
    else:
        entries = _local_entries()
    try:
        rows = _rows(entries, r)
    finally:
        if r is not None:
            try:
                r.close()
            except Exception:
                pass
    accounts = _accounts(entries)
    for row in rows:
        for key in ('last_error', 'error'):
            if row[key]:
                row[key] = _mask(row[key], accounts)
    if args.json:
        _say(stdout, {'channels': rows, 'actor': actor_info, 'config': args.config or None,
                      'config_dir': str(channels.config_dir())})
    else:
        stdout.write(_text_rows(rows, actor_info if args.config else None))
        stdout.flush()
    return 0


# ------------------------------------------------------------------ channel actor (one per fleet)
class _Stop(threading.Event):
    """An Event that is also the ``stop()`` callable inbound_loop polls."""

    def __call__(self):
        return self.is_set()


def _one_round():
    """``stop`` for P1e's outbound_loop that lets exactly one round run (and never its idle BRPOP)."""
    calls = {'n': 0}

    def stop():
        calls['n'] += 1
        return calls['n'] > 1
    return stop


def actor_main(cfg_path, *, parent_alive=None, clock=None, sleep=None, beat_every=10) -> int:
    from . import actor, lease, security
    from .supervise import BEAT, DRAIN, DRAINED
    clock = clock or time.time
    sleep = sleep or time.sleep
    if parent_alive is None:
        ppid = os.getppid()

        def parent_alive():
            return os.getppid() == ppid
    try:
        cfg = _load_cfg(cfg_path)
        r = connect(cfg)
        r.ping()
    except Exception as exc:  # noqa: BLE001  (the daemon's supervisor restarts us with backoff)
        print('channel actor: cannot start: ' + type(exc).__name__ + ': ' + channels.redact(str(exc)),
              file=sys.stderr)
        return 1
    run = cfg.get('run') or ''
    run_id = str(cfg.get('run_id') or Path(run).name)
    settings = dict(channels.SUPERVISE_DEFAULTS)
    settings.update({k: v for k, v in (cfg.get('channel_supervise') or {}).items() if k in settings})
    entries = [e for e in (cfg.get('channels') or []) if isinstance(e, dict)]

    def live_workers():  # §53「直连 worker」 hint: the run's worker slots (config.json follows roster changes)
        if cfg.get('mode') != 'service':
            return []
        try:
            return list(_load_cfg(cfg_path).get('workers') or [])
        except (OSError, ValueError):
            return list(cfg.get('workers') or [])
    policy = security.policy(cfg)
    outbox = Path(run) / 'outbox' if run else None
    inbox = Path(run) / 'inbox' / 'files' if run else None
    bound = actor.channel_audit(r, cfg, None, None)

    def audit(event, **fields):
        try:
            bound(event, **fields)
        except Exception as exc:  # noqa: BLE001
            print('channel actor: audit not written: ' + event + ': ' + type(exc).__name__, file=sys.stderr)

    def status(type_, account, **fields):
        actor.write_status(r, type_, account, fields)
        if fields.get('holder') is not None:
            try:
                r.hset(channels.STATUS + type_ + ':' + account, 'holder', str(fields['holder']))
            except Exception:
                pass

    stop_in, stop_beat = _Stop(), threading.Event()
    term = {'at': None}    # SIGTERM: drain until this wall-clock time
    drain_at = {'at': None}  # fx:channel:actor.drain seen: drain until this wall-clock time

    def watch_drain():
        """Fix the drain deadline the moment the daemon writes it (the beat thread also runs this, so a send
        that blocks the main loop does not eat the window) and stop inbound at once."""
        if drain_at['at'] is not None:
            return drain_at['at']
        try:
            raw = r.hget(channels.ACTOR, DRAIN)
        except Exception:
            return None
        if not raw:
            return None
        try:
            at = time.time() + max(0.0, float(raw) - clock())
        except (TypeError, ValueError):
            at = time.time()
        drain_at['at'] = at
        stop_in.set()
        return at
    held, chans, threads, pending = {}, {}, [], {}

    def beat_loop():
        last = None
        chunk = max(beat_every / 100.0, 0.01)
        while not stop_beat.is_set() and parent_alive():
            now = clock()
            if last is None or now - last >= beat_every:
                try:
                    r.hset(channels.ACTOR, BEAT, repr(now))
                except Exception as exc:  # noqa: BLE001
                    print('channel actor: beat not written: ' + type(exc).__name__, file=sys.stderr)
                last = now
            watch_drain()
            sleep(chunk)

    def build(entry):
        type_, name = entry['type'], entry['account']
        account = channels.load_account(type_, name)
        cls = channels.adapter_class(type_)
        ch = cls(account, store=actor.RedisStore(r, type_, name), audit=actor.channel_audit(r, cfg, type_, name),
                 inbox_dir=inbox)
        ch.fleet_name = channels.fleet_label(cfg)
        ch.push = list(entry.get('push') if entry.get('push') is not None else channels.DEFAULT_PUSH)
        return ch

    def bring_up(entry):
        """Build and start one leased account; schedules a retry on a missing / bad account file or refusal."""
        key = (entry['type'], entry['account'])
        type_, name = key
        try:
            ch = build(entry)
        except Exception as exc:  # noqa: BLE001  (no account file, bad file, adapter not installed)
            status(type_, name, state='needs_login', error=type(exc).__name__ + ': ' + str(exc))
            audit('channel.error', channel=type_, account=name, kind='bad_config', error=str(exc))
            pending[key] = ('config', clock() + settings['config_retry_s'])
            return
        chans[key] = ch
        try:
            ch.start()
        except channels.MissingDependency as exc:
            status(type_, name, state='missing_dependency', error=str(exc))
            audit('channel.unavailable', channel=type_, account=name, package=exc.package)
            return
        except channels.ChannelError as exc:
            state = 'needs_login' if exc.kind in ('auth', 'bad_config') else (
                exc.kind if exc.kind in channels.INBOUND_STATES else 'retrying')
            status(type_, name, state=state, error=str(exc))
            audit('channel.error', channel=type_, account=name, kind=exc.kind, error=str(exc))
            pending[key] = ('config', clock() + settings['config_retry_s'])
            return
        except Exception as exc:  # noqa: BLE001
            status(type_, name, state='retrying', error=type(exc).__name__ + ': ' + str(exc))
            audit('channel.error', channel=type_, account=name, kind='transient', error=str(exc))
            pending[key] = ('config', clock() + settings['config_retry_s'])
            return
        if not getattr(ch, 'RECEIVES', True):
            status(type_, name, state='outbound_only')
            return
        t = threading.Thread(target=actor.inbound_loop, args=(ch,), name='channel-in-' + type_ + '-' + name,
                             kwargs={'stop': stop_in, 'r': r, 'cfg': cfg, 'clock': clock, 'sleep': stop_in.wait},
                             daemon=True)
        t.start()
        threads.append(t)

    def retry_due():
        now = clock()
        for key, (kind, due) in list(pending.items()):
            if now < due or stop_in.is_set():
                continue
            entry = next(e for e in entries if (e['type'], e['account']) == key)
            pending.pop(key)
            if kind == 'lease':
                got = lease.acquire_all([entry], run_id=run_id, run=run, audit=audit, status=status)
                if got.get(key) is None:
                    pending[key] = ('lease', now + settings['lease_retry_s'])
                    continue
                held[key] = got[key]
            old = chans.pop(key, None)
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
            bring_up(entry)

    def on_term(signum, frame):
        term['at'] = time.time() + 5

    if threading.current_thread() is threading.main_thread():
        import signal
        signal.signal(signal.SIGTERM, on_term)

    code = 0
    beat = threading.Thread(target=beat_loop, name='channel-beat', daemon=True)
    try:
        r.hset(channels.ACTOR, mapping={'pid': str(os.getpid()), 'started': repr(clock())})
        r.hdel(channels.ACTOR, DRAINED)
        beat.start()
        got = lease.acquire_all(entries, run_id=run_id, run=run, audit=audit, status=status)
        for entry in entries:
            key = (entry['type'], entry['account'])
            if got.get(key) is None:
                pending.setdefault(key, ('lease', clock() + settings['lease_retry_s']))
                continue
            held[key] = got[key]
            bring_up(entry)
        last_scan = None
        while parent_alive():
            deadline = term['at'] if term['at'] is not None else watch_drain()
            if deadline is not None:
                stop_in.set()
                try:
                    actor.drain(r, dict(chans), deadline=deadline, clock=time.time, audit=audit, policy=policy)
                except Exception as exc:  # noqa: BLE001
                    print('channel actor: drain: ' + type(exc).__name__ + ': ' + channels.redact(str(exc)),
                          file=sys.stderr)
                break
            busy = False
            try:
                retry_due()
                now = clock()
                if chans and (last_scan is None or now - last_scan >= SCAN_EVERY_S):
                    last_scan = now
                    actor.outbound_scan(r, [e for e in entries if (e['type'], e['account']) in chans], clock=clock,
                                        outbox=outbox, workers=live_workers)
                if chans:
                    actor.outbound_loop(r, dict(chans), stop=_one_round(), clock=clock, sleep=sleep, audit=audit,
                                        policy=policy)
                    busy = bool(r.llen(channels.OUTQ)) or any(
                        r.llen(channels.OUTQ_ACCOUNT + t + ':' + a) for t, a in chans)
            except Exception as exc:  # noqa: BLE001  (one bad round never ends the actor)
                print('channel actor: round: ' + type(exc).__name__ + ': ' + channels.redact(str(exc)),
                      file=sys.stderr)
                sleep(1)
                continue
            sleep(BUSY_STEP_S if busy else STEP_S)
    except Exception as exc:  # noqa: BLE001
        print('channel actor: ' + type(exc).__name__ + ': ' + channels.redact(str(exc)), file=sys.stderr)
        code = 1
    finally:
        stop_beat.set()
        stop_in.set()
        for t in threads:
            t.join(0.5)
        if beat.is_alive():
            beat.join(2)
        for ch in chans.values():
            try:
                ch.close()
            except Exception:
                pass
        for held_lease in held.values():
            if held_lease is not None:
                held_lease.release()
        try:
            r.close()
        except Exception:
            pass
    return code


# ------------------------------------------------------------------ P8l: channel approve, login totp (approve-1)
P8L_COMMANDS = ('approve',)


def add_approve_parser(cs):
    """``channel approve --config <run>/config.json <pending id> <nonce>``: the desk second step (P8l)."""
    p = cs.add_parser('approve', help='desk second step of a high-tier action (only records whose mode is desk)')
    p.add_argument('--config', required=True, help='the run\'s config.json (the fleet that holds the pending record)')
    p.add_argument('pending_id')
    p.add_argument('nonce')
    return p


def run_p8l(args, *, stdin=None, stdout=None, stderr=None) -> int:
    if args.channel_cmd == 'approve':
        return approve_cmd(args, stdin=stdin, stdout=stdout, stderr=stderr)
    return login_totp(args, stdin=stdin, stdout=stdout, stderr=stderr)


def approve_cmd(args, *, stdin=None, stdout=None, stderr=None) -> int:
    """The desk mode's only entry (contracts §8 C1 / O7): accepts only kind=action, tier=high records whose frozen
    ``oob_modes`` is desk; signs ``desk_proof`` with the local desk key and calls ``approve.answer_action`` on the
    fleet that holds the record (no event, no supervisor, no phone). Exit 0 = ticket issued; 1 = refused / no
    Redis / bad config. Output never carries the nonce, a key or an account value."""
    from fleetmux.channels import approve, oob, security
    _, stdout, stderr = _io(stdin, stdout, stderr)
    pid = str(args.pending_id)
    try:
        cfg = _load_cfg(args.config)
    except (OSError, ValueError):
        _err(stderr, 'channel approve: cannot read the run config')
        return 1
    if not re.fullmatch(r'p-[0-9a-f]{8}', pid):
        _err(stderr, 'channel approve: not a pending id')
        return 1
    r = _client(cfg)
    if r is None:
        _err(stderr, 'channel approve: the run\'s Redis is not reachable; nothing approved')
        return 1
    try:
        approve.current_epoch(r)
    except approve.EpochError:
        _err(stderr, 'channel approve: refused (epoch_malformed)')
        return 1
    rec = r.hgetall(channels.PENDING + pid)
    if rec.get('kind') != 'action' or rec.get('tier') != 'high' or rec.get('oob_modes') != 'desk':
        _err(stderr, 'channel approve: refused: only a high-tier action whose confirmation mode is desk')
        return 1
    policy = security.policy(cfg)
    if policy is None:
        _err(stderr, 'channel approve: refused: the run has no security policy')
        return 1
    ev = {'kind': 'dialog_user_answer', 'pending_id': pid, 'decision': 'approve', 'nonce': str(args.nonce),
          'channel': 'desk', 'account': 'desk', 'sender': 'desk', 'conversation': 'desk',
          'desk_proof': oob.desk_proof(approve.desk_key(), rec)}
    from fleetmux.channels import actor
    bound = actor.channel_audit(r, cfg, 'desk', 'desk')

    def audit(event, **fields):
        try:
            bound(event, **fields)
        except Exception:  # noqa: BLE001  an unresolvable audit stream never blocks the answer
            pass
    try:
        answer = approve.answer_action(r, rec, ev, policy=policy, audit=audit)
    except Exception as exc:  # noqa: BLE001
        _err(stderr, 'channel approve: failed: ' + type(exc).__name__)
        return 1
    if answer.get('status') == 'accepted':
        _say(stdout, {'status': 'approved', 'pending_id': pid})
        return 0
    _err(stderr, 'channel approve: refused (' + str(answer.get('reason') or 'stale') + ')')
    return 1


def login_totp(args, *, stdin=None, stdout=None, stderr=None) -> int:
    """``channel login totp``: create ``config_dir()/totp.env`` (TOTP_SECRET, base32, 0600; dir 0700) once and print
    its ``otpauth://`` URI once, to add it to an authenticator app. An existing file is never shown or replaced."""
    import base64
    import secrets as _secrets
    from urllib.parse import quote
    _, stdout, stderr = _io(stdin, stdout, stderr)
    if getattr(args, 'account', None) or getattr(args, 'allow', None) or getattr(args, 'set', None) \
            or getattr(args, 'from_stdin', False):
        _err(stderr, 'channel login totp: takes no account and no options')
        return 2
    folder = channels.config_dir()
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    path = folder / 'totp.env'
    secret = base64.b32encode(_secrets.token_bytes(20)).decode().rstrip('=')
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        _err(stderr, 'channel login totp: already set up (' + str(path) + '); nothing changed, the secret is '
                     'not shown again. To replace it, move that file away yourself first.')
        return 0
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        fh.write('TOTP_SECRET=' + secret + '\n')
    label = quote('fleetmux:' + (os.environ.get('USER') or 'desk'))
    stdout.write('otpauth://totp/' + label + '?secret=' + secret + '&issuer=fleetmux&algorithm=SHA1&digits=6'
                 '&period=30\n')
    stdout.flush()
    _err(stderr, 'channel login totp: written ' + str(path) + ' (0600). Add the line above to your authenticator '
                 'app now; it is shown only this once.')
    return 0
