"""Who may take a task and in which order. Pure: no git, no wall clock, no model.

Candidates are machines whose node record says `online` (settled; not `rejoining`, not `leaving`) and that this
machine judges online, plus this machine itself when its own record says `online`. Hard limits here: free disk
above `disk_floor_any_gb` (and `disk_floor_heavy_gb` for heavy work), fewer than `max_tasks_per_node` working tasks
(and `max_heavy_per_node` heavy ones). Toolchain (`needs`) matching is not done here: the caller passes it in as a
`candidate_filter(candidate, task) -> reason | None`, checked first. Order: the task's `prefer` machine first when
it qualifies; the coordinator role prefers always-on machines; heavy work by capability score, light work by
fewest tasks. Every number is read from the settings at each call."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from . import records as R
from .liveness import ONLINE

COORDINATOR = "coordinator"


@dataclass(frozen=True)
class Candidate:
    id: str
    tasks: int
    heavy: int
    machine: dict | None
    score: float


CandidateFilter = Callable[[Candidate, dict], "str | None"]   # (candidate, task record) -> reason or None


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def score_of(machine: dict | None, settings) -> float:
    """cores x w + mem_gb x w + disk_free_gb x w; 0 when any value is unknown."""
    w = settings.score_weights()
    if not machine or not all(_num(machine.get(k)) for k in w):
        return 0
    return sum(w[k] * machine[k] for k in w)


def counts(tasks: dict) -> dict:
    """node -> (working tasks, working heavy tasks) from the task records."""
    out = {}
    for view in (tasks or {}).values():
        rec = view.record or {}
        if rec.get("state") != R.WORKING or not rec.get("owner"):
            continue
        n, h = out.get(rec["owner"], (0, 0))
        out[rec["owner"]] = (n + 1, h + (1 if rec.get("weight") == R.HEAVY else 0))
    return out


def candidates(nodes: dict, tasks: dict, classes: dict, me: str, settings) -> list:
    """Sorted by machine name."""
    c = counts(tasks)
    out = []
    for name in sorted(nodes or {}):
        rec = nodes[name].record or {}
        if rec.get("state") != R.ONLINE:
            continue
        if name != me and (classes or {}).get(name) != ONLINE:
            continue
        n, h = c.get(name, (0, 0))
        machine = rec.get("machine")
        out.append(Candidate(name, n, h, machine, score_of(machine, settings)))
    return out


def why_not(c: Candidate, task: dict, settings, *, ignore_caps: bool = False,
            candidate_filter: CandidateFilter | None = None) -> str | None:
    """None when `c` may take `task`; else the first reason: the filter's reason | unknown | disk | disk_heavy |
    cap | cap_heavy. The filter must not judge free slots (that is `ignore_caps`' job)."""
    if candidate_filter is not None:
        reason = candidate_filter(c, task)
        if reason:
            return str(reason)
    machine = c.machine or {}
    free = machine.get("disk_free_gb")
    if not _num(free):
        return "unknown"
    heavy = task.get("weight") == R.HEAVY
    if free < settings.hot("disk_floor_any_gb"):
        return "disk"
    if heavy and free < settings.hot("disk_floor_heavy_gb"):
        return "disk_heavy"
    if not ignore_caps:
        if c.tasks >= settings.hot("max_tasks_per_node"):
            return "cap"
        if heavy and c.heavy >= settings.hot("max_heavy_per_node"):
            return "cap_heavy"
    return None


def rank(cands: list, task: dict, settings, *, candidate_filter: CandidateFilter | None = None) -> list:
    """The candidates that may take `task`, best first."""
    ok = [c for c in cands if why_not(c, task, settings, candidate_filter=candidate_filter) is None]
    heavy = task.get("weight") == R.HEAVY
    prefer = task.get("prefer")

    def key(c):
        head = (0 if prefer and c.id == prefer else 1,)
        if task.get("id") == COORDINATOR:
            head += (0 if (c.machine or {}).get("always_on") else 1,)
        if heavy:
            return head + (-c.score, c.tasks, c.id)
        return head + (c.tasks, -c.score, c.id)
    return sorted(ok, key=key)


def plan(tasks: dict, cands: list, settings, *, candidate_filter: CandidateFilter | None = None) -> dict:
    """task id -> the machine that should take it (or None), heavy tasks first, counts raised inside the plan."""
    pool = {c.id: c for c in cands}
    out = {}
    order = sorted(tasks, key=lambda t: (0 if tasks[t].record.get("weight") == R.HEAVY else 1, t))
    for tid in order:
        rec = tasks[tid].record
        ranked = rank(list(pool.values()), rec, settings, candidate_filter=candidate_filter)
        out[tid] = ranked[0].id if ranked else None
        if ranked:
            c = ranked[0]
            pool[c.id] = replace(c, tasks=c.tasks + 1, heavy=c.heavy + (1 if rec.get("weight") == R.HEAVY else 0))
    return out


def unplaceable(task: dict, cands: list, settings, *,
                candidate_filter: CandidateFilter | None = None) -> dict | None:
    """None when some candidate could take `task` once it has a free slot; else {machine: reason} (for the
    relay.unplaceable notice). No candidate at all -> {}."""
    reasons = {c.id: why_not(c, task, settings, ignore_caps=True, candidate_filter=candidate_filter) for c in cands}
    if any(r is None for r in reasons.values()):
        return None
    return reasons


class Stagger:
    """Rank k waits k x claim_stagger_s (this machine's clock) from the first time it saw the chance."""

    def __init__(self, clock, settings):
        self.clock = clock
        self.settings = settings
        self._first: dict = {}

    def ready(self, key: str, rank_index: int) -> bool:
        first = self._first.setdefault(key, self.clock.mono())
        return self.clock.mono() - first >= rank_index * self.settings.hot("claim_stagger_s")

    def forget(self, key: str) -> None:
        self._first.pop(key, None)
