"""Interface contracts for task mode (task spec ``"contracts": true``; spec docs/design.md §18).

Pure functions over one JSON state document; the daemon keeps it in one Redis string key
and copies a read-only view to ``<run>/contracts.json`` and ``<workspace>/contracts.json``.

    state = {"phase": "interfaces" | "skeleton" | "parallel",
             "skeleton_worker": "worker-1", "skeleton_tries": 0,
             "plan": [{id, goal, provides, consumes}],
             "interfaces": {"IF-M0-x@1": entry},      # registry, status declared/ready/deprecated
             "pending": {"IF-M0-x@2": entry},         # worker proposals awaiting the supervisor
             "tasks": {"M0": {id, to, status, provides, consumes, edits, depends_on,
                              serial_reason, attempts, stale, message}},
             "owners": {"worker-1": ["M0", null, ...]}}  # what each queued message expects back

Models only propose; every rule here is a literal check the daemon can repeat.
Not in this chunk: peer notices (SendMessage / {{FLEET_PEER}}). Escalate / sub-fleet interfaces: fleet.py (§20).
"""
import copy
import json
import re
from pathlib import Path

IF_RE = r'IF-M\d+(?:\.\d+)*-[a-z0-9-]+'
M_RE = r'M\d+(?:\.\d+)*'
FIELDS = ('id', 'version', 'producer', 'consumers', 'path', 'min_chars', 'contains', 'json_keys',
          'sample', 'writers', 'decisions', 'final')
SAMPLE_MAX = 4000
SAMPLE_RULE = 'sample is required and must itself satisfy min_chars / contains / json_keys'
SKELETON_TRIES = None  # user 10/13: fix interfaces until everything passes; only the task `hours` budget bounds it
SKELETON = '__skeleton__'  # owner marker of the P1 skeleton delivery
READY_RE = re.compile(r'(' + IF_RE + r')@(\d+) provided: ready\b')
REPORT_KEYS = ('interface_change', 'escalate', 'proposal', 'workers')
EXTRA_SECTIONS = re.compile(r'^[\s>*#_-]*(interfaces|peer notices|question for supervisor)[\s*_]*:[\s*_]*(.*)$', re.I)


def new_state(skeleton_worker):
    return {'phase': 'interfaces', 'skeleton_worker': skeleton_worker, 'skeleton_tries': 0, 'plan': [],
            'interfaces': {}, 'pending': {}, 'tasks': {}, 'owners': {}}


def ref(entry):
    return entry['id'] + '@' + str(entry['version'])


def split_ref(value):
    m = re.fullmatch('(' + IF_RE + ')@([1-9][0-9]*)', value) if isinstance(value, str) else None
    return (m.group(1), int(m.group(2))) if m else (None, None)


def current(state, iface):
    """The newest non-deprecated registered entry of an interface id, or None."""
    entries = [e for e in state['interfaces'].values() if e['id'] == iface and e['status'] != 'deprecated']
    return max(entries, key=lambda e: e['version']) if entries else None


# ------------------------------------------------------------------ literal checks
def text_failures(text, entry):
    """min_chars / contains / json_keys on a text (a sample or a produced file)."""
    out = []
    if len(text) < entry.get('min_chars', 1):
        out.append('shorter than min_chars ' + str(entry.get('min_chars', 1)))
    out += ['missing ' + json.dumps(c, ensure_ascii=False) for c in entry.get('contains') or [] if c not in text]
    if entry.get('json_keys'):
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
        if not isinstance(obj, dict):
            out.append('not a JSON object')
        else:
            out += ['missing key ' + k for k in entry['json_keys'] if k not in obj]
    return out


def file_failures(entry, root):
    root = Path(root).resolve()
    path = (root / entry['path']).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return [entry['path'] + ' missing']
    if path.stat().st_size > 32_000_000:
        return [entry['path'] + ' too large to check']
    return [entry['path'] + ' ' + x for x in text_failures(path.read_text(errors='replace'), entry)]


