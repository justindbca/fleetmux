"""Profile authorization and idle transitions need no daemon, Redis, or CLI."""
import copy
import json

import pytest

from cmux_fleet.agent_registry import AgentRegistry
from cmux_fleet.assignment import assign_profiles
from cmux_fleet.planner import build_dispatch_plan
from cmux_fleet.profile_runtime import (
    ProfileRuntimeError, desired_profile, parse_recommended_profile,
    prepare_profile, profile_advisory,
)


@pytest.fixture
def task():
    return {"id": "test-task", "profile_assignments": json.dumps(assign_profiles(
        build_dispatch_plan("implement dynamic router"), ["worker-1", "worker-2"]))}


def test_only_current_slot_assignments_authorize_recommendations(task):
    assert desired_profile("worker-1", "Recommended profile: flashnext-coder. Fix tests", task) == "flashnext-coder"
    with pytest.raises(ProfileRuntimeError, match="not assigned to worker-1"):
        desired_profile("worker-1", "Recommended profile: claude-reviewer", task)
    with pytest.raises(ProfileRuntimeError, match="not assigned"):
        desired_profile("worker-1", "Recommended profile: flashnext-coder", {})


def test_subtask_must_match_slot_and_profile():
    task = {"profile_assignments": assign_profiles(build_dispatch_plan("implement router"), ["worker-1"])}
    assert desired_profile("worker-1", "Subtask: review", task) == "claude-reviewer"
    with pytest.raises(ProfileRuntimeError, match="not assigned.*subtask implementation"):
        desired_profile("worker-1", "Subtask: implementation\nRecommended profile: claude-reviewer", task)
    with pytest.raises(ProfileRuntimeError, match="not assigned"):
        desired_profile("worker-2", "Subtask: review", task)
    with pytest.raises(ProfileRuntimeError, match="ambiguous"):
        desired_profile("worker-1", "The prose mentions flashnext-coder", task)


@pytest.mark.parametrize("marker", [
    "Recommended profile:", "Recommended profile:\nflashnext-coder",
    "Recommended profile: flashnext-coder/unsafe", "Recommended profile: flashnext-coder_extra",
    "Recommended profile: " + "a" * 65,
    "Recommended profile: flashnext-coder\nRecommended profile: claude-reviewer",
])
def test_bad_markers_cannot_be_partially_matched(marker):
    with pytest.raises(ProfileRuntimeError):
        parse_recommended_profile(marker)


@pytest.mark.parametrize("marker", [
    "Recommended profile: flashnext-coder。", "Recommended profile: flashnext-coder。请修测试",
    "请按 Recommended profile: flashnext-coder，修测试", "Recommended profile: flashnext-coder；",
    "（Recommended profile: flashnext-coder）", "Recommended profile: flashnext-coder（备选 claude-coder）",
    "Recommended profile: flashnext-coder【必须】",
])
def test_cjk_punctuation_ends_the_marker(marker, task):
    # Incident: a Chinese 。 after the id kept the token invalid and blocked the task after 3 tries.
    assert parse_recommended_profile(marker) == "flashnext-coder"
    assert desired_profile("worker-1", marker, task) == "flashnext-coder"


def test_cjk_punctuation_does_not_hide_a_malformed_id():
    for marker in ("Recommended profile: flashnext-coder/unsafe。", "Recommended profile: Flashnext-coder。",
                   "Recommended profile: flashnext-coder。\nRecommended profile: claude-reviewer"):
        with pytest.raises(ProfileRuntimeError):
            parse_recommended_profile(marker)


@pytest.mark.parametrize("raw", ["not-json", "null", "[]", '{"assignments":null}',
                                  '{"assignments":[null]}', '{"assignments":[{}]}'])
def test_malformed_assignments_fail_closed(raw):
    with pytest.raises(ProfileRuntimeError, match="invalid task profile_assignments"):
        desired_profile("worker-1", "Fix tests", {"profile_assignments": raw})


