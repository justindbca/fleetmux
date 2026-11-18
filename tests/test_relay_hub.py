"""relay.repo + relay.hub: the cache repo and the one state remote (no meeting points, no copies, no arbiter)."""
import pytest

import relay_kit
from relay_kit import isolated_git, git, remote_refs, three_machines  # noqa: F401
from fleetmux.relay import records as R
from fleetmux.relay import repo
from fleetmux.relay.refs import EMPTY, RefUpdate


def new(m, id="M0", **kw):
    args = dict(job=m.job, id=id, title=f"task {id}", weight="light", dir=f".relay/{m.job}/{id}", base=kw.pop("base",
                                                                                                             None))
    args.update(kw)
    return R.new_task(**args)


@pytest.fixture
def three(tmp_path):
    return relay_kit.machines(tmp_path)


def test_cache_repo_is_created_by_the_program_and_bare(tmp_path):
    g = relay_kit.mk_git()
    path = repo.ensure_cache(g, tmp_path / "state" / "j" / "cache.git")
    assert g.bare_cache(path).out("rev-parse", "--is-bare-repository") == "true"
    assert repo.ensure_cache(g, path) == path                  # idempotent
    assert repo.has_commit(g, path, "a" * 40) is False


def test_empty_hub_lists_ok(three):
    hub, base, ms = three
    lst = ms["laptop"].hub.list()
    assert lst.ok and lst.refs == {} and ms["laptop"].hub.tasks(lst) == {}


def test_created_task_is_read_by_every_machine(three):
    hub, base, ms = three
    rec = new(ms["laptop"], base=base)
    res = ms["laptop"].hub.create_task(rec)
    assert res.won and remote_refs(hub)["refs/relay/j/tasks/M0"] == res.record_sha
    for m in ms.values():
        view = m.hub.tasks(m.hub.list())["M0"]
        assert view.sha == res.record_sha and view.record == rec


def test_second_create_of_the_same_task_is_a_race_lost(three):
    hub, base, ms = three
    assert ms["laptop"].hub.create_task(new(ms["laptop"], base=base)).won
    res = ms["desktop"].hub.create_task(new(ms["desktop"], base=base))
    assert not res.won and res.raced and not res.unknown


def test_cas_task_moves_only_from_the_expected_record(three):
    hub, base, ms = three
    a, d = ms["laptop"], ms["desktop"]
    first = a.hub.create_task(new(a, base=base))
    view = d.hub.tasks(d.hub.list())["M0"]
    claim_d = d.hub.cas_task("M0", view.sha, R.claim(view.record, me="desktop"))
    assert claim_d.won
    claim_a = a.hub.cas_task("M0", first.record_sha, R.claim(view.record, me="laptop"))   # stale view
    assert claim_a.raced
    assert a.hub.tasks(a.hub.list())["M0"].record["owner"] == "desktop"


def test_checkpoint_record_and_snap_ref_move_atomically(three):
    hub, base, ms = three
    a = ms["laptop"]
    created = a.hub.create_task(new(a, base=base))
    claimed = a.hub.cas_task("M0", created.record_sha, R.claim(a.hub.tasks(a.hub.list())["M0"].record, me="laptop"))
    snap_ref = a.hub.names.snap("M0")
    rec = a.hub.tasks(a.hub.list())["M0"].record
    a.hub.fetch_objects(["refs/heads/main"], remote="code")
    ok = a.hub.cas_task("M0", claimed.record_sha, R.checkpoint(rec, commit=base, snap=base, kind="auto"),
                        [RefUpdate(snap_ref, base, None)])
    assert ok.won and remote_refs(hub)[snap_ref] == base
    seed = relay_kit.git("rev-parse", f"{base}^{{tree}}", cwd=a.cache)
    newer = relay_kit.git("commit-tree", seed, "-p", base, "-m", "snap 2", cwd=a.cache)
    bad = a.hub.cas_task("M0", claimed.record_sha, R.checkpoint(rec, commit=base, snap=newer, kind="auto"),
                         [RefUpdate(snap_ref, newer, None)])            # stale record lease: snap must not move
    assert bad.raced and remote_refs(hub)["refs/relay/j/tasks/M0"] == ok.record_sha
    assert remote_refs(hub)[snap_ref] == base


def test_push_timeout_is_unknown(three):
    hub, base, ms = three
    hook = hub / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nsleep 3\n")
    hook.chmod(0o755)
    res = ms["laptop"].hub.create_task(new(ms["laptop"], base=base), timeout_s=0.5)
    assert res.unknown and not res.won and res.record_sha


