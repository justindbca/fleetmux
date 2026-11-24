"""Decomposition tree (docs/design.md §62): the supervisor hands in a tree first and dispatches its blocks.

Switch: block ``decompose`` of fleetmux/config/fleet.yaml, ``enabled`` (read once per task at submit and kept in the
task hash as ``decompose``). Off: everything is as before (``fx:items`` dispatch, dispatch ``leaf``, contracts, the
long-running nudge). On, in task mode:

- ``fx:tree`` holds the plan and the actuals (one JSON document), mirrored to ``<run>/tree.json`` and
  ``<workspace>/tree.json``; ``fx:items`` keeps who holds a task id now, unchanged.
- The supervisor's first RUNNING reply hands in the tree only (``tree``, no ``dispatch``). Until the program has
  accepted that first tree (``accepted`` = its revision), no reply may dispatch; a valid tree counts as the reply's
  progress, so "no worker is busy: RUNNING must dispatch" does not refuse it.
- A dispatch id must be a block of the tree: a node of this fleet's level with no children.
- Each fleet's tree has one level (user ruling: every supervisor splits only its own level): the root (this fleet's
  task; ``root`` in a top fleet, the task id in a sub-fleet) and its children with ids of this level (``M<n>``, or
  ``<task id>.<n>`` in a sub-fleet). Deeper nodes are written by the program only.

This module is pure (no Redis, no fleet import) except ``block`` / ``enabled``, which read the configuration.
"""
import copy
import json
import os
import re

from fleetmux import contracts

TREE = 'fx:tree'                # the tree (JSON string)
TREE_NOTES = 'fx:tree:notes'    # lines appended to the supervisor's next message
TREE_SUMMARY = 'fx:tree:summary'  # the opening summary waiting for the tick: {text, rev, sent_at}

CUTS = ('function', 'decision', 'serial')
SERIAL_REASONS = ('integration', 'exploratory')
CAUSES = ('shared_decision', 'file_overlap', 'missing_dep', 'scope', 'other')
DECISION_STATUS = ('open', 'decided', 'pending_user')
LIST_FIELDS = ('owns', 'decides', 'consumes', 'after', 'depends_on', 'merge_of')
TEXT_FIELDS = ('goal', 'accept')
WRITABLE = ('id', 'parent', 'goal', 'cut', 'est', 'accept', 'claim', 'serial_reason', 'split_from') + LIST_FIELDS
PROGRAM = ('leaf', 'leaf_reason', 'critical', 'bound', 'state', 'to', 'actual')
TUNE_PARAMS = ('split.k', 'split.margin', 'split.floor', 'cost.cold_start', 'cost.per_decision', 'cost.merge',
               'cost.supervisor', 'cost.spawn', 'overrun_ratio', 'returns_alert')
MAX_NODES = 200
TEXT_MAX = 2000
ID_RE = r'[A-Za-z][A-Za-z0-9_.-]{0,39}'

DEFAULTS = {'enabled': False, 'unit': 'minutes', 'split': {'k': 2, 'margin': None, 'floor': 2},
            'cost': {'cold_start': None, 'per_decision': None, 'merge': None, 'supervisor': None, 'spawn': 0.3},
            'overrun_ratio': 5, 'returns_alert': 3, 'write_back_seconds': 300, 'session_minute_tokens': None}


# ------------------------------------------------------------------ configuration
def _number(value, low=0.0, above=False, integer=False, null=False):
    if value is None:
        return null
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return False
    if integer and int(value) != value:
        return False
    return value > low if above else value >= low


def validate_block(block):
    """Errors of a ``decompose`` block ([] = fine). Unknown keys and bad values are errors."""
    if not isinstance(block, dict):
        return ['fleet.yaml#decompose must be a mapping']
    errors = ['fleet.yaml#decompose.' + k + ' is not a decompose key' for k in sorted(set(block) - set(DEFAULTS))]
    if 'enabled' in block and type(block['enabled']) is not bool:
        errors.append('fleet.yaml#decompose.enabled must be true or false')
    if block.get('unit', 'minutes') != 'minutes':
        errors.append('fleet.yaml#decompose.unit must be minutes')
    for name, keys in (('split', ('k', 'margin', 'floor')),
                       ('cost', ('cold_start', 'per_decision', 'merge', 'supervisor', 'spawn'))):
        sub = block.get(name, {})
        if not isinstance(sub, dict):
            errors.append('fleet.yaml#decompose.' + name + ' must be a mapping')
            continue
        for k in sorted(set(sub) - set(keys)):
            errors.append('fleet.yaml#decompose.' + name + '.' + k + ' is not a decompose key')
        for k in keys:
            if k in sub and not _number(sub[k], above=(name, k) == ('split', 'k'), null=(name, k) != ('split', 'k')):
                errors.append('fleet.yaml#decompose.' + name + '.' + k + ' must be a number >= 0'
                              + (' (k > 0)' if k == 'k' else ' or null'))
    checks = (('overrun_ratio', dict(low=1, above=True)), ('returns_alert', dict(low=1, integer=True)),
              ('write_back_seconds', dict(low=10)), ('session_minute_tokens', dict(null=True)))
    for k, rule in checks:
        if k in block and not _number(block[k], **rule):
            errors.append('fleet.yaml#decompose.' + k + ' has a bad value: ' + repr(block[k]))
    return errors


