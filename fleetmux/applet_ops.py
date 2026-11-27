"""Worker supply for applet leases (docs/applets.md section 3.3, applet-interface.md sections 1.2 and 2.5), and
the shape check of a message's ``fix`` (3.8). A fix reply goes straight back to the applet: there is no fix
review. ``drain_reviews`` hands back, once, what a host with the old review step still held.

Redis (host; internal unless named in applet-interface.md 3.1)
  fx:applet:<name>:added           internal hash: slot -> time the host added that worker row for this applet
  fx:applet:used_names             internal hash: every worker name the host added in this run (never reused)
  fx:applet:reviews                internal list left by the removed review step; emptied by ``drain_reviews``
"""
import os
import re
from pathlib import Path

from fleetmux import applet_lease as al
from fleetmux.applet.lease import file_lock

USED = 'fx:applet:used_names'
REVQ = 'fx:applet:reviews'  # older hosts only (drain_reviews)


def _fleet():
    from fleetmux import fleet
    return fleet


# ------------------------------------------------------------------ worker supply (section 3.3)
def added(f, name):
    """Rows the host added for this applet that are in the roster now, awake or asleep (and rows written moments
    ago that the roster reload has not picked up yet)."""
    wait = al.settings(f.cfg)['add_wait_s']
    now = al.clock()
    return [s for s, at in f.r.hgetall(al.KEY + name + ':added').items()
            if s in f.workers or now - float(at) < wait]


def hot_add_max(f, entry):
    raw = f.r.hget(al.hkey(entry['name']), 'hot_add_max')
    return int(raw) if raw not in (None, '') else int(entry['lease']['hot_add_max'])


def roster_paths(f):
    return [Path(p) for p in (f.cfg.get('roster'), f.cfg.get('roster_night')) if p]


def add_problem(f, entry):
    """Why no worker can be added now ('' when one can): the applet's ceiling, the fleet's own limits, or a
    roster the program cannot extend."""
    count, cap = len(added(f, entry['name'])), hot_add_max(f, entry)
    if count >= cap:
        return 'hot_add_max ' + str(cap) + ' reached (' + str(count) + ' added)'
    if not roster_paths(f):
        return 'this run has no roster file to add a worker to'
    for path in roster_paths(f):
        try:
            if not re.search(r'^workers:\s*(#.*)?$', path.read_text(), re.M):
                return 'roster ' + path.name + ' has no block-style workers: list to add a row to'
        except OSError:
            return 'roster ' + path.name + ' cannot be read'
    try:
        from fleetmux import subfleet
        lim = subfleet.limits(f.cfg)
        if f.cfg.get('parent_config') and len(f.workers) + 1 > lim['child_workers_max']:
            return 'child_workers_max ' + str(lim['child_workers_max'])
        total = subfleet.tree_agents(subfleet.root_config(f.cfg).get('run') or f.cfg['run'])
        if total + 1 > lim['max_agents_total']:
            return 'max_agents_total ' + str(lim['max_agents_total'])
    except Exception as exc:  # unreadable limits: the existing hot reload checks them again
        f.log('applet.limits_unread', error=str(exc)[:200])
    return ''


def next_name(f, entry):
    """``<base of hot_add_like>-<n>`` with the next number never used for that base in this run."""
    return free_name(f, re.sub(r'-[0-9]+$', '', entry['lease']['hot_add_like']))


def free_name(f, base):
    """``<base>-<n>`` with the next number never used for that base in this run (applets and jobs, §58)."""
    used = set(f.workers) | set(f.r.hgetall(USED)) | set(f.r.hgetall(_fleet().NAMES).values())
    n = 1
    while base + '-' + str(n) in used or (f.workspace() / (base + '-' + str(n))).exists():  # the reload refuses those
        n += 1
    return base + '-' + str(n)


def template_role(path, like):
    import yaml
    try:
        rows = (yaml.safe_load(Path(path).read_text()) or {}).get('workers') or []
    except (OSError, ValueError, yaml.YAMLError):
        return None
    return next((r.get('role') for r in rows if isinstance(r, dict) and r.get('name') == like), None)


def add_worker(f, entry, for_id):
    """Write one worker row for this applet into the host roster (and the night roster); the roster reload
    (design.md section 16) starts it. Returns the slot name."""
    name = next_name(f, entry)
    like = entry['lease']['hot_add_like']
    for path in roster_paths(f):
        write_worker_row(path, name, template_role(path, like))
    now = al.clock()
    f.r.hset(al.KEY + entry['name'] + ':added', mapping={name: now})
    f.r.hset(USED, mapping={name: entry['name']})
    al._audit(f, 'applet.worker_added', applet=entry['name'], slot=name, like=like, **{'for': for_id},
              count=len(added(f, entry['name'])), hot_add_max=hot_add_max(f, entry))
    return name


