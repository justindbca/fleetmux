"""The two relay records and every change of a task record.

`task.json` (ref `tasks/<task>`): one per task, changed only by compare-and-swap. Every change returns a new dict
with `seq` + 1 and a fresh `nonce`, so two writers never produce the same commit and an unknown push outcome can be
settled by sha. `checkpoint` stays across owner changes: it is where the next owner starts until that owner makes
its own checkpoint.

`node.json` (ref `nodes/<node>`): one per machine, written only by it; `beat` grows by one on every write."""
from __future__ import annotations

import copy
import secrets
from pathlib import Path

from .errors import RecordError, TransitionError
from .gitio import Git, GitError
from .refs import check_node, check_task_id, read_json_commit, task_branch, write_json_commit

VERSION = 1
NONCE_BYTES = 8
TASK_FILE = "task.json"
NODE_FILE = "node.json"

BLOCKED, PENDING, WORKING, HANDED_OVER, RECLAIMED, DELIVERED, PARKED = (
    "blocked", "pending", "working", "handed_over", "reclaimed", "delivered", "parked")
TASK_STATES = (BLOCKED, PENDING, WORKING, HANDED_OVER, RECLAIMED, DELIVERED, PARKED)
HEAVY, LIGHT = "heavy", "light"
WEIGHTS = (HEAVY, LIGHT)
WHY_CLAIM, WHY_LOST, WHY_GOODBYE = "claim", "takeover:lost", "takeover:goodbye"
CHECKPOINT_KINDS = ("phase", "auto")

ONLINE, LEAVING, REJOINING = "online", "leaving", "rejoining"
NODE_STATES = (ONLINE, LEAVING, REJOINING)
LEAVING_REASONS = ("shutdown", "sleep", "operator")

TASK_FIELDS = ("v", "job", "id", "title", "depends_on", "weight", "needs", "prefer", "dir", "base", "merge_in",
               "state", "owner", "epoch", "branch", "checkpoint", "progress", "owners", "delivered", "parked",
               "seq", "nonce")
NODE_FIELDS = ("v", "job", "node", "beat", "state", "leaving_reason", "tasks", "slots", "machine", "fleet", "idle",
               "role", "nonce")


def nonce() -> str:
    return secrets.token_hex(NONCE_BYTES)


def _next(prev: dict, **changes) -> dict:
    rec = copy.deepcopy(prev)
    rec.update(copy.deepcopy(changes))
    rec["seq"] = int(prev.get("seq", 0)) + 1
    rec["nonce"] = nonce()
    return rec


def _need(prev: dict, states: tuple, what: str) -> None:
    if prev.get("state") not in states:
        raise TransitionError(f"task {prev.get('id')}: cannot {what} from state {prev.get('state')!r} "
                              f"(needs {' / '.join(states)})")


# ----------------------------------------------------------------------------- task.json


def new_task(*, job: str, id: str, title: str, weight: str, dir: str, needs=(), depends_on=(), prefer=None,
             base: str | None = None, reserved_ok: bool = False) -> dict:
    """A new task record at epoch 0, seq 0: `blocked` when it depends on other tasks, else `pending`."""
    check_task_id(id, reserved_ok=reserved_ok)
    if weight not in WEIGHTS:
        raise RecordError(f"task {id}: weight must be one of {', '.join(WEIGHTS)}, got {weight!r}")
    deps = [str(d) for d in depends_on or ()]
    for d in deps:
        check_task_id(d, reserved_ok=True)
    if id in deps:
        raise RecordError(f"task {id} depends on itself")
    if not all(isinstance(n, str) and n.strip() for n in needs or ()):
        raise RecordError(f"task {id}: needs must be a list of non-empty strings, got {list(needs or ())!r}")
    return {"v": VERSION, "job": job, "id": id, "title": title, "depends_on": deps, "weight": weight,
            "needs": [str(n) for n in needs or ()], "prefer": prefer, "dir": dir, "base": base, "merge_in": [],
            "state": BLOCKED if deps else PENDING, "owner": None, "epoch": 0, "branch": None, "checkpoint": None,
            "progress": None, "owners": [], "delivered": None, "parked": None, "seq": 0, "nonce": nonce()}


def unblock(prev: dict, *, base: str, merge_in=()) -> dict:
    """blocked -> pending once every dependency is delivered: start from `base`, merge `merge_in` first."""
    _need(prev, (BLOCKED,), "unblock")
    return _next(prev, state=PENDING, base=base, merge_in=list(merge_in or ()))


