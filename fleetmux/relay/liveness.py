"""Judging the other machines of a job.

Only this machine's monotonic clock is used: `last_change[X]` is the observer's own time of the last listing in
which X's node ref changed or a task record now owned by X changed. Nothing X wrote about time is read, and no
record content counts as a sign of life (only a new sha does).

Classes: listing failed -> classes kept, every timer restarts at the next good listing (an observer that cannot
read the hub judges nobody); never seen -> absent; node record says `leaving` -> leaving; a change within
`lost_after_s` -> online; otherwise lost. A machine may be taken over when it is lost, or leaving for at least
`offline_grace_s` on this clock. Timers live in memory only, so a restart can delay a takeover, never cause one.
Each class change is stored in the view store before its `relay.peer` notice, and the baseline class after a
restart is read back from there, so a notice is never sent twice."""
from __future__ import annotations

from . import records as R
from .refs import RefNames

ONLINE, LEAVING, LOST, ABSENT = "online", "leaving", "lost", "absent"


def describe_tasks(rec: dict | None) -> str:
    items = [f"{t.get('id')}: {t.get('phase') or t.get('state') or '-'}" for t in (rec or {}).get("tasks") or []]
    return ", ".join(items) or "none"


class Observer:
    def __init__(self, settings, me: str, clock, events, *, job: str):
        self.settings = settings
        self.names = RefNames(job)
        self.me = me
        self.clock = clock
        self.events = events
        self._ref_sha: dict = {}         # node -> last seen sha of its node ref
        self._task_sha: dict = {}        # task -> last seen record sha
        self._last_change: dict = {}
        self._ever: set = set()
        self._cls: dict = {}
        self._baselined: set = set()
        self._nodes: dict = {}           # node -> NodeView
        self._leaving_since: dict = {}
        self._gone_sha: dict = {}        # node -> its node ref sha when it was judged lost / leaving
        self._fresh = True
        self._blind = False

    # ------------------------------------------------------------------ helpers

    def _baseline(self, node: str) -> None:
        if node in self._baselined:
            return
        self._baselined.add(node)
        view = self.events.view_node(node) if self.events is not None else None
        if view and view.get("class"):
            self._cls[node] = view["class"]
            if view["class"] in (LOST, LEAVING):
                self._gone_sha[node] = view.get("sha")

    def _rec(self, node: str) -> dict:
        view = self._nodes.get(node)
        return (view.record if view else None) or {}

    # ------------------------------------------------------------------ main entry

    def update(self, listing, tasks: dict, nodes: dict) -> dict:
        """Classes of the other machines after one listing (`tasks` / `nodes` = the views read from it)."""
        if listing is None or not listing.ok:
            self._fresh = True
            self._blind = True
            return self.classes()
        now = self.clock.mono()
        fresh, self._fresh, self._blind = self._fresh, False, False
        changed = set()
        for ref, sha in listing.refs.items():
            parsed = self.names.parse(ref)
            if parsed and parsed[0] == "node":
                node = parsed[1]
                self._ever.add(node)
                if self._ref_sha.get(node) != sha:
                    self._ref_sha[node] = sha
                    changed.add(node)
        for task, view in (tasks or {}).items():
            if self._task_sha.get(task) != view.sha:
                self._task_sha[task] = view.sha
                owner = (view.record or {}).get("owner")
                if owner:
                    self._ever.add(owner)
                    changed.add(owner)
        for node, view in (nodes or {}).items():
            self._ever.add(node)
            self._nodes[node] = view
        for node in self._ever:
            if fresh or node in changed or node not in self._last_change:
                self._last_change[node] = now
        for node in sorted(self._ever - {self.me}):
            self._baseline(node)
            prev = self._cls.get(node)
            cls = self._classify(node, now)
            self._cls[node] = cls
            if self.events is not None:
                self.events.store_node(node, self._nodes.get(node), cls)
                self._notice(node, prev, cls, changed)
        if self.events is not None:
            self.events.baseline()
        return self.classes()

    def _classify(self, node: str, now: float) -> str:
        if node not in self._ever:
            return ABSENT
        if self._rec(node).get("state") == R.LEAVING:
            self._leaving_since.setdefault(node, now)
            return LEAVING
        self._leaving_since.pop(node, None)
        return ONLINE if now - self._last_change[node] <= self.settings.hot("lost_after_s") else LOST

    def _notice(self, node: str, prev, cls: str, changed: set) -> None:
        rec = self._rec(node)
        sha = self._ref_sha.get(node)
        if prev == cls:
            return
        if cls in (LOST, LEAVING):
            self._gone_sha[node] = sha
        if cls == LOST:
            what = f"Machine {node} is lost: {self.judged(node)}"
        elif cls == LEAVING:
            what = f"Machine {node} said goodbye ({rec.get('leaving_reason') or 'unknown'})"
        elif cls == ONLINE and prev in (LOST, LEAVING):
            if sha == self._gone_sha.get(node):
                return                            # timers restarted (restart, outage): no new record, not back
            self._gone_sha.pop(node, None)
            what = f"Machine {node} is back ({rec.get('state') or '?'}, idle {rec.get('idle')})"
        else:
            return
        text = f"{what}. Its last record: state {rec.get('state') or '?'}, tasks {describe_tasks(rec)}."
        self.events.emit("relay.peer", f"peer:{node}:{cls}:{sha}",
                         audit={"node": node, "class": cls, "node_sha": sha}, notify=text)

    # ------------------------------------------------------------------ queries

    def classes(self) -> dict:
        return {n: c for n, c in self._cls.items() if n != self.me}

    def judged(self, node: str) -> str:
        rec = self._rec(node)
        if rec.get("state") == R.LEAVING:
            return f"goodbye: {rec.get('leaving_reason') or 'unknown'}"
        last = self._last_change.get(node)
        secs = int(self.clock.mono() - last) if last is not None else 0
        return f"lost after {secs} s without a change (observer's clock)"

    def takeable(self, node: str) -> bool:
        """Whether this machine may take `node`'s tasks now."""
        if self._blind or node == self.me:
            return False
        cls = self._cls.get(node)
        if cls == LOST:
            return True
        if cls != LEAVING or node not in self._leaving_since:
            return False
        return self.clock.mono() - self._leaving_since[node] >= self.settings.hot("offline_grace_s")
