"""Shared helpers for the relay flow tests: an in-memory state store, default parameters and a job tree.

The in-memory store stands in for the git state layer: it keeps one record per task with a version that a
compare-and-swap must name, which is the contract the git layer gives on the state remote.
"""
import copy
import textwrap
from pathlib import Path

PARAMS = {
    'notify_max': 1000, 'reason_max': 200, 'probe_timeout_s': 5,
    'beat_s': 60, 'lost_after_s': 300, 'snapshot_s': 120, 'poll_s': 30, 'git_timeout_s': 20,
    'max_tasks_per_node': 2, 'max_heavy_per_node': 1, 'disk_floor_heavy_gb': 40, 'disk_floor_any_gb': 15,
    'score': {'cores': 1.0, 'mem_gb': 0.25, 'disk_free_gb': 0.02}, 'offline_grace_s': 0,
    'claim_stagger_s': 20, 'wake_gap_s': 180, 'settle_s': 600, 'budget_pause_gap_s': 60,
    'coordinator': {'enabled': True, 'prefer': '', 'takeover': True},
}


def params(**over):
    out = copy.deepcopy(PARAMS)
    out.update(over)
    return out


class MemoryStore:
    """Task records with versions (compare-and-swap) and node records (single writer)."""

    def __init__(self):
        self._tasks = {}
        self._nodes = {}
        self._refs = {}
        self.pushes = []          # every write, in order: (kind, name)
        self.fail_reads = False

    def tasks(self):
        if self.fail_reads:
            raise OSError('state remote unreachable')
        return {k: (v, copy.deepcopy(r)) for k, (v, r) in self._tasks.items()}

    def cas_task(self, task, expected, record, snap=None):
        self.pushes.append(('task', task))
        cur = self._tasks.get(task)
        if (cur[0] if cur else None) != expected:
            return False
        self._tasks[task] = ((cur[0] + 1) if cur else 1, copy.deepcopy(record))
        if snap is not None:
            self._refs['snap/' + task] = snap
        return True

    def nodes(self):
        if self.fail_reads:
            raise OSError('state remote unreachable')
        return copy.deepcopy(self._nodes)

    def put_node(self, node, record):
        self.pushes.append(('node', node))
        self._nodes[node] = copy.deepcopy(record)

    def put_ref(self, name, content):
        self.pushes.append(('ref', name))
        self._refs[name] = copy.deepcopy(content)

    def put_salvage(self, task, node, epoch, content):
        name = 'salvage/%s/%s-e%d' % (task, node, epoch)
        self.pushes.append(('ref', name))
        if name in self._refs:
            return False
        self._refs[name] = copy.deepcopy(content)
        return True

    def salvages(self, task):
        return self.refs('salvage/' + task + '/')

    def refs(self, prefix=''):
        return {k: copy.deepcopy(v) for k, v in self._refs.items() if k.startswith(prefix)}


JOB_YAML = """\
job: demo
nodes: [laptop, desktop, server]
tasks:
  M0: {title: "client side", weight: light, needs: [], prefer: server}
  M3: {title: "protocol core", weight: heavy, needs: [rust], prefer: laptop}
  M1: {title: "server side", weight: heavy, needs: [rust], depends_on: [M3], prefer: desktop}
merge:
  depends_on: [M0, M1, M3]
  gate: .relay/demo/merge/gate.sh
"""

SPEC_YAML = """\
goal: "Read .relay/demo/{seg}/brief.md and do it."
hours: 8
artifacts:
  - {{path: notes/integ-1/integration.md, min_chars: 300}}
  - {{path: notes/reviewer-1/verdict.json, contains: ['"verdict"']}}
deliver:
  test_cmd: ["/bin/bash", ".relay/demo/{seg}/gate.sh"]
  test_timeout_s: 1800
  review_file: notes/reviewer-1/verdict.json
"""


def write_job(repo: Path, job_yaml: str = JOB_YAML, segments=('M0', 'M1', 'M3', 'merge'), job='demo'):
    """A project tree with `.relay/<job>/` as 03-interfaces section 2 lays it out."""
    root = Path(repo) / '.relay' / job
    root.mkdir(parents=True, exist_ok=True)
    (root / 'job.yaml').write_text(textwrap.dedent(job_yaml))
    for seg in segments:
        d = root / seg
        (d / 'roles').mkdir(parents=True, exist_ok=True)
        (d / 'job.spec.yaml').write_text(SPEC_YAML.format(seg=seg))
        (d / 'brief.md').write_text('# brief ' + seg + '\nOnly relative paths here.\n')
        (d / 'roster.yaml').write_text('workers: [dev-1, reviewer-1]\n')
        (d / 'roles' / 'dev-1.md').write_text('You are dev-1.\n')
        (d / 'gate.sh').write_text('#!/bin/bash\n"$RELAY_PYTHON" -m pytest -q tests\n')
    return root


MACHINES = {
    'laptop': {'cores': 10, 'perf_cores': 4, 'mem_gb': 32, 'disk_free_gb': 59, 'always_on': False,
               'toolchains': {'rust': '1.83', 'python': '3.12', 'node': '22.1'}},
    'desktop': {'cores': 16, 'perf_cores': 12, 'mem_gb': 128, 'disk_free_gb': 132, 'always_on': True,
                'toolchains': {'python': '3.12', 'node': '18.2'}},
    'server': {'cores': 10, 'perf_cores': 4, 'mem_gb': 16, 'disk_free_gb': 48, 'always_on': True,
               'toolchains': {'python': '3.12'}},
}


def node_rec(node, state='online', machine=None, **extra):
    rec = {'node': node, 'beat': 1, 'state': state, 'tasks': [], 'slots': [],
           'machine': copy.deepcopy(machine if machine is not None else MACHINES[node]),
           'idle': True, 'role': None}
    rec.update(extra)
    return rec