def claim(prev: dict, *, me: str) -> dict:
    """pending / reclaimed -> working for `me`: epoch + 1, a new branch, `owners` + claim."""
    _need(prev, (PENDING, RECLAIMED), "claim")
    check_node(me)
    epoch = int(prev["epoch"]) + 1
    return _next(prev, state=WORKING, owner=me, epoch=epoch, branch=task_branch(prev["job"], prev["id"], epoch, me),
                 owners=list(prev.get("owners") or []) + [{"node": me, "epoch": epoch, "why": WHY_CLAIM}])


def takeover(prev: dict, *, to: str | None, why: str) -> dict:
    """working / handed_over of a machine that left or went silent -> working for `to` (epoch + 1, new branch,
    `owners` + why), or -> reclaimed without owner when no machine can take it (`to` None)."""
    _need(prev, (WORKING, HANDED_OVER), "take over")
    if why not in (WHY_LOST, WHY_GOODBYE):
        raise TransitionError(f"takeover why must be {WHY_LOST} or {WHY_GOODBYE}, got {why!r}")
    epoch = int(prev["epoch"]) + 1
    if to is None:
        return _next(prev, state=RECLAIMED, owner=None, epoch=epoch, branch=None)
    check_node(to)
    return _next(prev, state=WORKING, owner=to, epoch=epoch, branch=task_branch(prev["job"], prev["id"], epoch, to),
                 owners=list(prev.get("owners") or []) + [{"node": to, "epoch": epoch, "why": why}])


def hand_over(prev: dict) -> dict:
    """working -> handed_over (the owner leaves; others may take it at once)."""
    _need(prev, (WORKING,), "hand over")
    return _next(prev, state=HANDED_OVER)


def resume(prev: dict, *, me: str) -> dict:
    """handed_over -> working again for the same owner (it came back before anyone took the task): same owner,
    epoch and branch."""
    _need(prev, (HANDED_OVER,), "resume")
    if prev.get("owner") != me:
        raise TransitionError(f"task {prev.get('id')}: only its owner {prev.get('owner')!r} can resume it, not {me!r}")
    return _next(prev, state=WORKING)


def checkpoint(prev: dict, *, commit: str | None, snap: str | None, kind: str, phase: str | None = None,
               progress: dict | None = None) -> dict:
    """A checkpoint of the owner: `commit` = the worker's HEAD, `snap` = the snapshot commit (may be None)."""
    _need(prev, (WORKING,), "checkpoint")
    if kind not in CHECKPOINT_KINDS:
        raise TransitionError(f"checkpoint kind must be one of {', '.join(CHECKPOINT_KINDS)}, got {kind!r}")
    if not commit and not snap:
        raise TransitionError("a checkpoint needs a commit or a snapshot")
    seq = int((prev.get("checkpoint") or {}).get("seq") or 0) + 1
    cp = {"commit": commit, "snap": snap, "kind": kind, "phase": phase, "seq": seq, "epoch": prev["epoch"]}
    return _next(prev, checkpoint=cp, progress=copy.deepcopy(progress) if progress is not None
                 else prev.get("progress"))


def deliver(prev: dict, *, branch: str, commit: str, gate=None, review=None) -> dict:
    _need(prev, (WORKING,), "deliver")
    return _next(prev, state=DELIVERED, delivered={"branch": branch, "commit": commit, "gate": gate,
                                                   "review": review})


def park(prev: dict, *, reason: str, by: str) -> dict:
    _need(prev, (WORKING, PENDING, RECLAIMED, HANDED_OVER), "park")
    return _next(prev, state=PARKED, parked={"reason": reason, "by": by})


def claimable(rec: dict) -> bool:
    return (rec or {}).get("state") in (PENDING, RECLAIMED)


def start_point(rec: dict) -> dict:
    """Where the next owner starts: the last checkpoint's snapshot, else its commit, else the task's base."""
    cp = rec.get("checkpoint") or {}
    if cp.get("snap"):
        return {"commit": cp["snap"], "source": "snap"}
    if cp.get("commit"):
        return {"commit": cp["commit"], "source": "commit"}
    if rec.get("base"):
        return {"commit": rec["base"], "source": "base"}
    raise TransitionError(f"task {rec.get('id')} has no checkpoint and no base")


def previous_owner(rec: dict) -> dict | None:
    """The `owners` entry before the current epoch's, or None."""
    entries = [o for o in rec.get("owners") or [] if o.get("epoch") != rec.get("epoch")]
    return entries[-1] if entries else None


