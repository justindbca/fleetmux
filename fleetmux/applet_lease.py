"""Applet leases (docs/applets.md sections 3 to 8, docs/applet-interface.md sections 3, 4 and 9).

The host daemon (the fleet that declares ``applets:``) runs every rule here: the op gate inside the request
event, the supervisor's ``applet`` decision key, and the clock on the daemon tick. The large modules only call in.

Redis (host)
  fx:applet:<name>                 hash: ``paused`` (this module); the rest is applet_host's and the SDK's
  fx:applet:<name>:leases          set of lease ids not yet ended
  fx:applet:<name>:lease:<id>      hash: the lease record (applet-interface.md section 3.3)
  fx:applet:<name>:esc:<op id>     hash: an open or answered escalation (kept 7 days after it is answered)
  fx:applet:<name>:events          stream: host -> applet events (field ``data``)
  fx:applet:<name>:notices         list: the last ``notices_max`` applet.notify texts and human-class escalation
                                   notices in full (JSON per item)
  fx:agent:<slot>                  fields applet_lease, applet_lease_holder, applet_lease_until, applet_review,
                                   applet_added_for
  fx:applet:op:<id>, fx:applet:opq internal: ops waiting for a decision (not part of the interface)
  fx:applet:<name>:open            internal list of open lease ids; the tick mirrors it into ``:leases``
  fx:applet:<name>:notify          internal list of notify times (rate limit)
  fx:applet:expire                 internal list of ended records the tick gives their keep time

Inside a v2 transaction ``f.r`` is the runtime's overlay: only string / hash / list keys are written there
(``HSET`` with a mapping, lists, ``XADD``); sets, trims and key expiry are applied on the tick.
  fx:applet:lctl, fx:applet:lctl:<id>  internal: operator requests from the CLI and their answers
"""
import hashlib
import json
import re
import secrets
import time

from fleetmux import awake

KEY = 'fx:applet:'
OP = 'fx:applet:op:'
OPQ = 'fx:applet:opq'
CTL = 'fx:applet:lctl'
EXPIRE = 'fx:applet:expire'
HOST = 'fx:applet:lhost'  # internal: last host paused flag seen by the tick (host.paused / host.resumed)
OPS = ('applet.lease.request', 'applet.lease.renew', 'applet.lease.release', 'applet.escalate',
       'applet.notify', 'applet.audit')
REASONS = ('bad_token', 'bad_version', 'applet_not_up', 'applet_paused', 'applet_detaching', 'host_paused',
           'host_draining', 'max_active', 'max_renewals', 'renew_pending', 'worker_limit', 'review_cap', 'invalid',
           'lease_not_active', 'wrong_slot', 'slot_leased', 'message_in_flight', 'rate_limited', 'duplicate',
           'no_decision', 'supervisor')
OPEN = ('requested', 'granted', 'active', 'renew_requested')
END_REASONS = ('supervisor', 'operator', 'host_down', 'applet_down', 'applet_detached', 'slot_held', 'slot_retired',
               'prepare_failed')
# A lease still waiting for the supervisor (``requested``) can only be ended by these causes; each maps to the
# (``by``, stable reason) its request is answered with. Term checks never apply before the term starts.
REQUEST_END = {'supervisor': ('supervisor', 'supervisor'), 'operator': ('operator', 'supervisor'),
               'host_down': ('program', 'host_draining'), 'applet_down': ('program', 'applet_not_up'),
               'applet_detached': ('program', 'applet_detaching')}
CLASSES = ('plan', 'scope', 'access', 'delete', 'human')
HUMAN_CLASSES = ('delete', 'human')  # ruled by the human owner only, on one path (applet-interface.md 3.7)
VERSIONS = (1,)
JOB_RE = r'[A-Za-z0-9._-]{1,64}'
NAME_RE = r'applet-[a-z][a-z0-9]*(-[a-z0-9]+)*-[0-9]+'
EVENT_RE = r'[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*'
SLOT_FIELDS = ('applet_lease', 'applet_lease_holder', 'applet_lease_until')
SETTINGS = {'retry_after_s': 60, 'keep_s': 7 * 86400, 'events_max': 1000, 'notify_window_s': 3600,
            'ctl_wait_s': 30, 'limit_note_s': 600, 'add_wait_s': 600, 'notices_max': 50}
BOOT = secrets.token_hex(4)  # this daemon process: a lease issued by an earlier one ended with its host (host_down)
clock = awake.now  # every lease time is awake seconds (awake.py): a sleep of the machine is not lease time


class Refused(Exception):
    """A stable refusal (applet-interface.md section 9): ``reason`` is a code of REASONS."""
    def __init__(self, reason, note='', by='program', **extra):
        super().__init__(reason + (': ' + note if note else ''))
        self.reason, self.note, self.by, self.extra = reason, note, by, extra


def _in_progress():
    from fleetmux import named_subfleet
    return named_subfleet.InProgress


def _fleet():
    from fleetmux import fleet
    return fleet


def settings(cfg):
    return {**SETTINGS, **((cfg or {}).get('applet_lease') or {})}


def hkey(name):
    return KEY + name


def leases_key(name):
    return KEY + name + ':leases'


def open_key(name):
    return KEY + name + ':open'


def _src(f):
    """The real connection for reads the transaction overlay does not offer (streams, sets, scans)."""
    return getattr(f.r, 'source', f.r)


def _keep(f, k):
    f.r.rpush(EXPIRE, k)


def lease_key(name, lid):
    return KEY + name + ':lease:' + lid


def esc_key(name, oid):
    return KEY + name + ':esc:' + oid


def events_key(name):
    return KEY + name + ':events'


def notices_key(name):
    return KEY + name + ':notices'


def notices(r, name, last=None):
    """The kept notices of ``name`` (``applet.notify`` texts, human-class escalation notices), oldest first (the
    last ``last`` of them when given)."""
    return [_loads(x, {}) for x in r.lrange(notices_key(name), -last if last else 0, -1)]


def keep_notice(f, name, item):
    """Keep one notice in full for ``fleetmux applet notices`` (the last ``notices_max``)."""
    f.r.rpush(notices_key(name), _j(item))
    f.r.ltrim(notices_key(name), -int(settings(f.cfg)['notices_max']), -1)


def specs(f):
    from fleetmux import applet_host  # an applet being detached keeps its entry until it has stopped (2.1)
    return dict(applet_host.detaching(f), **{e['name']: e for e in (getattr(f, 'applet_specs', None) or [])})


