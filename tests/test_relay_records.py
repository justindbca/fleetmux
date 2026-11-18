"""relay.records: task.json and node.json and every change of a task record."""
import pytest

import relay_kit
from relay_kit import isolated_git, mk_cache, mk_git  # noqa: F401
from fleetmux.relay import records as R
from fleetmux.relay.errors import ConfigError, RecordError, TransitionError

SHA_A, SHA_B, SHA_C = "a" * 40, "b" * 40, "c" * 40


def task(id="M3", **kw):
    args = dict(job="j", id=id, title="protocol core", weight="heavy", dir=f".relay/j/{id}", needs=["rust"],
                prefer="laptop", base=SHA_A)
    args.update(kw)
    return R.new_task(**args)


def test_new_task_has_every_interface_field():
    t = task()
    assert set(t) == set(R.TASK_FIELDS)
    assert t["state"] == "pending" and t["epoch"] == 0 and t["seq"] == 0 and t["owner"] is None
    assert t["needs"] == ["rust"] and t["prefer"] == "laptop" and t["dir"] == ".relay/j/M3"
    assert t["depends_on"] == [] and t["merge_in"] == [] and t["checkpoint"] is None


def test_task_with_dependencies_starts_blocked_and_cannot_be_claimed():
    t = task("M1", depends_on=["M3"], base=None)
    assert t["state"] == "blocked" and not R.claimable(t)
    with pytest.raises(TransitionError):
        R.claim(t, me="desktop")


def test_unblock_sets_base_and_merge_in():
    t = R.unblock(task("merge", depends_on=["M0", "M1"], base=None, reserved_ok=True), base=SHA_B,
                  merge_in=[SHA_C])
    assert t["state"] == "pending" and t["base"] == SHA_B and t["merge_in"] == [SHA_C] and t["seq"] == 1
    with pytest.raises(TransitionError):
        R.unblock(t, base=SHA_B)


def test_reserved_and_bad_task_definitions_refused():
    with pytest.raises(ConfigError):
        task("merge")
    with pytest.raises(ConfigError):
        task("merged", reserved_ok=True)
    with pytest.raises(RecordError):
        task(weight="huge")
    with pytest.raises(RecordError):
        task("M1", depends_on=["M1"])


def test_every_change_bumps_seq_and_nonce():
    t0 = task()
    t1 = R.claim(t0, me="laptop")
    t2 = R.checkpoint(t1, commit=SHA_B, snap=SHA_C, kind="auto")
    t3 = R.hand_over(t2)
    seen = {t["nonce"] for t in (t0, t1, t2, t3)}
    assert [t["seq"] for t in (t0, t1, t2, t3)] == [0, 1, 2, 3] and len(seen) == 4


def test_claim_owner_epoch_branch_owners():
    t = R.claim(task(), me="laptop")
    assert t["state"] == "working" and t["owner"] == "laptop" and t["epoch"] == 1
    assert t["branch"] == "relay/j/M3/e1-laptop"
    assert t["owners"] == [{"node": "laptop", "epoch": 1, "why": "claim"}]


def test_takeover_keeps_checkpoint_and_records_why():
    t = R.checkpoint(R.claim(task(), me="laptop"), commit=SHA_B, snap=SHA_C, kind="phase", phase="design",
                     progress={"path": "p", "ok": True, "done": 1, "doing": [], "next": ["x"], "red_tests": []})
    t2 = R.takeover(t, to="desktop", why=R.WHY_LOST)
    assert t2["owner"] == "desktop" and t2["epoch"] == 2 and t2["branch"] == "relay/j/M3/e2-desktop"
    assert t2["owners"][-1] == {"node": "desktop", "epoch": 2, "why": "takeover:lost"}
    assert t2["checkpoint"] == t["checkpoint"] and t2["progress"] == t["progress"]
    assert R.start_point(t2) == {"commit": SHA_C, "source": "snap"}
    assert R.previous_owner(t2) == {"node": "laptop", "epoch": 1, "why": "claim"}


def test_takeover_of_handed_over_task_is_goodbye():
    t = R.takeover(R.hand_over(R.claim(task(), me="laptop")), to="server", why=R.WHY_GOODBYE)
    assert t["state"] == "working" and t["owners"][-1]["why"] == "takeover:goodbye"


def test_takeover_to_nobody_is_reclaimed_then_claimable():
    t = R.takeover(R.claim(task(), me="laptop"), to=None, why=R.WHY_LOST)
    assert t["state"] == "reclaimed" and t["owner"] is None and t["branch"] is None and t["epoch"] == 2
    t2 = R.claim(t, me="desktop")
    assert t2["epoch"] == 3 and t2["owner"] == "desktop"


def test_owner_dropping_before_its_first_checkpoint_keeps_the_previous_start():
    t = R.checkpoint(R.claim(task(), me="laptop"), commit=SHA_B, snap=None, kind="auto")
    t = R.takeover(t, to="desktop", why=R.WHY_LOST)              # desktop drops before any checkpoint
    t = R.takeover(t, to="server", why=R.WHY_LOST)
    assert R.start_point(t) == {"commit": SHA_B, "source": "commit"} and t["epoch"] == 3


def test_start_point_falls_back_to_base_and_needs_one():
    assert R.start_point(R.claim(task(), me="laptop")) == {"commit": SHA_A, "source": "base"}
    with pytest.raises(TransitionError):
        R.start_point(task(base=None))


