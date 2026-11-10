#!/usr/bin/env python3.14
"""A fake ``wezterm`` CLI: no WezTerm needed (design §7.2).

Every invocation appends its argv to ``$FAKE_WZ_STATE/argv.log`` and mutates ``$FAKE_WZ_STATE/state.json``
(window / tab / pane tables plus the pane's text buffer), so the tests can read back exactly what the
backend asked for and assert the resulting topology.

It mimics the real CLI *as measured on 20240203* (impl-wezterm/probe-report.md §2):

* ``cli list --format json`` prints one object per pane with window_id / tab_id / pane_id / workspace /
  tab_title / title / cwd / is_active / tty_name / left_col / top_row.
* ``cli spawn [--new-window] [--pane-id P] [--workspace W] [--cwd D] -- PROG...`` prints the new pane id.
  ``--new-window`` opens a new window with one tab; ``--pane-id`` opens a new tab in that pane's window.
* ``cli split-pane --pane-id P (--right|--bottom) [--percent N] [--cwd D] [--move-pane-id M] -- PROG``
  prints the new pane id and puts it beside P (right: left_col, bottom: top_row).
* ``cli send-text [--pane-id P] [--no-paste] TEXT`` appends TEXT to the pane buffer; ``--no-paste``
  appends the raw bytes, otherwise it wraps them in ESC[200~ ... ESC[201~ (bracketed paste), which is
  what the real terminal does when the foreground program enabled ?2004h.
* ``cli get-text --pane-id P [--start-line -N]`` prints the last N lines of the pane buffer.
* ``cli set-tab-title --pane-id P TITLE``, ``activate-pane``, ``activate-tab``, ``kill-pane``,
  ``move-pane-to-new-tab --pane-id P`` work on the tables.

Failure knobs (env), used to prove the backend fails closed instead of pretending:
``FAKE_WZ_FAIL`` = comma list of command names that exit 1, ``FAKE_WZ_VERSION`` overrides --version,
``FAKE_WZ_MISSING`` = comma list of names that make the binary look absent from PATH (the test does that
by not creating the file at all).
"""
import json
import os
import re
import sys
from pathlib import Path

STATE_DIR = Path(os.environ.get('FAKE_WZ_STATE') or '/tmp/fake-wz-state')
STATE = STATE_DIR / 'state.json'
ARGV_LOG = STATE_DIR / 'argv.log'
VERSION = os.environ.get('FAKE_WZ_VERSION') or 'wezterm 20240203-110809-5046fc22'


def load():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {'next_id': 0, 'windows': {}, 'tabs': {}, 'panes': {}, 'buffer': {}, 'workspaces': ['default']}


def save(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=1))


def log(argv):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with ARGV_LOG.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps({'argv': argv, 'socket': os.environ.get('WEZTERM_UNIX_SOCKET', ''),
                             'home': os.environ.get('HOME', '')}) + '\n')


def new_id(state):
    state['next_id'] += 1
    return state['next_id']


def ensure_workspace(state, name):
    if name not in state['workspaces']:
        state['workspaces'].append(name)


def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)


def take_opt(args, name, default=None):
    """Pull ``--name VALUE`` out of args (mutates and returns (value, rest))."""
    out, i = [], 0
    value = default
    while i < len(args):
        if args[i] == name and i + 1 < len(args):
            value = args[i + 1]
            i += 2
            continue
        out.append(args[i])
        i += 1
    args[:] = out
    return value


def has_flag(args, name):
    if name in args:
        args.remove(name)
        return True
    return False


def split_dash(args):
    """Everything after the first bare ``--`` is the program; before it are options."""
    if '--' in args:
        i = args.index('--')
        return args[:i], args[i + 1:]
    return args, []


def split_prog(args):
    """Without ``--``: the first bare word that is not an option value starts PROG (real CLI behaviour)."""
    out, prog = [], []
    i = 0
    value_opts = {'--pane-id', '--window-id', '--workspace', '--cwd', '--percent', '--cells',
                  '--domain-name', '--move-pane-id', '--config', '--config-file', '--serial'}
    while i < len(args):
        if args[i] in value_opts:
            out.extend(args[i:i + 2])
            i += 2
            continue
        if args[i].startswith('-'):
            out.append(args[i])
            i += 1
            continue
        prog = args[i:]
        break
    return out, prog


