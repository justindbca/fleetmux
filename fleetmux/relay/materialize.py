"""A new owner's work tree: a fresh git repo on the task's branch whose files are byte-identical to the start
point (the last checkpoint's snapshot, else its commit, else the task's base). `origin` is the code remote, so
the workers push their own commits with `git push origin <branch>`.

A marker file in the relay's own state dir names the task, epoch and start point of every tree it made; a path
that holds anything else is foreign content and is never touched."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import records as R
from .gitio import Git
from .refs import branch_glob
from .repo import add_alternates, has_commit, object_dir, update_ref

START_REF = "refs/relay-start"           # cache-local refs, never pushed
FETCH_REF = "refs/relay-fetch"           # cache-local copies of the code remote's branches


@dataclass
class Placed:
    worktree: Path
    branch: str
    start: dict
    marker: Path


def marker_path(marker_dir: Path, job: str, task: str, epoch: int) -> Path:
    return Path(marker_dir) / f"{job}.{task}.e{int(epoch)}.json"


def _ensure_start(git: Git, cache: Path, hub, task: str, start: dict) -> None:
    sha = start["commit"]
    if has_commit(git, cache, sha):
        return
    if start["source"] == "snap":
        hub.fetch_objects([hub.names.snap(task)], remote="state")
    if not has_commit(git, cache, sha):
        hub.fetch_objects([f"{branch_glob(hub.job)}:{FETCH_REF}/{hub.job}/*",
                           f"refs/heads/*:{FETCH_REF}/heads/*"], remote="code")
    if not has_commit(git, cache, sha):
        raise OSError(f"start commit {sha} of task {task} is on neither remote")


def materialize(git: Git, cache: Path, hub, *, job: str, rec: dict, worktree: Path, marker_dir: Path) -> Placed:
    """Build (or find, when this machine already built it) the work tree of `rec`'s current epoch."""
    worktree = Path(worktree)
    task, epoch, branch = rec["id"], int(rec["epoch"]), rec["branch"]
    start = R.start_point(rec)
    marker = marker_path(marker_dir, job, task, epoch)
    mark = {"job": job, "task": task, "epoch": epoch, "start": start, "worktree": str(worktree)}
    if marker.exists():
        try:
            same = json.loads(marker.read_text()) == mark
        except ValueError:
            same = False
        if not same:
            raise OSError(f"{marker} names another task or start point (foreign content, not touched)")
        if (worktree / ".git").exists():
            return Placed(worktree, branch, start, marker)
    elif worktree.exists() and any(worktree.iterdir()):
        raise OSError(f"{worktree} already holds files without this task's marker (foreign content, not touched)")
    _ensure_start(git, cache, hub, task, start)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps(mark, sort_keys=True))
    start_ref = f"{START_REF}/{job}/{task}/e{epoch}"
    update_ref(git, cache, start_ref, start["commit"])
    worktree.mkdir(parents=True, exist_ok=True)
    git.run("init", "-q", str(worktree))
    git.run("config", "core.autocrlf", "false", cwd=worktree)
    git.run("config", "core.symlinks", "true", cwd=worktree)
    git.run("fetch", "--no-tags", "--no-write-fetch-head", "--quiet", "--", str(cache),
            f"{start_ref}:refs/heads/{branch}", cwd=worktree)
    git.run("checkout", "-q", branch, cwd=worktree)
    git.run("remote", "add", "origin", hub.code_remote, cwd=worktree)
    add_alternates(cache, [object_dir(git, worktree)])
    return Placed(worktree, branch, start, marker)
