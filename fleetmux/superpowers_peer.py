"""The cross-machine commands of ``fleetmux superpowers``: ``sync``, ``save --also``, ``load``, ``unload``
(docs/distributed-development.md, "Commands" and "Lifecycle details").

Registered in the command table of fleetmux/fleet_superpowers_cli.py (its ``EXTENSIONS`` imports this module).
Every ssh call goes through fleetmux/remote.py: ``remote.host`` (the machine from the inventory, its reach entries
in the fixed order of route kinds), ``remote.pick`` (the first of them that connects) and ``remote.ssh_argv`` (the
command line). ``far`` runs the command once on the picked entry with stdin / stdout attached to this process
(``remote.run`` inherits them, and ``sync`` must read the far section); a 255 is never retried.

``load`` on one machine (section "What load does on one machine"):

1. the repository has ``source.ref`` (fetched from ``source.from`` when missing) and is checked out at it, or at a
   later commit with the same applet files (``pinned_paths``);
2. the host fleet's roster file lies inside its run directory (open question 5: ``start`` does not copy it);
3. the rows are appended to that roster in place, every other byte kept: with ``host`` the supervisor worker row
   (when absent) and the ``subfleets:`` row; without ``host`` the ``applets:`` rows of ``roster_rows``, each with
   ``durable_state: false``. The roster before and after is kept in ``<run>/superpowers-load/<workflow>.json``;
4. wait until the applets are ``up`` (``fleetmux applet status``; through the parent's sub-fleet mirror for a host
   sub-fleet) and each runs in a process started by this load: a live pid in the host's record
   ``<run>/applets/<name>/pid`` that differs from the one recorded before the write. Or report why not.

``load`` refuses (before writing anything) while a process of an applet it would add still runs: it never stops a
process it did not start. A loaded workflow survives a restart of the host fleet: ``start`` and ``start --resume
--roster`` rebuild the run's roster copy and call ``reapply``, which appends the rows of every active record again
and rewrites the record against the new copy (``skipped/<workflow>.json`` when the load checks fail). ``unload`` puts the roster back byte for byte from that record, then waits until every
applet it removed has stopped (no live pid in the host's record, state no longer running), bounded by the row's
``detach_wait_s`` + ``stop_grace_s`` + ``STOP_EXTRA_S`` (a host sub-fleet: its ``drain_s`` instead of
``detach_wait_s``) or ``--wait``. At the bound it fails naming what still runs and keeps the record, so a second
``unload`` waits again. It never unloads another machine by itself (open question 11): ``unload --machine
<other>`` is the explicit step.
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

import yaml

from . import fleet_superpowers as fs, fleet_superpowers_cli as cli, releases, remote

WAIT_UP_S = 120.0  # seconds load waits for the applets to be up
POLL_S = 1.0       # seconds between two status reads
STOP_EXTRA_S = 5.0  # unload waits the row's detach_wait_s + stop_grace_s plus this for the process to end
RUNNING = ('starting', 'up', 'stalled', 'stopping')  # applet states with a process behind them
RECORDS = 'superpowers-load'
SKIPPED = 'skipped'  # <run>/superpowers-load/skipped/<workflow>.json: a record a restart could not load again


def _sleep(seconds):
    time.sleep(seconds)


def _clock():
    return time.monotonic()


# -- the far side ----------------------------------------------------------------------------------------

def target(name):
    """``remote.host(name)`` with its errors as ``InventoryError``."""
    try:
        return remote.host(name)
    except ValueError as exc:
        raise fs.InventoryError(str(exc)) from exc


def reach(name):
    """``(host, picked ssh destination)`` of machine ``name``; ``InventoryError`` when no entry connects."""
    host = target(name)
    dest = remote.pick(host)
    if dest is None:
        raise fs.InventoryError('no reach entry of %s connects (tried: %s)' % (name, ', '.join(host['reach'])))
    return host, dest


def far(name, args, input=None, capture=False, via=None):
    """Run ``fleetmux <args>`` once on machine ``name`` (``via``: a ``reach(name)`` result to reuse):
    ``(exit code, stdout or None)``. The command is never repeated, whatever its exit code."""
    host, dest = via or reach(name)
    return _ssh(remote.ssh_argv(dict(host, ssh=dest), list(args)), input, capture)


def _ssh(argv, input, capture):
    """The one ssh process of a far command: ``(exit code, stdout or None)``."""
    done = subprocess.run(argv, input=input, text=True, stdout=subprocess.PIPE if capture else None)
    return done.returncode, done.stdout


def _far_ok(name, args, input=None, capture=False, via=None):
    code, out = far(name, args, input=input, capture=capture, via=via)
    if code != 0:
        raise fs.InventoryError('%s: `fleetmux %s` exited %d' % (name, ' '.join(args), code))
    return out


# -- sync ------------------------------------------------------------------------------------------------

def sync(name, refresh=None):
    """Refresh this machine, replace the copy of ``name`` with its own section (``reach`` merged), send ours
    there (merged the same way on that side)."""
    from . import fleet_superpowers_refresh
    me, mine = (refresh or fleet_superpowers_refresh.refresh)()
    if name == me:
        raise fs.InventoryError('%s is this machine' % name)
    via = reach(name)  # resolved once: the copy written below may carry no reach of its own
    text = _far_ok(name, ['superpowers', 'show', '--self', '--json'], capture=True, via=via)
    far_name, entry = cli._one(text, name + ' show --self --json')
    if not isinstance(entry, dict):
        raise fs.InventoryError('%s sent a section that is not a mapping' % name)
    if far_name != name:  # workflows name machines verbatim: a second name would never match
        raise fs.InventoryError('%s calls itself %s: add it under that name (`fleetmux superpowers add %s --ssh '
                                '<dest>`) and sync that' % (name, far_name, far_name))
    fs.import_copy(name, entry)  # keeps the reach entries this machine has for it and adds the owner's
    _far_ok(name, ['superpowers', 'import', '-'], input=json.dumps({me: fs.machine(me)}, ensure_ascii=False),
            via=via)
    ours, theirs = (mine.get('fleetmux') or {}).get('release'), (entry.get('fleetmux') or {}).get('release')
    if ours and theirs and not releases.same(ours, theirs):
        print('superpowers: warning: %s has release %s installed, %s has %s; install the same release on both '
              'before `load`' % (me, ours, name, theirs), file=sys.stderr)
    return me


def _sync_args(p):
    p.add_argument('name', help='the other machine')


def cmd_sync(args):
    me = sync(args.name)
    print('synced %s <-> %s' % (me, args.name))
    return 0


# -- save [--also] ---------------------------------------------------------------------------------------

def _save_args(p):
    p.add_argument('file', help='a YAML file holding {name: workflow}; - reads stdin')
    p.add_argument('--also', action='append', default=[], metavar='NAME', help='save it on that machine too')


def cmd_save(args):
    text = sys.stdin.read() if args.file == '-' else None
    if text is None:
        try:
            text = Path(args.file).read_text()
        except OSError as exc:
            raise fs.InventoryError('cannot read %s: %s' % (args.file, exc)) from exc
    name, entry = cli._one(text, args.file)
    fs.save_workflow(name, entry)
    print('saved ' + name)
    for other in args.also:
        _far_ok(other, ['superpowers', 'save', '-'], input=text)
        print('saved %s on %s' % (name, other))
    return 0


# -- releases ------------------------------------------------------------------------------------------

def releases_here(fleets=None):
    """This machine's releases: ``{machine, installed, cli, fleets: [{fleet, config, release} or {fleet, error}]}``.
    ``fleets``: the host fleets to read, each picked as ``load`` picks it (``host_config``); default every
    running row of ``machines.<me>.fleets``. ``release`` is what the run's daemon runs (``releases.running``),
    read from the run now, not from the stored row."""
    me = _me()
    out = []
    if fleets:
        for fleet in fleets:
            try:
                path, cfg = host_config(me, fleet)
            except fs.InventoryError as exc:
                out.append({'fleet': fleet, 'error': str(exc)})
                continue
            out.append({'fleet': fleet, 'config': str(path), 'release': releases.running(cfg)})
    else:
        for row in (fs.machine(me) or {}).get('fleets') or []:
            if not isinstance(row, dict) or not row.get('running') or not row.get('config'):
                continue
            path = Path(row['config']).expanduser()
            cfg = _read_json(path)
            try:  # right after a restart the stored row may still name the stopped run
                if not cfg or not cfg.get('run') or not releases.daemon_running(cfg['run']):
                    continue
            except OSError:
                continue
            name = str(row.get('name')) + ('/' + str(row['sub']) if row.get('sub') else '')  # a sub-fleet
            out.append({'fleet': name, 'config': str(path), 'release': releases.running(cfg)})
    return {'machine': me, 'installed': releases.installed(), 'cli': releases.own(), 'fleets': out}


def _releases_args(p):
    p.add_argument('--fleet', action='append', default=[], metavar='NAME',
                   help='a host fleet, picked as load picks it (default: every running fleet of this machine)')
    p.add_argument('--json', action='store_true', help='print JSON')


def cmd_releases(args):
    found = releases_here(args.fleet)
    if args.json:
        print(json.dumps(found, ensure_ascii=False))
        return 0
    print('installed\t%s\ncli\t%s' % (found['installed'] or '-', found['cli'] or '-'))
    for row in found['fleets']:
        print('fleet %s\t%s\t%s' % (row['fleet'], row.get('release') or 'unknown',
                                      row.get('config') or row.get('error')))
    return 0


def release_table(workflow, machines, me):
    """[(machine, fleet, release or None, note)] of the host fleets ``load`` uses on ``machines``, read now:
    this machine in process, another one with `fleetmux superpowers releases --json` over its reach."""
    rows = []
    for m in machines:
        fleets = list(dict.fromkeys(i['fleet'] for i in workflow['applets'] if i['machine'] == m))
        if m == me:
            found = releases_here(fleets)
        else:
            argv = ['superpowers', 'releases', '--json'] + [w for f in fleets for w in ('--fleet', f)]
            try:
                code, text = far(m, argv, capture=True)
            except fs.InventoryError as exc:  # unknown, so --allow-mixed-release can still go past a machine down
                rows += [(m, f, None, str(exc)) for f in fleets]
                continue
            try:
                found = json.loads(text) if code == 0 else None
            except (TypeError, ValueError):
                found = None
            if not isinstance(found, dict):
                rows += [(m, f, None, '`fleetmux superpowers releases` exited %d there (a release without it is '
                          'older than this one)' % code) for f in fleets]
                continue
        for row in found.get('fleets') or []:
            rows.append((m, row.get('fleet'), row.get('release'), row.get('error') or ''))
    return rows


def mixed(rows, machines):
    """True when the releases of ``rows`` differ, or when ``machines`` holds more than one machine and a release
    is unknown (one machine alone: unknown releases are not compared)."""
    known = []
    for _, _, release, _ in rows:
        if release and not any(releases.same(release, k) for k in known):
            known.append(release)
    return len(known) > 1 or (len(set(machines)) > 1 and any(not r[2] for r in rows))


def _table(rows):
    return '\n'.join('  %s\t%s\t%s%s' % (m, f, r or 'unknown', ('\t' + n) if n else '') for m, f, r, n in rows)


def check_releases(workflow_name, workflow, machines, me, allow=False):
    """Refuse a load whose host fleets run different releases (``mixed``), listing machine, fleet and running
    release; with ``allow`` print the same table and go on."""
    rows = release_table(workflow, machines, me)
    if not mixed(rows, machines):
        return rows
    if not allow:
        raise fs.InventoryError('the host fleets of workflow %s run different releases (machine, fleet, running '
                                'release):\n%s\nInstall one release on every machine and restart these fleets '
                                'with their usual start command; --allow-mixed-release loads anyway (emergencies '
                                'only)' % (workflow_name, _table(rows)))
    print('superpowers: loading %s with mixed releases (--allow-mixed-release):\n%s' % (workflow_name, _table(rows)),
          file=sys.stderr)
    return rows


# -- the repository --------------------------------------------------------------------------------------

def _git(repo, *args):
    return subprocess.run(['git', '-C', str(repo)] + list(args), capture_output=True, text=True)


def source_repo(workflow):
    """``source.repo`` on this machine: absolute, or ``~/...`` under this user's home."""
    return Path(workflow['source']['repo']).expanduser()


