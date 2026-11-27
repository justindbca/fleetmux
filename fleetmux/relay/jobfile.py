"""The job definition kept in the project repo: `.relay/<job>/job.yaml` and `.relay/<job>/<segment>/`.

A machine that has only the repo can start any segment from these files (docs/relay.md, "Job files"). Every path
in them is relative to the repo root; a machine's absolute path is refused (`scan_abs_paths`).
"""
import re
from pathlib import Path

import yaml

from . import records as R

RESERVED = ('merge', 'merged', 'coordinator')
WEIGHTS = ('heavy', 'light')
MERGE = 'merge'
NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')

# Keys a segment's job.spec.yaml may set; the rest of the start spec is filled per machine (launch.render).
SPEC_KEYS = ('goal', 'hours', 'budget', 'artifacts', 'workers', 'supervisor_profile', 'worker_profiles', 'deliver')
SPEC_DELIVER_KEYS = ('test_cmd', 'test_timeout_s', 'review_file')  # review_file: an older spec's, ignored
PROGRAM_FILLED = ('name', 'lifecycle', 'roster', 'roles', 'overrides',
                  'deliver.repo', 'deliver.feature_branch', 'deliver.deploy_branch', 'deliver.queue')

# A machine's own absolute path: a home or volume directory, `~/`, `$HOME`, or a drive letter. System paths such
# as /bin/bash are the same on every machine and stay allowed.
ABS_PATH_RE = re.compile(r'(?<![\w.~-])/(?:Users|home|Volumes|private|root|mnt)/'
                         r'|(?<![\w])~/'
                         r'|\$\{?HOME\}?'
                         r'|(?<![\w])[A-Za-z]:\\')


SHOWN_HITS = 5                  # machine-path hits named in one error message


class JobError(ValueError):
    """A job file that cannot be used as it is."""


def job_dir(job):
    return '.relay/' + job


def segment_dir(job, seg):
    return job_dir(job) + '/' + seg


def check_name(kind, name):
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise JobError(kind + ' name must match ' + NAME_RE.pattern + ': ' + repr(name))
    return name


def _read_yaml(path):
    try:
        data = yaml.safe_load(Path(path).read_text())
    except FileNotFoundError:
        raise JobError('missing ' + str(path)) from None
    except (OSError, yaml.YAMLError) as exc:
        raise JobError('unreadable ' + str(path) + ': ' + str(exc)) from None
    if not isinstance(data, dict):
        raise JobError(str(path) + ' must be a mapping')
    return data


def _str_list(where, value):
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
        raise JobError(where + ' must be a list of names')
    return list(value)


def _check_cycles(deps):
    state = {}

    def visit(n, path):
        if state.get(n) == 'done':
            return
        if state.get(n) == 'on':
            raise JobError('dependency cycle: ' + ' -> '.join(path + [n]))
        state[n] = 'on'
        for d in deps.get(n, []):
            visit(d, path + [n])
        state[n] = 'done'

    for n in sorted(deps):
        visit(n, [])


def load_job(repo, job):
    """`.relay/<job>/job.yaml`, validated: names, weights, toolchain lists, dependencies (known, no cycle)."""
    check_name('job', job)
    data = _read_yaml(Path(repo) / job_dir(job) / 'job.yaml')
    if data.get('job') != job:
        raise JobError('job.yaml names job ' + repr(data.get('job')) + ', expected ' + repr(job))
    nodes = _str_list('nodes', data.get('nodes'))
    tasks = data.get('tasks')
    if not isinstance(tasks, dict) or not tasks:
        raise JobError('job.yaml needs a non-empty tasks mapping')
    out_tasks = {}
    for seg, t in tasks.items():
        seg = str(seg)
        if seg in RESERVED:
            raise JobError('segment name ' + seg + ' is reserved (' + ', '.join(RESERVED) + ')')
        check_name('segment', seg)
        if not isinstance(t, dict):
            raise JobError('task ' + seg + ' must be a mapping')
        weight = t.get('weight')
        if weight not in WEIGHTS:
            raise JobError('task ' + seg + ': weight must be one of ' + ', '.join(WEIGHTS))
        prefer = t.get('prefer')
        if prefer is not None and not isinstance(prefer, str):
            raise JobError('task ' + seg + ': prefer must be a machine name')
        out_tasks[seg] = {'title': str(t.get('title') or seg), 'weight': weight,
                          'needs': _str_list('task ' + seg + ' needs', t.get('needs')),
                          'depends_on': _str_list('task ' + seg + ' depends_on', t.get('depends_on')),
                          'prefer': prefer or None}
    for seg, t in out_tasks.items():
        unknown = [d for d in t['depends_on'] if d not in out_tasks]
        if unknown:
            raise JobError('task ' + seg + ' depends on unknown segment(s): ' + ', '.join(unknown))
    _check_cycles({s: t['depends_on'] for s, t in out_tasks.items()})
    merge = data.get('merge')
    out_merge = None
    if merge is not None:
        if not isinstance(merge, dict):
            raise JobError('merge must be a mapping')
        deps = _str_list('merge depends_on', merge.get('depends_on')) or sorted(out_tasks)
        unknown = [d for d in deps if d not in out_tasks]
        if unknown:
            raise JobError('merge depends on unknown segment(s): ' + ', '.join(unknown))
        weight = merge.get('weight', 'heavy')
        if weight not in WEIGHTS:
            raise JobError('merge: weight must be one of ' + ', '.join(WEIGHTS))
        out_merge = {'title': str(merge.get('title') or 'merge all segments'), 'weight': weight,
                     'needs': _str_list('merge needs', merge.get('needs')), 'depends_on': deps,
                     'prefer': merge.get('prefer') or None, 'gate': merge.get('gate')}
    return {'job': job, 'nodes': nodes, 'tasks': out_tasks, 'merge': out_merge}


