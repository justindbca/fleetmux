"""Trusted-Python completion gate of the dev_task lifecycle (master design v2 §4.14, docs/design.md section 70).

``dev_task`` is done only when its work is handed on, and that is judged here by reading git and the hand-off
record, never by a worker's own report. ``deliver.via`` picks the hand-off (the job spec's value, else
fleet.yaml#deliver.via, shipped ``rollout``):

``rollout`` (the default):

1. the feature worktree is clean and on the feature branch; it is rebased onto the newest deploy branch head
   (a conflict is aborted and reported);
2. the test command passes in the feature worktree, on that rebased sha;
3. the sha is submitted to the local rollout: the ``kind: git`` target of the same repository in the rollout
   registry, through the registry's ``site.client``. Done when the rollout answers with a request id
   (``enqueued`` or ``exists``); merging is the rollout's job, so the deploy branch is never moved here. Anything
   else (no registry, no client, no target, a refused submit) is a gate failure with its reason; there is no
   fallback to the queue file.

``queue`` (kept as an option; the relay pins it):

1. the feature worktree is clean, on the feature branch, and fast-forwards the deploy branch;
2. the test command passes in the feature worktree;
3. the deploy branch is fast-forwarded to the tested sha (refused while it is checked out anywhere);
4. one ``pending_approval`` entry is appended to the queue file. Enqueued is not deployed: the queue
   executor deploys only when the user names it (T-39 ⑤).

There is no review step: no verdict file is looked for or read. A deliver spec written before the review
removal may still carry ``review_file``; it is kept as written and ignored.
"""
import hashlib
import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path

import yaml

from fleetmux import fleet_config

DELIVER_KEYS = ('repo', 'feature_branch', 'deploy_branch', 'test_cmd', 'queue')   # via: queue
ROLLOUT_KEYS = ('repo', 'feature_branch', 'deploy_branch', 'test_cmd', 'brief', 'rollout')   # via: rollout
ROLLOUT_BLOCK = ('verify', 'target', 'title', 'commit_message', 'restart')   # deliver.rollout; verify is required
REFIX_KEYS = ('root', 'attempt', 'previous', 'evidence')
VIAS = fleet_config.DELIVER_VIAS
RID_RE = re.compile(r'[A-Za-z0-9._:-]{1,80}')   # rollout's request id (gittxn.RID_RE)
CTRL_RE = re.compile(r'[\x00-\x1f\x7f]')
TAKEN = ('enqueued', 'exists')   # submit replies that mean the local rollout holds the request
MAX_STR = 400     # rollout origin.MAX_STR
MAX_ARGV = 50     # rollout origin.MAX_ARGV (test_cmd)


def _str_ok(value):
    return isinstance(value, str) and 0 < len(value) <= MAX_STR and not CTRL_RE.search(value)


def _abs_ok(value):
    return _str_ok(value) and os.path.isabs(value)


MAX_MESSAGE = 20000   # rollout txn.commit_message_error: a string, no NUL, at most 20000 characters, a first line


def message_problem(text):
    """None when ``text`` passes the shape check the rollout core makes on a commit message (txn.commit_message_error:
    a string without NUL, at most 20000 characters, a non-empty first line); else why not. Content rules (for example
    ASCII only) belong to the registry's own commit message check (site.commit_msg_check) and come back as a refused
    submit."""
    if not isinstance(text, str) or '\x00' in text or len(text) > MAX_MESSAGE:
        return 'must be a string without NUL of at most ' + str(MAX_MESSAGE) + ' characters'
    if not text.strip() or not text.splitlines()[0].strip():
        return 'must start with a non-empty title line'
    return None


def default_via():
    return fleet_config.deliver()['via']


