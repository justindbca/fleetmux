"""This machine's node record (`nodes/<node>` on the hub): pushed every `beat_s` and at once on a change of
state. Every push is a new orphan commit with `beat` + 1; the ref has one writer, so the push is forced."""
from __future__ import annotations

from . import records as R


class Beats:
    def __init__(self, hub, *, node: str, fleet: dict, clock, settings):
        self.hub = hub
        self.node = node
        self.fleet = dict(fleet or {})
        self.clock = clock
        self.settings = settings
        self.last: dict | None = None        # the last record the hub accepted
        self._pushed_mono: float | None = None

    def adopt(self, nodes: dict) -> None:
        """After a restart: continue from this machine's record on the hub (never count the beat down)."""
        view = (nodes or {}).get(self.node)
        if view and (self.last is None or view.record["beat"] > self.last["beat"]):
            self.last = view.record

    def due(self) -> bool:
        return self._pushed_mono is None or self.clock.mono() - self._pushed_mono >= self.settings.hot("beat_s")

    def push(self, *, state: str, machine: dict, tasks=(), slots=(), idle: bool = True, role=None,
             leaving_reason=None, timeout_s=None):
        fields = {"state": state, "machine": machine, "tasks": list(tasks), "slots": list(slots), "idle": idle,
                  "role": role, "leaving_reason": leaving_reason, "fleet": self.fleet}
        if self.last is None:
            rec = R.new_node(job=self.hub.job, node=self.node, machine=machine, state=state, fleet=self.fleet)
            rec.update(fields)
            R.decode_node(rec)
        else:
            rec = R.next_node(self.last, **fields)
        res = self.hub.push_node(rec, timeout_s=timeout_s)
        if res.accepted:
            self.last = rec
            self._pushed_mono = self.clock.mono()
        return res
