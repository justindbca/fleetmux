"""relay.materialize + relay.checkpoint: a new owner's work tree is byte-identical to the last checkpoint (R8);
a checkpoint moves the task record and the snap ref together and never touches the worker (R5)."""
import pytest

import relay_kit
from relay_kit import checkout_commit, git, isolated_git, remote_refs, repo_fingerprint, tree_digest, write_files  # noqa: F401,E501
from fleetmux.relay import records as R
from fleetmux.relay.checkpoint import take_checkpoint
from fleetmux.relay.materialize import materialize


@pytest.fixture
def two(tmp_path):
    hub, base, ms = relay_kit.machines(tmp_path, names=("laptop", "desktop"),
                                       files={"README.md": "demo\n", "src/app.py": "print(1)\n",
                                              ".gitignore": "*.ignored\n"})
    return hub, base, ms


def claim(m, task="M3", base=None):
    lst = m.hub.list()
    tasks = m.hub.tasks(lst)
    if task not in tasks:
        assert m.hub.create_task(R.new_task(job="j", id=task, title="core", weight="heavy",
                                            dir=f".relay/j/{task}", base=base)).won
        tasks = m.hub.tasks(m.hub.list())
    view = tasks[task]
    res = m.hub.cas_task(task, view.sha, R.claim(view.record, me=m.name))
    assert res.won
    return m.hub.tasks(m.hub.list())[task]


def place(m, view):
    return materialize(m.git, m.cache, m.hub, job="j", rec=view.record, worktree=m.worktrees / "j" / view.record["id"],
                       marker_dir=m.state_dir / "markers")


def test_first_owner_starts_from_base_on_its_branch(two):
    hub, base, ms = two
    a = ms["laptop"]
    view = claim(a, base=base)
    p = place(a, view)
    assert p.branch == "relay/j/M3/e1-laptop" and p.start == {"commit": base, "source": "base"}
    assert git("rev-parse", "HEAD", cwd=p.worktree) == base
    assert git("symbolic-ref", "--short", "HEAD", cwd=p.worktree) == p.branch
    assert git("remote", "get-url", "origin", cwd=p.worktree) == str(hub)
    assert (p.worktree / "src/app.py").read_text() == "print(1)\n"
    git("commit", "-q", "--allow-empty", "-m", "wip: start", cwd=p.worktree)
    git("push", "-q", "origin", p.branch, cwd=p.worktree)             # the worker pushes its own branch
    assert remote_refs(hub)["refs/heads/relay/j/M3/e1-laptop"] == git("rev-parse", "HEAD", cwd=p.worktree)


def test_checkpoint_pushes_record_and_snap_atomically_worker_untouched(two):
    hub, base, ms = two
    a = ms["laptop"]
    view = claim(a, base=base)
    p = place(a, view)
    write_files(p.worktree, {"src/app.py": "print(2)\n", ".relay/j/M3/notes/dev-1/PROGRESS.md":
                             "## Done\n- a\n## Doing\n- b\n## Next\n- c\n## Dead ends\n- none\n## Red tests\n- none\n"})
    before = repo_fingerprint(p.worktree)
    cp = take_checkpoint(a.hub, task="M3", view=view, me="laptop", worktree=p.worktree, tmp_dir=a.tmp, last=None,
                         settings=a.settings, kind="phase", phase="design")
    assert cp.outcome == "ok", cp
    assert repo_fingerprint(p.worktree) == before
    refs = remote_refs(hub)
    assert refs["refs/relay/j/snap/M3"] == cp.snap.commit and refs["refs/relay/j/tasks/M3"] == cp.record_sha
    rec = a.hub.tasks(a.hub.list())["M3"].record
    assert rec["checkpoint"]["snap"] == cp.snap.commit and rec["checkpoint"]["commit"] == base
    assert rec["checkpoint"]["kind"] == "phase" and rec["checkpoint"]["phase"] == "design"
    assert rec["progress"]["path"] == ".relay/j/M3/notes/dev-1/PROGRESS.md" and rec["progress"]["doing"] == ["b"]
    again = take_checkpoint(a.hub, task="M3", view=a.hub.tasks(a.hub.list())["M3"], me="laptop", worktree=p.worktree,
                            tmp_dir=a.tmp, last=cp.last, settings=a.settings)
    assert again.outcome == "unchanged"


