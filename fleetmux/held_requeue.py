"""Held requeue (docs/design.md section 24 "Held requeue of an interrupted request").

A service worker whose turn on a pure request was interrupted (Escape, lease expiry, failed lease renewal) is held as
"execution stop unconfirmed". When the request may be re-run (``eligible``), only that slot is held: no public
result, no fleet pause. After ``held_requeue_after_s`` the daemon ends the old session (``Watchdog.rotate`` with
reason ``requeue``: kill and verify, ``confirm_retired``), starts the next generation and routes the request again
through the existing rules (``RuntimeV2.redeliverable`` / ``replay_request``: ``replay`` + 1 within
``max_attempts``). Giving up is today's behaviour: the request is published failed, the run is paused and the held
watch reports ``not_recovered``.

Functions here take the RuntimeV2 (``rt``) or the Fleet (``f``); they are called from runtime_v2 (settle, expire,
renew_tui, held_watch_tick) and never start a Redis intent of their own except through ``rt.transaction``.
"""
import json
import uuid

from fleetmux import advisor, awake, fleet_config

REQUEUE_WATCH = {'held_requeue': True, 'held_requeue_causes': ('aborted', 'lease_expired', 'renew_failed'),
                 'held_requeue_after_s': 30, 'held_requeue_max': 3, 'held_requeue_window_s': 3600}
# Every hold cause the daemon can name. Only those listed in held_requeue_causes are collected.
CAUSES = ('aborted', 'failed', 'unfinished', 'unconfirmed', 'lease_expired', 'renew_failed')
KEYS = tuple(REQUEUE_WATCH)


def knobs(block=None):
    """(knobs, problem): the held requeue keys of fleet.yaml#daemon_watch over ``REQUEUE_WATCH``, each validated on
    its own; a bad key keeps its default and is named in the problem text."""
    if block is None:
        try:
            block = fleet_config.block('daemon_watch') or {}
        except (OSError, ValueError, KeyError) as exc:
            return dict(REQUEUE_WATCH), 'fleet.yaml#daemon_watch unreadable: ' + str(exc)[:200]
    number = lambda v: type(v) in (int, float) and v > 0
    checks = {'held_requeue': lambda v: type(v) is bool,
              'held_requeue_causes': lambda v: isinstance(v, (list, tuple)) and all(x in CAUSES for x in v),
              'held_requeue_after_s': lambda v: type(v) in (int, float) and v >= 0,
              'held_requeue_max': lambda v: type(v) is int and v >= 1,
              'held_requeue_window_s': number}
    out, bad = dict(REQUEUE_WATCH), []
    for key, ok in checks.items():
        if key in block:
            if ok(block[key]):
                out[key] = block[key]
            else:
                bad.append(key)
    out['held_requeue_causes'] = tuple(out['held_requeue_causes'])
    return out, ('fleet.yaml#daemon_watch: ' + ', '.join(bad) + ' invalid; their defaults apply') if bad else None


def settle_cause(ev):
    """The cause name of a settlement that holds the slot (runtime_v2.settle: aborted / failed / unfinished)."""
    if ev.get('unfinished'):
        return 'unfinished'
    if ev.get('aborted') is True:
        return 'aborted'
    if ev.get('failed') is True:
        return 'failed'
    return 'unconfirmed'


