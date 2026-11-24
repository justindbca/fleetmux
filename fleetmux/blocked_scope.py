"""A root supervisor's BLOCKED on a notice about one of its own sub-fleets stops only that sub-fleet (design.md §24).

A root fleet has no parent, so a root pause waits for a human with no way out. When the root supervisor answers a
notice about one of its own sub-fleets (or jobs) with BLOCKED, the root goes on (no pause in service mode, no
``finish('blocked')`` in task mode), and the user is told once through the run's existing outbound path:

* ``[sub-fleet stuck]`` / ``[sub-fleet held]``: ``attention`` in that sub-fleet's record (shown by ``fleetmux status``);
* ``[sub-fleet report]``: the report's escalation record (``fx:escalations`` ``report:<task>:<id>``) waits for the
  user (status ``awaiting_user``): never nudged, struck, re-asked or approved by the program; the sub-fleet's record
  carries ``attention_reports``; the user answers with ``fleetmux answer`` (or the supervisor rules on it later);
* ``[Sub-fleet <id> completed|failed]`` and ``[job <id>]``: noted (audit + one push), nothing marked.

A notice counts only when it is the message the reply is bound to, it starts with one of these program prefixes
(after any ``[Previous generation did not finish]`` notes), and it resolves to a live record of this fleet. Every
other BLOCKED, and every BLOCKED at a fleet with a parent, keeps the previous behaviour. The reason is never written
to ``fx:service.reason``.

Switch: ``fleet.yaml#daemon_watch.subfleet_blocked_marks_child`` (bool, default true).
"""
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path

from fleetmux import fleet_config, subfleet

DEFAULTS = {'subfleet_blocked_marks_child': True}
REGEN_LEAD = '[Previous generation did not finish]'  # runtime_v2.REGEN_NOTE / REGEN_RESUME_SUPERVISOR
NOTICE = re.compile(r'\[sub-fleet (stuck|held)\] sub-fleet (\S+) \((.+?)\): ')
REPORT = re.compile(r'\[sub-fleet report\] sub-fleet (\S+) \((.*?)\): ')  # Fleet.report_notice
ENDED = re.compile(r'\[Sub-fleet (\S+) (completed|failed)\] ')  # subfleet.collect
ATTEMPT = re.compile(r' \(attempt (\d+)\): ')
JOB = re.compile(r'\[job (job-[0-9A-Za-z_-]+)\] ')  # Fleet.job_submit / job carry-over
RESENT_LEAD = re.compile(r'^\W*re-sent,\s*still\s*open\W*', re.I)
TEXT_KEEP = 300
REPORT_KEY = 'report:'  # fleet.REPORT_KEY
AWAITING = 'awaiting_user'
PENDING_KIND = 'report_pending'  # <origin run>/subfleet-vetoes: the report went to the user (no ruling yet)
EVENT = 'subfleet_blocked'  # the push's event name (notifications.jsonl, notify-undelivered.json)
CLOSED = 'fx:escalations:closed'  # JSON {key: {decision, by, why, at}}: answered / dropped report keys
PUSHED = 'fx:attention:pushed'  # JSON {fingerprint: at}: noted notices already pushed
KEEP = 500


def knobs():
    """(knobs, problem): the switch of fleet.yaml#daemon_watch over DEFAULTS; a bad value keeps the default."""
    try:
        block = fleet_config.block('daemon_watch') or {}
    except (OSError, ValueError, KeyError) as exc:
        return dict(DEFAULTS), 'fleet.yaml#daemon_watch unreadable: ' + str(exc)[:200]
    out, bad = dict(DEFAULTS), []
    for key in DEFAULTS:
        if key in block:
            if type(block[key]) is bool:
                out[key] = block[key]
            else:
                bad.append(key)
    return out, ('fleet.yaml#daemon_watch: ' + ', '.join(bad) + ' must be true or false') if bad else None


def on():
    return knobs()[0]['subfleet_blocked_marks_child']


def strip_regen(text):
    """The message as first queued: drop the notes a supervisor regeneration put in front of it."""
    while text.startswith(REGEN_LEAD) and '\n\n' in text:
        text = text.split('\n\n', 1)[1]
    return text


