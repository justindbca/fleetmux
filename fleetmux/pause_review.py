"""Pause review (docs/design.md section 24 "Pause review").

A run paused for ``pause_review_after_s`` awake seconds is checked by its own daemon, then every
``pause_review_every_s``. The pause cause comes from ``fx:service.cause`` (JSON {kind, slot?, message_id?,
generation?, subfleet_run?, brake_kind?}, written by ``Fleet.pause(reason, kind=..., **ref)``); without it, from the
brake record or the slot pause marks (``hold_pause`` / ``prompt_pause``). The reason text only names a kind for
the record and is never acted on (a valid service decision rewrites ``fx:service.reason``). For each cause:

- never lifted: operator, supervisor_blocked (a general BLOCKED), budget, integrity, opencode, security, unknown;
  a delete dialog; anything while a brake or budget line is set (wall excepted, below);
- lifted when the cause no longer holds: slot_hold (slot released, new generation, or gone from the roster), dialog
  (dialog gone), restart_recovery (no slot held, no uncertain lease), startup (slot READY), adopted (supervisor no
  longer held), subfleet (sub-fleet gone, not running, or not paused with nothing held), supervisor_fault of an
  earlier supervisor generation;
- wall: lifted when the wall record's reset time passed, else after ``pause_review_wall_retry_s``; at most
  ``WALL_TRIES`` lifts for one wall record (same line on the same slot), then kept;
- a supervisor fault that still holds (supervisor_fault, a held supervisor's slot_hold): handed to supervisor
  regeneration (section 24): the supervisor is held without a pause, this pause becomes its ``hold_pause`` and
  ``regen_due`` is set; the held watch then lifts it through ``hold_pause_lift`` and rotates. Guards, budget and probe
  of the regeneration apply; when they refuse, the pause is kept.

Runs from ``RuntimeV2.tick`` after the held watch, outside any intent (``job_tick`` returns while the run is paused,
so it cannot host this). The verdict goes to ``fx:service.review`` and to the audit stream (``pause_review.*``).
"""
import hashlib
import json
import re
import time
import uuid
from pathlib import Path

from fleetmux import awake, fleet_config

REVIEW_WATCH = {'pause_review': True, 'pause_review_after_s': 1800, 'pause_review_every_s': 300,
                'pause_review_wall_retry_s': 3600}
KEYS = tuple(REVIEW_WATCH)
WALL_TRIES = 3
KINDS = ('operator', 'supervisor_fault', 'supervisor_blocked', 'subfleet', 'budget', 'wall', 'integrity',
         'opencode', 'slot_hold', 'dialog', 'restart_recovery', 'startup', 'adopted', 'security')
NEVER = ('operator', 'supervisor_blocked', 'budget', 'integrity', 'opencode', 'security', 'unknown')
# The kinds the review may lift: the single table, also read by Fleet.pause (ruling 29: on a run that is already
# paused, a pause of one of these kinds keeps the cause there, even an empty one).
LIFTABLE = tuple(k for k in KINDS if k not in NEVER)
DELETE_WORDS = re.compile(r'delet|删除|\brm\b', re.I)
# Fallback for a pause written without a cause (older runs, direct writers): (pattern, kind, slot group or fixed slot)
TEXT = (
    (re.compile(r'^paused by operator'), 'operator', None),
    (re.compile(r'^brake: budget line reached'), 'budget', None),
    (re.compile(r'^brake: (\S+) hit an? .*wall'), 'wall', 1),
    (re.compile(r'^budget_exhausted:'), 'budget', None),
    (re.compile(r'^(ambiguous effect intent|prepared settlement invalid|ambiguous intent requires)'), 'integrity', None),
    (re.compile(r'^same-run daemon restart recovered held'), 'restart_recovery', None),
    (re.compile(r'^(canary tripped|security limit)'), 'security', None),
    (re.compile(r'^supervisor gave no valid decision JSON'), 'supervisor_fault', 'supervisor'),
    (re.compile(r'^held adopted supervisor not released'), 'adopted', 'supervisor'),
    (re.compile(r'^blocked: (\S+) startup timed out before READY'), 'startup', 1),
    (re.compile(r'^\S+ opencode \w+:'), 'opencode', None),
    (re.compile(r'^(\S+): (dialog |unknown confirmation dialog)'), 'dialog', 1),
    (re.compile(r'^(\S+): '), 'slot_hold', 1),
)