def new_record(job, seg, t, *, base):
    """A task record (03-interfaces section 1.3) as `relay init` creates it (`blocked` when it has dependencies)."""
    return R.new_task(job=job, id=seg, title=t['title'], weight=t['weight'], dir=segment_dir(job, seg),
                      needs=t['needs'], depends_on=t['depends_on'], prefer=t.get('prefer'), base=base,
                      reserved_ok=seg in RESERVED)


def initial_records(doc, *, base):
    """Every task record of a new job: segments, then the merge segment when job.yaml has `merge`."""
    out = {seg: new_record(doc['job'], seg, t, base=base) for seg, t in doc['tasks'].items()}
    if doc.get('merge'):
        out[MERGE] = new_record(doc['job'], MERGE, doc['merge'], base=base)
    return out


def scan_abs_paths(repo, job):
    """[(repo-relative file, line number, line)] for every line under `.relay/<job>/` naming a machine path."""
    root = Path(repo) / job_dir(job)
    hits = []
    for path in sorted(p for p in root.rglob('*') if p.is_file()):
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for no, line in enumerate(text.splitlines(), 1):
            if ABS_PATH_RE.search(line):
                hits.append((path.relative_to(repo).as_posix(), no, line.strip()))
    return hits


def check_job_tree(repo, job):
    """JobError when any file of the job names a machine's absolute path (R24)."""
    hits = scan_abs_paths(repo, job)
    if hits:
        raise JobError('absolute machine path in job files: ' +
                       '; '.join('%s:%d: %s' % h for h in hits[:SHOWN_HITS]))


def load_spec(repo, job, seg):
    """`.relay/<job>/<segment>/job.spec.yaml`: only the keys a project sets; the program fills the rest."""
    check_name('job', job)
    path = Path(repo) / segment_dir(job, seg) / 'job.spec.yaml'
    if not path.is_file():
        raise JobError('missing ' + segment_dir(job, seg) + '/job.spec.yaml')
    spec = _read_yaml(path)
    deliver = spec.get('deliver') or {}
    if not isinstance(deliver, dict):
        raise JobError('job.spec.yaml deliver must be a mapping')
    given = set(spec) | {'deliver.' + k for k in deliver}
    filled = sorted(k for k in PROGRAM_FILLED if k in given)
    if filled:
        raise JobError('job.spec.yaml sets keys filled by the program: ' + ', '.join(filled))
    extra = sorted(set(spec) - set(SPEC_KEYS))
    extra += sorted('deliver.' + k for k in set(deliver) - set(SPEC_DELIVER_KEYS))
    if extra:
        raise JobError('job.spec.yaml takes only ' + ', '.join(SPEC_KEYS) + ' (not ' + ', '.join(extra) + ')')
    if not isinstance(spec.get('goal'), str) or not spec['goal'].strip():
        raise JobError('job.spec.yaml needs a goal')
    cmd = deliver.get('test_cmd')
    if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) and x for x in cmd):
        raise JobError('job.spec.yaml deliver.test_cmd must be a non-empty argv list')
    return spec