def notice_run(f, about):
    """The run directory of this fleet's own sub-fleet that ``about`` (a [sub-fleet stuck] / [sub-fleet held]
    notice) is about, or None. Any fleet, switch on or off: the pause cause of a BLOCKED that is not absorbed."""
    m = NOTICE.match(strip_regen(about or ''))
    if not m:
        return None
    want = os.path.realpath(m.group(3))
    return next((str(d) for d in subfleet.children_dirs(f.run_dir) if os.path.realpath(d) == want), None)


def own_child(f, run):
    """The direct sub-fleet directory of this fleet that ``run`` is, or lies under (a grandchild's report), or None."""
    if not run:
        return None
    kids = {os.path.realpath(d): d for d in subfleet.children_dirs(f.run_dir)}
    path = Path(os.path.realpath(str(run)))
    return next((kids[str(p)] for p in [path] + list(path.parents) if str(p) in kids), None)


def _message_chain(f, about_id):
    """The bound message id and the ids it was re-queued from (``stopfail_of``, API-error requeue)."""
    ids, mid = [], about_id
    bus = getattr(getattr(f, 'v2', None), 'bus', None)
    while mid and len(ids) < 8 and mid not in ids:
        ids.append(mid)
        if bus is None:
            break
        try:
            mid = bus.r.hget(bus.msg_key(mid), 'stopfail_of')
        except Exception:  # noqa: BLE001  (a lookup never breaks a settlement)
            break
    return ids


def report_key(f, text, about_id=None):
    """The open report record a ``[sub-fleet report]`` text belongs to, or None. The bound message id first (the
    record's ``message_id``); else (a regeneration copy has a new id) the record whose own notice text the message
    starts with. A key written inside the request text by a sub-fleet never selects a record by itself."""
    esc = f.escalation_state()
    open_ = {k: r for k, r in esc.items() if k.startswith(REPORT_KEY) and r.get('kind') == 'report'
             and r.get('status') in ('escalated', AWAITING)}
    ids = _message_chain(f, about_id)
    for k, r in open_.items():
        if r.get('message_id') and r['message_id'] in ids:
            return k
    best = None
    for k, r in open_.items():
        try:
            head = f.report_notice(k, r)
        except (KeyError, TypeError):
            continue
        if text.startswith(head) and (best is None or len(head) > best[1]):
            best = (k, len(head))
    return best[0] if best else None


def classify(f, about, about_id=None):
    """The notice about one of this fleet's sub-fleets (or jobs) a BLOCKED answers, or None. ``about`` is the text of
    the message the reply is bound to, ``about_id`` its bus message id (v2).

    Returns {'notice': 'stuck' | 'held' | 'report' | 'completed' | 'failed' | 'job', 'task', 'run', 'active', 'text'}
    (+ 'key' for a report). Only at a root fleet with the switch on, and only when the notice resolves to this fleet's
    own sub-fleet / report record / job."""
    if f.cfg.get('parent_config') or not about:
        return None
    text = strip_regen(about)
    found = next(((kind, m) for kind, rx in (('subfleet', NOTICE), ('report', REPORT), ('ended', ENDED), ('job', JOB))
                  for m in [rx.match(text)] if m), None)
    if not found:
        return None
    kind, m = found
    knob, problem = knobs()
    if problem:
        f.log('held_watch.config_rejected', level='warn', error=problem)
    if not knob['subfleet_blocked_marks_child']:
        return None
    hit = {'text': text[:TEXT_KEEP]}
    if kind == 'subfleet':
        want = os.path.realpath(m.group(3))
        d = next((d for d in subfleet.children_dirs(f.run_dir) if os.path.realpath(d) == want), None)
        if d is None:
            return None
        hit.update(notice=m.group(1), task=m.group(2), run=str(d))
    elif kind == 'report':
        key = report_key(f, text, about_id)
        d = own_child(f, f.escalation_state()[key].get('origin_run')) if key else None
        if d is None:
            return None
        hit.update(notice='report', task=m.group(1), run=str(d), key=key)
    elif kind == 'ended':
        attempt = ATTEMPT.search(text)
        rows = [d for d in subfleet.children_dirs(f.run_dir) if subfleet.record(d).get('task_id') == m.group(1)]
        if attempt:
            rows = [d for d in rows if str(subfleet.record(d).get('attempt') or 1) == attempt.group(1)]
        if not rows:
            return None
        hit.update(notice=m.group(2), task=m.group(1), run=str(rows[-1]))
    else:
        jid = m.group(1)
        if jid not in f.job_state():
            return None
        runs = subfleet.job_children(f.run_dir, jid)
        hit.update(notice='job', task=jid, job=jid, run=str(runs[-1]) if runs else '')
    hit['active'] = bool(hit['run']) and subfleet.record(hit['run']).get('state') in subfleet.ACTIVE
    return hit