def check_source(workflow, me):
    """The repository has ``source.ref`` (fetched from ``source.from`` over its reach entry when missing), and
    its checkout is at that commit or a later one with the same applet files (``pinned_paths``): ``source.ref``
    is an ancestor of HEAD and no pinned path differs between them. A pinned path changed in the working tree is
    refused too. Otherwise ``InventoryError``."""
    src = workflow['source']
    repo, ref, origin = source_repo(workflow), src['ref'], src['from']
    if not repo.is_dir():
        raise fs.InventoryError('source.repo %s is not a directory on %s' % (repo, me))
    if _git(repo, 'cat-file', '-e', ref + '^{commit}').returncode != 0:
        if origin == me:
            raise fs.InventoryError('commit %s is missing in %s and source.from is this machine' % (ref, repo))
        host = target(origin)
        dest = remote.pick(host)
        if dest is None:
            raise fs.InventoryError('commit %s is missing in %s and %s does not connect' % (ref, repo, origin))
        # The workflow's own spelling: git-upload-pack on the origin expands ~/ against that user's home.
        _git(repo, 'fetch', '--quiet', dest + ':' + src['repo'])
        if _git(repo, 'cat-file', '-e', ref + '^{commit}').returncode != 0:
            raise fs.InventoryError('commit %s is not in %s after fetching from %s' % (ref, repo, origin))
    head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    full = _git(repo, 'rev-parse', ref + '^{commit}').stdout.strip()
    fix = 'git -C %s checkout %s' % (shlex.quote(str(repo)), ref)
    pinned = pinned_paths(workflow, me, repo)
    missing = [p for p in pinned if _git(repo, 'cat-file', '-e', '%s:%s' % (full, p)).returncode != 0]
    if missing:
        raise fs.InventoryError('applet files not in %s (untracked or added later): %s' % (ref, ', '.join(missing)))
    if not head or head != full:
        if not head or _git(repo, 'merge-base', '--is-ancestor', full, head).returncode != 0:
            raise fs.InventoryError('%s is at %s, which does not contain %s: %s'
                                    % (repo, head[:12] or '?', ref, fix))
        changed = _changed(repo, full, head, pinned)
        if changed:
            raise fs.InventoryError('%s is at %s; the workflow needs %s and these applet files changed since: %s. '
                                    '%s' % (repo, head[:12], ref, ', '.join(changed), fix))
    dirty = _changed(repo, 'HEAD', None, pinned)
    if dirty:
        raise fs.InventoryError('%s has uncommitted changes to applet files: %s' % (repo, ', '.join(dirty)))