def eligible(f, slot, msg, cause, knobs, ev=None):
    """(True, '') when the request interrupted on ``slot`` may be re-run after a kill-and-verify, else (False, why).

    All of: the switch on; a service fleet; a worker slot (not the supervisor, not the advisor); a cause listed in
    held_requeue_causes; no tool still running at the end of the turn (pi ``unfinished``); a worker message bound to
    a request that came in through ``request`` and is dispatched to this slot (not mid-turn, not a handoff); the
    request does not say ``replay: false``, is not an operator request, its attempts are not used up
    (``max_attempts``); the slot holds no applet lease. Claude / Codex / opencode report no per-turn tool record, so
    for them only these declarations count."""
    from fleetmux.fleet import REQ
    if not knobs.get('held_requeue'):
        return False, 'held_requeue is off'
    if not f.service:
        return False, 'task mode: a dispatch is never re-run by the daemon'
    if slot not in f.workers or slot == advisor.SLOT:
        return False, 'not a worker slot'
    if cause not in tuple(knobs.get('held_requeue_causes') or ()):
        return False, 'cause ' + str(cause) + ' not in held_requeue_causes'
    if ev is not None and ev.get('unfinished'):
        return False, 'a tool was still running when the turn ended (unfinished)'
    msg = msg or {}
    rid = msg.get('request_id')
    if not msg.get('message_id') or msg.get('kind') != 'worker' or not rid:
        return False, 'not a worker message bound to a request'
    if msg.get('midturn_id'):
        return False, 'a mid-turn split message'
    body = msg.get('body') if isinstance(msg.get('body'), dict) else {}
    if body.get('purpose') == 'handoff':
        return False, 'a handoff delivery'
    req = f.r.hgetall(REQ + rid)
    if not req or req.get('status') != 'dispatched' or req.get('worker') != slot:
        return False, 'request ' + rid + ' is not dispatched to this slot'
    try:
        spec = json.loads(req.get('spec') or '{}')
    except ValueError:
        spec = {}
    if spec.get('replay') is False:
        return False, 'request has replay: false'
    if req.get('operator') == '1':
        return False, 'an operator request (any action may have been taken)'
    if int(req.get('replay') or 1) >= int(f.cfg.get('max_attempts') or 1):
        return False, 'attempts used (max-attempts ' + str(f.cfg.get('max_attempts')) + ')'
    if f.r.hget('fx:agent:' + slot, 'applet_lease'):
        return False, 'the slot holds an applet lease'
    return True, ''


def retire_provable(mgr, pane):
    """Can ``Watchdog.rotate``'s kill-and-verify prove the old CLI stopped? Not for a tmux *view* tab: killing it
    ends only its own client, the session (and the CLI in it) runs on (backends/fleetmux.py and cmux.py
    ``kill_pane``). A tmux owner tab ends its session; any other pane is proven by ``get_pane_status`` after the
    kill, as for every frozen rotation."""
    view = getattr(mgr, 'tmux_view_info', None)
    if view is None or not view(pane):
        return True, ''
    role = (getattr(mgr, '_tmux_of', lambda p: {})(pane) or {}).get('tmux_role')
    if role == 'owner':
        return True, ''
    return False, 'a tmux view tab: killing it does not end the tmux session, so the old CLI may still run'


def mark(f, slot, msg, cause):
    """Record on the slot that its hold waits for a requeue (the wiring calls this right after its hold)."""
    f.setf(slot, requeue_due=json.dumps({'at': awake.now(), 'message_id': (msg or {}).get('message_id') or '',
                                         'request_id': (msg or {}).get('request_id') or '', 'cause': cause}),
           requeue_state='due')


def due(info):
    try:
        out = json.loads(info.get('requeue_due') or 'null')
    except ValueError:
        return None
    return out if isinstance(out, dict) else None