# ------------------------------------------------------------------ entries
def entry_errors(raw, task_ids, *, new=True):
    """§2.2 field rules. Returns (entry, errors); the entry keeps only known fields."""
    errors = []
    if not isinstance(raw, dict):
        return None, ['an interface must be a JSON object']
    e = {k: copy.deepcopy(raw[k]) for k in FIELDS if k in raw}
    if not isinstance(e.get('id'), str) or not re.fullmatch(IF_RE, e['id']):
        errors.append('interface id must match IF-M<n>(.<n>)*-[a-z0-9-]+ and be unique')
    if type(e.get('version')) is not int or e['version'] < 1 or (new and e['version'] != 1):
        errors.append('version must be 1 for a new interface, or current+1 for a breaking change')
    if e.get('producer') not in task_ids:
        errors.append('producer must be a task id in the task table')
    e.setdefault('consumers', [])
    if not isinstance(e['consumers'], list) or not all(c in task_ids for c in e['consumers']):
        errors.append('consumers must be a list of task ids')
    path = e.get('path')
    if not isinstance(path, str) or Path(path).is_absolute() or '..' in Path(path).parts or len(Path(path).parts) < 2:
        errors.append("path must be inside the producer's own directory")
    e.setdefault('min_chars', 1)
    if type(e['min_chars']) is not int or not 1 <= e['min_chars'] <= 8_000_000:
        errors.append('invalid min_chars')
    e.setdefault('contains', [])
    if not isinstance(e['contains'], list) or not all(isinstance(x, str) and x for x in e['contains']):
        errors.append('contains must be a list of literal strings')
    e.setdefault('json_keys', None)
    if e['json_keys'] is not None and (not isinstance(e['json_keys'], list)
                                       or not all(isinstance(x, str) and x for x in e['json_keys'])):
        errors.append('json_keys must be a list of key names')
    e.setdefault('writers', {})
    if not isinstance(e['writers'], dict) or not all(
            (v in task_ids) if isinstance(v, str) else (isinstance(v, list) and all(x in task_ids for x in v))
            for v in e['writers'].values()):
        errors.append('writers must map each field to one task id')
    e.setdefault('decisions', [])
    if not isinstance(e['decisions'], list) or not all(
            isinstance(d, dict) and isinstance(d.get('q'), str) and isinstance(d.get('answer'), str)
            and d.get('status') in ('pending_user', 'final') for d in e['decisions']):
        errors.append('decisions must be a list of {q, answer, status: pending_user|final}')
    e['final'] = e.get('final', False)
    if type(e['final']) is not bool:
        errors.append('final must be true or false')
    sample = e.get('sample')
    if not isinstance(sample, str) or not sample or len(sample) > SAMPLE_MAX:
        errors.append(SAMPLE_RULE + ' (at most ' + str(SAMPLE_MAX) + ' chars)')
    elif not errors and text_failures(sample, e):
        errors.append(SAMPLE_RULE + ' (' + '; '.join(text_failures(sample, e)) + ')')
    where = (e.get('id') if isinstance(e.get('id'), str) else 'interface') + ': '
    return e, [where + x for x in errors]


def writer_errors(e):
    """P0 check 5 (devflow rule 1): every json key has exactly one writer."""
    out = []
    for k in e.get('json_keys') or []:
        w = e['writers'].get(k)
        names = [w] if isinstance(w, str) else list(w or [])
        if not names:
            out.append('P0: field ' + k + ' of ' + e['id'] + ' has no writer')
        elif len(names) > 1:
            out.append('P0: field ' + k + ' of ' + e['id'] + ' has two writers: ' + ', '.join(names))
    return out


def classify(old, new):
    """(kind, reason): kind None = no change; breaking = moved or removed something."""
    if old['path'] != new['path']:
        return 'breaking', 'path changed'
    if set(old.get('contains') or []) - set(new.get('contains') or []):
        return 'breaking', 'contains removed'
    if set(old.get('json_keys') or []) - set(new.get('json_keys') or []):
        return 'breaking', 'json_keys removed'
    same = all(old.get(k) == new.get(k) for k in FIELDS if k != 'version')
    return (None, '') if same else ('compatible', '')