def knobs(block=None):
    """(knobs, problem): the pause review keys of fleet.yaml#daemon_watch over ``REVIEW_WATCH``, each validated on its
    own; a bad key keeps its default and is named in the problem text."""
    if block is None:
        try:
            block = fleet_config.block('daemon_watch') or {}
        except (OSError, ValueError, KeyError) as exc:
            return dict(REVIEW_WATCH), 'fleet.yaml#daemon_watch unreadable: ' + str(exc)[:200]
    number = lambda v: type(v) in (int, float) and v > 0
    checks = {'pause_review': lambda v: type(v) is bool, 'pause_review_after_s': number,
              'pause_review_every_s': number, 'pause_review_wall_retry_s': number}
    out, bad = dict(REVIEW_WATCH), []
    for key, ok in checks.items():
        if key in block:
            if ok(block[key]):
                out[key] = block[key]
            else:
                bad.append(key)
    return out, ('fleet.yaml#daemon_watch: ' + ', '.join(bad) + ' invalid; their defaults apply') if bad else None


def _json(raw, default=None):
    try:
        out = json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default
    return out


def _slot(f, name):
    """A slot name from a slot or a label ('' when none matches)."""
    if not name:
        return ''
    if name in f.names:
        return name
    try:
        slot = f.resolve(name)
    except Exception:  # noqa: BLE001  (an unknown label is no slot)
        return ''
    return slot if slot in f.names else ''


def classify(f):
    """{kind, ref, source, reason, at} of the current pause (source: cause / brake / mark / text / none)."""
    from fleetmux.fleet import BRAKE, SERVICE
    service = f.r.hgetall(SERVICE)
    reason, at = service.get('reason') or '', service.get('at') or ''
    base = {'reason': reason, 'at': at}
    cause = _json(service.get('cause'), {})
    if isinstance(cause, dict) and cause.get('kind') in KINDS:
        return dict(base, kind=cause['kind'], ref={k: v for k, v in cause.items() if k != 'kind'}, source='cause')
    owner = _json(f.r.hget(BRAKE, 'paused_by'), {}) or {}
    if isinstance(owner, dict) and owner.get('reason') and owner.get('reason') == reason:
        kind = 'wall' if owner.get('kind') == 'wall' else 'budget'
        return dict(base, kind=kind, ref={'slot': owner.get('agent') or '', 'brake_kind': owner.get('kind')},
                    source='brake')
    for slot in f.names:
        info = f.agent(slot)
        for field, kind in (('hold_pause', 'slot_hold'), ('prompt_pause', 'dialog')):
            mark = _json(info.get(field), {})
            if isinstance(mark, dict) and mark.get('at') and str(mark['at']) == str(at):
                return dict(base, kind=kind, ref={'slot': slot, 'generation': mark.get('generation')}, source='mark')
    for pattern, kind, slot in TEXT:
        m = pattern.match(reason)
        if not m:
            continue
        name = slot if isinstance(slot, str) else (_slot(f, m.group(slot)) if slot else '')
        if kind == 'slot_hold' and not name:
            continue
        return dict(base, kind=kind, ref={'slot': name} if name else {}, source='text')
    return dict(base, kind='unknown', ref={}, source='none')


def brake_set(f):
    """What a brake or budget line says now ('' when nothing)."""
    from fleetmux import brake
    from fleetmux.fleet import BRAKE
    state = f.r.hgetall(BRAKE)
    if state.get('tripped') or state.get('paused_by'):
        return 'a brake is set (' + str(_json(state.get('paused_by') or state.get('tripped'), {}).get('kind', '')) + ')'
    return 'budget over the line' if budget_over(f) else ''


def budget_over(f):
    from fleetmux import brake
    from fleetmux.fleet import BRAKE
    budget = f.brake_budget()
    return bool(budget and brake.trips(f.brake_card(budget), brake.counters(f.r.hgetall(BRAKE), f.quota_reading())))


def _verdict(action, check, slot='', **evidence):
    return {'action': action, 'check': check, 'slot': slot, 'evidence': evidence}


BLOCKABLE = ('slot_hold', 'dialog', 'restart_recovery')


