import json

import pytest

from cmux_fleet import fleet
from cmux_fleet.agent_registry import AgentRegistry
from cmux_fleet.planner import build_dispatch_plan


def test_code_change_plan_has_implementation_and_independent_review():
    goal = "implement feature X and add tests"
    plan = build_dispatch_plan(goal)
    assert plan.kind == "code-change"
    assert [(task.id, task.agent_id) for task in plan.subtasks] == [
        ("implementation", "flashnext-coder"),
        ("review", "claude-reviewer"),
    ]
    assert plan.subtasks[1].depends_on == ("implementation",)
    assert all(goal in task.prompt for task in plan.subtasks)


def test_research_plan_routes_to_researcher_with_self_contained_prompt():
    goal = "research web sources about deterministic dispatch"
    plan = build_dispatch_plan(goal)
    assert plan.kind == "research"
    assert plan.subtasks[0].agent_id == "researcher"
    assert goal in plan.subtasks[0].prompt


def test_review_goal_is_not_misclassified_as_implementation():
    plan = build_dispatch_plan("review code changes for safety")
    assert plan.kind == "review"
    assert [(task.id, task.agent_id) for task in plan.subtasks] == [("review", "claude-reviewer")]


@pytest.mark.parametrize("goal,capability", [
    ("fix security bug", "test-fix"),
    ("implement feature and review it", "code-edit"),
    ("修复安全漏洞并复核", "test-fix"),
    ("实现功能并复核", "code-edit"),
    ("implement web search feature and review security", "code-edit"),
    ("实现网页搜索功能并审查安全风险", "code-edit"),
])
def test_code_change_subtasks_route_by_role_despite_goal_keywords(goal, capability):
    plan = build_dispatch_plan(goal)
    assert plan.kind == "code-change"
    assert [(task.id, task.agent_id, task.capability) for task in plan.subtasks] == [
        ("implementation", "flashnext-coder", capability),
        ("review", "claude-reviewer", "code-review"),
    ]
    assert plan.subtasks[0].depends_on == ()
    assert plan.subtasks[1].depends_on == ("implementation",)
    assert plan.goal == goal
    assert all("Goal:\n%s\n\n" % goal in task.prompt for task in plan.subtasks)


@pytest.mark.parametrize("goal", [
    "research security risks",
    "research implementation and review options",
    "研究代码安全风险",
    "搜索修复安全漏洞的资料",
])
def test_research_role_is_not_overridden_by_review_keywords(goal):
    plan = build_dispatch_plan(goal)
    assert plan.kind == "research"
    assert [(task.id, task.agent_id, task.capability) for task in plan.subtasks] == [
        ("research", "researcher", "web-research"),
    ]
    assert "Goal:\n%s\n\n" % goal in plan.subtasks[0].prompt


@pytest.mark.parametrize("goal,capabilities,missing", [
    ("fix security bug", ["code-review", "safety-audit"], "test-fix"),
    ("fix security bug", ["code-edit", "code-review", "safety-audit"], "test-fix"),
    ("implement feature and review it", ["code-review", "safety-audit"], "code-edit"),
    ("implement security feature", ["code-edit", "safety-audit"], "code-review"),
    ("review code changes", ["code-edit"], "code-review"),
    ("research security risks", ["safety-audit", "planning"], "web-research"),
    ("summarize current workflow", ["web-research"], "planning"),
])
def test_plan_rejects_missing_subtask_capability(tmp_path, goal, capabilities, missing):
    (tmp_path / "only.json").write_text(json.dumps({
        "id": "only", "display_name": "Only", "cli": "codex",
        "capabilities": capabilities,
    }))
    with pytest.raises(ValueError, match="no registered agent supports task capabilities: " + missing):
        build_dispatch_plan(goal, AgentRegistry(tmp_path))


def test_planning_subtask_requires_planning_capability():
    plan = build_dispatch_plan("summarize current workflow")
    task, = plan.subtasks
    assert plan.kind == "planning"
    assert task.capability == "planning"
    assert task.capability in AgentRegistry().get(task.agent_id).capabilities


def test_inspection_cli_does_not_connect_to_fleet(monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("inspection command contacted live fleet infrastructure")

    monkeypatch.setattr(fleet, "connect", forbidden)
    monkeypatch.setattr(fleet, "start", forbidden)
    monkeypatch.setattr(fleet, "daemon", forbidden)

    fleet.main(["agents", "list"])
    listing = json.loads(capsys.readouterr().out)
    assert {card["id"] for card in listing["agents"]} == {
        "flashnext-coder", "claude-coder", "claude-reviewer", "researcher",
        "claude-fable", "claude-opus", "claude-sonnet", "claude-haiku",  # advisor model cards
    }

    fleet.main(["agents", "match", "fix failing pytest in router", "--json"])
    assert json.loads(capsys.readouterr().out)["agent_id"] == "flashnext-coder"

    fleet.main(["plan", "implement feature X"])
    plan = json.loads(capsys.readouterr().out)
    assert [task["agent_id"] for task in plan["subtasks"]] == [
        "flashnext-coder", "claude-reviewer"
    ]

    fleet.main(["plan", "implement feature X", "--workers", "worker-1,worker-2"])
    assigned = json.loads(capsys.readouterr().out)
    assert [item["worker"] for item in assigned["profile_assignments"]["assignments"]] == [
        "worker-1", "worker-2"
    ]
