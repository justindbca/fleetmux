"""Worker supply and fix reviews for applet leases (docs/applets.md sections 3.3 and 6, applet-interface.md
sections 1.2, 1.3, 2.5 and 3.9). The host daemon runs both; no supervisor turn is spent on a review.

Redis (host; internal unless named in applet-interface.md 3.1)
  fx:applet:<name>:review:<R-id>   hash: the review record (3.1)
  fx:applet:<name>:added           internal hash: slot -> time the host added that worker row for this applet
  fx:applet:<name>:rounds          internal hash: job -> FAIL verdicts so far; ``<job>:reviewer`` -> last reviewer slot
  fx:applet:used_names             internal hash: every worker name the host added in this run (never reused)
  fx:applet:reviews                internal list of ``<applet>|<review id>`` not yet closed
"""
import json
import os
import re
import secrets
from pathlib import Path

from fleetmux import applet_lease as al
from fleetmux.applet.lease import file_lock

USED = 'fx:applet:used_names'
REVQ = 'fx:applet:reviews'
REVIEW_REPLY = {'required': ['verdict', 'findings'], 'allowed': ['verdict', 'findings', 'report']}
BLOCKING = ('P0', 'P1')


def review_key(name, rid):
    return al.KEY + name + ':review:' + rid


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


# ------------------------------------------------------------------ fix reviews (section 6)
def check_fix(f, name, rec, fix):
    """Shape of a message's ``fix`` (3.8) and the job's round cap; returns the normalized fix."""
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
    cap = int(al.specs(f)[name]['review']['max_rounds'])
    if int(f.r.hget(al.KEY + name + ':rounds', rec['job']) or 0) >= cap:
        raise al.Refused('review_cap', 'job ' + rec['job'] + ' used up review.max_rounds ' + str(cap))
    return {'dir': d, 'base': fix['base'], 'head': fix.get('head'), 'files': files}


def withhold(f, rid, status, fields):
    """Called as a result is published: the leased worker's ``ok`` reply to a ``fix`` message is withheld and a
    review starts. True when the result was withheld."""
    slot = f.resolve(fields.get('worker') or '')
    lid = al.leased(f, slot)
    if not lid:
        return False
    name = f.r.hget('fx:agent:' + slot, 'applet_lease_holder') or ''
    rec = al.record(f.r, name, lid)
    fixes = al._loads(rec.get('fixes'), {})
    if rid not in fixes or fixes[rid].get('review'):
        return False
    if status != 'ok':
        fixes[rid]['review'] = '-'
        f.r.hset(al.lease_key(name, lid), mapping={'fixes': al._j(fixes)})
        return False
    review = 'R-' + secrets.token_hex(4)
    fails = int(f.r.hget(al.KEY + name + ':rounds', rec['job']) or 0)
    now = al.clock()
    f.r.hset(review_key(name, review), mapping={
        'id': review, 'job': rec['job'], 'lease': lid, 'message': rid, 'fixer': slot, 'reviewer': '',
        'round': fails + 1, 'state': 'waiting', 'verdict': '', 'reason': '', 'started_at': now, 'ended_at': '',
        'fix': al._j(fixes[rid]['fix']), 'withheld': al._j(dict(fields, status=status)), 'prep_gen': '',
        'prep_fresh': 0})
    fixes[rid]['review'] = review
    f.r.hset(al.lease_key(name, lid), mapping={'fixes': al._j(fixes), 'reviewing': review})
    f.r.rpush(REVQ, name + '|' + review)
    fleet = _fleet()
    f.r.delete(fleet.REQ + rid)  # the worker's turn is over: no request deadline runs during the review
    f.r.lrem(fleet.REQQ, 0, rid)
    al.event(f, name, 'review.started', lease=lid, id=rid, review=review, round=fails + 1)
    _audit_review(f, 'applet.review.started', name, review)
    return True


def _audit_review(f, kind, name, review, **extra):
    rec = f.r.hgetall(review_key(name, review))
    al._audit(f, kind, applet=name, review=review, job=rec.get('job', ''), lease=rec.get('lease', ''),
              message=rec.get('message', ''), fixer=rec.get('fixer', ''), reviewer=rec.get('reviewer', ''),
              round=int(rec.get('round') or 0), **extra)


