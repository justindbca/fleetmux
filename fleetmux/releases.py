"""Which fleetmux release runs where (docs/design.md section 56, "Releases").

Three releases can differ on one machine:

* **CLI**: the code of this process (``own``).
* **Installed**: the release ``~/fleetmux-current`` points at (``installed``); ``install_release.sh --activate``
  moves the link; each root daemon then switches its run to it by itself (``release_drift``, design section 56).
* **Running**: the code a run's daemon runs (``running``). The daemon writes it to ``<run>/release`` at every
  start and resume (``record``); for a daemon started before that file existed, it is the release directory in
  the run config's ``python`` path, else unknown (None).

A release is the directory name under ``fleetmux-releases`` or under the releases root ``$FLEETMUX_RELEASES``
(any name), else the checkout's short commit (``release_of``).
``same`` treats two commit shas where one is a prefix of the other as one release.
"""
import fcntl
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILE = 'release'  # <run>/release: the release the run's daemon runs
LINK = '~/fleetmux-current'  # what install_release.sh --activate moves
SHA = re.compile(r'[0-9a-f]{7,40}\Z')
_own = []
_warned = set()


def _in_root(path):
    """The first part of ``path`` below the releases root ``$FLEETMUX_RELEASES`` (compared as given and with its
    directories resolved), else None. The installer allows a root of any name, also a nested one."""
    env = os.environ.get('FLEETMUX_RELEASES')
    if not env or not path:
        return None
    root = Path(env).expanduser()
    path = Path(path)
    for r in (root, Path(os.path.realpath(root))):
        for p in (path, Path(os.path.realpath(path.parent)) / path.name):
            try:
                parts = p.relative_to(r).parts
            except ValueError:
                continue
            if parts and parts[0] not in ('.', '..', VERSIONS):
                return parts[0]
    return None


