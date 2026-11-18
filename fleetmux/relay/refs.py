"""Relay refs on the hub: names (one state remote), JSON record commits, ls-remote, the CAS push and fetches.

Layout under `refs/relay/<job>/`:

    tasks/<task>                    task.json, moved only by compare-and-swap
    nodes/<node>                    node.json, one writer (that machine)
    snap/<task>                     the owner's last work tree snapshot
    salvage/<task>/<node>-e<epoch>  what a returning former owner had left, pushed once

Code branches live on the code remote: `refs/heads/relay/<job>/<task>/e<epoch>-<node>` and
`refs/heads/relay/<job>/merged`. All pushes and fetches go from the local cache repo by URL (no named remote), so
no remote-tracking ref is ever written."""
from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError
from .gitio import Git, GitError

PREFIX = "refs/relay"
BRANCH_PREFIX = "relay"
EMPTY = ""                       # RefUpdate.expect: the ref must not exist yet
RESERVED = ("merge", "merged", "coordinator")
IDENTITY = {"name": "fleetmux-relay", "email": "relay@localhost"}

_JOB_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_NODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_TASK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")
SALVAGE_PARTS = 3      # salvage/<task>/<node>-e<epoch>
_SALVAGE_RE = re.compile(r"^([a-z0-9][a-z0-9-]{0,39})-e([0-9]+)$")


def check_job(job) -> None:
    if not isinstance(job, str) or not _JOB_RE.match(job) or job.endswith(".lock") or ".." in job:
        raise ConfigError(f"job {job!r} does not match ^[A-Za-z0-9._-]{{1,64}}$")


def check_node(node) -> None:
    if not isinstance(node, str) or not _NODE_RE.match(node):
        raise ConfigError(f"node {node!r} does not match ^[a-z0-9][a-z0-9-]{{0,39}}$")


def check_task_id(task, *, reserved_ok: bool = False) -> None:
    """A task id; `merged` is never one, `merge` and `coordinator` only for the special records (reserved_ok)."""
    if not isinstance(task, str) or not _TASK_RE.match(task) or task.endswith(".lock") or ".." in task:
        raise ConfigError(f"task id {task!r} does not match ^[A-Za-z0-9][A-Za-z0-9._-]{{0,47}}$")
    if task == "merged" or (task in RESERVED and not reserved_ok):
        raise ConfigError(f"task id {task!r} is reserved")


def is_task_id(task) -> bool:
    try:
        check_task_id(task, reserved_ok=True)
    except ConfigError:
        return False
    return True


class RefNames:
    """Ref names of one job."""

    def __init__(self, job: str):
        check_job(job)
        self.job = job
        self.base = f"{PREFIX}/{job}"

    def task(self, task: str) -> str:
        return f"{self.base}/tasks/{task}"

    def node(self, node: str) -> str:
        return f"{self.base}/nodes/{node}"

    def snap(self, task: str) -> str:
        return f"{self.base}/snap/{task}"

    def salvage(self, task: str, node: str, epoch: int) -> str:
        return f"{self.base}/salvage/{task}/{node}-e{int(epoch)}"

    def all_glob(self) -> str:
        return f"{self.base}/*"

    def parse(self, ref: str):
        """("task", t) | ("node", n) | ("snap", t) | ("salvage", t, n, epoch) | None."""
        head = self.base + "/"
        if not ref.startswith(head):
            return None
        parts = ref[len(head):].split("/")
        if len(parts) == 2 and parts[0] in ("tasks", "snap") and is_task_id(parts[1]):
            return ("task" if parts[0] == "tasks" else "snap", parts[1])
        if len(parts) == 2 and parts[0] == "nodes" and _NODE_RE.match(parts[1]):
            return ("node", parts[1])
        if len(parts) == SALVAGE_PARTS and parts[0] == "salvage" and is_task_id(parts[1]):
            m = _SALVAGE_RE.match(parts[2])
            if m:
                return ("salvage", parts[1], m.group(1), int(m.group(2)))
        return None


def task_branch(job: str, task: str, epoch: int, node: str) -> str:
    """Short branch name `relay/<job>/<task>/e<epoch>-<node>`; the ref is `refs/heads/<name>`."""
    return f"{BRANCH_PREFIX}/{job}/{task}/e{int(epoch)}-{node}"


def merged_branch(job: str) -> str:
    return f"{BRANCH_PREFIX}/{job}/merged"


def branch_glob(job: str) -> str:
    return f"refs/heads/{BRANCH_PREFIX}/{job}/*"


# ----------------------------------------------------------------------------- record commits


def identity_env(identity: dict | None = None) -> dict:
    who = identity or IDENTITY
    return {"GIT_AUTHOR_NAME": who["name"], "GIT_AUTHOR_EMAIL": who["email"],
            "GIT_COMMITTER_NAME": who["name"], "GIT_COMMITTER_EMAIL": who["email"]}