@pytest.mark.parametrize("state", ["idle", "starting"])
def test_initial_metadata_application_is_pure_and_preserves_identity(task, state):
    worker = {"state": state, "delivered": "0", "label": "worker-1.3", "generation": "3", "pane": "pane-7"}
    before = copy.deepcopy((worker, task))
    application = prepare_profile("worker-1", "Fix pytest", task, worker, AgentRegistry())
    assert (worker, task) == before
    updated = dict(worker, **application.metadata())
    assert updated["advisory_profile"] == updated["desired_profile"] == "flashnext-coder"
    assert updated["profile_state"] == "applied-metadata"
    assert all(updated[key] == worker[key] for key in worker)
    assert json.loads(updated["profile_capabilities"]) == ["code-edit", "test-fix", "planning"]
    prefix = profile_advisory("worker-1", application)
    assert "Capabilities: code-edit, test-fix, planning" in prefix
    assert "stable slot worker-1" in prefix
    assert "No CLI/model switch" in prefix


@pytest.mark.parametrize("state,delivered", [("busy", "1"), ("handoff", "2"), ("dead", "0"),
                                            ("starting", "1"), ("", "0")])
def test_unsafe_boundaries_record_intent_without_application(task, state, delivered):
    application = prepare_profile("worker-1", "Fix pytest", task,
                                  {"state": state, "delivered": delivered}, AgentRegistry())
    assert application.state == "pending"
    assert application.desired_profile == "flashnext-coder"
    assert "advisory_profile" not in application.metadata()


def test_different_advisory_profile_stays_pending_even_at_idle(task):
    worker = {"state": "idle", "advisory_profile": "researcher"}
    application = prepare_profile("worker-1", "Fix pytest", task, worker, AgentRegistry())
    assert application.state == "pending"
    assert dict(worker, **application.metadata())["advisory_profile"] == "researcher"
    assert "disabled" in application.reason


def test_repeated_same_profile_is_idempotent(task):
    worker = {"state": "idle"}
    first = prepare_profile("worker-1", "Fix pytest", task, worker, AgentRegistry())
    second = prepare_profile("worker-1", "Fix pytest", task, dict(worker, **first.metadata()), AgentRegistry())
    assert second == first


def test_unknown_assigned_card_does_not_fall_back_to_assignment_capability(task):
    data = json.loads(task["profile_assignments"])
    data["assignments"][0]["agent_id"] = "unknown-card"
    task["profile_assignments"] = data
    with pytest.raises(ProfileRuntimeError, match="unknown assigned profile"):
        prepare_profile("worker-1", "Fix pytest", task, {"state": "idle"}, AgentRegistry())


@pytest.mark.parametrize("state", ["idle", "starting", "busy", "handoff"])
@pytest.mark.parametrize("task_opt,config_opt", [(False, False), (False, True), (True, False), (True, True)])
def test_live_switch_requires_both_opt_ins(task, state, task_opt, config_opt):
    task["apply_profiles_live"] = json.dumps(task_opt)
    if task_opt and not config_opt:
        with pytest.raises(ProfileRuntimeError, match="requires allow_profile_live_switch"):
            prepare_profile("worker-1", "Fix pytest", task, {"state": state}, AgentRegistry())
        return
    result = prepare_profile("worker-1", "Fix pytest", task, {"state": state}, AgentRegistry(),
                             allow_profile_live_switch=config_opt)
    if task_opt:
        assert result.state == ("switching" if state == "idle" else "pending")
    else:
        assert result.state == ("applied-metadata" if state in ("idle", "starting") else "pending")
    assert "current_profile" not in result.metadata()
    assert "runtime_profile" not in result.metadata()


def test_legacy_instruction_has_no_profile_or_advisory():
    result = prepare_profile("worker-1", "Subtask: write a report", {}, {"state": "idle"}, None)
    assert result.state == "none"
    assert "advisory_profile" not in result.metadata()
    assert profile_advisory("worker-1", result) == ""
