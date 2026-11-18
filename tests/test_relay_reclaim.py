"""relay.reclaim + relay.state: taking over the tasks of a machine that left or went silent (R6, R8, R16), and
settling a push whose outcome was unknown by sha (no duplicate owner after a restart)."""
import pytest

import relay_kit
from relay_kit import Rig, isolated_git, remote_refs  # noqa: F401
from fleetmux.relay import records as R
from fleetmux.relay.errors import TransitionError

TOOLS = {"python": "3.14.0", "rust": "1.83.0"}


@pytest.fixture
def rigs(tmp_path):
    hub, base, ms = relay_kit.machines(tmp_path)
    machines = {"laptop": relay_kit.machine_info(10, 32, 59.0, always_on=False, toolchains=TOOLS),
                "desktop": relay_kit.machine_info(16, 128, 132.0, toolchains=TOOLS),
                "server": relay_kit.machine_info(10, 16, 48.0, toolchains={"python": "3.14.0"})}
    rs = {n: Rig(m, machines[n], candidate_filter=relay_kit.toolchain_filter) for n, m in ms.items()}
    for r in rs.values():
        r.beat()
    for r in rs.values():
        r.look()
    return hub, base, rs


def owned_by_laptop(rs, base, id="M3", weight="heavy", needs=("rust",)):
    a = rs["laptop"]
    assert a.m.hub.create_task(R.new_task(job="j", id=id, title=id, weight=weight, dir=f".relay/j/{id}",
                                          needs=list(needs), base=base)).won
    view = a.look()[id]
    res = a.reclaimer.claim(id, view)
    assert res.outcome == "won"
    for r in rs.values():
        r.look()                         # everyone sees the claim now: it is laptop's last sign of life
    return res


def silence(rs, seconds, *, silent=("laptop",)):
    for r in rs.values():
        r.m.clock.advance(seconds)
        if r.m.name not in silent:
            r.beat()


def test_lost_owner_task_taken_by_the_best_machine_epoch_up(rigs):
    hub, base, rs = rigs
    owned_by_laptop(rs, base)
    silence(rs, 301)
    for r in rs.values():
        r.look()
    out = rs["desktop"].step()
    assert [(x.task, x.outcome) for x in out] == [("M3", "won")]
    rec = rs["server"].look()["M3"].record
    assert rec["owner"] == "desktop" and rec["epoch"] == 2 and rec["owners"][-1]["why"] == "takeover:lost"
    assert rs["server"].step() == []                                   # nothing left to take


def test_second_ranked_machine_waits_its_stagger_then_takes(rigs):
    hub, base, rs = rigs
    owned_by_laptop(rs, base, id="M0", weight="light", needs=())
    silence(rs, 301)
    for r in rs.values():
        r.look()
    first, ranked_second = "desktop", "server"                          # light task, both idle: higher score first
    assert rs[ranked_second].step() == []                              # not its turn yet
    rs[ranked_second].m.clock.advance(rs[ranked_second].m.settings.hot("claim_stagger_s"))
    out = rs[ranked_second].step()                                     # the first never acted: it takes it
    assert [x.outcome for x in out] == ["won"]
    assert rs[first].look()["M0"].record["owner"] == ranked_second


def test_handed_over_task_is_taken_at_once_as_goodbye(rigs):
    hub, base, rs = rigs
    owned_by_laptop(rs, base)
    a = rs["laptop"]
    view = a.look()["M3"]
    assert a.m.hub.cas_task("M3", view.sha, R.hand_over(view.record)).won
    a.beat(state="leaving", leaving_reason="operator")
    out = rs["desktop"].step()
    assert [x.outcome for x in out] == ["won"]
    rec = rs["desktop"].look()["M3"].record
    assert rec["owner"] == "desktop" and rec["owners"][-1]["why"] == "takeover:goodbye"


def test_no_machine_with_the_toolchain_reclaimed_and_notified_once(rigs):
    hub, base, rs = rigs
    a = rs["laptop"]
    a.machine["toolchains"]["go"] = "1.23.0"       # only laptop has go (Reclaimer.claim checks no eligibility)
    a.beat()
    for r in rs.values():
        r.look()
    owned_by_laptop(rs, base, id="M7", weight="light", needs=("go",))
    silence(rs, 301)
    for r in rs.values():
        r.look()
    rs["desktop"].step()
    rec = rs["server"].look()["M7"].record
    assert rec["state"] == "reclaimed" and rec["owner"] is None and rec["epoch"] == 2
    rs["desktop"].step()
    notes = [t for t in rs["desktop"].notices if "M7" in t]
    assert len(notes) == 1 and "toolchain:go" in notes[0]


