"""relay.snapshot: temp-index snapshots of a task work tree (R5) and the salvage snapshot (R12)."""
from pathlib import Path

import pytest

from relay_kit import (checkout_commit, git, isolated_git, mk_git, repo_fingerprint, tree_digest,  # noqa: F401
                       worker_repo, write_files)
from fleetmux.relay.repo import ensure_cache, object_dir, set_alternates
from fleetmux.relay.snapshot import EMPTY_TREE, salvage_snapshot, snapshot_tree

SECRETS = [".env", "*.pem", "id_rsa*"]


@pytest.fixture
def kit(tmp_path):
    g = mk_git()

    def cache_for(*worktrees, name="cache.git"):
        cache = ensure_cache(g, tmp_path / name)
        set_alternates(cache, [object_dir(g, w) for w in worktrees])
        return cache
    return g, cache_for, tmp_path / "tmp"


def snap(g, cache, tmp, w, last=None, seq=1, task="M0", **kw):
    return snapshot_tree(g, cache, job="j", task=task, worktree=w, tmp_dir=tmp, last=last, seq=seq, **kw)


def dirty(w):
    write_files(w, {
        "README.md": "# changed, unstaged\n",
        "staged.txt": "staged\n",
        "new/untracked.txt": "untracked\n",
        "bin/run.sh": ("exec", "#!/bin/sh\necho run\n"),
        "link": ("link", "README.md"),
        "blob.bin": bytes(range(256)) * 4,
        "x.ignored": "ignored\n",
        "build/out.o": "ignored dir\n",
    })
    git("add", "staged.txt", cwd=w)
    write_files(w, {"src/app.py": None})          # tracked file deleted, not staged


def covered(w):
    out = git("ls-files", "-co", "--exclude-standard", cwd=w).splitlines()
    return [p for p in out if (w / p).exists() or (w / p).is_symlink()]


@pytest.mark.parametrize("linked", [False, True])
def test_snapshot_leaves_index_head_worktree_and_objects_unchanged(kit, worker_repo, linked):
    g, cache_for, tmp = kit
    w = worker_repo("main", {"src/app.py": "print(1)\n"}, linked=linked)
    dirty(w)
    before = repo_fingerprint(w)
    r = snap(g, cache_for(w), tmp, w)
    assert r.status == "changed", r.reason
    assert repo_fingerprint(w) == before


def test_snapshot_writes_no_lock_or_temp_files(kit, worker_repo):
    g, cache_for, tmp = kit
    w = worker_repo("main")
    dirty(w)
    snap(g, cache_for(w), tmp, w)
    assert not (w / ".git" / "index.lock").exists()
    assert list(tmp.glob("j.M0.index*")) == []


def test_staged_unstaged_untracked_deleted_in_snapshot_ignored_not(kit, worker_repo):
    g, cache_for, tmp = kit
    w = worker_repo("main", {"src/app.py": "print(1)\n"})
    dirty(w)
    cache = cache_for(w)
    r = snap(g, cache, tmp, w)
    files = set(g.bare_cache(cache).out("ls-tree", "-r", "--name-only", r.commit).splitlines())
    assert {"README.md", "staged.txt", "new/untracked.txt", "bin/run.sh", "link", "blob.bin", ".gitignore"} <= files
    assert "src/app.py" not in files and "x.ignored" not in files and "build/out.o" not in files
    assert g.bare_cache(cache).out("show", f"{r.commit}:README.md") == "# changed, unstaged"


def test_snapshot_bytes_and_modes_equal_worktree(kit, worker_repo, tmp_path):
    g, cache_for, tmp = kit
    w = worker_repo("main", {"src/app.py": "print(1)\n"})
    dirty(w)
    cache = cache_for(w)
    r = snap(g, cache, tmp, w)
    out = checkout_commit(cache, r.commit, tmp_path / "restored")
    assert tree_digest(out) == tree_digest(w, covered(w))
    assert tree_digest(out)["bin/run.sh"][0] == "exec" and tree_digest(out)["link"][0] == "link"


def test_parent_is_head_branch_recorded_unborn_has_no_parent(kit, worker_repo, tmp_path):
    g, cache_for, tmp = kit
    w = worker_repo("main")
    head = git("rev-parse", "HEAD", cwd=w)
    write_files(w, {"a.txt": "a\n"})
    cache = cache_for(w)
    r = snap(g, cache, tmp, w)
    gc = g.bare_cache(cache)
    assert r.head == head and gc.out("rev-parse", f"{r.commit}^") == head and r.branch == "main"
    assert "relay snapshot j/M0 seq 1" in gc.out("log", "-1", "--format=%s", r.commit)
    u = worker_repo("unborn", {"f.txt": "f\n"}, commit=False)
    cache2 = cache_for(u, name="c2.git")
    r2 = snap(g, cache2, tmp, u, task="M1")
    assert r2.status == "changed" and r2.head is None
    assert g.bare_cache(cache2).out("rev-list", "--parents", "-1", r2.commit) == r2.commit


def test_explicit_parent_overrides_head(kit, worker_repo):
    g, cache_for, tmp = kit
    w = worker_repo("main")
    first = git("rev-parse", "HEAD", cwd=w)
    write_files(w, {"b.txt": "b\n"})
    git("add", "-A", cwd=w)
    git("commit", "-q", "-m", "second", cwd=w)
    cache = cache_for(w)
    r = snap(g, cache, tmp, w, parent=first)
    assert g.bare_cache(cache).out("rev-parse", f"{r.commit}^") == first