def _j(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _loads(raw, default):
    try:
        return json.loads(raw) if raw else default
    except ValueError:
        return default


def _audit(f, name, **fields):
    """One audit record with the fields of applet-interface.md section 6. The audit writers reserve two names:
    ``event`` (the record's own name, v2) becomes ``applet_event``; ``kind`` (``Fleet.log``'s first parameter,
    legacy transport only) becomes ``lease_kind``."""
    fields = {('applet_event' if k == 'event' else k): v for k, v in fields.items()}
    if getattr(f, 'v2', None):
        f.v2.log(name, fields)
    else:
        f.log(name, **{('lease_kind' if k == 'kind' else k): v for k, v in fields.items()})


def event(f, name, kind, **fields):
    data = dict(type=kind, at=clock(), **fields)
    f.r.xadd(events_key(name), {'data': _j(data)})  # trimmed on the tick


def record(r, name, lid):
    return r.hgetall(lease_key(name, lid))


def find(r, lid, names):
    """(applet, record) of lease ``lid`` among the applets ``names``, or (None, {})."""
    for name in names:
        rec = record(r, name, lid)
        if rec:
            return name, rec
    return None, {}


def all_names(r):
    """Applets that have a hash on this host (declared or not)."""
    out = set()
    for k in r.scan_iter(KEY + '*'):
        rest = k[len(KEY):]
        if re.fullmatch(NAME_RE, rest):  # applet names only, not the internal keys under the same prefix
            out.add(rest)
    return sorted(out)


def leased(f, slot):
    """Lease id held on ``slot``, '' when it is free."""
    return (f.r.hget('fx:agent:' + slot, 'applet_lease') or '') if slot else ''


def reviewing(f, slot):
    """Review id the slot is running, '' when none."""
    return (f.r.hget('fx:agent:' + slot, 'applet_review') or '') if slot else ''


def taken(f, slot):
    """Leased or reviewing: off limits for the scheduler and the supervisor (applets.md section 4)."""
    return leased(f, slot) or reviewing(f, slot)


# ------------------------------------------------------------------ the op door (inside the request event)
def is_op(spec):
    return isinstance(spec, dict) and spec.get('op') in OPS


def _check_id(f, rid):
    fleet = _fleet()
    if not isinstance(rid, str) or not re.fullmatch(fleet.REQ_ID_RE, rid):
        raise ValueError('invalid: id must match ' + fleet.REQ_ID_RE)
    op = f.r.hgetall(OP + rid)
    if op:
        raise _in_progress()(rid, op.get('status') or 'awaiting')
    if f.r.hgetall(fleet.REQ + rid) or f.r.get(fleet.RESULT + rid) or (
            f.v2 and f.r.get(f.v2.prefix + ':public:' + _digest(rid))):
        raise ValueError('duplicate: request id ' + rid + ' was used; its first result stays at fx:result:' + rid)


def _digest(rid):
    from fleetmux import runtime_v2
    return runtime_v2.digest(rid)


def token_ok(h, token):
    return bool(h.get('token_sha256')) and isinstance(token, str) and \
        hashlib.sha256(token.encode()).hexdigest() == h['token_sha256']


def up(h):
    return h.get('state') == 'up' and bool(h.get('pid'))


def draining(f, name):
    for _, fields in _src(f).xrevrange(events_key(name), count=50):
        data = _loads(fields.get('data'), {})
        if data.get('type') == 'host.draining' and data.get('reason') != 'detach':  # a detach: applet_detaching
            return True
    return False


def gate_applet(f, spec, *, host=True, new_lease=False):
    """Gate steps 1 and 2: the caller is the running applet it names, the host takes work."""
    name = spec.get('applet')
    h = f.r.hgetall(hkey(name)) if isinstance(name, str) and name in specs(f) else {}
    if not token_ok(h, spec.get('token')):
        raise Refused('bad_token')
    if not up(h):
        raise Refused('applet_not_up', 'applet state is ' + (h.get('state') or 'unknown'))
    if new_lease and h.get('detaching') == '1':  # applet_host.DETACHING: the row was removed (applets.md 2.1)
        raise Refused('applet_detaching')
    if spec.get('v') not in VERSIONS:
        raise Refused('bad_version', 'supported: ' + ', '.join(map(str, VERSIONS)))
    if host and f.paused():
        raise Refused('host_paused')
    if host and draining(f, name):
        raise Refused('host_draining')
    return name, h


def request(f, spec):
    """One applet op. ValueError: refused before it got an id; InProgress: the same open op resent."""
    rid = spec.get('id')
    _check_id(f, rid)
    op = spec['op']
    try:
        if op == 'applet.lease.request':
            return lease_request(f, rid, spec)
        if op == 'applet.lease.renew':
            return lease_renew(f, rid, spec)
        if op == 'applet.lease.release':
            return lease_release(f, rid, spec)
        if op == 'applet.escalate':
            return escalate(f, rid, spec)
        if op == 'applet.notify':
            return notify(f, rid, spec)
        return audit_op(f, rid, spec)
    except Refused as exc:
        refuse(f, rid, exc)
        return rid


def refuse(f, rid, exc):
    f.publish(rid, 'rejected', by=exc.by, reason=exc.reason, note=exc.note, **exc.extra)


def _text(spec, field, limit, required=True):
    value = spec.get(field)
    if value is None and not required:
        return ''
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise Refused('invalid', field + ' must be text of 1 to ' + str(limit) + ' characters')
    return value


def _paths(value):
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or not all(isinstance(p, str) and p.startswith('/') for p in value):
        raise Refused('invalid', 'paths must be a list of absolute paths')
    import os.path
    return [os.path.normpath(p) for p in value]


def _json_size(value, field, limit):
    if not isinstance(value, dict) or len(_j(value)) > limit:
        raise Refused('invalid', field + ' must be a JSON object of at most ' + str(limit) + ' characters')
    return value


def _number(value, field, lo, hi):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value:
        raise Refused('invalid', field + ' must be a number of seconds >= ' + str(lo))
    return min(float(value), float(hi))


def open_ids(f, name):
    return f.r.lrange(open_key(name), 0, -1)


def jobs_slots(f, name, job):
    """Slots that held a lease of this applet for ``job`` (any state, records kept): never its reviewers."""
    out = set()
    for k in _src(f).scan_iter(lease_key(name, 'L-*')):
        rec = f.r.hgetall(k)
        if rec.get('job') == job and rec.get('slot'):
            out.add(rec['slot'])
    return out


def nested_supervisors(f):
    from fleetmux import named_subfleet
    return {e.get('supervisor_slot') for e in named_subfleet.declared(f).values()}


def idle(f, slot):
    """Free for a new lease: idle, READY, nothing queued, not leased or reviewing, not a sub-fleet's supervisor."""
    info = f.agent(slot)
    if slot not in f.workers or info.get('state') != 'idle' or taken(f, slot) or f.inbox_depth(slot):
        return False
    if f.v2 and f.r.hget(f.v2.bus.lease_key(slot), 'current_delivery'):
        return False
    return slot not in nested_supervisors(f)


def asleep(f, slot):
    """Put to sleep by the idle reclaim (no session, no pane): a grant wakes it."""
    return slot in f.workers and f.agent(slot).get('state') == 'reclaimed' and not taken(f, slot)


def eligible(f, entry):
    """The entry's eligible slots plus the workers the host added for this applet (section 3.3)."""
    from fleetmux import applet_ops
    added = applet_ops.added(f, entry['name'])
    return [s for s in f.workers if s in (entry['lease'].get('eligible') or f.workers) or s in added]


def supply(f, entry, exclude=()):
    """Where a slot can come from now (applets.md 3.3): ('idle', [slots]), ('asleep', [slots]),
    ('add', [new name]) or ('limit', [why])."""
    from fleetmux import applet_ops
    pool = [s for s in eligible(f, entry) if s not in exclude]
    free = [s for s in pool if idle(f, s)]
    if free:
        return 'idle', free
    sleeping = [s for s in pool if asleep(f, s)]
    if sleeping:
        return 'asleep', sleeping
    why = applet_ops.add_problem(f, entry)
    return ('limit', [why]) if why else ('add', [applet_ops.next_name(f, entry)])


def limit_note(f, rid, name, why):
    """At the worker ceiling: one line to the supervisor, at most every ``limit_note_s`` per applet."""
    k = KEY + name + ':limit_note'
    now = clock()
    if now - float(f.r.get(k) or 0) < settings(f.cfg)['limit_note_s']:
        return
    f.r.set(k, now)
    _tell(f, rid, '[applet ' + name + '] worker limit reached (' + why + '); its lease requests are refused '
          'worker_limit until a worker is free or hot_add_max is raised (`fleetmux applet set ' + name
          + ' hot_add_max <n>`). Information only; no answer is expected.')


def lease_request(f, rid, spec):
    name, h = gate_applet(f, spec, new_lease=True)
    if h.get('paused') == '1':
        raise Refused('applet_paused')
    entry = specs(f)[name]
    lc = entry['lease']
    if len(open_ids(f, name)) >= int(lc['max_active']):
        raise Refused('max_active', 'open leases: ' + ', '.join(open_ids(f, name)))
    job = spec.get('job')
    if not isinstance(job, str) or not re.fullmatch(JOB_RE, job):
        raise Refused('invalid', 'job must be 1 to 64 characters [A-Za-z0-9._-]')
    purpose = _text(spec, 'purpose', 2000)
    ttl = _number(spec.get('ttl_s', lc['ttl_s']), 'ttl_s', 60, lc['ttl_max_s'])
    paths = _paths(spec.get('paths'))
    want = spec.get('want') or {}
    context = _json_size(spec.get('context') or {}, 'context', 20000)
    if not isinstance(want, dict):
        raise Refused('invalid', 'want must be an object')
    lid = 'L-' + secrets.token_hex(4)
    now = clock()
    rec = {'id': lid, 'applet': name, 'job': job, 'purpose': purpose, 'added': 0,
           'slot': '', 'state': 'requested', 'requested_at': now, 'granted_at': '', 'active_at': '',
           'expires_at': '', 'ended_at': '', 'ttl_s': ttl, 'renewals': 0, 'paths': _j(paths), 'granted_by': '',
           'messages': 0, 'last_message': '', 'last_status': '', 'end_reason': '', 'outcome': '',
           'op': rid, 'boot': BOOT, 'applet_started_at': h.get('started_at') or '', 'inflight': '[]'}
    how, found = supply(f, entry)
    if how == 'limit':
        limit_note(f, rid, name, found[0])
        raise Refused('worker_limit', found[0], retry_after_s=settings(f.cfg)['retry_after_s'])
    f.r.hset(lease_key(name, lid), mapping={k: str(v) for k, v in rec.items()})
    f.r.rpush(open_key(name), lid)
    _open_op(f, rid, name, 'request', lid, lc['decision_wait_s'])
    _audit(f, 'applet.lease.requested', applet=name, lease=lid, job=job, slot='', added=0, ttl_s=ttl,
          by='applet', reason='')
    board = {'idle': 'Idle eligible workers now: ' + ', '.join(f.label(s) for s in found),
             'asleep': 'No worker is idle. Asleep eligible workers (a grant wakes the one you name): '
                       + ', '.join(found),
             'add': 'No worker is free; a grant adds worker ' + found[0] + ' (omit "slot").'}[how]
    _tell(f, rid, '[applet lease request] ' + rid + ': ' + name + ' asks for one worker.\n'
            + 'Job: ' + job
            + '. Term: ' + str(int(ttl)) + ' s.\nPurpose: ' + purpose
            + '\nPaths: ' + (', '.join(paths) or 'none') + '\nHint: ' + _j(want) + '\nContext (JSON): ' + _j(context)
            + '\n' + board
            + '\nAnswer in your service decision JSON (status RUNNING, dispatch [], skip []) with "applet": [{"id": "'
            + rid + '", "decision": "grant", "slot": "<worker>", "ttl_s": <seconds>, "note": "..."}] or '
            '{"id": "' + rid + '", "decision": "deny", "note": "<why>"}. Paths may only narrow what was asked.')
    return rid


def _tell(f, rid, text):
    """One message to the host supervisor about op ``rid`` (v2: a follow-up bound to the op, so the runtime does
    not drop it for having no request record)."""
    if getattr(f, 'v2', None):
        f.v2.queue('supervisor', text, request_id=rid, kind='followup')
    else:
        f.queue('supervisor', text)


def _open_op(f, rid, name, kind, lid, wait_s):
    f.r.hset(OP + rid, mapping={'applet': name, 'op': kind, 'lease': lid, 'status': 'awaiting',
                                'created': clock(), 'wait_s': wait_s if wait_s is not None else ''})
    f.r.rpush(OPQ, rid)


def _close_op(f, rid):
    f.r.delete(OP + rid)
    f.r.lrem(OPQ, 0, rid)


def _own_lease(f, spec, name):
    lid = spec.get('lease')
    rec = record(f.r, name, lid) if isinstance(lid, str) else {}
    if not rec or rec.get('state') not in OPEN:
        raise Refused('lease_not_active', (lid or 'no lease') + ' is ' + (rec.get('state') or 'unknown'))
    return lid, rec


def lease_renew(f, rid, spec):
    name, _ = gate_applet(f, spec, host=False)
    lid, rec = _own_lease(f, spec, name)
    lc = specs(f)[name]['lease']
    if rec['state'] == 'renew_requested':
        raise Refused('renew_pending')
    if rec['state'] != 'active':
        raise Refused('lease_not_active', lid + ' is ' + rec['state'])
    if int(rec.get('renewals') or 0) >= int(lc['max_renewals']):
        raise Refused('max_renewals', str(lc['max_renewals']) + ' renewals used')
    need = spec.get('need_s')
    if isinstance(need, bool) or not isinstance(need, (int, float)) or not 60 <= need <= lc['ttl_max_s']:
        raise Refused('invalid', 'need_s must be 60 to ' + str(lc['ttl_max_s']))
    progress = _text(spec, 'progress', 2000)
    why = _text(spec, 'why', 2000, required=False)
    f.r.hset(lease_key(name, lid), mapping={'state': 'renew_requested', 'renew_op': rid, 'need_s': need})
    _open_op(f, rid, name, 'renew', lid, lc['decision_wait_s'])
    _audit(f, 'applet.lease.renew_requested', applet=name, lease=lid, need_s=need, ttl_s=float(rec['ttl_s']),
          renewals=int(rec.get('renewals') or 0), by='applet')
    _tell(f, rid, '[applet lease renewal] ' + rid + ': ' + name + ' asks ' + str(int(need)) + ' s more for '
            + lid + ' on ' + f.label(rec['slot']) + ' (job ' + rec['job'] + ', ends '
            + time.strftime('%H:%M', time.localtime(float(rec['expires_at'] or 0))) + ', renewals '
            + str(rec.get('renewals') or 0) + ' of ' + str(lc['max_renewals']) + ').\nProgress: ' + progress
            + ('\nWhy: ' + why if why else '') + '\nAnswer with "applet": [{"id": "' + rid + '", "decision": '
            '"grant", "ttl_s": <= ' + str(int(need)) + ', "note": "..."}] or {"id": "' + rid + '", "decision": '
            '"deny", "note": "<why>"} in your service decision JSON (status RUNNING, dispatch [], skip []).')
    return rid


def inflight(f, rec):
    """Messages of a lease that have no result yet."""
    fleet = _fleet()
    return [m for m in _loads(rec.get('inflight'), []) if not f.r.get(fleet.RESULT + m)]


def lease_release(f, rid, spec):
    name, _ = gate_applet(f, spec, host=False)
    lid, rec = _own_lease(f, spec, name)
    outcome = _json_size(spec.get('outcome') or {}, 'outcome', 4000)
    if 'force' in spec and type(spec['force']) is not bool:
        raise Refused('invalid', 'force must be true or false')
    if rec['state'] == 'requested':
        raise Refused('lease_not_active', lid + ' is not granted yet')
    if inflight(f, rec) and not spec.get('force'):
        raise Refused('message_in_flight', 'unanswered: ' + ', '.join(inflight(f, rec)))
    f.r.hset(lease_key(name, lid), mapping={'outcome': _j(outcome)})
    end(f, name, lid, 'released', '', 'released by the applet')
    f.publish(rid, 'ok', lease={'id': lid, 'state': 'released'})
    return rid


# ------------------------------------------------------------------ escalation, notice, audit ops
def escalate(f, rid, spec):
    name, _ = gate_applet(f, spec, host=False)
    cls = spec.get('class')
    if cls not in CLASSES:
        raise Refused('invalid', 'class must be one of ' + ', '.join(CLASSES))
    what, why = _text(spec, 'what', 2000), _text(spec, 'why', 2000)
    options = spec.get('options') or []
    if not isinstance(options, list) or len(options) > 5 or not all(isinstance(o, str) and o for o in options):
        raise Refused('invalid', 'options must be at most 5 texts')
    paths = _paths(spec.get('paths'))
    if cls in ('access', 'delete') and not paths:
        raise Refused('invalid', 'class ' + cls + ' needs paths')
    wait = spec.get('wait_s')
    if wait is not None:
        wait = _number(wait, 'wait_s', 10, 86400)
    job = spec.get('job') or ''
    root_host = not f.cfg.get('parent_config')
    human = cls in HUMAN_CLASSES
    status = 'awaiting_human' if human else 'awaiting'
    f.r.hset(esc_key(name, rid), mapping={'id': rid, 'applet': name, 'class': cls, 'what': what, 'why': why,
                                         'job': job, 'options': _j(options), 'paths': _j(paths), 'status': status,
                                         'created': clock(), 'decision': '', 'choice': '', 'by': '', 'note': ''})
    _open_op(f, rid, name, 'escalate', '', wait)
    f.r.hset(OP + rid, mapping={'status': status})
    _audit(f, 'applet.escalated', applet=name, id=rid, **{'class': cls}, decision='', by='applet')
    body = (name + ' (' + cls + (', job ' + job if job else '') + '): ' + what + '\nWhy: ' + why
            + ('\nOptions: ' + '; '.join(str(i) + ' = ' + o for i, o in enumerate(options)) if options else '')
            + ('\nPaths: ' + ', '.join(paths) if paths else ''))
    if human:
        from fleetmux import tabs
        import shlex
        from pathlib import Path
        cmd = '`fleetmux applet answer ' + rid
        config = ' --config ' + shlex.quote(str(Path(f.cfg['run']) / 'config.json'))  # works from any shell
        listed = ', '.join(str(i) + ' = ' + ' '.join(o.split())[:60] for i, o in enumerate(options))  # one line
        hint = (cmd + ' approve --choice <n>' + config + '` (n: ' + listed + ') or ' + cmd + ' reject' + config + '`.'
                if options else cmd + ' approve|reject' + config + '`.')  # with options, approve needs a choice
        head = '[applet escalation ' + rid + '] needs the human owner: '
        tabs.notice(f, head + body.splitlines()[0] + ' Answer with ' + hint, name)
        keep_notice(f, name, {'id': rid, 'at': clock(), 'job': job, 'class': cls,
                              'text': head + body + '\nAnswer with ' + hint})  # the card has one line only
        return rid
    if cls == 'access' and not root_host:
        _tell(f, rid, '[applet escalation] ' + rid + ' (access) from ' + body + '\nYou cannot approve this '
                'yourself. Forward it to the top supervisor with report_up (paths as above). When its ruling comes '
                'back, relay it with "applet": [{"id": "' + rid + '", "decision": "approve" | "revise" | "reject", '
                '"by": "root", "choice": <index or null>, "note": "..."}].')
        return rid
    _tell(f, rid, '[applet escalation] ' + rid + ' from ' + body + '\nRule in your service decision JSON '
            '(status RUNNING, dispatch [], skip []) with "applet": [{"id": "' + rid + '", "decision": "approve" | '
            '"revise" | "reject", "choice": <option index when approving>, "note": "..."}].')
    return rid


def notify(f, rid, spec):
    name, _ = gate_applet(f, spec, host=False)
    text = _text(spec, 'text', 1000)
    now, window = clock(), settings(f.cfg)['notify_window_s']
    k = KEY + name + ':notify'
    cap = int(specs(f)[name].get('notify', {}).get('max_per_hour', 6))
    if sum(float(x) > now - window for x in f.r.lrange(k, 0, -1)) >= cap:
        raise Refused('rate_limited', str(cap) + ' per hour')
    f.r.rpush(k, now)
    f.r.ltrim(k, -max(cap, 1), -1)
    _tell(f, rid, '[applet ' + name + '] ' + text.replace('\n', ' ')
            + ('\n(job ' + spec['job'] + ')' if isinstance(spec.get('job'), str) and spec['job'] else '')
            + '\nInformation only; no answer is expected.')
    job = spec['job'] if isinstance(spec.get('job'), str) else ''
    keep_notice(f, name, {'id': rid, 'at': now, 'job': job, 'text': text})
    _audit(f, 'applet.notified', applet=name, id=rid, text=text[:300])  # the full text: ``fleetmux applet notices``
    f.publish(rid, 'ok')
    return rid


def audit_op(f, rid, spec):
    name, _ = gate_applet(f, spec, host=False)
    ev = spec.get('event')
    if not isinstance(ev, str) or not re.fullmatch(EVENT_RE, ev):
        raise Refused('invalid', 'event must match ' + EVENT_RE)
    detail = _json_size(spec.get('detail') or {}, 'detail', 4000)
    _audit(f, 'applet.event', applet=name, event=ev, detail=detail)
    f.publish(rid, 'ok')
    return rid


# ------------------------------------------------------------------ messages to a leased slot
def screen(f, spec):
    """A service request with ``to`` and / or ``lease``. Returns the id when the program refused it (the result is
    published), None when it goes on through the usual request path (and is then a message of that lease)."""
    to, lid = spec.get('to'), spec.get('lease')
    if lid is None and not to:
        return None
    slot = f.resolve(to) if isinstance(to, str) else ''
    holder = taken(f, slot)
    rid = spec.get('id')
    try:
        if lid is None:
            if holder and not (holder == rid == reviewing(f, slot)):  # the host's own review request passes
                raise Refused('slot_leased', f.label(slot) + ' is ' + ('reviewing' if reviewing(f, slot)
                                                                       else 'leased') + ' (' + holder + ')')
            return None
        name = spec.get('applet')
        h = f.r.hgetall(hkey(name)) if isinstance(name, str) and name in specs(f) else {}
        if not token_ok(h, spec.get('token')):
            raise Refused('bad_token')
        rec = record(f.r, name, lid) if isinstance(lid, str) else {}
        if rec.get('state') not in ('granted', 'active', 'renew_requested'):
            raise Refused('lease_not_active', str(lid) + ' is ' + (rec.get('state') or 'unknown'))
        if slot != rec.get('slot'):
            raise Refused('wrong_slot', 'lease ' + lid + ' is on ' + rec.get('slot', ''))
        if f.paused():
            raise Refused('host_paused')
        if spec.get('fix') is not None:
            from fleetmux import applet_ops
            fix = applet_ops.check_fix(f, name, rec, spec['fix'])
    except Refused as exc:
        _check_id(f, rid)
        refuse(f, rid, exc)
        return rid
    # A resent id is a duplicate before it touches the lease record (ValueError, as request() answers one). A fix
    # reply under review has neither a request record nor a result yet: the lease record still knows its id.
    _check_id(f, rid)
    if rid in _loads(rec.get('inflight'), []) or rid in _loads(rec.get('fixes'), {}):
        raise ValueError('duplicate: request id ' + rid + ' was used; its first result stays at fx:result:' + rid)
    spec.pop('token', None)  # the request record keeps no secret
    sent = _loads(rec.get('inflight'), []) + [rid]
    fields = {'messages': int(rec.get('messages') or 0) + 1, 'last_message': rid, 'inflight': _j(sent)}
    if spec.get('fix') is not None:
        fields['fixes'] = _j(dict(_loads(rec.get('fixes'), {}), **{rid: {'fix': fix, 'review': ''}}))
    f.r.hset(lease_key(name, lid), mapping=fields)
    _audit(f, 'applet.message.sent', applet=name, lease=lid, id=rid, status='')
    return None


def frame(f, spec, slot):
    """The program's lease line for a message delivery (behind the transport's [Request <id>] header)."""
    lid = spec.get('lease')
    if not lid or leased(f, slot) != lid:
        return ''
    name = f.r.hget('fx:agent:' + slot, 'applet_lease_holder') or ''
    rec = record(f.r, name, lid)
    until = float(rec.get('expires_at') or 0)
    return ('[Applet lease ' + lid + ' · ' + name + ' · message ' + str(rec.get('messages') or 1) + ' · term ends '
            + (time.strftime('%H:%M', time.localtime(until)) if until else 'when active') + ']\n')


def withhold(f, rid, status, fields):
    """RuntimeV2.publish: a leased worker's reply to a ``fix`` message waits for its review (applets.md 6)."""
    from fleetmux import applet_ops
    return applet_ops.withhold(f, rid, status, fields)


def take_blocked(f, slot, rid, result):
    """A leased worker's ``blocked`` reply or split proposal goes back to the applet, not to the supervisor."""
    lid = leased(f, slot)
    if not lid:
        return False
    name = f.r.hget('fx:agent:' + slot, 'applet_lease_holder') or ''
    if rid not in _loads(record(f.r, name, lid).get('inflight'), []):
        return False
    f.publish(rid, 'blocked', worker=f.label(slot), result=result, lease=lid)
    _audit(f, 'applet.message.blocked', applet=name, lease=lid, id=rid)
    return True


# ------------------------------------------------------------------ the supervisor's decision key
def validate_decision(items):
    if not isinstance(items, list) or not all(
            isinstance(x, dict) and isinstance(x.get('id'), str)
            and x.get('decision') in ('grant', 'deny', 'approve', 'revise', 'reject', 'revoke', 'pause', 'resume')
            for x in items):
        raise ValueError('applet must be a list of {"id", "decision", ...} (decision: grant / deny / approve / '
                         'revise / reject / revoke / pause / resume)')
    for x in items:
        if 'note' in x and not isinstance(x['note'], str):
            raise ValueError('applet: note must be text')
        if x['decision'] in ('deny', 'revise', 'reject', 'revoke') and not (x.get('note') or '').strip():
            raise ValueError('applet: ' + x['decision'] + ' for ' + x['id'] + ' needs a note')


def check_decision(f, decision):
    """The program's re-check of a decision (ValueError = the usual correction path): no dispatch or fresh to a
    leased slot, and every ``applet`` item answers something open within its bounds."""
    for item in decision.get('dispatch') or []:
        slot = f.resolve(item.get('to') or '')
        if reviewing(f, slot):
            raise ValueError('dispatch to ' + f.label(slot) + ' refused: it is reviewing a fix for an applet; '
                             'pick another worker')
        if leased(f, slot):
            raise ValueError('dispatch to ' + f.label(slot) + ' refused: it is leased by '
                             + (f.agent(slot).get('applet_lease_holder') or 'an applet') + ' (' + leased(f, slot)
                             + '); pick another worker')
    for item in decision.get('applet') or []:
        _check_item(f, item)


def _check_item(f, item):
    d, oid = item['decision'], item['id']
    if d in ('pause', 'resume'):
        if oid not in specs(f):
            raise ValueError('applet ' + d + ': no applet named ' + oid)
        return
    if d == 'revoke':
        if not find(f.r, oid, specs(f))[1].get('state') in OPEN:
            raise ValueError('applet revoke: ' + oid + ' is not an open lease')
        return
    op = f.r.hgetall(OP + oid)
    if not op:
        return  # answered already or unknown: logged and skipped in decide
    if op['op'] in ('request', 'renew') and d not in ('grant', 'deny'):
        raise ValueError('applet ' + oid + ': a lease ' + op['op'] + ' is answered grant or deny')
    if op['op'] == 'escalate':
        _check_ruling(f, op, oid, item)
        return
    if op['op'] == 'request' and d == 'grant':
        name, rec = op['applet'], record(f.r, op['applet'], op['lease'])
        entry = specs(f).get(name)
        slot = f.resolve(item.get('slot') or '')
        how, found = supply(f, entry) if entry else ('limit', ['the applet is not declared'])
        if not item.get('slot'):
            if how != 'add':
                raise ValueError('applet ' + oid + ': name a slot: ' + (', '.join(found) if how != 'limit'
                                                                        else found[0]))
        elif slot not in eligible(f, entry):
            raise ValueError('applet ' + oid + ': slot must be one of ' + ', '.join(eligible(f, entry)))
        elif not (idle(f, slot) or asleep(f, slot)):
            raise ValueError('applet ' + oid + ': ' + f.label(slot) + ' is not free; pick an idle or asleep worker'
                             + (' (free now: ' + ', '.join(found) + ')' if how in ('idle', 'asleep') else ''))
        asked = set(_loads(rec.get('paths'), []))
        if not set(_paths(item.get('paths', sorted(asked)))) <= asked:
            raise ValueError('applet ' + oid + ': paths may only narrow what was asked (' + ', '.join(sorted(asked))
                             + ')')
    if 'ttl_s' in item and (isinstance(item['ttl_s'], bool) or not isinstance(item['ttl_s'], (int, float))
                            or item['ttl_s'] < 60):
        raise ValueError('applet ' + oid + ': ttl_s must be a number of seconds >= 60')


def _check_ruling(f, op, oid, item):
    esc = f.r.hgetall(esc_key(op['applet'], oid))
    if item['decision'] not in ('approve', 'revise', 'reject'):
        raise ValueError('applet ' + oid + ': an escalation is answered approve, revise or reject')
    if esc.get('status') == 'awaiting_human':
        raise ValueError('applet ' + oid + ': only the human owner answers this escalation')
    if esc.get('class') == 'access' and f.cfg.get('parent_config') and item.get('by') != 'root':
        raise ValueError('applet ' + oid + ': an access escalation is ruled by the top supervisor; forward it with '
                         'report_up and relay its ruling with "by": "root"')
    options = _loads(esc.get('options'), [])
    choice = item.get('choice')
    if item['decision'] == 'approve' and options and (isinstance(choice, bool) or not isinstance(choice, int)
                                                      or not 0 <= choice < len(options)):
        raise ValueError('applet ' + oid + ': approve needs choice, an index 0 to ' + str(len(options) - 1))


def decide(f, items):
    who = f.label('supervisor')
    for item in items:
        d, oid = item['decision'], item['id']
        if d in ('pause', 'resume'):
            pause(f, oid, d == 'pause', 'supervisor', item.get('note') or '')
            continue
        if d == 'revoke':
            name, _ = find(f.r, oid, specs(f))
            end(f, name, oid, 'revoked', 'supervisor', item.get('note') or '')
            continue
        op = f.r.hgetall(OP + oid)
        if not op:
            late = closed_op(f, oid)
            _audit(f, 'applet.decision_late' if late else 'applet.decision_unknown', id=oid, decision=d, **late)
            continue
        if op.get('status') not in ('awaiting',):
            _audit(f, 'applet.decision_unknown', id=oid, decision=d)
            continue
        if op['op'] == 'request':
            _decide_request(f, oid, op, item, who)
        elif op['op'] == 'renew':
            _decide_renew(f, oid, op, item, who)
        else:
            access = f.r.hget(esc_key(op['applet'], oid), 'class') == 'access'  # a root host's supervisor is the root
            rule(f, op['applet'], oid, d, item.get('choice'), item.get('note') or '',
                 'root' if item.get('by') == 'root' or (access and not f.cfg.get('parent_config')) else 'supervisor')


def closed_op(f, oid):
    """What became of op ``oid`` that is no longer open ({} when it is unknown): a decision on it came too late."""
    for name in sorted(set(specs(f)) | set(all_names(_src(f)))):
        esc = f.r.hgetall(esc_key(name, oid))
        if esc:
            return {'applet': name, 'state': esc.get('status', '')}
        for k in _src(f).scan_iter(lease_key(name, 'L-*')):
            rec = f.r.hgetall(k)
            if rec.get('op') == oid:
                return {'applet': name, 'lease': rec.get('id', ''), 'state': rec.get('state', ''),
                        'end_reason': rec.get('end_reason', '')}
    return {}


def _decide_request(f, oid, op, item, who):
    name, lid = op['applet'], op['lease']
    rec = record(f.r, name, lid)
    _close_op(f, oid)
    if item['decision'] == 'deny':
        _deny(f, name, lid, 'supervisor', item.get('note') or '', 'supervisor')
        f.publish(oid, 'rejected', by='supervisor', reason='supervisor', note=item.get('note') or '')
        return
    lc = specs(f)[name]['lease']
    asked = _loads(rec.get('paths'), [])
    ttl = min(float(item.get('ttl_s') or rec['ttl_s']), float(lc['ttl_max_s']))
    paths = _paths(item.get('paths', asked))
    added = 0
    if item.get('slot'):
        slot = f.resolve(item['slot'])
    else:
        from fleetmux import applet_ops
        slot, added = applet_ops.add_worker(f, specs(f)[name], lid), 1
    info = f.agent(slot)
    fresh = int(info.get('delivered') or 0) > 0 and info.get('state') != 'reclaimed' and not added
    now = clock()
    f.r.hset(lease_key(name, lid), mapping={'state': 'granted', 'slot': slot, 'granted_at': now, 'ttl_s': ttl,
                                           'paths': _j(paths), 'granted_by': who, 'note': item.get('note') or '',
                                           'prep_gen': info.get('generation') or '0', 'prep_fresh': int(fresh),
                                           'label': f.label(slot), 'added': added})
    f.setf(slot, applet_lease=lid, applet_lease_holder=name, applet_lease_until='')
    if fresh:  # a clean session at lease start (design.md section 37), rotated once idle
        f.setf(slot, fresh_pending=1, result_fresh=1)
    write_section(f, slot)
    _audit(f, 'applet.lease.granted', applet=name, lease=lid, job=rec['job'], slot=slot, added=added, ttl_s=ttl,
          by=who, reason='')
    f.publish(oid, 'ok', lease={'id': lid, 'slot': slot, 'label': f.label(slot), 'job': rec['job'],
                                'state': 'granted', 'ttl_s': ttl, 'paths': paths, 'added': bool(added),
                                'note': item.get('note') or ''})


def _deny(f, name, lid, reason, note, by):
    now = clock()
    f.r.hset(lease_key(name, lid), mapping={'state': 'denied', 'end_reason': reason, 'note': note, 'ended_at': now})
    _keep(f, lease_key(name, lid))
    f.r.lrem(open_key(name), 0, lid)
    rec = record(f.r, name, lid)
    _audit(f, 'applet.lease.denied', applet=name, lease=lid, job=rec.get('job', ''), slot='', added=0,
          ttl_s=float(rec.get('ttl_s') or 0), by=by, reason=reason)


def _decide_renew(f, oid, op, item, who):
    name, lid = op['applet'], op['lease']
    rec = record(f.r, name, lid)
    _close_op(f, oid)
    if rec.get('state') != 'renew_requested':
        f.publish(oid, 'rejected', by='program', reason='lease_not_active', note=lid + ' ended')
        return
    if item['decision'] == 'deny':
        renew_denied(f, name, lid, oid, 'supervisor', 'supervisor', item.get('note') or '')
        return
    lc = specs(f)[name]['lease']
    need = float(rec.get('need_s') or lc['ttl_s'])
    ttl = min(float(item.get('ttl_s') or need), need, float(lc['ttl_max_s']))
    expires = clock() + ttl
    renewals = int(rec.get('renewals') or 0) + 1
    f.r.hset(lease_key(name, lid), mapping={'state': 'active', 'expires_at': expires, 'renewals': renewals,
                                           'ttl_s': ttl, 'renew_op': '', 'warned_for': ''})
    f.setf(rec['slot'], applet_lease_until=expires)
    event(f, name, 'lease.renewed', lease=lid, expires_at=expires, renewals=renewals)
    _audit(f, 'applet.lease.renewed', applet=name, lease=lid, need_s=need, ttl_s=ttl, renewals=renewals, by=who)
    f.publish(oid, 'ok', lease={'id': lid, 'expires_at': expires, 'renewals': renewals})


def renew_denied(f, name, lid, oid, by, reason, note, status='rejected'):
    f.r.hset(lease_key(name, lid), mapping={'state': 'active', 'renew_op': ''})
    rec = record(f.r, name, lid)
    event(f, name, 'lease.renew_denied', lease=lid, by=by, reason=reason, note=note)
    _audit(f, 'applet.lease.renew_denied', applet=name, lease=lid, need_s=float(rec.get('need_s') or 0),
          ttl_s=float(rec.get('ttl_s') or 0), renewals=int(rec.get('renewals') or 0), by=by)
    if status == 'timeout':
        f.publish(oid, 'timeout')
    else:
        f.publish(oid, 'rejected', by=by, reason=reason, note=note)


def rule(f, name, oid, decision, choice, note, by):
    """An escalation is ruled (supervisor, root via the host supervisor, or the human owner)."""
    _close_op(f, oid)
    choice = choice if decision == 'approve' and isinstance(choice, int) and not isinstance(choice, bool) else None
    f.r.hset(esc_key(name, oid), mapping={'status': 'ruled', 'decision': decision,
                                         'choice': '' if choice is None else choice, 'by': by, 'note': note})
    _keep(f, esc_key(name, oid))
    event(f, name, 'escalation.ruled', id=oid, decision=decision, choice=choice, by=by, note=note)
    _audit(f, 'applet.ruled', applet=name, id=oid, **{'class': f.r.hget(esc_key(name, oid), 'class') or ''},
          decision=decision, by=by)
    if decision == 'reject':
        f.publish(oid, 'rejected', by=by, reason='supervisor', note=note)
    else:
        f.publish(oid, 'ok', decision=decision, choice=choice, by=by, note=note)


def pause(f, name, paused, by, note):
    f.r.hset(hkey(name), mapping={'paused': '1' if paused else '0'})
    event(f, name, 'control', action='pause' if paused else 'resume', by=by, note=note)
    _audit(f, 'applet.paused' if paused else 'applet.resumed', applet=name, by=by, note=note)


# ------------------------------------------------------------------ granted -> active, rule file, prompts
SECTION_HEAD = '\n## Applet lease '


def section(f, slot):
    lid = leased(f, slot)
    if not lid:
        return ''
    name = f.r.hget('fx:agent:' + slot, 'applet_lease_holder') or ''
    rec = record(f.r, name, lid)
    if rec.get('state') not in ('granted', 'active', 'renew_requested'):
        return ''
    paths = _loads(rec.get('paths'), [])
    return (SECTION_HEAD + lid + ' (written by the fleet program)\n'
            'Until this section disappears you work only for ' + name + '.\n'
            'Purpose: ' + rec.get('purpose', '').replace('\n', ' ') + '\n'
            'You may also write inside: ' + (', '.join(paths) or 'nothing beyond your own directory') + '\n'
            '- Messages that start with "[Applet lease ' + lid + '" are your instructions for this lease.\n'
            '- Do exactly what each message asks, inside the purpose above. Anything outside it: reply with '
            '"blocked".\n'
            '- You do not decide whether the work passed; the applet checks it.\n'
            '- Never delete or move files. If that seems necessary, reply with "blocked" and say what and why.\n')


def rule_files(f, slot):
    return [_fleet().agent_cwd(f.cfg, slot) / n for n in ('AGENTS.md', 'CLAUDE.md')]


def _strip(text):
    i = text.find(SECTION_HEAD)
    if i < 0:
        return text
    j = text.find('\n## ', i + len(SECTION_HEAD))
    return text[:i] + (text[j:] if j >= 0 else '\n')


def write_section(f, slot):
    """Write (or remove, when the slot holds no open lease) the lease section of the worker's rule files."""
    add = section(f, slot)
    for path in rule_files(f, slot):
        try:
            text = path.read_text()
        except OSError:
            continue
        new = _strip(text).rstrip('\n') + '\n' + add if add else _strip(text)
        if new != text:
            path.write_text(new)


def rule_section(f, slot):
    """For role_scope: the section a new generation's rule file carries while the lease is open."""
    return section(f, slot)


def start_prompt(f, slot, prompt):
    """A new generation of a leased slot: its start prompt repeats the Lease block (applets.md section 5)."""
    block = handoff_block(f, slot)
    return prompt + '\n' + block if block else prompt


def handoff_block(f, slot):
    lid = leased(f, slot)
    if not lid:
        return ''
    name = f.r.hget('fx:agent:' + slot, 'applet_lease_holder') or ''
    rec = record(f.r, name, lid)
    until = float(rec.get('expires_at') or 0)
    open_ = inflight(f, rec)
    return ('Lease: you hold applet lease ' + lid + ' for ' + name + ' (purpose: ' + rec.get('purpose', '')[:300]
            + '; paths: ' + (', '.join(_loads(rec.get('paths'), [])) or 'none') + '; term ends '
            + (time.strftime('%H:%M', time.localtime(until)) if until else 'when active') + '; last message '
            + (rec.get('last_message') or 'none') + ' (' + (rec.get('last_status') or 'no result yet') + ')'
            + ('; in flight: ' + ', '.join(open_) if open_ else '') + '). The rule file section "Applet lease '
            + lid + '" still applies.')


# ------------------------------------------------------------------ ending a lease
def end(f, name, lid, state, end_reason, note):
    """Close an open lease: record, slot fields, rule section, running turn, queued messages, event, audit."""
    rec = record(f.r, name, lid)
    if rec.get('state') not in OPEN:
        return False
    if rec['state'] == 'requested':  # denied with its true cause; the request is answered with the stable reason
        if end_reason not in REQUEST_END:
            return False  # no term yet: the wait for the decision is bounded by decision_wait_s alone (op_timeout)
        by, reason = REQUEST_END[end_reason]
        _close_op(f, rec.get('op', ''))
        _deny(f, name, lid, end_reason, note, by)
        f.publish(rec['op'], 'rejected', by=by, reason=reason, note=note)
        return True
    now = clock()
    if rec.get('renew_op'):
        _close_op(f, rec['renew_op'])
        f.publish(rec['renew_op'], 'rejected', by='program', reason='lease_not_active',
                  note='the lease ended: ' + state + (' (' + end_reason + ')' if end_reason else ''))
    slot = rec.get('slot') or ''
    from fleetmux import applet_ops
    reviewed = applet_ops.under_review(f, name, rec)  # a reply already given: its review finishes and publishes
    open_ = [m for m in inflight(f, rec) if m not in reviewed]
    f.r.hset(lease_key(name, lid), mapping={'state': state, 'end_reason': end_reason, 'ended_at': now,
                                           'end_note': note, 'inflight': '[]'})
    _keep(f, lease_key(name, lid))
    f.r.lrem(open_key(name), 0, lid)
    if slot and leased(f, slot) == lid:
        info = f.agent(slot)
        if open_ and info.get('state') == 'busy' and info.get('pane'):
            f.mgr.send_key(info['pane'], 'Escape')  # the fleet's only accepted stop (design.md section 17)
            _audit(f, 'applet.lease.turn_stopped', applet=name, lease=lid, slot=slot)
        f.setf(slot, **{k: '' for k in SLOT_FIELDS})
        write_section(f, slot)
        if int(info.get('delivered') or 0) > 0 and slot in f.workers:
            f.setf(slot, fresh_pending=1, result_fresh=1)  # nothing of the lease carries over (section 4.4)
    for mid in open_:
        f.publish(mid, 'lease_ended', lease=lid, worker=f.label(slot) if slot else '')
    event(f, name, 'lease.ended', lease=lid, state=state, end_reason=end_reason, note=note)
    held = now - float(rec.get('active_at') or rec.get('granted_at') or now)
    _audit(f, 'applet.lease.' + state, applet=name, lease=lid, slot=slot, end_reason=end_reason, held_s=round(held, 1),
           messages=int(rec.get('messages') or 0), last_review=_loads(rec.get('last_review'), None))
    return True


def slot_retired(f, slot):
    """Roster hot reload removes ``slot``: its lease is revoked first (applets.md section 4)."""
    lid = leased(f, slot)
    if lid:
        end(f, f.r.hget('fx:agent:' + slot, 'applet_lease_holder') or '', lid, 'revoked', 'slot_retired',
            'the slot left the roster')


def revoke_all(f, name, end_reason, note):
    for lid in open_ids(f, name):
        end(f, name, lid, 'revoked', end_reason, note)


# ------------------------------------------------------------------ the daemon tick
def tick(f):
    if not getattr(f, 'service', False):
        return
    from fleetmux import applet_ops
    names = set(specs(f)) | set(all_names(f.r))
    now = clock()
    host_flag(f, names)
    for entry in specs(f).values():
        applet_ops.sync(f, entry)
    for name in sorted(names):
        h = f.r.hgetall(hkey(name))
        for lid in open_ids(f, name):
            rec = record(f.r, name, lid)
            if not rec:
                f.r.lrem(open_key(name), 0, lid)
                continue
            if rec.get('boot') != BOOT:
                end(f, name, lid, 'revoked', 'host_down', 'the host daemon restarted')
            elif name not in specs(f) or not up(h) or h.get('started_at') != rec.get('applet_started_at'):
                end(f, name, lid, 'revoked', 'applet_down', 'applet state is ' + (h.get('state') or 'removed'))
            else:
                lease_tick(f, name, lid, rec, now)
    applet_ops.review_tick(f, now)
    for oid in f.r.lrange(OPQ, 0, -1):
        op_timeout(f, oid, now)
    answer_ctl(f)
    housekeeping(f, names)


def housekeeping(f, names):
    """What the transaction overlay cannot write: the open-lease sets, stream caps, keep times."""
    s = settings(f.cfg)
    for name in names:
        want, have = set(open_ids(f, name)), f.r.smembers(leases_key(name))
        if want - have:
            f.r.sadd(leases_key(name), *(want - have))
        if have - want:
            f.r.srem(leases_key(name), *(have - want))
        f.r.xtrim(events_key(name), maxlen=s['events_max'], approximate=True)
    for k in f.r.lrange(EXPIRE, 0, -1):
        f.r.expire(k, int(s['keep_s']))
    f.r.delete(EXPIRE)


def host_flag(f, names):
    paused = '1' if f.paused() else '0'
    if f.r.hget(HOST, 'paused') == paused:
        return
    first = f.r.hget(HOST, 'paused') is None
    f.r.hset(HOST, mapping={'paused': paused})
    if first and paused == '0':
        return
    for name in names:
        event(f, name, 'host.paused' if paused == '1' else 'host.resumed',
              reason=f.r.hget(_fleet().SERVICE, 'reason') or '')


def lease_tick(f, name, lid, rec, now):
    if rec['state'] == 'requested':
        return  # waiting for the supervisor: no slot and no term yet; op_timeout ends it after decision_wait_s
    lc = specs(f)[name]['lease']
    slot = rec.get('slot') or ''
    if slot and slot not in f.workers:
        if rec['state'] == 'granted' and rec.get('added') == '1' \
                and now - float(rec['granted_at']) <= float(lc['prepare_timeout_s']):
            return  # the row was written; the roster reload starts the worker
        end(f, name, lid, 'revoked', 'slot_retired', 'the slot left the roster')
        return
    info = f.agent(slot) if slot else {}
    if slot and info.get('state') == 'held':
        end(f, name, lid, 'revoked', 'slot_held', 'the slot is held')
        return
    record_results(f, name, lid, rec)
    if rec['state'] == 'granted' and info.get('state') == 'reclaimed' and rec.get('woken') != '1':
        f.r.hset(lease_key(name, lid), mapping={'woken': 1})
        f.v2.respawn([slot], True)  # the idle reclaim's own wake (lifecycle_hold.service_tick)
        _audit(f, 'applet.lease.wake', applet=name, lease=lid, slot=slot)
        return
    if rec['state'] == 'granted':
        rotated = rec.get('prep_fresh') != '1' or info.get('generation') != rec.get('prep_gen')
        if rotated and info.get('fresh_pending') != '1' and info.get('state') in ('idle', 'busy'):
            expires = now + float(rec['ttl_s'])
            f.r.hset(lease_key(name, lid), mapping={'state': 'active', 'active_at': now, 'expires_at': expires,
                                                   'gen': info.get('generation') or '0', 'label': f.label(slot)})
            f.setf(slot, applet_lease_until=expires)
            event(f, name, 'lease.active', lease=lid, slot=slot, label=f.label(slot), expires_at=expires)
            _audit(f, 'applet.lease.active', applet=name, lease=lid, job=rec['job'], slot=slot,
                   added=int(rec.get('added') or 0), ttl_s=float(rec['ttl_s']), by='program', reason='')
        elif now - float(rec['granted_at']) > float(lc['prepare_timeout_s']):
            end(f, name, lid, 'revoked', 'prepare_failed', 'the worker was not ready in time')
        return
    if info.get('generation') and rec.get('gen') and info['generation'] != rec['gen']:
        f.r.hset(lease_key(name, lid), mapping={'gen': info['generation'], 'label': f.label(slot)})
        event(f, name, 'worker.rotated', lease=lid, slot=slot, **{'from': rec.get('label') or slot},
              to=f.label(slot), why=info.get('rotate_why') or 'handoff')
    if rec.get('reviewing'):
        return  # a review of this lease's fix runs: no expiry (applets.md section 6)
    expires = float(rec.get('expires_at') or 0)
    grace = float(lc['renew_grace_s']) if rec['state'] == 'renew_requested' else 0
    if now >= expires + grace:
        end(f, name, lid, 'expired', '', 'term over')
        return
    if now >= expires - float(lc['renew_margin_s']) and rec.get('warned_for') != rec.get('expires_at'):
        f.r.hset(lease_key(name, lid), mapping={'warned_for': rec['expires_at']})
        event(f, name, 'lease.expiring', lease=lid, expires_at=expires, in_s=round(expires - now, 1))


def record_results(f, name, lid, rec):
    fleet = _fleet()
    sent = _loads(rec.get('inflight'), [])
    left = []
    for mid in sent:
        raw = f.r.get(fleet.RESULT + mid)
        if not raw:
            left.append(mid)
            continue
        res = _loads(raw, {})
        status = res.get('status', '')
        f.r.hset(lease_key(name, lid), mapping={'last_status': status})
        verdict = (res.get('review') or {}).get('verdict') if isinstance(res.get('review'), dict) else None
        event(f, name, 'message.result', lease=lid, id=mid, status=status,
              **({'review': verdict} if verdict else {}))
        _audit(f, 'applet.message.result', applet=name, lease=lid, id=mid, status=status)
    if left != sent:
        f.r.hset(lease_key(name, lid), mapping={'inflight': _j(left)})


def op_timeout(f, oid, now):
    op = f.r.hgetall(OP + oid)
    if not op:
        f.r.lrem(OPQ, 0, oid)
        return
    if not op.get('wait_s') or now - float(op['created']) < float(op['wait_s']):
        return
    name, lid = op['applet'], op.get('lease', '')
    _close_op(f, oid)
    if op['op'] == 'request':
        _deny(f, name, lid, 'no_decision', 'the supervisor did not answer in time', 'program')
        f.publish(oid, 'timeout', reason='no_decision')
    elif op['op'] == 'renew':
        renew_denied(f, name, lid, oid, 'program', 'no_decision', 'the supervisor did not answer in time',
                     status='timeout')
    else:
        f.r.hset(esc_key(name, oid), mapping={'status': 'timeout'})
        _keep(f, esc_key(name, oid))
        f.publish(oid, 'timeout', reason='no_decision')


# ------------------------------------------------------------------ operator requests (CLI -> daemon)
def answer_ctl(f):
    for rid, raw in f.r.hgetall(CTL).items():
        f.r.hdel(CTL, rid)
        req = _loads(raw, {})
        try:
            out = operator(f, req)
        except Refused as exc:
            out = {'status': 'rejected', 'reason': exc.reason, 'note': exc.note}
        f.r.set(CTL + ':' + rid, _j(out), ex=3600)


def operator(f, req):
    action = req.get('action')
    if action in ('pause', 'resume'):
        if req.get('name') not in specs(f):
            raise Refused('invalid', 'no applet named ' + str(req.get('name')) + ' is declared')
        pause(f, req['name'], action == 'pause', 'operator', req.get('note') or '')
        return {'status': 'ok', 'applet': req['name'], 'paused': action == 'pause'}
    if action == 'set':
        from fleetmux import applet_ops
        if req.get('name') not in specs(f) or req.get('key') != 'hot_add_max':
            raise Refused('invalid', 'set takes a declared applet and the key hot_add_max')
        applet_ops.set_hot_add_max(f, req['name'], req.get('value'))
        return {'status': 'ok', 'applet': req['name'], 'hot_add_max': req['value']}
    if action == 'revoke':
        name, rec = find(f.r, req.get('lease') or '', set(specs(f)) | set(all_names(f.r)))
        if rec.get('state') not in OPEN:
            raise Refused('lease_not_active', str(req.get('lease')) + ' is ' + (rec.get('state') or 'unknown'))
        end(f, name, req['lease'], 'revoked', 'operator', req.get('note') or '')
        return {'status': 'ok', 'lease': req['lease'], 'state': 'revoked'}
    if action == 'answer':
        oid = req.get('id') or ''
        op = f.r.hgetall(OP + oid)
        esc = f.r.hgetall(esc_key(op.get('applet', ''), oid)) if op else {}
        if esc.get('status') != 'awaiting_human':
            raise Refused('invalid', oid + ' is not an escalation waiting for the human owner')
        options = _loads(esc.get('options'), [])
        choice = req.get('choice')
        if req.get('decision') == 'approve' and options and not (isinstance(choice, int) and 0 <= choice < len(options)):
            raise Refused('invalid', 'approve needs --choice 0 to ' + str(len(options) - 1))
        rule(f, op['applet'], oid, req['decision'], choice, req.get('note') or '', 'human')
        return {'status': 'ok', 'id': oid, 'decision': req['decision']}
    raise Refused('invalid', 'unknown action ' + str(action))


def ask(r, req, wait=None, sleep=time.sleep):
    """CLI side: hand one operator request to the host daemon and wait for its answer."""
    rid = secrets.token_hex(6)
    r.hset(CTL, rid, _j(dict(req, at=time.time())))
    deadline = awake.mono() + (SETTINGS['ctl_wait_s'] if wait is None else wait)
    while awake.mono() < deadline:
        raw = r.get(CTL + ':' + rid)
        if raw:
            return json.loads(raw)
        sleep(.1)
    return {'status': 'pending', 'id': rid, 'note': 'the host daemon has not answered yet'}


# ------------------------------------------------------------------ status
def slot_row(r, slot):
    h = r.hmget('fx:agent:' + slot, *SLOT_FIELDS)
    if not h[0]:
        return None
    return {'id': h[0], 'holder': h[1] or '', 'until': float(h[2]) if h[2] else None}


def skip_fresh(f, slot):
    """A leased slot keeps one session for the whole lease: no clean session after each result (section 4)."""
    return bool(taken(f, slot))


def annotate_cfg(cfg, out):
    fleet = _fleet()
    r = fleet.connect(cfg)
    try:
        annotate(r, out)
    finally:
        r.close()


def annotate(r, out):
    """``fleetmux status``: each slot row gains ``applet_lease`` while it is leased."""
    for slot, row in (out.get('slots') or {}).items():
        if not isinstance(row, dict):
            continue
        lease = slot_row(r, slot)
        if lease:
            row['applet_lease'] = lease
        review, added_for = r.hmget('fx:agent:' + slot, 'applet_review', 'applet_added_for')
        if review:
            row['applet_review'] = review
        if added_for:
            row['applet_added_for'] = added_for


def lease_rows(r, all_=False):
    rows = []
    for name in all_names(r):
        ids = sorted(r.smembers(leases_key(name))) if not all_ else sorted(
            k.rsplit(':', 1)[1] for k in r.scan_iter(lease_key(name, 'L-*')))
        for lid in ids:
            rec = record(r, name, lid)
            if rec:
                rows.append({k: v for k, v in rec.items() if k not in ('boot', 'inflight', 'prep_gen', 'prep_fresh')})
    return rows
