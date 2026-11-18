"""Which machine takes a segment (04-design sections 2 and 10). Every machine computes the same order from the
same table and node records; the first in the order claims at once, the k-th waits (k-1) x `claim_stagger_s`.

Filter: online (not leaving, lost or rejoining), toolchains meet `needs`, disk above the floors, below the task
caps. Order: `prefer` first when it passes the filter; then heavy segments by capability score (high first),
light segments by fewest tasks in hand; ties by machine name.
"""
from . import knobs, toolchain

HELD = ('working', 'parked')          # states in which a segment occupies its owner
ROLE_TASKS = ('coordinator',)          # records that are a role, not development work


def score(machine, params):
    """The default capability score from the `score` weights; callers may pass the state layer's scoring."""
    w = knobs.get(params, 'score')
    return sum(float(w.get(k, 0)) * float(machine.get(k) or 0) for k in ('cores', 'mem_gb', 'disk_free_gb'))


weights_score = score


def counts(table):
    """{node: {'tasks': n, 'heavy': h}} of segments each node holds."""
    out = {}
    for seg, r in table.items():
        if seg in ROLE_TASKS or r.get('state') not in HELD or not r.get('owner'):
            continue
        c = out.setdefault(r['owner'], {'tasks': 0, 'heavy': 0})
        c['tasks'] += 1
        c['heavy'] += 1 if r.get('weight') == 'heavy' else 0
    return out


def candidates(nodes, judged, table):
    """[{'id', 'machine', 'tasks', 'heavy'}] for nodes judged online whose own record says `online`."""
    held = counts(table)
    out = []
    for node in sorted(nodes):
        rec = nodes[node] or {}
        if judged.get(node) != 'online' or rec.get('state') != 'online':
            continue
        c = held.get(node, {'tasks': 0, 'heavy': 0})
        out.append({'id': node, 'machine': rec.get('machine') or {}, 'tasks': c['tasks'], 'heavy': c['heavy']})
    return out


def refusal(cand, task, params):
    """Why `cand` cannot take `task` ('toolchain: <needs>', 'disk', 'full'), or None when it can."""
    m = cand['machine']
    lack = toolchain.missing(m.get('toolchains'), task.get('needs'))
    if lack:
        return 'toolchain: ' + ', '.join(lack)
    heavy = task.get('weight') == 'heavy'
    disk = float(m.get('disk_free_gb') or 0)
    if disk < knobs.get(params, 'disk_floor_any_gb'):
        return 'disk'
    if heavy and disk < knobs.get(params, 'disk_floor_heavy_gb'):
        return 'disk'
    if cand['tasks'] >= knobs.get(params, 'max_tasks_per_node'):
        return 'full'
    if heavy and cand['heavy'] >= knobs.get(params, 'max_heavy_per_node'):
        return 'full'
    return None


def rank(task, cands, params, score=None):
    """Eligible node names, best first. `score(machine, params)` defaults to the weights in `score`."""
    score = score or weights_score
    ok = [c for c in cands if refusal(c, task, params) is None]
    if task.get('weight') == 'heavy':
        ok.sort(key=lambda c: (-score(c['machine'], params), c['tasks'], c['id']))
    else:
        ok.sort(key=lambda c: (c['tasks'], -score(c['machine'], params), c['id']))
    names = [c['id'] for c in ok]
    pref = task.get('prefer')
    if pref in names:
        names = [pref] + [n for n in names if n != pref]
    return names


def unplaceable_reason(task, cands, params):
    """None when some candidate can take `task`; else the reasons, e.g. 'toolchain: rust' or 'disk; full'."""
    reasons = [refusal(c, task, params) for c in cands]
    if not cands:
        return 'no machine online'
    if any(r is None for r in reasons):
        return None
    return '; '.join(sorted(set(reasons)))


def stagger_s(order, me, params):
    """Seconds `me` waits before claiming: (k-1) x claim_stagger_s for rank k; None when not in `order`."""
    if me not in order:
        return None
    return order.index(me) * knobs.get(params, 'claim_stagger_s')


class ClaimQueue:
    """When this node first saw each claimable record version; a claim is due `wait` seconds after that."""

    def __init__(self):
        self._seen = {}

    def due(self, seg, version, wait, *, now):
        first = self._seen.get(seg)
        if first is None or first[0] != version:
            self._seen[seg] = (version, now)
            first = self._seen[seg]
        return now - first[1] >= wait

    def forget(self, seg):
        self._seen.pop(seg, None)