def test_unreachable_hub_listing_is_not_ok(tmp_path):
    hub, base, ms = relay_kit.machines(tmp_path, names=("laptop",))
    m = ms["laptop"]
    m.hub.state_remote = str(tmp_path / "gone.git")
    lst = m.hub.list()
    assert not lst.ok and lst.error and m.hub.tasks(lst) == {} and m.hub.nodes(lst) == {}


def test_node_record_one_ref_per_machine_latest_wins(three):
    hub, base, ms = three
    for m in ms.values():
        n0 = R.new_node(job="j", node=m.name, machine=relay_kit.machine_info(), state="online")
        assert m.hub.push_node(n0).accepted
        assert m.hub.push_node(R.next_node(n0, state="online")).accepted
    nodes = ms["server"].hub.nodes(ms["server"].hub.list())
    assert sorted(nodes) == ["desktop", "laptop", "server"]
    assert all(v.record["beat"] == 1 and v.record["state"] == "online" for v in nodes.values())
    assert sorted(r for r in remote_refs(hub) if "/nodes/" in r) == [
        "refs/relay/j/nodes/desktop", "refs/relay/j/nodes/laptop", "refs/relay/j/nodes/server"]


def test_invalid_record_on_the_hub_is_skipped_and_reported(three):
    hub, base, ms = three
    a = ms["laptop"]
    junk = R.write_task(a.git, a.cache, new(a, base=base), parent=None)
    bad = relay_kit.git("commit-tree", "4b825dc642cb6eb9a060e54bf8d69288fbee4904", "-m", "junk", cwd=a.cache)
    from fleetmux.relay.refs import cas_push
    assert cas_push(a.git, a.cache, str(hub), [RefUpdate("refs/relay/j/tasks/M9", bad, EMPTY),
                                               RefUpdate("refs/relay/j/tasks/M0", junk, EMPTY)],
                    timeout_s=20).accepted
    d = ms["desktop"]
    tasks = d.hub.tasks(d.hub.list())
    assert list(tasks) == ["M0"] and "M9" in d.hub.bad


def test_salvage_ref_is_create_only(three):
    hub, base, ms = three
    a = ms["laptop"]
    a.hub.fetch_objects(["refs/heads/main"], remote="code")
    assert a.hub.push_salvage("M3", "laptop", 1, base).accepted
    assert remote_refs(hub)["refs/relay/j/salvage/M3/laptop-e1"] == base
    assert a.hub.push_salvage("M3", "laptop", 1, base).accepted                 # same value again: no change
    other = git("commit-tree", git("rev-parse", f"{base}^{{tree}}", cwd=a.cache), "-m", "x", cwd=a.cache)
    assert a.hub.push_salvage("M3", "laptop", 1, other).outcome == "stale"


def test_separate_state_remote_holds_relay_refs_code_remote_branches(tmp_path):
    state = relay_kit.bare(tmp_path / "state.git")
    hub, base, ms = relay_kit.machines(tmp_path, names=("laptop",), state_remote=str(tmp_path / "state.git"))
    m = ms["laptop"]
    assert m.hub.create_task(new(m, base=base)).won
    m.hub.fetch_objects(["refs/heads/main"], remote="code")
    assert m.hub.push_branch("relay/j/M0/e1-laptop", base).accepted
    assert "refs/relay/j/tasks/M0" in remote_refs(state) and "refs/relay/j/tasks/M0" not in remote_refs(hub)
    assert remote_refs(hub)["refs/heads/relay/j/M0/e1-laptop"] == base
    assert "refs/heads/relay/j/M0/e1-laptop" not in remote_refs(state)


def test_three_machines_fixture_is_a_real_bare_hub_and_three_temp_dirs(three_machines, tmp_path):
    hub, base, ms = three_machines
    assert git("rev-parse", "--is-bare-repository", cwd=hub) == "true"
    assert git("rev-parse", "main", cwd=hub) == base
    assert sorted(ms) == ["desktop", "laptop", "server"]
    roots = {m.root for m in ms.values()}
    assert len(roots) == 3 and all(r.is_relative_to(tmp_path) for r in roots)
    for m in ms.values():
        assert git("rev-parse", "HEAD", cwd=m.clone) == base and m.cache.is_dir()
    assert ms["laptop"].hub.create_task(new(ms["laptop"], base=base)).won
    assert "M0" in ms["server"].hub.tasks(ms["server"].hub.list())