def find_cycle(edges):
    """edges: {node: set(next)}; returns 'A -> B -> A' or ''."""
    state, stack = {}, []

    def visit(n):
        state[n] = 1
        stack.append(n)
        for m in sorted(edges.get(n, ())):
            if state.get(m) == 1:
                return stack[stack.index(m):] + [m]
            if not state.get(m):
                found = visit(m)
                if found:
                    return found
        stack.pop()
        state[n] = 2
        return None

    for n in sorted(edges):
        if not state.get(n):
            found = visit(n)
            if found:
                return ' -> '.join(found)
    return ''


# ------------------------------------------------------------------ P0
P0_SHAPE = 'P0: interfaces are not fixed yet; reply with phase "interfaces", a full plan and no dispatch'


def p0(decision):
    """Validate a phase "interfaces" reply. Returns (plan, interfaces) or raises ValueError (all gaps)."""
    plan, raws = decision.get('plan'), decision.get('interfaces')
    if (decision.get('phase') != 'interfaces' or decision.get('dispatch') or not isinstance(plan, list) or not plan
            or not isinstance(raws, list) or not raws):
        raise ValueError(P0_SHAPE)
    errors, ids = [], []
    for item in plan:
        if (not isinstance(item, dict) or not isinstance(item.get('id'), str) or not re.fullmatch(M_RE, item['id'])
                or not isinstance(item.get('goal'), str) or not item['goal'].strip() or 'to' in item
                or not all(isinstance(item.get(k, []), list) for k in ('provides', 'consumes'))):
            errors.append('P0: every plan item needs id (M<n>), goal, provides and consumes lists, and no to')
            continue
        if item['id'] in ids:
            errors.append('P0: plan id ' + item['id'] + ' appears twice')
        ids.append(item['id'])
    if errors:
        raise ValueError('; '.join(errors))
    entries = {}
    for raw in raws:
        e, errs = entry_errors(raw, ids)
        if e and isinstance(e.get('sample'), str) and any(SAMPLE_RULE + ' (' in x for x in errs) and len(errs) == 1:
            errs = ['P0: ' + e['id'] + ' sample fails its own check: ' + '; '.join(text_failures(e['sample'], e))]
        errors += errs
        if e and isinstance(e.get('id'), str):
            if e['id'] in entries:
                errors.append('interface id must match IF-M<n>(.<n>)*-[a-z0-9-]+ and be unique: ' + e['id'])
            entries[e['id']] = e
            if not errs:
                errors += writer_errors(e)
    plan = [dict(item, provides=list(item.get('provides', [])), consumes=list(item.get('consumes', [])))
            for item in plan]
    consumers = {i: set(e.get('consumers') or []) for i, e in entries.items()}
    for item in plan:
        for i in item['provides']:
            if i not in entries:
                errors.append('P0: ' + item['id'] + ' provides ' + str(i) + ' which is not declared in interfaces')
            elif entries[i].get('producer') != item['id']:
                errors.append(i + ' is produced by ' + str(entries[i].get('producer')) + ', not ' + item['id'])
        for c in item['consumes']:
            i = split_ref(c)[0] or c
            if i not in entries:
                errors.append('P0: ' + item['id'] + ' consumes ' + str(c) + ' but no task provides it')
            else:
                consumers[i].add(item['id'])
    edges = {}
    for i, e in entries.items():
        if not consumers.get(i) and not e.get('final'):
            errors.append('P0: ' + i + ' is provided by ' + str(e.get('producer'))
                          + ' but nobody consumes it; add a consumer or mark final')
        for c in consumers.get(i, ()):
            edges.setdefault(e.get('producer'), set()).add(c)
    cycle = find_cycle(edges)
    if cycle:
        errors.append('P0: interface cycle: ' + cycle)
    if errors:  # every P0 gap carries the P0 prefix: P0 rejections never count toward the three strikes
        raise ValueError('; '.join(x if x.startswith('P0') else 'P0: ' + x for x in dict.fromkeys(errors)))
    for i, e in entries.items():
        e['consumers'] = sorted(consumers[i])
        e.update(status='proposed', approved_by=None, deprecated_by=None)
    return plan, {ref(e): e for e in entries.values()}