def review_tick(f, now):
    for item in f.r.lrange(REVQ, 0, -1):
        name, review = item.split('|', 1)
        rec = f.r.hgetall(review_key(name, review))
        entry = al.specs(f).get(name)
        if not rec or not entry:
            f.r.lrem(REVQ, 0, item)
            continue
        if rec['state'] == 'done':
            _release_reviewer(f, name, review, rec, item)
            continue
        if now - float(rec['started_at']) > float(entry['review']['timeout_s']):
            finish(f, name, review, 'none', [], 'no_reviewer' if rec['state'] == 'waiting' else 'reviewer_timeout')
            continue
        if rec['state'] == 'waiting':
            pick(f, entry, review, rec)
        elif rec['state'] == 'preparing':
            send(f, name, review, rec)
        elif rec['state'] == 'running':
            raw = f.r.get(_fleet().RESULT + review)
            if raw:
                verdict(f, name, review, json.loads(raw))


def pick(f, entry, review, rec):
    """A reviewer slot: not the fixer's, never a lease of this applet for the job; idle, else asleep, else added."""
    name = entry['name']
    exclude = {rec['fixer']} | al.jobs_slots(f, name, rec['job'])
    pool = [s for s in f.workers if s not in exclude]
    prev = f.r.hget(al.KEY + name + ':rounds', rec['job'] + ':reviewer')
    free = [s for s in pool if al.idle(f, s)]
    slot = prev if prev in free else (free[0] if free else None)
    if slot is None:
        sleeping = [s for s in pool if al.asleep(f, s)]
        if sleeping:
            slot = sleeping[0]
            f.v2.respawn([slot], True)
        elif not add_problem(f, entry):
            slot = add_worker(f, entry, review)
        else:
            return  # wait for a slot, at most review.timeout_s
    info = f.agent(slot) if slot in f.workers else {}
    fresh = int(info.get('delivered') or 0) > 0 and info.get('state') == 'idle'
    if slot in f.workers:
        f.setf(slot, applet_review=review, **({'fresh_pending': 1, 'result_fresh': 1} if fresh else {}))
    f.r.hset(review_key(name, review), mapping={'reviewer': slot, 'state': 'preparing', 'prep_fresh': int(fresh),
                                               'prep_gen': info.get('generation') or ''})


def send(f, name, review, rec):
    """Once the reviewer slot is READY in a clean session: one program-written review request."""
    slot = rec['reviewer']
    if slot not in f.workers:
        return  # an added row the roster reload has not started yet
    info = f.agent(slot)
    if info.get('applet_review') != review:
        f.setf(slot, applet_review=review)
    if info.get('state') != 'idle' or info.get('fresh_pending') == '1' or (
            rec.get('prep_fresh') == '1' and info.get('generation') == rec.get('prep_gen')):
        return
    fix = al._loads(rec.get('fix'), {})
    prev = al._loads(f.r.hget(al.KEY + name + ':rounds', rec['job'] + ':findings'), [])
    text = ('[Fix review ' + review + ', ' + name + ', job ' + rec['job'] + ', round ' + rec['round'] + ']\n'
            'Review only this change, read-only: directory ' + fix.get('dir', '') + ', from revision '
            + fix.get('base', '') + ' to ' + (fix.get('head') or 'the working tree') + (
                ', files ' + ', '.join(fix['files']) if fix.get('files') else '') + '.\n'
            + ('Open findings of the previous round (re-run their reproductions first): ' + al._j(prev) + '\n'
               if prev else '')
            + 'Follow REVIEWER.md in your directory. Do not edit any file. Reply with verdict "PASS" (zero P0 and zero '
            'P1) or "FAIL", and findings: a list of {level: P0|P1, file, line, what, repro}; optionally report: the '
            'path of your report file.')
    from fleetmux import runtime_v2
    spec = {'id': review, 'to': slot, 'payload': {'review': review, 'fix': fix}, 'reply': REVIEW_REPLY,
            'instruction': text}
    runtime_v2.post_event(f.v2.bus, {'kind': 'request', 'request_id': 'review-' + review, 'spec': spec},
                          'review-' + review)
    f.r.hset(review_key(name, review), mapping={'state': 'running', 'sent_at': al.clock()})


