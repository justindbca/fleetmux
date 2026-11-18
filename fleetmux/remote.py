"""`fleetmux remote` (design.md section 55): run a fleet command on another machine of yours over ssh.

The fleet (and every agent CLI login) lives on that machine; this one is a screen and a keyboard. Machines come
from the `machines` block of the fleet-superpowers inventory (docs/distributed-development.md "Transport"); the
private fleet.yaml#remote names the `default` one and may set `reach_order`. Nothing here opens a connection but
`ssh` itself: how this machine reaches the other one is the person's own ssh configuration.
"""
import shlex
import subprocess
import sys
import time

from fleetmux import fleet_config

DEFAULT_BIN = '~/fleetmux-current/bin/fleetmux'  # what `install_release.sh --activate` leaves on a machine
WHERE = 'fleet.yaml#remote'
CONNECT_TIMEOUT_S = 5  # ssh -o ConnectTimeout for the reach probe
# The kinds of route, in the one order every machine tries them (fleet.yaml#remote.reach_order overrides it).
REACH_ORDER = ('thunderbolt', 'lan', 'overlay', 'tunnel', 'tunnel-overseas')


def _machines():
    """The `machines` block of this machine's fleet-superpowers inventory: {name: entry}, in file order. A file
    that cannot be used is a ValueError naming the inventory; a missing file is no machines."""
    from fleetmux import fleet_superpowers
    try:
        return fleet_superpowers.machines()
    except fleet_superpowers.InventoryError as exc:
        raise ValueError('fleet-superpowers inventory: ' + str(exc)) from exc


def _dest(where, value):
    if not isinstance(value, str) or not value or value.startswith('-') or value.split() != [value]:
        raise ValueError(where + ' must be an ssh destination (a Host alias or user@host), got ' + repr(value))
    return value


def reach_order():
    """fleet.yaml#remote.reach_order: the route kinds in the order they are tried (REACH_ORDER when the block does
    not set it). A bad value is a ValueError."""
    value = (fleet_config.block('remote') or {}).get('reach_order')
    if value is None:
        return REACH_ORDER
    if (not isinstance(value, list) or not value or len(set(value)) != len(value)
            or not all(isinstance(v, str) and v.strip() for v in value)):
        raise ValueError(WHERE + '.reach_order must be a list of distinct route kinds, got ' + repr(value))
    return tuple(value)


def from_entry(name, entry):
    """A `machines.<name>` entry as {name, reach: [ssh destination, ...], bin}; ValueError says what to fix. The
    destinations are in the order they are tried: by the kind in `via` (reach_order), list order within a kind,
    and an unknown kind last."""
    where = 'machines.' + str(name)
    if not isinstance(entry, dict):
        raise ValueError(where + ' must be a mapping, got ' + repr(entry))
    reach = entry.get('reach') or []
    if not isinstance(reach, list):
        raise ValueError(where + '.reach must be an ordered list of {via, ssh}, got ' + repr(reach))
    dests = []
    for i, item in enumerate(reach):
        at = where + '.reach[' + str(i) + ']'
        if not isinstance(item, dict):
            raise ValueError(at + ' must be a mapping {via, ssh}, got ' + repr(item))
        dests.append((item.get('via'), _dest(at + '.ssh', item.get('ssh'))))
    order = reach_order()
    dests = [d for _, d in sorted(dests, key=lambda vd: order.index(vd[0]) if vd[0] in order else len(order))]
    if not dests:
        raise ValueError(where + '.reach is empty: that machine is not reachable from outside')
    fm = entry.get('fleetmux') or {}
    binary = fm.get('bin', DEFAULT_BIN) if isinstance(fm, dict) else None
    if not isinstance(binary, str) or not binary.strip():
        raise ValueError(where + '.fleetmux.bin must be the path of fleetmux on that machine, got ' + repr(binary))
    return {'name': str(name), 'reach': dests, 'bin': binary}


def host(name=None, machines=None):
    """The machine `name` (None: remote.default, or the only machine that is not this one) from `machines`
    (None: the inventory's block) as {name, reach, bin}; ValueError says what to fix."""
    block = fleet_config.block('remote') or {}
    if set(block) - {'default', 'reach_order'}:
        raise ValueError(WHERE + '.' + ', '.join(sorted(set(block) - {'default', 'reach_order'})) + ' is no longer '
                         'read: machines and their ssh destinations come from the machines block of '
                         'fleet-superpowers; keep only remote.default and remote.reach_order in '
                         + str(fleet_config.user_path()))
    where = ''
    if machines is None:
        from fleetmux import fleet_superpowers
        machines, where = _machines(), ' in ' + str(fleet_superpowers.file_path())
    others = sorted(n for n, e in machines.items() if not (isinstance(e, dict) and e.get('source') == 'self'))
    name = name or block.get('default') or (others[0] if len(others) == 1 else None)
    if name is None:
        raise ValueError(WHERE + '.default is not set and the inventory does not name exactly one other machine ('
                         + (', '.join(others) or 'none') + '): set remote.default or pass --host NAME')
    if name not in machines:
        raise ValueError("fleet-superpowers: no machine '" + str(name) + "'" + where + ' (machines: '
                         + (', '.join(sorted(machines)) or 'none: add one with `fleetmux superpowers add <name> '
                                                            '--ssh <destination>`') + ')')
    return from_entry(name, machines[name])


