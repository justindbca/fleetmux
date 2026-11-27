"""Trusted-Python completion gate of the dev_task lifecycle (master design v2 §4.14).

``dev_task`` is done only when its work is merged into the deploy branch and enqueued, and that is
judged here by reading git and the queue file, never by a worker's own report:

1. the feature worktree is clean, on the feature branch, and fast-forwards the deploy branch;
2. the test command passes in the feature worktree;
3. the deploy branch is fast-forwarded to the tested sha (refused while it is checked out anywhere);
4. one ``pending_approval`` entry is appended to the queue file. Enqueued is not deployed: the queue
   executor deploys only when the user names it (T-39 ⑤).

There is no review step: no verdict file is looked for or read. A deliver spec written before the review
removal may still carry ``review_file``; it is kept as written and ignored.
"""
import json
import os
import subprocess
import time
from pathlib import Path

DELIVER_KEYS = ('repo', 'feature_branch', 'deploy_branch', 'test_cmd', 'queue')


def validate_deliver(spec):
    """The task's ``deliver`` block for dev_task, normalised; ValueError when unusable."""
    if not isinstance(spec, dict):
        raise ValueError('dev_task needs "deliver": {' + ', '.join(DELIVER_KEYS) + '} (master design v2 §4.14: '
                         'merge / test / enqueue are done and judged by trusted Python)')
    missing = [k for k in DELIVER_KEYS if k not in spec]
    if missing:
        raise ValueError('deliver is missing: ' + ', '.join(missing))
    for k in ('repo', 'queue'):
        if not isinstance(spec[k], str) or not os.path.isabs(spec[k]):
            raise ValueError('deliver.' + k + ' must be an absolute path')
    for k in ('feature_branch', 'deploy_branch'):
        if not isinstance(spec[k], str) or not spec[k] or spec[k].startswith('-') or ' ' in spec[k]:
            raise ValueError('deliver.' + k + ' must be a branch name')
    if spec['feature_branch'] == spec['deploy_branch']:
        raise ValueError('deliver.feature_branch and deploy_branch must differ')
    cmd = spec['test_cmd']
    if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) and x for x in cmd):
        raise ValueError('deliver.test_cmd must be a non-empty argv list')
    timeout = spec.get('test_timeout_s', 3600)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError('deliver.test_timeout_s must be a positive number')
    return dict(spec, test_timeout_s=timeout)


def _git(repo, *args, check=True):
    p = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, timeout=120)
    if check and p.returncode:
        raise RuntimeError('git ' + ' '.join(args) + ' failed: ' + (p.stderr or p.stdout).strip()[-500:])
    return p


def _sha(repo, ref):
    return _git(repo, 'rev-parse', '--verify', '--quiet', ref + '^{commit}').stdout.strip()


def _checked_out(repo, branch):
    """Worktrees that have ``branch`` checked out (git refuses to move such a branch safely)."""
    out = _git(repo, 'worktree', 'list', '--porcelain').stdout
    paths, current = [], None
    for line in out.splitlines():
        if line.startswith('worktree '):
            current = line[len('worktree '):]
        elif line == 'branch refs/heads/' + branch:
            paths.append(current)
    return paths


def _queued(queue, sha, branch):
    try:
        lines = Path(queue).read_text(encoding='utf-8').splitlines()
    except FileNotFoundError:
        return None
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get('sha') == sha and row.get('deploy_branch') == branch:
            return row
    return None


LOG_CAP = 20 * 1024 * 1024   # the persisted test log never grows past 20 MiB ...
LOG_HEAD = 1 * 1024 * 1024   # ... keeping its first 1 MiB ...
LOG_TAIL = 19 * 1024 * 1024  # ... and its last 19 MiB, with a visible marker in between
TRUNCATED = '\n... [truncated %d bytes of the test output] ...\n'


def _log_path(workspace, run, task_id):
    """``<run>/gate/<task_id>-tests.log``; without ``run`` beside the workspace, never inside it."""
    base = Path(run) if run else Path(workspace).parent
    path = base / 'gate' / (task_id + '-tests.log')
    return path if path.is_absolute() else Path.cwd() / path   # the record names an absolute path


def _text(value):
    """``TimeoutExpired.stdout`` / ``stderr`` may be bytes even with ``text=True``; None means nothing."""
    if value is None:
        return ''
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return value


def _capped(text):
    """The head 1 MiB + the tail 19 MiB of an over-long output, with the cut stated in the log."""
    data = text.encode('utf-8', 'replace')
    if len(data) <= LOG_CAP:
        return text
    head, tail = data[:LOG_HEAD], data[-LOG_TAIL:]
    return (head.decode('utf-8', 'replace') + TRUNCATED % (len(data) - len(head) - len(tail))
            + tail.decode('utf-8', 'replace'))


def _header(cmd, cwd, started, outcome):
    """First lines of every gate log: what ran, where, when, and how it ended."""
    return ('gate test log\n'
            'cmd: ' + json.dumps(list(cmd), ensure_ascii=False) + '\n'
            'cwd: ' + str(cwd) + '\n'
            'started: ' + started + '\n'
            + outcome + '\n'
            '--- output ---\n')