def test_unchanged_tree_returns_unchanged_then_changed(kit, worker_repo):
    g, cache_for, tmp = kit
    w = worker_repo("main")
    cache = cache_for(w)
    r1 = snap(g, cache, tmp, w)
    last = {"tree": r1.tree, "head": r1.head, "commit": r1.commit}
    r2 = snap(g, cache, tmp, w, last=last, seq=2)
    assert r2.status == "unchanged" and r2.commit == r1.commit
    write_files(w, {"c.txt": "c\n"})
    r3 = snap(g, cache, tmp, w, last=last, seq=3)
    assert r3.status == "changed" and r3.commit != r1.commit
    git("add", "-A", cwd=w)
    git("commit", "-q", "-m", "c", cwd=w)          # same content, new HEAD -> changed
    r4 = snap(g, cache, tmp, w, last={"tree": r3.tree, "head": r3.head, "commit": r3.commit}, seq=4)
    assert r4.status == "changed" and r4.tree == r3.tree and r4.head != r3.head


def test_empty_tree_guard_refuses_wiped_worktree(kit, worker_repo):
    g, cache_for, tmp = kit
    w = worker_repo("main")
    cache = cache_for(w)
    for p in list(w.iterdir()):
        if p.name != ".git":
            p.unlink()
    r = snap(g, cache, tmp, w)
    assert r.status == "refused_empty" and r.commit is None and r.tree == EMPTY_TREE


def test_missing_worktree_is_error_not_exception(kit, tmp_path, worker_repo):
    g, cache_for, tmp = kit
    w = worker_repo("main")
    cache = cache_for(w)
    assert snap(g, cache, tmp, tmp_path / "gone").status == "error"
    plain = tmp_path / "plain"
    plain.mkdir()
    assert snap(g, cache, tmp, plain).status == "error"


def test_secret_named_files_never_enter_a_snapshot(kit, worker_repo):
    g, cache_for, tmp = kit
    w = worker_repo("main")
    write_files(w, {".env": "TOKEN=x\n", "keys/server.pem": "k\n", "id_rsa": "k\n", "notes.md": "ok\n"})
    cache = cache_for(w)
    r = snap(g, cache, tmp, w, secret_globs=SECRETS)
    files = set(g.bare_cache(cache).out("ls-tree", "-r", "--name-only", r.commit).splitlines())
    assert "notes.md" in files and not files & {".env", "keys/server.pem", "id_rsa"}
    assert sorted(r.skipped) == [".env", "id_rsa", "keys/server.pem"]
    assert (w / ".env").exists()                    # the worker's file is untouched


def test_salvage_snapshot_holds_uncommitted_work_on_top_of_head(kit, worker_repo):
    g, cache_for, tmp = kit
    w = worker_repo("main")
    head = git("rev-parse", "HEAD", cwd=w)
    write_files(w, {"late.txt": "written after the last checkpoint\n"})
    before = repo_fingerprint(w)
    cache = cache_for(w)
    r = salvage_snapshot(g, cache, job="j", task="M3", epoch=1, worktree=w, tmp_dir=tmp, secret_globs=SECRETS)
    assert r.status == "changed" and g.bare_cache(cache).out("rev-parse", f"{r.commit}^") == head
    assert g.bare_cache(cache).out("show", f"{r.commit}:late.txt") == "written after the last checkpoint"
    assert repo_fingerprint(w) == before


def test_same_size_edit_in_the_checkout_second_is_not_missed(kit, worker_repo):
    """Racy git: git compares whole-second mtimes; an edit of the same size inside the second of the last index
    write is only found when the temp index keeps the real index's mtime (a fresh copy hides the edit)."""
    import os
    g, cache_for, tmp = kit
    w = worker_repo("main", {"src/app.py": "print(1)\n"})
    git("config", "core.trustctime", "false", cwd=w)            # a test cannot set ctime: stand in for "same second"
    f, index = w / "src/app.py", w / ".git" / "index"
    past = (int(f.stat().st_mtime) - 100) * 10 ** 9
    os.utime(f, ns=(past, past))
    git("update-index", "--refresh", cwd=w, check=False)        # the entry now records the past mtime
    os.utime(index, ns=(past, past))                            # ... and the index was written in that second
    f.write_text("print(2)\n")                                  # same size, same second
    os.utime(f, ns=(past, past))
    cache = cache_for(w)
    r = snap(g, cache, tmp, w)
    assert g.bare_cache(cache).out("show", f"{r.commit}:src/app.py") == "print(2)"


def test_secret_filter_keeps_paths_head_already_tracks(kit, worker_repo):
    """A secret-looking file that HEAD tracks (e.g. .env.example) is part of the branch already: dropping it
    would turn into a deletion on the next machine. Only untracked secret-named paths are skipped."""
    g, cache_for, tmp = kit
    w = worker_repo("main")
    write_files(w, {".env.example": "TOKEN=\n"})
    git("add", ".env.example", cwd=w)
    git("commit", "-q", "-m", "add env example", cwd=w)
    write_files(w, {".env.example": "TOKEN=changed\n", ".env": "TOKEN=x\n", "new.pem": "k\n"})
    cache = cache_for(w)
    r = snap(g, cache, tmp, w, secret_globs=SECRETS + [".env.*"])
    gc = g.bare_cache(cache)
    files = set(gc.out("ls-tree", "-r", "--name-only", r.commit).splitlines())
    assert ".env.example" in files and not files & {".env", "new.pem"}
    assert gc.out("show", f"{r.commit}:.env.example") == "TOKEN=changed"
    assert sorted(r.skipped) == [".env", "new.pem"]