def _replace_atomic(path, text):
    from fleetmux import roster
    roster.parse(text.encode(), path.parent)  # never write a roster the daemon would reject
    tmp = path.with_name('.' + path.name + '.tmp')
    tmp.write_text(text)
    os.replace(tmp, path)


def write_worker_row(path, name, role):
    """Append ``- {name: <name>, role: <role>}`` to the block-style ``workers:`` list, keeping every other byte."""
    path = Path(path)
    with file_lock(path):
        lines = path.read_text().splitlines(keepends=True)
        start = next((i for i, x in enumerate(lines) if re.match(r'workers:\s*(#.*)?$', x)), None)
        if start is None:
            raise ValueError('roster has no block-style workers: list: ' + str(path))
        last, indent = start, '  '
        for i in range(start + 1, len(lines)):
            x = lines[i]
            if x.strip() and not x[0].isspace() and not x.startswith('-'):
                break  # the next top-level key
            if x.strip() and not x.lstrip().startswith('#'):
                last = i
                m = re.match(r'(\s*)- ', x)
                if m and i == next(j for j in range(start + 1, len(lines)) if lines[j].strip()):
                    indent = m.group(1)
        row = indent + '- {name: ' + name + (', role: ' + role if role else '') + '}\n'
        if not lines[last].endswith('\n'):
            lines[last] += '\n'
        lines.insert(last + 1, row)
        _replace_atomic(path, ''.join(lines))


def remove_worker_row(path, name):
    """Remove the ``- {name: <name>...}`` row :func:`write_worker_row` wrote (§58: a job's added worker); the roster
    reload retires the slot. True when a row was removed."""
    path = Path(path)
    with file_lock(path):
        lines = path.read_text().splitlines(keepends=True)
        row = re.compile(r'\s*- \{name: ' + re.escape(name) + r'(, [^}]*)?\}\s*$')
        keep = [x for x in lines if not row.match(x)]
        if len(keep) == len(lines):
            return False
        _replace_atomic(path, ''.join(keep))
        return True


def write_hot_add_max(path, applet, value):
    """Write ``hot_add_max`` back into the applet's roster row (the same lock and atomic replace as every writer)."""
    path = Path(path)
    with file_lock(path):
        text = path.read_text()
        lines = text.splitlines(keepends=True)
        at = next((i for i, x in enumerate(lines) if re.search(r'\bname:\s*' + re.escape(applet) + r'\b', x)), None)
        if at is None:
            raise ValueError('no applets: row named ' + applet + ' in ' + str(path))
        head = at if re.match(r'\s*- ', lines[at]) else next(i for i in range(at, -1, -1) if re.match(r'\s*- ', lines[i]))
        item = len(re.match(r'(\s*)', lines[head]).group(1))
        end = next((i for i in range(head + 1, len(lines)) if lines[i].strip()
                    and len(lines[i]) - len(lines[i].lstrip()) <= item), len(lines))
        span = ''.join(lines[head:end])
        if re.search(r'hot_add_max:\s*\d+', span):
            span = re.sub(r'hot_add_max:\s*\d+', 'hot_add_max: ' + str(value), span, count=1)
        elif re.search(r'lease:\s*\{', span):
            span = re.sub(r'lease:\s*\{\s*', lambda m: 'lease: {hot_add_max: ' + str(value) + ', ', span, count=1)
            span = span.replace(', }', '}')
        elif re.search(r'^\s*lease:\s*$', span, re.M):
            m = re.search(r'^(\s*)lease:\s*\n', span, re.M)
            span = span[:m.end()] + m.group(1) + '  hot_add_max: ' + str(value) + '\n' + span[m.end():]
        elif lines[head].lstrip().startswith('- {'):
            span = re.sub(r'- \{', '- {lease: {hot_add_max: ' + str(value) + '}, ', span, count=1)
        else:
            key = re.match(r'(\s*)(- )?', lines[at])
            pad = ' ' * (len(key.group(1)) + (2 if key.group(2) else 0))
            first = lines[head:end]
            first.insert(1, pad + 'lease: {hot_add_max: ' + str(value) + '}\n')
            span = ''.join(first)
        _replace_atomic(path, ''.join(lines[:head]) + span + ''.join(lines[end:]))