def _check_refix(chain):
    """deliver.refix: the rollback chain a refix job carries into its next delivery (rollout origin.refix)."""
    if not isinstance(chain, dict) or set(chain) != set(REFIX_KEYS):
        raise ValueError('deliver.refix must be a mapping with exactly: ' + ', '.join(REFIX_KEYS))
    if not (isinstance(chain['root'], str) and RID_RE.fullmatch(chain['root'])):
        raise ValueError('deliver.refix.root must be a request id')
    attempt = chain['attempt']
    if type(attempt) is not int or not 0 <= attempt <= 20:
        raise ValueError('deliver.refix.attempt must be an integer in 0..20')
    if chain['previous'] is not None and not (isinstance(chain['previous'], str) and RID_RE.fullmatch(chain['previous'])):
        raise ValueError('deliver.refix.previous must be a request id or null')
    ev = chain['evidence']
    if not isinstance(ev, list) or len(ev) > 20 or not all(_abs_ok(p) for p in ev):
        raise ValueError('deliver.refix.evidence must be a list of at most 20 absolute paths')


def validate_deliver(spec):
    """The task's ``deliver`` block for dev_task, normalised (``via`` resolved and written in); ValueError when
    unusable."""
    if not isinstance(spec, dict):
        raise ValueError('dev_task needs "deliver": {' + ', '.join(ROLLOUT_KEYS) + '} (via: rollout) or {'
                         + ', '.join(DELIVER_KEYS) + '} (via: queue) (master design v2 §4.14: '
                         'merge / test / enqueue are done and judged by trusted Python)')
    via = spec.get('via')
    if via is None:
        via = default_via()
    if via not in VIAS:
        raise ValueError('deliver.via must be one of ' + ', '.join(VIAS) + ', got ' + repr(via))
    keys = DELIVER_KEYS if via == 'queue' else ROLLOUT_KEYS
    missing = [k for k in keys if k not in spec]
    if missing:
        raise ValueError('deliver is missing: ' + ', '.join(missing) + ' (via: ' + via + ')')
    for k in ('repo', 'queue') if via == 'queue' else ('repo', 'brief'):
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
    if via == 'rollout':
        block = spec['rollout']
        if not isinstance(block, dict):
            raise ValueError('deliver.rollout must be a mapping with only: ' + ', '.join(ROLLOUT_BLOCK))
        extra = sorted(map(str, set(block) - set(ROLLOUT_BLOCK)))
        if extra:
            raise ValueError('deliver.rollout has unknown keys: ' + ', '.join(extra) + ' (allowed: '
                             + ', '.join(ROLLOUT_BLOCK) + ')')
        if not _str_ok(block.get('verify')):
            raise ValueError('deliver.rollout.verify (the acceptance script path in the repository) is required')
        for k in ('target', 'title'):
            if k in block and not _str_ok(block[k]):
                raise ValueError('deliver.rollout.' + k + ' must be a short string')
        restart = block.get('restart')
        if 'restart' in block and restart != 'none' and not (
                isinstance(restart, list) and restart and len(restart) <= 50
                and all(_str_ok(x) and not x.startswith('-') and ' ' not in x for x in restart)):
            raise ValueError('deliver.rollout.restart must be "none" or a non-empty list of service labels (the '
                             'submit --restart values), got ' + repr(restart)[:200])
        if 'commit_message' in block and message_problem(block['commit_message']):
            raise ValueError('deliver.rollout.commit_message ' + message_problem(block['commit_message']))
        if len(cmd) > MAX_ARGV or not all(_str_ok(x) for x in cmd):
            raise ValueError('deliver.test_cmd goes into the rollout origin: at most ' + str(MAX_ARGV) + ' arguments of '
                             'at most ' + str(MAX_STR) + ' characters without control characters')
        if not _abs_ok(spec['brief']):
            raise ValueError('deliver.brief must be an absolute path of at most ' + str(MAX_STR) + ' characters')
        if spec.get('refix') is not None:
            _check_refix(spec['refix'])
    return dict(spec, test_timeout_s=timeout, via=via)


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


