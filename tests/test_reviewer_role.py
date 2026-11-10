"""Independent reviewer worker: claude-reviewer card -> reviewer.md role (fleet copy of the review rules)."""
from cmux_fleet import fleet, profile_launch
from cmux_fleet.agent_registry import AgentRegistry, PACKAGE_DIR
from cmux_fleet.planner import build_dispatch_plan
from test_fleet import fx  # noqa: F401  (fixture used by live)
from test_profile_integration import dispatch
from test_profile_live import live  # noqa: F401

REQUIRED = [
    "复核是为了尽快上线",                        # top principle, verbatim
    "multi-round-review.md", "R-WKF-033",       # sync note to the source rules
    "Read-only",
    "**P0**", "**P1**", "**P2**",
    "Unsure → go down (P2)",
    "P0 even when unsure",                      # real-money safety exception
    "after go-live, in what normal scenario it happens → wrong result",
    "file:line",
    "Legacy", "Out of scope", "## 记录·不修",
    "From r2 on: only run the failing tests / reproduction commands r1 attached",
    "失败测试或复现命令",
    "PASS = zero P0 and zero P1",
    "## This round's issues",                   # report template
    "Use the base reply format (Done / Evidence / Not done / Blocked)",
    "- Evidence: `<report path> <bytes> bytes; verdict PASS|FAIL",
]


def test_claude_reviewer_card_uses_reviewer_role_and_opus():
    registry = AgentRegistry()
    card = registry.get("claude-reviewer")
    assert (card.cli, card.role_file, card.model) == ("claude", "reviewer.md", "opus")
    assert (PACKAGE_DIR / "agents" / "reviewer.md").is_file()
    launch = profile_launch.resolve_launch(card, registry, {})
    assert launch["overrides"] == {"worker_cli": "claude", "worker_model": "opus"}
    text = launch["role_text"]
    assert text == (registry.directory / "reviewer.md").read_text()
    missing = [item for item in REQUIRED if item not in text]
    assert not missing, missing


def test_other_cards_keep_plain_worker_role():
    registry = AgentRegistry()
    assert registry.get("flashnext-coder").role_file == "worker.md"
    assert registry.get("researcher").role_file == "worker.md"


def test_code_change_plan_sends_dependent_review_to_reviewer_role():
    plan = build_dispatch_plan("implement feature X and add tests")
    assert [(t.id, t.agent_id, t.depends_on) for t in plan.subtasks] == [
        ("implementation", "flashnext-coder", ()),
        ("review", "claude-reviewer", ("implementation",)),
    ]
    assert AgentRegistry().get(plan.subtasks[1].agent_id).role_file == "reviewer.md"


def test_spawned_reviewer_scope_keeps_base_protocol_then_reviewer_role(live):
    dispatch(live, "claude-reviewer")
    live.deliver("worker-1")
    scope = (fleet.agent_cwd(live.cfg, "worker-1") / "CLAUDE.md").read_text()
    base = scope.index("## 3. Reply format")
    role = scope.index("## Assigned profile role")
    assert base < role < scope.index("复核是为了尽快上线")
    assert "{{" not in scope


# -- official Claude workers: implementation goes to claude-coder, never to a Pi card

import json

import pytest

from cmux_fleet.router import route_task
from test_v2_codex_claude import mixed  # noqa: F401,E402
from test_message_bus_v2 import private_redis_v2  # noqa: F401,E402

GOAL = "implement a router bug fix and tests"


def plan_pairs(task):
    return [(t["id"], t["agent_id"], t["depends_on"]) for t in json.loads(task["dispatch_plan"])["subtasks"]]


def test_claude_coder_card_is_claude_opus_with_plain_worker_role():
    card = AgentRegistry().get("claude-coder")
    assert (card.cli, card.model, card.role_file) == ("claude", "opus", "worker.md")
    assert {"code-edit", "test-fix"} <= set(card.capabilities) and "code-review" not in card.capabilities


def test_legacy_routing_without_preferred_cli_is_unchanged():
    assert [(t.id, t.agent_id) for t in build_dispatch_plan(GOAL).subtasks] == [
        ("implementation", "flashnext-coder"), ("review", "claude-reviewer")]
    assert build_dispatch_plan("plan the architecture").subtasks[0].agent_id == "flashnext-coder"
    assert route_task("fix failing tests").agent_id == "flashnext-coder"


def test_preferred_claude_plan_uses_claude_coder_then_claude_reviewer():
    plan = build_dispatch_plan(GOAL, preferred_cli="claude")
    assert [(t.id, t.agent_id, t.depends_on) for t in plan.subtasks] == [
        ("implementation", "claude-coder", ()), ("review", "claude-reviewer", ("implementation",))]


@pytest.mark.parametrize("worker_cli", ["pi", "codex"])
def test_pi_and_codex_fleets_keep_existing_plan(fx, worker_cli):  # noqa: F811
    fx.finish("cancelled", "replace fixture task")
    fx.cfg["worker_cli"] = worker_cli
    fx.submit({"goal": GOAL, "auto_agents": True})
    assert plan_pairs(fx.task()) == [
        ("implementation", "flashnext-coder", []), ("review", "claude-reviewer", ["implementation"])]
    fx.finish("cancelled", "next goal")
    fx.submit({"goal": "plan the architecture", "auto_agents": True})
    assert plan_pairs(fx.task()) == [("planning", "flashnext-coder", [])]


def test_codex_supervisor_claude_workers_plan_passes_v2_preflight(mixed, monkeypatch):  # noqa: F811
    f = mixed
    f.cfg["allow_profile_live_switch"] = True
    f.submit({"goal": GOAL, "auto_agents": True, "apply_profiles_live": True})
    assert plan_pairs(f.task()) == [
        ("implementation", "claude-coder", []), ("review", "claude-reviewer", ["implementation"])]
    monkeypatch.setattr(fleet.shutil, "which", lambda binary: "/usr/bin/" + binary)
    registry = AgentRegistry()
    for profile in ("claude-coder", "claude-reviewer"):
        f.preflight_profile(profile_launch.resolve_launch(registry.get(profile), registry, f.cfg))
    with pytest.raises(ValueError, match="cannot change the worker CLI"):  # the Pi card this replaces is rejected by v2
        f.preflight_profile(profile_launch.resolve_launch(registry.get("flashnext-coder"), registry, f.cfg))
