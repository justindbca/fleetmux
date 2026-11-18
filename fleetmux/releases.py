"""Which fleetmux release runs where (docs/design.md section 56, "Releases").

Three releases can differ on one machine:

* **CLI**: the code of this process (``own``).
* **Installed**: the release ``~/fleetmux-current`` points at (``installed``); ``install_release.sh --activate``
  moves the link, nothing restarts a fleet.
* **Running**: the code a run's daemon runs (``running``). The daemon writes it to ``<run>/release`` at every
  start and resume (``record``); for a daemon started before that file existed, it is the release directory in
  the run config's ``python`` path, else unknown (None).

A release is the directory name under ``fleetmux-releases``, else the checkout's short commit (``release_of``).
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


def release_of(root=ROOT):
    """The release of a code directory: its name under ``fleetmux-releases``, else the checkout's short commit."""
    root = Path(root)
    if root.parent.name == 'fleetmux-releases':
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


def installed(link=None):
    """The release ``link`` (default ``~/fleetmux-current``) points at; None when there is no such link."""
    path = Path(link or LINK).expanduser()
    if not path.exists():
        return None
    return release_of(path.resolve())


def from_python(python):
    """The release directory in an interpreter path ``.../fleetmux-releases/<release>/...``, else None."""
    parts = Path(python or '').parts
    return parts[parts.index('fleetmux-releases') + 1] if 'fleetmux-releases' in parts[:-1] else None


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
