import json

import pytest

from cmux_fleet.agent_registry import AgentRegistry
from cmux_fleet.router import classify_task, route_task


def test_code_task_routes_to_flashnext_coder():
    decision = route_task("fix failing pytest in router")
    assert decision.agent_id == "flashnext-coder"
    assert decision.capability == "test-fix"
    assert set(classify_task("fix failing pytest in router")) >= {"code-edit", "test-fix"}


def test_review_task_routes_to_claude_reviewer():
    decision = route_task("审查这次改动并做安全复核")
    assert decision.agent_id == "claude-reviewer"
    assert decision.capability == "code-review"


def test_web_research_task_routes_to_researcher():
    decision = route_task("search the web and research primary sources")
    assert decision.agent_id == "researcher"
    assert decision.capability == "web-research"


def test_routing_is_stable():
    decisions = [route_task("refactor code and fix tests").to_dict() for _ in range(5)]
    assert decisions == [decisions[0]] * 5


def test_primary_capability_and_rank_tie_breakers(tmp_path):
    cards = [
        {"id": "fast-risky", "display_name": "Fast Risky", "cli": "codex",
         "capabilities": ["code-edit", "code-review", "safety-audit"],
         "latency_rank": 1, "cost_rank": 1, "risk_rank": 9},
        {"id": "safe-slow", "display_name": "Safe Slow", "cli": "claude",
         "capabilities": ["code-edit", "code-review", "safety-audit"],
         "latency_rank": 9, "cost_rank": 9, "risk_rank": 1},
        {"id": "web-only", "display_name": "Web Only", "cli": "codex",
         "capabilities": ["web-research"], "latency_rank": 9},
    ]
    for card in cards:
        (tmp_path / (card["id"] + ".json")).write_text(json.dumps(card))
    registry = AgentRegistry(tmp_path)
    assert route_task("review this code", registry).agent_id == "safe-slow"
    assert route_task("edit this code", registry).agent_id == "fast-risky"
    assert route_task("research this code online", registry).agent_id == "web-only"
    assert route_task("review this code", registry, required_capability="code-edit").agent_id == "fast-risky"
    assert route_task("edit this code", registry, required_capability="code-review").agent_id == "safe-slow"
    assert route_task("review security and code", registry, required_capability="web-research").agent_id == "web-only"


@pytest.mark.parametrize("text,capability,agent_id", [
    ("fix security bug", "test-fix", "flashnext-coder"),
    ("implement feature and review it", "code-edit", "flashnext-coder"),
    ("修复安全漏洞并复核", "test-fix", "flashnext-coder"),
    ("research security risks", "web-research", "researcher"),
    ("研究代码安全风险", "web-research", "researcher"),
    ("fix failing tests", "code-review", "claude-reviewer"),
    ("fix failing tests", "safety-audit", "claude-reviewer"),
    ("review security", "planning", "flashnext-coder"),
])
def test_explicit_capability_overrides_goal_keywords(text, capability, agent_id):
    registry = AgentRegistry()
    decision = route_task(text, registry, required_capability=capability)
    assert decision.agent_id == agent_id
    assert decision.capability == capability
    assert decision.capability in registry.get(decision.agent_id).capabilities
    assert decision.confidence == 1.0


@pytest.mark.parametrize("text,capability,confidence", [
    ("fix security bug", "safety-audit", .33),
    ("implement feature and review it", "code-review", .67),
    ("修复安全漏洞并复核", "code-review", .5),
    ("research security risks", "safety-audit", .5),
])
def test_legacy_direct_routing_remains_keyword_based(text, capability, confidence):
    decision = route_task(text)
    assert decision.agent_id == "claude-reviewer"
    assert decision.capability == capability
    assert decision.confidence == confidence
    assert route_task(text, required_capability=None).to_dict() == decision.to_dict()


def test_required_capability_rejects_legacy_partial_match(tmp_path):
    (tmp_path / "coder.json").write_text(json.dumps({
        "id": "coder", "display_name": "Coder", "cli": "codex",
        "capabilities": ["code-edit"],
    }))
    registry = AgentRegistry(tmp_path)
    legacy = route_task("review code", registry)
    assert (legacy.agent_id, legacy.capability) == ("coder", "code-review")
    with pytest.raises(ValueError, match="no registered agent supports task capabilities: code-review"):
        route_task("review code", registry, required_capability="code-review")


def test_required_capability_ignores_unrelated_overlap_and_uses_id_tie_breaker(tmp_path):
    for agent_id, capabilities in [
        ("z-mixed", ["code-edit", "code-review", "safety-audit"]),
        ("a-coder", ["code-edit"]),
    ]:
        (tmp_path / (agent_id + ".json")).write_text(json.dumps({
            "id": agent_id, "display_name": agent_id, "cli": "codex",
            "capabilities": capabilities,
        }))
    registry = AgentRegistry(tmp_path)
    decisions = [route_task("implement feature and review security", registry,
                            required_capability="code-edit").to_dict() for _ in range(5)]
    assert decisions == [decisions[0]] * 5
    assert decisions[0]["agent_id"] == "a-coder"


@pytest.mark.parametrize("capability", ["", " ", True, 1, []])
def test_invalid_required_capability_is_rejected(capability):
    with pytest.raises(ValueError, match="required_capability must be a non-empty string"):
        route_task("fix security bug", required_capability=capability)