def absorb(f, about, reason, about_id=None):
    """Called for a BLOCKED decision. True: it answered a notice about one of this root's sub-fleets (or jobs), which
    is now marked / waits for the user / was noted; the caller neither pauses nor ends the task. False: the caller
    goes on as before."""
    hit = classify(f, about, about_id)
    if hit is None:
        return False
    reason = reason or ''
    if hit['notice'] == 'report':
        return _absorb_report(f, hit, reason)
    if hit['notice'] in ('completed', 'failed', 'job'):
        return _note(f, hit, reason)
    if not hit['active']:
        f.log('blocked.subfleet_gone', level='warn', task=hit['task'], run=hit['run'], notice=hit['notice'],
              reason=reason[:300])
        return True
    rec = subfleet.record(hit['run'])
    had = bool(rec.get('attention'))
    rec['attention'] = {'reason': reason, 'notice': hit['notice'], 'notice_text': hit['text'],
                        'by': 'supervisor BLOCKED', 'label': f.label('supervisor'), 'at': time.time()}
    subfleet.save_record(hit['run'], rec)
    f.log('blocked.subfleet_marked', level='warn', task=hit['task'], run=hit['run'], notice=hit['notice'],
          reason=reason[:300])
    if not had:  # once per mark: a repeat BLOCKED on the same episode is not pushed again
        push(f, 'sub-fleet ' + str(hit['task']) + ' waits for a human: the root supervisor answered BLOCKED on its ['
             'sub-fleet ' + hit['notice'] + '] notice: ' + reason[:500] + '. The root goes on. See it with: fleetmux '
             'status --config ' + _config(f.cfg))
    return True


def _config(cfg):
    return str(Path(cfg['run']) / 'config.json')


def _note(f, hit, reason):
    """[Sub-fleet <id> completed|failed] / [job <id>]: nothing to mark (the sub-fleet has ended, or a running job's
    sub-fleet would lose a mark on the next watch); audit, and push once."""
    job = hit['notice'] == 'job'
    f.log('blocked.job_noted' if job else 'blocked.subfleet_noted', level='warn', task=hit['task'], run=hit['run'],
          notice=hit['notice'], reason=reason[:300])
    head = hit['text'].split('\n', 1)[0][:200]
    about = ('job ' + hit['task'] + (' (its sub-fleet ' + hit['run'] + ')' if hit['run'] else ' (no sub-fleet yet)')
             if job else 'sub-fleet ' + hit['task'] + ' (' + hit['run'] + ')')
    push_once(f, hit['notice'] + '|' + (hit['run'] or hit['task']) + '|' + hit['text'],
              'the root supervisor answered BLOCKED on a notice about ' + about + ': "' + head + '". Its reason: '
              + reason[:500] + '. The root goes on; the root was not paused for it.')
    return True


