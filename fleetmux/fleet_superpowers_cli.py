"""``fleetmux superpowers <command>``: the command line of the fleet-superpowers inventory.

Commands are a table, ``COMMANDS``: name -> ``(help, add_arguments(parser), run(args) -> exit code)``. This
module registers ``refresh``, ``add``, ``show``, ``import``, ``check`` and the local ``save``. Another module adds
or replaces a command with ``register(name, help, add_arguments, run)``; the modules named in ``EXTENSIONS`` are
imported before the table is read, when they exist (``fleetmux/superpowers_peer.py`` holds ``sync``,
``save --also``, ``load`` and ``unload``).

Exit codes: 0 done, 1 refused (one line ``superpowers: <reason>`` on stderr), 2 usage.
"""
import argparse
import importlib
import json
import sys
from pathlib import Path

import yaml

from . import fleet_superpowers as fs, releases

COMMANDS = {}
EXTENSIONS = ('fleetmux.superpowers_peer',)


def register(name, help, add_arguments, run):
    """Add or replace one subcommand."""
    COMMANDS[name] = (help, add_arguments, run)


def _dump(value, as_json):
    if as_json:
        return json.dumps(value, ensure_ascii=False, indent=1)
    return yaml.safe_dump(value, sort_keys=False, default_flow_style=False, allow_unicode=True).rstrip('\n')


def _one(text, what):
    """A mapping with exactly one key, from YAML or JSON text: ``(name, value)``."""
    try:
        data = fs.load_yaml(text)
    except yaml.YAMLError as exc:
        raise fs.InventoryError(what + ' is not valid YAML or JSON: ' + str(exc)) from exc
    if not isinstance(data, dict) or len(data) != 1:
        raise fs.InventoryError(what + ' must be a mapping with exactly one name')
    name, value = next(iter(data.items()))
    return str(name), value


# -- refresh ---------------------------------------------------------------------------------------------

def _refresh_args(p):
    p.add_argument('--name', help='the machine name; required on the first refresh, later ones use the stored '
                   'name')
    p.add_argument('--json', action='store_true', help='print the written entry as JSON')


def cmd_refresh(args):
    from . import fleet_superpowers_refresh
    name, entry = fleet_superpowers_refresh.refresh(name=args.name)
    print(_dump({name: entry}, args.json))
    return 0


# -- add ------------------------------------------------------------------------------------------------

def _add_args(p):
    p.add_argument('name', help='the machine (another one, or this one to register its own ways in)')
    what = p.add_mutually_exclusive_group(required=True)
    what.add_argument('--ssh', help='append this reach entry: a Host alias of ~/.ssh/config or user@host')
    what.add_argument('--remove-reach', metavar='SSH', help='remove the one reach entry with this ssh')
    p.add_argument('--via', help='route kind of the reach entry, one of fleet.yaml#remote.reach_order (default for '
                   '--ssh: lan; with --remove-reach it narrows the match)')


def cmd_add(args):
    if args.remove_reach is not None:
        entry = fs.remove_reach(args.name, args.remove_reach, via=args.via)
    else:
        entry = fs.add_copy(args.name, args.ssh, via=args.via or 'lan')
    print(_dump({args.name: entry}, False))
    return 0


# -- releases ------------------------------------------------------------------------------------------

def stale_fleets(entry, link=None):
    """One line per running fleet of ``entry`` (this machine's) whose daemon runs another release than the one
    ``~/fleetmux-current`` points at (design.md section 56, "Releases"); read from each run now. A row whose run
    has no live daemon is skipped: right after a restart the stored row may still name the stopped run."""
    inst = releases.installed(link)
    out = []
    for row in (entry or {}).get('fleets') or [] if inst else []:
        if not isinstance(row, dict) or not row.get('running'):
            continue
        try:
            cfg = json.loads(Path(row['config']).expanduser().read_text())
        except (OSError, ValueError, KeyError, TypeError):
            cfg = None
        try:
            if not isinstance(cfg, dict) or not cfg.get('run') or not releases.daemon_running(cfg['run']):
                continue
        except OSError:  # the run directory is gone
            continue
        found = releases.running(cfg)
        if found and not releases.same(found, inst):
            name = str(row.get('name')) + ('/' + str(row['sub']) if row.get('sub') else '')
            out.append(releases.restart_line(name, found, inst))
    return out


# -- show -----------------------------------------------------------------------------------------------

def _show_args(p):
    which = p.add_mutually_exclusive_group()
    which.add_argument('--self', dest='self_', action='store_true', help='only this machine\'s own section')
    which.add_argument('--machines', action='store_true', help='one line per machine')
    which.add_argument('--workflows', action='store_true', help='one line per workflows entry')
    which.add_argument('--machine', metavar='NAME', help='one machine entry')
    which.add_argument('--workflow', metavar='NAME', help='one workflows entry')
    p.add_argument('--json', action='store_true', help='print JSON')


