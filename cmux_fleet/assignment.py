"""Deterministic profile metadata assignment for stable fleet worker slots.

Assignments are planning metadata only.  A profile such as ``flashnext-coder``
never becomes a worker name and this module does not enqueue or schedule work.
"""

from typing import Any, Dict, Optional, Sequence

from cmux_fleet.agent_registry import AgentRegistry
from cmux_fleet.planner import DispatchPlan


def assign_profiles(
        plan: DispatchPlan,
        workers: Sequence[str],
        registry: Optional[AgentRegistry] = None,
        running: Optional[Dict[str, Optional[str]]] = None,
) -> Dict[str, Any]:
    """Map planned profile recommendations onto stable worker slots.

    New profile ids take the next worker in order until capacity is exhausted;
    later profiles reuse workers round-robin.  Repeated uses of one profile stay
    on its first assigned worker.  Dependencies are copied as metadata and are
    deliberately not executed here.

    ``running`` ({slot: model id it runs now}, model_usage) lets a new profile take a free slot
    that already runs its model first (same-slot reuse: no extra max_concurrent ticket). None keeps
    the plain order. Needs ``registry`` to know each profile's model.
    """
    if isinstance(workers, (str, bytes)):
        raise ValueError("workers must be a non-empty list of stable slot names")
    slots = tuple(workers)
    if not slots or any(not isinstance(slot, str) or not slot.strip() for slot in slots):
        raise ValueError("workers must be a non-empty list of stable slot names")
    if len(set(slots)) != len(slots):
        raise ValueError("worker slot names must be distinct")

    profile_ids = {subtask.agent_id for subtask in plan.subtasks}
    if registry is not None:
        profile_ids.update(card.id for card in registry)
    conflicts = sorted(profile_ids.intersection(slots))
    if conflicts:
        raise ValueError("worker slots must not use agent profile ids: " + ", ".join(conflicts))

    subtask_ids = [subtask.id for subtask in plan.subtasks]
    if len(set(subtask_ids)) != len(subtask_ids):
        raise ValueError("dispatch plan subtask ids must be distinct")
    known = set(subtask_ids)
    for subtask in plan.subtasks:
        missing = sorted(set(subtask.depends_on) - known)
        if missing:
            raise ValueError(
                "subtask %s has unknown dependencies: %s" % (subtask.id, ", ".join(missing))
            )

    by_profile = {}
    next_slot = 0
    assignments = []
    order = list(slots)
    for subtask in plan.subtasks:
        worker = by_profile.get(subtask.agent_id)
        if worker is None:
            worker = _reuse(subtask.agent_id, order[next_slot:], running, registry)
            if worker is not None:
                order.remove(worker)
                order.insert(next_slot, worker)
            worker = order[next_slot % len(order)]
            by_profile[subtask.agent_id] = worker
            next_slot += 1
        assignments.append({
            "subtask_id": subtask.id,
            "worker": worker,
            "agent_id": subtask.agent_id,
            "capability": subtask.capability,
            "depends_on": list(subtask.depends_on),
        })
    return {"assignments": assignments}


def _reuse(profile_id, free, running, registry):
    """A not yet assigned slot already running this profile's model, if any."""
    if not running or registry is None:
        return None
    from cmux_fleet.model_usage import limit_key
    try:
        key = limit_key(registry.get(profile_id))
    except KeyError:
        return None
    return next((slot for slot in free if running.get(slot) == key), None)
