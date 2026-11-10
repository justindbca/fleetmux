"""The stand-in ``claude`` of the tmux-view tests: a symlink named ``claude`` to a host ``sleep``, so its comm is
``claude`` (``has_cli_process``) and its argv[0] is its own path (the tests match processes on it). A copied system
binary is killed by code signing on macOS, hence the symlink.

Not every ``sleep`` can run under another name. uutils coreutils (Ubuntu 25.10+, the ubuntu-26.04 runner) is one
hard-linked multicall binary that refuses when argv[0] does not name the executable ("Security violation: Requested
utility `claude` does not match executable name", seen on the runner 2026-11-10), whatever arguments follow. Those
hosts keep GNU sleep as ``/usr/bin/gnusleep``, which ignores argv[0]. ``make`` links to the first candidate that
works as ``claude 0`` (probed once).
"""
import functools
from pathlib import Path
import subprocess
import tempfile

CANDIDATES = ('/bin/sleep', '/usr/bin/gnusleep')


@functools.cache
def _target() -> str:
    with tempfile.TemporaryDirectory() as d:
        for i, target in enumerate(CANDIDATES):
            if not Path(target).exists():
                continue
            claude = Path(d) / str(i) / 'claude'
            claude.parent.mkdir()
            claude.symlink_to(target)
            if subprocess.run([str(claude), '0'], capture_output=True).returncode == 0:
                return target
    raise RuntimeError('no host sleep runs as `claude 0`; tried ' + ', '.join(CANDIDATES))


def make(directory) -> Path:
    claude = Path(directory) / 'claude'
    claude.symlink_to(_target())
    return claude


def args(seconds) -> str:
    """What follows the ``claude`` path on a command line to sleep ``seconds``."""
    return str(seconds)