def _absorb_report(f, hit, reason):
    from fleetmux import fleet as legacy
    key, run = hit['key'], hit['run']
    esc = f.escalation_state()
    rec = esc[key]
    if not hit['active']:
        drop(f, esc, key, rec, 'sub-fleet ' + str(rec.get('task')) + ' ended (' + str(subfleet.record(run).get('state'))
             + ')')
        f.save_escalations(esc)
        return True
    if rec.get('status') == AWAITING:
        f.log('blocked.report_again', report=key, reason=reason[:300])
        return True
    twin = next((k for k, r in esc.items() if k != key and r.get('kind') == 'report' and r.get('status') == AWAITING
                 and rec.get('fingerprint') and r.get('fingerprint') == rec['fingerprint']), None)
    if twin:  # the same request delivered before its twin went to the user: one request, one answer, one push
        esc[twin]['aliases'] = list(esc[twin].get('aliases') or []) + [key] + list(rec.get('aliases') or [])
        esc.pop(key)
        f.save_escalations(esc)
        f.log('report.resent_held', report=twin, alias=key, by='supervisor BLOCKED')
        return True
    now, cfg = time.time(), _config(f.cfg)
    rec.update(status=AWAITING, blocked_reason=reason[:2000], blocked_at=now, blocked_by=f.label('supervisor'),
               child_run=str(run))
    rec.pop('nudged', None)
    crec = subfleet.record(run)
    marks = dict(crec.get('attention_reports') or {})
    marks[key] = {'reason': reason[:2000], 'what': rec.get('what', ''), 'who': rec.get('who', ''),
                  'paths': rec.get('paths') or [], 'task': rec.get('task'), 'by': 'supervisor BLOCKED',
                  'label': f.label('supervisor'), 'at': now,
                  'answer': 'fleetmux answer ' + key + ' approve|reject [--note TEXT] --config ' + cfg}
    crec['attention_reports'] = marks
    subfleet.save_record(run, crec)
    try:  # R2: one line down the ruling path; the sub-fleet keeps the report pending and its clock stopped
        folder = Path(rec['origin_run']) / 'subfleet-vetoes'
        folder.mkdir(parents=True, exist_ok=True)
        legacy.save(folder / (uuid.uuid4().hex[:12] + '.json'), {'kind': PENDING_KIND, 'ref': key, 'note': reason[:500]})
    except OSError as exc:
        f.log('report.pending_failed', level='warn', report=key, error=str(exc)[:300])
    rec['push_status'] = push(f, 'sub-fleet ' + str(rec.get('task')) + ' waits for you: its request ' + key + ' ('
                              + (rec.get('who') + ': ' if rec.get('who') else '') + str(rec.get('what', ''))[:300]
                              + (' ; paths: ' + ', '.join(rec.get('paths')) if rec.get('paths') else '')
                              + ') went to the root supervisor, who answered BLOCKED: ' + reason[:500]
                              + '. The root goes on; only this request waits.\nAnswer: fleetmux answer ' + key
                              + ' approve|reject [--note TEXT] --config ' + cfg) or ''
    rec['pushed_at'] = time.time()
    esc[key] = rec
    f.save_escalations(esc)
    f.log('blocked.report_awaiting', level='warn', report=key, task=rec.get('task'), run=str(run), reason=reason[:300])
    return True


# ------------------------------------------------------------------ push
def push(f, text):
    """One notice for the user through the run's existing outbound path (never at a fleet with a parent): a managed
    root -> lifecycle_close.notify; otherwise notify_route.unmanaged_send (channels via RuntimeV2.channel_alarm, else
    counted in notify-undelivered.json). Returns a short status; never raises."""
    if f.cfg.get('parent_config'):
        return None
    from fleetmux import lifecycle_close, notify_route
    try:
        if lifecycle_close.managed(f):
            from fleetmux import lifecycle
            try:
                policy = lifecycle.get_mode(f.cfg['lifecycle'])
            except (ValueError, KeyError, TypeError):
                policy = {}
            out = lifecycle_close.notify(f, EVENT, '[fleet ' + lifecycle_close.fleet_name(f) + '] ' + text, policy)
            return ','.join(str(x.get('status')) for x in out) if isinstance(out, list) else 'sent'
        return notify_route.unmanaged_send(f, EVENT, text) or 'counted'
    except Exception as exc:  # noqa: BLE001  (a notice never takes the daemon down)
        f.log('blocked.push_failed', level='error', error=str(exc)[:300])
        return 'failed'


def _json_get(f, name):
    try:
        value = json.loads(f.r.get(name) or '{}')
    except ValueError:
        value = {}
    return value if isinstance(value, dict) else {}


def _json_set(f, name, value, order):
    if len(value) > KEEP:
        value = dict(sorted(value.items(), key=order)[-KEEP:])
    f.r.set(name, json.dumps(value, ensure_ascii=False, separators=(',', ':')))


def push_once(f, what, text):
    seen = _json_get(f, PUSHED)
    mark = hashlib.sha1(what.encode('utf-8', 'replace')).hexdigest()[:20]
    if mark in seen:
        return None
    seen[mark] = time.time()
    _json_set(f, PUSHED, seen, lambda kv: kv[1])
    return push(f, text)