def _changed(repo, a, b, pinned):
    """Pinned paths that differ between commit ``a`` and commit ``b`` (None: the working tree and index);
    ``InventoryError`` when git cannot compare them (never read as "unchanged")."""
    if not pinned:
        return []
    done = _git(repo, 'diff', '--name-only', a, *([b] if b else []), '--', *pinned)
    if done.returncode != 0:
        raise fs.InventoryError('cannot compare applet files in %s: %s' % (repo, (done.stderr or '').strip()))
    return done.stdout.split()


def _row_paths(row, base):
    """What one ``applets:`` row runs, as absolute paths that exist: ``command`` arguments that name a file or a
    directory under ``cwd`` (``-m pkg.mod``: the top package ``pkg`` or ``pkg.py``), and ``params.file``."""
    cwd = base / Path(str(row.get('cwd', '.'))).expanduser()
    out, command = [], row.get('command') if isinstance(row.get('command'), list) else []
    for before, arg in zip([None] + command, command):
        if not isinstance(arg, str) or '{' in arg:
            continue
        names = ([arg.split('.')[0], arg.split('.')[0] + '.py'] if before == '-m' else [arg])
        out += [cwd / n for n in names]
    if isinstance(row.get('params'), dict) and isinstance(row['params'].get('file'), str):
        out.append(base / Path(row['params']['file']).expanduser())
    return [p for p in out if p.exists()]


def pinned_paths(workflow, me, repo):
    """Repository-relative paths of what this machine's applets run, sorted: the roster files a workflow names
    (``roster_rows``, ``host.roster``, ``host.supervisor_role``) and, from the ``applets:`` rows in them, the
    files and directories each row runs (``_row_paths``). ``load`` accepts a checkout after ``source.ref`` only
    when none of these changed."""
    top = Path(repo).resolve()
    found = []
    for item in workflow['applets']:
        if item.get('machine') != me:
            continue
        host = item.get('host')
        rosters = [repo / host['roster']] if host else [repo / item['roster_rows']]
        found += rosters + ([repo / host['supervisor_role']] if host else [])
        for roster in rosters:
            try:
                data = yaml.safe_load(roster.read_text()) or {}
            except (OSError, yaml.YAMLError):
                continue
            rows = data.get('applets') if isinstance(data, dict) else None
            for row in rows if isinstance(rows, list) else []:
                if isinstance(row, dict):
                    found += _row_paths(row, roster.parent)
    out = set()
    for path in found:
        try:
            out.add(str(Path(path).resolve().relative_to(top)))
        except ValueError:
            continue  # outside the repository: not pinned by source.ref
    return sorted(out)


# -- the host fleet and its roster -----------------------------------------------------------------------

def _read_config(path, fleet):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise fs.InventoryError('cannot read the config of fleet %s: %s' % (fleet, exc)) from exc


