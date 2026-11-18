"""relay.assign: capability scoring and order (R14, R15, R17) and the candidate_filter hook. Toolchain matching
(R16) is flow-1's placement and is not tested here."""
import pytest

import relay_kit
from fleetmux.relay import assign as A
from fleetmux.relay import records as R
from fleetmux.relay.clock import FakeClock
from fleetmux.relay.hub import NodeView, TaskView
from fleetmux.relay.liveness import LEAVING, LOST, ONLINE

S = relay_kit.settings()

LAPTOP = relay_kit.machine_info(10, 32, 59.0, always_on=False)
DESKTOP = relay_kit.machine_info(16, 128, 132.0)
SERVER = relay_kit.machine_info(10, 16, 48.0)


def only(*allowed):
    """A candidate filter (the hook flow-1's placement fills) that lets only `allowed` machines through."""
    return lambda c, task: None if c.id in allowed else "filtered"


def node(name, machine, state="online"):
    rec = R.new_node(job="j", node=name, machine=machine, state="online")
    return NodeView("s" + name, R.next_node(rec, state=state))


def nodes(**over):
    base = {"laptop": node("laptop", LAPTOP), "desktop": node("desktop", DESKTOP), "server": node("server", SERVER)}
    base.update(over)
    return base


def task(id, weight="light", prefer=None, owner=None, state=None):
    rec = R.new_task(job="j", id=id, title=id, weight=weight, dir=f".relay/j/{id}", prefer=prefer,
                     base="a" * 40, reserved_ok=True)
    if owner:
        rec = R.claim(rec, me=owner)
    if state:
        rec = dict(rec, state=state)
    return TaskView("t" + id, rec)


def cands(ns=None, tasks=None, classes=None, me="laptop", settings=S):
    ns = ns or nodes()
    classes = classes if classes is not None else {n: ONLINE for n in ns if n != me}
    return A.candidates(ns, tasks or {}, classes, me, settings)


def ids(cs):
    return [c.id for c in cs]


# ----------------------------------------------------------------------------- score and order


def test_heavy_task_goes_to_the_highest_score():
    ranked = A.rank(cands(), task("M4", "heavy").record, S)
    assert ids(ranked)[0] == "desktop"
    assert A.score_of(DESKTOP, S) > A.score_of(LAPTOP, S) > A.score_of(SERVER, S)


def test_light_task_goes_to_the_machine_with_fewest_tasks():
    busy = {"M0": task("M0", owner="desktop"), "M1": task("M1", owner="laptop")}
    assert ids(A.rank(cands(tasks=busy), task("M5").record, S))[0] == "server"


def test_prefer_first_when_eligible_skipped_when_not():
    assert ids(A.rank(cands(), task("M0", prefer="server").record, S))[0] == "server"
    ranked = A.rank(cands(), task("M3", "heavy", prefer="server").record, S, candidate_filter=only("laptop"))
    assert ids(ranked) == ["laptop"]                                      # the filter rules server out


def test_hot_score_weight_change_changes_the_order():
    from fleetmux.relay import params
    hot = params.DictHot(relay_kit.block()["hot"])
    s = params.load(relay_kit.block(), hot=hot)
    assert ids(A.rank(cands(settings=s), task("M4", "heavy").record, s))[0] == "desktop"
    hot.set("score", {"cores": 0.0, "mem_gb": 0.0, "disk_free_gb": -1.0})
    assert ids(A.rank(cands(settings=s), task("M4", "heavy").record, s))[0] == "server"


# ----------------------------------------------------------------------------- hard limits


def test_low_disk_no_heavy_task_and_very_low_disk_no_task():
    ns = nodes(desktop=node("desktop", dict(DESKTOP, disk_free_gb=30.0)),
               server=node("server", dict(SERVER, disk_free_gb=10.0)))
    cs = {c.id: c for c in cands(ns)}
    heavy, light = task("M4", "heavy").record, task("M5").record
    assert A.why_not(cs["desktop"], heavy, S) == "disk_heavy" and A.why_not(cs["desktop"], light, S) is None
    assert A.why_not(cs["server"], light, S) == "disk"
    assert ids(A.rank(list(cs.values()), heavy, S)) == ["laptop"]


