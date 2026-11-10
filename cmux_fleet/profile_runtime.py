"""Pure authorization and profile transitions; no Redis or process operations."""

from dataclasses import dataclass
import json
import re
from typing import Any, Dict, Mapping, Optional, Tuple

from cmux_fleet.agent_registry import AGENT_ID_RE, AgentRegistry


class ProfileRuntimeError(ValueError):
    """A profile request is untrusted, ambiguous, or unsupported."""


_CJK_STOP = "。，；：、！？（）「」『』【】《》"


def _marker(instruction: str, name: str) -> Optional[str]:
    # Read the whole token: a malformed/overlong id must not match a valid prefix. A token ends at
    # whitespace or CJK punctuation: "Recommended profile: x。" names x (incident: 。 kept -> blocked).
    values = re.findall(r"\b" + re.escape(name) + r":[ \t]*([^\s" + _CJK_STOP + r"]*)", instruction, re.IGNORECASE)
    if not values:
        return None
    values = {value.rstrip(".,;") for value in values}
    if len(values) != 1 or "" in values:
        raise ProfileRuntimeError("missing or conflicting %s markers" % name)
    return values.pop()


def parse_recommended_profile(instruction: str) -> Optional[str]:
    value = _marker(instruction, "Recommended profile")
    if value is not None and not AGENT_ID_RE.fullmatch(value):
        raise ProfileRuntimeError("invalid recommended profile id: %s" % value)
    return value


def desired_profile(slot: str, instruction: str, task: Mapping[str, Any]) -> Optional[str]:
    """Authorize only stored assignments, narrowed by an optional Subtask marker.

    Without a recommendation, all matching assignments must agree on one profile.
    Bare profile ids mentioned in prose have no authority.
    """
    recommended = parse_recommended_profile(instruction)
    raw = task.get("profile_assignments")
    subtask = _marker(instruction, "Subtask") if raw is not None else None
    if raw is None:
        assignments = []
    else:
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            assignments = data["assignments"]
            if not isinstance(assignments, list) or any(
                not isinstance(item, dict)
                or not all(isinstance(item.get(field), str) and item[field]
                           for field in ("worker", "agent_id", "subtask_id"))
                or not AGENT_ID_RE.fullmatch(item["agent_id"])
                for item in assignments
            ):
                raise ValueError("invalid assignment entries")
        except (KeyError, TypeError, ValueError) as exc:
            raise ProfileRuntimeError("invalid task profile_assignments") from exc
    candidates = [item for item in assignments if item["worker"] == slot]
    if subtask is not None:
        candidates = [item for item in candidates if item["subtask_id"] == subtask]
        if not candidates:
            raise ProfileRuntimeError("subtask %s is not assigned to %s" % (subtask, slot))
    allowed = {item["agent_id"] for item in candidates}
    if recommended is not None:
        if recommended not in allowed:
            raise ProfileRuntimeError("profile %s is not assigned to %s%s" % (
                recommended, slot, " for subtask " + subtask if subtask else ""))
        return recommended
    if len(allowed) > 1:
        raise ProfileRuntimeError("ambiguous profile assignment for %s; include Recommended profile or Subtask" % slot)
    return next(iter(allowed)) if allowed else None


def validate_live_setting(value: Any, *, allowed: bool = False) -> None:
    """Require two explicit booleans; config permission alone never opts a task in."""
    if type(allowed) is not bool:
        raise ProfileRuntimeError("allow_profile_live_switch must be a boolean")
    if type(value) is not bool:
        raise ProfileRuntimeError("apply_profiles_live must be a boolean")
    if value and not allowed:
        raise ProfileRuntimeError("apply_profiles_live=true requires allow_profile_live_switch=true in fleet config")


def task_live_setting(task: Mapping[str, Any], allowed: bool = False) -> bool:
    value = task.get("apply_profiles_live", False)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise ProfileRuntimeError("apply_profiles_live must be a boolean") from exc
    validate_live_setting(value, allowed=allowed)
    return value