def host_config(me, fleet):
    """The run config of fleet ``fleet`` on this machine, from ``machines.<me>.fleets`` (``refresh``). A
    sub-fleet inherits its parent's fleet name, so with more than one row of that name the root service fleet
    is the host: a row with ``root: true`` and mode service (a row without ``root``, from an older refresh: mode
    service and no ``parent_config`` in its config); more than one such root is refused."""
    rows = (fs.machine(me) or {}).get('fleets') or []
    found = [(Path(r['config']).expanduser(), r) for r in rows if isinstance(r, dict) and r.get('name') == fleet]
    if not found:
        raise fs.InventoryError('no fleet %s in machines.%s.fleets: start it, then run '
                                '`fleetmux superpowers refresh`' % (fleet, me))
    if len(found) == 1:
        return current(found[0][0], _read_config(found[0][0], fleet))
    roots = []
    for path, row in found:
        if row.get('root') is False or row.get('mode') != 'service':
            continue
        try:
            cfg = json.loads(path.read_text())
        except (OSError, ValueError):
            continue  # an unreadable run cannot be the host
        if row.get('root') is True or not cfg.get('parent_config'):
            roots.append((path, cfg))
    configs = ', '.join(str(p) for p, _ in found)
    if not roots:
        raise fs.InventoryError('%d fleets named %s in machines.%s.fleets and none is a root service fleet '
                                '(configs: %s); start the host fleet, then run `fleetmux superpowers refresh`'
                                % (len(found), fleet, me, configs))
    if len(roots) > 1:
        raise fs.InventoryError('%d root service fleets named %s in machines.%s.fleets (configs: %s); stop the '
                                'extra ones, then run `fleetmux superpowers refresh`' % (len(roots), fleet, me, configs))
    return current(*roots[0])


def current(path, cfg):
    """The run that holds the fleet's loads now. The inventory row may still name the run before a restart (the
    daemon refreshes it about 30 s after start): a run whose daemon has stopped, while its fleet home's
    latest.json names another root run, hands over to that run, for ``load`` and ``unload`` alike. Records still
    in the stopped run are stale (``start`` carries them over and removes them; a start of an older release did
    not): they are removed with a note, so neither command reads them as loaded."""
    home = cfg.get('fleet_home')
    latest = (_read_json(Path(home).expanduser() / 'latest.json') if home else None) or {}
    new = latest.get('config') or (latest.get('run') and str(Path(latest['run']) / 'config.json'))
    if not new or not cfg.get('run') or Path(new).expanduser().resolve() == Path(path).expanduser().resolve():
        return path, cfg
    new_cfg = _read_json(Path(new).expanduser())
    if not new_cfg or not new_cfg.get('run') or new_cfg.get('parent_config'):
        return path, cfg
    from . import fleet as fleet_mod
    try:
        if fleet_mod.daemon_running(cfg['run']):
            return path, cfg  # still running: it is the host, whatever latest.json says
    except OSError:
        pass  # the run directory is gone
    stale = Path(cfg['run']) / RECORDS
    left = sorted(p.stem for p in list(stale.glob('*.json')) + list((stale / SKIPPED).glob('*.json')))
    if left:
        forget(stale)
        print('superpowers: removed the stale load records of the stopped run %s (%s); the fleet now runs %s, '
              'where the restart carried over what was still loaded' % (cfg['run'], ', '.join(left), new_cfg['run']),
              file=sys.stderr)
    return Path(new).expanduser(), new_cfg


def roster_in_run(cfg, fleet):
    """The roster the host daemon follows (``fleet.roster_file``); refused unless it lies inside the run
    directory (open question 5)."""
    from . import fleet as fleet_mod
    if not cfg.get('run'):
        raise fs.InventoryError('fleet %s has no run directory' % fleet)
    roster = Path(fleet_mod.roster_file(cfg)).expanduser().resolve()
    run = Path(cfg['run']).expanduser().resolve()
    if not roster.is_file():
        raise fs.InventoryError('fleet %s has no roster file %s' % (fleet, roster))
    if run not in roster.parents:
        raise fs.InventoryError('the roster of fleet %s (%s) is outside its run directory %s; an edit would '
                                'outlive the run (start it with a roster inside the run, e.g. --kit-fleet)'
                                % (fleet, roster, run))
    return roster


def _absolute(row, base):
    """An ``applets:`` row with ``cwd`` and ``params.file`` made absolute against the file it came from."""
    row = dict(row, cwd=str((base / Path(str(row.get('cwd', '.'))).expanduser()).resolve()))
    if isinstance(row.get('params'), dict) and isinstance(row['params'].get('file'), str):
        row['params'] = dict(row['params'], file=str((base / Path(row['params']['file']).expanduser()).resolve()))
    row['durable_state'] = False  # workflow applets keep no durable state (open question 6)
    return row


def rows_for(item, repo, roster_data):
    """``{block: [rows to append]}`` for one ``applets[]`` item of a workflow."""
    host = item.get('host')
    if host:
        out = {}
        workers = roster_data.get('workers') or []
        if not any(isinstance(w, dict) and w.get('name') == host['supervisor_slot'] for w in workers):
            out['workers'] = [{'name': host['supervisor_slot'], 'role': str(repo / host['supervisor_role'])}]
        out['subfleets'] = [{'name': host['subfleet'], 'supervisor_slot': host['supervisor_slot'],
                             'supervisor_role': str(repo / host['supervisor_role']),
                             'roster': str(repo / host['roster']), 'autostart': True, 'mode': 'service'}]
        return out
    path = repo / item['roster_rows']
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise fs.InventoryError('cannot read roster_rows %s: %s' % (path, exc)) from exc
    rows = data.get('applets') if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows or not all(isinstance(r, dict) for r in rows):
        raise fs.InventoryError('%s holds no applets: list' % path)
    return {'applets': [_absolute(r, path.parent) for r in rows]}


