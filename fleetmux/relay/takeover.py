"""The takeover package: `TAKEOVER.md` and `takeover.json` in the task's directory (`<work tree>/<task dir>/`),
written by the new owner's relay after it built the work tree, for the supervisor of the new dev sub-fleet. It
says who had the task, why it moved, which checkpoint the tree starts from, the previous branch, and quotes the
newest progress file's five sections. Pure file writes: no git, no network."""
from __future__ import annotations

import json
import os
from pathlib import Path

from . import progress as P
from . import records as R
from .liveness import describe_tasks
from .refs import RefNames, task_branch

MD_NAME = "TAKEOVER.md"
JSON_NAME = "takeover.json"
SHORT = 12


def _short(sha) -> str:
    return str(sha)[:SHORT] if sha else "none"


def package_data(rec: dict, *, previous: dict | None, judged: str, by: str, progress_path: str | None) -> dict:
    prev_owner = R.previous_owner(rec) or {}
    why = (rec.get("owners") or [{}])[-1].get("why")
    prev_branch = (task_branch(rec["job"], rec["id"], prev_owner["epoch"], prev_owner["node"])
                   if prev_owner.get("node") else None)
    salvage = (RefNames(rec["job"]).salvage(rec["id"], prev_owner["node"], prev_owner["epoch"])
               if prev_owner.get("node") else None)
    return {"task": rec["id"], "title": rec.get("title"), "weight": rec.get("weight"), "needs": rec.get("needs"),
            "dir": rec.get("dir"), "epoch": rec.get("epoch"), "branch": rec.get("branch"), "why": why, "by": by,
            "checkpoint": rec.get("checkpoint"), "start": R.start_point(rec), "merge_in": rec.get("merge_in"),
            "previous": {"node": prev_owner.get("node"), "epoch": prev_owner.get("epoch"), "branch": prev_branch,
                         "judged": judged, "salvage_ref": salvage,
                         "last_state": (previous or {}).get("state"),
                         "last_tasks": (previous or {}).get("tasks") or []},
            "progress_file": progress_path}


def render(data: dict, parsed, settings) -> str:
    limit = int(settings.takeover_md_max_lines)
    cp = data.get("checkpoint") or {}
    prev = data["previous"]
    head = [
        f"# Takeover of task {data['task']}: {data['title']}",
        "",
        f"- Weight {data['weight']}; needs {', '.join(data.get('needs') or []) or 'nothing'}; "
        f"brief {data['dir']}/brief.md.",
        f"- New owner {data['by']}, epoch {data['epoch']}, branch {data['branch']} ({data['why']}).",
        f"- Previous owner {prev['node'] or 'none'} (epoch {prev['epoch']}, branch {prev['branch'] or 'none'}): "
        f"{prev['judged']}. Its last record: state {prev['last_state'] or '?'}, tasks "
        f"{describe_tasks({'tasks': prev['last_tasks']})}.",
        f"- The tree starts from {data['start']['source']} {_short(data['start']['commit'])}"
        + (f" (checkpoint seq {cp.get('seq')}, {cp.get('kind')}" + (f", phase {cp['phase']}" if cp.get("phase")
                                                                    else "") + ")" if cp else "") + ".",
    ]
    if data.get("merge_in"):
        head.append(f"- Merge these commits first: {', '.join(_short(c) for c in data['merge_in'])}.")
    if prev.get("salvage_ref"):
        head.append(f"- If {prev['node']} comes back, its unsaved work arrives as {prev['salvage_ref']}; compare it "
                    f"and keep what is useful.")
    head.append("")
    body = []
    path = data.get("progress_file")
    if parsed is None:
        body.append("No progress file in this checkpoint: start from the brief.")
    elif not parsed.ok:
        body.append(f"The progress file {path} did not parse ({'; '.join(parsed.problems)}): read it directly.")
    else:
        body.append(f"## Progress at this checkpoint (quoted from {path})")
        for name in P.SECTIONS:
            body.append("")
            body.append(f"### {name}")
            for item in parsed.sections.get(name) or ["none"]:
                body.append(f"- {item}")
    lines = head + body
    if len(lines) > limit:
        lines = lines[: max(0, limit - 1)] + [f"(cut to {limit} lines; the full text is in {path})"]
    text = "\n".join(lines) + "\n"
    return text.encode("ascii", "replace").decode("ascii")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_package(worktree: Path, rec: dict, *, previous: dict | None, judged: str, by: str, settings) -> Path:
    """Write the package into `<worktree>/<rec.dir>/`; returns the TAKEOVER.md path."""
    worktree = Path(worktree)
    rel = P.find(worktree, rec["dir"])
    parsed = P.parse((worktree / rel).read_text(encoding="utf-8", errors="replace")) if rel else None
    data = package_data(rec, previous=previous, judged=judged, by=by, progress_path=rel)
    target = worktree / rec["dir"]
    _write(target / JSON_NAME, json.dumps(data, sort_keys=True, indent=1) + "\n")
    md = target / MD_NAME
    _write(md, render(data, parsed, settings))
    return md