def blockers(f, slot):
    """Ruling 30: other slots whose own pause reason still holds. A dialog that waits for the user, or a hold that
    pauses the fleet on its own (``hold_pauses`` is not '0'; RuntimeV2.hold writes it). A slot-only hold (section 43
    expiry, a held requeue wait, a link loss) does not block. The advisor never pauses the fleet."""
    from fleetmux import advisor
    out = []
    for other in f.names:
        if other == slot or other == advisor.SLOT:
            continue
        info = f.agent(other)
        if info.get('prompt_wait') and info.get('prompt_route') == 'user':
            out.append(other + ' (a dialog waits for the user)')
        elif info.get('state') == 'held' and info.get('hold_pauses') != '0':
            out.append(other + ' (held; that hold pauses the fleet on its own)')
    return out


def still_holds(rt, cls, k, paused_s):
    """The verdict for one classified pause: action 'lift' | 'keep' | 'regen', with what was checked and the facts.
    A slot_hold / dialog / restart_recovery lift is kept while another slot's own pause reason holds (ruling 30)."""
    verdict = _still_holds(rt, cls, k, paused_s)
    if verdict['action'] == 'lift' and cls['kind'] in BLOCKABLE:
        blocked = blockers(rt.f, verdict['slot'])
        if blocked:
            return _verdict('keep', verdict['check'] + '; but another slot still holds the pause: ' + ', '.join(blocked),
                            verdict['slot'], blocked_by=blocked, **verdict['evidence'])
    return verdict


def _still_holds(rt, cls, k, paused_s):
    f, kind, ref = rt.f, cls['kind'], cls.get('ref') or {}
    slot = _slot(f, ref.get('slot')) or ''
    if kind in NEVER:
        return _verdict('keep', 'never lifted automatically (' + kind + ')', slot)
    if cls.get('source') == 'text':
        # a valid service decision rewrites fx:service.reason, so the text only names the kind; it is never acted on
        return _verdict('keep', 'no structured cause (classified from the reason text only); never acted on', slot)
    if kind == 'wall':
        return wall(rt, cls, slot, k, paused_s)
    held_brake = brake_set(f)
    if held_brake:
        return _verdict('keep', held_brake + '; resume stays with the operator', slot)
    if kind == 'subfleet':
        return subfleet_check(ref)
    if kind == 'restart_recovery':
        held = [s for s in f.names if f.agent(s).get('state') == 'held']
        uncertain = [s for s in f.names if rt.bus.slot_state(s).get('status') == 'uncertain']
        if held or uncertain:
            return _verdict('keep', 'slots still held or uncertain', held=held, uncertain=uncertain)
        return _verdict('lift', 'no slot held, no uncertain lease since the restart')
    if not slot:
        return _verdict('keep', 'the pause names no slot of this fleet', name=ref.get('slot') or '')
    info = f.agent(slot)
    gen = str(info.get('generation') or 0)
    facts = {'state': info.get('state', ''), 'generation': gen}
    if kind == 'dialog':
        if DELETE_WORDS.search(cls.get('reason') or '') or info.get('prompt_class') == 'delete':
            return _verdict('keep', 'a delete dialog pause; deleting is the user\'s alone', slot, **facts)
        if info.get('prompt_wait') and info.get('prompt_route') == 'user':
            return _verdict('keep', 'the dialog still waits for the user', slot, **facts)
        return _verdict('lift', 'the dialog is gone', slot, **facts)  # other slots: blockers (ruling 30)
    if kind == 'startup':
        if info.get('state') in ('idle', 'busy') and info.get('runtime_ready') == '1':
            return _verdict('lift', 'the slot said READY since', slot, **facts)
        return _verdict('keep', 'the slot is still not READY', slot, **facts)
    if kind == 'adopted':
        if info.get('state') != 'held':
            return _verdict('lift', 'the adopted supervisor is no longer held', slot, **facts)
        return _verdict('keep', 'the adopted supervisor is still held (adopted_held_tick owns it)', slot, **facts)
    if ref.get('generation') is not None and str(ref['generation']) != gen:
        return _verdict('lift', 'the slot runs a new generation since the pause', slot,
                        paused_generation=str(ref['generation']), **facts)
    if kind == 'slot_hold' and info.get('state') != 'held':
        return _verdict('lift', 'the slot is no longer held', slot, **facts)
    if slot == 'supervisor' and kind in ('slot_hold', 'supervisor_fault'):
        return regen_check(rt, info, facts)
    if info.get('requeue_due'):
        return _verdict('keep', 'the slot is still held; held requeue owns it', slot, **facts)
    return _verdict('keep', 'the slot is still held', slot, **facts)