def append_rows(text, block, rows):
    """``text`` with ``rows`` appended to the top-level list ``block``; every other byte kept."""
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith('\n'):
        lines[-1] += '\n'
    dumped = ['- ' + yaml.safe_dump(r, default_flow_style=True, sort_keys=False, width=10 ** 6).strip() + '\n'
              for r in rows]  # one line per row
    start = next((i for i, ln in enumerate(lines) if ln.startswith(block + ':')), None)
    if start is None:
        return ''.join(lines) + block + ':\n' + ''.join('  ' + d for d in dumped)
    head = lines[start].split('#', 1)[0].strip()
    if head in (block + ':', block + ': []'):
        end = start + 1
        while end < len(lines) and (not lines[end].strip() or lines[end][0] in ' \t-#'):
            end += 1
        while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith('#')):
            end -= 1
        items = [ln for ln in lines[start + 1:end] if ln.lstrip().startswith('- ')]
        indent = (len(items[0]) - len(items[0].lstrip())) if items else 2
        if head == block + ': []':
            lines[start] = block + ':\n'
        return ''.join(lines[:end] + [' ' * indent + d for d in dumped] + lines[end:])
    raise fs.InventoryError('cannot add to %s: write it in block style (one "- " item per line)' % block)


def _write(path, text):
    tmp = path.with_name('.' + path.name + '.superpowers.tmp')
    tmp.write_text(text)
    os.replace(tmp, path)


def _record_path(cfg, workflow_name):
    return Path(cfg['run']) / RECORDS / (workflow_name + '.json')


def _skipped_path(cfg, workflow_name):
    return Path(cfg['run']) / RECORDS / SKIPPED / (workflow_name + '.json')


# -- waiting for up --------------------------------------------------------------------------------------

def connect(cfg):
    from . import fleet
    return fleet.connect(cfg)


def states(cfg, added):
    """{applet or sub-fleet name: state} of what a load added."""
    from . import applet_cli
    try:
        r = connect(cfg)
        return _states(applet_cli, cfg, r, added)
    except fs.InventoryError:
        raise
    except Exception as exc:  # a stopped or restarted run: its Redis is gone
        raise fs.InventoryError('fleet of run %s does not answer: %s' % (cfg.get('run'), exc)) from exc


def _states(applet_cli, cfg, r, added):
    out = {}
    for row in added.get('applets', []):
        code, got = applet_cli.cmd_status(cfg, r, argparse.Namespace(name=row['name']))
        out[row['name']] = (got.get('hash') or {}).get('state') or got.get('reason') or 'unknown'
    for row in added.get('subfleets', []):
        raw = r.hget('fx:subfleet:' + row['name'], 'applets') or '{}'
        found = json.loads(raw) if raw.startswith('{') else {}
        out.update({row['name'] + '/' + k: v for k, v in found.items()} or {row['name']: 'starting'})
    return out


def wait_up(cfg, added, wait=WAIT_UP_S, poll=POLL_S, sleep=None, clock=None, before=None, pids=None):
    """Until every added applet is ``up`` in a new process; ``InventoryError`` on crashloop / config_error or at
    the deadline. A state equal to ``before`` (read before the rows were written) is a leftover of an earlier load
    and only counts once it has changed. ``pids`` ({label: pid}, read before the rows were written): a process
    counts only when its pid is live and differs from that one."""
    sleep, clock = sleep or _sleep, clock or _clock
    deadline, before, moved, pids = clock() + wait, before or {}, set(), pids or {}
    while True:
        now = states(cfg, added)
        moved |= {k for k, v in now.items() if v != before.get(k)}
        bad = {k: v for k, v in now.items() if v in ('crashloop', 'config_error') and k in moved}
        if bad:
            raise fs.InventoryError('not up: ' + ', '.join('%s %s' % kv for kv in bad.items()))
        old = {label: pid for label, pid in processes(cfg, added).items()
               if pid is None or pid == pids.get(label) or not alive(pid)}
        if now and all(v == 'up' for v in now.values()) and not old:
            return now
        if clock() >= deadline:
            why = ['%s %s' % kv for kv in now.items()]
            why += ['%s has no process started by this load (pid %s)' % (k, v or '-') for k, v in old.items()
                    if now.get(k, 'up') == 'up']
            raise fs.InventoryError('not up after %gs: ' % wait + ', '.join(why))
        sleep(poll)


# -- the processes behind the rows -----------------------------------------------------------------------

