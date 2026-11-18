"""`fleetmux tabs`: a running fleet's tab titles and pane text, read from its fleetmux server without a screen.

    fleetmux tabs [--config C] [--json]                    every tab: page, group, title, type, pid, pane id
    fleetmux tabs read TARGET [--config C] [--lines N]     the last N text rows of one tab (TARGET: a pane id or a
                                                           title prefix); the blank rows below the text are not counted

Read-only: only ``hello``, ``page.list``, ``pane.list`` and ``pane.read`` reach the server, through the backend's own
manager (``FleetmuxSessionManager``: hello, uid and protocol checks, page binding). It never starts a server and never
writes to a pane. ``--config`` is a run's config.json or a join stub (default: this fleet home's latest.json). A root
config lists every page of the fleet tree (the root page and its sub-fleet pages); a sub-fleet config lists its own
page. Exit codes: 0 ok, 2 usage, no such tab or several tabs match, 3 the server cannot be read.
"""
import argparse
import json
import sys

GROUPS = ('supervisor', 'lower', 'workers')
DEFAULT_LINES = 50


class TabsError(Exception):
    def __init__(self, message, code=2):
        super().__init__(message)
        self.code = code


def config(path):
    """The run config ``path`` names; a join stub resolves to its service sub-fleet's config, or to its root."""
    from fleetmux import fleet, join
    try:
        raw = fleet.load(path)
        if join.is_stub(raw):
            stub = raw[join.KEY]
            raw = fleet.load(join.child_config_path(stub) if stub.get('kind') == 'service' else stub['root_config'])
    except join.JoinError as exc:
        raise TabsError(str(exc)) from exc
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise TabsError('cannot read the fleet config ' + str(path) + ': ' + str(exc)) from exc
    if not isinstance(raw, dict):
        raise TabsError(str(path) + ' is not a fleet config')
    return raw


def manager(cfg):
    from fleetmux import fleet
    backend = fleet.run_backend(cfg)
    if backend != 'fleetmux':
        raise TabsError('tabs reads the fleetmux server; this run uses the ' + backend + ' backend, not the fleetmux '
                        'backend')
    return fleet.manager_for(cfg)


def rows(mgr):
    """Every tab of ``mgr``'s fleet, root page first, then by group and tab order."""
    from fleetmux.backends.fleetmux import FleetmuxError
    panes = mgr._list() if mgr.page is not None else mgr._list_all()
    try:
        pages = [p for p in mgr._call('page.list').get('pages') or [] if isinstance(p, dict)]
    except FleetmuxError as exc:
        if exc.code != 'unknown_cmd':  # a server without pages lists only the root page
            raise
        pages = []
    order = {str(p.get('page') or ''): n for n, p in enumerate(pages)}
    titles = {str(p.get('page') or ''): str(p.get('title') or '') for p in pages}
    out = []
    for pane in panes:
        page = str(pane.get('page') or '')
        out.append({'pane': str(pane.get('pane') or ''), 'page': page, 'page_title': titles.get(page, ''),
                    'group': str(pane.get('group') or ''), 'index': pane.get('index'),
                    'title': str(pane.get('title') or ''), 'type': str(pane.get('type') or ''),
                    'pid': pane.get('pid'), 'focused': bool(pane.get('focused')), 'rows': pane.get('rows')})
    group = {g: n for n, g in enumerate(GROUPS)}
    return sorted(out, key=lambda r: (order.get(r['page'], -1 if r['page'] == '' else len(order)),
                                      group.get(r['group'], len(group)), r['index'] if isinstance(r['index'], int) else 0))


def render(table):
    """A plain text table: one tab per line."""
    head = ('PAGE', 'GROUP', 'TITLE', 'TYPE', 'PID', 'PANE')
    lines = [head] + [(('root' if not r['page'] else r['page_title'] or r['page']), r['group'], r['title'], r['type'],
                       str(r['pid'] if r['pid'] is not None else ''), r['pane']) for r in table]
    widths = [max(len(line[i]) for line in lines) for i in range(len(head))]
    return '\n'.join('  '.join(cell.ljust(w) for cell, w in zip(line, widths)).rstrip() for line in lines)


def pick(table, target):
    """The one tab ``target`` names: a pane id, else an exact title, else a title prefix."""
    for match in ([r for r in table if r['pane'] == target], [r for r in table if r['title'] == target],
                  [r for r in table if r['title'].startswith(target)]):
        if len(match) == 1:
            return match[0]
        if match:
            raise TabsError('several tabs match ' + repr(target) + ': ' + ', '.join(
                r['pane'] + ' ' + repr(r['title']) + (' (page ' + r['page'] + ')' if r['page'] else '')
                for r in match) + '; pass a pane id')
    raise TabsError('no tab matches ' + repr(target) + ' (`fleetmux tabs` lists them)')


def parsers():
    from fleetmux import fleet
    listing = argparse.ArgumentParser(prog='fleetmux tabs', description='List the tabs of a running fleet (read-only); '
                                      '`fleetmux tabs read TARGET` prints the text of one tab.')
    listing.add_argument('--config', default=str(fleet.LATEST), help='a run\'s config.json or a join stub '
                         '(default: this fleet home\'s latest.json)')
    listing.add_argument('--json', action='store_true', help='print the tabs as a JSON array')
    read = argparse.ArgumentParser(prog='fleetmux tabs read', description='Print the text of one tab (read-only).')
    read.add_argument('target', help='a pane id, or the start of a tab title')
    read.add_argument('--config', default=str(fleet.LATEST), help='as for `fleetmux tabs`')
    read.add_argument('--lines', type=int, default=DEFAULT_LINES, help='the last N rows (default %d)' % DEFAULT_LINES)
    return listing, read


def main(argv):
    from fleetmux.backends.fleetmux import FleetmuxError, ServerDown
    listing, reader = parsers()
    reading = argv[:1] == ['read']
    args = reader.parse_args(argv[1:]) if reading else listing.parse_args(argv)
    try:
        if reading and args.lines < 1:
            raise TabsError('--lines must be at least 1')
        mgr = manager(config(args.config))
        table = rows(mgr)
        if not reading:
            print(json.dumps(table, ensure_ascii=False, indent=1) if args.json else render(table))
            return 0
        tab = pick(table, args.target)
        screen = tab['rows'] if isinstance(tab['rows'], int) and tab['rows'] > 0 else 0
        # pane.read counts the blank rows below the text on screen (PROTOCOL.md 4.9): read them too, then drop them
        text = str(mgr._call('pane.read', pane=tab['pane'], lines=args.lines + screen).get('text') or '')
        text = '\n'.join(text.rstrip('\n').split('\n')[-args.lines:])
    except TabsError as exc:
        sys.stderr.write('fleetmux tabs: ' + str(exc) + '\n')
        return exc.code
    except ServerDown as exc:
        sys.stderr.write('fleetmux tabs: ' + str(exc) + '; is the fleet running?\n')
        return 3
    except FleetmuxError as exc:
        sys.stderr.write('fleetmux tabs: ' + str(exc) + '\n')
        return 2 if exc.code == 'no_such_pane' else 3
    sys.stdout.write(text + '\n')
    return 0