def skeleton_verdict(text, interfaces):
    """P1: one PASS line per interface, no FAIL anywhere. Returns failures (empty = pass)."""
    if text is None:
        return ['skeleton.md was not written']
    lines = text.splitlines()
    # A literal 'PASS/FAIL' (header or legend copied from the instruction) is not a verdict.
    out = ['FAIL line: ' + ' '.join(x.split()) for x in lines
           if re.search(r'\bFAIL\b', re.sub(r'PASS\s*/\s*FAIL', '', x))]
    for r in sorted(interfaces):
        if not any(r in x and re.search(r'\bPASS\b', x) for x in lines):
            out.append('no PASS line for ' + r)
    return out


# ------------------------------------------------------------------ changes (§4, single fleet)
def merge_change(state, raw, task_ids, *, worker=None):
    """Validate a new / compatible / breaking change. Returns (kind, entry) or (None, None) for a
    repeat that changes nothing. ``worker`` = (slot, task id) for a worker's interface_change."""
    who = 'interface_change' if worker else 'change'
    pre = 'interface_change: ' if worker else ''
    if not isinstance(raw, dict) or not isinstance(raw.get('id'), str):
        raise ValueError(pre + 'an interface change needs id and version')
    change = raw.get('change', 'new')
    if change not in ('new', 'compatible', 'breaking'):
        raise ValueError(pre + 'change must be new, compatible or breaking')
    cur = current(state, raw['id'])
    if cur is None:
        if change != 'new' or any(e['id'] == raw['id'] for e in state['interfaces'].values()):
            raise ValueError(pre + raw['id'] + ' is not registered; use change: new')
        if worker and raw.get('version') != 1:
            raise ValueError('interface_change: version must be 1 for change new')
        entry, errors = entry_errors(raw, task_ids)
        if errors:
            raise ValueError('; '.join(pre + x for x in errors))
        return 'new', entry
    if change == 'new':
        if raw.get('version') == cur['version'] and all(
                raw.get(k, cur.get(k)) == cur.get(k) for k in FIELDS if k in raw):
            return None, None
        raise ValueError(pre + raw['id'] + '@' + str(raw.get('version')) + ' is already registered with different '
                         'fields: bump the version')
    merged = {k: copy.deepcopy(cur[k]) for k in FIELDS if k in cur}
    merged.update({k: copy.deepcopy(raw[k]) for k in FIELDS if k in raw and k != 'version'})
    merged['version'] = raw.get('version')
    kind, reason = classify(cur, merged)
    if kind is None:
        return None, None
    if change == 'compatible' and kind == 'breaking':
        raise ValueError(who + ' declared compatible but ' + reason + ': it is breaking, version must be '
                         + str(cur['version'] + 1))
    if change == 'breaking' and kind == 'compatible':
        raise ValueError(who + ' declared breaking but nothing was moved or removed: it is compatible, keep version '
                         + str(cur['version']))
    expected = cur['version'] + (kind == 'breaking')
    if merged['version'] != expected:
        raise ValueError('interface_change: version must be ' + str(expected) + ' for change ' + kind if worker else
                         'version must be 1 for a new interface, or current+1 for a breaking change')
    entry, errors = entry_errors(merged, task_ids, new=False)
    if errors:
        raise ValueError('; '.join(pre + x for x in errors))
    return kind, entry


def approve(state, kind, entry):
    """Apply an approved change. Returns task ids to notify (producer + consumers)."""
    entry = dict(entry, approved_by='supervisor', deprecated_by=None)
    cur = current(state, entry['id'])
    if kind == 'new':
        state['interfaces'][ref(entry)] = dict(entry, status='declared')
    elif kind == 'compatible':
        added = (set(entry.get('contains') or []) - set(cur.get('contains') or [])
                 or set(entry.get('json_keys') or []) - set(cur.get('json_keys') or []))
        status = 'declared' if added else cur['status']  # new required text: the producer re-announces ready
        state['interfaces'][ref(entry)] = dict(entry, status=status)
    else:
        state['interfaces'][ref(entry)] = dict(entry, status='declared')
        cur.update(status='deprecated', deprecated_by=ref(entry))
        for task in state['tasks'].values():
            if ref(cur) in task.get('consumes') or []:
                task['stale'] = sorted(set(task.get('stale') or []) | {ref(cur)})
    old = cur or {}
    return sorted({entry['producer'], *entry.get('consumers', []), *old.get('consumers', [])})


