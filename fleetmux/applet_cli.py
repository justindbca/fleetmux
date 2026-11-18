"""``fleetmux applet <command>`` (docs/applet-interface.md section 5).

Every command takes ``--config <host config.json>`` (or the join stub of a service sub-fleet that ``fleetmux start``
attached: its own config.json is the host) and prints one JSON line. Exit code: 0 ok, 2 rejected,
3 pending (the convention of ``fleetmux operator``).

The commands are a dispatch table, ``COMMANDS``: name -> ``Command(run, setup, help)``.

- ``run(cfg, r, args) -> (exit_code, dict)``: ``cfg`` is the host's loaded config, ``r`` a connected client of the
  host Redis, ``args`` the parsed ``argparse.Namespace`` (``args.config`` is the config path).
- ``setup(parser)`` adds the command's own arguments; ``--config`` is added for every command.

Another module adds its commands with ``register(name, run, setup, help)``.
"""
import argparse
import json
import re
import sys
import time
from collections import namedtuple
from pathlib import Path

from fleetmux import applet_host

OK, REJECTED, PENDING = 0, 2, 3
STOP_WAIT_EXTRA_S = 5  # `applet stop` waits the row's stop_grace_s plus this for the process to end
Command = namedtuple('Command', 'run setup help')
COMMANDS = {}


def register(name, run, setup=None, help=''):
    COMMANDS[name] = Command(run, setup, help)


def rejected(reason, **extra):
    return REJECTED, dict({'status': 'rejected', 'reason': reason}, **extra)


def declared(cfg):
    """Applet entries of the roster this host follows now ({} without a roster or when it cannot be read), plus
    the built-in ones (relay_applet: the relay when relay.enabled on a root v2 service fleet)."""
    from fleetmux import fleet, relay_applet, roster
    try:
        found = roster.load_applets(fleet.roster_file(cfg))
    except (OSError, ValueError, KeyError, TypeError):
        found = []
    try:
        found = relay_applet.compose(found, cfg, cfg.get('workers') or [])
    except (ValueError, KeyError, TypeError):
        pass
    return {e['name']: e for e in found}


def _known(cfg, r, name):
    return name in declared(cfg) or r.exists(applet_host.key(name))


def _hb_age(h, now):
    try:
        return round(now - float(h['hb_at']), 1)
    except (KeyError, TypeError, ValueError):
        return None


# ------------------------------------------------------------------ commands
def cmd_list(cfg, r, args):
    now, names = time.time(), declared(cfg)
    rows = []
    for name in sorted(set(names) | set(applet_host.names(r))):
        h = r.hgetall(applet_host.key(name))
        rows.append({'name': name, 'state': h.get('state') or '', 'pid': h.get('pid') or '',
                     'hb_age_s': _hb_age(h, now), 'restarts': int(h.get('restarts') or 0),
                     'leases': r.scard(applet_host.key(name) + ':leases'), 'phase': h.get('phase') or '',
                     'declared': name in names})
    return OK, {'status': 'ok', 'applets': rows}


def cmd_status(cfg, r, args):
    name = args.name
    if not _known(cfg, r, name):
        return rejected('no applet ' + name + ' on this host')
    base = applet_host.key(name)
    h = r.hgetall(base)
    leases = [r.hgetall(base + ':lease:' + lid) or {'id': lid} for lid in sorted(r.smembers(base + ':leases'))]
    escalations = [dict(r.hgetall(k), key=k) for k in sorted(r.scan_iter(match=base + ':esc:*', count=200))]
    out = {'status': 'ok', 'applet': name, 'declared': name in declared(cfg), 'hash': h,
           'hb_age_s': _hb_age(h, time.time()), 'leases': leases, 'escalations': escalations}
    if h.get('state') in ('config_error', 'crashloop'):
        out['last_log_line'] = applet_host.last_line(cfg, name)
    return OK, out


def _control(action):
    def run(cfg, r, args):
        if args.name not in declared(cfg):
            return rejected('the roster declares no applet ' + args.name)
        answer = applet_host.request(r, args.name, action, wait=args.wait)
        code = {'ok': OK, 'pending': PENDING}.get(answer.get('status'), REJECTED)
        return code, answer
    return run


