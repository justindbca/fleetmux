"""Three machines (three temp dirs) on one hub (a local bare repo): the state-layer part of the
end-to-end story, without dev sub-fleets (those are started by the flow layer)."""
import threading

import pytest

import relay_kit
from relay_kit import Rig, checkout_commit, git, isolated_git, remote_refs, tree_digest, write_files  # noqa: F401
from fleetmux.relay import assign
from fleetmux.relay import records as R
from fleetmux.relay.checkpoint import take_checkpoint
from fleetmux.relay.liveness import LEAVING, LOST, ONLINE
from fleetmux.relay.materialize import materialize
from fleetmux.relay.snapshot import salvage_snapshot
from fleetmux.relay.takeover import write_package

TOOLS = {"python": "3.14.0", "rust": "1.83.0"}
MACHINES = {"laptop": relay_kit.machine_info(10, 32, 59.0, always_on=False, toolchains=TOOLS),
            "desktop": relay_kit.machine_info(16, 128, 132.0, toolchains=TOOLS),
            "server": relay_kit.machine_info(10, 16, 48.0, toolchains={"python": "3.14.0"})}


@pytest.fixture
def world(tmp_path):
    hub, base, ms = relay_kit.machines(tmp_path, files={"README.md": "demo\n", "src/core.rs": "fn main() {}\n",
                                                        ".gitignore": "*.ignored\n"})
    rs = {n: Rig(m, MACHINES[n], candidate_filter=relay_kit.toolchain_filter) for n, m in ms.items()}
    for r in rs.values():
        r.beat()
    for r in rs.values():
        r.look()
    return hub, base, rs


def init(r, base, *defs):
    for d in defs:
        args = dict(job="j", title=d["id"], dir=f".relay/j/{d['id']}", base=None if d.get("depends_on") else base)
        args.update(d)
        assert r.m.hub.create_task(R.new_task(**args)).won


def claim_pass(r):
    """What every machine does each poll: claim the claimable tasks it ranks first for."""
    tasks = r.look()
    cands = assign.candidates(r.nodes, tasks, r.observer.classes(), r.m.name, r.m.settings)
    won = []
    for tid, target in assign.plan({t: v for t, v in tasks.items() if R.claimable(v.record)}, cands,
                                   r.m.settings).items():
        if target == r.m.name:
            res = r.reclaimer.claim(tid, tasks[tid])
            if res.outcome == "won":
                won.append(tid)
    return won


def everyone_looks(rs):
    for r in rs.values():
        r.look()


def owner_tree(r, tid):
    view = r.look()[tid]
    return view, materialize(r.m.git, r.m.cache, r.m.hub, job="j", rec=view.record,
                             worktree=r.m.worktrees / "j" / tid, marker_dir=r.m.state_dir / "markers")


def silence(rs, seconds, silent=("laptop",)):
    for r in rs.values():
        r.m.clock.advance(seconds)
        if r.m.name not in silent:
            r.beat()


STORY = ({"id": "M0", "weight": "light", "prefer": "server"},
         {"id": "M3", "weight": "heavy", "needs": ["rust"], "prefer": "laptop"},
         {"id": "M1", "weight": "heavy", "needs": ["rust"], "prefer": "desktop", "depends_on": ["M3"]},
         {"id": "M4", "weight": "heavy"})
NO_M4 = STORY[:3]                     # with M4 the desktop would sit at its heavy cap and could take no heavy task


def test_three_machines_work_their_own_tasks_at_once(world):
    """R1, R2, R14: M0 and M3 go to their preferred machines, the unpinned heavy M4 to the strongest one, M1
    waits for M3."""
    hub, base, rs = world
    init(rs["laptop"], base, *STORY)
    for r in rs.values():
        claim_pass(r)
    tasks = rs["server"].look()
    owners = {t: (v.record["state"], v.record["owner"]) for t, v in tasks.items()}
    assert owners == {"M0": ("working", "server"), "M3": ("working", "laptop"), "M4": ("working", "desktop"),
                      "M1": ("blocked", None)}