def set_hot_add_max(f, name, value):
    """``fleetmux applet set <name> hot_add_max <n>``: the running value now, the roster row for later."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise al.Refused('invalid', 'hot_add_max must be an integer >= 0')
    row = str(al.specs(f)[name]['lease']['hot_add_max'])  # the row value now; a reload with the new one agrees
    f.r.hset(al.hkey(name), mapping={'hot_add_max': value, 'hot_add_row': f.r.hget(al.hkey(name), 'hot_add_row') or row})
    for path in roster_paths(f):
        write_hot_add_max(path, name, value)
    al._audit(f, 'applet.param_changed', applet=name, key='hot_add_max', value=value, by='operator')


def sync(f, entry):
    """Load the row's ``hot_add_max`` into Redis when the row's value changed (a roster load); an online change
    stays until the row itself changes. Mark the rows this applet added."""
    name, value = entry['name'], str(entry['lease']['hot_add_max'])
    if f.r.hget(al.hkey(name), 'hot_add_row') != value:  # internal: the row value last loaded
        f.r.hset(al.hkey(name), mapping={'hot_add_max': value, 'hot_add_row': value})
    for slot in added(f, name):
        if slot in f.workers and f.r.hget('fx:agent:' + slot, 'applet_added_for') != name:
            f.setf(slot, applet_added_for=name)


# ------------------------------------------------------------------ fixes (3.8)
def check_fix(f, name, rec, fix):
    """Shape of a message's ``fix`` (3.8); returns the normalized fix. It is not reviewed: the reply goes straight
    back to the applet."""
    if not isinstance(fix, dict) or not set(fix) <= {'dir', 'base', 'head', 'files'}:
        raise al.Refused('invalid', 'fix must be {dir, base, head, files}')
    d = fix.get('dir')
    if not isinstance(d, str) or not d.startswith('/'):
        raise al.Refused('invalid', 'fix.dir must be an absolute path')
    d = os.path.normpath(d)
    roots = al._loads(rec.get('paths'), []) + [str(_fleet().agent_cwd(f.cfg, rec.get('slot') or ''))]
    if not any(d == r or d.startswith(r.rstrip('/') + '/') for r in roots):
        raise al.Refused('invalid', 'fix.dir is outside the lease (' + ', '.join(roots) + ')')
    if not isinstance(fix.get('base'), str) or not fix['base'].strip():
        raise al.Refused('invalid', 'fix.base must name the revision the change starts from')
    if fix.get('head') is not None and not isinstance(fix['head'], str):
        raise al.Refused('invalid', 'fix.head must be a revision or null')
    files = fix.get('files') or []
    if not isinstance(files, list) or not all(isinstance(x, str) and x for x in files):
        raise al.Refused('invalid', 'fix.files must be a list of paths relative to dir')
    return {'dir': d, 'base': fix['base'], 'head': fix.get('head'), 'files': files}


def drain_reviews(f):
    """A host upgraded while the removed review step still held a fix reply: publish each held reply as the worker
    gave it, give the lease its normal expiry back and the reviewer slot back to the scheduler. Runs on the applet
    tick; a no-op once the list is empty."""
    for item in f.r.lrange(REVQ, 0, -1):
        name, _, review = item.partition('|')
        key = al.KEY + name + ':review:' + review
        rec = f.r.hgetall(key)
        if rec and rec.get('state') != 'done':
            lid = rec.get('lease') or ''
            if lid and al.record(f.r, name, lid).get('reviewing') == review:
                f.r.hset(al.lease_key(name, lid), mapping={'reviewing': ''})
            slot = rec.get('reviewer') or ''
            if slot and f.r.hget('fx:agent:' + slot, 'applet_review') == review:
                had = f.r.hget('fx:agent:' + slot, 'applet_review_grant')
                f.r.hset('fx:agent:' + slot, mapping={'applet_review': '', 'applet_review_grant': ''})
                if had:  # an older host wrote a read-only grant section into the reviewer's rule files
                    al.write_section(f, slot)
            f.r.hset(key, mapping={'state': 'done', 'verdict': 'none', 'reason': 'review_removed',
                                   'ended_at': al.clock()})
            held = al._loads(rec.get('withheld'), {})
            held.pop('status', None)
            if rec.get('message'):
                f.publish(rec['message'], 'ok', **dict(held, lease=lid))
            al._audit(f, 'applet.review.drained', applet=name, review=review, lease=lid,
                      message=rec.get('message', ''))
        f.r.lrem(REVQ, 0, item)
        f.r.rpush(al.EXPIRE, key)