def probe_argv(dest, timeout=CONNECT_TIMEOUT_S):
    return ['ssh', '-o', 'ConnectTimeout=' + str(timeout), '--', dest, 'true']


def pick(target, timeout=CONNECT_TIMEOUT_S):
    """The first reach entry of `target` (already in reach_order, from_entry) whose `ssh ... true` exits 0, or None.
    The probe never reads stdin, so a piped `request -` stays intact for the one real command."""
    for dest in target['reach']:
        if subprocess.call(probe_argv(dest, timeout), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL) == 0:
            return dest
    return None


def _remote_bin(path):
    """`path` as the remote shell must read it: a leading ~/ becomes that machine's "$HOME"/ (a quoted ~ would
    not expand), the rest is quoted."""
    if path.startswith('~/'):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


def ssh_argv(target, args, home=None):
    """The ssh command for `fleetmux remote [--home DIR] ARGS...`. No ARGS = `view` (the live page), the only form
    that gets a terminal (-t): with one, a piped `task -` would have its stdin rewritten by the remote tty. `home`
    runs the far command with FLEETMUX_HOME=<home>; its leading ~/ is the far "$HOME"/, like `bin`."""
    args = list(args)
    words = [_remote_bin(target['bin'])] + [shlex.quote(a) for a in (args or ['view'])]
    if home:
        words.insert(0, 'FLEETMUX_HOME=' + _remote_bin(home))
    return ['ssh'] + ([] if args else ['-t']) + ['--', target['ssh'], ' '.join(words)]


def run(target, args, home=None):
    """Pick a reach entry, then run the command once on it; the exit code is ssh's, which is the remote command's
    (255: ssh could not connect). A 255 is never retried on another entry: the connection may have dropped
    mid-command, and a second run could repeat an `operator` or a `start` whose stdin is already consumed."""
    dest = pick(target)
    if dest is None:
        sys.stderr.write('fleetmux remote: no reach entry of ' + target['name'] + ' connects (tried: '
                         + ', '.join(target['reach']) + ')\n')
        return 255
    target = dict(target, ssh=dest)
    code = subprocess.call(ssh_argv(target, args, home))
    if code == 2 and not args:  # the far side's argparse does not know `view`
        sys.stderr.write("fleetmux remote: if the line above says invalid choice: 'view', " + target['name']
                         + ' runs an older release; install this release there too\n')
    return code


# -- the peer message set (docs/distributed-development.md "Message set") ---------------------------------------

PEER_PREFIX = 'peer.'  # request ids from a peer fleet: peer.<sender machine>.<topic>.<n>
PEER_REPLY = {'required': ['status', 'text', 'owner']}
PEER_STATUSES = ('ask', 'done', 'blocked')


def peer_id(machine, topic, n):
    """The request id a liaison on `machine` sends for report `n` of `topic`."""
    return PEER_PREFIX + str(machine) + '.' + str(topic) + '.' + str(n)


# -- fleetmux result <id> ----------------------------------------------------------------------------------

FOUND, UNKNOWN, PENDING = 0, 2, 3  # exit codes of `fleetmux result`
RESULT_POLL_S = 0.5


def result(r, rid, wait=0, poll=RESULT_POLL_S, sleep=time.sleep, clock=time.monotonic):
    """(exit code, JSON line or None) for request `rid`: FOUND once fx:result:<id> exists, PENDING while
    fx:req:<id> or a sub-fleet op fx:subfleet:op:<id> (attach / detach / up / down) is open (polled up to `wait`
    seconds), UNKNOWN otherwise. Reads only: fx:results is never touched, so other callers keep their results."""
    from fleetmux.fleet import REQ, RESULT
    from fleetmux.named_subfleet import OP
    deadline = clock() + max(float(wait or 0), 0.0)
    while True:
        raw = r.get(RESULT + rid)
        if raw:
            return FOUND, raw
        if not r.exists(REQ + rid) and not r.exists(OP + rid):
            raw = r.get(RESULT + rid)  # publish() sets the result before it deletes the request / op
            return (FOUND, raw) if raw else (UNKNOWN, None)
        left = deadline - clock()
        if left <= 0:
            return PENDING, None
        sleep(min(poll, left))


def result_cli(r, rid, wait=0):
    """`fleetmux result`: the line on stdout, a reason on stderr; returns the exit code."""
    code, raw = result(r, rid, wait)
    if code == FOUND:
        print(raw)
    else:
        why = ' is pending\n' if code == PENDING else ' is not a known request id\n'
        sys.stderr.write('fleetmux result: ' + rid + why)
    return code