def supervisor_interfaces(state, raws):
    """Parallel phase: this fleet's supervisor is the top supervisor, so its own declarations
    are the approval. Returns [(ref, kind or 'rejected', task ids to notify)]."""
    if raws is None:
        return []
    if not isinstance(raws, list):
        raise ValueError('interfaces must be a list')
    out = []
    for raw in raws:
        if isinstance(raw, dict) and 'decision' in raw:
            key = str(raw.get('id')) + '@' + str(raw.get('version'))
            if raw['decision'] == 'veto':  # spec §4.2: a sub-fleet's own interfaces are in effect until vetoed
                origin = (state.get('child_interfaces') or {}).pop(key, None)
                if origin is None:
                    raise ValueError('veto applies only to interfaces a sub-fleet registered; ' + key + ' is not one')
                out.append((key, 'veto', {'run': origin, 'note': str(raw.get('note', ''))}))
                continue
            if raw['decision'] not in ('approve', 'reject'):
                raise ValueError('decision must be approve or reject')
            if key not in state['pending']:
                raise ValueError(key + ' has no pending proposal to ' + raw['decision'])
            proposal = state['pending'].pop(key)
            if raw['decision'] == 'reject':
                out.append((key, 'rejected', [proposal['producer']]))
            else:
                out.append((key, proposal['change'], approve(state, proposal['change'],
                                                              {k: proposal[k] for k in FIELDS if k in proposal})))
            continue
        kind, entry = merge_change(state, raw, set(state['tasks']) | {p['id'] for p in state['plan']})
        if kind:
            out.append((ref(entry), kind, approve(state, kind, entry)))
    return out


def worker_change(state, raw, slot, task_id, root):
    """Validate a worker's interface_change (§4.3). Returns the pending proposal ref."""
    if not isinstance(raw, dict) or not isinstance(raw.get('id'), str):
        raise ValueError('interface_change: an interface change needs id and version')
    raw = dict(raw)
    cur = current(state, raw['id'])
    producer = cur['producer'] if cur else raw.setdefault('producer', task_id)
    if not cur and not raw['id'].startswith('IF-' + task_id + '-'):
        raise ValueError('interface_change: a new interface of ' + task_id + ' must be named IF-' + task_id + '-<slug>')
    if producer != task_id:
        raise ValueError('interface_change: only the producer (' + slot + ' / ' + str(producer)
                         + ') may propose a change to ' + raw['id'])
    sample_rule = ('interface_change: a breaking change needs sample_file in your own directory that passes '
                   'the new contains / json_keys')
    if raw.get('sample_file') is not None:
        own = (Path(root) / slot).resolve()
        path = (own / str(raw['sample_file'])).resolve()
        if not path.is_relative_to(own) or not path.is_file() or path.stat().st_size > SAMPLE_MAX:
            raise ValueError(sample_rule)
        raw['sample'] = path.read_text(errors='replace')
    elif raw.get('change') == 'breaking':
        raise ValueError(sample_rule)
    try:
        kind, entry = merge_change(state, raw, set(state['tasks']) | {p['id'] for p in state['plan']},
                                   worker=(slot, task_id))
    except ValueError as exc:
        if 'sample is required' in str(exc) and raw.get('change') == 'breaking':
            raise ValueError(sample_rule) from exc
        raise
    if kind is None:
        raise ValueError('interface_change: nothing differs from ' + ref(cur))
    if Path(entry['path']).parts[0] != slot:
        raise ValueError("interface_change: path must be inside the producer's own directory (" + slot + '/)')
    key = ref(entry)
    state['pending'][key] = dict(entry, change=kind, reason=str(raw.get('reason', '')), status='proposed',
                                 proposed_by=slot)
    return key


