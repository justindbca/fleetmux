import pytest

from cmux_fleet.assignment import assign_profiles
from cmux_fleet.planner import build_dispatch_plan


def test_one_worker_reuses_stable_slot_for_dependent_profiles():
    assignments = assign_profiles(
        build_dispatch_plan("implement feature X and add tests"), ["worker-1"]
    )["assignments"]

    assert [(item["subtask_id"], item["worker"], item["agent_id"]) for item in assignments] == [
        ("implementation", "worker-1", "flashnext-coder"),
        ("review", "worker-1", "claude-reviewer"),
    ]
    assert assignments[1]["depends_on"] == ["implementation"]
    assert all(item["worker"] not in {"flashnext-coder", "claude-reviewer"} for item in assignments)


def test_two_workers_assign_different_profiles_deterministically():
    plan = build_dispatch_plan("implement feature X and add tests")
    first = assign_profiles(plan, ["worker-1", "worker-2"])
    second = assign_profiles(plan, ["worker-1", "worker-2"])

    assert first == second
    assert [item["worker"] for item in first["assignments"]] == ["worker-1", "worker-2"]


def test_profile_ids_cannot_be_passed_as_worker_slots():
    with pytest.raises(ValueError, match="must not use agent profile ids"):
        assign_profiles(build_dispatch_plan("implement feature X"), ["flashnext-coder"])
