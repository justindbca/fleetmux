"""``fleetmux home migrate`` -- migration only (docs/design.md §50.1).

The project was called cmux-fleet before it was called fleetmux. This module is the one place that still knows
the old names, so a machine set up under them can be moved over; nothing else in fleetmux reads an old path or an
old environment variable. Everything below is migration only.

What it moves (never merges, never deletes data; each move leaves a symlink at the old path):

* ``~/.cmux-fleet``                 -> ``~/.fleetmux``                 (child by child: ``~/.fleetmux`` exists already)
* ``~/.local/state/cmux-fleet``     -> ``~/.fleetmux/state``
* ``~/.config/fleetmux/launchers.env`` -> ``~/.fleetmux/launchers.env``
* ``~/.config/fleetmux``            -> ``~/.fleetmux/config``
* ``~/.cmux-fleet-<name>``          -> ``~/.fleetmux/fleets/<name>``   (per-fleet homes; only real fleet homes)
* ``~/.local/state/cmux-fleet-<name>`` -> ``~/.fleetmux/fleets/<name>/state`` (only with its home, never into a
  ``state/`` the old home already holds)

Then, under ``~/.fleetmux`` and every ``~/.fleetmux/fleets/<name>``: the registry files and runtime dirs named
after the old session (``cmux-fleet*.json``, ``fleetmux/cmux-fleet*``, ``wezterm/cmux-fleet*``) get the new
session name, and every run record (``latest.json``, ``runs/*/config.json``, sub-fleet ``sub/*/config.json``) is
rewritten: moved directories to their new paths, ``/tmp/cmux-fleet-<x>.sock`` to ``/tmp/fleetmux-<x>.sock``, the
session names ``cmux-fleet`` / ``cmux-fleet:<id>`` to ``fleetmux`` / ``fleetmux:<id>`` and ``CMUX_FLEET_*``
environment names to ``FLEETMUX_*``, so the run records no longer depend on the symlinks. Old releases
(``~/cmux-fleet-releases``) are not moved. A fleet running on this machine -> refuse, exit 2.

``check()`` is the start-up guard of the human-facing commands: old data not yet migrated, an old environment
variable, or a fleet home under the old name -> one line saying what to do.
"""
import errno
import fcntl
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from fleetmux import paths

OLD = 'cmux-fleet'                       # migration only: the old product name
OLD_ENV = 'CMUX_FLEET_'                  # migration only: the old environment prefix
# Old environment names whose new name is not FLEETMUX_<same suffix>.
ENV_RENAMED = {'FLEETMUX': 'FLEETMUX_BIN', 'FLEETMUX_WEB': 'FLEETMUX_WEB_BIN', 'FLEETMUX_REAL': 'FLEETMUX_REAL_BIN',
               'FLEETMUX_THEME': 'FLEETMUX_THEME'}
HOME_MARKERS = ('runs', 'latest.json', 'fleet.yaml', 'quota.json', 'discovered.yaml', 'dsh-home', 'codex-home')
# Names under the root that belong to other items: never moved in from the old fleet home.
RESERVED = ('board', 'state', 'config', 'launchers.env', 'fleets')
OLD_SOCKET = re.compile(r'/tmp/' + OLD + r'-([A-Za-z0-9_.-]+)\.sock\Z')
_ENV_RE = re.compile(r'\b' + OLD_ENV + r'([A-Z0-9_]+)')
_SESSION_RE = re.compile(r'\A((?:[A-Za-z0-9_-]+:)?)' + OLD + r'(?=\Z|:)')


def new_env_name(old):
    """``CMUX_FLEET_X`` -> its ``FLEETMUX_*`` name."""
    suffix = old[len(OLD_ENV):]
    return ENV_RENAMED.get(suffix, 'FLEETMUX_' + suffix)


def _home():
    return Path.home()


def top_items():
    """[(item, old, new)] of the per-user directories (launchers.env before the config dir that holds it)."""
    h, r = _home(), paths.root()
    return [('home', h / ('.' + OLD), r), ('state', h / '.local/state' / OLD, r / 'state'),
            ('launchers', h / '.config/fleetmux/launchers.env', r / 'launchers.env'),
            ('config', h / '.config/fleetmux', r / 'config')]


def fleet_items():
    """[(item, old, new)] of the per-fleet homes and state dirs found under the old names."""
    h, out = _home(), []
    names = {p.name[len(OLD) + 2:] for p in h.glob('.' + OLD + '-*')}
    names |= {p.name[len(OLD) + 1:] for p in (h / '.local/state').glob(OLD + '-*')}
    for name in sorted(n for n in names if n and '/' not in n):
        new = paths.root() / 'fleets' / name
        out.append(('fleet ' + name, h / ('.%s-%s' % (OLD, name)), new))
        out.append(('fleet %s state' % name, h / '.local/state' / ('%s-%s' % (OLD, name)), new / 'state'))
    return out