# ------------------------------------------------------------------ dispatch (P2)
def dispatch(state, items, resolve):
    """Validate the dispatch of one decision against the registry and the task table.

    Mutates ``state`` (task rows, stale cleared). Returns rows to queue now and rows held
    until their depends_on are done. ``resolve`` maps a label to its worker slot."""
    tasks = state['tasks']
    now, held, edits, seen = [], [], {}, set()
    for m, t in tasks.items():
        if t['status'] == 'running':
            for path in t.get('edits') or []:
                edits.setdefault(path, m)
    for item in items:
        m = item.get('id')
        if not isinstance(m, str) or not re.fullmatch(M_RE, m):
            raise ValueError('every dispatch item needs an id (M<n>, e.g. M0)')
        if m in seen:
            raise ValueError('dispatch id ' + m + ' appears twice in one reply')
        seen.add(m)
        slot = resolve(item['to'])
        lists = {}
        for k in ('provides', 'consumes', 'edits', 'depends_on'):
            lists[k] = item.get(k, [])
            if not isinstance(lists[k], list) or not all(isinstance(x, str) and x for x in lists[k]):
                raise ValueError('dispatch ' + m + ' ' + k + ' must be a list of strings')
        for i in lists['provides']:
            e = current(state, i)
            if e is None:
                raise ValueError('dispatch ' + m + ' provides ' + i + ' which is not a registered interface: '
                                 'declare it in interfaces first')
            if e['producer'] != m:
                raise ValueError(i + ' is produced by ' + e['producer'] + ', not ' + m)
            if Path(e['path']).parts[0] != slot:
                raise ValueError("path must be inside the producer's own directory: " + i + ' is ' + e['path']
                                 + ', not under ' + slot + '/')
        for c in lists['consumes']:
            iface, version = split_ref(c)
            if not iface:
                raise ValueError('dispatch ' + m + ' consumes entries must be id@version, got ' + c)
            e = state['interfaces'].get(c)
            if e is None:
                if c in state['pending']:
                    raise ValueError(c + ' is proposed, not approved')
                raise ValueError('dispatch ' + m + ' consumes ' + c + ' which is not a registered interface')
            if e['status'] == 'deprecated':
                raise ValueError('dispatch ' + m + ' consumes ' + c + ' which is deprecated by ' + str(e['deprecated_by']))
        for old in (tasks.get(m) or {}).get('stale') or []:
            new = current(state, split_ref(old)[0])
            if not new or ref(new) not in lists['consumes']:
                raise ValueError(m + ' is stale on ' + old + ': the next dispatch must consume @'
                                 + str(new['version'] if new else '?'))
        for path in lists['edits']:
            other = edits.get(path)
            if other and other != m:
                raise ValueError('edits conflict: ' + path + ' is assigned to ' + other + ' and ' + m + ' in this '
                                 'round; give it to one task and have the other call a function it exposes')
            edits[path] = m
        reason = item.get('serial_reason')
        if lists['depends_on'] and reason is None:
            raise ValueError('depends_on requires serial_reason (integration / exploratory); if the dependency is '
                             'an interface, consume its sample instead')
        if reason is not None and reason not in ('integration', 'exploratory'):
            raise ValueError('serial_reason must be integration or exploratory')
        row = dict(tasks.get(m) or {'id': m, 'attempts': 0, 'stale': []})
        row.update(to=slot, provides=lists['provides'], consumes=lists['consumes'], edits=lists['edits'],
                   depends_on=lists['depends_on'], serial_reason=reason, instruction=item['instruction'].strip())
        row['stale'] = []
        tasks[m] = row
    for m in seen:
        for d in tasks[m]['depends_on']:
            if d not in tasks:
                raise ValueError('depends_on ' + d + ' is not a task in the task table')
            if tasks[m]['serial_reason'] == 'exploratory' and tasks[d].get('provides'):
                raise ValueError('an exploratory dependency may not also provide an interface: either fix the shape '
                                 'and go parallel, or drop provides')
    cycle = find_cycle({m: set(t.get('depends_on') or []) for m, t in tasks.items()})
    if cycle:
        raise ValueError('depends_on cycle: ' + cycle)
    for item in items:
        row = tasks[item['id']]
        wait = [d for d in row['depends_on'] if tasks[d]['status'] != 'done']
        row['status'] = 'pending' if wait else 'running'
        (held if wait else now).append(row)
    return now, held


