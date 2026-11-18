"""Local view store and notices: each change is sent once, an audit line then (when there is text) one notice to
this machine's root supervisor. The key of every notice is saved before it is sent, so a restart never repeats
one; the first listing with an empty store is a baseline that sends nothing.

Notice types: relay.take, relay.lost, relay.salvage, relay.peer, relay.unplaceable. `notify`
and `audit` are callables given by the caller (the applet SDK in the running relay, a list in tests)."""
from __future__ import annotations

import json
import os
from pathlib import Path

TYPES = ("relay.take", "relay.lost", "relay.salvage", "relay.peer", "relay.unplaceable")


class ViewStore:
    """`<state dir>/view-<job>.json`: the last record of every node and task this machine saw, the class it gave
    every node, and the last event key per type and subject. Wall times are only for "how long ago" texts."""

    def __init__(self, path: Path, clock):
        self.path = Path(path)
        self.clock = clock
        self.nodes: dict = {}
        self.tasks: dict = {}
        self.last_event: dict = {}
        self.baseline_done = False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            data = None
        if isinstance(data, dict):
            self.nodes = data.get("nodes") or {}
            self.tasks = data.get("tasks") or {}
            self.last_event = data.get("last_event") or {}
            self.baseline_done = bool(data.get("baseline_done"))

    def put_node(self, node: str, record: dict | None, sha: str | None, cls: str | None) -> None:
        now = self.clock.wall()
        entry = dict(self.nodes.get(node) or {})
        if record is not None and sha != entry.get("sha"):
            entry.update(record=record, sha=sha, last_change_wall=now)
        if cls is not None and cls != entry.get("class"):
            entry.update({"class": cls, "class_since_wall": now})
        entry.setdefault("last_change_wall", None)
        self.nodes[node] = entry

    def put_task(self, task: str, record: dict, sha: str) -> None:
        if sha != (self.tasks.get(task) or {}).get("sha"):
            self.tasks[task] = {"record": record, "sha": sha, "last_change_wall": self.clock.wall()}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name("." + self.path.name + ".tmp")
        tmp.write_text(json.dumps({"nodes": self.nodes, "tasks": self.tasks, "last_event": self.last_event,
                                   "baseline_done": self.baseline_done}, sort_keys=True, indent=1))
        os.replace(tmp, self.path)


def _slot(type_: str, key: str) -> str:
    parts = key.split(":")
    return f"{type_}/{parts[1] if len(parts) > 1 else ''}"


class Events:
    def __init__(self, notify, audit, store: ViewStore, *, settings):
        """`settings`: relay Settings; notices are clipped to `notify_max`, refusal reasons to `reason_max`."""
        self.settings = settings
        self.notify = notify
        self.audit_fn = audit
        self.store = store

    def emit(self, type: str, key: str, *, audit: dict | None = None, notify: str | None = None) -> bool:
        """One event. The same key as the last one of this type and subject -> nothing, False. Before the
        baseline -> the key is recorded, nothing sent, False. Otherwise the key is saved, then the audit line and
        the notice (when `notify`) are sent; True."""
        slot = _slot(type, key)
        if self.store.last_event.get(slot) == key:
            return False
        self.store.last_event[slot] = key
        self.store.save()
        if not self.store.baseline_done:
            return False
        self.audit(type, **dict(audit or {}, key=key))
        if notify:
            try:
                self.notify(notify[:self.settings.notify_max])
            except Exception as exc:                     # a refused notice is audited, never retried
                self.audit("relay.notify_refused", for_event=type, reason=str(exc)[:self.settings.reason_max])
        return True

    def audit(self, event: str, **detail) -> None:
        try:
            self.audit_fn(event, **detail)
        except Exception:
            pass

    def baseline(self) -> None:
        if not self.store.baseline_done:
            self.store.baseline_done = True
            self.store.save()

    def store_node(self, node: str, view, cls: str | None) -> None:
        sha, rec = view if view else (None, None)
        self.store.put_node(node, rec, sha, cls)
        self.store.save()

    def store_task(self, task: str, view) -> None:
        sha, rec = view
        self.store.put_task(task, rec, sha)
        self.store.save()

    def view_node(self, node: str):
        """{record, sha, last_change_wall, class} of the view store, or None."""
        e = self.store.nodes.get(node)
        if not e or "record" not in e:
            return None
        return {"record": e.get("record"), "sha": e.get("sha"), "last_change_wall": e.get("last_change_wall"),
                "class": e.get("class"), "class_since_wall": e.get("class_since_wall")}