def alive(pid):
    """True while process ``pid`` runs as an applet: the host starts each one as a session leader, so a recycled
    pid that leads no session is not it. Only ``ProcessLookupError`` means gone; a process this user may not
    inspect still runs."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return os.getsid(pid) == pid
    except ProcessLookupError:
        return False
    except OSError:
        return True


def recorded_pid(run, name):
    """The pid the host daemon recorded at the applet's last start (``<run>/applets/<name>/pid``, kept after the
    process exits), or None."""
    from . import applet_host
    try:
        return int((applet_host.run_dir({'run': str(run)}, name) / 'pid').read_text().strip())
    except (OSError, ValueError):
        return None


def _children(cfg, sub):
    """(run directory, applet names) of the running host sub-fleet ``sub``, from the parent's ``fx:subfleet:``
    hash (``run_dir`` and the ``applets`` mirror); (None, []) when it has none."""
    try:
        h = connect(cfg).hgetall('fx:subfleet:' + sub) or {}
        found = json.loads(h.get('applets') or '{}')
    except Exception:
        return None, []
    return h.get('run_dir') or None, sorted(found) if isinstance(found, dict) else []


def processes(cfg, added):
    """{label: recorded pid or None} of every applet process a load of ``added`` runs: the ``applets:`` rows in
    this run (label: the name), the applets of each host sub-fleet in its child run (label: ``<sub>/<name>``)."""
    out = {row['name']: recorded_pid(cfg['run'], row['name']) for row in added.get('applets', [])}
    for row in added.get('subfleets', []):
        run, names = _children(cfg, row['name'])
        out.update({row['name'] + '/' + n: recorded_pid(run, n) if run else None for n in names})
    return out


def _bound(cfg, added):
    """Seconds unload waits for the processes of ``added`` to end: per row ``detach_wait_s`` (a host sub-fleet:
    its ``drain_s``) + ``stop_grace_s`` + ``STOP_EXTRA_S``, roster defaults for what the row leaves out; the
    largest of them."""
    from . import roster
    out = [0.0]
    for row in added.get('applets', []):
        entry = dict(roster.APPLET_DEFAULTS, **row)
        out.append(float(entry['detach_wait_s']) + float(entry['stop_grace_s']) + STOP_EXTRA_S)
    for row in added.get('subfleets', []):
        try:
            grace = max([float(e['stop_grace_s']) for e in roster.load_applets(row['roster'])] or [0.0])
        except (OSError, ValueError, KeyError, TypeError):
            grace = float(roster.APPLET_DEFAULTS['stop_grace_s'])
        out.append(float(row.get('drain_s') or roster.DRAIN_S) + grace + STOP_EXTRA_S)
    return max(out)


def _still_running(cfg, added, pids):
    """{label: why} of the processes in ``pids`` that have not stopped: the pid is live, or (an ``applets:`` row)
    the host still reports a running state. A host that does not answer reports no state."""
    try:
        r = connect(cfg)
    except Exception:
        r = None
    out = {}
    for label, pid in pids.items():
        state = ''
        if r is not None and '/' not in label:
            try:
                from . import applet_host
                state = r.hget(applet_host.key(label), 'state') or ''
            except Exception:
                state = ''
        if alive(pid) or state in RUNNING:
            out[label] = 'pid %s, state %s' % (pid or '-', state or '-')
    return out


# -- load / unload on this machine -----------------------------------------------------------------------

def _workflow(name):
    workflow = fs.workflow(name)
    if workflow is None:
        raise fs.InventoryError('no workflow %s here (save it with `fleetmux superpowers save`)' % name)
    problems = fs.validate_workflow(name, workflow)
    if problems:
        raise fs.InventoryError('; '.join(problems))
    if workflow.get('kind') != 'workflow':
        raise fs.InventoryError('workflows.%s has kind: %s; a workflow has kind: workflow (edit the entry and save '
                                'it again)' % (name, workflow.get('kind')))
    return workflow


def _me():
    me = fs.self_name()
    if me is None:
        raise fs.InventoryError('no self entry: run `fleetmux superpowers refresh --name <name>`')
    return me


def load_here(name, wait=WAIT_UP_S, poll=POLL_S):
    """Load the applets of workflow ``name`` that run on this machine; return the record."""
    workflow, me = _workflow(name), _me()
    items = [i for i in workflow['applets'] if i['machine'] == me]
    if not items:
        raise fs.InventoryError('workflow %s has no applet for %s' % (name, me))
    check_source(workflow, me)
    repo = source_repo(workflow)
    plan = []  # every check of every item first: a refusal never leaves a half-written load
    for item in items:
        _, cfg = host_config(me, item['fleet'])
        releases.warn_cli(cfg)
        roster = roster_in_run(cfg, item['fleet'])
        record = _record_path(cfg, name)
        if record.exists():
            raise fs.InventoryError('workflow %s is already loaded in fleet %s: unload it first' % (name, item['fleet']))
        before = roster.read_text()
        added = rows_for(item, repo, yaml.safe_load(before) or {})
        pids = processes(cfg, added)
        running = ['%s is still running (pid %d)' % (k, v) for k, v in pids.items() if alive(v)]
        if running:  # load never stops a process it did not start
            raise fs.InventoryError('%s in fleet %s: wait for it to stop (unload %s first if it is loaded) and run '
                                    'load again' % ('; '.join(running), item['fleet'], name))
        after = before
        for block, rows in added.items():
            after = append_rows(after, block, rows)
        expected = yaml.safe_load(before) or {}
        for block, rows in added.items():
            expected[block] = list(expected.get(block) or []) + rows
        if yaml.safe_load(after) != expected:
            raise fs.InventoryError('cannot add the rows to %s without changing other rows' % roster)
        plan.append((cfg, roster, record, before, after, added, states(cfg, added), pids))  # states: it answers
    for cfg, roster, record, before, after, added, _, _ in plan:
        record.parent.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps({'roster': str(roster), 'before': before, 'after': after, 'added': added,
                                      'at': time.time()}))
        _skipped_path(cfg, name).unlink(missing_ok=True)  # loaded by hand after a restart skipped it
        _write(roster, after)
    for cfg, _, _, _, _, added, old, pids in plan:
        wait_up(cfg, added, wait=wait, poll=poll, before=old, pids=pids)
    return [p[5] for p in plan]


def unload_here(name, wait=None, poll=POLL_S):
    """Put back every host roster ``load`` changed for workflow ``name`` on this machine, in reverse order; then
    wait until every applet process those rows ran has stopped (``wait`` seconds, None: ``_bound`` of the rows).
    The records are removed only then; at the bound ``InventoryError`` names what still runs."""
    try:
        workflow = _workflow(name)
    except fs.InventoryError:
        dropped = _drop_skipped(name, _me())  # skipped at a restart because it no longer validates
        if dropped:
            return dropped
        raise
    me = _me()
    items = [i for i in workflow['applets'] if i['machine'] == me]
    done, dropped = [], []
    for item in reversed(items):
        _, cfg = host_config(me, item['fleet'])
        releases.warn_cli(cfg)
        record = _record_path(cfg, name)
        if not record.exists():
            skipped = _skipped_path(cfg, name)
            if skipped.exists():  # a restart could not load it again: nothing runs, only the listing goes
                skipped.unlink()
                dropped.append(str(skipped))
            continue
        saved = json.loads(record.read_text())
        roster = Path(saved['roster'])
        text = roster.read_text()
        if text != saved['after'] and text != saved['before']:  # before: an earlier unload that timed out
            raise fs.InventoryError('%s changed after load; remove the rows by hand: %s'
                                    % (roster, json.dumps(saved['added'])))
        pids = processes(cfg, saved['added'])  # before the write: a detached applet's keys are cleared
        if text != saved['before']:
            _write(roster, saved['before'])
        done.append((cfg, record, roster, saved['added'], pids))
    if not done and dropped:
        return dropped
    if not done:
        raise fs.InventoryError('workflow %s is not loaded on %s' % (name, me))
    bound = wait if wait is not None else max(_bound(cfg, added) for cfg, _, _, added, _ in done)
    deadline = _clock() + bound
    while True:
        left = {}
        for cfg, _, _, added, pids in done:
            left.update(_still_running(cfg, added, pids))
        if not left:
            break
        if _clock() >= deadline:
            raise fs.InventoryError('the rows are removed, but still running after %gs: %s; run `fleetmux superpowers '
                                    'unload %s` again to wait for them' % (bound, ', '.join(
                                        '%s (%s)' % kv for kv in sorted(left.items())), name))
        _sleep(poll)
    for _, record, _, _, _ in done:
        record.unlink()
    return [str(roster) for _, _, roster, _, _ in done]


# -- a restart of the host fleet -------------------------------------------------------------------------

def _read_json(path):
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _records(directory):
    """({workflow: record}, {workflow: skipped entry}) of one run's records directory."""
    directory = Path(directory)
    records = {p.stem: _read_json(p) for p in sorted(directory.glob('*.json'))}
    skipped = {p.stem: _read_json(p) for p in sorted((directory / SKIPPED).glob('*.json'))}
    def usable(v):  # a record whose shape no code below can trip on
        added = v.get('added') if v else None
        return isinstance(added, dict) and all(isinstance(rows, list) for rows in added.values())
    return {k: v for k, v in records.items() if usable(v)}, {k: v for k, v in skipped.items() if usable(v)}


