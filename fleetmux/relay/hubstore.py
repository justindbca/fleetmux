"""The flow's store, workspace and liveness on the git state layer (Hub, records, snapshots, materialize,
Observer, Beats). Thin adapters: every git operation is the state layer's own.

HubStore       flow store on `Hub`: one listing per read; records by compare-and-swap (the snapshot ref in the same
               atomic push); node records through `Beats`; salvage refs create-only.
GitWorkspace   worktrees under `<root>/<job>/<seg>-e<epoch>`: `materialize` from the start point, the deploy branch
               the dev_task gate needs, TAKEOVER.md, temp-index snapshots, salvage snapshots, SALVAGE.md.
observer_judge the Observer's classes after each listing.
start_spec     renders the segment's job.spec.yaml from the worktree (launch.render).
"""
import subprocess
from pathlib import Path

from . import jobfile, launch
from . import progress as P
from .hub import Listing  # noqa: F401  (the listing the observer reads)
from .materialize import materialize
from .refs import RefUpdate
from .snapshot import salvage_snapshot, snapshot_tree

UNKNOWN = None


class HubStore:
    def __init__(self, hub, beats):
        self.hub = hub
        self.beats = beats
        self.listing = None
        self.task_views = {}
        self.node_views = {}

    def tasks(self):
        listing = self.hub.list()
        self.listing = listing
        if not listing.ok:
            raise OSError(listing.error or 'state remote unreadable')
        self.task_views = self.hub.tasks(listing)
        self.node_views = self.hub.nodes(listing)
        if self.beats.last is None:
            self.beats.adopt(self.node_views)
        return {t: (v.sha, v.record) for t, v in self.task_views.items()}

    def nodes(self):
        return {n: v.record for n, v in self.node_views.items()}

    def cas_task(self, seg, expected, record, snap=None):
        if expected is None:
            res = self.hub.create_task(record)
        else:
            extra = [RefUpdate(self.hub.names.snap(seg), snap, None)] if snap else []
            timeout = self.hub.settings.snapshot_timeout_s if snap else None
            res = self.hub.cas_task(seg, expected, record, extra, timeout_s=timeout)
        if res.won:
            return True
        return False if res.raced else UNKNOWN

    def put_node(self, node, record):
        self.beats.push(state=record['state'], machine=record.get('machine') or {}, tasks=record.get('tasks') or [],
                        slots=record.get('slots') or [], idle=bool(record.get('idle')), role=record.get('role'),
                        leaving_reason=record.get('leaving_reason'))

    def put_salvage(self, seg, node, epoch, content):
        return self.hub.push_salvage(seg, node, epoch, content).accepted

    def salvages(self, seg):
        out = {}
        for ref, sha in ((self.listing.refs if self.listing and self.listing.ok else {}) or {}).items():
            parsed = self.hub.names.parse(ref)
            if parsed and parsed[0] == 'salvage' and parsed[1] == seg:
                kind, task, node, epoch = parsed
                out['salvage/%s/%s-e%d' % (task, node, int(epoch))] = sha
        return out


def observer_judge(observer, store):
    """judge(nodes, ok) for the flow: the Observer's classes after the store's last listing."""
    def judge(nodes, ok):
        return observer.update(store.listing, store.task_views, store.node_views)
    return judge