def _is_home(path):
    return any(os.path.lexists(path / m) for m in HOME_MARKERS)


def _in_use(item, new):
    return _is_home(new) if item == 'home' else os.path.lexists(new)


def _migrated(old, new):
    """The old path is the symlink a migration left (or any link to the new path)."""
    return old.is_symlink() and os.path.realpath(old) == os.path.realpath(new)


def _pending(old):
    """Old data that a migration would move: a real file or directory (a symlink is left alone)."""
    return os.path.lexists(old) and not old.is_symlink()


def _has_content(path):
    return path.is_dir() and any(path.iterdir())


def _dev(path):
    while not os.path.lexists(path):
        path = path.parent
    return os.stat(path).st_dev


# ------------------------------------------------------------------ start-up guard

def problems():
    """(fatal, notes): one line each. Fatal: data or settings under the old names that this version no longer
    reads; notes: old data that is shadowed by data already under ~/.fleetmux (not merged)."""
    fatal, notes, waiting = [], [], []
    for item, old, new in top_items():
        if not _pending(old):
            continue
        if not _in_use(item, new):
            waiting.append(str(old))
        elif item == 'home' and _is_home(old):
            notes.append('fleetmux: using %s; %s also holds fleet data and is not read (not merged)' % (new, old))
    for var in ('FLEETMUX_HOME', 'FLEETMUX_STATE'):
        value = os.environ.get(var)
        if not value:
            continue
        p = Path(value).expanduser()
        if any(part.startswith(OLD) or part.startswith('.' + OLD) for part in p.parts):
            fatal.append('fleetmux: %s=%s uses the old name; use ~/.fleetmux/fleets/<name> (state: <home>/state); '
                         "'fleetmux home migrate' moves existing fleet homes there" % (var, value))
            continue
        for item, old, target in fleet_items():
            if target == p and not os.path.lexists(p) and _pending(old):
                waiting.append(str(old))
    if waiting:
        fatal.append("fleetmux: data under the old name in %s is not read any more; stop all fleets and run "
                     "'fleetmux home migrate' to move it under %s" % (', '.join(waiting), paths.root()))
    for key in sorted(os.environ):
        if key.startswith(OLD_ENV) and os.environ[key] and os.environ.get(new_env_name(key)) != os.environ[key]:
            fatal.append('fleetmux: %s is not read any more; set %s instead' % (key, new_env_name(key)))
    return fatal, notes


def check(fatal=True, err=None):
    """Print the lines of ``problems()``; exit 2 on a fatal one when ``fatal`` (start / resume / setup / gui)."""
    bad, notes = problems()
    for line in bad + notes:
        print(line, file=err or sys.stderr)
    if bad and fatal:
        raise SystemExit(2)


# ------------------------------------------------------------------ running fleets

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
    """A fleet process of this or an older release: the fleet CLI / daemon / actor, a bridge, the fleetmux server,
    or anything holding a path inside a directory the migration moves (e.g. a fleet's ``tmux -S`` socket)."""
    names = [os.path.basename(a) for a in args]
    old_package = OLD.replace('-', '_')
    for i, name in enumerate(names):
        if name == 'fleet.py' and os.path.basename(os.path.dirname(args[i])) in ('fleetmux', old_package):
            return True
        if name in ('fleetmux-py', OLD, 'agent_bridge.py') or args[i] in ('fleetmux.fleet', old_package + '.fleet'):
            return True
    if names and names[0] == 'fleetmux' and len(args) > 1 and args[1] in ('server', 'gui', 'attach', 'web'):
        return True
    return any(a == d or a.startswith(d + '/') for a in args for d in data_dirs)


def running_fleets(items=None):
    """Descriptions of fleet processes on this machine and of runs whose daemon holds its lock."""
    items = top_items() + fleet_items() if items is None else items
    data_dirs = {str(old).rstrip('/') for _, old, _ in items if _pending(old)}
    data_dirs |= {os.path.realpath(d) for d in data_dirs}
    found = []
    mine = _ancestors()
    p = subprocess.run(['ps', '-axww', '-o', 'pid=,args='], capture_output=True, text=True)
    for line in p.stdout.splitlines():
        row = line.strip().split(None, 1)
        if len(row) != 2 or not row[0].isdigit() or int(row[0]) in mine:
            continue
        if _fleet_process(row[1].split(), data_dirs):
            found.append('pid %s: %s' % (row[0], row[1][:160]))
    seen = set()
    for base in _homes(items):
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