def decode_task(data) -> dict:
    if not isinstance(data, dict):
        raise RecordError("task record is not an object")
    missing = [f for f in TASK_FIELDS if f not in data]
    if missing:
        raise RecordError(f"task record lacks {', '.join(missing)}")
    if data["state"] not in TASK_STATES:
        raise RecordError(f"task record has unknown state {data['state']!r}")
    if data["weight"] not in WEIGHTS:
        raise RecordError(f"task record has unknown weight {data['weight']!r}")
    for f in ("epoch", "seq"):
        if not isinstance(data[f], int) or isinstance(data[f], bool):
            raise RecordError(f"task record {f} must be an integer")
    for f in ("depends_on", "needs", "merge_in", "owners"):
        if not isinstance(data[f], list):
            raise RecordError(f"task record {f} must be a list")
    if data["state"] in (WORKING, HANDED_OVER) and not data["owner"]:
        raise RecordError(f"{data['state']} task record has no owner")
    return data


def write_task(git: Git, cache: Path, rec: dict, *, parent: str | None) -> str:
    """A record commit holding `task.json`, parent = the previous record commit (None for a new task)."""
    decode_task(rec)
    return write_json_commit(git, cache, TASK_FILE, rec, parents=[parent] if parent else [],
                             message=f"relay task {rec['job']}/{rec['id']} {rec['state']} e{rec['epoch']} "
                                     f"seq {rec['seq']}")


def read_task(git: Git, cache: Path, sha: str) -> dict:
    return decode_task(_read(git, cache, sha, TASK_FILE, "task"))


# ----------------------------------------------------------------------------- node.json


def first_state(existing: dict | None) -> str:
    """The state of a machine's first record in this process: online for a fresh join (no node.json yet),
    rejoining when this node's node.json already exists (it comes back and must settle first)."""
    return REJOINING if existing else ONLINE


def new_node(*, job: str, node: str, machine: dict, state: str, fleet: dict | None = None) -> dict:
    """A machine's first record: beat 0, `state` from the caller (online | rejoining; see first_state)."""
    check_node(node)
    if state not in (ONLINE, REJOINING):
        raise RecordError(f"node {node}: a first record is online or rejoining, got {state!r}")
    return {"v": VERSION, "job": job, "node": node, "beat": 0, "state": state, "leaving_reason": None,
            "tasks": [], "slots": [], "machine": copy.deepcopy(machine), "fleet": copy.deepcopy(fleet or {}),
            "idle": True, "role": None, "nonce": nonce()}


def next_node(prev: dict, **changes) -> dict:
    """The next record of the same machine: beat + 1, a fresh nonce, `changes` applied."""
    rec = copy.deepcopy(prev)
    rec.update(copy.deepcopy(changes))
    rec["beat"] = int(prev.get("beat", 0)) + 1
    rec["nonce"] = nonce()
    if rec["state"] != LEAVING:
        rec["leaving_reason"] = None
    decode_node(rec)
    return rec


def decode_node(data) -> dict:
    if not isinstance(data, dict):
        raise RecordError("node record is not an object")
    missing = [f for f in NODE_FIELDS if f not in data]
    if missing:
        raise RecordError(f"node record lacks {', '.join(missing)}")
    if data["state"] not in NODE_STATES:
        raise RecordError(f"node record has unknown state {data['state']!r}")
    if data["state"] == LEAVING and data["leaving_reason"] not in LEAVING_REASONS:
        raise RecordError(f"leaving node record needs a reason in {', '.join(LEAVING_REASONS)}")
    if not isinstance(data["beat"], int) or isinstance(data["beat"], bool):
        raise RecordError("node record beat must be an integer")
    return data


def write_node(git: Git, cache: Path, rec: dict) -> str:
    """An orphan commit holding `node.json` (every write differs: beat and nonce)."""
    decode_node(rec)
    return write_json_commit(git, cache, NODE_FILE, rec, parents=[],
                             message=f"relay node {rec['job']}/{rec['node']} {rec['state']} beat {rec['beat']}")


def read_node(git: Git, cache: Path, sha: str) -> dict:
    return decode_node(_read(git, cache, sha, NODE_FILE, "node"))


def _read(git: Git, cache: Path, sha: str, filename: str, kind: str):
    try:
        return read_json_commit(git, cache, sha, filename)
    except (ValueError, UnicodeDecodeError) as exc:
        raise RecordError(f"{kind} record {sha[:NONCE_BYTES]} is not JSON: {exc}") from exc
    except GitError as exc:
        raise RecordError(f"{kind} record {sha[:NONCE_BYTES]} not readable: {exc}") from exc
