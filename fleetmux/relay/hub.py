"""The one hub: read the task table and the node records from the state remote, move a task record by
compare-and-swap, push this machine's own refs.

There is exactly one state remote (`relay.state_remote`, "" = the code remote). Ownership is whatever record that
remote accepted; a refused CAS means someone else wrote first and is never retried with a new record. Code
branches go to the code remote. Every network call is bounded by `git_timeout_s` (or the caller's limit)."""
from __future__ import annotations

from collections import namedtuple
from dataclasses import dataclass
from pathlib import Path

from . import records as R
from .errors import RecordError
from .gitio import Git, GitError, short_error
from .refs import EMPTY, PushResult, RefNames, RefUpdate, cas_push, fetch, ls_remote
from .repo import has_commit

TaskView = namedtuple("TaskView", "sha record")
NodeView = namedtuple("NodeView", "sha record")

_RACED = ("stale", "lock", "nonff", "rejected")
_UNKNOWN = ("timeout", "error")


@dataclass
class Listing:
    ok: bool
    refs: dict           # ref -> sha of `refs/relay/<job>/*` on the state remote ({} when not ok)
    mono: float          # this machine's monotonic time of the listing
    error: str = ""


@dataclass
class CasResult:
    outcome: str                     # ok | stale | lock | nonff | rejected | timeout | error
    record_sha: str | None           # the record commit that was pushed
    push: PushResult | None = None

    @property
    def won(self) -> bool:
        return self.outcome == "ok"

    @property
    def raced(self) -> bool:
        """Someone else wrote first (or the hub refused): give up this record, read again."""
        return self.outcome in _RACED

    @property
    def unknown(self) -> bool:
        """The push may or may not have landed: keep it pending and settle it by sha on the next listing."""
        return self.outcome in _UNKNOWN


class Hub:
    def __init__(self, *, job: str, git: Git, cache: Path, code_remote: str, state_remote: str = "", settings,
                 clock):
        self.job = job
        self.git = git
        self.cache = Path(cache)
        self.code_remote = code_remote
        self.state_remote = state_remote or code_remote
        self.settings = settings
        self.clock = clock
        self.names = RefNames(job)
        self.last: Listing | None = None
        self.bad: dict = {}              # ref name part -> why its record was skipped in the last read

    def _timeout(self, timeout_s=None) -> float:
        return float(self.settings.hot("git_timeout_s") if timeout_s is None else timeout_s)

    def _remote(self, which: str) -> str:
        return self.code_remote if which == "code" else self.state_remote

    # ------------------------------------------------------------------ reading

    def list(self) -> Listing:
        """One listing of the state remote; record objects that are not in the cache yet are fetched."""
        try:
            refs = ls_remote(self.git, self.state_remote, [self.names.all_glob()])
            wanted = []
            for ref, sha in refs.items():
                kind = self.names.parse(ref)
                if kind and kind[0] in ("task", "node") and not has_commit(self.git, self.cache, sha):
                    wanted.append(ref)
            if wanted:
                fetch(self.git, self.cache, self.state_remote, wanted, timeout_s=self._timeout())
        except GitError as exc:
            listing = Listing(False, {}, self.clock.mono(), short_error(exc))
        else:
            # a ref that moved between ls-remote and fetch may point at a commit we do not have: drop it this round
            refs = {r: s for r, s in refs.items() if r not in wanted or has_commit(self.git, self.cache, s)}
            listing = Listing(True, refs, self.clock.mono())
        self.last = listing
        return listing

    def _views(self, listing, kind: str, reader) -> dict:
        listing = listing or self.last
        out = {}
        if listing is None or not listing.ok:
            return out
        for ref, sha in sorted(listing.refs.items()):
            parsed = self.names.parse(ref)
            if not parsed or parsed[0] != kind:
                continue
            try:
                out[parsed[1]] = (sha, reader(self.git, self.cache, sha))
            except RecordError as exc:
                self.bad[parsed[1]] = str(exc)
                continue
            self.bad.pop(parsed[1], None)
        return out

    def tasks(self, listing: Listing | None = None) -> dict:
        """task id -> TaskView of every readable task record."""
        return {t: TaskView(*v) for t, v in self._views(listing, "task", R.read_task).items()}

    def nodes(self, listing: Listing | None = None) -> dict:
        """node -> NodeView of every readable node record."""
        return {n: NodeView(*v) for n, v in self._views(listing, "node", R.read_node).items()}

    # ------------------------------------------------------------------ writing

    def _cas(self, task: str, expected: str, new, extra, timeout_s) -> CasResult:
        if isinstance(new, dict):
            new = R.write_task(self.git, self.cache, new, parent=expected or None)
        updates = [RefUpdate(self.names.task(task), new, expected)] + list(extra or ())
        res = cas_push(self.git, self.cache, self.state_remote, updates, timeout_s=self._timeout(timeout_s))
        return CasResult(res.outcome, new, res)

    def create_task(self, rec: dict, *, timeout_s=None) -> CasResult:
        """A new task record; refused when the task exists."""
        return self._cas(rec["id"], EMPTY, rec, (), timeout_s)

    def cas_task(self, task: str, expected_sha: str, new, extra=(), *, timeout_s=None) -> CasResult:
        """Move `tasks/<task>` from `expected_sha` to `new` (a record dict, written with parent `expected_sha`, or
        an existing record commit sha), atomically with `extra` updates (e.g. the snap ref)."""
        return self._cas(task, expected_sha, new, extra, timeout_s)

    def push_node(self, rec: dict, *, timeout_s=None) -> PushResult:
        """This machine's node record (one writer: forced)."""
        sha = R.write_node(self.git, self.cache, rec)
        return cas_push(self.git, self.cache, self.state_remote, [RefUpdate(self.names.node(rec["node"]), sha, None)],
                        timeout_s=self._timeout(timeout_s))

    def push_salvage(self, task: str, node: str, epoch: int, commit: str, *, timeout_s=None) -> PushResult:
        """What a returning former owner had left; create-only (a second, different value is refused)."""
        return cas_push(self.git, self.cache, self.state_remote,
                        [RefUpdate(self.names.salvage(task, node, epoch), commit, EMPTY)],
                        timeout_s=self._timeout(timeout_s))

    def push_branch(self, branch: str, sha: str, *, timeout_s=None) -> PushResult:
        """A code branch of this machine (one writer per name: forced) to the code remote."""
        return cas_push(self.git, self.cache, self.code_remote, [RefUpdate(f"refs/heads/{branch}", sha, None)],
                        timeout_s=self._timeout(timeout_s))

    def fetch_objects(self, refs: list, *, remote: str = "state", timeout_s=None) -> None:
        """Fetch the objects of `refs` into the cache repo (no local refs written). Raises GitError."""
        fetch(self.git, self.cache, self._remote(remote), list(refs), timeout_s=self._timeout(timeout_s))