def subfleet_check(ref):
    from fleetmux import fleet as legacy, subfleet
    run = ref.get('subfleet_run') or ''
    if not run:
        return _verdict('keep', 'the pause names no sub-fleet run')
    d = Path(run)
    if not d.is_dir():
        return _verdict('lift', 'the sub-fleet is gone (no run directory)', subfleet_run=run)
    try:
        rec = subfleet.record(d)
    except (OSError, ValueError):
        rec = {}
    state = (rec or {}).get('state') or ''
    if state not in subfleet.ACTIVE:
        return _verdict('lift', 'the sub-fleet is ' + (state or 'not recorded') + ', not running', subfleet_run=run)
    try:
        cfg = legacy.load(d / 'config.json')
        facts = subfleet.child_progress(cfg, rec)
    except (OSError, ValueError):
        facts = None
    if facts is None:
        return _verdict('keep', 'the sub-fleet\'s Redis cannot be read', subfleet_run=run, state=state)
    seen = {'paused': bool(facts.get('paused')), 'held': list(facts.get('held') or []),
            'supervisor_held': bool(facts.get('supervisor_held'))}
    if seen['paused'] or seen['held'] or seen['supervisor_held']:
        return _verdict('keep', 'the sub-fleet is still paused or holds a slot', subfleet_run=run, **seen)
    return _verdict('lift', 'the sub-fleet recovered (not paused, nothing held)', subfleet_run=run, **seen)


def regen_check(rt, info, facts):
    """A supervisor fault that still holds: hand it to supervisor regeneration when that may run now."""
    rk = rt.held_knobs()
    if not rt.regen_on('supervisor', info, rk):
        return _verdict('keep', 'supervisor regeneration does not apply (off, adopted, or the fleet is stopping)',
                        'supervisor', **facts)
    guard = rt.regen_guard('supervisor', dict(info, state='held'))
    if guard:
        return _verdict('keep', 'supervisor regeneration guarded: ' + guard, 'supervisor', **facts)
    now, window = awake.now(), float(rk['supervisor_regen_window_s'])
    used = [t for t in (_json(info.get('regen_log'), []) or []) if now - float(t) < window]
    if len(used) >= int(rk['supervisor_regen_max']):
        return _verdict('keep', 'supervisor regeneration budget used up (' + str(len(used)) + ' within '
                        + str(int(window)) + ' s)', 'supervisor', **facts)
    return _verdict('regen', 'the supervisor fault still holds; handed to supervisor regeneration', 'supervisor',
                    **facts)


def wall(rt, cls, slot, k, paused_s):
    f = rt.f
    if budget_over(f):
        return _verdict('keep', 'budget over the line; resume stays with the operator', slot)
    info = f.agent(slot) if slot else {}
    rec = _json(info.get('wall'), {}) or {}
    if rec.get('kind') == 'safeguards' and rec.get('action') == 'stop':
        # design section 23: a refusal has no reset time; a lift would hand the message to the flagged model again
        return _verdict('keep', 'safeguards stop: no usable model left on the chain; resume stays with the operator',
                        slot)
    key = hashlib.sha256((slot + '|' + str(rec.get('line') or cls.get('reason') or '')).encode()).hexdigest()[:16]
    seen = _json(info.get('wall_review'), {}) or {}
    tries = int(seen.get('tries') or 0) if seen.get('key') == key else 0
    if tries >= WALL_TRIES:
        return _verdict('keep', 'wall retry tries used (' + str(tries) + ' of ' + str(WALL_TRIES) + ')', slot)
    reset = rec.get('reset_at')
    if isinstance(reset, (int, float)) and not isinstance(reset, bool):
        if awake.now() < awake.from_wall(float(reset)):  # a wall-clock instant, compared on the awake clock
            return _verdict('keep', 'the wall resets at ' + time.strftime('%H:%M:%S', time.localtime(reset)), slot)
        why = 'the wall reset time passed'
    elif paused_s < float(k['pause_review_wall_retry_s']):
        return _verdict('keep', 'waiting for the wall retry time (' + str(int(k['pause_review_wall_retry_s']))
                        + ' s; the wall record has no reset time)', slot)
    else:
        why = 'the wall retry time passed (the wall record has no reset time)'
    return _verdict('lift', why + '; try ' + str(tries + 1) + ' of ' + str(WALL_TRIES), slot, wall_key=key,
                    tries=tries + 1)