def test_silent_laptop_is_lost_and_its_task_moves_byte_identical(world, tmp_path):
    """R5, R7, R8, R9: no goodbye; after lost_after_s on the desktop's own clock it takes M3 over, epoch 2, from the
    last snapshot, byte for byte; the takeover note names the start; server's M0 is untouched."""
    hub, base, rs = world
    init(rs["laptop"], base, *NO_M4)
    for r in rs.values():
        claim_pass(r)
    lap = rs["laptop"]
    view, placed = owner_tree(lap, "M3")
    write_files(placed.worktree, {"src/core.rs": "fn main() { relay(); }\n", "src/new.rs": "// not committed\n",
                                  ".relay/j/M3/notes/dev-1/PROGRESS.md":
                                  "## Done\n- a\n## Doing\n- b\n## Next\n- c\n## Dead ends\n- none\n## Red tests\n- none\n"})
    cp = take_checkpoint(lap.m.hub, task="M3", view=view, me="laptop", worktree=placed.worktree, tmp_dir=lap.m.tmp,
                         last=None, settings=lap.m.settings, kind="phase", phase="design")
    assert cp.outcome == "ok"
    everyone_looks(rs)
    m0_before = rs["server"].look()["M0"]
    silence(rs, 299)
    assert rs["desktop"].step() == [] and rs["desktop"].observer.classes()["laptop"] == ONLINE
    silence(rs, 2)
    rs["server"].step()                                                 # server lacks rust: never takes M3
    won = rs["desktop"].step()
    assert [(x.task, x.outcome) for x in won] == [("M3", "won")]
    assert rs["desktop"].observer.classes()["laptop"] == LOST
    view2, placed2 = owner_tree(rs["desktop"], "M3")
    rec = view2.record
    assert rec["owner"] == "desktop" and rec["epoch"] == 2 and rec["owners"][-1]["why"] == "takeover:lost"
    assert tree_digest(placed2.worktree) == tree_digest(checkout_commit(hub, cp.snap.commit, tmp_path / "exp"))
    md = write_package(placed2.worktree, rec, previous=rs["desktop"].nodes["laptop"].record,
                       judged=rs["desktop"].observer.judged("laptop"), by="desktop", settings=rs["desktop"].m.settings)
    assert "laptop" in md.read_text() and "phase design" in md.read_text()
    assert rs["server"].look()["M0"] == m0_before


def test_goodbye_hands_over_and_is_taken_without_waiting(world):
    """R6: leave = snapshot, hand_over, leaving record; others see a goodbye (not lost) and take it at once."""
    hub, base, rs = world
    init(rs["laptop"], base, *NO_M4)
    for r in rs.values():
        claim_pass(r)
    lap = rs["laptop"]
    view, placed = owner_tree(lap, "M3")
    write_files(placed.worktree, {"late.txt": "x\n"})
    cp = take_checkpoint(lap.m.hub, task="M3", view=view, me="laptop", worktree=placed.worktree, tmp_dir=lap.m.tmp,
                         last=None, settings=lap.m.settings)
    after = lap.look()["M3"]
    assert lap.m.hub.cas_task("M3", after.sha, R.hand_over(after.record)).won
    lap.beat(state="leaving", leaving_reason="sleep")
    d = rs["desktop"]
    won = d.step()
    assert d.observer.classes()["laptop"] == LEAVING and d.observer.judged("laptop") == "goodbye: sleep"
    assert [(x.task, x.outcome) for x in won] == [("M3", "won")]
    rec = d.look()["M3"].record
    assert rec["owners"][-1]["why"] == "takeover:goodbye" and R.start_point(rec)["commit"] == cp.snap.commit


