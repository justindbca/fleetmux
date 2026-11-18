"""relay.gitio + relay.refs: the git runner, the ref layout on the one hub, ls-remote and the CAS push."""
import threading

import pytest

import relay_kit
from relay_kit import isolated_git, mk_cache, mk_git, git, remote_refs  # noqa: F401
from fleetmux.relay.errors import ConfigError
from fleetmux.relay.gitio import Git, GitError, short_error
from fleetmux.relay.refs import (EMPTY, RefNames, RefUpdate, branch_glob, cas_push, check_job, check_node,
                                 check_task_id, classify, fetch, ls_remote, merged_branch, read_json_commit,
                                 task_branch, write_json_commit)


def rec(g, cache, n, parents=()):
    return write_json_commit(g, cache, "x.json", {"n": n}, parents=list(parents), message=f"r{n}")


@pytest.fixture
def hub(tmp_path):
    return relay_kit.bare(tmp_path / "hub.git")


# ----------------------------------------------------------------------------- git runner


def test_git_run_sets_safety_env_and_config(tmp_path):
    g = mk_git()
    assert g.out("config", "--get", "core.fsmonitor", cwd=tmp_path) == "false"
    assert g.out("config", "--get", "gc.auto", cwd=tmp_path) == "0"
    assert g.out("config", "--get", "maintenance.auto", cwd=tmp_path) == "false"
    env = g.out("-c", "alias.e=!env", "e", cwd=tmp_path).splitlines()
    assert "GIT_OPTIONAL_LOCKS=0" in env and "GIT_TERMINAL_PROMPT=0" in env


def test_git_error_carries_details_and_short_reason(tmp_path):
    with pytest.raises(GitError) as e:
        mk_git().run("rev-parse", "HEAD", cwd=tmp_path)
    assert e.value.returncode != 0 and not e.value.timed_out and "rev-parse" in " ".join(e.value.argv)
    assert "rev-parse" not in short_error(e.value)


def test_git_timeout_kills_and_flags(tmp_path):
    git("init", "-q", str(tmp_path / "r"))
    with pytest.raises(GitError) as e:
        Git("git", lambda: 0.3).run("-c", "alias.slow=!sleep 5", "slow", cwd=tmp_path / "r")
    assert e.value.timed_out and short_error(e.value) == "timed out"


def test_bare_cache_binds_git_dir(tmp_path):
    cache = mk_cache(tmp_path / "c.git")
    assert mk_git().bare_cache(cache).out("rev-parse", "--is-bare-repository") == "true"


# ----------------------------------------------------------------------------- layout


def test_ref_layout_matches_the_interface():
    n = RefNames("job-1")
    assert n.task("M3") == "refs/relay/job-1/tasks/M3"
    assert n.node("laptop") == "refs/relay/job-1/nodes/laptop"
    assert n.snap("M3") == "refs/relay/job-1/snap/M3"
    assert n.salvage("M3", "laptop", 1) == "refs/relay/job-1/salvage/M3/laptop-e1"
    assert n.all_glob() == "refs/relay/job-1/*"
    assert task_branch("job-1", "M3", 2, "desktop") == "relay/job-1/M3/e2-desktop"
    assert merged_branch("job-1") == "relay/job-1/merged"
    assert branch_glob("job-1") == "refs/heads/relay/job-1/*"
    assert not hasattr(n, "diverged")


def test_ref_names_parse_every_layout_ref():
    n = RefNames("j")
    assert n.parse(n.task("M3")) == ("task", "M3")
    assert n.parse(n.task("merge")) == ("task", "merge")
    assert n.parse(n.node("server")) == ("node", "server")
    assert n.parse(n.snap("M0")) == ("snap", "M0")
    assert n.parse(n.salvage("M3", "laptop", 4)) == ("salvage", "M3", "laptop", 4)
    for bad in ("refs/relay/j/diverged/M3/abc", "refs/relay/j/tasks/M3/record", "refs/relay/j/nodes/Laptop",
                "refs/relay/other/tasks/M3", "refs/relay/j/salvage/M3/laptop"):
        assert n.parse(bad) is None, bad


@pytest.mark.parametrize("job, ok", [("job-1", True), ("a.b_c", True), ("", False), ("a/b", False),
                                     ("x" * 65, False), ("a..b", False)])
def test_check_job(job, ok):
    if ok:
        check_job(job)
    else:
        with pytest.raises(ConfigError):
            check_job(job)


@pytest.mark.parametrize("node, ok", [("laptop", True), ("server-2", True), ("Laptop", False), ("-a", False),
                                      ("a" * 41, False)])
def test_check_node(node, ok):
    if ok:
        check_node(node)
    else:
        with pytest.raises(ConfigError):
            check_node(node)