def merged(block):
    """``block`` laid over DEFAULTS (``split`` and ``cost`` key by key)."""
    out = copy.deepcopy(DEFAULTS)
    for k, v in (block or {}).items():
        if k in ('split', 'cost') and isinstance(v, dict):
            out[k].update(v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def block():
    """The validated ``decompose`` block of the merged configuration, over DEFAULTS. A bad block raises ValueError."""
    from fleetmux import fleet_config
    raw = fleet_config.block('decompose') or {}
    errors = validate_block(raw)
    if errors:
        raise ValueError('; '.join(errors))
    return merged(raw)


def enabled():
    """The switch. An unreadable or bad block is the old path (False)."""
    try:
        return block()['enabled'] is True
    except (OSError, ValueError, KeyError):
        return False


PARAMS_KEY = 'fx:decompose:params'  # the decompose block while the fleet runs (JSON string)
META_KEY = 'fx:decompose:meta'      # hash: write_back_seconds, loaded_at, flushed_at, dirty ("1" / "0")


def params_load(r, now=None):
    """Daemon start and resume: copy the configuration's block into Redis, unless Redis holds a value changed and
    not yet written back (that one is kept). As fleetmux/fleet_superpowers_hot.py ``load``."""
    import time
    now = time.time() if now is None else now
    meta = r.hgetall(META_KEY) or {}
    if not (meta.get('dirty') == '1' and r.get(PARAMS_KEY) is not None):
        r.set(PARAMS_KEY, json.dumps(block(), ensure_ascii=False))
        r.hset(META_KEY, mapping={'dirty': '0'})
    r.hset(META_KEY, mapping={'write_back_seconds': str(params_get(r)['write_back_seconds']), 'loaded_at': str(now),
                              'flushed_at': meta.get('flushed_at') or str(now)})


def params_get(r):
    """The block from Redis (over DEFAULTS); from the configuration when it was never loaded."""
    raw = r.get(PARAMS_KEY)
    if raw is None:
        return block()
    return merged(json.loads(raw))


def params_put(r, value):
    """Replace the block in Redis and mark it for write-back. A bad block raises ValueError."""
    errors = validate_block(value)
    if errors:
        raise ValueError('; '.join(errors))
    r.set(PARAMS_KEY, json.dumps(merged(value), ensure_ascii=False))
    r.hset(META_KEY, mapping={'dirty': '1'})


TUNED_KEY = 'fx:decompose:tuned'    # list of JSON: the tune items applied in this fleet (its own and forwarded)


def tune_apply(r, tune, origin):
    """Apply ``decompose_tune`` items to the block in Redis (marked for write-back) and record them with the fleet
    they came from. ValueError on a bad item: nothing is applied."""
    validate_tune(tune)
    params = copy.deepcopy(params_get(r))
    for item in tune:
        section, _, name = item['param'].partition('.')
        if name:
            params[section] = dict(params.get(section) or {}, **{name: item['value']})
        else:
            params[section] = item['value']
    params_put(r, params)
    rows = [dict({k: item[k] for k in ('param', 'value', 'reason')}, origin=item.get('origin') or origin)
            for item in tune]
    for row in rows:
        r.rpush(TUNED_KEY, json.dumps(row, ensure_ascii=False))
    return rows


def tuned(r):
    out = []
    for raw in r.lrange(TUNED_KEY, 0, -1):
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return out


def params_flush(r, now=None, force=False, path=None):
    """Write a dirty block back to the private fleet.yaml once write_back_seconds passed since the last write-back
    (or ``force``); True when it was written. The caller decides who may write (a root fleet only)."""
    import time
    from fleetmux import fleet_config
    now = time.time() if now is None else now
    meta = r.hgetall(META_KEY) or {}
    if not meta:
        return False
    interval = float(meta.get('write_back_seconds') or DEFAULTS['write_back_seconds'])
    if not force and now - float(meta.get('flushed_at') or 0) < interval:
        return False
    written = False
    if meta.get('dirty') == '1' and r.get(PARAMS_KEY) is not None:
        fleet_config.write_user_block('decompose', json.loads(r.get(PARAMS_KEY)), path=path)
        written = True
        r.hset(META_KEY, mapping={'dirty': '0'})
    r.hset(META_KEY, mapping={'flushed_at': str(now)})
    return written


def cost(params, name):
    value = (params.get('cost') or {}).get(name)
    return float(value) if value is not None else 0.0


def split_param(params, name):
    value = (params.get('split') or {}).get(name)
    return float(value) if value is not None else 0.0


# ------------------------------------------------------------------ tree
def new_tree(root, goal):
    return {'rev': 0, 'accepted': 0, 'root': root, 'goal': goal, 'decisions': [],
            'nodes': [{'id': root, 'parent': None, 'goal': goal, 'cut': None, 'leaf': False, 'state': 'planned'}],
            'splits': [], 'chain': {'ids': [], 'minutes': 0}, 'width': 0, 'review': None, 'params_rev': ''}


def blank_actual():
    return {'minutes': 0, 'session_minutes': 0, 'returns': 0, 'causes': {}, 'started': 0, 'ended': 0,
            'dispatches': 0, 'overrun_noted': False}


def by_id(tree):
    return {n['id']: n for n in tree['nodes']}


def level_re(prefix):
    return re.escape(prefix) + r'\d+'


def level_nodes(tree):
    """The nodes of this fleet's level: the root's children."""
    return [n for n in tree['nodes'] if n.get('parent') == tree['root']]


def children(tree, nid):
    return [n['id'] for n in tree['nodes'] if n.get('parent') == nid]


def _strings(value):
    return isinstance(value, list) and all(isinstance(x, str) and x.strip() for x in value)


def _node_errors(item, ctx):
    """Shape of one node entry the supervisor sent (before it is merged)."""
    nid = item.get('id')
    if not isinstance(nid, str) or not nid:
        return ['every tree item needs an id (a node) or a decision object']
    errors = [k + ' is written by the program, not in "tree" (' + nid + ')' for k in PROGRAM if k in item]
    errors += [nid + ': unknown tree field ' + k for k in sorted(set(item) - set(WRITABLE) - set(PROGRAM))]
    if nid != ctx['root'] and not re.fullmatch(level_re(ctx['prefix']), nid):
        errors.append(nid + ' is not an id of this level (' + ctx['prefix'] + '<n>): split only your own level; '
                      'a block that needs splitting goes to a sub-fleet')
    if 'parent' in item and item['parent'] not in (None, ctx['root']):
        errors.append(nid + ': parent must be the root ' + ctx['root'] + ' (one level per fleet)')
    if 'cut' in item and item['cut'] not in CUTS:
        errors.append(nid + ': cut must be one of ' + ', '.join(CUTS))
    for k in TEXT_FIELDS:
        if k in item and (not isinstance(item[k], str) or len(item[k]) > TEXT_MAX):
            errors.append(nid + ': ' + k + ' must be text (<= ' + str(TEXT_MAX) + ' chars)')
    for k in LIST_FIELDS:
        if k in item and not _strings(item[k]):
            errors.append(nid + ': ' + k + ' must be a list of non-empty strings')
    est = item.get('est')
    if 'est' in item and not (isinstance(est, dict) and set(est) <= {'minutes', 'sessions'}
                              and _number(est.get('minutes'), above=True)
                              and _number(est.get('sessions', 1), low=1, integer=True)):
        errors.append(nid + ': est must be {"minutes": > 0, "sessions": integer >= 1}')
    claim = item.get('claim')
    if 'claim' in item and not (isinstance(claim, dict) and set(claim) <= {'leaf', 'reason'}
                                and type(claim.get('leaf')) is bool and isinstance(claim.get('reason', ''), str)):
        errors.append(nid + ': claim must be {"leaf": true/false, "reason": "..."}')
    if 'serial_reason' in item and item['serial_reason'] not in SERIAL_REASONS + (None,):
        errors.append(nid + ': serial_reason must be integration or exploratory')
    split = item.get('split_from')
    if 'split_from' in item and split is not None and not (
            isinstance(split, dict) and isinstance(split.get('id'), str) and set(split) <= {'id', 'dT', 'dC'}
            and _number(split.get('dT')) and _number(split.get('dC'))):
        errors.append(nid + ': split_from must be {"id": "<block>", "dT": minutes, "dC": session minutes}')
    return errors


def _decision_errors(d):
    if not isinstance(d, dict) or not isinstance(d.get('id'), str) or not re.fullmatch(ID_RE, d['id']):
        return ['a tree decision needs an id (e.g. D1)']
    errors = ['decision ' + d['id'] + ': unknown field ' + k
              for k in sorted(set(d) - {'id', 'q', 'owner', 'status', 'answer'})]
    if not isinstance(d.get('q', ''), str) or not isinstance(d.get('answer', ''), str):
        errors.append('decision ' + d['id'] + ': q and answer must be text')
    if 'status' in d and d['status'] not in DECISION_STATUS:
        errors.append('decision ' + d['id'] + ': status must be one of ' + ', '.join(DECISION_STATUS))
    return errors


def apply(tree, items, drop, ctx):
    """A new tree: ``items`` (nodes by id, added or changed field by field; ``{"decision": {...}}`` entries by id)
    applied to a copy of ``tree``, ``drop`` removed, then the shape checks. Raises ValueError with every error."""
    if not isinstance(items, list) or not all(isinstance(x, dict) for x in items):
        raise ValueError('tree must be a list of nodes / {"decision": {...}} objects')
    if not isinstance(drop, list) or not all(isinstance(x, str) for x in drop):
        raise ValueError('tree_drop must be a list of block ids')
    t = copy.deepcopy(tree)
    nodes, errors, seen = by_id(t), [], set()
    decisions = {d['id']: d for d in t['decisions']}
    for item in items:
        if set(item) == {'decision'}:
            errs = _decision_errors(item['decision'])
            errors += errs
            if not errs:
                d = dict(decisions.get(item['decision']['id']) or {'q': '', 'owner': '', 'status': 'open',
                                                                     'answer': ''}, **item['decision'])
                decisions[d['id']] = d
            continue
        errs = _node_errors(item, ctx)
        errors += errs
        if errs:
            continue
        nid = item['id']
        if nid in seen:
            errors.append(nid + ' appears twice in one tree reply')
            continue
        seen.add(nid)
        if nid == ctx['root']:
            errors.append(ctx['root'] + ' is the root (this fleet\'s task): it is not changed in "tree"')
            continue
        node = nodes.get(nid)
        if node is None:
            node = {'id': nid, 'parent': ctx['root'], 'goal': '', 'cut': None, 'state': 'planned',
                    'actual': blank_actual()}
            for k in LIST_FIELDS:
                node[k] = []
            nodes[nid] = node
            t['nodes'].append(node)
        node.update({k: copy.deepcopy(v) for k, v in item.items() if k != 'parent'})
        node['parent'] = ctx['root']
    for nid in drop:
        node = nodes.get(nid)
        if node is None:
            errors.append('tree_drop: ' + nid + ' is not in the tree')
        elif nid == ctx['root']:
            errors.append('tree_drop: the root cannot be dropped')
        elif children(t, nid):
            errors.append('tree_drop: ' + nid + ' has children')
        elif node.get('state') in ('running', 'delegated'):
            errors.append('tree_drop: ' + nid + ' is ' + node['state'] + '; drop it when it has reported')
    dropped = set(drop)
    for item in items:
        for k in ('merge_of',):
            for other in item.get(k) or []:
                if other not in dropped:
                    errors.append(item.get('id', '?') + ': merge_of ' + other + ' must be in tree_drop of this reply')
        split = item.get('split_from')
        if isinstance(split, dict) and split.get('id') not in dropped:
            errors.append(item.get('id', '?') + ': split_from ' + str(split.get('id'))
                          + ' must be in tree_drop of this reply')
        if item.get('merge_of'):
            union = {p for m in item['merge_of'] for p in (nodes.get(m) or {}).get('owns') or []}
            missing = sorted(union - set(nodes[item['id']].get('owns') or []))
            if missing:
                errors.append(item['id'] + ': a merge owns what the merged blocks owned; missing ' + ', '.join(missing))
    t['nodes'] = [n for n in t['nodes'] if n['id'] not in dropped]
    t['decisions'] = list(decisions.values())
    if errors:
        raise ValueError('; '.join(dict.fromkeys(errors)))
    errors = shape_errors(t, ctx)
    if errors:
        raise ValueError('; '.join(dict.fromkeys(errors)))
    _split_check(tree, t, [i for i in items if i.get('id') in seen], ctx)
    return t


def _split_check(old, new, items, ctx):
    """A split in the tree (pieces with split_from, the block dropped) passes the program's own account, else
    ValueError; a passing one is recorded in ``splits`` with the supervisor's numbers beside the program's."""
    groups = {}
    for item in items:
        if isinstance(item.get('split_from'), dict):
            groups.setdefault(item['split_from']['id'], []).append(item)
    new_decisions = len({d['id'] for d in new['decisions']} - {d['id'] for d in old['decisions']})
    nodes = by_id(new)
    for of, entries in groups.items():
        if of not in {n['id'] for n in level_nodes(old)}:
            raise ValueError('split_from ' + of + ' is not a block of the tree')
        ids = [e['id'] for e in entries]
        pieces = [{'id': i, 'est': nodes[i].get('est'), 'after': nodes[i].get('after') or []} for i in ids]
        acc = split_account(old, of, pieces, ctx['params'], new_decisions=new_decisions)
        claimed = {k: entries[0]['split_from'].get(k) for k in ('dT', 'dC')}
        if not acc['split']:
            raise ValueError('split of ' + of + ' refused: ' + acc['reason'] + " (the program's account; yours: dT "
                             + _fmt(claimed['dT']) + ', dC ' + _fmt(claimed['dC']) + ')')
        new.setdefault('splits', []).append({'of': of, 'pieces': ids, 'dT': acc['dT'], 'dC': acc['dC'],
                                             'claimed': claimed, 'by': 'supervisor',
                                             'est': copy.deepcopy(by_id(old)[of].get('est'))})


def _norm(path):
    return os.path.normpath(path.strip()).rstrip('/')


def _overlap(a, b):
    a, b = _norm(a), _norm(b)
    return a == b or a.startswith(b + '/') or b.startswith(a + '/')


def edges(tree):
    """{block: set(blocks it waits for)} over this level: after, depends_on, and the owner of an open decision
    it consumes."""
    level = {n['id'] for n in level_nodes(tree)}
    owner = {d['id']: d.get('owner') for d in tree['decisions'] if d.get('status', 'open') == 'open'}
    out = {}
    for n in level_nodes(tree):
        waits = set(n.get('after') or []) | set(n.get('depends_on') or [])
        waits |= {owner[d] for d in n.get('consumes') or [] if owner.get(d) in level}
        out[n['id']] = {w for w in waits if w in level and w != n['id']}
    return out


def _reach(graph):
    """{node: every node it waits for, transitively}."""
    memo = {}

    def up(n, stack=()):
        if n in memo:
            return memo[n]
        if n in stack:
            return set()
        out = set()
        for m in graph.get(n, ()):
            out |= {m} | up(m, stack + (n,))
        memo[n] = out
        return out
    return {n: up(n) for n in graph}


def shape_errors(tree, ctx):
    """What the program can check about the shape (never whether the split is good)."""
    errors = []
    nodes, level = by_id(tree), level_nodes(tree)
    if len(tree['nodes']) > MAX_NODES:
        errors.append('the tree has more than ' + str(MAX_NODES) + ' nodes')
    ids = {n['id'] for n in level}
    decisions = {d['id']: d for d in tree['decisions']}
    deciders = {}
    for n in level:
        for d in n.get('decides') or []:
            deciders.setdefault(d, []).append(n['id'])
    for did, d in decisions.items():
        owner = d.get('owner')
        if not owner:
            errors.append('decision ' + did + ' needs an owner (supervisor or the block that decides it)')
        elif owner != 'supervisor' and owner not in ids:
            errors.append('decision ' + did + ': owner ' + owner + ' is not a block of the tree')
        elif owner != 'supervisor' and did not in (nodes[owner].get('decides') or []):
            errors.append('decision ' + did + ': its owner ' + owner + ' must list it in decides')
        if d.get('status') in ('decided', 'pending_user') and not (d.get('answer') or '').strip():
            errors.append(did + ' is ' + d['status'] + ': give its answer' + (' (your conservative pick)'
                                                                              if d['status'] == 'pending_user' else ''))
    for did, who in deciders.items():
        if did not in decisions:
            errors.append(who[0] + ' decides ' + did + ', which is not a decision in the tree')
        if len(who) > 1:
            errors.append(did + ' is decided by ' + ' and '.join(sorted(who)) + ': one decision, one block')
        elif did in decisions and decisions[did].get('owner') not in (who[0], None, ''):
            errors.append(did + ' is decided by ' + who[0] + ' but its owner is ' + str(decisions[did].get('owner')))
    floor = split_param(ctx['params'], 'floor')
    for n in level:
        nid = n['id']
        for d in n.get('consumes') or []:
            if d not in decisions:
                errors.append(nid + ' consumes ' + d + ', which is not a decision in the tree')
        for k in ('after', 'depends_on'):
            for m in n.get(k) or []:
                if m not in ids:
                    errors.append(nid + ': ' + k + ' ' + m + ' is not a block of the tree')
        if n.get('depends_on') and n.get('serial_reason') not in SERIAL_REASONS:
            errors.append(nid + ': depends_on needs serial_reason (integration / exploratory); if it only needs a '
                          'shape, consume the decision instead')
        if not children(tree, nid):
            if not isinstance(n.get('est'), dict) or not _number(n['est'].get('minutes'), above=True):
                errors.append(nid + ' needs est.minutes (a block is dispatched with an estimate)')
            if not (n.get('accept') or '').strip():
                errors.append(nid + ' needs accept (how you will accept the block)')
            minutes = (n.get('est') or {}).get('minutes')
            if (floor and _number(minutes, above=True) and minutes < floor
                    and any(m['id'] != nid for m in level)):
                errors.append(nid + ' (est ' + _fmt(minutes) + ' min) is below floor ' + _fmt(floor)
                              + ' min: merge it into a sibling ("merge_of")')
    graph = edges(tree)
    cycle = contracts.find_cycle(graph)
    if cycle:
        errors.append('the tree has a cycle: ' + cycle)
        return errors
    reach = _reach(graph)
    for i, a in enumerate(level):
        for b in level[i + 1:]:
            if a['id'] in reach[b['id']] or b['id'] in reach[a['id']]:
                continue
            both = sorted({p for p in a.get('owns') or [] for q in b.get('owns') or [] if _overlap(p, q)})
            if both:
                errors.append(a['id'] + ' and ' + b['id'] + ' both own ' + ', '.join(both) + ' and run in parallel: '
                              'give it to one, add "after", or merge them')
    return errors


# ------------------------------------------------------------------ the longest chain, the width, the leaf judgement
def _fmt(x):
    x = round(float(x), 1)
    return str(int(x)) if x == int(x) else str(x)


def _num(x):
    x = round(float(x), 1)
    return int(x) if x == int(x) else x


def weight(node, now=None):
    """Remaining minutes of a block: 0 once reported; est minus the time it ran (at least 1) while out."""
    est = float((node.get('est') or {}).get('minutes') or 0)
    state = node.get('state', 'planned')
    if state == 'reported':
        return 0.0
    started = float((node.get('actual') or {}).get('started') or 0)
    if state in ('running', 'delegated') and started and now:
        return max(est - (now - started) / 60.0, 1.0)
    return est


def schedule(tree, now=None, override=None):
    """Earliest start / finish over this level with unlimited workers. ``override``: {id: minutes}.
    Returns {T, minutes, chain, slack, critical, width, es, ef}."""
    graph = edges(tree)
    nodes = by_id(tree)
    w = {n: (override or {}).get(n, weight(nodes[n], now)) for n in graph}
    order, done = [], set()
    while len(order) < len(graph):
        ready = sorted(n for n in graph if n not in done and graph[n] <= done)
        if not ready:  # a cycle (refused by the shape check): stop here
            break
        order += ready
        done |= set(ready)
    es, ef = {}, {}
    for n in order:
        es[n] = max((ef[m] for m in graph[n]), default=0.0)
        ef[n] = es[n] + w[n]
    total = max(ef.values(), default=0.0)
    succ = {n: [m for m in order if n in graph[m]] for n in order}
    lf = {}
    for n in reversed(order):
        lf[n] = min((lf[m] - w[m] for m in succ[n]), default=total)
    slack = {n: round(lf[n] - ef[n], 6) for n in order}
    critical = {n for n in order if slack[n] == 0 and w[n] > 0}
    chain, cur = [], None
    starts = sorted((n for n in critical if es[n] == 0), key=lambda n: n)
    cur = starts[0] if starts else None
    while cur is not None:
        chain.append(cur)
        nxt = sorted(m for m in succ[cur] if m in critical and abs(es[m] - ef[cur]) < 1e-9)
        cur = nxt[0] if nxt else None
    points = sorted({es[n] for n in order if w[n] > 0})
    width = max((sum(1 for n in order if w[n] > 0 and es[n] <= p < ef[n]) for p in points), default=0)
    return {'T': total, 'minutes': _num(total), 'chain': chain, 'slack': {n: _num(s) for n, s in slack.items()},
            'critical': critical, 'width': max(width, 1 if order else 0), 'es': es, 'ef': ef}


def _est(piece):
    est = piece.get('est')
    return float(est.get('minutes') or 0) if isinstance(est, dict) else float(est or 0)


def _chain(minutes, after):
    """Longest path over ``minutes`` {id: m} with ``after`` {id: set(ids before it)}."""
    memo = {}

    def end(n, stack=()):
        if n not in memo:
            memo[n] = minutes[n] + max((end(m, stack + (n,)) for m in after.get(n, ()) if m not in stack), default=0.0)
        return memo[n]
    return max((end(n) for n in minutes), default=0.0)


def split_account(tree, nid, pieces, params, subfleet=False, new_decisions=0, now=None):
    """The two accounts of splitting block ``nid`` into ``pieces`` ([{id, est, after: [piece ids]}]):
    dT = minutes the whole task finishes earlier: the longest chain with ``nid`` replaced by the pieces' own chain
    plus the merge (and the spawn of a sub-fleet), against the chain as it is; 0 when ``nid`` is off the chain.
    dC = extra session minutes: in this fleet cold_start x (n - 1) (the block's worker keeps one piece); a
    sub-fleet: spawn + cold_start x n + its supervisor x the sub-chain; both + per_decision x new decisions + merge.
    Split only when dT > 0, dT >= k x dC and dT >= margin. Returns {dT, dC, split, reason}."""
    s = schedule(tree, now)
    k, margin = split_param(params, 'k'), split_param(params, 'margin')
    merge, n = cost(params, 'merge'), len(pieces)
    minutes = {p['id']: _est(p) for p in pieces}
    sub = _chain(minutes, {p['id']: set(p.get('after') or []) & set(minutes) for p in pieces}) + merge
    if subfleet:
        d_c = cost(params, 'spawn') + cost(params, 'cold_start') * n + cost(params, 'supervisor') * sub
    else:
        d_c = cost(params, 'cold_start') * max(n - 1, 0)
    d_c += cost(params, 'per_decision') * new_decisions + merge
    if nid not in s['critical']:
        return {'dT': 0, 'dC': _num(d_c), 'split': False,
                'reason': 'off the longest chain (slack ' + _fmt(s['slack'].get(nid, 0)) + ' min): dT = 0'}
    replaced = schedule(tree, now, {nid: sub + (cost(params, 'spawn') if subfleet else 0.0)})
    d_t = max(s['T'] - replaced['T'], 0.0)
    a, c = round(d_t, 6), round(k * d_c, 6)
    if a <= 0 or a < c:
        reason, split = 'dT ' + _fmt(d_t) + ' < k x dC = ' + _fmt(k) + ' x ' + _fmt(d_c), False
    elif a < round(margin, 6):
        reason, split = 'dT ' + _fmt(d_t) + ' < margin ' + _fmt(margin), False
    else:
        reason, split = 'dT ' + _fmt(d_t) + ' >= k x dC = ' + _fmt(k) + ' x ' + _fmt(d_c), True
    return {'dT': _num(d_t), 'dC': _num(d_c), 'split': split, 'reason': reason}


def judge(tree, params, now=None):
    """Re-judge ``leaf`` / ``critical`` / ``bound`` of every block of this level and the tree's chain and width, on
    the tree as it is now (in place; returns the tree). A block off the longest chain saves nothing when split
    (dT = 0): a leaf. On the chain the program bounds the best cut (two halves plus a merge, in this fleet): dT at
    most what that saves, dC at least one cold start plus one merge; when even that does not pay
    (dT < k x dC, or dT < margin, or dT = 0) the block is a leaf; otherwise the supervisor's claim decides, and
    without one the block is not a leaf (a split may pay)."""
    s = schedule(tree, now)
    k = split_param(params, 'k')
    nodes = by_id(tree)
    for n in level_nodes(tree):
        nid = n['id']
        n['critical'] = nid in s['critical']
        n.pop('bound', None)
        if children(tree, nid):
            n['leaf'], n['leaf_reason'] = False, 'split into ' + ', '.join(children(tree, nid))
            continue
        if n.get('state') == 'reported':
            n['leaf'], n['leaf_reason'] = True, 'reported'
            continue
        if nid not in s['critical']:
            n['leaf'], n['leaf_reason'] = True, ('off the longest chain (slack ' + _fmt(s['slack'].get(nid, 0))
                                                 + ' min): dT = 0')
            continue
        w = weight(nodes[nid], now)
        acc = split_account(tree, nid, [{'id': 'h0', 'est': w / 2}, {'id': 'h1', 'est': w / 2}], params, now=now)
        d_t, d_c = acc['dT'], acc['dC']
        n['bound'] = {'dT': d_t, 'dC': d_c}
        bound = 'dT <= ' + _fmt(d_t) + ' min, dC >= ' + _fmt(d_c) + ' session min (k=' + _fmt(k) + ')'
        claim = n.get('claim') or {}
        if not acc['split']:
            n['leaf'], n['leaf_reason'] = True, 'on the longest chain; a split does not pay: ' + bound
        elif claim.get('leaf') is True:
            n['leaf'] = True
            n['leaf_reason'] = 'supervisor: ' + (claim.get('reason') or '-') + '; program bound ' + bound
        else:
            n['leaf'] = False
            n['leaf_reason'] = 'on the longest chain: a split may pay (' + bound + '); split it or dispatch as is'
    tree['chain'] = {'ids': s['chain'], 'minutes': s['minutes']}
    tree['width'] = s['width']
    return tree


def review(tree, now=None):
    """The review table of plan 2.9 (recomputed on every save, final in result.json); only numbers, no verdict.
    returns: each block's returns by cause class. parallel: wall minutes (first start to last end, now while a block
    is out) / minutes of the longest chain over the actual durations (``fill``; 1 = the chain alone set the pace),
    with the sum of block minutes beside it. splits: each split's extra session minutes (actual session minutes
    minus the estimate it replaced) against the minutes it saved (estimate minus actual wall minutes); a split
    rolled back saved nothing and cost what its sub-fleet spent."""
    import time
    now = time.time() if now is None else now
    level = level_nodes(tree)
    nodes = by_id(tree)
    by_block, by_cause = {}, {}
    for n in level:
        actual = n.get('actual') or {}
        causes = {k: int(v) for k, v in sorted((actual.get('causes') or {}).items()) if v}
        if int(actual.get('returns') or 0):
            by_block[n['id']] = {'returns': int(actual['returns']), 'causes': causes}
        for k, v in causes.items():
            by_cause[k] = by_cause.get(k, 0) + v
    ran, starts, ends = {}, [], []
    for n in level:
        actual = n.get('actual') or {}
        start = float(actual.get('started') or 0)
        if not start:
            continue
        end = float(actual.get('ended') or 0) or now
        starts.append(start)
        ends.append(end)
        ran[n['id']] = (end - start) / 60.0
    wall = (max(ends) - min(starts)) / 60.0 if starts else 0.0
    s = schedule(tree, now, override={n['id']: ran.get(n['id'], 0.0) for n in level})
    parallel = {'wall_minutes': _num(wall), 'chain_minutes': s['minutes'], 'chain': s['chain'],
                'fill': _num(wall / s['T']) if s['T'] > 0 else None, 'work_minutes': _num(sum(ran.values()))}
    rows = []
    for sp in tree.get('splits') or []:
        of = nodes.get(sp.get('of')) or {}
        est = sp.get('est') or of.get('est') or {}
        est_minutes = float(est.get('minutes') or 0)
        est_session = est_minutes * float(est.get('sessions') or 1)
        row = {'of': sp.get('of'), 'by': sp.get('by'), 'pieces': list(sp.get('pieces') or []),
               'claimed': sp.get('claimed'), 'program': {'dT': sp.get('dT'), 'dC': sp.get('dC')},
               'est_minutes': _num(est_minutes), 'actual_minutes': None, 'saved_minutes': None,
               'session_minutes': None, 'extra_session_minutes': None, 'rolled_back': sp.get('rolled_back'),
               'open': False}
        if sp.get('rolled_back'):
            spent = float(sp.get('session_minutes') or 0)
            row.update(actual_minutes=None, saved_minutes=0, session_minutes=_num(spent),
                       extra_session_minutes=_num(spent))
        elif sp.get('by') == 'supervisor':  # the block was replaced by its pieces
            pieces = [nodes[p] for p in row['pieces'] if p in nodes]
            session = sum(float((p.get('actual') or {}).get('session_minutes') or 0) for p in pieces)
            done = pieces and all(p.get('state') == 'reported' for p in pieces)
            row.update(session_minutes=_num(session), extra_session_minutes=_num(session - est_session),
                       open=not done)
            if done:
                st = [float((p.get('actual') or {}).get('started') or 0) for p in pieces]
                en = [float((p.get('actual') or {}).get('ended') or 0) for p in pieces]
                minutes = (max(en) - min(x for x in st if x)) / 60.0 if any(st) else 0.0
                row.update(actual_minutes=_num(minutes), saved_minutes=_num(est_minutes - minutes))
        else:  # a worker's split: the block itself carries the sub-fleet's actuals once it returned
            actual = of.get('actual') or {}
            session = float(actual.get('session_minutes') or 0)
            done = of.get('state') == 'reported'
            row.update(session_minutes=_num(session), extra_session_minutes=_num(session - est_session),
                       open=not done)
            if done:
                minutes = float(actual.get('minutes') or 0)
                row.update(actual_minutes=_num(minutes), saved_minutes=_num(est_minutes - minutes))
        rows.append(row)
    return {'at': now, 'returns': {'total': sum(by_cause.values()), 'by_cause': dict(sorted(by_cause.items())),
                                   'by_block': by_block},
            'parallel': parallel, 'splits': rows,
            'seed': review_seed(tree, {'ids': s['chain'], 'minutes': s['minutes']})}


def _seed_value(field, value):
    """A seed field in a comparable form: est as {minutes, sessions (1)}, lists as sorted sets (owns normalised)."""
    if field == 'est':
        if isinstance(value, dict):
            return (float(value.get('minutes') or 0), float(value.get('sessions') or 1))
        return None if value is None else (float(value), 1.0)
    items = value or []
    return sorted({_norm(x) for x in items} if field == 'owns' else set(items))


def _ran(node):
    """Actual minutes of a reported block (None while it has not reported)."""
    if (node or {}).get('state') != 'reported':
        return None
    return float((node.get('actual') or {}).get('minutes') or 0)


def _ratio(actual, est):
    return round(actual / est, 2) if actual is not None and est else None


def _plan_chain(root, blocks, decisions):
    """The longest chain over ``blocks``' estimates (every block planned), as {ids, minutes}."""
    pseudo = {'root': root, 'decisions': decisions,
              'nodes': [{'id': root, 'parent': None}]
              + [dict(b, parent=root, state='planned',
                      est={'minutes': _est(b)} if not isinstance(b.get('est'), dict) else b['est']) for b in blocks]}
    s = schedule(pseudo)
    return {'ids': s['chain'], 'minutes': s['minutes']}


def review_seed(tree, actual_chain):
    """Review section ``seed`` (None without a seed): the submitter's division against the tree as it ran; numbers
    only. ``blocks``: per seed block ``{id, fate, changed, into, pieces, est_minutes, actual_minutes, ratio}``, fate
    one of kept (in the tree, no seed field changed) / changed (``changed`` lists the fields) / merged (``into`` the
    block whose merge_of names it) / split (``pieces``: the supervisor's split_from pieces, or the program's pieces
    under the block) / dropped. ``actual_minutes``: the block's own once it reported; a merge target's (the ratio
    then divides by the seed estimates of every seed block merged into it); a supervisor split's wall minutes from
    the first piece's start to the last piece's end once all reported. ``ratio`` = actual / seed estimate.
    ``added``: blocks of the tree that are neither seed blocks nor a merge or split of one. ``chain``: the longest
    chain of the seed's estimates, of the tree's estimates (``plan``) and over the actual durations (``actual``).
    ``counts``: blocks per fate and added."""
    seed = tree.get('seed')
    if not isinstance(seed, dict):
        return None
    level = {n['id']: n for n in level_nodes(tree)}
    seed_ids = [b['id'] for b in seed.get('blocks') or []]
    merged_into = {m: nid for nid, n in level.items() for m in n.get('merge_of') or [] if m in seed_ids}
    split_pieces = {}
    for nid, n in level.items():
        of = (n.get('split_from') or {}).get('id') if isinstance(n.get('split_from'), dict) else None
        if of in seed_ids and of not in level:
            split_pieces.setdefault(of, []).append(nid)
    group_est = {}
    for b in seed.get('blocks') or []:
        if b['id'] in merged_into and b['id'] not in level:
            group_est[merged_into[b['id']]] = group_est.get(merged_into[b['id']], 0.0) + _est(b)
    rows, counts, derived = [], dict.fromkeys(('kept', 'changed', 'merged', 'split', 'dropped'), 0), set()
    for b in seed.get('blocks') or []:
        nid, est = b['id'], _est(b)
        row = {'id': nid, 'fate': 'dropped', 'changed': [], 'into': None, 'pieces': [], 'est_minutes': _num(est),
               'actual_minutes': None, 'ratio': None}
        actual, base = None, est
        if nid in level:
            node = level[nid]
            row['changed'] = [k for k in SEED_FIELDS if _seed_value(k, b.get(k)) != _seed_value(k, node.get(k))]
            kids = children(tree, nid)
            row['fate'] = 'split' if kids else ('changed' if row['changed'] else 'kept')
            row['pieces'] = kids
            actual = _ran(node)
        elif nid in merged_into:
            into = merged_into[nid]
            row.update(fate='merged', into=into)
            derived.add(into)
            actual, base = _ran(level[into]), group_est.get(into) or est
        elif nid in split_pieces:
            pieces = [level[p] for p in split_pieces[nid]]
            row.update(fate='split', pieces=[p['id'] for p in pieces])
            derived.update(row['pieces'])
            if all(p.get('state') == 'reported' for p in pieces):
                st = [float((p.get('actual') or {}).get('started') or 0) for p in pieces]
                en = [float((p.get('actual') or {}).get('ended') or 0) for p in pieces]
                actual = (max(en) - min(x for x in st if x)) / 60.0 if any(st) else 0.0
        counts[row['fate']] += 1
        row.update(actual_minutes=None if actual is None else _num(actual), ratio=_ratio(actual, base))
        rows.append(row)
    added = []
    for nid, n in level.items():
        if nid in seed_ids or nid in derived:
            continue
        actual = _ran(n)
        added.append({'id': nid, 'est_minutes': _num(_est(n)), 'actual_minutes': None if actual is None
                      else _num(actual), 'ratio': _ratio(actual, _est(n))})
    counts['added'] = len(added)
    return {'by': seed.get('by'), 'at': seed.get('at'), 'blocks': rows, 'added': added, 'counts': counts,
            'chain': {'seed': _plan_chain(tree['root'], seed.get('blocks') or [], seed.get('decisions') or []),
                      'plan': _plan_chain(tree['root'], list(level.values()), tree.get('decisions') or []),
                      'actual': actual_chain}}


def waits(tree, nid):
    """Why block ``nid`` cannot start yet ('' = it can): an unreported after / depends_on, an open decision."""
    nodes = by_id(tree)
    n = nodes[nid]
    for k in ('after', 'depends_on'):
        for m in n.get(k) or []:
            if (nodes.get(m) or {}).get('state') != 'reported':
                return 'waits for ' + m + ' (' + k + ') to report first'
    open_ = {d['id']: d.get('owner') for d in tree['decisions'] if d.get('status', 'open') == 'open'}
    for d in n.get('consumes') or []:
        if d in open_:
            owner = open_[d]
            if owner == 'supervisor':
                return 'consumes ' + d + ', which you have not decided yet'
            if (nodes.get(owner) or {}).get('state') != 'reported':
                return 'consumes ' + d + ', still open with ' + owner
    return ''


def dispatchable(tree):
    return [n['id'] for n in level_nodes(tree) if not children(tree, n['id']) and n.get('state') == 'planned'
            and not waits(tree, n['id'])]


def accepted_message(tree):
    """The supervisor's ``[tree] rev <n> accepted`` line: chain, width, what can go now, and the blocks the program
    judged leaf=false (on the longest chain, a split may pay) with the bounds of the two accounts."""
    chain = tree.get('chain') or {}
    text = ('[tree] rev ' + str(tree['rev']) + ' accepted: longest chain ' + (' -> '.join(chain.get('ids') or []) or '-')
            + ' (' + _fmt(chain.get('minutes') or 0) + ' min), max parallel width ' + str(tree.get('width') or 0)
            + '; dispatchable now: ' + (', '.join(dispatchable(tree)) or 'none') + ';')
    pay = [n for n in level_nodes(tree) if n.get('leaf') is False and n.get('bound')]
    if pay:
        text += ('\nleaf=false (on the longest chain, a split may pay): '
                 + '; '.join(n['id'] + ' (dT <= ' + _fmt(n['bound']['dT']) + ' min, dC >= ' + _fmt(n['bound']['dC'])
                             + ' session min)' for n in pay)
                 + '. Split it in the tree ("split_from") or dispatch it as is.')
    return text


def summary_text(tree):
    """The opening summary for the user: first-level blocks, the longest chain, what the supervisor decided."""
    lines = ['Decomposition of: ' + tree['goal'].splitlines()[0][:300] if tree.get('goal') else 'Decomposition',
             'First-level blocks:']
    for n in level_nodes(tree):
        lines.append('- ' + n['id'] + ' ' + (n.get('goal') or '').splitlines()[0][:200] if n.get('goal') else '- ' + n['id'])
        lines[-1] += (' (' + _fmt((n.get('est') or {}).get('minutes') or 0) + ' min'
                      + (', longest chain' if n.get('critical') else '') + ')')
    chain = tree.get('chain') or {}
    lines.append('Longest chain: ' + (' -> '.join(chain.get('ids') or []) or '-') + ' (' + _fmt(chain.get('minutes') or 0)
                 + ' min); max parallel width ' + str(tree.get('width') or 0) + '.')
    mine = [d for d in tree['decisions'] if d.get('owner') == 'supervisor']
    if mine:
        lines.append('Decided by the supervisor for you (it goes on without waiting; tell it through `fleetmux '
                     'operator` to change any):')
        lines += ['- ' + d['id'] + ' ' + (d.get('q') or '') + ': ' + (d.get('answer') or '(open)')
                  + (' [your call, conservative pick]' if d.get('status') == 'pending_user' else '') for d in mine]
    return '\n'.join(lines)


# ------------------------------------------------------------------ decision keys and dispatch
def validate_keys(decision, ctx):
    """Task-mode decision keys of the tree (switch on). ``ctx``: {tree, root, prefix, params}. Puts the new tree
    (or None) in ``decision['_tree']``; returns True when the reply handed in a valid tree change (it counts as the
    reply's progress). Raises ValueError."""
    tree = ctx['tree']
    escalations = decision.get('escalations') or []
    for item in escalations:
        if not isinstance(item, dict):
            continue
        if 'cause' in item and item['cause'] not in CAUSES:
            raise ValueError('escalations: cause must be one of ' + ', '.join(CAUSES))
        if str(item.get('task', '')).startswith('blocked:') and 'cause' not in item:
            raise ValueError('escalations: a [blocked] ruling needs a cause (' + ', '.join(CAUSES) + ')')
    tune = decision.get('decompose_tune')
    if tune is not None:
        if decision.get('status') != 'COMPLETED':
            raise ValueError('decompose_tune is for the closing COMPLETED reply only')
        validate_tune(tune)
    items, drop = decision.get('tree'), decision.get('tree_drop')
    changed = bool(items) or bool(drop)
    new = apply(tree, items or [], drop or [], ctx) if changed else None
    dispatch = decision.get('dispatch') or []
    if not tree.get('accepted') and decision.get('status') == 'RUNNING' and (not items or dispatch):
        raise ValueError('decompose is on: your first reply hands in the tree only ("tree": [...], "dispatch": []); '
                         'dispatch its blocks in the next reply')
    if decision.get('status') == 'RUNNING' and dispatch:
        dispatch_errors(judge(copy.deepcopy(new or tree), ctx['params']), dispatch, ctx.get('running') or [])
    decision['_tree'] = new
    return changed


def validate_tune(tune):
    if not isinstance(tune, list) or not tune:
        raise ValueError('decompose_tune must be a non-empty list of {"param", "value", "reason"}')
    for item in tune:
        if not (isinstance(item, dict) and set(item) - {'origin'} == {'param', 'value', 'reason'}
                and item['param'] in TUNE_PARAMS
                and isinstance(item['reason'], str) and item['reason'].strip()):
            raise ValueError('decompose_tune: every item is {"param", "value", "reason"}, param one of '
                             + ', '.join(TUNE_PARAMS))
        rule = (dict(above=True) if item['param'] == 'split.k' else dict(low=1, above=True)
                if item['param'] == 'overrun_ratio' else dict(low=1, integer=True)
                if item['param'] == 'returns_alert' else {})
        if not _number(item['value'], **rule):
            raise ValueError('decompose_tune: ' + item['param'] + ' has a bad value: ' + repr(item['value']))


def dispatch_errors(tree, dispatch, running):
    """Refuse a dispatch that is not made of this tree's blocks (ValueError). ``running``: ids out now."""
    nodes, ids = by_id(tree), []
    for item in dispatch:
        nid = item.get('id')
        if not nid or not isinstance(nid, str):
            raise ValueError('decompose is on: every dispatch item needs the id of a block in the tree '
                             '(dispatch[].id)')
        node = nodes.get(nid)
        if node is None or nid == tree['root'] or node.get('parent') != tree['root']:
            raise ValueError('dispatch id ' + nid + ' is not in the tree: add it to the tree first ("tree": [{"id": "'
                             + nid + '", ...}]), then dispatch it')
        kids = children(tree, nid)
        if kids:
            raise ValueError(nid + ' has children in the tree: dispatch its blocks (' + ', '.join(kids) + ') instead')
        if node.get('state') == 'delegated':
            raise ValueError(nid + ' is delegated to sub-fleet ' + nid + '; do not dispatch it')
        if node.get('state') == 'planned':
            why = waits(tree, nid)
            if why:
                raise ValueError(nid + ' ' + why + '; dispatch it when that is done, or change the tree')
        ids.append(nid)
    out = {n for n in ids if nodes[n].get('state') != 'reported'}  # a correction of a reported block starts nothing
    out |= {n for n in running if (nodes.get(n) or {}).get('state') == 'running'}
    width = tree.get('width') or 1
    if len(out) > width:
        raise ValueError('this dispatch would run ' + str(len(out)) + ' blocks at once but the tree\'s max parallel '
                         'width is ' + str(width) + ': dispatch fewer, or change the tree')


def brief(tree, nid):
    """The line the program appends to a block's dispatch: what it owns, consumes and how it is accepted."""
    n = by_id(tree).get(nid) or {}
    parts = []
    if n.get('owns'):
        parts.append('owns ' + ', '.join(n['owns']))
    if n.get('consumes'):
        dec = {d['id']: d for d in tree['decisions']}
        parts.append('uses ' + ', '.join(d + (' = ' + dec[d]['answer'] if (dec.get(d) or {}).get('answer') else '')
                                         for d in n['consumes']))
    if n.get('accept'):
        parts.append('accepted by: ' + n['accept'])
    return ('\n[tree ' + nid + '] ' + '; '.join(parts)) if parts else ''


# ------------------------------------------------------------------ seeds (a task's tree file, a sub-fleet's parent node)
SEED_FIELDS = ('est', 'owns', 'after', 'depends_on', 'consumes', 'decides')  # compared by review()['seed']
SEED_BY = 'submitter'


def seed_by(value):
    """The ``by`` of a seed (spec key, tree-file key): a non-empty string of at most 80 characters."""
    if not isinstance(value, str) or not value.strip() or len(value) > 80:
        raise ValueError('by must be a non-empty string of at most 80 characters (who wrote the tree)')
    return value.strip()


def tree_required(what):
    """The refusal of a dev_task ``what`` (task / job) submitted without a tree while the switch is on."""
    return ('decompose is on: a dev_task ' + what + ' must bring its tree. 1) Write the division of the work as a tree '
            'file ({"nodes": [...], "decisions": [...]}); 2) run `fleetmux tree check <file>` and fix what it '
            'reports; 3) give the file\'s absolute path as "tree" in the spec (docs/design.md section 62)')


def seed_from(path, root, goal, prefix, by=None, now=None):
    """A tree from a seed file: {"nodes": [...], "decisions": [...], "by": optional}. A node without parent whose id
    is not of this level (or equals ``root``) is the root: its fields describe the task, its id becomes ``root``.
    Other nodes are first-level blocks, kept as written (they may lack est / accept: the supervisor completes them).
    The seed is not accepted: the supervisor's first reply still hands in the tree.

    ``tree['seed']`` keeps the submitter's division as written, read only (nothing else writes it):
    ``{by, at, root, blocks: [{id, goal, est, owns, after, depends_on, consumes, decides}],
    decisions: [{id, owner, status}]}``; ``by`` = the caller's ``by``, else the file's, else ``submitter``;
    ``at`` = submit time; ``root`` = the root id as the file wrote it. A file without blocks (e.g. the program's seed of
    a job that brought no tree: the root only) divides nothing: no ``seed``."""
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ValueError('tree must be an absolute path to a tree JSON file')
    try:
        data = json.loads(open(path, encoding='utf-8').read())
    except (OSError, ValueError) as exc:
        raise ValueError('tree: cannot read ' + path + ': ' + str(exc)[:200]) from None
    if not isinstance(data, dict) or not isinstance(data.get('nodes', []), list) \
            or not isinstance(data.get('decisions', []), list):
        raise ValueError('tree: the file must be {"nodes": [...], "decisions": [...]}')
    if by is None:
        by = data.get('by', SEED_BY)
    by = seed_by(by)
    t = new_tree(root, goal)
    root_node = t['nodes'][0]
    for raw in data.get('nodes', []):
        if not isinstance(raw, dict) or not isinstance(raw.get('id'), str):
            raise ValueError('tree: every node needs an id')
        if raw['id'] == root or (raw.get('parent') is None and not re.fullmatch(level_re(prefix), raw['id'])):
            root_node.update({k: copy.deepcopy(v) for k, v in raw.items() if k in ('goal', 'accept', 'est', 'owns')})
            root_node['seed_id'] = raw['id']
            continue
        if not re.fullmatch(level_re(prefix), raw['id']):
            raise ValueError('tree: ' + raw['id'] + ' is not an id of this level (' + prefix + '<n>)')
        node = {k: copy.deepcopy(v) for k, v in raw.items() if k in WRITABLE}
        node.update(parent=root, state='planned', actual=blank_actual())
        for k in LIST_FIELDS:
            node.setdefault(k, [])
        node.setdefault('cut', None)
        t['nodes'].append(node)
    for d in data.get('decisions', []):
        errs = _decision_errors(d)
        if errs:
            raise ValueError('tree: ' + errs[0])
        t['decisions'].append(dict({'q': '', 'owner': '', 'status': 'open', 'answer': ''}, **d))
    if len(t['nodes']) > MAX_NODES:
        raise ValueError('tree: more than ' + str(MAX_NODES) + ' nodes')
    import time
    if not level_nodes(t):
        return t
    t['seed'] = {'by': by, 'at': time.time() if now is None else now, 'root': root_node.get('seed_id', root),
                 'blocks': [dict({'id': n['id'], 'goal': n.get('goal', '')},
                                 **{k: copy.deepcopy(n.get(k, [] if k != 'est' else None)) for k in SEED_FIELDS})
                            for n in level_nodes(t)],
                 'decisions': [{k: d.get(k) for k in ('id', 'owner', 'status')} for d in t['decisions']]}
    return t


def renumber(data, prefix):
    """A tree file written for another level (e.g. a job's tree with M0, M1 ...) moved into level ``prefix``: a node
    that another node names as its parent is the root and stays; every other node gets ``<prefix><i>`` in file order
    (ids already of this level are kept) and every reference follows (after, depends_on, merge_of, split_from,
    parent, decision owners)."""
    nodes = [n for n in data.get('nodes') or [] if isinstance(n, dict) and isinstance(n.get('id'), str)]
    parents = {n.get('parent') for n in nodes if n.get('parent')}
    mapping, i = {}, 0
    for n in nodes:
        if n['id'] in parents:
            continue
        if not re.fullmatch(level_re(prefix), n['id']):
            while prefix + str(i) in {m['id'] for m in nodes} or prefix + str(i) in mapping.values():
                i += 1
            mapping[n['id']] = prefix + str(i)
            i += 1
    out = []
    for n in nodes:
        n = copy.deepcopy(n)
        n['id'] = mapping.get(n['id'], n['id'])
        for k in ('after', 'depends_on', 'merge_of'):
            if isinstance(n.get(k), list):
                n[k] = [mapping.get(x, x) for x in n[k]]
        if isinstance(n.get('split_from'), dict):
            n['split_from']['id'] = mapping.get(n['split_from'].get('id'), n['split_from'].get('id'))
        out.append(n)
    decisions = [dict(d, owner=mapping.get(d.get('owner'), d.get('owner'))) for d in data.get('decisions') or []
                 if isinstance(d, dict)]
    out = {'nodes': out, 'decisions': decisions}
    if 'by' in data:
        out['by'] = data['by']
    return out


def write_seed(path, root_node, nodes, decisions=(), by=None):
    """A sub-fleet's seed file: ``root_node`` (its goal / accept / est / owns) as the root, ``nodes`` as its
    first-level blocks (their writable fields), ``decisions`` as written, ``by`` (who wrote the division) when
    given."""
    data = {'nodes': [dict({k: v for k, v in root_node.items() if k in ('goal', 'accept', 'est', 'owns')},
                           id=root_node['id'])]
            + [{k: v for k, v in n.items() if k in WRITABLE} for n in nodes if n.get('id')],
            'decisions': list(decisions)}
    if by is not None:
        data['by'] = by
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        fh.write(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    os.replace(tmp, path)
    return path


# ------------------------------------------------------------------ the supervisor's role text
PROMPT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'agents', 'decompose_supervisor.md')
LEAF_HEAD = '### Leaf blocks (`dispatch[].leaf`)\n'
FORMAT_LINES = ('- `dispatch[].id`: optional task id', '- `dispatch[].leaf`: optional `true`')


def prompt_swap(text):
    """A task supervisor's role text with the tree on: the "Leaf blocks" section and the dispatch[].id /
    dispatch[].leaf format lines give way to agents/decompose_supervisor.md. A text without them (a custom role)
    gets the section appended."""
    raw = open(PROMPT_FILE, encoding='utf-8').read()
    section, fmt = raw.split('<!-- format:', 1)
    section = section.split('-->', 1)[1].strip() + '\n'
    fmt = fmt.split('-->', 1)[1].strip()
    start = text.find(LEAF_HEAD)
    if start < 0:
        return text.rstrip('\n') + '\n\n' + section
    end = text.find('\n### ', start + len(LEAF_HEAD))
    end = len(text) if end < 0 else end + 1
    text = text[:start] + section + '\n' + text[end:]
    lines = text.split('\n')
    out, placed = [], False
    for line in lines:
        if line.startswith(FORMAT_LINES):
            if not placed:
                out.append(fmt)
                placed = True
            continue
        out.append(line)
    return '\n'.join(out)


WORKER_JSON_END = '"workers": 2}'
WORKER_DELTA = ('"workers": 2, "delta_t": <minutes the whole task finishes earlier>, "delta_c": <extra session minutes: '
                'cold starts, agreeing, merging, the sub-fleet supervisor>}` (with the tree on, a split request without '
                'both numbers is refused; dT counts only if the whole task finishes earlier)')
CONTRACT_LEAF = '- `dispatch[].leaf`: `"leaf": true` on an item that is a leaf block'


def worker_swap(text):
    """A task worker's role text with the tree on: the split request JSON asks for delta_t / delta_c."""
    return text.replace(WORKER_JSON_END + '`', WORKER_DELTA, 1)


def contracts_swap(text):
    """CONTRACTS.md of a contract supervisor with the tree on: the dispatch[].leaf line points at the tree."""
    lines = text.split('\n')
    out = ['- `dispatch[].leaf`: not used while the decomposition tree is on: the program judges leaf on the tree '
           '(see "Decomposition tree" in AGENTS.md)' if line.startswith(CONTRACT_LEAF) else line for line in lines]
    return '\n'.join(out)