def _present(added, data):
    """True when every row of ``added`` is in the parsed roster ``data``: the load is still in effect."""
    return all(row in (data.get(block) or []) for block, rows in added.items() for row in rows)


def _ordered(records, directory):
    """[(workflow, record)] in load order. ``unload`` takes the roster back only from the text a record wrote, so
    the order is the chain the records form (one's ``after`` is the next one's ``before``); a record outside the
    chain goes by its ``at`` (load time), else its file time."""
    def stamp(name):
        try:
            return (records[name].get('at') or (Path(directory) / (name + '.json')).stat().st_mtime, name)
        except OSError:
            return (0, name)
    rest, out = sorted(records, key=stamp), []
    while rest:
        afters = {records[n].get('after') for n in rest}
        head = next((n for n in rest if records[n].get('before') not in afters - {records[n].get('after')}), rest[0])
        out.append((head, records[head]))
        rest.remove(head)
    return out


def _check_record(name, me, saved, text, base):
    """The load checks of one carried record against roster ``text``: (new text, added) or ``InventoryError``."""
    if me is None:
        raise fs.InventoryError('no self entry: run `fleetmux superpowers refresh --name <name>`')
    workflow = _workflow(name)
    if not [i for i in workflow['applets'] if i['machine'] == me]:
        raise fs.InventoryError('workflow %s has no applet for %s any more' % (name, me))
    check_source(workflow, me)
    data = yaml.safe_load(text) or {}
    if not all(isinstance(r, dict) for rows in saved['added'].values() for r in rows):
        raise fs.InventoryError('the record holds a row that is not a mapping')
    names = {w.get('name') for w in data.get('workers') or [] if isinstance(w, dict)}
    added = {block: [r for r in rows if block != 'workers' or r.get('name') not in names]  # as rows_for does
             for block, rows in saved['added'].items()}
    added = {block: rows for block, rows in added.items() if rows}
    after = text
    for block, rows in added.items():
        after = append_rows(after, block, rows)
    expected = dict(data)
    for block, rows in added.items():
        expected[block] = list(expected.get(block) or []) + rows
    if yaml.safe_load(after) != expected:
        raise fs.InventoryError('cannot add the rows without changing other rows')
    from . import roster as roster_mod
    try:
        roster_mod.parse_entries(after.encode(), base)
    except ValueError as exc:
        raise fs.InventoryError('the roster with these rows is not valid: ' + ' '.join(str(exc).split())) from exc
    return after, added