def releasable(state):
    """Held rows whose prerequisites are all done now."""
    tasks = state['tasks']
    return [t for t in tasks.values() if t['status'] == 'pending' and t.get('instruction')
            and all(tasks[d]['status'] == 'done' for d in t.get('depends_on') or [])]


# ------------------------------------------------------------------ worker reports
def last_json_object(text):
    """The JSON object the text ends with (never the first '{': evidence may quote JSON)."""
    text = (text or '').rstrip()
    if text.endswith('```'):
        text = text[:-3].rstrip()
    if not text.endswith('}'):
        return None, text
    decoder = json.JSONDecoder()
    for i in range(len(text) - 1, -1, -1):
        if text[i] != '{':
            continue
        try:
            obj, end = decoder.raw_decode(text, i)
        except ValueError:
            continue
        if end == len(text) and isinstance(obj, dict):
            return obj, text[:i].rstrip().removesuffix('```json').removesuffix('```').rstrip()
    return None, text


def report_fields(text):
    """Contract fields of a worker report: raw text (v1) or the settled JSON value (v2)."""
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    out = {}
    if isinstance(value, dict) and 'done' in value:
        for k in ('interfaces', 'peer_notices', 'question') + REPORT_KEYS:
            if value.get(k) not in (None, ''):
                out[k] = value[k]
        return out
    obj, rest = last_json_object(text)
    if isinstance(obj, dict) and any(k in obj for k in REPORT_KEYS):
        out.update({k: obj[k] for k in REPORT_KEYS if k in obj})
    else:
        rest = text
    for line in rest.splitlines():
        m = EXTRA_SECTIONS.match(line)
        if m:
            name = {'interfaces': 'interfaces', 'peer notices': 'peer_notices'}.get(m.group(1).lower(), 'question')
            out[name] = m.group(2).strip()
    return out


def ready_claims(line):
    return [(m.group(1), int(m.group(2))) for m in READY_RE.finditer(line or '')]


# ------------------------------------------------------------------ registry view
def view(state, worker_cli, runtime=None):
    """What agents read: registry + task table (+ program-filled consumers_runtime).
    ``runtime``: worker slot -> (cli, Claude session name or None), refreshed by the daemon."""
    to = {m: t.get('to') for m, t in state['tasks'].items()}
    runtime = runtime or {}
    interfaces = []
    for r in sorted(state['interfaces']):
        e = dict(state['interfaces'][r])
        e['consumers_runtime'] = [{'m': m, 'worker': to.get(m), 'session': runtime.get(to.get(m), (None, None))[1],
                                   'cli': runtime.get(to.get(m), (worker_cli, None))[0]}
                                  for m in e.get('consumers') or []]
        interfaces.append(e)
    return {'phase': state['phase'], 'interfaces': interfaces,
            'proposed': [state['pending'][r] for r in sorted(state['pending'])],
            'tasks': [{k: v for k, v in state['tasks'][m].items() if k != 'message'} for m in sorted(state['tasks'])]}


def table_line(state):
    parts = []
    for m in sorted(state['tasks'], key=lambda x: [int(p) for p in x[1:].split('.')]):
        t = state['tasks'][m]
        extra = (t['to'] if t['status'] == 'running' else
                 'waits ' + ','.join(t.get('depends_on') or []) if t['status'] == 'pending' else '')
        stale = ' stale on ' + ','.join(t['stale']) if t.get('stale') else ''
        parts.append(m + ' ' + t['status'] + (' (' + extra + ')' if extra else '') + stale)
    return '[contract] phase ' + state['phase'] + '; task table: ' + (' · '.join(parts) or 'empty')