def test_task_ids_and_reserved_names():
    check_task_id("M0")
    for bad in ("", "-x", "a/b", "a..b", "x.lock", "y" * 49):
        with pytest.raises(ConfigError):
            check_task_id(bad)
    for name in ("merge", "coordinator"):
        with pytest.raises(ConfigError, match="reserved"):
            check_task_id(name)
        check_task_id(name, reserved_ok=True)
    with pytest.raises(ConfigError, match="reserved"):
        check_task_id("merged", reserved_ok=True)


# ----------------------------------------------------------------------------- records, ls-remote


def test_json_commit_roundtrip_and_identity(tmp_path):
    g, cache = mk_git(), mk_cache(tmp_path / "c.git")
    c = write_json_commit(g, cache, "task.json", {"a": 1}, parents=[], message="m")
    assert read_json_commit(g, cache, c, "task.json") == {"a": 1}
    assert g.bare_cache(cache).out("log", "-1", "--format=%an <%ae>", c) == "fleetmux-relay <relay@localhost>"


def test_ls_remote_exact_and_glob(tmp_path, hub):
    g, cache = mk_git(), mk_cache(tmp_path / "c.git")
    a = rec(g, cache, 1)
    n = RefNames("j")
    ups = [RefUpdate(n.task("M0"), a, EMPTY), RefUpdate(n.node("laptop"), a, EMPTY),
           RefUpdate("refs/relay/other/tasks/M0", a, EMPTY)]
    assert cas_push(g, cache, str(hub), ups, timeout_s=20).accepted
    assert set(ls_remote(g, str(hub), [n.all_glob()])) == {n.task("M0"), n.node("laptop")}
    assert ls_remote(g, str(hub), [n.task("M0")]) == {n.task("M0"): a}


def test_ls_remote_failure_raises(tmp_path):
    with pytest.raises(GitError):
        ls_remote(mk_git(), str(tmp_path / "missing.git"), ["refs/relay/j/*"])


# ----------------------------------------------------------------------------- CAS push


def test_cas_create_only_when_absent(tmp_path, hub):
    g, cache = mk_git(), mk_cache(tmp_path / "c.git")
    ref = "refs/relay/j/tasks/M0"
    a, b = rec(g, cache, 1), rec(g, cache, 2)
    assert cas_push(g, cache, str(hub), [RefUpdate(ref, a, EMPTY)], timeout_s=20).outcome == "ok"
    r = cas_push(g, cache, str(hub), [RefUpdate(ref, b, EMPTY)], timeout_s=20)
    assert not r.accepted and r.outcome == "stale"
    assert remote_refs(hub)[ref] == a


def test_cas_stale_expectation_refuses_the_whole_atomic_push(tmp_path, hub):
    g, cache = mk_git(), mk_cache(tmp_path / "c.git")
    task, snap = "refs/relay/j/tasks/M0", "refs/relay/j/snap/M0"
    t0, t1, t2, s1 = rec(g, cache, 0), rec(g, cache, 1), rec(g, cache, 2), rec(g, cache, 9)
    assert cas_push(g, cache, str(hub), [RefUpdate(task, t0, EMPTY)], timeout_s=20).accepted
    assert cas_push(g, cache, str(hub), [RefUpdate(task, t1, t0)], timeout_s=20).accepted
    r = cas_push(g, cache, str(hub), [RefUpdate(task, t2, t0), RefUpdate(snap, s1, None)], timeout_s=20)
    assert not r.accepted and r.outcome == "stale", r.raw
    assert remote_refs(hub) == {task: t1}


def test_forced_update_without_lease(tmp_path, hub):
    g, cache = mk_git(), mk_cache(tmp_path / "c.git")
    ref = "refs/relay/j/snap/M0"
    a, b = rec(g, cache, 1), rec(g, cache, 2)
    assert cas_push(g, cache, str(hub), [RefUpdate(ref, a, None)], timeout_s=20).accepted
    assert cas_push(g, cache, str(hub), [RefUpdate(ref, b, None)], timeout_s=20).accepted
    assert remote_refs(hub)[ref] == b


def test_push_by_url_writes_no_remote_tracking_refs(tmp_path, hub):
    g, cache = mk_git(), mk_cache(tmp_path / "c.git")
    assert cas_push(g, cache, str(hub), [RefUpdate("refs/relay/j/tasks/M0", rec(g, cache, 1), EMPTY)],
                    timeout_s=20).accepted
    gc = g.bare_cache(cache)
    assert gc.out("for-each-ref") == ""
    assert gc.run("config", "--get-regexp", "^remote\\.", check=False).stdout.strip() == b""


def test_push_unreachable_is_error(tmp_path):
    g, cache = mk_git(), mk_cache(tmp_path / "c.git")
    r = cas_push(g, cache, str(tmp_path / "missing.git"), [RefUpdate("refs/relay/j/tasks/M0", rec(g, cache, 1),
                                                                     EMPTY)], timeout_s=20)
    assert not r.accepted and r.outcome == "error"