def tick(rt, k=None, now=None):
    """One review step (daemon tick). Returns the verdict written, or None when nothing was checked."""
    from fleetmux.fleet import SERVICE
    f = rt.f
    if k is None:
        k, _ = knobs()
    if not k.get('pause_review'):
        return None
    service = f.r.hgetall(SERVICE)
    if service.get('paused') != '1' or not service.get('at'):
        return None
    paused_s = awake.since_wall(float(service['at']))
    if paused_s < float(k['pause_review_after_s']):
        return None
    now = awake.now() if now is None else now
    last = _json(service.get('review'), {}) or {}
    if last.get('pause_at') == service['at'] and now - float(last.get('checked') or 0) < float(k['pause_review_every_s']):
        return None
    cls = classify(f)
    verdict = still_holds(rt, cls, k, paused_s)
    rec = dict(verdict, pause_at=service['at'], checked=now, at=time.time(), kind=cls['kind'], source=cls['source'])
    f.r.hset(SERVICE, 'review', json.dumps(rec, ensure_ascii=False))
    fields = dict(paused_at=service['at'], paused_s=int(paused_s), reason=(service.get('reason') or '')[:300],
                  kind=cls['kind'], source=cls['source'], check=verdict['check'], evidence=verdict['evidence'],
                  action=verdict['action'], slot=verdict['slot'])
    if verdict['action'] == 'lift':
        lift(rt, cls, verdict, service, fields)
    elif verdict['action'] == 'regen':
        hand_to_regen(rt, service, fields)
    elif not (last.get('pause_at') == service['at'] and last.get('check') == verdict['check']):
        rt.audit('pause_review.unknown' if cls['kind'] == 'unknown' else 'pause_review.kept', level='warn', **fields)
    return rec


def lift(rt, cls, verdict, service, fields):
    """Resume the run (never past a brake or budget line; a wall clears its own brake record first)."""
    from fleetmux.fleet import BRAKE
    f = rt.f
    slot = verdict['slot'] if verdict['slot'] in f.names else 'supervisor'
    if cls['kind'] == 'wall':
        ev = verdict['evidence']
        if slot in f.names and ev.get('wall_key'):
            f.setf(slot, wall_review=json.dumps({'key': ev['wall_key'], 'tries': ev.get('tries', 1)}))
        f.r.hset(BRAKE, mapping={'tripped': '', 'paused_by': ''})
        f.resume()
    else:
        text = ('暂停已超过时限，daemon 复查：' + verdict['check'] + '，暂停原因已不成立，已自动解除（原因：'
                + (service.get('reason') or '')[:120] + '）。')
        rt.transaction('pause-review-' + uuid.uuid4().hex, lambda: rt.pause_owner_resume(
            slot, '', {'source': 'pause_review', 'kind': cls['kind'], 'paused_at': service['at'],
                       'check': verdict['check']}, text, 'review:' + str(service['at'])))
    if f.paused():
        rt.audit('pause_review.kept', level='warn', **dict(fields, action='keep',
                                                          check=verdict['check'] + '; resume refused (brake / budget)'))
        return False
    rt.audit('pause_review.lifted', level='warn', **fields)
    return True


def hand_to_regen(rt, service, fields):
    """Make this pause the held supervisor's own (hold_pause) and set regen_due: the held watch's
    supervisor_regen lifts it through hold_pause_lift (brake checked) and rotates."""
    f = rt.f
    info = f.agent('supervisor')
    if info.get('state') != 'held':
        rt.transaction('pause-review-hold-' + uuid.uuid4().hex, lambda: rt.hold(
            'supervisor', 'pause review: ' + (service.get('reason') or 'supervisor fault')[:200], pause=False))
        info = f.agent('supervisor')
    lease = rt.bus.slot_state('supervisor')
    f.setf('supervisor', hold_pause=json.dumps({'at': service['at'], 'message_id': lease.get('message_id') or '',
                                                'generation': int(info.get('generation') or 0)}),
           regen_due=awake.now())
    rt.audit('pause_review.handed_to_regen', level='warn', **fields)