# -- commands ----------------------------------------------------------------------------------------
def cmd_list(opts, prog):
    state = load()
    rows = []
    for pane_id, pane in sorted(state['panes'].items(), key=lambda kv: int(kv[1]['pane_id'])):
        tab = state['tabs'][str(pane['tab_id'])]
        win = state['windows'][str(pane['window_id'])]
        rows.append({
            'window_id': int(win['window_id']), 'tab_id': int(tab['tab_id']), 'pane_id': int(pane['pane_id']),
            'workspace': tab['workspace'], 'title': pane.get('title', ''),
            'cwd': Path(pane.get('cwd') or os.getcwd()).as_uri(),
            'tab_title': tab.get('title', ''),
            'window_title': win.get('title', ''),
            'is_active': bool(pane.get('is_active')), 'is_zoomed': False,
            'left_col': pane.get('left_col', 0), 'top_row': pane.get('top_row', 0),
            'size': {'rows': 24, 'cols': 80, 'pixel_width': 640, 'pixel_height': 384, 'dpi': 0},
            'cursor_x': 0, 'cursor_y': 0, 'tty_name': pane.get('tty_name', ''),
        })
    fmt = take_opt(opts, '--format', 'table')
    if fmt != 'json':
        print('WINDOW  TAB  PANE  WORKSPACE  TITLE  CWD')
        for r in rows:
            print('%d  %d  %d  %s  %s  %s' % (r['window_id'], r['tab_id'], r['pane_id'], r['workspace'],
                                              r['tab_title'], r['cwd']))
        return 0
    print(json.dumps(rows, indent=2))
    return 0


def add_pane(state, window_id, tab_id, pane_id=None, cwd=None, left_col=0, top_row=0, title=''):
    if pane_id is None:
        pane_id = new_id(state)
    state['panes'][str(pane_id)] = {
        'pane_id': pane_id, 'window_id': window_id, 'tab_id': tab_id,
        'cwd': cwd or os.getcwd(), 'left_col': left_col, 'top_row': top_row, 'title': title,
        'tty_name': '/dev/faketty%03d' % pane_id, 'is_active': True,
    }
    state['buffer'][str(pane_id)] = state['buffer'].get(str(pane_id), '')
    state['tabs'][str(tab_id)]['panes'].append(pane_id)
    return pane_id


def cmd_spawn(opts, prog):
    opts, prog = split_prog(opts + (['--'] + prog if prog else []))
    state = load()
    pane_id = take_opt(opts, '--pane-id')
    window_id = take_opt(opts, '--window-id')
    workspace = take_opt(opts, '--workspace')
    cwd = take_opt(opts, '--cwd')
    new_window = '--new-window' in opts
    if new_window and (pane_id or window_id):
        die('Error: --new-window cannot be used with --pane-id or --window-id')
    if workspace and not new_window:
        die('Error: --workspace requires --new-window')
    if new_window:
        wid = new_id(state)
        ensure_workspace(state, workspace or 'default')
        state['windows'][str(wid)] = {'window_id': wid, 'title': ''}
        tid = new_id(state)
        state['tabs'][str(tid)] = {'tab_id': tid, 'window_id': wid, 'workspace': workspace or 'default',
                                   'title': '', 'panes': []}
        print(add_pane(state, wid, tid, cwd=cwd))
    else:
        anchor = pane_id or window_id
        if anchor is None:
            if state['panes']:
                anchor = str(sorted((int(v['window_id']), int(k)) for k, v in state['panes'].items())[0][1])
        if anchor is None or str(anchor) not in state['panes']:
            die('Error: no such pane %s' % anchor)
        wid = state['panes'][str(anchor)]['window_id']
        tid = new_id(state)
        state['tabs'][str(tid)] = {'tab_id': tid, 'window_id': wid,
                                   'workspace': state['tabs'][str(state['panes'][str(anchor)]['tab_id'])]['workspace'],
                                   'title': '', 'panes': []}
        print(add_pane(state, wid, tid, cwd=cwd or state['panes'][str(anchor)].get('cwd')))
    save(state)
    return 0