def test_push_timeout_is_outcome_unknown(tmp_path, hub):
    hook = hub / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nsleep 3\n")
    hook.chmod(0o755)
    g, cache = mk_git(), mk_cache(tmp_path / "c.git")
    r = cas_push(g, cache, str(hub), [RefUpdate("refs/relay/j/tasks/M0", rec(g, cache, 1), EMPTY)], timeout_s=0.5)
    assert not r.accepted and r.outcome == "timeout"


def test_push_refused_by_server_hook_is_rejected(tmp_path, hub):
    hook = hub / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho no >&2\nexit 1\n")
    hook.chmod(0o755)
    g, cache = mk_git(), mk_cache(tmp_path / "c.git")
    r = cas_push(g, cache, str(hub), [RefUpdate("refs/relay/j/tasks/M0", rec(g, cache, 1), EMPTY)], timeout_s=20)
    assert not r.accepted and r.outcome == "rejected", r.raw


def test_refusal_texts_are_classified():
    assert classify("! [rejected] x -> y (non-fast-forward)") == "nonff"
    assert classify("! [rejected] x -> y (fetch first)") == "nonff"
    assert classify("! [rejected] x -> y (stale info)") == "stale"
    assert classify("cannot lock ref 'refs/relay/j/tasks/M0': is at a but expected b") == "lock"
    assert classify("pre-receive hook declined") == "rejected"


def _race(g, caches, hub, updates):
    results = {}
    barrier = threading.Barrier(len(caches))

    def go(name):
        barrier.wait()
        results[name] = cas_push(g, caches[name], str(hub), updates[name], timeout_s=20)

    threads = [threading.Thread(target=go, args=(n,)) for n in caches]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_three_machines_claim_at_once_exactly_one_wins_under_server_lock(tmp_path, hub):
    """Every push passes the client-side lease check (a pre-receive hook holds them until all arrived); the
    server's ref lock lets exactly one through (R8)."""
    g = mk_git()
    names = ("laptop", "desktop", "server")
    caches = {n: mk_cache(tmp_path / f"{n}.git") for n in names}
    ref = "refs/relay/j/tasks/M0"
    t0 = rec(g, caches["laptop"], 0)
    assert cas_push(g, caches["laptop"], str(hub), [RefUpdate(ref, t0, EMPTY)], timeout_s=20).accepted
    current = t0
    for i in range(5):                              # a fresh gate per round: the barrier counts arrivals once
        for n in names:
            fetch(g, caches[n], str(hub), [ref], timeout_s=20)
        relay_kit.install_barrier(hub, len(names), tmp_path / f"gate{i}")
        news = {n: rec(g, caches[n], f"claim-{n}-{i}", [current]) for n in names}
        res = _race(g, caches, hub, {n: [RefUpdate(ref, news[n], current)] for n in names})
        won = [n for n, r in res.items() if r.accepted]
        raw = (i, {n: r.raw for n, r in res.items()})
        assert len(won) == 1, raw
        assert sorted(r.outcome for r in res.values()) == ["lock", "lock", "ok"], raw
        current = news[won[0]]
        assert remote_refs(hub)[ref] == current


def test_concurrent_cas_exactly_one_wins_repeated(tmp_path, hub):
    g = mk_git()
    caches = {"a": mk_cache(tmp_path / "a.git"), "b": mk_cache(tmp_path / "b.git")}
    ref = "refs/relay/j/tasks/M0"
    current = rec(g, caches["a"], 0)
    assert cas_push(g, caches["a"], str(hub), [RefUpdate(ref, current, EMPTY)], timeout_s=20).accepted
    for i in range(10):
        news = {n: rec(g, c, f"{n}{i}") for n, c in caches.items()}
        res = _race(g, caches, hub, {n: [RefUpdate(ref, news[n], current)] for n in caches})
        won = [n for n, r in res.items() if r.accepted]
        assert len(won) == 1, {n: r.raw for n, r in res.items()}
        assert res["b" if won == ["a"] else "a"].outcome in ("stale", "lock")
        current = news[won[0]]
        assert remote_refs(hub)[ref] == current


def test_fetch_brings_objects_into_another_cache(tmp_path, hub):
    g = mk_git()
    ca, cb = mk_cache(tmp_path / "a.git"), mk_cache(tmp_path / "b.git")
    c = write_json_commit(g, ca, "node.json", {"beat": 3}, parents=[], message="n")
    ref = "refs/relay/j/nodes/laptop"
    assert cas_push(g, ca, str(hub), [RefUpdate(ref, c, EMPTY)], timeout_s=20).accepted
    fetch(g, cb, str(hub), [ref], timeout_s=20)
    assert read_json_commit(g, cb, c, "node.json") == {"beat": 3}
