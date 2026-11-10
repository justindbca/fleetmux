"""Rule-based dispatch plan generation for offline inspection.

This module does not enqueue work.  The assignment layer can map its profile ids to
stable worker slots while keeping the existing task/service protocols unchanged.
"""

from dataclasses import dataclass
import re
from typing import Any, Dict, Optional, Tuple

from cmux_fleet.agent_registry import AgentRegistry
from cmux_fleet.router import classify_task, route_task


@dataclass(frozen=True)
class DispatchSubtask:
    id: str
    title: str
    agent_id: str
    capability: str
    prompt: str
    depends_on: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "agent_id": self.agent_id,
            "capability": self.capability,
            "prompt": self.prompt,
            "depends_on": list(self.depends_on),
        }


@dataclass(frozen=True)
class DispatchPlan:
    goal: str
    kind: str
    subtasks: Tuple[DispatchSubtask, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "goal": self.goal,
            "kind": self.kind,
            "subtasks": [subtask.to_dict() for subtask in self.subtasks],
        }


def _implementation_prompt(goal: str) -> str:
    return (
        "Goal:\n%s\n\n"
        "Implementation task: inspect the current project and its contributor instructions, implement the requested "
        "code change, and add or update focused tests. Preserve existing architecture and protocols. Do not start, "
        "restart, deploy, or replace production services. Deliver a concise list of changed files and the exact "
        "validation commands and results."
    ) % goal


def _review_prompt(goal: str) -> str:
    return (
        "Goal:\n%s\n\n"
        "Independent review task: review the implementation currently present in the shared working tree against "
        "this goal and applicable contributor instructions. Check correctness, regressions, safety boundaries, and "
        "test coverage. Do not deploy or start production services. Report findings in severity order with file and "
        "line references; if there are no findings, state that explicitly and list any residual risks."
    ) % goal


def _research_prompt(goal: str) -> str:
    return (
        "Goal:\n%s\n\n"
        "Research task: investigate the question using relevant web and primary sources. Separate sourced facts from "
        "inference, include source links and dates, note conflicts or missing evidence, and finish with an actionable "
        "summary that directly answers the goal. Do not make unrelated code or production changes."
    ) % goal


def _planning_prompt(goal: str) -> str:
    return (
        "Goal:\n%s\n\n"
        "Planning task: inspect the available project context and produce a concrete implementation plan. Identify "
        "affected components, constraints, validation, rollout boundaries, and unresolved decisions. Do not edit code, "
        "deploy, or start production services."
    ) % goal


def build_dispatch_plan(goal: str, registry: Optional[AgentRegistry] = None,
                        preferred_cli: Optional[str] = None, host: Optional[str] = None,
                        occupancy=None) -> DispatchPlan:
    """``host`` / ``occupancy`` are passed to every ``route_task`` (card hosts / max_concurrent); each
    step also passes its role (implementation: implementer, review: reviewer, research: researcher;
    planning: none, the role vocabulary has no planner) so cards that deny it are not chosen."""
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("goal must be a non-empty string")
    goal = goal.strip()
    registry = registry or AgentRegistry()
    hints = set(classify_task(goal))

    review_goal = re.match(
        r"\s*(?:\b(?:review|audit|inspect)\b|复核|审查|评审|审核)", goal, re.IGNORECASE)
    research_goal = re.match(
        r"\s*(?:\b(?:search|research|browse)\b|搜索|调研|研究)", goal, re.IGNORECASE)

    if review_goal:
        decision = route_task("Review and safety-audit: " + goal, registry,
                              required_capability="code-review", preferred_cli=preferred_cli, host=host, occupancy=occupancy,
                              role="reviewer")
        return DispatchPlan(goal, "review", (
            DispatchSubtask("review", "Review the requested scope", decision.agent_id,
                            decision.capability, _review_prompt(goal)),
        ))

    if research_goal or ("web-research" in hints and not hints.intersection(("code-edit", "test-fix"))):
        decision = route_task("Web research with sources: " + goal, registry,
                              required_capability="web-research", preferred_cli=preferred_cli, host=host, occupancy=occupancy,
                              role="researcher")
        return DispatchPlan(goal, "research", (
            DispatchSubtask("research", "Research and synthesize evidence", decision.agent_id,
                            decision.capability, _research_prompt(goal)),
        ))

    if hints.intersection(("code-edit", "test-fix")):
        implementation = route_task("Implement this code change: " + goal, registry,
                                    required_capability="test-fix" if "test-fix" in hints else "code-edit", preferred_cli=preferred_cli, host=host, occupancy=occupancy,
                                    role="implementer")
        review = route_task("Review and safety-audit this code change: " + goal, registry,
                            required_capability="code-review", preferred_cli=preferred_cli, host=host, occupancy=occupancy,
                              role="reviewer")
        subtasks = (
            DispatchSubtask("implementation", "Implement and test the change", implementation.agent_id,
                            implementation.capability, _implementation_prompt(goal)),
            DispatchSubtask("review", "Independently review the change", review.agent_id,
                            review.capability, _review_prompt(goal), ("implementation",)),
        )
        return DispatchPlan(goal, "code-change", subtasks)

    if hints.intersection(("code-review", "safety-audit")):
        decision = route_task("Review and safety-audit: " + goal, registry,
                              required_capability="code-review", preferred_cli=preferred_cli, host=host, occupancy=occupancy,
                              role="reviewer")
        return DispatchPlan(goal, "review", (
            DispatchSubtask("review", "Review the requested scope", decision.agent_id,
                            decision.capability, _review_prompt(goal)),
        ))

    decision = route_task("Plan and design: " + goal, registry,
                          required_capability="planning", preferred_cli=preferred_cli, host=host, occupancy=occupancy)
    return DispatchPlan(goal, "planning", (
        DispatchSubtask("planning", "Plan the work", decision.agent_id,
                        decision.capability, _planning_prompt(goal)),
    ))