def _run_tests(spec, repo, workspace, run, task_id, runner, rec):
    """Run deliver.test_cmd in ``repo`` and keep its log; None when it passed, else the gate problem. A log that
    cannot be written is named in ``rec['problem']`` but never changes the verdict."""
    timeout = spec.get('test_timeout_s', 3600)
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
        return problem + ('' if not log_error else '; test log not written: ' + log_error)
    except OSError as exc:
        out = 'tests could not start: ' + str(exc) + '\n'
        log, log_error = _write_log(log_path, _header(spec['test_cmd'], repo, started,
                                                     'start failed: ' + str(exc)), out)
        rec['tests'] = {'cmd': spec['test_cmd'], 'returncode': None, 'tail': out[-1500:], 'log': log}
        problem = 'tests could not start: ' + str(exc)
        if not log_error:
            problem += '; log: ' + log
        return problem + ('' if not log_error else '; test log not written: ' + log_error)
    out = (p.stdout or '') + (p.stderr or '')
    log, log_error = _write_log(log_path, _header(spec['test_cmd'], repo, started,
                                                 'exit: ' + str(p.returncode)), out)
    rec['tests'] = {'cmd': spec['test_cmd'], 'returncode': p.returncode, 'tail': out[-1500:], 'log': log}
    if log_error:  # the log is evidence, never the verdict: the gate outcome stays as it is
        rec['problem'] = 'test log not written: ' + log_error
    if p.returncode:
        return ('tests failed (exit ' + str(p.returncode) + ')'
                + ('; log: ' + log if log else '')
                + ('' if not log_error else '; test log not written: ' + log_error))
    return None


def _via(spec):
    """The delivery of a stored spec. validate_deliver writes ``via`` into every spec it passes, so a spec without
    it was stored by an older release, when the queue was the only delivery: it stays a queue delivery."""
    return spec.get('via') or 'queue'


def done_when_problem(spec, sha, rec=None):
    """None when dev_task done_when holds for ``sha``. via queue: ``sha`` is in the deploy branch and queued (read
    from git + the queue file). via rollout: the gate record ``rec`` holds a request id the local rollout took for
    that same sha (the deploy branch is the rollout's to move, so it is not read here)."""
    if _via(spec) == 'rollout':
        got = (rec or {}).get('rollout') or {}
        rid = str(got.get('request_id') or '')
        if not rec or rec.get('sha') != sha or got.get('status') not in TAKEN or not RID_RE.fullmatch(rid):
            return 'done_when check failed: ' + sha[:12] + ' has no request id from the local rollout'
        return None
    repo, dep = spec['repo'], spec['deploy_branch']
    now = _sha(repo, dep)
    if not now or _git(repo, 'merge-base', '--is-ancestor', sha, now, check=False).returncode or not _queued(spec['queue'], sha, dep):
        return 'done_when check failed: ' + sha[:12] + ' not in ' + dep + ' or not queued'
    return None


def deliver(spec, workspace, *, task_id='', run='', runner=subprocess.run):
    """Run the dev_task gate. Returns a record {ok, steps[], problem?, sha?, queue_entry? | rollout?}; never raises
    for a gate failure (that is the task's outcome, not a daemon error). Idempotent: via queue, an already merged
    sha is not moved again and an already queued sha is not queued twice; via rollout, the same sha gets the same
    request id, which the rollout answers with ``exists``.

    The record keeps ``spec``, so a recheck (``lifecycle_recheck``) can rerun the gate from files."""
    if _via(spec) == 'rollout':
        return _deliver_rollout(spec, workspace, task_id, run, runner)
    steps = []
    rec = {'ok': False, 'steps': steps, 'at': time.time(), 'spec': spec}

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
        problem = _run_tests(spec, repo, workspace, run, task_id, runner, rec)
        if problem:
            return fail(problem)
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


# ------------------------------------------------------------------ via: rollout
class Blocked(Exception):
    """The delivery cannot be handed to the local rollout; the message is the reason the job reports."""


def request_id(job, task_id, run, sha):
    """The rollout request id of one delivery: ``<job>-<sha12>`` for a job's sub-fleet; for a root fleet's own
    dev_task (no job) ``<task>-<sha256(run dir)[:10]>-<sha12>``, the task id cut to its safe characters. The same
    task and sha give the same id (a resubmit is answered ``exists``); the run hash keeps two fleets' tasks of the
    same name apart. Always matches RID_RE (at most 80 characters)."""
    if job:
        return job + '-' + sha[:12]
    tag = re.sub(r'[^A-Za-z0-9._:-]', '_', task_id or 'task')[:80 - 2 - 10 - 12]
    digest = hashlib.sha256(str(run).encode('utf-8')).hexdigest()[:10]
    return tag + '-' + digest + '-' + sha[:12]


