"""One checkpoint of an owned task: snapshot the work tree, then move the task record (new `checkpoint` and
`progress`) and the `snap/<task>` ref in one atomic push. A refused push means the task has another owner now:
the caller goes to the return flow, nothing more is written."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import progress as P
from . import records as R
from .refs import RefUpdate
from .snapshot import SnapResult, snapshot_tree


@dataclass
class CheckpointResult:
    outcome: str                      # ok | unchanged | lost | refused | unknown
    record_sha: str | None = None
    record: dict | None = None
    snap: SnapResult | None = None
    last: dict | None = None          # {tree, head, commit} to pass as `last` next time
    reason: str = ""


def take_checkpoint(hub, *, task: str, view, me: str, worktree: Path, tmp_dir: Path, last: dict | None, settings,
                    kind: str = "auto", phase: str | None = None, progress: dict | None = None) -> CheckpointResult:
    """`view` = the task's TaskView as this machine last saw it (its sha is the CAS lease)."""
    rec = view.record
    if rec.get("owner") != me or rec.get("state") != R.WORKING:
        return CheckpointResult("lost", reason="the record no longer names this machine as working owner")
    seq = int((rec.get("checkpoint") or {}).get("seq") or 0) + 1
    snap = snapshot_tree(hub.git, hub.cache, job=hub.job, task=task, worktree=worktree, tmp_dir=tmp_dir, last=last,
                         seq=seq, secret_globs=settings.secret_globs)
    if snap.status in ("error", "refused_empty"):
        return CheckpointResult("refused", snap=snap, reason=f"snapshot {snap.status}: {snap.reason}")
    now = {"tree": snap.tree, "head": snap.head, "commit": snap.commit}
    if snap.status == "unchanged" and kind == "auto":
        return CheckpointResult("unchanged", snap=snap, last=now)
    if progress is None:
        progress = P.summary_for(worktree, rec["dir"], text_max=settings.text_max)
    new = R.checkpoint(rec, commit=snap.head, snap=snap.commit, kind=kind, phase=phase, progress=progress)
    res = hub.cas_task(task, view.sha, new, [RefUpdate(hub.names.snap(task), snap.commit, None)],
                       timeout_s=settings.snapshot_timeout_s)
    if res.won:
        return CheckpointResult("ok", res.record_sha, new, snap, now)
    if res.raced:
        return CheckpointResult("lost", res.record_sha, new, snap, reason=f"push {res.outcome}")
    return CheckpointResult("unknown", res.record_sha, new, snap, reason=f"push {res.outcome}")
