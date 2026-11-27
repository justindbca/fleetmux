"""Worker cards and dispatch plans: every card runs the plain worker role, a code-change plan is one implementation
subtask (no review subtask is appended), and official Claude workers implement on claude-coder, never a Pi card."""
import json

import pytest

from fleetmux import fleet, profile_launch
from fleetmux.agent_registry import AgentRegistry
from fleetmux.planner import build_dispatch_plan
from fleetmux.router import route_task
from test_fleet import fx  # noqa: F401  (fixture)
from test_v2_codex_claude import mixed  # noqa: F401
from test_message_bus_v2 import private_redis_v2  # noqa: F401

GOAL = "implement a router bug fix and tests"


def plan_pairs(task):
    return [(t["id"], t["agent_id"], t["depends_on"]) for t in json.loads(task["dispatch_plan"])["subtasks"]]


def test_cards_keep_plain_worker_role():
    registry = AgentRegistry()
    assert {card.role_file for card in registry.cards if card.role_file} == {"worker.md"}
    assert registry.get("flashnext-coder").role_file == "worker.md"
    assert registry.get("researcher").role_file == "worker.md"


def test_claude_coder_card_is_claude_opus_with_plain_worker_role():
    card = AgentRegistry().get("claude-coder")
    assert (card.cli, card.model, card.role_file) == ("claude", "opus", "worker.md")
    assert {"code-edit", "test-fix"} <= set(card.capabilities) and "code-review" not in card.capabilities


def test_legacy_routing_without_preferred_cli_is_unchanged():
    assert [(t.id, t.agent_id) for t in build_dispatch_plan(GOAL).subtasks] == [("implementation", "flashnext-coder")]
    assert build_dispatch_plan("plan the architecture").subtasks[0].agent_id == "flashnext-coder"
    assert route_task("fix failing tests").agent_id == "flashnext-coder"


def test_preferred_claude_plan_uses_claude_coder_only():
    plan = build_dispatch_plan(GOAL, preferred_cli="claude")
    assert [(t.id, t.agent_id, t.depends_on) for t in plan.subtasks] == [("implementation", "claude-coder", ())]


@pytest.mark.parametrize("worker_cli", ["pi", "codex"])
def test_pi_and_codex_fleets_keep_existing_plan(fx, worker_cli):  # noqa: F811
    fx.finish("cancelled", "replace fixture task")
    fx.cfg["worker_cli"] = worker_cli
    fx.submit({"goal": GOAL, "auto_agents": True})
    assert plan_pairs(fx.task()) == [("implementation", "flashnext-coder", [])]
    fx.finish("cancelled", "next goal")
    fx.submit({"goal": "plan the architecture", "auto_agents": True})
    assert plan_pairs(fx.task()) == [("planning", "flashnext-coder", [])]


def test_codex_supervisor_claude_workers_plan_passes_v2_preflight(mixed, monkeypatch):  # noqa: F811
    f = mixed
    f.cfg["allow_profile_live_switch"] = True
    f.submit({"goal": GOAL, "auto_agents": True, "apply_profiles_live": True})
    assert plan_pairs(f.task()) == [("implementation", "claude-coder", [])]
    monkeypatch.setattr(fleet.shutil, "which", lambda binary: "/usr/bin/" + binary)
    registry = AgentRegistry()
    f.preflight_profile(profile_launch.resolve_launch(registry.get("claude-coder"), registry, f.cfg))
    with pytest.raises(ValueError, match="cannot change the worker CLI"):  # the Pi card this replaces is rejected by v2
        f.preflight_profile(profile_launch.resolve_launch(registry.get("flashnext-coder"), registry, f.cfg))
