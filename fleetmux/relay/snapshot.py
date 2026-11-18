"""Snapshots: commits built from a task work tree's current files (committed or not, ignored ones left out)
through a temporary index.

Never changes the worker's index, HEAD, branches, config or files and writes no object into the worker's
repository: the index is copied, new blobs go to the cache repo (GIT_OBJECT_DIRECTORY), the worker's objects are
read through GIT_ALTERNATE_OBJECT_DIRECTORIES, and GIT_OPTIONAL_LOCKS=0 keeps git from refreshing the real index.
Files whose name matches a secret pattern (`relay.secret_globs`) and that HEAD does not track are taken out of the
snapshot and reported."""
from __future__ import annotations

import fnmatch
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .gitio import Git, GitError
from .refs import identity_env

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


@dataclass
class SnapResult:
    status: str                 # changed | unchanged | refused_empty | error
    commit: str | None = None   # the snapshot commit (changed), or the last one (unchanged)
    tree: str | None = None
    head: str | None = None     # HEAD at snapshot time (None: unborn)
    branch: str | None = None   # HEAD's branch name (None: detached or unborn without a name)
    reason: str | None = None   # for error / refused_empty
    skipped: list = field(default_factory=list)   # paths left out because their name looks like a secret


def secret_paths(paths: list, globs) -> list:
    """The paths whose file name (or whole path) matches one of `globs`."""
    out = []
    for p in paths:
        name = p.rsplit("/", 1)[-1]
        if any(fnmatch.fnmatchcase(name, g) or fnmatch.fnmatchcase(p, g) for g in globs or ()):
            out.append(p)
    return out


def _remove(*paths: Path) -> None:
    for p in paths:
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def snapshot_tree(git: Git, repo: Path, *, job: str, task: str, worktree: Path, tmp_dir: Path, last: dict | None,
                  seq: int, parent: str | None = None, secret_globs=()) -> SnapResult:
    """Snapshot one task work tree into the cache repo `repo`. `last` = {tree, head, commit} of the previous
    snapshot of this tree (or None). `parent` overrides the parent commit (default: the work tree's HEAD). The temp
    index `<tmp_dir>/<job>.<task>.index` is deleted after the round."""
    skipped: list = []
    cache = repo
    worktree = Path(worktree)
    tmp_dir = Path(tmp_dir)
    cache = Path(cache)
    if not worktree.is_dir():
        return SnapResult("error", reason=f"work tree {worktree} does not exist")
    try:
        index = Path(git.out("rev-parse", "--path-format=absolute", "--git-path", "index", cwd=worktree))
        common = Path(git.out("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=worktree))
        top = Path(git.out("rev-parse", "--show-toplevel", cwd=worktree))
        head = git.run("rev-parse", "-q", "--verify", "HEAD^{commit}", cwd=worktree, check=False)
        head = head.stdout.decode().strip() if head.returncode == 0 else None
        br = git.run("symbolic-ref", "-q", "--short", "HEAD", cwd=worktree, check=False)
        branch = br.stdout.decode().strip() if br.returncode == 0 else None
    except GitError as exc:
        return SnapResult("error", reason=str(exc))
    if top.resolve() != worktree.resolve():
        return SnapResult("error", reason=f"{worktree} is not the top of a git work tree")

    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_index = tmp_dir / f"{job}.{task}.index"
    lock = tmp_index.with_name(tmp_index.name + ".lock")
    _remove(tmp_index, lock)
    env = {"GIT_INDEX_FILE": str(tmp_index), "GIT_OBJECT_DIRECTORY": str(cache / "objects"),
           "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(common / "objects")}
    try:
        try:
            if index.exists():
                shutil.copy2(index, tmp_index)          # keep its mtime: git's racy-entry check needs it
            elif head:
                git.run("read-tree", head, cwd=worktree, env=env)
            git.run("add", "-A", "--", ":/", cwd=worktree, env=env)
            skipped = _drop_secrets(git, worktree, env, secret_globs, head)
        except (OSError, GitError):
            # A copy that fails or cannot be used (e.g. a split-index link): rebuild from HEAD.
            _remove(tmp_index, lock)
            try:
                if head:
                    git.run("read-tree", head, cwd=worktree, env=env)
                git.run("add", "-A", "--", ":/", cwd=worktree, env=env)
                skipped = _drop_secrets(git, worktree, env, secret_globs, head)
            except GitError as exc:
                return SnapResult("error", head=head, branch=branch, reason=str(exc))
        try:
            tree = git.out("write-tree", cwd=worktree, env=env)
        except GitError as exc:
            return SnapResult("error", head=head, branch=branch, reason=str(exc))
    finally:
        _remove(tmp_index, lock)

    if last and last.get("tree") == tree and last.get("head") == head:
        return SnapResult("unchanged", commit=last.get("commit"), tree=tree, head=head, branch=branch,
                          skipped=skipped)
    alt_env = {"GIT_ALTERNATE_OBJECT_DIRECTORIES": str(common / "objects")}
    gc = git.bare_cache(cache)
    if tree == EMPTY_TREE and head:
        try:
            head_tree = gc.out("rev-parse", f"{head}^{{tree}}", env=alt_env)
        except GitError as exc:
            return SnapResult("error", tree=tree, head=head, branch=branch, reason=str(exc))
        if head_tree != EMPTY_TREE:
            return SnapResult("refused_empty", tree=tree, head=head, branch=branch,
                              reason="work tree is empty while HEAD is not")
    first = parent or head
    args = ["commit-tree", tree] + (["-p", first] if first else []) + \
        ["-m", f"relay snapshot {job}/{task} seq {seq}"]
    try:
        commit = gc.out(*args, env={**alt_env, **identity_env()})
    except GitError as exc:
        return SnapResult("error", tree=tree, head=head, branch=branch, reason=str(exc))
    return SnapResult("changed", commit=commit, tree=tree, head=head, branch=branch, skipped=skipped)


def _drop_secrets(git: Git, worktree: Path, env: dict, globs, head: str | None) -> list:
    """Remove secret-named paths that `head` does not track from the temp index only; returns them. A path HEAD
    tracks (e.g. .env.example) is already on the branch: leaving it out would read as a deletion on takeover."""
    if not globs:
        return []
    listed = git.run("ls-files", "-z", "--cached", cwd=worktree, env=env).stdout.decode("utf-8", "replace")
    tracked: set = set()
    if head:
        out = git.run("ls-tree", "-r", "--name-only", "-z", head, cwd=worktree, env=env).stdout
        tracked = {p for p in out.decode("utf-8", "replace").split("\0") if p}
    found = secret_paths([p for p in listed.split("\0") if p and p not in tracked], globs)
    if found:
        git.run("update-index", "--force-remove", "-z", "--stdin", cwd=worktree, env=env,
                input=("\0".join(found) + "\0").encode())
    return found


def salvage_snapshot(git: Git, repo: Path, *, job: str, task: str, epoch: int, worktree: Path, tmp_dir: Path,
                     secret_globs=()) -> SnapResult:
    """Everything a former owner's work tree holds now (committed or not), parent = its HEAD; the caller pushes
    the commit to `salvage/<task>/<node>-e<epoch>`. Nothing in the work tree changes."""
    return snapshot_tree(git, repo, job=job, task=f"{task}.e{epoch}.salvage", worktree=worktree, tmp_dir=tmp_dir,
                         last=None, seq=0, secret_globs=secret_globs)