def cmd_split_pane(opts, prog):
    state = load()
    pane_id = take_opt(opts, '--pane-id')
    cwd = take_opt(opts, '--cwd')
    percent = int(take_opt(opts, '--percent', '50'))
    cells = take_opt(opts, '--cells')
    move = take_opt(opts, '--move-pane-id')
    if percent <= 0 or percent >= 100:
        die('Error: --percent must be in the range 1..99')
    if cells is not None and int(cells) <= 0:
        die('Error: cells must be non-zero')
    right = any(f in opts for f in ('--right', '--horizontal'))
    left = '--left' in opts
    top = '--top' in opts
    bottom = any(f in opts for f in ('--bottom',))
    if sum([right, left, top]) > 1:
        die('Error: conflicting split directions')
    if pane_id is None:
        if not state['panes']:
            die('Error: no pane to split')
        pane_id = sorted(state['panes'], key=int)[0]
    if str(pane_id) not in state['panes']:
        die('Error: no such pane %s' % pane_id)
    src = state['panes'][str(pane_id)]
    wid, tid = src['window_id'], src['tab_id']
    if move is not None:  # move an existing pane into the new split
        if str(move) not in state['panes']:
            die('Error: no such pane %s' % move)
        moved = state['panes'].pop(str(move))
        state['buffer'].setdefault(str(move), '')
        new = int(move)
        for tab in state['tabs'].values():
            if new in tab['panes']:
                tab['panes'].remove(new)
        moved['window_id'], moved['tab_id'] = wid, tid
        state['panes'][str(new)] = moved
        state['tabs'][str(tid)]['panes'].append(new)
    else:
        new = add_pane(state, wid, tid, cwd=cwd or src.get('cwd'))
    pane = state['panes'][str(new)]
    if right:
        pane['left_col'] = int(src.get('left_col', 0)) + int(80 * percent / 100)
        pane['top_row'] = int(src.get('top_row', 0))
    elif left:
        pane['left_col'] = int(src.get('left_col', 0))
        pane['top_row'] = int(src.get('top_row', 0))
    else:  # bottom is the default
        pane['top_row'] = int(src.get('top_row', 0)) + int(24 * percent / 100)
        pane['left_col'] = int(src.get('left_col', 0))
    save(state)
    print(new)
    return 0


def cmd_send_text(opts, prog):
    state = load()
    pane_id = take_opt(opts, '--pane-id')
    no_paste = '--no-paste' in opts
    if no_paste:
        opts.remove('--no-paste')
    text = ' '.join(prog or opts)  # ``send-text [opts] -- TEXT`` or the bare TEXT
    if not opts and not prog:
        text = sys.stdin.read()
    if pane_id is None:
        if not state['panes']:
            die('Error: no pane to send to')
        pane_id = sorted(state['panes'], key=int)[0]
    if str(pane_id) not in state['panes']:
        die('Error: no such pane %s' % pane_id)
    payload = text if no_paste else '\x1b[200~' + text + '\x1b[201~'
    state['buffer'][str(pane_id)] = state['buffer'].get(str(pane_id), '') + payload
    save(state)
    return 0


def cmd_get_text(opts, prog):
    state = load()
    pane_id = take_opt(opts, '--pane-id')
    start = int(take_opt(opts, '--start-line', '0'))
    end = take_opt(opts, '--end-line')
    escapes = has_flag(opts, '--escapes')
    if pane_id is None:
        if not state['panes']:
            die('Error: no pane')
        pane_id = sorted(state['panes'], key=int)[0]
    if str(pane_id) not in state['panes']:
        die('Error: no such pane %s' % pane_id)
    raw = state['buffer'].get(str(pane_id), '')
    if not escapes:  # the real CLI strips SGR / paste markers from the text it returns
        raw = re.sub(r'\x1b\[[0-9;?]*[a-zA-Z~]', '', raw)
    lines = raw.split('\n')
    if start < 0:
        lines = lines[start:]
    if end is not None and int(end) < 0:
        lines = lines[:int(end) or None]
    print('\n'.join(lines))
    return 0


def cmd_set_tab_title(opts, prog):
    state = load()
    tab_id = take_opt(opts, '--tab-id')
    pane_id = take_opt(opts, '--pane-id')
    title = ' '.join(opts)
    if tab_id is None:
        if pane_id is None:
            die('Error: specify --tab-id or --pane-id')
        if str(pane_id) not in state['panes']:
            die('Error: no such pane %s' % pane_id)
        tab_id = state['panes'][str(pane_id)]['tab_id']
    if str(tab_id) not in state['tabs']:
        die('Error: no such tab %s' % tab_id)
    state['tabs'][str(tab_id)]['title'] = title
    save(state)
    return 0


def cmd_kill_pane(opts, prog):
    state = load()
    pane_id = take_opt(opts, '--pane-id')
    if pane_id is None:
        if not state['panes']:
            die('Error: no pane')
        pane_id = sorted(state['panes'], key=int)[0]
    if str(pane_id) not in state['panes']:
        die('Error: no such pane %s' % pane_id)
    pane = state['panes'].pop(str(pane_id))
    state['buffer'].pop(str(pane_id), None)
    tab = state['tabs'].get(str(pane['tab_id']))
    if tab and pane_id in tab['panes']:
        tab['panes'].remove(int(pane_id))
    if tab is not None and not tab['panes']:  # a tab with no panes goes, and so does an empty window
        state['tabs'].pop(str(tab['tab_id']))
        win = state['windows'].get(str(tab['window_id']))
        if win and not any(t['window_id'] == win['window_id'] for t in state['tabs'].values()):
            state['windows'].pop(str(win['window_id']))
    save(state)
    return 0


