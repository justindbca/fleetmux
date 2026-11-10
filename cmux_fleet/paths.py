"""Where fleetmux keeps per-user data: one root, ``~/.fleetmux`` (docs/design.md §50.1, user 2026-11-11).

Layout (new default <- legacy default):

* fleet home   ``~/.fleetmux``              <- ``~/.cmux-fleet``             runs/, latest.json, codex-home/, private
  fleet.yaml, quota.json, discovered.yaml, dsh-home/
* state        ``~/.fleetmux/state``        <- ``~/.local/state/cmux-fleet`` backend window registries, runtimes
* config       ``~/.fleetmux/config``       <- ``~/.config/fleetmux``        channel accounts, leases, security.yaml
* launchers    ``~/.fleetmux/launchers.env``<- ``~/.config/fleetmux/launchers.env`` (read by launchers/*.command)
* board        ``~/.fleetmux/board``        (unchanged, session_board.py)

Resolution, per item: an explicit environment variable wins (new or old name, as before). Otherwise the new
path when it is in use, else the legacy path when it exists, else the new path. "In use" for the fleet home
means one of ``HOME_MARKERS`` exists under ``~/.fleetmux`` (the root alone proves nothing: board/ lives there
too); for state / config it means the directory exists. A legacy path that is a symlink to the new one (left by
``fleetmux home migrate``) counts as migrated, not as legacy. Resolving is silent; ``warn_legacy`` prints one
stderr line per item per process and is called by the human-facing commands only (fleet.main).
"""
import errno
import fcntl
import os
import subprocess
import sys
from pathlib import Path

HOME_MARKERS = ('runs', 'latest.json', 'fleet.yaml', 'quota.json', 'discovered.yaml', 'dsh-home', 'codex-home')
# Names under the root that belong to other items: never moved in from the legacy fleet home.
RESERVED = ('board', 'state', 'config', 'launchers.env', 'fleets')
ENV = {'home': ('FLEETMUX_HOME', 'CMUX_FLEET_HOME'), 'state': ('FLEETMUX_STATE', 'CMUX_FLEET_STATE'),
       'config': ('FLEETMUX_CONFIG_DIR',), 'launchers': ('FLEETMUX_LAUNCHER_ENV',)}

_warned = set()


def root():
    return Path.home() / '.fleetmux'


def locations(item):
    """(new, legacy) default paths of one item."""
    home = Path.home()
    return {'home': (root(), home / '.cmux-fleet'),
            'state': (root() / 'state', home / '.local/state/cmux-fleet'),
            'config': (root() / 'config', home / '.config/fleetmux'),
            'launchers': (root() / 'launchers.env', home / '.config/fleetmux/launchers.env')}[item]


def _in_use(item, new):
    if item == 'home':
        return any(os.path.lexists(new / name) for name in HOME_MARKERS)
    return os.path.lexists(new)


def _migrated(legacy, new):
    """The legacy path is the symlink `home migrate` left behind (or any link to the new path)."""
    return legacy.is_symlink() and os.path.realpath(legacy) == os.path.realpath(new)


def default(item):
    """('new' | 'legacy', path) for an item, ignoring the environment."""
    new, legacy = locations(item)
    if _in_use(item, new):
        return 'new', new
    if legacy.exists() and not _migrated(legacy, new):
        return 'legacy', legacy
    return 'new', new


def _env(item):
    for name in ENV[item]:
        if os.environ.get(name):
            return Path(os.environ[name]).expanduser()
    return None


def resolve(item):
    return _env(item) or default(item)[1]


def default_home():
    """The default fleet home, never overridden by FLEETMUX_HOME: the private fleet.yaml, quota.json and the
    shared dsh-home live here for every fleet, including per-fleet homes such as ~/.cmux-fleet-dev."""
    return default('home')[1]


def home():
    return resolve('home')


def state_dir():
    return resolve('state')


def config_dir():
    return resolve('config')


