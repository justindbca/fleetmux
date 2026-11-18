"""Running git: fixed safety config and environment, bounded by a timeout that kills the whole process group."""
from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path
from typing import Callable, Union

Timeout = Union[float, Callable[[], float]]
EXIT_NOT_FOUND = 127   # the shell convention for "command not found"


class GitError(Exception):
    def __init__(self, argv, returncode, stdout=b"", stderr=b"", timed_out=False):
        self.argv = list(argv)
        self.returncode = returncode
        self.stdout = stdout or b""
        self.stderr = stderr or b""
        self.timed_out = timed_out
        what = "timed out" if timed_out else f"exit {returncode}"
        tail = self.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] if self.stderr else []
        super().__init__(f"git {' '.join(self.argv[1:])}: {what}" + (f": {tail[0]}" if tail else ""))


class Git:
    """One git runner. Every call gets `-c core.fsmonitor=false -c gc.auto=0 -c maintenance.auto=false` and the
    environment `GIT_OPTIONAL_LOCKS=0 GIT_TERMINAL_PROMPT=0 LC_ALL=C`. `timeout_s` is a number or a callable read at
    each call (a hot parameter)."""

    def __init__(self, git_bin: str, timeout_s: Timeout, git_dir: Path | None = None):
        self.git_bin = git_bin
        self.timeout_s = timeout_s
        self.git_dir = Path(git_dir) if git_dir else None

    def bare_cache(self, cache_dir: Path) -> "Git":
        """The same runner bound to `--git-dir=<cache_dir>`."""
        return Git(self.git_bin, self.timeout_s, git_dir=cache_dir)

    def default_timeout(self) -> float:
        return float(self.timeout_s() if callable(self.timeout_s) else self.timeout_s)

    def argv(self, *args: str) -> list:
        head = [self.git_bin, "-c", "core.fsmonitor=false", "-c", "gc.auto=0", "-c", "maintenance.auto=false"]
        if self.git_dir is not None:
            head.append(f"--git-dir={self.git_dir}")
        return head + [str(a) for a in args]

    def run(self, *args: str, cwd: Path | None = None, env: dict | None = None, timeout_s: float | None = None,
            input: bytes | None = None, check: bool = True) -> subprocess.CompletedProcess:
        """Run git; stdout/stderr are bytes. Raises GitError on a timeout always, and on exit != 0 when `check`."""
        argv = self.argv(*args)
        full_env = dict(os.environ)
        full_env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "LANG": "C"})
        if env:
            full_env.update({k: str(v) for k, v in env.items()})
        limit = self.default_timeout() if timeout_s is None else float(timeout_s)
        try:
            proc = subprocess.Popen(argv, cwd=str(cwd) if cwd else None, env=full_env, start_new_session=True,
                                    stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            raise GitError(argv, EXIT_NOT_FOUND, b"", str(exc).encode()) from exc
        try:
            out, err = proc.communicate(input=input, timeout=limit)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            out, err = proc.communicate()
            raise GitError(argv, None, out, err, timed_out=True)
        result = subprocess.CompletedProcess(argv, proc.returncode, out, err)
        if check and proc.returncode != 0:
            raise GitError(argv, proc.returncode, out, err)
        return result

    def out(self, *args: str, **kw) -> str:
        """stdout of a checked run, decoded and stripped."""
        return self.run(*args, **kw).stdout.decode("utf-8", "replace").strip()


def short_error(exc: GitError) -> str:
    """The reason of a failed git call for a notice: the last stderr line (or "timed out"), not the argv."""
    if exc.timed_out:
        return "timed out"
    tail = exc.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] if exc.stderr else []
    return tail[0] if tail else f"exit {exc.returncode}"