def release_of(root=ROOT):
    """The release of a code directory: its name under ``fleetmux-releases`` or ``$FLEETMUX_RELEASES``, else the
    checkout's short commit."""
    root = Path(root)
    if root.parent.name == 'fleetmux-releases' or _in_root(root) == root.name:
        return root.name
    try:
        out = subprocess.run(['git', '-C', str(root), 'rev-parse', '--short', 'HEAD'], capture_output=True,
                             text=True, timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None if out.returncode == 0 else None


def own():
    """The release of this process's code (computed once)."""
    if not _own:
        _own.append(release_of(ROOT))
    return _own[0]


def python():
    """This release's interpreter (docs/design.md section 60): the release's own ``.venv`` (the one the ``fleetmux``
    command runs it with), else the interpreter of this process (a checkout without a venv). Symlinks are kept, so
    the venv path stays the venv path."""
    venv = ROOT / '.venv' / 'bin'
    for name in (Path(sys.executable).name, 'python3', 'python'):
        if (venv / name).exists():
            return str(venv / name)
    return sys.executable


def installed(link=None):
    """The release ``link`` (default ``~/fleetmux-current``) points at; None when there is no such link."""
    path = Path(link or LINK).expanduser()
    if not path.exists():
        return None
    return release_of(path.resolve())


def from_python(python):
    """The release directory in an interpreter path ``.../fleetmux-releases/<release>/...`` or
    ``$FLEETMUX_RELEASES/<release>/...``, else None."""
    parts = Path(python or '').parts
    if 'fleetmux-releases' in parts[:-1]:
        return parts[parts.index('fleetmux-releases') + 1]
    return _in_root(python)


def cmdline(pid):
    """The argv of process ``pid`` (Linux: exact; elsewhere ``ps -ww`` split on blanks), None when unreadable."""
    try:
        if sys.platform.startswith('linux'):
            raw = Path('/proc/%d/cmdline' % int(pid)).read_bytes()
            return [a.decode('utf-8', 'replace') for a in raw.split(b'\0') if a] or None
        out = subprocess.run(['ps', '-ww', '-p', str(int(pid)), '-o', 'command='], capture_output=True, text=True,
                             timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return out.stdout.split() if out.returncode == 0 and out.stdout.strip() else None


LAUNCHER = '__PYVENV_LAUNCHER__'


def launcher_in(ps_env):
    """``__PYVENV_LAUNCHER__`` in ``ps -wwE -o command=`` output (the command line followed by the start
    environment), else None. The value runs up to the next environment word (`` NAME=``) or the end of the line:
    a venv path may hold blanks (a home directory with a blank in its name)."""
    found = re.search(r'(?:^|\s)' + LAUNCHER + r'=(.*?)(?=\s+[A-Za-z_][A-Za-z0-9_]*=|\s*$)', ps_env or '', re.M)
    return found.group(1) or None if found else None


def launcher(pid):
    """The venv python that started process ``pid`` on macOS: a venv made from a framework Python re-executes the
    framework's ``.../Python.app/Contents/MacOS/Python``, so ``ps`` shows that path as argv[0] and the venv's python
    is only in the start environment, as ``__PYVENV_LAUNCHER__``. None on Linux (a venv python keeps its own path
    as argv[0]), when unreadable or when there is none."""
    if sys.platform.startswith('linux'):
        return None
    try:
        out = subprocess.run(['ps', '-wwE', '-p', str(int(pid)), '-o', 'command='], capture_output=True, text=True,
                             timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return launcher_in(out.stdout) if out.returncode == 0 else None


def of_argv(argv):
    """The release of a process from its argv: the first ``.../fleetmux-releases/<release>/...`` or
    ``$FLEETMUX_RELEASES/<release>/...`` path in it."""
    for part in argv or ():
        found = from_python(part)
        if found:
            return found
    return None


def of_pid(pid):
    """The release process ``pid`` runs (its interpreter, script or binary under the releases root), else
    None (gone, unreadable, or a checkout)."""
    return of_argv(cmdline(pid))


def running(cfg):
    """The release the daemon of run config ``cfg`` runs: ``<run>/release``, else the ``python`` path, else
    None."""
    run = (cfg or {}).get('run')
    if run:
        try:
            text = (Path(run) / FILE).read_text().strip()
        except OSError:
            text = ''
        if text:
            return text
    return from_python((cfg or {}).get('python'))


def record(run, release=None):
    """Write ``<run>/release`` (the daemon, at start and resume). An unknown release removes the file, so a
    resume never leaves the release of the daemon before it."""
    release = release or own()
    if not release:
        (Path(run) / FILE).unlink(missing_ok=True)
        return
    fd, tmp = tempfile.mkstemp(dir=str(run), prefix='.release.')
    with os.fdopen(fd, 'w') as f:
        f.write(release + '\n')
    os.replace(tmp, Path(run) / FILE)


def same(a, b):
    """True when ``a`` and ``b`` name one release: equal, or commit shas where one is a prefix of the other.
    An unknown release (None) is never the same as anything."""
    if not a or not b:
        return False
    return a == b or bool(SHA.match(a) and SHA.match(b) and (a.startswith(b) or b.startswith(a)))


def field(cfg, link=None):
    """``fleetmux status`` field: ``{running, installed, restart_needed}``; None when the running release is
    unknown. ``restart_needed``: an installed release exists and differs from the running one."""
    run_release = running(cfg)
    if not run_release:
        return None
    inst = installed(link)
    return {'running': run_release, 'installed': inst, 'restart_needed': bool(inst) and not same(run_release, inst)}


def restart_line(name, run_release, inst):
    """One line: a fleet still runs ``run_release`` while ``inst`` is installed."""
    return ('fleet %s runs release %s; release %s is installed (~/fleetmux-current): restart the fleet with its '
            'usual start command to run it' % (name, run_release, inst))


def daemon_running(run):
    """True while a daemon holds the run's lock (the check of ``fleet.daemon_running``, without importing
    fleet.py, which may be ``__main__``)."""
    with (Path(run) / 'daemon.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def warn_cli(cfg, stream=None, alive=None):
    """One stderr line when this CLI's release differs from the release the fleet's running daemon runs. Silent
    when either is unknown, when no daemon runs, and after the first warning for a run in this process."""
    run = (cfg or {}).get('run')
    run_release = running(cfg)
    if not run or not run_release or run in _warned:
        return False
    mine = own()
    if not mine or same(mine, run_release):
        return False
    try:
        if not (alive or daemon_running)(run):
            return False
    except OSError:
        return False
    _warned.add(run)
    (stream or sys.stderr).write(
        'fleetmux: warning: this command is release %s, the daemon of %s runs release %s; restart the fleet with '
        'its usual start command to run %s, or use ~/fleetmux-releases/%s/bin/fleetmux\n'
        % (mine, run, run_release, mine, run_release))
    return True


# ------------------------------------------------------------------ a release named by its version
VERSION_PREFIX = 'version:'
VERSIONS = 'versions'  # $FLEETMUX_RELEASES/versions/<v> -> ../<sha>, kept by the installer
VERSION_NAME = re.compile(r'[0-9A-Za-z][0-9A-Za-z._+-]*\Z')


def releases_root():
    """Where releases are installed: $FLEETMUX_RELEASES, else ~/fleetmux-releases."""
    return Path(os.environ.get('FLEETMUX_RELEASES') or Path.home() / 'fleetmux-releases').expanduser()


def resolve(value):
    """The release directory a ``version:<v>`` value names, None for any other value (a directory or a sha is the
    caller's own business). The installer's side of the interface (scripts/install_release.sh): it writes
    ``"version"`` into ``<release>/bin/installed.json`` and points ``<releases root>/versions/<v>`` at ``../<sha>``.
    The link must lead to a directory right under the releases root whose installed.json names the same version.
    Raises ValueError with the reason."""
    import json
    value = str(value)
    if not value.startswith(VERSION_PREFIX):
        return None
    version = value[len(VERSION_PREFIX):]
    if not VERSION_NAME.match(version) or version in ('.', '..'):
        raise ValueError('version %r: not a version name' % version)
    root = releases_root()
    link = root / VERSIONS / version
    if not link.is_dir():
        raise ValueError('version %s: no release, %s does not lead to a directory' % (version, link))
    target = link.resolve()
    if target.parent != root.resolve():
        raise ValueError('version %s: %s leads to %s, not a release under %s' % (version, link, target, root))
    meta = target / 'bin' / 'installed.json'
    try:
        recorded = json.loads(meta.read_text()).get('version')
    except (OSError, ValueError, AttributeError) as exc:
        raise ValueError('version %s: %s cannot be read (%s)' % (version, meta, exc)) from exc
    if recorded != version:
        raise ValueError('version %s: %s leads to %s, whose installed.json says version %s'
                         % (version, link, target, recorded))
    return target