class GitWorkspace:
    def __init__(self, hub, *, job, root, state_dir, settings, run):
        self.hub = hub
        self.job = job
        self.root = Path(root)
        self.state_dir = Path(state_dir)
        self.settings = settings
        self.run = run
        self._last = {}

    def path(self, seg, epoch):
        return self.root / self.job / ('%s-e%d' % (seg, int(epoch)))

    def queue_path(self, seg, epoch):
        return self.state_dir / self.job / 'queues' / ('%s-e%d.jsonl' % (seg, int(epoch)))

    def _tmp(self):
        d = self.state_dir / self.job / 'tmp'
        d.mkdir(parents=True, exist_ok=True)
        return d

    def prepare(self, seg, rec, takeover):
        wt = self.path(seg, rec['epoch'])
        placed = materialize(self.hub.git, self.hub.cache, self.hub, job=self.job, rec=rec, worktree=wt,
                             marker_dir=self.state_dir / self.job / 'markers')
        launch.ensure_deploy_branch(wt, launch.deploy_branch_of(self.job, seg),
                                    rec.get('base') or placed.start['commit'], run=self.run)
        if rec.get('merge_in'):
            self._fetch_delivered(wt, rec['merge_in'])
        if takeover:
            write_takeover(wt, rec, settings=self.settings)
        return wt

    def _fetch_delivered(self, wt, shas):
        """The goal tells the worker to `git merge` the other delivered dependencies: fetch the job's deploy
        branches (where each delivery was pushed) so those commits are in the worktree. Raises OSError (the
        segment is then parked with the reason)."""
        spec = 'refs/heads/relay/%s/*:refs/remotes/origin/relay/%s/*' % (self.job, self.job)
        done = self.run(['git', '-C', str(wt), 'fetch', '-q', 'origin', spec], capture_output=True, text=True,
                        timeout=self.settings.hot('git_timeout_s'))
        missing = [sha for sha in shas if self.run(['git', '-C', str(wt), 'cat-file', '-e', sha + '^{commit}'],
                                                   capture_output=True, text=True).returncode != 0]
        if done.returncode != 0 or missing:
            raise OSError('delivered commits to merge are not on the code remote: %s %s' % (
                ' '.join(missing), (done.stderr or '').strip()))

    def snapshot(self, seg, rec, kind):
        wt = self.path(seg, rec['epoch'])
        seq = int((rec.get('checkpoint') or {}).get('seq') or 0) + 1
        snap = snapshot_tree(self.hub.git, self.hub.cache, job=self.job, task=seg, worktree=wt, tmp_dir=self._tmp(),
                             last=self._last.get(seg), seq=seq, secret_globs=self.settings.secret_globs)
        if snap.status in ('error', 'refused_empty'):
            return None
        self._last[seg] = {'tree': snap.tree, 'head': snap.head, 'commit': snap.commit}
        if snap.status == 'unchanged' and kind == 'auto':
            return None
        return {'commit': snap.head, 'snap': snap.commit, 'skipped': list(snap.skipped or [])}

    def publish(self, seg, epoch, branch, sha):
        """Push the delivered commit to `branch` on the code remote, from the segment's worktree (its own repo:
        the cache does not hold the worker's commits). None when pushed, else why not."""
        wt = self.path(seg, epoch)
        try:
            done = self.run(['git', '-C', str(wt), 'push', '-q', 'origin', '%s:refs/heads/%s' % (sha, branch)],
                            capture_output=True, text=True, timeout=self.settings.hot('git_timeout_s'))
        except (OSError, subprocess.TimeoutExpired) as exc:
            return str(exc) or type(exc).__name__
        return None if done.returncode == 0 else (done.stderr or done.stdout or 'git push failed').strip()

    def progress(self, seg, rec):
        return P.summary_for(self.path(seg, rec['epoch']), rec['dir'], text_max=self.settings.text_max)

    def salvage(self, seg, epoch):
        wt = self.path(seg, epoch)
        if not wt.is_dir():
            return None
        snap = salvage_snapshot(self.hub.git, self.hub.cache, job=self.job, task=seg, epoch=epoch, worktree=wt,
                                tmp_dir=self._tmp(), secret_globs=self.settings.secret_globs)
        return snap.commit if snap.status in ('changed', 'unchanged') else None

    def salvage_note(self, seg, epoch, name, content):
        note = self.path(seg, epoch) / jobfile.segment_dir(self.job, seg) / 'notes' / 'SALVAGE.md'
        note.parent.mkdir(parents=True, exist_ok=True)
        ref = 'refs/relay/%s/%s' % (self.job, name)
        with open(note, 'a', encoding='utf-8') as fh:
            fh.write('## %s\n\nThe previous owner pushed everything it had locally to `%s` (commit %s).\n'
                     'Fetch it with `git fetch origin %s` (state remote), compare with this branch and merge what is '
                     'useful. Record in PROGRESS.md what you took and what you left.\n\n' % (name, ref, content, ref))


def write_takeover(worktree, rec, *, settings):
    """TAKEOVER.md (and takeover.json) for the new owner's supervisor, by the state layer's
    takeover.write_package (it reads the previous owner from the record); why it was taken over comes from the
    record's last owner entry."""
    from . import takeover
    owners = rec.get('owners') or []
    why = str((owners[-1] if owners else {}).get('why') or '')
    judged = why.split(':', 1)[1] if why.startswith('takeover:') else ''      # lost | goodbye
    return takeover.write_package(worktree, rec, previous=None, judged=judged, by=rec.get('owner'),
                                  settings=settings)


def start_spec(ws, job, *, relay_python):
    """(seg, record, worktree, takeover) -> the dev_task spec rendered from the worktree's job.spec.yaml."""
    def render(seg, rec, wt, takeover):
        spec = jobfile.load_spec(wt, job, seg)
        return launch.render(spec, job=job, seg=seg, epoch=rec['epoch'], worktree=wt, task_branch=rec['branch'],
                             deploy_branch=launch.deploy_branch_of(job, seg), queue=ws.queue_path(seg, rec['epoch']),
                             relay_python=relay_python, takeover=takeover, merge_in=rec.get('merge_in') or [])
    return render