def test_two_machines_race_for_one_task_exactly_one_owner(world, tmp_path):
    """R8: desktop and server both decide to take the same task at the same moment; the hub lets one through."""
    hub, base, rs = world
    init(rs["laptop"], base, {"id": "M0", "weight": "light"})
    assert rs["laptop"].reclaimer.claim("M0", rs["laptop"].look()["M0"]).outcome == "won"
    everyone_looks(rs)
    silence(rs, 301)
    for n in ("desktop", "server"):
        rs[n].m.settings.store.set("claim_stagger_s", 0)              # both act at once
        rs[n].look()
    relay_kit.install_barrier(hub, 2, tmp_path / "gate")
    results = {}

    def go(n):
        results[n] = rs[n].reclaimer.step(rs[n].tasks, rs[n].nodes)
    threads = [threading.Thread(target=go, args=(n,)) for n in ("desktop", "server")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    outcomes = sorted(x.outcome for n in results for x in results[n])
    assert outcomes == ["lost_race", "won"]
    winner = next(n for n in results if any(x.outcome == "won" for x in results[n]))
    assert rs["laptop"].look()["M0"].record["owner"] == winner
    assert all(rs[n].state.cas_pending() == {} for n in ("desktop", "server"))


def test_an_observer_that_cannot_read_the_hub_takes_nothing(world, tmp_path):
    """R7: while the desktop itself is offline nobody is judged lost; timers restart when it reads again."""
    hub, base, rs = world
    init(rs["laptop"], base, {"id": "M0", "weight": "light"})
    assert rs["laptop"].reclaimer.claim("M0", rs["laptop"].look()["M0"]).outcome == "won"
    everyone_looks(rs)
    d = rs["desktop"]
    real = d.m.hub.state_remote
    d.m.hub.state_remote = str(tmp_path / "unreachable.git")
    d.m.clock.advance(1000)
    assert d.step() == [] and not d.observer.takeable("laptop")
    d.m.hub.state_remote = real
    assert d.step() == [] and d.observer.classes()["laptop"] == ONLINE
    d.m.clock.advance(301)
    assert [x.outcome for x in d.step()] == ["won"]


def test_returning_owner_pushes_salvage_and_never_writes_the_task_again(world):
    """R11, R12: the laptop comes back; its checkpoint is refused (the task moved); everything it had left goes to
    salvage/<task>/laptop-e1, the task ref is untouched by it."""
    hub, base, rs = world
    init(rs["laptop"], base, {"id": "M3", "weight": "heavy", "needs": ["rust"]})
    lap = rs["laptop"]
    assert lap.reclaimer.claim("M3", lap.look()["M3"]).outcome == "won"
    view, placed = owner_tree(lap, "M3")
    everyone_looks(rs)
    silence(rs, 301)
    assert [x.outcome for x in rs["desktop"].step()] == ["won"]
    write_files(placed.worktree, {"offline-work.txt": "done while asleep\n"})
    before = remote_refs(hub)["refs/relay/j/tasks/M3"]
    lap.m.clock.advance(301)
    cp = take_checkpoint(lap.m.hub, task="M3", view=view, me="laptop", worktree=placed.worktree, tmp_dir=lap.m.tmp,
                         last=None, settings=lap.m.settings)
    assert cp.outcome == "lost"
    now = lap.look()["M3"].record
    assert now["owner"] == "desktop" and now["epoch"] == 2                 # read first: it was taken over
    s = salvage_snapshot(lap.m.git, lap.m.cache, job="j", task="M3", epoch=1, worktree=placed.worktree,
                         tmp_dir=lap.m.tmp, secret_globs=lap.m.settings.secret_globs)
    assert lap.m.hub.push_salvage("M3", "laptop", 1, s.commit).accepted
    refs = remote_refs(hub)
    assert refs["refs/relay/j/tasks/M3"] == before
    assert git("show", f"{refs['refs/relay/j/salvage/M3/laptop-e1']}:offline-work.txt", cwd=hub) == "done while asleep"
    assert (placed.worktree / "offline-work.txt").exists()                # the local tree is kept


def test_two_machines_lost_the_last_one_takes_what_fits(world):
    """R8: the survivor takes up to its caps; the rest waits as reclaimed, no unplaceable notice
    (only the caps are full)."""
    hub, base, rs = world
    init(rs["laptop"], base, {"id": "A1", "weight": "heavy"}, {"id": "A2", "weight": "light"},
         {"id": "B1", "weight": "heavy"}, {"id": "B2", "weight": "light"})
    for tid, owner in (("A1", "laptop"), ("A2", "laptop"), ("B1", "desktop"), ("B2", "desktop")):
        assert rs[owner].reclaimer.claim(tid, rs[owner].look()[tid]).outcome == "won"
    everyone_looks(rs)
    srv = rs["server"]
    silence(rs, 301, silent=("laptop", "desktop"))
    won = srv.step()
    srv.m.clock.advance(srv.m.settings.hot("claim_stagger_s"))
    won += srv.step()
    tasks = srv.look()
    mine = sorted(t for t, v in tasks.items() if v.record["owner"] == "server")
    waiting = sorted(t for t, v in tasks.items() if v.record["state"] == "reclaimed")
    assert len(mine) == srv.m.settings.hot("max_tasks_per_node") and len(waiting) == 2
    assert sum(1 for t in mine if tasks[t].record["weight"] == "heavy") <= srv.m.settings.hot("max_heavy_per_node")
    assert not [t for t in srv.notices if "has no machine" in t]


def test_heavy_cap_full_everywhere_task_waits_reclaimed_without_notice(world):
    """R15 with caps: the only rust machine left already holds a heavy task: M3 waits as reclaimed."""
    hub, base, rs = world
    init(rs["laptop"], base, *STORY)
    for r in rs.values():
        claim_pass(r)
    everyone_looks(rs)
    silence(rs, 301)
    assert rs["desktop"].step() == []
    rec = rs["server"].look()["M3"].record
    assert rec["state"] == "reclaimed" and rec["owner"] is None
    assert not [t for r in rs.values() for t in r.notices if "has no machine" in t]
