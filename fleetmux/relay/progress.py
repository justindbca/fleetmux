"""A slot's progress file `.relay/<job>/<task>/notes/<slot>/PROGRESS.md`: five fixed level-2 headings in order,
each a bullet list (`- none` when empty); red-test bullets are `` - `<test id>`: <one line> ``. A malformed file
never blocks a checkpoint: the summary says `ok: false`."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

SECTIONS = ("Done", "Doing", "Next", "Dead ends", "Red tests")
CLIP_MARK = "-"
_RED_RE = re.compile(r"^-\s+`([^`]+)`\s*:?\s*(.*)$")
_HEAD_RE = re.compile(r"^##\s+(.+?)\s*$")


def template() -> str:
    return "".join(f"## {s}\n\n- none\n\n" for s in SECTIONS).rstrip("\n") + "\n"


@dataclass
class ParseResult:
    sections: dict                       # heading -> bullet texts (without "- "; "none" dropped)
    ok: bool
    red_tests: int
    red_test_items: list = field(default_factory=list)   # [(test id, line)]
    problems: list = field(default_factory=list)


def parse(text: str) -> ParseResult:
    found, order, problems = {}, [], []
    current = None
    for line in text.splitlines():
        m = _HEAD_RE.match(line)
        if m and not line.startswith("###"):
            head = m.group(1)
            if head in found:
                problems.append(f"duplicate heading {head!r}")
            elif head in SECTIONS:
                found[head] = []
                order.append(head)
            current = head if head in SECTIONS else None
            continue
        if current is not None and line.strip().startswith("- "):
            item = line.strip()[2:].strip()
            if item.lower() != "none":
                found[current].append(item)
    missing = [s for s in SECTIONS if s not in found]
    if missing:
        problems.append("missing heading(s): " + ", ".join(missing))
    if [s for s in SECTIONS if s in order] != order:
        problems.append("headings out of order")
    red = []
    for item in found.get(SECTIONS[-1], []):
        m = _RED_RE.match("- " + item)
        red.append((m.group(1), m.group(2)) if m else (item, ""))
    return ParseResult({s: found.get(s, []) for s in SECTIONS}, not problems, len(red), red, problems)


def clip(text, limit: int) -> str:
    """ASCII text cut to `limit` characters (the last one is CLIP_MARK when cut)."""
    text = str(text).encode("ascii", "replace").decode("ascii")
    return text if len(text) <= limit else text[: max(0, limit - len(CLIP_MARK))] + CLIP_MARK


def summary(path: str, parsed: ParseResult, *, text_max: int) -> dict:
    """The task.json `progress` value."""
    return {"path": path, "ok": parsed.ok, "done": len(parsed.sections["Done"]),
            "doing": [clip(x, text_max) for x in parsed.sections["Doing"]],
            "next": [clip(x, text_max) for x in parsed.sections["Next"]],
            "red_tests": [clip(t, text_max) for t, _ in parsed.red_test_items]}


def find(worktree: Path, task_dir: str) -> str | None:
    """The newest `<task_dir>/notes/<slot>/PROGRESS.md` in the work tree (repo-relative), or None."""
    root = Path(worktree)
    files = [p for p in (root / task_dir / "notes").glob("*/PROGRESS.md") if p.is_file()]
    if not files:
        return None
    newest = max(files, key=lambda p: (p.stat().st_mtime_ns, str(p)))
    return newest.relative_to(root).as_posix()


def summary_for(worktree: Path, task_dir: str, *, text_max: int) -> dict | None:
    rel = find(worktree, task_dir)
    if rel is None:
        return None
    text = (Path(worktree) / rel).read_text(encoding="utf-8", errors="replace")
    return summary(rel, parse(text), text_max=text_max)