def write_json_commit(git: Git, cache: Path, filename: str, data: dict, *, parents: list, message: str) -> str:
    """A commit in the cache repo whose tree holds one file `filename` with `data` as canonical JSON."""
    gc = git.bare_cache(cache)
    body = (json.dumps(data, sort_keys=True, indent=1, ensure_ascii=True) + "\n").encode()
    blob = gc.out("hash-object", "-w", "--stdin", input=body)
    tree = gc.out("mktree", input=f"100644 blob {blob}\t{filename}\n".encode())
    args = ["commit-tree", tree]
    for p in parents:
        args += ["-p", p]
    args += ["-m", message]
    return gc.out(*args, env=identity_env())


def read_json_commit(git: Git, cache: Path, commit: str, filename: str) -> dict:
    raw = git.bare_cache(cache).run("cat-file", "blob", f"{commit}:{filename}").stdout
    return json.loads(raw.decode("utf-8"))


# ----------------------------------------------------------------------------- ls-remote


def ls_remote(git: Git, remote: str, patterns: list) -> dict:
    """ref -> sha for the refs (exact names, or globs with `*`) that exist on the remote; one call = one consistent
    listing. Raises GitError when the remote cannot be read (a failed read is never "unchanged")."""
    out = git.out("ls-remote", "--", remote, *patterns)
    found = {}
    for line in out.splitlines():
        if "\t" not in line:
            continue
        sha, name = line.split("\t", 1)
        if any(name == p or ("*" in p and fnmatch.fnmatchcase(name, p)) for p in patterns):
            found[name] = sha
    return found


# ----------------------------------------------------------------------------- CAS push


@dataclass(frozen=True)
class RefUpdate:
    ref: str
    new: str
    expect: str | None   # None = forced update without a lease; EMPTY = must not exist; else the old sha


@dataclass
class PushResult:
    accepted: bool
    outcome: str                      # ok | stale | lock | nonff | rejected | timeout | error
    refs: dict = field(default_factory=dict)   # ref -> porcelain flag (' ', '+', '*', '=', '!')
    raw: str = ""


def classify(text: str) -> str:
    low = text.lower()
    if "stale info" in low:
        return "stale"
    if "cannot lock ref" in low or "but expected" in low or "atomic transaction failed" in low:
        return "lock"
    if "non-fast-forward" in low or "fetch first" in low:
        return "nonff"
    return "rejected"


def cas_push(git: Git, cache: Path, remote: str, updates: list, *, timeout_s: float) -> PushResult:
    """One `git push --atomic --porcelain` from the cache repo by URL. Every update with `expect` set carries
    `--force-with-lease=<ref>:<expect>`; the others are forced. All refs move or none.

    Outcomes: `ok`; `stale` (the advertised value differs from the lease); `lock` (the server's ref lock found
    another old value: a concurrent winner); `nonff`; `rejected` (any other refusal: hook, disk); `timeout` (killed,
    outcome unknown); `error` (no answer from the remote, nothing pushed)."""
    if not updates:
        return PushResult(True, "ok", {}, "")
    args = ["push", "--atomic", "--porcelain", "--no-verify"]
    specs = []
    for u in updates:
        if u.expect is None:
            specs.append(f"+{u.new}:{u.ref}")
        else:
            args.append(f"--force-with-lease={u.ref}:{u.expect}")
            specs.append(f"{u.new}:{u.ref}")
    try:
        proc = git.bare_cache(cache).run(*args, "--", remote, *specs, timeout_s=timeout_s, check=False)
    except GitError as exc:
        raw = (exc.stdout + exc.stderr).decode("utf-8", "replace")
        return PushResult(False, "timeout" if exc.timed_out else "error", {}, raw)
    stdout = proc.stdout.decode("utf-8", "replace")
    stderr = proc.stderr.decode("utf-8", "replace")
    raw = stdout + stderr
    flags, rejected = {}, []
    for line in stdout.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and len(parts[0]) == 1 and ":" in parts[1]:
            dst = parts[1].split(":", 1)[1]
            flags[dst] = parts[0]
            if parts[0] == "!":
                rejected.append(line)
    if proc.returncode == 0 and not rejected and len(flags) == len(updates):
        return PushResult(True, "ok", flags, raw)
    if not flags:
        return PushResult(False, "error", flags, raw)
    # In an atomic push the refs that were fine are reported "atomic push failed"; the cause is on another line.
    causes = [line for line in rejected if "atomic push failed" not in line] or rejected
    return PushResult(False, classify("\n".join(causes) + "\n" + stderr), flags, raw)


# ----------------------------------------------------------------------------- fetch


def fetch(git: Git, cache: Path, remote: str, refspecs: list, *, timeout_s: float) -> None:
    """Fetch `refspecs` (`<remote ref>:<local ref>`, or a bare remote ref for objects only) by URL into the cache
    repo. No tags, no FETCH_HEAD, no remote-tracking refs. Raises GitError."""
    git.bare_cache(cache).run("fetch", "--no-tags", "--no-write-fetch-head", "--quiet", "--", remote, *refspecs,
                              timeout_s=timeout_s)