def verdict(f, name, review, result):
    if result.get('status') != 'ok' or not isinstance(result.get('result'), dict):
        finish(f, name, review, 'none', [], 'reviewer_invalid' if result.get('status') == 'invalid'
               else 'reviewer_timeout')
        return
    body = result['result']
    findings = body.get('findings') if isinstance(body.get('findings'), list) else []
    said = str(body.get('verdict', '')).strip().upper()
    if said not in ('PASS', 'FAIL'):
        finish(f, name, review, 'none', [], 'reviewer_invalid')
        return
    blocking = any(isinstance(x, dict) and x.get('level') in BLOCKING for x in findings)
    finish(f, name, review, 'pass' if said == 'PASS' and not blocking else 'fail', findings, None,
           body.get('report') if isinstance(body.get('report'), str) else None)


def finish(f, name, review, outcome, findings, reason, report=None):
    """Publish the withheld result with ``review``; move the lease's end; count the round."""
    rec = f.r.hgetall(review_key(name, review))
    entry = al.specs(f)[name]
    rounds = al.KEY + name + ':rounds'
    capped = False
    if outcome == 'fail':
        fails = int(f.r.hget(rounds, rec['job']) or 0) + 1
        capped = fails >= int(entry['review']['max_rounds'])
        f.r.hset(rounds, mapping={rec['job']: fails, rec['job'] + ':findings': al._j(findings)})
    elif outcome == 'pass':
        f.r.hset(rounds, mapping={rec['job'] + ':findings': '[]'})
    if rec.get('reviewer'):
        f.r.hset(rounds, mapping={rec['job'] + ':reviewer': rec['reviewer']})
    now = al.clock()
    f.r.hset(review_key(name, review), mapping={'state': 'done', 'verdict': outcome, 'reason': reason or '',
                                               'ended_at': now})
    out = {'id': review, 'verdict': outcome, 'round': int(rec['round']), 'capped': capped, 'reason': reason,
           'reviewer': f.label(rec['reviewer']) if rec.get('reviewer') else '', 'findings': findings,
           'report': report}
    lid = rec['lease']
    lease = al.record(f.r, name, lid)
    if lease:
        fields = {'reviewing': '', 'last_review': al._j({'id': review, 'verdict': outcome,
                                                          'round': int(rec['round']), 'message': rec['message']})}
        if lease.get('state') in ('active', 'renew_requested'):
            margin = float(entry['lease']['renew_margin_s'])
            expires = max(float(lease.get('expires_at') or 0), now + margin)
            fields.update(expires_at=expires, warned_for='')
            f.setf(lease['slot'], applet_lease_until=expires)
        f.r.hset(al.lease_key(name, lid), mapping=fields)
    withheld = al._loads(rec.get('withheld'), {})
    withheld.pop('status', None)
    f.publish(rec['message'], 'ok', **dict(withheld, lease=lid, review=out))
    kind = {'pass': 'passed', 'fail': 'failed', 'none': 'unavailable'}[outcome]
    _audit_review(f, 'applet.review.' + kind, name, review, findings=len(findings), capped=capped,
                  reason=reason or '')
    _release_reviewer(f, name, review, f.r.hgetall(review_key(name, review)), name + '|' + review)


def _release_reviewer(f, name, review, rec, item):
    """Give the reviewer slot back once its review request is over (clean session after)."""
    slot = rec.get('reviewer') or ''
    sent = rec.get('sent_at')
    if sent and not f.r.get(_fleet().RESULT + review):
        return  # the reviewer is still on it; looked at again next tick
    if slot and slot in f.workers and f.agent(slot).get('applet_review') == review:
        f.setf(slot, applet_review='', **({'fresh_pending': 1, 'result_fresh': 1}
                                          if int(f.agent(slot).get('delivered') or 0) > 0 else {}))
    f.r.lrem(REVQ, 0, item)
    f.r.rpush(al.EXPIRE, review_key(name, review))


def under_review(f, name, rec):
    """Message ids of a lease whose review is still running (a lease end does not cut them off)."""
    out = []
    for mid, x in al._loads(rec.get('fixes'), {}).items():
        r = x.get('review') or ''
        if r.startswith('R-') and f.r.hget(review_key(name, r), 'state') != 'done':
            out.append(mid)
    return out


def review_rows(r, all_=False):
    rows = []
    open_ = set(r.lrange(REVQ, 0, -1))
    for k in r.scan_iter(al.KEY + 'applet-*:review:R-*'):
        name, rid = k[len(al.KEY):].split(':review:')
        if all_ or name + '|' + rid in open_:
            rec = r.hgetall(k)
            rows.append({x: rec.get(x, '') for x in ('id', 'job', 'lease', 'message', 'fixer', 'reviewer', 'round',
                                                     'state', 'verdict', 'reason')} | {'applet': name})
    return rows
