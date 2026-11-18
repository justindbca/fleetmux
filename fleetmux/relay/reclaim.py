"""Claims and takeovers on the task table.

Every write is a compare-and-swap on the task ref; the record commit is saved as `cas_pending` before the push,
so a push with an unknown outcome (timeout, no answer) is settled by sha on a later listing or after a restart:
the saved record is now on the hub -> it landed; the hub still holds the expected record -> push the same saved
record again (never a new one); anything else -> someone else wrote, drop it.

A takeover round looks at every `working` / `handed_over` task whose owner this machine may take over
(Observer.takeable). Each machine ranks the candidates with the same rule (assign.rank, plus the caller's
`candidate_filter`, e.g. toolchain matching; every machine must pass the same filter); the k-th ranked machine
waits k x claim_stagger_s on its own clock before it tries. When no machine qualifies, the task is written back
as `reclaimed` (no owner) and a `relay.unplaceable` notice says what is missing."""
from __future__ import annotations

from dataclasses import dataclass, replace

from . import assign
from . import records as R
from .liveness import LEAVING

WON, LOST_RACE, UNKNOWN = "won", "lost_race", "unknown"


@dataclass(frozen=True)
class ClaimResult:
    task: str
    epoch: int | None
    record_sha: str | None
    outcome: str            # won | lost_race | unknown


class Reclaimer:
    def __init__(self, settings, me: str, hub, events, state, clock, observer, *,
                 candidate_filter: assign.CandidateFilter | None = None):
        self.settings = settings
        self.me = me
        self.hub = hub
        self.events = events
        self.state = state
        self.clock = clock
        self.observer = observer
        self.candidate_filter = candidate_filter
        self.stagger = assign.Stagger(clock, settings)

    # ------------------------------------------------------------------ CAS with cas_pending

    def _cas(self, task: str, view, new: dict) -> ClaimResult:
        sha = R.write_task(self.hub.git, self.hub.cache, new, parent=view.sha)
        self.state.save_cas_pending(task, sha, view.sha)
        res = self.hub.cas_task(task, view.sha, sha)
        if res.won:
            self.state.clear_cas_pending(task)
            return ClaimResult(task, new["epoch"], sha, WON)
        if res.raced:
            self.state.clear_cas_pending(task)
            return ClaimResult(task, new["epoch"], sha, LOST_RACE)
        return ClaimResult(task, new["epoch"], sha, UNKNOWN)

    def claim(self, task: str, view) -> ClaimResult:
        """One claim of a pending / reclaimed task for this machine (TransitionError for any other state)."""
        return self._cas(task, view, R.claim(view.record, me=self.me))

    def settle(self, tasks: dict) -> list:
        """Decide every saved cas_pending entry by sha; returns the ones that landed for this machine."""
        adopted = []
        for task, (record_sha, expected) in sorted(self.state.cas_pending().items()):
            view = tasks.get(task)
            if view is None or view.sha == expected:
                continue                                     # undecided: `step` pushes the saved record again
            if view.sha == record_sha and (view.record or {}).get("owner") == self.me:
                adopted.append(ClaimResult(task, view.record.get("epoch"), record_sha, WON))
            self.state.clear_cas_pending(task)
        return adopted

    def _retry(self, tasks: dict) -> list:
        out = []
        for task, (record_sha, expected) in sorted(self.state.cas_pending().items()):
            view = tasks.get(task)
            if view is None or view.sha != expected:
                continue
            res = self.hub.cas_task(task, expected, record_sha)
            if res.won or res.raced:
                self.state.clear_cas_pending(task)
            if res.won:
                rec = R.read_task(self.hub.git, self.hub.cache, record_sha)
                if rec.get("owner") == self.me:
                    out.append(ClaimResult(task, rec.get("epoch"), record_sha, WON))
        return out

    # ------------------------------------------------------------------ notices

    def _unplaceable(self, rec: dict, reasons: dict, epoch) -> None:
        detail = "; ".join(f"{n} {r}" for n, r in sorted(reasons.items())) or "no machine is online"
        text = (f"Task {rec['id']} \"{rec.get('title')}\" ({rec.get('weight')}, needs "
                f"{', '.join(rec.get('needs') or []) or 'nothing'}) has no machine that can take it: {detail}. "
                f"It waits to be claimed (epoch {epoch}); nothing is forced.")
        self.events.emit("relay.unplaceable", f"unplaceable:{rec['id']}:{epoch}",
                         audit={"task": rec["id"], "epoch": epoch, "reasons": reasons}, notify=text)

    # ------------------------------------------------------------------ one round

    def _why(self, rec: dict, nodes: dict) -> str:
        owner = rec.get("owner")
        node_rec = (nodes.get(owner).record if nodes.get(owner) else None) or {}
        gone = rec.get("state") == R.HANDED_OVER or node_rec.get("state") == R.LEAVING
        gone = gone or self.observer.classes().get(owner) == LEAVING
        return R.WHY_GOODBYE if gone else R.WHY_LOST

    @staticmethod
    def _plus(c, rec: dict):
        return replace(c, tasks=c.tasks + 1, heavy=c.heavy + (1 if rec.get("weight") == R.HEAVY else 0))

    def step(self, tasks: dict, nodes: dict) -> list:
        """One takeover round on the views of the last listing. Returns this machine's results (won ones are to
        be built and started by the caller)."""
        results = self.settle(tasks) + self._retry(tasks)
        pending = self.state.cas_pending()
        dropped = {t: v for t, v in tasks.items()
                   if v.record.get("state") in (R.WORKING, R.HANDED_OVER) and v.record.get("owner") != self.me
                   and self.observer.takeable(v.record.get("owner"))}
        cands = assign.candidates(nodes, tasks, self.observer.classes(), self.me, self.settings)
        pool = {c.id: c for c in cands}
        for task in sorted(dropped, key=lambda t: (0 if dropped[t].record.get("weight") == R.HEAVY else 1, t)):
            if task in pending:
                continue
            view = dropped[task]
            rec = view.record
            ranked = assign.rank(list(pool.values()), rec, self.settings, candidate_filter=self.candidate_filter)
            why = self._why(rec, nodes)
            if not ranked:
                res = self._cas(task, view, R.takeover(rec, to=None, why=why))
                reasons = assign.unplaceable(rec, cands, self.settings, candidate_filter=self.candidate_filter)
                if reasons is not None:
                    self._unplaceable(rec, reasons, rec["epoch"] + 1)
                continue
            ids = [c.id for c in ranked]
            if self.me not in ids or not self.stagger.ready(f"take:{task}:{rec['epoch']}", ids.index(self.me)):
                pool[ranked[0].id] = self._plus(ranked[0], rec)       # another machine is expected to take it
                continue
            res = self._cas(task, view, R.takeover(rec, to=self.me, why=why))
            results.append(res)
            if res.outcome == WON:
                pool[self.me] = self._plus(pool[self.me], rec)
        for task, view in sorted(tasks.items()):
            rec = view.record
            if R.claimable(rec):
                reasons = assign.unplaceable(rec, cands, self.settings, candidate_filter=self.candidate_filter)
                if reasons is not None:
                    self._unplaceable(rec, reasons, rec["epoch"])
        return results