def _write_log(path, header, output):
    """Write the log; returns ``(absolute path or '', error)`` and never raises.

    A log that cannot be written is evidence missing, not a gate failure: the caller keeps its verdict
    and only names the problem."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(header + _capped(output), encoding='utf-8')
        return str(path), ''
    except OSError as exc:
        return '', str(exc)


def done_when_problem(spec, sha):
    """None when ``sha`` is in the deploy branch and queued (dev_task done_when, read from git + the queue)."""
    repo, dep = spec['repo'], spec['deploy_branch']
    now = _sha(repo, dep)
    if not now or _git(repo, 'merge-base', '--is-ancestor', sha, now, check=False).returncode or not _queued(spec['queue'], sha, dep):
        return 'done_when check failed: ' + sha[:12] + ' not in ' + dep + ' or not queued'
    return None


def deliver(spec, workspace, *, task_id='', run='', runner=subprocess.run):
    """Run the dev_task gate. Returns a record {ok, steps[], problem?, sha?, queue_entry?}; never raises
    for a gate failure (that is the task's outcome, not a daemon error). Idempotent: an already merged
    sha is not moved again and an already queued sha is not queued twice.

    The record keeps ``spec``, so a recheck (``lifecycle_recheck``) can rerun the gate from files."""
    steps = []
    rec = {'ok': False, 'steps': steps, 'at': time.time(), 'spec': spec}
    timeout = spec.get('test_timeout_s', 3600)

    def fail(problem):
        rec['problem'] = problem
        return rec
    repo, feat, dep = spec['repo'], spec['feature_branch'], spec['deploy_branch']
    try:
        if not Path(repo).is_dir():
            return fail('deliver.repo ' + repo + ' is not a directory')
        head = _git(repo, 'symbolic-ref', '--quiet', '--short', 'HEAD', check=False).stdout.strip()
        if head != feat:
            return fail('deliver.repo is on ' + repr(head or 'detached HEAD') + ', not the feature branch ' + feat)
        dirty = _git(repo, 'status', '--porcelain', '--untracked-files=no').stdout.strip()
        if dirty:
            return fail('feature worktree has uncommitted changes: ' + dirty.splitlines()[0])
        sha, base = _sha(repo, feat), _sha(repo, dep)
        if not sha or not base:
            return fail('branch missing: ' + (feat if not sha else dep))
        rec['sha'] = sha
        merged = _git(repo, 'merge-base', '--is-ancestor', sha, base, check=False).returncode == 0
        if not merged:
            if _git(repo, 'merge-base', '--is-ancestor', base, sha, check=False).returncode:
                return fail(dep + ' is not an ancestor of ' + feat + ': not a fast-forward (integrator must rebase)')
            if not int(_git(repo, 'rev-list', '--count', base + '..' + sha).stdout.strip() or 0):
                return fail(feat + ' has no commits beyond ' + dep)
        steps.append('fast_forward_check')
        started = time.strftime('%Y-%m-%dT%H:%M:%S')
        log_path = _log_path(workspace, run, task_id)
        try:
            p = runner(spec['test_cmd'], cwd=repo, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            # partial output survives the kill; it may be bytes even with text=True
            out = _text(exc.stdout) + _text(exc.stderr)
            log, log_error = _write_log(log_path, _header(spec['test_cmd'], repo, started,
                                                         'timeout: ' + str(timeout)), out)
            rec['tests'] = {'cmd': spec['test_cmd'], 'returncode': None, 'tail': out[-1500:], 'log': log}
            problem = 'tests timed out after ' + str(timeout) + ' s' + ('; log: ' + log if log else '')
            return fail(problem + ('' if not log_error else '; test log not written: ' + log_error))
        except OSError as exc:
            out = 'tests could not start: ' + str(exc) + '\n'
            log, log_error = _write_log(log_path, _header(spec['test_cmd'], repo, started,
                                                         'start failed: ' + str(exc)), out)
            rec['tests'] = {'cmd': spec['test_cmd'], 'returncode': None, 'tail': out[-1500:], 'log': log}
            problem = 'tests could not start: ' + str(exc)
            if not log_error:
                problem += '; log: ' + log
            return fail(problem + ('' if not log_error else '; test log not written: ' + log_error))
        out = (p.stdout or '') + (p.stderr or '')
        log, log_error = _write_log(log_path, _header(spec['test_cmd'], repo, started,
                                                     'exit: ' + str(p.returncode)), out)
        rec['tests'] = {'cmd': spec['test_cmd'], 'returncode': p.returncode, 'tail': out[-1500:], 'log': log}
        if log_error:  # the log is evidence, never the verdict: the gate outcome stays as it is
            rec['problem'] = 'test log not written: ' + log_error
        if p.returncode:
            return fail('tests failed (exit ' + str(p.returncode) + ')'
                        + ('; log: ' + log if log else '')
                        + ('' if not log_error else '; test log not written: ' + log_error))
        steps.append('tests')
        if not merged:
            holders = _checked_out(repo, dep)
            if holders:
                return fail(dep + ' is checked out in ' + ', '.join(holders) + '; refusing to move it under a worktree')
            _git(repo, 'update-ref', 'refs/heads/' + dep, sha, base)  # compare-and-swap: fails if it moved
        steps.append('merged')
        entry = _queued(spec['queue'], sha, dep)
        if entry is None:
            entry = {'status': 'pending_approval', 'sha': sha, 'feature_branch': feat, 'deploy_branch': dep,
                     'repo': repo, 'task_id': task_id, 'run': run, 'at': time.time()}
            Path(spec['queue']).parent.mkdir(parents=True, exist_ok=True)
            with open(spec['queue'], 'a', encoding='utf-8') as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + '\n')
        steps.append('enqueued')
        rec['queue_entry'] = entry
        # done_when is re-read from git + queue, not taken from the steps above
        problem = done_when_problem(spec, sha)
        if problem:
            return fail(problem)
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as exc:
        return fail(str(exc))
    rec['ok'] = True
    return rec
