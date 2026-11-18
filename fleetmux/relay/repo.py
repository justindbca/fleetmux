"""The cache repo: a bare repo the relay creates under its own state dir (`<state dir>/<job>/cache.git`). Record
commits and snapshot objects are written there, and every push and fetch goes from it by URL. Its alternates
point at the task work trees' object directories (cache -> work tree only), so snapshots and branch pushes find
the workers' objects without copying them."""
from __future__ import annotations

import os
from pathlib import Path

from .gitio import Git


def ensure_cache(git: Git, path: Path) -> Path:
    """The cache repo at `path`, created (bare) when missing."""
    path = Path(path)
    if not (path / "HEAD").exists():
        path.mkdir(parents=True, exist_ok=True)
        git.run("init", "-q", "--bare", str(path))
    return path


def object_dir(git: Git, worktree: Path) -> Path:
    """`<git common dir>/objects` of a work tree (linked work trees resolve to the main repository)."""
    common = git.out("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=worktree)
    return (Path(common) / "objects").resolve()


def _alternates_file(cache: Path) -> Path:
    return Path(cache) / "objects" / "info" / "alternates"


def alternates(cache: Path) -> list:
    f = _alternates_file(cache)
    return [x for x in f.read_text().splitlines() if x] if f.exists() else []


def set_alternates(cache: Path, paths: list) -> None:
    """Set the cache's alternates to exactly `paths` (order kept, duplicates dropped)."""
    seen, lines = set(), []
    for p in paths:
        s = str(Path(p))
        if s not in seen:
            seen.add(s)
            lines.append(s)
    f = _alternates_file(cache)
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(".alternates.tmp")
    tmp.write_text("".join(line + "\n" for line in lines))
    os.replace(tmp, f)


def add_alternates(cache: Path, paths: list) -> None:
    set_alternates(cache, alternates(cache) + [str(Path(p)) for p in paths])


def has_commit(git: Git, cache: Path, sha: str | None) -> bool:
    if not sha:
        return False
    return git.bare_cache(cache).run("cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0


def is_ancestor(git: Git, cache: Path, a: str, b: str) -> bool:
    if a == b:
        return True
    return git.bare_cache(cache).run("merge-base", "--is-ancestor", a, b, check=False).returncode == 0


def resolve(git: Git, cache: Path, name: str):
    """The commit `name` (ref or sha) names in the cache repo, or None."""
    proc = git.bare_cache(cache).run("rev-parse", "-q", "--verify", f"{name}^{{commit}}", check=False)
    return proc.stdout.decode().strip() if proc.returncode == 0 else None


def update_ref(git: Git, cache: Path, ref: str, sha: str) -> None:
    git.bare_cache(cache).run("update-ref", ref, sha)