def test_blocked_task_is_never_claimed(rigs):
    hub, base, rs = rigs
    a = rs["laptop"]
    assert a.m.hub.create_task(R.new_task(job="j", id="M1", title="x", weight="light", dir=".relay/j/M1",
                                          depends_on=["M3"])).won
    with pytest.raises(TransitionError):
        a.reclaimer.claim("M1", a.look()["M1"])
    assert remote_refs(hub)["refs/relay/j/tasks/M1"] == a.look()["M1"].sha


def test_claim_lost_race_is_given_up(rigs):
    hub, base, rs = rigs
    a, d = rs["laptop"], rs["desktop"]
    assert a.m.hub.create_task(R.new_task(job="j", id="M0", title="x", weight="light", dir=".relay/j/M0",
                                          base=base)).won
    va, vd = a.look()["M0"], d.look()["M0"]
    assert a.reclaimer.claim("M0", va).outcome == "won"
    res = d.reclaimer.claim("M0", vd)
    assert res.outcome == "lost_race" and d.state.cas_pending() == {}


def test_unknown_outcome_is_kept_and_settled_by_sha_after_restart(rigs):
    hub, base, rs = rigs
    a = rs["laptop"]
    assert a.m.hub.create_task(R.new_task(job="j", id="M0", title="x", weight="light", dir=".relay/j/M0",
                                          base=base)).won
    view = a.look()["M0"]
    hook = hub / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nsleep 2\n")
    hook.chmod(0o755)
    a.m.settings.store.set("git_timeout_s", 1)      # the push is killed; it may still land
    res = a.reclaimer.claim("M0", view)
    hook.unlink()
    a.m.settings.store.set("git_timeout_s", 20)
    assert res.outcome == "unknown" and "M0" in a.state.cas_pending()
    restarted = Rig(a.m, a.machine)                  # a restart reads the saved cas_pending
    restarted.look()
    landed = restarted.tasks["M0"].sha == res.record_sha
    settled = restarted.reclaimer.settle(restarted.tasks)
    if landed:
        assert [(x.task, x.outcome) for x in settled] == [("M0", "won")]
    else:
        assert settled == [] and "M0" in restarted.state.cas_pending()
        retried = restarted.reclaimer.step(restarted.tasks, restarted.nodes)
        assert [(x.task, x.outcome) for x in retried] == [("M0", "won")]
        assert restarted.look()["M0"].sha == res.record_sha          # the saved record, never a new one
    assert restarted.state.cas_pending() == {}


def test_settle_drops_a_pending_record_someone_else_overwrote(rigs):
    hub, base, rs = rigs
    a, d = rs["laptop"], rs["desktop"]
    assert a.m.hub.create_task(R.new_task(job="j", id="M0", title="x", weight="light", dir=".relay/j/M0",
                                          base=base)).won
    view = a.look()["M0"]
    a.state.save_cas_pending("M0", "f" * 40, view.sha)               # our push never landed
    assert d.reclaimer.claim("M0", d.look()["M0"]).outcome == "won"
    assert a.reclaimer.settle(a.look()) == [] and a.state.cas_pending() == {}


def test_reclaimer_without_filter_ignores_needs(tmp_path):
    """Toolchain matching is the placement filter's job: with no candidate_filter the takeover round ranks by
    capability only."""
    hub, base, ms = relay_kit.machines(tmp_path)
    machines = {"laptop": relay_kit.machine_info(10, 32, 59.0, always_on=False, toolchains={"go": "1.23.0"}),
                "desktop": relay_kit.machine_info(16, 128, 132.0),
                "server": relay_kit.machine_info(10, 16, 48.0)}
    rs = {n: Rig(m, machines[n]) for n, m in ms.items()}
    for r in rs.values():
        r.beat()
    for r in rs.values():
        r.look()
    owned_by_laptop(rs, base, id="M7", weight="light", needs=("go",))
    silence(rs, 301)
    for r in rs.values():
        r.look()
    assert [x.outcome for x in rs["desktop"].step()] == ["won"]
    assert rs["server"].look()["M7"].record["owner"] == "desktop"