def cmd_show(args):
    if args.self_:
        name = fs.self_name()
        if name is None:
            raise fs.InventoryError('no self entry: run `fleetmux superpowers refresh --name <name>`')
        entry = fs.machine(name)
        print(_dump({name: entry}, args.json))
        if not args.json:  # YAML comments: the output stays the entry
            for line in stale_fleets(entry):
                print('# ' + line)
    elif args.machines:
        found = fs.machines()
        if args.json:
            print(_dump(found, True))
        for name, e in ({} if args.json else found).items():
            print('%s\t%s\t%s\t%s' % (name, e.get('source'), e.get('updated_at') or '-',
                                      ','.join(r.get('ssh', '') for r in e['reach']) or '-'))
    elif args.workflows:
        found = fs.workflows()
        if args.json:
            print(_dump(found, True))
        for name, e in ({} if args.json else found).items():
            print('%s\t%s' % (name, (e or {}).get('summary', '')))
    elif args.machine or args.workflow:
        name = args.machine or args.workflow
        found = fs.machine(name) if args.machine else fs.workflow(name)
        if found is None:
            raise fs.InventoryError('no %s %s' % ('machine' if args.machine else 'workflow', name))
        print(_dump({name: found}, args.json))
    elif args.json:
        print(_dump(fs.read(), True))
    else:
        path = fs.file_path()
        sys.stdout.write(path.read_text() if path.exists() else '')
    return 0


# -- import ---------------------------------------------------------------------------------------------

def _import_args(p):
    p.add_argument('source', choices=['-'], help='- : one section on stdin, {name: entry} as JSON or YAML')


def cmd_import(args):
    name, entry = _one(sys.stdin.read(), 'stdin')
    if name == fs.self_name():
        raise fs.InventoryError('%s is this machine; its section is written by refresh' % name)
    if not isinstance(entry, dict):
        raise fs.InventoryError('the section of %s must be a mapping' % name)
    fs.import_copy(name, entry)
    print('imported ' + name)
    return 0


# -- save (local) ---------------------------------------------------------------------------------------

def _save_args(p):
    p.add_argument('file', help='a YAML file holding {name: workflow}')


def save_file(file, path=None):
    """Store the one workflows entry of ``file`` in this machine's inventory; return its name."""
    try:
        text = open(file).read()
    except OSError as exc:
        raise fs.InventoryError('cannot read %s: %s' % (file, exc)) from exc
    name, entry = _one(text, str(file))
    fs.save_workflow(name, entry, path=path)
    return name


def cmd_save(args):
    print('saved ' + save_file(args.file))
    return 0


# -- check ----------------------------------------------------------------------------------------------

def _check_args(p):
    pass


def cmd_check(args):
    problems = fs.validate(fs.read(), require_self=True)
    for line in problems:
        print(line)
    if not problems:
        print('ok ' + str(fs.file_path()))
    return 1 if problems else 0


register('refresh', 'rewrite this machine\'s own machines entry from local facts', _refresh_args, cmd_refresh)
register('add', 'add or remove a reach entry of a machine (creates a minimal copy entry so the first '
         'sync can reach it)', _add_args, cmd_add)
register('show', 'print the file, this machine\'s section, or list and look up machines and workflows',
         _show_args, cmd_show)
register('import', 'store a section from stdin as a copy (never this machine\'s own)', _import_args, cmd_import)
register('save', 'write a workflows entry here', _save_args, cmd_save)
register('check', 'validate the whole file', _check_args, cmd_check)


# -- baseline check -------------------------------------------------------------------------------------

def _baseline_args(p):
    p.add_argument('action', choices=['check'], help='check: read-only; the baseline block hash, the workflows loaded '
                   'into each running fleet and the deviations their daemons report')
    p.add_argument('--before', metavar='HASH', help='fail (exit 1) when the block hash is not this one (taken before a '
                   'release switch)')
    p.add_argument('--before-report', metavar='FILE', help='the --json output taken before: also fail on a new '
                   'deviation or a workflow no longer loaded')
    p.add_argument('--json', action='store_true', help='print the report as JSON')


def cmd_baseline(args):
    from . import baseline_check
    report = baseline_check.upgrade_report()
    before = None
    if args.before_report:
        try:
            before = json.loads(Path(args.before_report).read_text() or 'null')
        except (OSError, ValueError) as exc:
            raise fs.InventoryError('--before-report unreadable: ' + str(exc)) from exc
    problems = baseline_check.upgrade_problems(args.before, before if isinstance(before, dict) else None, report)
    if args.json:
        print(json.dumps(dict(report, problems=problems), indent=2, sort_keys=True))
    else:
        print(baseline_check.upgrade_text(report, problems))
    return 1 if problems else 0


register('baseline', 'the switch baseline: `baseline check [--before HASH]` (read-only; before and after a '
         'release switch)', _baseline_args, cmd_baseline)


def _extensions():
    for name in EXTENSIONS:
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as exc:
            if exc.name != name:
                raise


def parser():
    _extensions()
    p = argparse.ArgumentParser(prog='fleetmux superpowers',
                                description='the fleet-superpowers resource inventory (docs/distributed-development.md)')
    sub = p.add_subparsers(dest='command', required=True)
    for name, (help, add_arguments, _run) in COMMANDS.items():
        add_arguments(sub.add_parser(name, help=help))
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return COMMANDS[args.command][2](args)
    except fs.InventoryError as exc:
        print('superpowers: ' + str(exc), file=sys.stderr)
        return 1