def reapply(source, old_text, run, roster, say=print, refuse=None):
    """``start`` / ``start --resume --roster`` of a root fleet rebuilt the run's roster copy ``roster`` (None: the
    run has none). Load again every record of ``source`` (the records directory of the previous run, or of this run
    on a resume) that was in effect in the old copy ``old_text``, in load order, then every record an earlier
    restart skipped: append its rows to the copy, as ``load`` does, and write the record again against the new
    copy, so ``unload`` restores the right bytes. A record that fails the load checks (the workflow, the source
    checkout, a valid roster) is not started: it goes to ``<run>/superpowers-load/skipped/<workflow>.json`` with
    the reason, and ``fleetmux status`` lists it. A record whose rows were no longer in the old copy (an unload
    that timed out) is not loaded again. ``refuse``: a reason this run loads nothing (a task fleet hosts no
    applet); every record is kept as skipped, so the next service start of the fleet home loads it. A record that
    breaks in any way is skipped, never the start. True when the copy changed."""
    records, skipped = _records(source)
    if not records and not skipped:
        return False
    run, target = Path(run), Path(run) / RECORDS
    try:
        old = (yaml.safe_load(old_text) or {}) if old_text else {}
    except yaml.YAMLError:
        old = {}  # an unreadable old copy: nothing was in effect, every record is left where it was
    old = old if isinstance(old, dict) else {}
    active = [(n, r) for n, r in _ordered(records, source) if _present(r['added'], old)]
    stale = [n for n in records if n not in dict(active)]
    retry = sorted(((n, e) for n, e in skipped.items() if n not in records), key=lambda ne: (ne[1].get('at') or 0, ne[0]))
    try:
        me = fs.self_name()
    except Exception as exc:  # an unreadable inventory: every record is skipped with this reason
        me, refuse = None, refuse or 'cannot read the inventory: %s' % exc
    text = roster.read_text() if roster else None
    first, done, failed = text, [], []
    for name, saved in active + retry:
        try:
            if refuse:
                raise fs.InventoryError(refuse)
            if roster is None:
                raise fs.InventoryError('this run has no roster file (start it with --roster or --kit-fleet)')
            after, added = _check_record(name, me, saved, text, roster.parent)
        except Exception as exc:  # any failure of one record skips that record, never the start
            failed.append((name, saved, str(exc) if isinstance(exc, fs.InventoryError) else repr(exc)))
            continue
        done.append((name, {'roster': str(roster), 'before': text, 'after': after, 'added': added,
                            'at': saved.get('at') or time.time()}))
        text = after
    (target / SKIPPED).mkdir(parents=True, exist_ok=True)
    for name, record in done:
        (target / (name + '.json')).write_text(json.dumps(record))
        (target / SKIPPED / (name + '.json')).unlink(missing_ok=True)
        say('superpowers: workflow %s loaded again (%s)' % (name, ', '.join(
            str(r.get('name')) for rows in record['added'].values() for r in rows)))
    for name, saved, why in failed:
        (target / SKIPPED / (name + '.json')).write_text(json.dumps(dict(saved, reason=why)))
        (target / (name + '.json')).unlink(missing_ok=True)
        say('superpowers: workflow %s not loaded: %s. Fix it and run `fleetmux superpowers load %s`, or drop it '
            'with `fleetmux superpowers unload %s` (or delete %s)' % (name, why, name, name,
                                                                   target / SKIPPED / (name + '.json')))
    for name in stale:  # on a resume its file is this run's own: it waits for the processes and goes (unload)
        if Path(source).resolve() == target.resolve() and text is not None:
            (target / (name + '.json')).write_text(json.dumps(dict(records[name], roster=str(roster), before=text,
                                                                   after=text)))
    if done:
        _write(roster, text)
    return text != first


def forget(source):
    """The records of the previous run once the new run holds them (after ``start`` wrote latest.json): an early
    ``unload`` on a not yet refreshed inventory then finds nothing there instead of editing a stopped run."""
    source = Path(source)
    for path in list(source.glob('*.json')) + list((source / SKIPPED).glob('*.json')):
        path.unlink(missing_ok=True)


def run_loads(run):
    """``fleetmux status``: {loaded: [workflow], skipped: {workflow: reason}} of one run, None without records."""
    records, skipped = _records(Path(run) / RECORDS)
    if not records and not skipped:
        return None
    return {'loaded': sorted(records), 'skipped': {n: e.get('reason') or '?' for n, e in sorted(skipped.items())}}


def _drop_skipped(name, me):
    """``unload`` of a workflow that no longer validates: remove its skipped records from the root fleets of this
    machine (nothing of it runs). The paths removed."""
    out = []
    for row in (fs.machine(me) or {}).get('fleets') or []:
        if not isinstance(row, dict) or row.get('root') is False or not row.get('config'):
            continue
        try:
            _, cfg = current(Path(row['config']).expanduser(), _read_config(Path(row['config']).expanduser(),
                                                                            row.get('name')))
        except fs.InventoryError:
            continue
        path = _skipped_path(cfg, name)
        if cfg.get('run') and path.exists():
            path.unlink()
            out.append(str(path))
    return out


def _load_args(p):
    p.add_argument('workflow')
    p.add_argument('--machine', help='only this machine (default: every machine of the workflow, in order)')
    p.add_argument('--wait', type=float, default=WAIT_UP_S, help='seconds to wait for up (default %(default)s)')
    p.add_argument('--poll', type=float, default=POLL_S, help='seconds between status reads (default %(default)s)')
    p.add_argument('--allow-mixed-release', action='store_true',
                   help='emergencies only: load although the host fleets run different releases (prints them)')


def cmd_load(args):
    me = _me()
    workflow = _workflow(args.workflow)
    machines = list(dict.fromkeys(i['machine'] for i in workflow['applets']))
    if args.machine:
        machines = [args.machine]
    if args.machine != me:  # the calling machine compares them all; `--machine <self>` (the far half) does not
        every = list(dict.fromkeys(i['machine'] for i in workflow['applets']))
        check_releases(args.workflow, workflow, every, me, allow=args.allow_mixed_release)
    for m in machines:
        if m == me:
            load_here(args.workflow, wait=args.wait, poll=args.poll)
        else:
            _far_ok(m, ['superpowers', 'load', args.workflow, '--machine', m])
        print('loaded %s on %s' % (args.workflow, m))
    return 0


def _unload_args(p):
    p.add_argument('workflow')
    p.add_argument('--machine', help='that machine only, over remote (default: this machine only)')
    p.add_argument('--wait', type=float, default=None, help='seconds to wait for the applet processes to end '
                   '(default: per row detach_wait_s + stop_grace_s + %g; a host sub-fleet drain_s instead of '
                   'detach_wait_s)' % STOP_EXTRA_S)
    p.add_argument('--poll', type=float, default=POLL_S, help='seconds between status reads (default %(default)s)')


def cmd_unload(args):
    me = _me()
    if args.machine and args.machine != me:
        _far_ok(args.machine, ['superpowers', 'unload', args.workflow, '--machine', args.machine])
    else:
        unload_here(args.workflow, wait=args.wait, poll=args.poll)
    print('unloaded %s on %s' % (args.workflow, args.machine or me))
    return 0


cli.register('sync', 'refresh, take the copy of NAME from it, send our own section there', _sync_args, cmd_sync)
cli.register('save', 'write a workflows entry here, and with --also on that machine too', _save_args, cmd_save)
cli.register('releases', 'the release installed here and the one each host fleet runs', _releases_args,
             cmd_releases)
cli.register('load', 'load the applets of a workflow (every machine in order, the far ones over remote)',
             _load_args, cmd_load)
cli.register('unload', 'remove what load added on this machine (or on --machine, explicitly)',
             _unload_args, cmd_unload)