def cmd_activate_pane(opts, prog):
    state = load()
    pane_id = take_opt(opts, '--pane-id')
    if pane_id is None:
        die('Error: no pane')
    if str(pane_id) not in state['panes']:
        die('Error: no such pane %s' % pane_id)
    for pane in state['panes'].values():
        pane['is_active'] = pane['pane_id'] == int(pane_id)
    save(state)
    return 0


def cmd_activate_tab(opts, prog):
    state = load()
    tab_id = take_opt(opts, '--tab-id')
    if tab_id is None:
        die('Error: specify --tab-id')
    if str(tab_id) not in state['tabs']:
        die('Error: no such tab %s' % tab_id)
    for tab in state['tabs'].values():
        for pane_id in tab['panes']:
            state['panes'][str(pane_id)]['is_active'] = tab['tab_id'] == int(tab_id)
    save(state)
    return 0


def cmd_move_pane_to_new_tab(opts, prog):
    state = load()
    pane_id = take_opt(opts, '--pane-id')
    window_id = take_opt(opts, '--window-id')
    workspace = take_opt(opts, '--workspace')
    new_window = '--new-window' in opts
    if pane_id is None:
        die('Error: specify --pane-id')
    if str(pane_id) not in state['panes']:
        die('Error: no such pane %s' % pane_id)
    pane = state['panes'][str(pane_id)]
    old = state['tabs'].get(str(pane['tab_id']))
    if old and int(pane_id) in old['panes']:
        old['panes'].remove(int(pane_id))
    if new_window:
        wid = new_id(state)
        ensure_workspace(state, workspace or 'default')
        state['windows'][str(wid)] = {'window_id': wid, 'title': ''}
        ws = workspace or 'default'
    else:
        wid = int(window_id) if window_id else pane['window_id']
        ws = state['tabs'][str(pane['tab_id'])]['workspace']
    tid = new_id(state)
    state['tabs'][str(tid)] = {'tab_id': tid, 'window_id': wid, 'workspace': ws, 'title': '', 'panes': [int(pane_id)]}
    pane['window_id'], pane['tab_id'] = wid, tid
    if old is not None and not old['panes']:
        state['tabs'].pop(str(old['tab_id']))
    save(state)
    print(tid)
    return 0


def cmd_set_window_title(opts, prog):
    state = load()
    pane_id = take_opt(opts, '--pane-id')
    title = ' '.join(opts)
    if str(pane_id) not in state['panes']:
        die('Error: no such pane %s' % pane_id)
    state['windows'][str(state['panes'][str(pane_id)]['window_id'])]['title'] = title
    save(state)
    return 0


def cmd_rename_workspace(opts, prog):
    state = load()
    pane_id = take_opt(opts, '--pane-id')
    name = ' '.join(opts)
    if str(pane_id) not in state['panes']:
        die('Error: no such pane %s' % pane_id)
    old = state['tabs'][str(state['panes'][str(pane_id)]['tab_id'])]['workspace']
    ensure_workspace(state, name)
    for tab in state['tabs'].values():
        if tab['workspace'] == old:
            tab['workspace'] = name
    save(state)
    return 0


COMMANDS = {'list': cmd_list, 'spawn': cmd_spawn, 'split-pane': cmd_split_pane,
            'send-text': cmd_send_text, 'get-text': cmd_get_text, 'set-tab-title': cmd_set_tab_title,
            'kill-pane': cmd_kill_pane, 'activate-pane': cmd_activate_pane, 'activate-tab': cmd_activate_tab,
            'move-pane-to-new-tab': cmd_move_pane_to_new_tab, 'set-window-title': cmd_set_window_title,
            'rename-workspace': cmd_rename_workspace}


def main(argv):
    log(argv)
    args = list(argv[1:])
    if not args:
        print('wezterm: no subcommand')
        return 64
    if args[0] in ('--version', '-V'):
        print(VERSION)
        return 0
    if args[0] in ('--help', '-h', 'help'):
        print('fake wezterm')
        return 0
    if args[0] != 'cli':
        print('Error: this fake only implements `wezterm cli ...`', file=sys.stderr)
        return 64
    args = args[1:]
    if '--no-auto-start' in args:
        args.remove('--no-auto-start')
    if not args:
        print('Error: no cli subcommand', file=sys.stderr)
        return 64
    name, rest = args[0], args[1:]
    failing = {x.strip() for x in (os.environ.get('FAKE_WZ_FAIL') or '').split(',') if x.strip()}
    if name in failing:
        print('Error: %s failed (FAKE_WZ_FAIL)' % name, file=sys.stderr)
        return 1
    if name not in COMMANDS:
        print('Error: unknown subcommand %s' % name, file=sys.stderr)
        return 64
    opts, prog = split_dash(rest)
    return COMMANDS[name](opts, prog)


if __name__ == '__main__':
    sys.exit(main(sys.argv))
