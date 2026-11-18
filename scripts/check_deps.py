#!/usr/bin/env python3
"""Check deps.yaml on this machine: the running interpreter, its packages (requirements.txt pins) and
the external programs on PATH. Prints one line per item; exit 1 when anything is missing or too old.

Programs may carry `when: cmux|wezterm|fleetmux` (design §6): the fleet runs one terminal backend at a time, so
a gated program is only required for the backend the run will use. Which backend that is follows the
run's own rule (design §5), not the manifest's line order:

  `--backend` / `FLEETMUX_BACKEND` -> that backend;
  otherwise fleetmux on every platform.

Gated items of the other backend print as `skip` and never fail the check.

`agent: true` marks an optional agent CLI of the user (codex, claude, opencode): it is reported, never a failure.
Present and at its minimum -> `ok`; missing or below its minimum -> `skip` with the reason (`start` does not use
it either). pi is not optional: it is a plain required program, and node (`with: pi`) with it.
`with: <program>` ties an item to another program: it is checked only when that program is installed.

Programs are found the way fleet discovery finds them (docs/design.md §50.3): this release's own bin/ first, then
fleetmux.discover.search_path() (the interactive login shell's PATH, then the process PATH, then the directories this
machine recorded before: the installed release's installed.json `dependencies` and discovery's rows). Each row names
the absolute path it found; FLEETMUX_DEPS_JSON=<file> also writes the rows as JSON with `dir` (the PATH entry it was
found in) and, for an `#!/usr/bin/env` script such as pi, `interpreter` / `interpreter_path` / `interpreter_dir`
(install_release.sh keeps them in installed.json, where the next lookup reads them). A missing program's FAIL line
says where it was last found when there is a record. Nothing is installed or upgraded here.

FLEETMUX_DEPS_DEFER (names separated by spaces or commas) defers those programs to a later check on the target:
they print as `deferred` and never fail. The docker image build uses it for the agent CLIs, which are downloaded
into the container afterwards (docker/install_agents.py) and checked there without it."""
import json
import os
import re
import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BACKENDS = ('cmux', 'wezterm', 'fleetmux')
BACKEND_ENV = 'FLEETMUX_BACKEND'
DEFER_ENV = 'FLEETMUX_DEPS_DEFER'
DEFAULT_BACKEND = 'fleetmux'  # design §33.3: the default on every platform


def version(text):
    """First version-looking token as a tuple: 'codex-cli 0.162.1' -> (0, 162, 1), 'tmux 3.6a' -> (3, 6).
    A single number counts as a version too: WezTerm releases are date stamps ('20240203-110809-5046fc22'
    -> (20240203,)), and the minimum in deps.yaml may be given unquoted or as a bare number."""
    m = re.search(r'\d+(?:\.\d+)*', text or '')
    return tuple(int(x) for x in m.group(0).split('.')) if m else None


def at_least(have, need):
    need = version(str(need))
    return have is not None and need is not None and have[:len(need)] >= need


SEARCH = None  # the PATH programs are looked up on (main sets it; None = the process PATH)
FOUND = {}     # program -> resolved absolute path, filled by probe
LOCATED = {}   # program -> discover.locate() row (path, dir, interpreter*), filled by probe


def _discover():
    """fleetmux.discover next to this script, or None (an older layout without the package)."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        from fleetmux import discover
    except Exception:
        return None
    return discover


def probe(cmd):
    """The parsed version of one `cmd` entry, or None when it is missing or does not answer."""
    path = shutil.which(cmd[0], path=SEARCH)
    if not path:
        return None
    FOUND[cmd[0]] = os.path.realpath(path)  # like discovery: the install itself, not a session-private shim
    discover = _discover()
    if discover is not None:
        LOCATED[cmd[0]] = discover.locate(cmd[0], SEARCH) or {}
    env = dict(os.environ, PATH=SEARCH) if SEARCH else None  # e.g. `#!/usr/bin/env node` finds the same node
    try:
        done = subprocess.run([path] + list(cmd[1:]), capture_output=True, text=True, timeout=20, env=env)
        return version(done.stdout + done.stderr) if done.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def search(warn):
    """This release's bin/ (its own fleetmux binaries win), then discover.search_path()."""
    discover = _discover()
    if discover is None:
        raise ImportError('no fleetmux package next to ' + str(ROOT / 'scripts'))
    first = [str(ROOT / 'bin')] if (ROOT / 'bin').is_dir() else []
    return os.pathsep.join(first + [p for p in discover.search_path(warn=warn).split(os.pathsep) if p not in first])


def pick_backend(spec, backend=None, plat=None, probe_it=None):
    """The backend whose gated programs are required here, by design §5's rule.

    Explicit `backend` or FLEETMUX_BACKEND wins. Otherwise the run's default applies: DEFAULT_BACKEND
    (fleetmux) on every platform, whatever else is installed. The manifest's line order plays no part.
    `plat` / `probe_it` are kept for callers; the default no longer depends on them."""
    chosen = (backend or os.environ.get(BACKEND_ENV) or '').strip().lower()
    if chosen:
        if chosen not in BACKENDS:
            raise ValueError('unknown backend ' + chosen + ' (expected one of: ' + ', '.join(BACKENDS) + ')')
        return chosen
    return DEFAULT_BACKEND