# ------------------------------------------------------------------ re-sent requests, closing, dropping
def fingerprint(note):
    """The same request again (ruling 5): origin run + worker + paths + the request text, whitespace and case folded
    and a leading 'Re-sent, still open' marker dropped. ``why`` is left out."""
    norm = lambda s: ' '.join(str(s or '').split()).lower()  # noqa: E731
    what = RESENT_LEAD.sub('', str(note.get('what') or '').strip())
    paths = sorted(os.path.normpath(str(p)) for p in note.get('paths') or [])
    raw = json.dumps([os.path.realpath(str(note.get('origin_run') or '')), norm(note.get('who')), paths, norm(what)])
    return hashlib.sha256(raw.encode('utf-8', 'replace')).hexdigest()[:16]


def resend_held(f, note):
    """report_intake: True when ``note`` repeats a request that already waits for the user. It is not queued to the
    supervisor; its key joins that record's ``aliases`` (the answer settles it too)."""
    if f.cfg.get('parent_config') or not on():
        return False
    fp, esc = fingerprint(note), f.escalation_state()
    for k, r in esc.items():
        if r.get('kind') == 'report' and r.get('status') == AWAITING and r.get('fingerprint') == fp:
            r['aliases'] = list(r.get('aliases') or []) + [note['id']]
            r.update(resent=int(r.get('resent') or 0) + 1, resent_at=time.time())
            f.save_escalations(esc)
            f.log('report.resent_held', report=k, alias=note['id'])
            return True
    return False


def closed_rows(f):
    return _json_get(f, CLOSED)


def closed(f, key, rec, decision, by, why=''):
    """A report record left fx:escalations: the closed ledger (each key and alias) and the sub-fleet's mark."""
    keys = [key] + list(rec.get('aliases') or [])
    ledger = closed_rows(f)
    row = {'decision': decision, 'by': by, 'why': why, 'at': time.time()}
    ledger.update({k: row for k in keys})
    _json_set(f, CLOSED, ledger, lambda kv: kv[1].get('at') or 0)
    run = rec.get('child_run') or (own_child(f, rec.get('origin_run')) if rec.get('origin_run') else None)
    if run:
        crec = subfleet.record(run)
        marks = dict(crec.get('attention_reports') or {})
        if any(marks.pop(k, None) for k in keys):
            if marks:
                crec['attention_reports'] = marks
            else:
                crec.pop('attention_reports', None)
            subfleet.save_record(run, crec)
            f.log('subfleet.attention_cleared', task=crec.get('task_id'), run=str(run), why='report ' + key + ' '
                  + decision + ' by ' + by)


def drop(f, esc, key, rec, why):
    """An awaiting report whose sub-fleet ended: dropped, closed in the ledger (``esc`` changed in place)."""
    esc.pop(key, None)
    closed(f, key, rec, 'dropped', 'program', why)
    f.log('report.awaiting_dropped', level='warn', report=key, reason=why)


def awaiting_tick(f, esc, key, rec):
    """escalation_tick: True when an awaiting report was dropped because its sub-fleet is no longer active."""
    if rec.get('status') != AWAITING:
        return False
    run = rec.get('child_run')
    state = subfleet.record(run).get('state') if run else None
    if state in subfleet.ACTIVE:
        return False
    drop(f, esc, key, rec, 'sub-fleet ' + str(rec.get('task')) + ' ended (' + str(state or 'gone') + ')')
    return True


# ------------------------------------------------------------------ fleetmux answer
def _closed_reason(key, row):
    return (key + ' is already closed (' + str(row.get('decision')) + ' by ' + str(row.get('by'))
            + (': ' + row['why'] if row.get('why') else '') + ', '
            + time.strftime('%Y-%m-%d %H:%M', time.localtime(float(row.get('at') or 0))) + ')')