def registry_path():
    """The local rollout registry: $ROLLOUT_TARGETS (as given, even when missing), else the first existing file of
    fleet.yaml#deliver.registries; None when there is none."""
    env = os.environ.get('ROLLOUT_TARGETS')
    if env:
        return Path(os.path.expanduser(env))
    for path in fleet_config.deliver()['registries']:
        if Path(path).is_file():
            return Path(path)
    return None


def _common_dir(path):
    """The real path of a repository's shared git directory (the same for every worktree of one repository)."""
    p = _git(path, 'rev-parse', '--git-common-dir', check=False)
    if p.returncode:
        return None
    return os.path.realpath(os.path.join(str(path), p.stdout.strip()))


def _target(doc, registry, repo, dep, name):
    """(target name, its spec): the registry's kind: git target whose root is a worktree of the same repository
    as ``repo``. ``name`` (deliver.rollout.target) picks one; else the one whose live_branch is ``dep``."""
    mine = _common_dir(repo)
    found = {}
    for tname, t in (doc.get('targets') or {}).items():
        if not isinstance(t, dict) or t.get('kind') != 'git' or not isinstance(t.get('root'), str):
            continue
        root = os.path.expanduser(t['root'])
        if mine and Path(root).is_dir() and _common_dir(root) == mine:
            found[str(tname)] = t
    where = ' in ' + str(registry)
    if name is not None:
        if name not in found:
            raise Blocked('deliver.rollout.target ' + name + ' is not a kind: git target of ' + repo + where)
        return name, found[name]
    on_dep = {n: t for n, t in found.items() if t.get('live_branch') == dep} or found
    if not on_dep:
        raise Blocked('no kind: git target with its root in the repository of ' + repo + where)
    if len(on_dep) > 1:
        raise Blocked('more than one kind: git target of ' + repo + where + ' (' + ', '.join(sorted(on_dep))
                      + '); name one in deliver.rollout.target')
    return next(iter(on_dep.items()))


def _client(doc, registry):
    client = (doc.get('site') or {}).get('client') if isinstance(doc.get('site'), dict) else None
    if client is None:
        raise Blocked('the registry ' + str(registry) + ' has no site.client {cwd, argv}: fleetmux cannot run the '
                      'rollout submit client')
    cwd = client.get('cwd') if isinstance(client, dict) else None
    argv = client.get('argv') if isinstance(client, dict) else None
    if not (isinstance(cwd, str) and os.path.isabs(os.path.expanduser(cwd)) and isinstance(argv, list) and argv
            and all(isinstance(a, str) and a for a in argv)):
        raise Blocked('site.client in ' + str(registry) + ' must be {cwd: <absolute path>, argv: [<python>, -m, '
                      'rollout]}, got ' + repr(client)[:200])
    return {'cwd': os.path.expanduser(cwd), 'argv': list(argv)}


def _host():
    """(machine name, where it came from): the inventory's ``source: self`` entry, else this host's short name."""
    try:
        from fleetmux import fleet_superpowers
        name = fleet_superpowers.self_name()
    except Exception:  # an unreadable inventory falls back to the host name; origin.host_source says so
        name = None
    if name:
        return name, 'inventory'
    return socket.gethostname().split('.')[0].lower() or 'localhost', 'hostname'


