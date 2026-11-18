"""Start and stop a segment's development sub-fleet from the job files in the repo.

`render` turns a segment's `job.spec.yaml` (relative paths only) into this machine's dev_task job spec: it fills
the worktree, the branches, the queue file and the interpreter the gate runs with. An `Executor` hands that spec to
the machine's root fleet. `FleetmuxExecutor` uses the existing command line: `start` (attach to the live root), then
`task -`, and `stop`. It spawns nothing unless a runner is passed in.
"""
import json
import re
from pathlib import Path

from . import jobfile

MERGE = jobfile.MERGE


CLIP = 300                      # characters of fleetmux output quoted in an error


class ExecutorError(RuntimeError):
    """The root fleet did not take or stop the segment's sub-fleet."""


def deploy_branch_of(job, seg):
    """The branch dev_task fast-forwards on delivery: per segment, or the job's merged branch for `merge`."""
    if seg == MERGE:
        return 'relay/' + job + '/merged'
    return 'relay/' + job + '/' + seg + '/delivered'


NAME_RE = re.compile(r'^relay-(?P<job>.+)-(?P<seg>[^-]+)-e(?P<epoch>[0-9]+)$')


def sub_fleet_name(job, seg, epoch):
    """`relay-<job>-<seg>-e<epoch>`: the executor finds a segment's running sub-fleet by this name."""
    return 'relay-%s-%s-e%d' % (job, seg, int(epoch))


def parse_name(name):
    """(job, seg, epoch) of a sub-fleet name, or None."""
    m = NAME_RE.match(name or '')
    return (m.group('job'), m.group('seg'), int(m.group('epoch'))) if m else None


def render(spec, *, job, seg, epoch, worktree, task_branch, deploy_branch, queue, relay_python, takeover=False,
           merge_in=()):
    """The dev_task job spec for this machine (keys of fleetmux's job spec only)."""
    seg_dir = jobfile.segment_dir(job, seg)
    lead = []
    if takeover:
        lead.append('First read ' + seg_dir + '/TAKEOVER.md and continue from its Next step.')
    if merge_in:
        lead.append('Then run: git merge ' + ' '.join(merge_in) + ' (the other delivered dependencies).')
    tail = ('Every slot keeps its PROGRESS.md (Done / Doing / Next / Dead ends / Red tests) at ' + seg_dir +
            '/notes/<slot>/PROGRESS.md in this worktree and commits it: checkpoints and takeovers read the newest.')
    goal = ' '.join(lead + [spec['goal'].strip(), tail])
    d = spec['deliver']
    deliver = {'repo': str(worktree), 'feature_branch': task_branch, 'deploy_branch': deploy_branch,
               'test_cmd': ['/usr/bin/env', 'RELAY_PYTHON=' + str(relay_python)] + list(d['test_cmd']),
               'queue': str(queue), 'review_file': d['review_file']}
    if 'test_timeout_s' in d:
        deliver['test_timeout_s'] = d['test_timeout_s']
    out = {'name': sub_fleet_name(job, seg, epoch), 'goal': goal, 'lifecycle': 'dev_task', 'deliver': deliver}
    for key in ('hours', 'budget', 'artifacts', 'workers', 'supervisor_profile', 'worker_profiles'):
        if key in spec:
            out[key] = spec[key]
    roster = Path(worktree) / seg_dir / 'roster.yaml'
    if roster.is_file():
        out['roster'] = str(roster)
    roles = Path(worktree) / seg_dir / 'roles'
    if roles.is_dir():
        out['roles'] = str(roles)
    return out


class Executor:
    """Where a rendered spec goes. `start` returns a JSON-able handle (with `epoch`) that `stop` takes back;
    `running(job)` lists the segments whose sub-fleet runs now, {seg: handle}."""

    def start(self, job, seg, spec):
        raise NotImplementedError

    def stop(self, handle):
        raise NotImplementedError

    def running(self, job):
        raise NotImplementedError


class FakeExecutor(Executor):
    """Records starts and stops; starts nothing."""

    def __init__(self):
        self.started = []
        self.stopped = []

    def start(self, job, seg, spec):
        self.started.append((job, seg, spec))
        parsed = parse_name(spec.get('name'))
        return {'fake': len(self.started), 'job': job, 'seg': seg, 'epoch': parsed[2] if parsed else None}

    def stop(self, handle):
        self.stopped.append(handle)

    def running(self, job):
        out = {}
        for i, (j, seg, spec) in enumerate(self.started, 1):
            parsed = parse_name(spec.get('name'))
            handle = {'fake': i, 'job': j, 'seg': seg, 'epoch': parsed[2] if parsed else None}
            if j == job and handle not in self.stopped:
                out[seg] = handle
        return out


