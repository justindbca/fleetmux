"""`fleetmux setup --install`: install what a fleet needs and this machine lacks, after the user confirms.

What a fleet needs (deps.yaml): redis-server, git and tmux (system packages), Node.js 22+ and pi. The command
1. finds each one the way fleet does (the login shell's PATH, then this process's) and reads its version;
2. prints what is missing and the exact steps: `sudo apt-get install` (Debian / Ubuntu) or `brew install` (macOS)
   for the system packages, the official Node.js tarball (sha256-checked) and `npm install` of pi into
   ~/.fleetmux/tools, linked into the directory that holds the `fleetmux` command;
3. runs them only after the user says yes (or with --yes).
A program that is there is never touched, also when it is older than deps.yaml asks: that is reported only.
Other agent CLIs (claude, codex, opencode) are never installed. The model login stays with the user (`pi`).
"""
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

import yaml

from . import discover, paths

DEPS_FILE = Path(__file__).resolve().with_name('deps.yaml')  # packaged copy of deps.yaml (tests keep them equal)
SYSTEM = ('redis-server', 'git', 'tmux')
PACKAGES = {'apt': {'redis-server': 'redis-server', 'git': 'git', 'tmux': 'tmux'},
            'brew': {'redis-server': 'redis', 'git': 'git', 'tmux': 'tmux'}}
NEEDED = SYSTEM + ('node', 'pi')
NODE_DIST = 'https://nodejs.org/dist/latest-v22.x/'
TIMEOUT = 600


def deps():
    return yaml.safe_load(DEPS_FILE.read_text())


def tools_dir():
    return paths.root() / 'tools'


def _version(cmd):
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=20, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r'(\d+)\.(\d+)(?:\.(\d+))?', (done.stdout or '') + (done.stderr or ''))
    return tuple(int(x or 0) for x in m.groups()) if m else None


def _tuple(text):
    return tuple(int(x) for x in str(text).split('.'))


def check(search):
    """One row per needed program: name, path, version, min, state ('ok' | 'missing' | 'old')."""
    programs = deps()['programs']
    rows = []
    for name in NEEDED:
        spec = programs[name]
        path = shutil.which(name, path=search)
        have = _version([path] + spec['cmd'][1:]) if path else None
        need = _tuple(spec['min'])
        state = 'missing' if not path else ('ok' if have and have >= need else 'old')
        rows.append({'name': name, 'path': path, 'version': '.'.join(map(str, have)) if have else None,
                     'min': spec['min'], 'state': state})
    return rows


def manager(system=None, which=shutil.which):
    """'apt', 'brew' or None (then the system packages are listed for the user to install)."""
    system = system or platform.system()
    if system == 'Darwin' and which('brew'):
        return 'brew'
    if system == 'Linux' and which('apt-get'):
        return 'apt'
    return None


def plan(rows, mgr, bin_dir):
    """(steps, notes). A step is ('system', argv) | ('node',) | ('pi',)."""
    by = {r['name']: r for r in rows}
    steps, notes = [], []
    for r in rows:
        if r['state'] == 'old':
            notes.append('%s %s is below %s: not changed; upgrade it yourself' % (r['name'], r['version'], r['min']))
    pkgs = [n for n in SYSTEM if by[n]['state'] == 'missing']
    if pkgs and mgr == 'apt':
        steps.append(('system', ['sudo', 'DEBIAN_FRONTEND=noninteractive', 'apt-get', 'install', '-y'] + [PACKAGES['apt'][n] for n in pkgs]))
    elif pkgs and mgr == 'brew':
        steps.append(('system', ['brew', 'install'] + [PACKAGES['brew'][n] for n in pkgs]))
    elif pkgs:
        notes.append('install with your package manager: ' + ', '.join(pkgs) + ' (no apt-get or Homebrew found)')
    if by['node']['state'] == 'missing':
        steps.append(('node',))
    if by['pi']['state'] == 'missing':
        if by['node']['state'] == 'old':
            notes.append('pi is not installed: it needs Node.js %s or newer' % by['node']['min'])
        else:
            steps.append(('pi',))
    if any(s[0] in ('node', 'pi') for s in steps) and bin_dir is None:
        notes.append('no `fleetmux` command on PATH: node and pi are linked into ~/.local/bin; put that on PATH')
    return steps, notes


def describe(step, bin_dir):
    if step[0] == 'system':
        return ' '.join(step[1])
    if step[0] == 'node':
        return ('Node.js 22 (official tarball from %s, sha256-checked) into %s; link node, npm, npx into %s'
                % (NODE_DIST, tools_dir(), bin_dir))
    pi = deps()['agent_installs']['pi']
    return 'npm install %s@%s into %s; link pi into %s' % (pi['npm'], pi['version'], tools_dir() / 'npm', bin_dir)