def test_checkpoint_needs_commit_or_snap_and_working_state():
    t = R.claim(task(), me="laptop")
    with pytest.raises(TransitionError):
        R.checkpoint(t, commit=None, snap=None, kind="auto")
    with pytest.raises(TransitionError):
        R.checkpoint(t, commit=SHA_B, snap=None, kind="weird")
    c1 = R.checkpoint(t, commit=SHA_B, snap=None, kind="auto")
    c2 = R.checkpoint(c1, commit=SHA_B, snap=SHA_C, kind="phase", phase="tests")
    assert c2["checkpoint"] == {"commit": SHA_B, "snap": SHA_C, "kind": "phase", "phase": "tests", "seq": 2,
                                "epoch": 1}
    with pytest.raises(TransitionError):
        R.checkpoint(R.hand_over(c2), commit=SHA_B, snap=None, kind="auto")


def test_deliver_and_park():
    t = R.claim(task(), me="laptop")
    d = R.deliver(t, branch=t["branch"], commit=SHA_B, gate="pass", review="pass")
    assert d["state"] == "delivered" and d["delivered"] == {"branch": t["branch"], "commit": SHA_B, "gate": "pass",
                                                            "review": "pass"}
    with pytest.raises(TransitionError):
        R.takeover(d, to="desktop", why=R.WHY_LOST)
    p = R.park(t, reason="needs a human", by="laptop")
    assert p["state"] == "parked" and p["parked"] == {"reason": "needs a human", "by": "laptop"}


def test_decode_task_errors():
    good = task()
    for change in ({"state": "lost"}, {"epoch": "1"}, {"weight": "x"}, {"needs": "rust"}):
        with pytest.raises(RecordError):
            R.decode_task(dict(good, **change))
    with pytest.raises(RecordError):
        R.decode_task({k: v for k, v in good.items() if k != "depends_on"})
    with pytest.raises(RecordError):
        R.decode_task(dict(good, state="working", owner=None))


def test_task_record_commit_roundtrip(tmp_path):
    g, cache = mk_git(), mk_cache(tmp_path / "c.git")
    t0 = task()
    s0 = R.write_task(g, cache, t0, parent=None)
    t1 = R.claim(t0, me="laptop")
    s1 = R.write_task(g, cache, t1, parent=s0)
    assert R.read_task(g, cache, s1) == t1
    assert g.bare_cache(cache).out("rev-parse", f"{s1}^") == s0
    junk = relay_kit.mk_git().bare_cache(cache)
    with pytest.raises(RecordError):
        R.read_task(g, cache, junk.out("commit-tree", "4b825dc642cb6eb9a060e54bf8d69288fbee4904", "-m", "x"))


# ----------------------------------------------------------------------------- node.json


def test_node_record_fields_states_and_beat(tmp_path):
    m = relay_kit.machine_info(10, 32, 59.0, always_on=False, toolchains={"rust": "1.83.0"})
    n0 = R.new_node(job="j", node="laptop", machine=m, state="rejoining",
                    fleet={"name": "main", "release": "abc", "machine": "laptop"})
    assert set(n0) == set(R.NODE_FIELDS) and n0["state"] == "rejoining" and n0["beat"] == 0
    assert n0["machine"]["always_on"] is False and n0["machine"]["toolchains"] == {"rust": "1.83.0"}
    n1 = R.next_node(n0, state="online", tasks=[{"id": "M3", "state": "working", "phase": "impl", "epoch": 1}],
                     idle=False)
    assert n1["beat"] == 1 and n1["nonce"] != n0["nonce"]
    n2 = R.next_node(n1, state="leaving", leaving_reason="sleep")
    assert n2["leaving_reason"] == "sleep" and R.next_node(n2, state="rejoining")["leaving_reason"] is None
    with pytest.raises(RecordError):
        R.next_node(n1, state="leaving")
    with pytest.raises(RecordError):
        R.next_node(n1, state="gone")
    g, cache = mk_git(), mk_cache(tmp_path / "c.git")
    sha = R.write_node(g, cache, n1)
    assert R.read_node(g, cache, sha) == n1
    assert g.bare_cache(cache).out("rev-list", "--parents", "-n", "1", sha) == sha    # orphan


def test_resume_brings_a_handed_over_task_back_to_its_owner_only():
    c = R.claim(task(), me="laptop")
    h = R.hand_over(c)
    r = R.resume(h, me="laptop")
    assert r["state"] == R.WORKING and (r["owner"], r["epoch"], r["branch"]) == (c["owner"], c["epoch"], c["branch"])
    assert r["owners"] == c["owners"] and r["seq"] == h["seq"] + 1
    with pytest.raises(TransitionError):
        R.resume(h, me="server")                    # someone else takes over instead
    with pytest.raises(TransitionError):
        R.resume(c, me="laptop")                    # not handed over


def test_new_task_keeps_needs_as_given_and_refuses_non_strings():
    """needs are parsed by flow-1's placement; the record only stores non-empty strings."""
    t = R.new_task(job="j", id="M3", title="x", weight="light", dir=".relay/j/M3", needs=["node>=20", "rust"])
    assert t["needs"] == ["node>=20", "rust"]
    for bad in (["rust", ""], [3]):
        with pytest.raises(RecordError):
            R.new_task(job="j", id="M3", title="x", weight="light", dir=".relay/j/M3", needs=bad)



def test_new_node_takes_its_first_state_from_the_caller():
    """Q29: a fresh join is online; rejoining only when this node's node.json already exists."""
    m = relay_kit.machine_info()
    assert R.first_state(None) == "online"
    prev = R.new_node(job="j", node="laptop", machine=m, state="online")
    assert R.first_state(prev) == "rejoining"
    assert R.new_node(job="j", node="laptop", machine=m, state=R.first_state(None))["state"] == "online"
    assert R.new_node(job="j", node="laptop", machine=m, state="rejoining")["state"] == "rejoining"
    with pytest.raises(TypeError):
        R.new_node(job="j", node="laptop", machine=m)            # no hidden default
    with pytest.raises(RecordError):
        R.new_node(job="j", node="laptop", machine=m, state="leaving")