def step(rt, slot, knobs, now=None):
    """One held-watch step for ``slot``. None (no requeue mark), 'released' (a hold exit came first), 'waiting'
    (held_requeue_after_s not over, or the run is paused by someone else: rotate never restarts a paused run; no
    count spent), 'started' (rotated, the request routed again) or 'gave_up' (today's behaviour)."""
    f = rt.f
    info = f.agent(slot)
    m = due(info)
    if not m:
        return None
    if info.get('state') != 'held':
        f.setf(slot, requeue_due='', requeue_state='released')
        rt.audit('held_requeue.released', slot=slot, level='info', message_id=m.get('message_id', ''))
        return 'released'
    lease = rt.bus.slot_state(slot)
    if (lease.get('message_id') or '') != m.get('message_id'):
        f.setf(slot, requeue_due='', requeue_state='')
        return None
    now = awake.now() if now is None else now
    if not knobs.get('held_requeue'):
        return give_up(rt, slot, 'held_requeue was switched off')
    if now - float(m.get('at') or 0) < float(knobs['held_requeue_after_s']):
        return 'waiting'
    if info.get('prompt_wait'):
        return give_up(rt, slot, 'a dialog waits for the user')
    pane = info.get('pane')
    if not pane or f.mgr.get_pane_status(pane) is None:
        return give_up(rt, slot, 'its tab is gone (no proof its session stopped)')
    owns = getattr(f.mgr, 'owns', None)
    if owns is not None and not owns(pane):
        return give_up(rt, slot, 'its pane is not in this fleet\'s registry')
    ok, why = retire_provable(f.mgr, pane)
    if not ok:
        return give_up(rt, slot, why)
    if rt.screen_dialog_class(slot, info) == 'delete':
        return give_up(rt, slot, 'a delete confirmation dialog is on its screen; deleting is the user\'s alone, '
                                 'nothing is pressed')
    window, cap = float(knobs['held_requeue_window_s']), int(knobs['held_requeue_max'])
    try:
        log = [float(t) for t in json.loads(info.get('requeue_log') or '[]') if now - float(t) < window]
    except (ValueError, TypeError):
        log = []
    if len(log) >= cap:
        return give_up(rt, slot, 'requeue budget used up (' + str(len(log)) + ' new generations within '
                       + str(int(window)) + ' s)')
    if f.paused():
        if info.get('requeue_state') != 'waiting: paused':
            f.setf(slot, requeue_state='waiting: paused')
            rt.audit('held_requeue.waits_paused', slot=slot, level='warn', message_id=m.get('message_id', ''))
        return 'waiting'
    reason = str(rt.bus.r.hget(rt.bus.msg_key(m.get('message_id') or '-'), 'uncertain_reason')
                 or 'execution state unconfirmed')[:300]
    log.append(now)
    f.setf(slot, requeue_log=json.dumps(log), requeue_state='rotating')
    rt.audit('held_requeue.started', slot=slot, level='error', reason=reason, message_id=m.get('message_id', ''),
             request_id=m.get('request_id', ''), detail={'attempt': len(log), 'max': cap, 'window_s': window,
                                                         'cause': m.get('cause', '')})
    read = getattr(f.mgr, 'capture_output', None)
    try:
        screen = (read(pane, 2000) if read else '') or ''
    except Exception:  # noqa: BLE001  (the screen is evidence only)
        screen = ''
    if not rt.watchdog.rotate(slot, 'held: ' + reason, screen, reason='requeue'):
        return give_up(rt, slot, 'the old session could not be proven stopped (rotation failed)')
    return 'started'


def give_up(rt, slot, why):
    """Today's behaviour for an interrupted request: publish failed, pause the run (the hold owns that pause, so the
    held watch may lift it as before), report upwards as not_recovered. The requeue mark is cleared."""
    f = rt.f
    info = f.agent(slot)
    m = due(info) or {}
    reason = str(rt.bus.r.hget(rt.bus.msg_key(m.get('message_id') or '-'), 'uncertain_reason')
                 or 'execution state unconfirmed')
    f.setf(slot, requeue_due='', requeue_state='gave_up: ' + why[:200])
    rt.audit('held_requeue.gave_up', slot=slot, level='error', error=why[:300], message_id=m.get('message_id', ''),
             request_id=m.get('request_id', ''))
    rt.transaction('requeue-giveup-' + uuid.uuid4().hex, lambda: rt.hold(
        slot, reason + '; not re-run: ' + why, public_status='failed'))
    held_s = int(awake.now() - float(m.get('at') or awake.now()))
    f.setf(slot, held_alert_at=awake.now(), held_tried=1)
    rt.held_escalate(slot, 'held (' + reason[:300] + ') and not re-run: ' + why,
                     {'held_s': held_s, 'paused': f.paused(), 'state': 'held', 'reason': 'not_recovered',
                      'requeue': why[:200]})
    return 'gave_up'