def node_asset(shasums, system=None, machine=None):
    """(file name, sha256) of the Node.js tarball for this machine from SHASUMS256.txt."""
    os_name = {'Darwin': 'darwin', 'Linux': 'linux'}[system or platform.system()]
    arch = {'arm64': 'arm64', 'aarch64': 'arm64', 'x86_64': 'x64', 'amd64': 'x64'}[(machine or platform.machine()).lower()]
    for line in shasums.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].endswith('-%s-%s.tar.gz' % (os_name, arch)):
            return parts[1], parts[0]
    raise SystemExit('setup: no Node.js tarball for %s-%s in %sSHASUMS256.txt' % (os_name, arch, NODE_DIST))


def _get(url):
    with urllib.request.urlopen(url, timeout=TIMEOUT) as resp:
        return resp.read()


def link(target, bin_dir, out):
    """bin_dir/<name> -> target; a file there that is not our link is left alone."""
    dest = bin_dir / target.name
    bin_dir.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink() and Path(os.readlink(dest)) == target:
        return
    if dest.exists() or dest.is_symlink():
        print('  %s exists and is not ours: left alone (%s is in %s)' % (dest, target.name, target.parent), file=out)
        return
    dest.symlink_to(target)
    print('  linked %s -> %s' % (dest, target), file=out)


def install_node(bin_dir, out, get=_get):
    name, digest = node_asset(get(NODE_DIST + 'SHASUMS256.txt').decode())
    data = get(NODE_DIST + name)
    if hashlib.sha256(data).hexdigest() != digest:
        raise SystemExit('setup: sha256 mismatch for ' + name)
    dest = tools_dir() / name[:-len('.tar.gz')]
    if not dest.is_dir():
        tmp = dest.with_name(dest.name + '.tmp')
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        archive = tmp / name
        archive.write_bytes(data)
        with tarfile.open(archive) as t:
            kw = {'filter': 'tar'} if hasattr(tarfile, 'tar_filter') else {}
            t.extractall(tmp, **kw)
        archive.unlink()
        (tmp / name[:-len('.tar.gz')]).rename(dest)
        shutil.rmtree(tmp)
    for exe in ('node', 'npm', 'npx'):
        link(dest / 'bin' / exe, bin_dir, out)
    return dest / 'bin'


def install_pi(node_bin, bin_dir, out, run=subprocess.run):
    pi = deps()['agent_installs']['pi']
    prefix = tools_dir() / 'npm'
    env = dict(os.environ, PATH=str(node_bin) + os.pathsep + os.environ.get('PATH', ''))
    done = run([str(node_bin / 'npm'), 'install', '-g', '--prefix', str(prefix), '%s@%s' % (pi['npm'], pi['version'])],
               env=env)
    if done.returncode != 0:
        raise SystemExit('setup: npm install of pi failed (exit %s)' % done.returncode)
    link(prefix / 'bin' / 'pi', bin_dir, out)


def main(yes=False, out=None, search=None, mgr=None, bin_dir=None, run=subprocess.run, get=_get, tty=None):
    """Exit code: 0 done or nothing to do, 1 the user said no / no terminal to ask, 2 a step failed."""
    out = out or sys.stdout
    search = search if search is not None else discover.search_path()
    rows = check(search)
    if bin_dir is None:
        found = shutil.which('fleetmux', path=search)
        bin_dir = Path(found).parent if found else None
    steps, notes = plan(rows, mgr if mgr is not None else manager(), bin_dir)
    bin_dir = bin_dir or Path.home() / '.local' / 'bin'
    print('fleetmux setup --install: what a fleet needs on this machine', file=out)
    for r in rows:
        print('  %-12s %-8s %s' % (r['name'], r['state'], r['path'] or '-') + (
            '  (%s, need %s)' % (r['version'], r['min']) if r['state'] == 'old' else ''), file=out)
    for n in notes:
        print('  note: ' + n, file=out)
    if not steps:
        print('nothing to install', file=out)
        return 0
    print('steps:', file=out)
    for i, s in enumerate(steps, 1):
        print('  %d. %s' % (i, describe(s, bin_dir)), file=out)
    if not yes:
        if not (tty if tty is not None else sys.stdin.isatty()):
            print('no terminal to ask: run `fleetmux setup --install --yes` to install', file=out)
            return 1
        if input('install these? [y/N] ').strip().lower() not in ('y', 'yes'):
            print('nothing installed', file=out)
            return 1
    node_bin = None
    for s in steps:
        print('==> ' + describe(s, bin_dir), file=out, flush=True)
        if s[0] == 'system':
            if run(s[1]).returncode != 0:
                print('setup: `%s` failed; nothing else was run' % ' '.join(s[1]), file=out)
                return 2
        elif s[0] == 'node':
            node_bin = install_node(bin_dir, out, get=get)
        else:
            if node_bin is None:
                node_bin = Path(shutil.which('node', path=search)).parent
            install_pi(node_bin, bin_dir, out, run=run)
    print('done. Log in to a model with `pi` (type /login), or add a provider to ~/.pi/agent/models.json;'
          ' then `fleetmux setup` lists what fleet can use.', file=out)
    return 0