_STUB_RE = re.compile(r'fleetmux task --config (\S+) -')


def _json(text):
    for line in reversed((text or '').strip().splitlines() or ['']):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class FleetmuxExecutor(Executor):
    """`fleetmux start` (attach to the live root fleet), `task --config <stub> -`, `stop --config <stub>`.

    `run` has no default: the caller passes `subprocess.run` (the relay process) or a fake (tests)."""

    def __init__(self, fleetmux_argv, *, root_config, run, timeout_s):
        self.argv = list(fleetmux_argv)
        self.root_config = str(root_config)
        self.run = run
        self.timeout_s = timeout_s

    def _call(self, args, stdin=None):
        done = self.run(self.argv + list(args), input=stdin, capture_output=True, text=True,
                        timeout=self.timeout_s)
        return done.returncode, done.stdout or '', done.stderr or ''

    def start(self, job, seg, spec):
        code, _, err = self._call(['status', '--config', self.root_config])
        if code != 0:
            raise ExecutorError('the root fleet ' + self.root_config + ' is not running; not started: ' + err.strip())
        name = spec['name']
        code, out, err = self._call(['start', '--name', name, '--no-view'])
        m = _STUB_RE.search(out)
        if code != 0 or 'attached to the running' not in out or not m:
            raise ExecutorError('fleetmux start did not attach to the root fleet (exit %d): %s'
                                % (code, (out + err).strip()[:CLIP]))
        stub = m.group(1)
        code, out, err = self._call(['task', '--config', stub, '-'], stdin=json.dumps(spec))
        answer = _json(out)
        if answer.get('status') != 'accepted' or not answer.get('job_id'):
            raise ExecutorError('the root refused the segment: ' +
                                str(answer.get('reason') or err.strip() or out.strip() or 'exit %d' % code))
        parsed = parse_name(name)
        return {'stub': stub, 'job_id': answer['job_id'], 'name': name, 'epoch': parsed[2] if parsed else None}

    def stop(self, handle):
        config = handle.get('stub') or handle.get('child_config')
        if not config:
            raise ExecutorError('sub-fleet %s has not started yet; nothing to stop' % handle.get('name'))
        code, out, err = self._call(['stop', '--config', config])
        if code != 0:
            raise ExecutorError('fleetmux stop failed (exit %d): %s' % (code, (err or out).strip()[:CLIP]))

    def running(self, job):
        """The root's open jobs named `relay-<job>-<seg>-e<n>`, from `fleetmux status --config <root>`."""
        code, out, err = self._call(['status', '--config', self.root_config])
        status = _json(out) if code == 0 else {}
        if code != 0 or not status:
            raise ExecutorError('status of the root fleet failed: ' + (err or out).strip()[:CLIP])
        tree = status.get('subfleets') or {}
        found = {}
        for row in status.get('jobs') or []:
            parsed = parse_name(row.get('name'))
            if not parsed or parsed[0] != job:
                continue
            item = next((v for k, v in tree.items() if row.get('subfleet') and
                         (k == row['subfleet'] or k.startswith(row['subfleet'] + ' ('))), None)
            run_dir = (item or {}).get('run')
            found[parsed[1]] = {'stub': None, 'job_id': row.get('id'), 'name': row.get('name'),
                                'epoch': parsed[2], 'child_config': str(Path(run_dir) / 'config.json')
                                if run_dir else None}
        return found


def ensure_deploy_branch(worktree, branch, base, *, run):
    """Create the local deploy branch at `base` when it is missing (the dev_task gate refuses a missing one and
    fast-forwards it on delivery). Never moves an existing branch. True when it was created."""
    have = run(['git', '-C', str(worktree), 'rev-parse', '--verify', '--quiet', 'refs/heads/' + branch],
               capture_output=True, text=True)
    if have.returncode == 0:
        return False
    done = run(['git', '-C', str(worktree), 'branch', branch, base], capture_output=True, text=True)
    if done.returncode != 0:
        raise ExecutorError('cannot create %s at %s: %s' % (branch, base, (done.stderr or '').strip()[:CLIP]))
    return True


def queued_delivery(queue, deploy_branch):
    """The dev_task gate's queue row for `deploy_branch` (written only after the gate passed), or None."""
    try:
        lines = Path(queue).read_text(encoding='utf-8').splitlines()
    except (FileNotFoundError, NotADirectoryError):
        return None
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get('deploy_branch') == deploy_branch and row.get('sha'):
            return row
    return None