def test_without_a_filter_assign_ranks_by_capability_only():
    cs = {c.id: c for c in cands()}
    t = dict(task("M3", "heavy").record, needs=["rust"])               # needs are not judged by assign
    assert A.why_not(cs["desktop"], t, S) is None
    assert ids(A.rank(list(cs.values()), t, S)) == ["desktop", "laptop", "server"]
    assert A.plan({"M3": TaskView("tM3", t)}, list(cs.values()), S) == {"M3": "desktop"}


def test_candidate_filter_reason_comes_first_and_excludes():
    cs = {c.id: c for c in cands()}
    t = task("M3", "heavy").record
    f = only("laptop")
    assert A.why_not(cs["desktop"], t, S, candidate_filter=f) == "filtered"
    assert ids(A.rank(list(cs.values()), t, S, candidate_filter=f)) == ["laptop"]
    assert A.plan({"M3": TaskView("tM3", t)}, list(cs.values()), S, candidate_filter=f) == {"M3": "laptop"}
    seen = []
    A.why_not(cs["server"], t, S, candidate_filter=lambda c, task: seen.append((c.id, task["id"])))
    assert seen == [("server", "M3")]


def test_task_caps_per_machine():
    busy = {"M0": task("M0", "heavy", owner="desktop"), "M1": task("M1", owner="server"),
            "M2": task("M2", owner="server")}
    cs = {c.id: c for c in cands(tasks=busy)}
    assert A.why_not(cs["desktop"], task("M4", "heavy").record, S) == "cap_heavy"
    assert A.why_not(cs["desktop"], task("M5").record, S) is None
    assert A.why_not(cs["server"], task("M5").record, S) == "cap"


def test_only_online_settled_machines_are_candidates():
    ns = nodes(server=node("server", SERVER, state="rejoining"), desktop=node("desktop", DESKTOP))
    assert ids(cands(ns, classes={"desktop": LOST, "server": ONLINE})) == ["laptop"]
    assert ids(cands(nodes(), classes={"desktop": LEAVING, "server": ONLINE})) == ["laptop", "server"]
    assert ids(cands(nodes(laptop=node("laptop", LAPTOP, state="rejoining")))) == ["desktop", "server"]


def test_coordinator_prefers_an_always_on_machine():
    ns = nodes(laptop=node("laptop", relay_kit.machine_info(64, 512, 900.0, always_on=False)))
    ranked = A.rank(cands(ns), task("coordinator").record, S)
    assert ranked[0].id != "laptop" and ranked[-1].id == "laptop"


def test_plan_counts_tasks_inside_the_plan_heavy_first():
    todo = {"M4": task("M4", "heavy"), "M5": task("M5", "heavy"), "M6": task("M6")}
    plan = A.plan(todo, cands(), S)
    assert plan["M4"] == "desktop" and plan["M5"] == "laptop" and plan["M6"] == "server"


def test_unplaceable_reasons_ignore_free_slots_only():
    busy = {f"M{i}": task(f"M{i}", owner=n) for i, n in enumerate(["laptop", "laptop", "desktop", "desktop",
                                                                   "server", "server"])}
    cs = cands(tasks=busy)
    assert A.unplaceable(task("M9").record, cs, S) is None                 # waits for a slot: not a notice
    assert A.unplaceable(task("M8").record, cs, S, candidate_filter=only()) == {
        "desktop": "filtered", "laptop": "filtered", "server": "filtered"}   # the filter's reasons are reported


def test_stagger_waits_rank_times_claim_stagger_s():
    clock = FakeClock(mono=0.0)
    st = A.Stagger(clock, S)
    assert st.ready("M0", 0)
    assert not st.ready("M1", 2)
    clock.advance(39)
    assert not st.ready("M1", 2)
    clock.advance(1)
    assert st.ready("M1", 2)
    st.forget("M1")
    assert not st.ready("M1", 2)