def test_takeover_tree_is_byte_identical_to_the_last_snapshot(two, tmp_path):
    hub, base, ms = two
    a, d = ms["laptop"], ms["desktop"]
    view = claim(a, base=base)
    p = place(a, view)
    write_files(p.worktree, {"src/app.py": "print(3)\n", "new/file.txt": "uncommitted\n",
                             "bin/run.sh": ("exec", "#!/bin/sh\n"), "link": ("link", "README.md"),
                             "x.ignored": "never\n"})
    git("add", "src/app.py", cwd=p.worktree)
    cp = take_checkpoint(a.hub, task="M3", view=view, me="laptop", worktree=p.worktree, tmp_dir=a.tmp, last=None,
                         settings=a.settings)
    assert cp.outcome == "ok"
    tv = d.hub.tasks(d.hub.list())["M3"]
    res = d.hub.cas_task("M3", tv.sha, R.takeover(tv.record, to="desktop", why=R.WHY_LOST))
    assert res.won
    q = place(d, d.hub.tasks(d.hub.list())["M3"])
    assert q.branch == "relay/j/M3/e2-desktop" and q.start["source"] == "snap"
    expected = checkout_commit(hub, cp.snap.commit, tmp_path / "expected")
    assert tree_digest(q.worktree) == tree_digest(expected)
    assert not (q.worktree / "x.ignored").exists() and (q.worktree / "new/file.txt").read_text() == "uncommitted\n"


def test_materialize_is_idempotent_and_never_touches_foreign_content(two):
    hub, base, ms = two
    a = ms["laptop"]
    view = claim(a, base=base)
    p1 = place(a, view)
    write_files(p1.worktree, {"mine.txt": "keep\n"})
    p2 = place(a, view)
    assert p2.worktree == p1.worktree and (p1.worktree / "mine.txt").exists()
    other = a.worktrees / "j" / "M9"
    write_files(other, {"foreign.txt": "not ours\n"})
    fake = dict(view.record, id="M9", branch="relay/j/M9/e1-laptop")
    with pytest.raises(OSError, match="foreign"):
        materialize(a.git, a.cache, a.hub, job="j", rec=fake, worktree=other, marker_dir=a.state_dir / "markers")
    assert (other / "foreign.txt").read_text() == "not ours\n"


def test_checkpoint_after_losing_the_task_is_lost_and_writes_nothing(two):
    hub, base, ms = two
    a, d = ms["laptop"], ms["desktop"]
    view = claim(a, base=base)
    p = place(a, view)
    tv = d.hub.tasks(d.hub.list())["M3"]
    assert d.hub.cas_task("M3", tv.sha, R.takeover(tv.record, to="desktop", why=R.WHY_LOST)).won
    write_files(p.worktree, {"late.txt": "x\n"})
    before = remote_refs(hub)
    cp = take_checkpoint(a.hub, task="M3", view=view, me="laptop", worktree=p.worktree, tmp_dir=a.tmp, last=None,
                         settings=a.settings)
    assert cp.outcome == "lost" and remote_refs(hub) == before


def test_tracked_secret_named_file_survives_takeover_untracked_secret_does_not(two):
    hub, base, ms = two
    a, d = ms["laptop"], ms["desktop"]
    assert ".env.*" in a.settings.secret_globs and ".env" in a.settings.secret_globs
    view = claim(a, base=base)
    p = place(a, view)
    write_files(p.worktree, {".env.example": "TOKEN=\n"})
    git("add", ".env.example", cwd=p.worktree)
    git("commit", "-q", "-m", "add env example", cwd=p.worktree)
    write_files(p.worktree, {".env": "TOKEN=secret\n"})
    cp = take_checkpoint(a.hub, task="M3", view=view, me="laptop", worktree=p.worktree, tmp_dir=a.tmp, last=None,
                         settings=a.settings)
    assert cp.outcome == "ok" and cp.snap.skipped == [".env"]
    tv = d.hub.tasks(d.hub.list())["M3"]
    assert d.hub.cas_task("M3", tv.sha, R.takeover(tv.record, to="desktop", why=R.WHY_LOST)).won
    q = place(d, d.hub.tasks(d.hub.list())["M3"])
    assert (q.worktree / ".env.example").read_text() == "TOKEN=\n"
    assert not (q.worktree / ".env").exists()