def _is_home_item(item):
    return item == 'home' or (item.startswith('fleet ') and not item.endswith(' state'))


def _new_homes():
    return [paths.root()] + sorted((paths.root() / 'fleets').glob('*'))


def _homes(items):
    """Every fleet home, old and new: where run records and daemon locks live."""
    return _new_homes() + [old for item, old, _ in items if _is_home_item(item) and _pending(old) and old.is_dir()]


# ------------------------------------------------------------------ plan

def plan(items=None):
    """[(item, action, detail)]: action is 'move' (detail = steps) or 'skip' (reason)."""
    items = top_items() + fleet_items() if items is None else items
    steps, homes = [], {}
    for item, old, new in items:
        if item.startswith('fleet ') and _is_home_item(item):
            homes[item] = old  # listed just before its state item
        if not _pending(old):
            continue  # nothing there, or already a link (ours or the operator's): left alone
        if item == 'home':
            steps.append(_plan_root(old, new))
            continue
        if item.endswith(' state') and item != 'state':
            home_item = item[:-len(' state')]
            old_home = homes.get(home_item)
            if old_home is not None and _pending(old_home):
                if not any(s[0] == home_item and s[1] == 'move' for s in steps):
                    steps.append((item, 'skip', 'its fleet home %s is not moved (see above); not merged' % old_home))
                    continue
                if os.path.lexists(old_home / 'state'):
                    steps.append((item, 'skip', 'both %s and %s exist; not merged' % (old_home / 'state', old)))
                    continue
        if item != 'home' and _is_home_item(item) and not _is_home(old):
            steps.append((item, 'skip', '%s is not a fleet home (none of %s); move it by hand'
                          % (old, ', '.join(HOME_MARKERS))))
            continue
        if _dev(old) != _dev(new.parent):
            steps.append((item, 'skip', '%s and %s are on different filesystems; move it by hand' % (old, new)))
            continue
        if item == 'launchers':
            if os.path.lexists(new):
                steps.append((item, 'skip', 'both %s and %s exist; not merged' % (old, new)))
            else:
                steps.append((item, 'move', [('mkdir', new.parent, None), ('rename', old, new), ('link', old, new)]))
            continue
        if _has_content(new) or (os.path.lexists(new) and not new.is_dir()):
            steps.append((item, 'skip', 'both %s and %s exist with content; not merged' % (old, new)))
            continue
        pre = [('rmdir', new, None)] if new.is_dir() else [('mkdir', new.parent, None)]
        steps.append((item, 'move', pre + [('rename', old, new), ('link', old, new)]))
    return steps


def _plan_root(old, new):
    """~/.fleetmux exists already (board/, fleets/): the old home moves child by child."""
    children = sorted(old.iterdir())
    if _dev(old) != _dev(new.parent):
        return 'home', 'skip', '%s and %s are on different filesystems; move it by hand' % (old, new)
    clash = [c.name for c in children if c.name in RESERVED or os.path.lexists(new / c.name)]
    if children and _is_home(new):
        return 'home', 'skip', 'both %s and %s hold fleet data; not merged' % (old, new)
    if clash:
        return 'home', 'skip', ('%s holds %s, which already exist(s) under %s or name another item; not merged'
                                % (old, ', '.join(clash), new))
    moves = [('rename', c, new / c.name) for c in children]
    return 'home', 'move', [('mkdir', new, None)] + moves + [('rmdir', old, None), ('link', old, new)]


def _say(op, a, b):
    return {'mkdir': 'mkdir -p %s', 'rmdir': 'rmdir %s (empty)', 'rename': 'mv %s -> %s',
            'link': 'ln -s %s %s'}[op] % ((a,) if op in ('mkdir', 'rmdir') else ((b, a) if op == 'link' else (a, b)))


# ------------------------------------------------------------------ session names and run records

def _new_name(name):
    """``cmux-fleet`` / ``cmux-fleet:<id>`` / ``cmux-fleet_<id>`` (and the same + suffix) -> the fleetmux name."""
    return 'fleetmux' + name[len(OLD):] if name.startswith(OLD) else None


def renames(state_dirs):
    """[(old, new)] of session-named registry files and runtime dirs in ``state_dirs``."""
    out = []
    for state in state_dirs:
        for d in (state, state / 'fleetmux', state / 'wezterm'):
            if not d.is_dir():
                continue
            for p in sorted(d.iterdir()):
                new = _new_name(p.name)
                if new and not p.is_symlink():
                    out.append((p, d / new))
    return out