@dataclass(frozen=True)
class ProfileApplication:
    desired_profile: str = ""
    state: str = "none"
    capabilities: Tuple[str, ...] = ()
    # Advisory metadata is separate from the process READY identity.
    advisory_profile: Optional[str] = None
    reason: str = ""

    def metadata(self) -> Dict[str, str]:
        """An explicit allowlist: no slot, label, generation, or process fields."""
        fields = {
            "desired_profile": self.desired_profile,
            "profile_state": self.state,
            "profile_capabilities": json.dumps(self.capabilities, ensure_ascii=False),
            "profile_reason": self.reason,
        }
        if self.advisory_profile is not None:
            fields["advisory_profile"] = self.advisory_profile
        return fields


def prepare_profile(
    slot: str,
    instruction: str,
    task: Mapping[str, Any],
    worker: Mapping[str, str],
    registry: Optional[AgentRegistry],
    *,
    allow_profile_live_switch: bool = False,
) -> ProfileApplication:
    """Compute a transition without mutating the worker or the task.

    Busy/handoff (and unknown states) can record intent but cannot apply it. The
    daemon re-evaluates the queued instruction at its idle delivery boundary.
    """
    live = task_live_setting(task, allow_profile_live_switch)
    desired = desired_profile(slot, instruction, task)
    if desired is None:
        if live:
            raise ProfileRuntimeError('live instruction requires an assigned profile for ' + slot)
        return ProfileApplication()
    if registry is None:
        raise ProfileRuntimeError("profile registry is required for assigned profile " + desired)
    try:
        card = registry.get(desired)
    except KeyError as exc:
        raise ProfileRuntimeError("unknown assigned profile: " + desired) from exc

    state = worker.get("state")
    safe_boundary = state == "idle" or (state == "starting" and str(worker.get("delivered", "0")) == "0")
    if not safe_boundary:
        return ProfileApplication(desired, "pending", card.capabilities,
                                  reason="waiting for an idle worker boundary")
    if live:
        if state != "idle":
            return ProfileApplication(desired, "pending", card.capabilities,
                                      reason="waiting for READY and an idle worker boundary")
        if (worker.get("runtime_profile") == desired and worker.get("runtime_ready") == "1"
                and worker.get("current_profile") == desired):
            return ProfileApplication(desired, "applied-live", card.capabilities,
                                      reason="assigned runtime returned READY")
        return ProfileApplication(desired, "switching", card.capabilities,
                                  reason="controlled respawn required before delivery")
    current = worker.get("advisory_profile")
    if current and current != desired:
        return ProfileApplication(desired, "pending", card.capabilities,
                                  reason="different profile requested; live CLI/model switching is disabled")
    return ProfileApplication(desired, "applied-metadata", card.capabilities, desired,
                              "advisory metadata only; running CLI/model is unchanged")


def launch_drift(frozen: Optional[str], expected: Mapping[str, Any]) -> Optional[str]:
    """Which part of a frozen runtime_launch differs from the launch resolved now (None = same).

    Covers the advisor ``extras`` (effort) as well as the card fields and role text."""
    try:
        current = json.loads(frozen or "{}")
    except ValueError:
        return "runtime_launch is not valid JSON"
    for part in ("profile_id", "overrides", "role_text", "extras"):
        if current.get(part) != expected.get(part):
            return part
    return None


def profile_advisory(slot: str, application: ProfileApplication) -> str:
    if not application.desired_profile:
        return ""
    if application.state == "applied-live":
        return ("[Applied runtime profile: %s]\nCapabilities: %s\n"
                "You are still the stable slot %s. The daemon started this runtime and accepted READY. "
                "Do not launch or switch CLIs, models, or credentials yourself.\n\n"
                ) % (application.desired_profile, ", ".join(application.capabilities), slot)
    return (
        "[Recommended agent profile: %s]\nCapabilities: %s\n"
        "This profile is advisory metadata. You are still the stable slot %s.\n"
        "No CLI/model switch or role file application is authorized by this advisory. "
        "Continue using your existing runtime; do not launch or switch CLIs, models, or credentials.\n\n"
    ) % (application.desired_profile, ", ".join(application.capabilities), slot)