def _run_info(run):
    """What the run's own files say about the job: (job id or None, root config path, job name, roster, roles)."""
    base = Path(run) if run else None
    cfg, name = {}, None
    if base is not None:
        try:
            cfg = json.loads((base / 'config.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            cfg = {}
        try:
            name = json.loads((base / 'subfleet.json').read_text(encoding='utf-8')).get('name')
        except (OSError, ValueError, AttributeError):
            name = None
    root = cfg.get('root_config') or (str((base / 'config.json').absolute()) if base is not None else '')
    return cfg.get('job') or None, root, name, cfg.get('roster'), cfg.get('roles')


def _title_and_message(repo, base, sha, block):
    """(request title, squash commit message). The message's first line is the oldest commit subject as written
    (the title may add "(+N more)"; the commit subject never does); with several commits the body lists every subject.
    deliver.rollout.title / commit_message override them."""
    subjects = _git(repo, 'log', '--reverse', '--format=%s', base + '..' + sha).stdout.splitlines()
    first = subjects[0] if subjects else ''
    title = block.get('title') or ((first if len(subjects) == 1 else first + ' (+' + str(len(subjects) - 1) + ' more)')
                                   if first.strip() else sha[:12])
    message = block.get('commit_message')
    if not message:
        message = first + ('\n\n' + '\n'.join('- ' + x for x in subjects) if len(subjects) > 1 else '') + '\n'
        problem = message_problem(message)
        if problem:
            raise Blocked('the squash commit message made from the branch commits ' + problem
                          + '; write one in deliver.rollout.commit_message')
    return title[:MAX_STR], message


def _earlier_requests(out_dir, rid, task):
    """Request ids this workspace submitted before for the same task under another sha (their origin files), oldest
    first: a rerun after the deploy branch moved submits a new sha under a new id, and these may still be live."""
    found = []
    for path in out_dir.glob('origin-*.json'):
        other = path.name[len('origin-'):-len('.json')]
        if other == rid:
            continue
        try:
            same = json.loads(path.read_text(encoding='utf-8')).get('task') == task
        except (OSError, ValueError, AttributeError):
            continue
        if same:
            found.append((path.stat().st_mtime, other))
    return [other for _, other in sorted(found)]


def _submit(spec, workspace, task_id, run, rec, base, sha):
    """Hand ``sha`` to the local rollout; returns rec['rollout']. Raises Blocked with the reason otherwise."""
    repo, feat, dep, block = spec['repo'], spec['feature_branch'], spec['deploy_branch'], spec['rollout']
    registry = registry_path()
    if registry is None:
        raise Blocked('no rollout registry: $ROLLOUT_TARGETS is unset and none of '
                      + ', '.join(fleet_config.deliver()['registries']) + ' exists')
    try:
        doc = yaml.safe_load(registry.read_text(encoding='utf-8')) or {}
    except OSError as exc:
        raise Blocked('the rollout registry ' + str(registry) + ' cannot be read: ' + str(exc)) from None
    except yaml.YAMLError as exc:
        raise Blocked('the rollout registry ' + str(registry) + ' is not valid YAML: '
                      + str(exc).splitlines()[0]) from None
    if not isinstance(doc, dict):
        raise Blocked('the rollout registry ' + str(registry) + ' is not a mapping')
    client = _client(doc, registry)
    target, _ = _target(doc, registry, repo, dep, block.get('target'))
    job, root_config, name, roster, roles = _run_info(run)
    rid = request_id(job, task_id, run, sha)
    host, host_source = _host()
    origin = {'v': 1, 'job': job, 'task': task_id or rid, 'brief': spec['brief'], 'branch': feat, 'repo': repo,
              'host': host, 'host_source': host_source, 'root_config': root_config,
              'test_cmd': list(spec['test_cmd']), 'verify': block['verify'],
              'refix': spec.get('refix') or {'root': rid, 'attempt': 0, 'previous': None, 'evidence': []}}
    for key, value in (('name', name), ('roster', roster), ('roles', roles)):
        if value and (key == 'name' and _str_ok(value) or key != 'name' and _abs_ok(value)):
            origin[key] = value
    if 'restart' in block:   # a refix task delivers with the same restart (rollout origin.restart)
        origin['restart'] = block['restart']
    title, message = _title_and_message(repo, base, sha, block)
    out_dir = Path(workspace).absolute() / '.deliver'
    out_dir.mkdir(parents=True, exist_ok=True)
    origin_file, message_file = out_dir / ('origin-' + rid + '.json'), out_dir / ('commit-' + rid + '.txt')
    earlier = _earlier_requests(out_dir, rid, task_id or rid)
    origin_file.write_text(json.dumps(origin, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    message_file.write_text(message, encoding='utf-8')
    argv = client['argv'] + ['submit', '--repo', repo, '--target', target, '--request-id', rid, '--sha', sha,
                             '--verify', block['verify'], '--title', title,
                             '--commit-message-file', str(message_file), '--origin-file', str(origin_file)]
    restart = block.get('restart')
    for label in ['none'] if restart == 'none' else (restart or []):
        argv += ['--restart', label]   # services to restart; none = the change needs no restart
    if job:
        argv += ['--session', job]   # the author session a refused request is reported to
    rec['rollout'] = {'target': target, 'request_id': rid, 'status': None, 'registry': str(registry)}
    if earlier:
        rec['rollout']['earlier_request_ids'] = earlier
    status_cmd = ('cd ' + client['cwd'] + ' && ROLLOUT_TARGETS=' + str(registry) + ' ' + ' '.join(client['argv'])
                  + ' submit --target ' + target + ' --status ')
    seen = ('' if not earlier else '; this workspace submitted ' + ', '.join(earlier) + ' for the same task before: '
            'check each with ' + status_cmd + '<id> before resubmitting')
    env = dict(os.environ, ROLLOUT_TARGETS=str(registry), PYTHONDONTWRITEBYTECODE='1')
    timeout = fleet_config.deliver()['submit_timeout_s']
    try:
        p = subprocess.run(argv, cwd=client['cwd'], env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise Blocked('the rollout submit client ran past ' + str(timeout) + ' s; the rollout may hold request ' + rid
                      + ' already: check with ' + status_cmd + rid + ' first, and if it is there do not recheck '
                      '(a rerun after the deploy branch moved submits another sha under another request id)'
                      + seen) from None
    except OSError as exc:
        raise Blocked('the rollout submit client could not start (' + ' '.join(client['argv']) + ' in '
                      + client['cwd'] + '): ' + str(exc) + seen) from None
    tail = ((p.stderr or '') + (p.stdout or '')).strip()[-800:]
    if p.returncode:
        raise Blocked('the rollout submit of ' + rid + ' to ' + target + ' was refused (exit ' + str(p.returncode)
                      + '): ' + tail + seen)
    reply = None
    for line in reversed((p.stdout or '').splitlines()):
        try:
            reply = json.loads(line)
            break
        except ValueError:
            continue
    status = reply.get('status') if isinstance(reply, dict) else None
    if status not in TAKEN:
        raise Blocked('the rollout did not take ' + rid + ' (status ' + repr(status) + '): ' + tail + seen)
    if reply.get('request_id') != rid:
        raise Blocked('the rollout answered for request id ' + repr(reply.get('request_id')) + ', not ' + rid)
    rec['rollout']['status'] = status
    return rec['rollout']


def _deliver_rollout(spec, workspace, task_id, run, runner):
    steps = []
    spec = dict(spec, via='rollout')
    rec = {'ok': False, 'steps': steps, 'at': time.time(), 'spec': spec}

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
        if _git(repo, 'merge-base', '--is-ancestor', base, sha, check=False).returncode:
            p = _git(repo, 'rebase', '--quiet', base, check=False)   # onto the newest deploy branch head
            if p.returncode:
                _git(repo, 'rebase', '--abort', check=False)
                return fail('rebase of ' + feat + ' onto ' + dep + ' ' + base[:12] + ' failed; aborted, ' + feat
                            + ' left at ' + sha[:12] + ': ' + (p.stderr or p.stdout).strip()[-500:])
            rec['rebased_from'], sha = sha, _sha(repo, feat)
            rec['sha'] = sha
            steps.append('rebased')
        if not int(_git(repo, 'rev-list', '--count', base + '..' + sha).stdout.strip() or 0):
            return fail(feat + ' has no commits beyond ' + dep)
        steps.append('base_check')
        problem = _run_tests(spec, repo, workspace, run, task_id, runner, rec)
        if problem:
            return fail(problem)
        steps.append('tests')
        if _sha(repo, feat) != sha:
            return fail(feat + ' moved while the tests ran; the tested sha ' + sha[:12] + ' is not submitted')
        _submit(spec, workspace, task_id, run, rec, base, sha)
        steps.append('submitted')
        problem = done_when_problem(spec, sha, rec)
        if problem:
            return fail(problem)
    except Blocked as exc:
        return fail('rollout: ' + str(exc))
    except (RuntimeError, subprocess.TimeoutExpired, OSError, ValueError, KeyError) as exc:
        return fail(type(exc).__name__ + ': ' + str(exc) if isinstance(exc, KeyError) else str(exc))
    rec['ok'] = True
    return rec
