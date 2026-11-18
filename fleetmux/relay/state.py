"""This machine's relay state file `<state dir>/relay-<job>.json`: replaced atomically and saved before the side
effect it guards (a CAS push whose outcome may be unknown is saved as `cas_pending` first)."""
from __future__ import annotations

import json
import os
from pathlib import Path


class StateFile:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict:
        """The saved state; {} when the file does not exist. ValueError when it is not a JSON object."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError(f"state file {self.path} does not hold a JSON object")
        return data

    def save(self, data: dict) -> None:
        """Write to a temp file in the same directory, fsync it, and replace the state file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name("." + self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, sort_keys=True, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        try:
            fd = os.open(self.path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)


class RelayState:
    """Keys: tasks {task: {...}} (flow-1's per-task facts), cas_pending {task: [record_sha, expected_sha]}."""

    def __init__(self, path: Path):
        self.file = StateFile(path)
        self.data = self.file.load()
        self.data.setdefault("tasks", {})
        self.data.setdefault("cas_pending", {})

    def save(self) -> None:
        self.file.save(self.data)

    def task(self, task: str) -> dict:
        return self.data["tasks"].setdefault(task, {})

    def save_cas_pending(self, task: str, record_sha: str, expected_sha: str) -> None:
        self.data["cas_pending"][task] = [record_sha, expected_sha]
        self.save()

    def clear_cas_pending(self, task: str) -> None:
        if self.data["cas_pending"].pop(task, None) is not None:
            self.save()

    def cas_pending(self) -> dict:
        return {t: (v[0], v[1]) for t, v in self.data["cas_pending"].items()}