def desktop_session(plat=None):
    """A graphical session to open windows in: always on macOS; elsewhere DISPLAY or WAYLAND_DISPLAY is set."""
    plat = sys.platform if plat is None else plat
    return plat == 'darwin' or bool(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))


def check(root=ROOT, backend=None, plat=None, probe_it=None):
    spec = yaml.safe_load((root / 'deps.yaml').read_text())
    backend = pick_backend(spec, backend, plat=plat, probe_it=probe_it)
    desktop = desktop_session(plat)
    probe_it = probe if probe_it is None else probe_it
    deferred = set(os.environ.get(DEFER_ENV, '').replace(',', ' ').split())
    rows = []
    py = sys.version_info[:3]
    rows.append(('python', '.'.join(map(str, py)), spec['python']['min'], at_least(py, spec['python']['min'])))
    for line in (root / spec['python_packages']).read_text().splitlines():
        line = line.split('#')[0].strip()
        if not line:
            continue
        name, want = line.split('==')
        try:
            have = metadata.version(name)
        except metadata.PackageNotFoundError:
            have = None
        rows.append((name, have or 'missing', '==' + want, have == want))
    programs = spec['programs']
    found = {}

    def have_of(name):
        if name not in found:
            found[name] = probe_it(programs[name]['cmd'])
        return found[name]

    for name, item in programs.items():
        if item.get('when') and item['when'] != backend:
            rows.append((name, 'skip (backend=' + item['when'] + ')', '>=' + item['min'], True))
            continue
        if name in deferred:
            rows.append((name, 'deferred (' + DEFER_ENV + ')', '>=' + item['min'], True))
            continue
        if item.get('with') and have_of(item['with']) is None:
            rows.append((name, 'skip (only with ' + item['with'] + ')', '>=' + item['min'], True))
            continue
        have = have_of(name)
        shown = '.'.join(map(str, have)) if have else 'missing'
        ok = at_least(have, item['min'])
        if item.get('agent') and not ok:  # the user's optional CLI: reported, never a failure
            why = 'not installed' if have is None else 'below ' + str(item['min'])
            rows.append((name, 'skip ' + (shown + ' (' if have else '(') + why + ': not used by fleets on this machine)',
                         '>=' + item['min'], True))
            continue
        if have is None and item.get('desktop') and not desktop:
            # `desktop: true` (fleetmux-web, brief-m25 §3.5): only a desktop session opens windows; warn, do not block.
            rows.append((name, 'missing (warning only: no desktop session, DISPLAY / WAYLAND_DISPLAY unset)',
                         '>=' + item['min'], True))
            continue
        if not ok and name == 'wezterm-mux-server':
            # design §4.4: a wezterm run needs both binaries; say so instead of a bare version miss.
            shown += ' (needed for the private mux server: install wezterm, it ships both)'
        rows.append((name, shown, '>=' + item['min'], ok))
    return rows


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    backend = None
    if argv:
        if len(argv) == 2 and argv[0] == '--backend':
            backend = argv[1]
        else:
            print('usage: check_deps.py [--backend cmux|wezterm|fleetmux]', file=sys.stderr)
            return 2
    global SEARCH
    try:
        SEARCH = search(warn=lambda line: print('note  ' + line))
    except Exception as exc:  # no fleetmux package next to this script: say so, use the process PATH
        print('note  login shell PATH unavailable (' + type(exc).__name__ + ': ' + str(exc)[:120]
              + '); using the process PATH only')
    try:
        rows = check(backend=backend)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    out = []
    discover = _discover()
    for name, have, need, ok in rows:
        path = FOUND.get(name)
        row = {'name': name, 'have': have, 'need': need, 'ok': bool(ok), 'path': path}
        where = LOCATED.get(name) or {}
        if path and where.get('path') == path:
            row.update((k, v) for k, v in where.items() if k != 'path')
        hint = ''
        if not ok and not path and discover is not None:  # missing: say where this machine last had it
            last = discover.last_found_at(name)
            if last:
                row['last_found'] = last[0]
                hint = '; ' + discover.last_found(name)
        out.append(row)
        if ok and have.startswith('skip'):
            print('skip  ' + name + ' ' + have[4:].strip())
        else:
            print(('ok  ' if ok else 'FAIL') + '  ' + name + ' ' + have + ' (need ' + need + ')'
                  + (' at ' + path if path else '') + hint)
    if os.environ.get('FLEETMUX_DEPS_JSON'):
        with open(os.environ['FLEETMUX_DEPS_JSON'], 'w', encoding='utf-8') as fh:
            json.dump(out, fh, indent=2)
            fh.write('\n')
    return 0 if all(r[3] for r in rows) else 1


if __name__ == '__main__':
    sys.exit(main())