def launcher_env():
    return resolve('launchers')


def warn_legacy(err=None):
    """One stderr line per item (per process) that falls back to its legacy default."""
    for item in ('home', 'state', 'config'):
        if item in _warned or _env(item):
            continue
        kind, path = default(item)
        new, legacy = locations(item)
        if kind == 'legacy':
            _warned.add(item)
            print("fleetmux: using legacy %s; run 'fleetmux home migrate' to move it to %s"
                  % (path, new), file=err or sys.stderr)
        elif item == 'home' and not legacy.is_symlink() and _in_use(item, legacy):
            _warned.add(item)  # split: new data started under ~/.fleetmux while the old home still holds runs
            print('fleetmux: using %s; %s also holds fleet data and is not read any more (not merged)'
                  % (new, legacy), file=err or sys.stderr)


# ------------------------------------------------------------------ fleetmux home migrate

def _ancestors():
    """This process and its parents (a `fleetmux home migrate` must not count itself as a running fleet)."""
    pids, pid = {os.getpid()}, os.getppid()
    for _ in range(64):
        if pid <= 1 or pid in pids:
            break
        pids.add(pid)
        p = subprocess.run(['ps', '-o', 'ppid=', '-p', str(pid)], capture_output=True, text=True)
        try:
            pid = int(p.stdout.strip())
        except ValueError:
            break
    return pids


def _fleet_process(args, data_dirs):
    names = [os.path.basename(a) for a in args]
    for i, name in enumerate(names):
        if name == 'fleet.py' and os.path.basename(os.path.dirname(args[i])) == 'cmux_fleet':
            return True
        if name in ('cmux-fleet', 'agent_bridge.py') or args[i] == 'cmux_fleet.fleet':
            return True
    if names and names[0] == 'fleetmux' and len(args) > 1 and args[1] in ('server', 'gui', 'attach', 'web'):
        return True
    if names and names[0] == 'tmux' and '-S' in args:
        sock = args[args.index('-S') + 1] if args.index('-S') + 1 < len(args) else ''
        return any(sock.startswith(str(d).rstrip('/') + '/') for d in data_dirs)
    return False


def running_fleets():
    """Descriptions of fleet processes on this machine and of default-home runs whose daemon holds its lock."""
    found = []
    data_dirs = {str(p) for item in ('home', 'state') for p in locations(item)}  # fleet tmux sockets live here
    data_dirs |= {os.path.realpath(p) for p in data_dirs}
    mine = _ancestors()
    p = subprocess.run(['ps', '-axww', '-o', 'pid=,args='], capture_output=True, text=True)
    for line in p.stdout.splitlines():
        row = line.strip().split(None, 1)
        if len(row) != 2 or not row[0].isdigit() or int(row[0]) in mine:
            continue
        if _fleet_process(row[1].split(), data_dirs):
            found.append('pid %s: %s' % (row[0], row[1][:160]))
    seen = set()
    for base in locations('home'):
        for lock in sorted(base.glob('runs/*/daemon.lock')):
            key = os.path.realpath(lock)
            if key in seen:
                continue
            seen.add(key)
            try:
                with open(lock, 'a+') as fh:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                found.append('daemon of ' + str(lock.parent))
            except OSError:
                continue
    return found


def _has_content(path):
    return path.is_dir() and any(path.iterdir())


def _dev(path):
    while not os.path.lexists(path):
        path = path.parent
    return os.stat(path).st_dev


