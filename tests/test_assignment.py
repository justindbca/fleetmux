import pytest

from fleetmux.assignment import assign_profiles
from fleetmux.planner import DispatchPlan, DispatchSubtask, build_dispatch_plan


def two_step_plan(goal="feature X", registry=None):
    """An implementation and a dependent review the user asked for: the planner makes each one on its own (it no
    longer appends a review), joined here to exercise the assignment of dependent profiles."""
    impl, = build_dispatch_plan("implement " + goal + " and add tests", registry).subtasks
    review, = build_dispatch_plan("review " + goal, registry).subtasks
    return DispatchPlan(goal, "code-change", (impl, DispatchSubtask(
        review.id, review.title, review.agent_id, review.capability, review.prompt, (impl.id,))))


def test_one_worker_reuses_stable_slot_for_dependent_profiles():
    assignments = assign_profiles(two_step_plan(), ["worker-1"])["assignments"]

    assert [(item["subtask_id"], item["worker"], item["agent_id"]) for item in assignments] == [
        ("implementation", "worker-1", "flashnext-coder"),
        ("review", "worker-1", "claude-opus"),
    ]
    assert assignments[1]["depends_on"] == ["implementation"]
    assert all(item["worker"] not in {"flashnext-coder", "claude-opus"} for item in assignments)


def test_two_workers_assign_different_profiles_deterministically():
    plan = two_step_plan()
    first = assign_profiles(plan, ["worker-1", "worker-2"])
    second = assign_profiles(plan, ["worker-1", "worker-2"])

    assert first == second
    assert [item["worker"] for item in first["assignments"]] == ["worker-1", "worker-2"]


def test_profile_ids_cannot_be_passed_as_worker_slots():
    with pytest.raises(ValueError, match="must not use agent profile ids"):
        assign_profiles(build_dispatch_plan("implement feature X"), ["flashnext-coder"])