def _setup_control(p):
    p.add_argument('name')
    p.add_argument('--wait', type=float, default=30, help='seconds to wait for the daemon (default 30)')


def cmd_stop(cfg, r, args):
    """Stop, then wait (bounded by the row's ``stop_grace_s`` + STOP_WAIT_EXTRA_S) until the process has ended, so
    the answer is the final state. ``--no-wait-exit`` returns the daemon's first answer (``stopping``) at once."""
    code, answer = _control('stop')(cfg, r, args)
    if code != OK or answer.get('state') != 'stopping' or args.no_wait_exit:
        return code, answer
    bound = float(declared(cfg)[args.name].get('stop_grace_s', 30)) + STOP_WAIT_EXTRA_S
    state = applet_host.wait_stopped(r, args.name, bound)
    if state in applet_host.HELD_STATES:
        return OK, {'status': 'ok', 'applet': args.name, 'state': state, 'stopped': True}
    return PENDING, {'status': 'pending', 'applet': args.name, 'state': state, 'stopped': False,
                     'note': 'still not ended after ' + ('%g' % bound) + ' s; see `fleetmux applet status '
                     + args.name + '`'}


def _setup_stop(p):
    _setup_control(p)
    p.add_argument('--no-wait-exit', action='store_true',
                   help='return at once (state stopping) instead of waiting for the process to end')


def cmd_send(cfg, r, args):
    if not _known(cfg, r, args.name):
        return rejected('no applet ' + args.name + ' on this host')
    try:
        text = sys.stdin.read() if args.file == '-' else Path(args.file).expanduser().read_text()
        item = json.loads(text)
    except (OSError, ValueError) as exc:
        return rejected('not one JSON item: ' + str(exc)[:200])
    depth = r.rpush(applet_host.key(args.name) + ':inbox', json.dumps(item, ensure_ascii=False))
    return OK, {'status': 'ok', 'applet': args.name, 'queued': depth}


def cmd_param(cfg, r, args):
    if not _known(cfg, r, args.name):
        return rejected('no applet ' + args.name + ' on this host')
    try:
        value = json.loads(args.value)
    except ValueError as exc:
        return rejected('value is not JSON: ' + str(exc)[:200])
    encoded = json.dumps(value, ensure_ascii=False)
    r.hset(applet_host.key(args.name) + ':params', args.key, encoded)
    audit(cfg, r, 'applet.param_changed', applet=args.name, key=args.key, value=encoded, by='operator')
    return OK, {'status': 'ok', 'applet': args.name, 'key': args.key, 'value': value}


def cmd_clear_state(cfg, r, args):
    """Delete an applet's state directory. Lists what it holds; deletes only with ``--yes`` and only while no live
    process owns it (``host.json``). Without ``--yes`` it is a dry run that exits 0. Works with the host down
    (``r`` may be None)."""
    import shutil
    name = args.name
    if not re.fullmatch(applet_host.NAME_RE, name):
        return rejected('not an applet name: ' + name)
    spec = dict(declared(cfg).get(name) or {'name': name})
    path = applet_host.state_dir(cfg, spec)
    if not path.is_dir():
        return OK, {'status': 'ok', 'applet': name, 'state_dir': str(path), 'removed': [], 'note': 'no state directory'}
    files = sorted(str(p.relative_to(path)) for p in path.rglob('*') if not p.is_dir())
    try:
        owner = json.loads((path / applet_host.OWNER).read_text())
    except (OSError, ValueError):
        owner = None
    pid = owner.get('pid') if isinstance(owner, dict) else None
    running = None
    if isinstance(pid, int) and applet_host._leads_session(pid):
        running = ('applet ' + name + ' is running (pid ' + str(pid) + ', run ' + str(owner.get('run')) + '); '
                   'stop it first (`fleetmux applet stop ' + name + '`)')
    if not args.yes:  # a dry run: listed, nothing removed (exit 0; non-zero only for a real refusal or error)
        return OK, {'status': 'ok', 'applet': name, 'state_dir': str(path), 'files': files, 'removed': [],
                    'note': 'listed only, nothing removed; ' + (running + ' before --yes' if running
                                                                 else 'pass --yes to delete these files')}
    if running:
        return rejected(running, state_dir=str(path), files=files)
    shutil.rmtree(path)
    if r is not None:
        audit(cfg, r, 'applet.state_cleared', applet=name, state_dir=str(path), files=len(files))
    return OK, {'status': 'ok', 'applet': name, 'state_dir': str(path), 'removed': files}