def plan():
    """[(item, action, detail)]: action is 'move' (detail = steps), 'skip' (reason) or 'done'."""
    steps = []
    # launchers.env first: it sits inside the legacy config dir, which may move as a whole after it.
    for item in ('home', 'state', 'launchers', 'config'):
        new, legacy = locations(item)
        if not os.path.lexists(legacy) or legacy.is_symlink():
            continue  # nothing there, or already a link (ours or the operator's): left alone
        if item != 'home' and _dev(legacy) != _dev(new.parent):
            steps.append((item, 'skip', '%s and %s are on different filesystems; move it by hand' % (legacy, new)))
            continue
        if item == 'launchers':
            if os.path.lexists(new):
                steps.append((item, 'skip', 'both %s and %s exist; not merged' % (legacy, new)))
            else:
                steps.append((item, 'move', [('mkdir', new.parent, None), ('rename', legacy, new), ('link', legacy, new)]))
            continue
        if item == 'home':
            children = sorted(legacy.iterdir())
            if _dev(legacy) != _dev(new):
                steps.append((item, 'skip', '%s and %s are on different filesystems; move it by hand' % (legacy, new)))
                continue
            clash = [c.name for c in children if c.name in RESERVED or os.path.lexists(new / c.name)]
            if children and _in_use(item, new):
                steps.append((item, 'skip', 'both %s and %s hold fleet data; not merged' % (legacy, new)))
            elif clash:
                steps.append((item, 'skip', '%s holds %s, which already exist(s) under %s or name another item; '
                              'not merged' % (legacy, ', '.join(clash), new)))
            else:
                moves = [('rename', c, new / c.name) for c in children]
                steps.append((item, 'move', [('mkdir', new, None)] + moves + [('rmdir', legacy, None),
                                                                               ('link', legacy, new)]))
            continue
        if _has_content(new) or (os.path.lexists(new) and not new.is_dir()):
            steps.append((item, 'skip', 'both %s and %s exist with content; not merged' % (legacy, new)))
            continue
        pre = [('rmdir', new, None)] if new.is_dir() else [('mkdir', new.parent, None)]
        steps.append((item, 'move', pre + [('rename', legacy, new), ('link', legacy, new)]))
    return steps


def _say(op, a, b):
    return {'mkdir': 'mkdir -p %s', 'rmdir': 'rmdir %s (empty)', 'rename': 'mv %s -> %s',
            'link': 'ln -s %s %s'}[op] % ((a,) if op in ('mkdir', 'rmdir') else ((b, a) if op == 'link' else (a, b)))


def migrate(dry_run=False, out=None, running=None):
    """`fleetmux home migrate`: move legacy data dirs under ~/.fleetmux and leave a symlink at each legacy path.
    Never merges, never deletes (only an emptied legacy dir is rmdir'ed before its symlink). Returns the exit code:
    0 done / nothing to do, 1 some item skipped, 2 refused (a fleet is running)."""
    out = out or sys.stdout
    steps = plan()
    moves = [s for s in steps if s[1] == 'move']
    skips = [s for s in steps if s[1] == 'skip']
    if not steps:
        print('nothing to do: fleetmux data already lives under %s' % root(), file=out)
        return 0
    busy = (running or running_fleets)() if moves else []
    if busy and not dry_run:
        print('refused: fleets are running; stop them first, then run `fleetmux home migrate` again:', file=sys.stderr)
        for b in busy:
            print('  ' + b, file=sys.stderr)
        return 2
    for item, action, detail in steps:
        if action == 'skip':
            print('%s: skip: %s' % (item, detail), file=out)
            continue
        for op, a, b in detail:
            print('%s: %s%s' % (item, 'would ' if dry_run else '', _say(op, a, b)), file=out)
            if dry_run:
                continue
            if op == 'mkdir':
                Path(a).mkdir(parents=True, exist_ok=True)
            elif op == 'rmdir':
                os.rmdir(a)
            elif op == 'rename':
                try:
                    os.rename(a, b)
                except OSError as e:
                    if e.errno == errno.EXDEV:
                        print('%s: stopped: %s and %s are on different filesystems' % (item, a, b), file=sys.stderr)
                        return 1
                    raise
            elif op == 'link':
                os.symlink(b, a)
    if dry_run and busy:
        print('note: fleets are running now; the real run would refuse until they stop:', file=out)
        for b in busy:
            print('  ' + b, file=out)
    return 1 if skips else 0