def run_records(home):
    """``latest.json`` and every run / sub-fleet ``config.json`` under one fleet home."""
    found = [home / 'latest.json'] if (home / 'latest.json').is_file() else []
    todo = sorted((home / 'runs').glob('*')) if (home / 'runs').is_dir() else []
    while todo:
        run = todo.pop(0)
        if (run / 'config.json').is_file():
            found.append(run / 'config.json')
        if (run / 'sub').is_dir():
            todo.extend(sorted((run / 'sub').glob('*')))
    return found


def _variants(path):
    """The spellings a run record may hold for one path: as given, and with its parent's links resolved."""
    path = Path(path)
    return {str(path), str(Path(os.path.realpath(path.parent)) / path.name)}


def path_map(items, also=()):
    """[(old prefix, new prefix)], longest first: every migrated directory (old path now a link to the new one)
    plus ``also`` (moves only planned, for --dry-run)."""
    pairs = {}
    for _, old, new in items:
        if _migrated(old, new) or (old, new) in also:
            for v in _variants(old):
                pairs[v] = str(new)
    return sorted(pairs.items(), key=lambda kv: -len(kv[0]))


def rewrite(value, pmap):
    """One run-record value with the old names replaced (see the module doc). Dict keys too (environment maps)."""
    if isinstance(value, dict):
        return {rewrite(k, pmap): rewrite(v, pmap) for k, v in value.items()}
    if isinstance(value, list):
        return [rewrite(v, pmap) for v in value]
    if not isinstance(value, str):
        return value
    for old, new in pmap:
        if value == old or value.startswith(old + '/'):
            value = new + value[len(old):]
            break
    m = OLD_SOCKET.fullmatch(value)
    if m:
        value = '/tmp/fleetmux-%s.sock' % m.group(1)
    value = _SESSION_RE.sub(r'\1fleetmux', value)  # session names, also as a workspace id 'fleetmux:<session>'
    return _ENV_RE.sub(lambda m: new_env_name(m.group(0)), value)


def record_changes(homes, pmap):
    """[(path, new data)] of the run records under ``homes`` that the rewrite changes."""
    out = []
    for home in homes:
        for path in run_records(home):
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            new = rewrite(data, pmap)
            if new != data:
                out.append((path, new))
    return out


def _write_json(path, data):
    tmp = path.with_name(path.name + '.migrate.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    os.chmod(tmp, os.stat(path).st_mode & 0o7777)
    os.replace(tmp, path)


def _after_moves(items):
    homes = _new_homes()
    return renames([h / 'state' for h in homes]), record_changes(homes, path_map(items))


# ------------------------------------------------------------------ migrate

def migrate(dry_run=False, out=None, running=None):
    """`fleetmux home migrate`. Returns the exit code: 0 done / nothing to do, 1 some item skipped, 2 refused
    (a fleet is running). Never merges, never deletes data (only an emptied old dir is rmdir'ed before its
    symlink)."""
    out = out or sys.stdout
    items = top_items() + fleet_items()
    steps = plan(items)
    moves = [s for s in steps if s[1] == 'move']
    skips = [s for s in steps if s[1] == 'skip']
    if dry_run:  # nothing has moved yet: look where the data is now, as if the planned moves were done
        planned = {(s[2][-1][1], s[2][-1][2]) for s in moves}  # each move ends with ('link', old, new)
        olds = {item: old for item, old, new in items if (old, new) in planned}
        homes = _new_homes() + [old for item, old in olds.items() if _is_home_item(item)]
        states = [h / 'state' for h in homes] + [old for item, old in olds.items() if item.endswith('state')]
        names, records = renames(states), record_changes(homes, path_map(items, planned))
    else:
        names, records = _after_moves(items)
    if not steps and not names and not records:
        print('nothing to do: fleetmux data already lives under %s' % paths.root(), file=out)
        return 0
    busy = (running or running_fleets)() if moves or names or records else []
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
    if not dry_run:  # the moved data is under ~/.fleetmux now
        names, records = _after_moves(items)
    for a, b in names:
        if os.path.lexists(b):
            print('session name: skip: %s exists; %s left as is' % (b, a), file=out)
            skips.append(('session name', 'skip', str(a)))
            continue
        print('session name: %s%s' % ('would ' if dry_run else '', _say('rename', a, b)), file=out)
        if not dry_run:
            os.rename(a, b)
    for path, data in records:
        print('run record: %srewrite old names in %s' % ('would ' if dry_run else '', path), file=out)
        if not dry_run:
            _write_json(path, data)
    if dry_run and busy:
        print('note: fleets are running now; the real run would refuse until they stop:', file=out)
        for b in busy:
            print('  ' + b, file=out)
    return 1 if skips else 0