def audit(cfg, r, event, **fields):
    """One audit row on the host's audit stream (the same shape the daemon writes)."""
    from fleetmux import runtime_v2
    bus = runtime_v2.bus_for(cfg, r)
    r.xadd(bus.audit_key, dict(fields, event=event, run_id=bus.run_id, at=str(int(time.time() * 1000))))


def _setup_name(p):
    p.add_argument('name')


def _setup_send(p):
    p.add_argument('name')
    p.add_argument('file', help='a JSON file, or - for stdin')


def _setup_clear_state(p):
    p.add_argument('name')
    p.add_argument('--yes', action='store_true', help='really delete (without it: list what would be removed)')


def _setup_param(p):
    p.add_argument('name')
    p.add_argument('key')
    p.add_argument('value', help='the new value as JSON')


register('list', cmd_list, None, 'applets of this host: state, pid, heartbeat age, restarts, open leases, phase')
register('status', cmd_status, _setup_name, 'one applet: its hash, open leases and escalations')
register('start', _control('start'), _setup_control, 'start an applet; clears crashloop / config_error')
register('stop', cmd_stop, _setup_stop, 'stop an applet (SIGTERM, grace, SIGKILL) and wait for the process to end')
register('restart', _control('restart'), _setup_control, 'stop, then start an applet')
register('send', cmd_send, _setup_send, "append one JSON item to the applet's inbox")
register('param', cmd_param, _setup_param, 'change one hot parameter of an applet')
register('clear-state', cmd_clear_state, _setup_clear_state, "delete an applet's state directory (only with --yes; "
         'refused while the applet runs); works with the host down')
OFFLINE = ('clear-state',)  # commands that read only files: they run with r=None when the host Redis is down
from fleetmux import applet_lease_cli  # noqa: E402,F401  (pause, resume, leases, revoke, answer: lease-1)


# ------------------------------------------------------------------ entry point
def parser():
    from fleetmux import fleet
    p = argparse.ArgumentParser(prog='fleetmux applet', description='Applets of a host fleet '
                                '(docs/applet-interface.md section 5). Output: one JSON line; exit 0 ok, '
                                '2 rejected, 3 pending.')
    sub = p.add_subparsers(dest='applet_cmd', required=True)
    for name, command in COMMANDS.items():
        cp = sub.add_parser(name, help=command.help)
        cp.add_argument('--config', default=str(fleet.LATEST), help="the host's config.json")
        if command.setup:
            command.setup(cp)
    return p


def main(argv=None):
    from fleetmux import fleet, join, releases
    args = parser().parse_args(argv)
    try:
        raw = fleet.load(args.config)
        if join.is_stub(raw):  # the service sub-fleet a `start` attached: its own config is the applet host
            args.config = str(join.applet_config(Path(args.config), raw))
            raw = fleet.load(args.config)
        cfg = fleet.pin_paths(raw)
        releases.warn_cli(cfg)  # design.md §56 "Releases": this CLI vs the running daemon
    except Exception as exc:
        cfg, code, out = None, *rejected('cannot read the host fleet config: ' + str(exc)[:200])
    r = None
    if cfg is not None:
        try:
            r = fleet.connect(cfg)
        except Exception as exc:
            if args.applet_cmd not in OFFLINE:
                cfg, code, out = None, *rejected('cannot reach the host fleet: ' + str(exc)[:200])
    if cfg is not None:
        try:
            code, out = COMMANDS[args.applet_cmd].run(cfg, r, args)
        finally:
            if r is not None:
                r.close(); r.connection_pool.disconnect()
    print(json.dumps(out, ensure_ascii=False, default=str))
    return code