def user_answer(f, ev):
    """The daemon side of ``fleetmux answer``: the user's own ruling on a report that waits for the user, applied
    through Fleet.escalation_apply. Returns the reply ({status: accepted | rejected, ...})."""
    key, decision = str(ev.get('key') or ''), str(ev.get('decision') or '')
    note = str(ev.get('note') or '').strip()[:2000]
    if not key.startswith(REPORT_KEY):
        return {'status': 'rejected', 'reason': 'only a report:<task>:<id> key that waits for you can be answered '
                                                'here (got ' + key[:80] + ')'}
    if decision not in ('approve', 'reject'):
        return {'status': 'rejected', 'reason': 'answer approve or reject'}
    esc = f.escalation_state()
    hit = key if key in esc else next((k for k, r in esc.items() if key in (r.get('aliases') or [])), None)
    if hit is None:
        row = closed_rows(f).get(key)
        if row:
            return {'status': 'rejected', 'reason': _closed_reason(key, row)}
        return {'status': 'rejected', 'reason': 'unknown key ' + key + ' in this fleet (' + str(f.run_dir)
                                                + '); see fleetmux status --config ' + _config(f.cfg)}
    rec = esc[hit]
    if rec.get('kind') != 'report':
        return {'status': 'rejected', 'reason': key + ' is not a sub-fleet report'}
    if rec.get('status') != AWAITING:
        return {'status': 'rejected', 'reason': key + ' is still with the supervisor, not waiting for you'}
    f.escalation_apply({}, None, hit, rec, {'task': hit, 'decision': decision, 'note': note, 'by': 'user'})
    f.log('report.user_answered', level='warn', report=hit, decision=decision, by='user', pid=str(ev.get('pid') or ''),
          note=note[:300])
    return {'status': 'accepted', 'key': hit, 'decision': decision}


def caller_problem(cfg):
    """Best effort (ruling 12): ``fleetmux answer`` is the user's approval, so it is refused when it runs from an agent
    pane of this fleet: the working directory, $PWD or cmux's $CMUX_AGENT_LAUNCH_CWD inside this run's directory
    (every agent of the run and of its sub-fleets works there). Not an isolation boundary."""
    run = os.path.realpath(str(cfg.get('run') or ''))
    if not cfg.get('run'):
        return None
    seen = []
    try:
        seen.append(os.getcwd())
    except OSError:
        pass
    seen += [os.environ[k] for k in ('PWD', 'CMUX_AGENT_LAUNCH_CWD') if os.environ.get(k)]
    for p in seen:
        real = os.path.realpath(p)
        if real == run or real.startswith(run + os.sep):
            return ('fleetmux answer is the user\'s own approval; refused in an agent pane of this fleet (' + p
                    + ' is inside its run directory ' + run + '). Run it from your own terminal outside that directory '
                    '(a best-effort check, not an isolation boundary).')
    return None


def answer_send(cfg, key, decision, note='', wait=30):
    """``fleetmux answer``: post the user's ruling to the run's daemon over the fleet bus and wait for its reply."""
    problem = caller_problem(cfg)
    if problem:
        return {'status': 'rejected', 'reason': problem}
    if cfg.get('transport_version') != 2:
        return {'status': 'rejected', 'reason': 'answer needs a transport v2 run (the fleet bus)'}
    from fleetmux import fleet as legacy, runtime_v2
    r = legacy.connect(cfg)
    try:
        bus = runtime_v2.bus_for(cfg, r)
        correlation = 'ui-' + uuid.uuid4().hex
        runtime_v2.post_event(bus, dict(kind='escalation_answer', key=key, decision=decision, note=note, by='user',
                                        pid=os.getpid(), run_id=bus.run_id, request_id=correlation), correlation)
        return runtime_v2.wait_reply(bus, correlation, timeout=wait)
    finally:
        r.close()
        r.connection_pool.disconnect()


# ------------------------------------------------------------------ status
def clear(f, d, rec, why):
    """Drop the stuck / held mark from ``rec`` (the caller saves it). True when there was one. A report mark
    (``attention_reports``) stays: it leaves only with an answer or when the report is dropped."""
    if not rec.pop('attention', None):
        return False
    f.log('subfleet.attention_cleared', task=rec.get('task_id'), run=str(d), why=why)
    return True


def rows(cfg):
    """status: every active sub-fleet of this run that waits for a human, with its mark, and one row per report that
    waits for the user (with the command that answers it)."""
    out = []
    for d in subfleet.active_children(cfg['run']):
        rec = subfleet.record(d)
        name = rec.get('task_id') or d.name
        if rec.get('attention'):
            out.append(dict(rec['attention'], subfleet=name, run=str(d)))
        for key, mark in sorted((rec.get('attention_reports') or {}).items()):
            out.append(dict(mark, subfleet=name, run=str(d), notice='report', key=key))
    return out
