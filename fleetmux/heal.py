"""Parent heals child (docs/design.md section 67): the supervisor one level up recovers a stuck sub-fleet supervisor.

A sub-fleet's supervisor is a worker slot of its parent fleet, adopted by the child daemon (subfleet.py). The child
cannot give that pane a new session, and the daemon never regenerates a sub-fleet supervisor by itself: only a root
fleet's supervisor is the daemon's (section 24). So whatever stops a sub-fleet's supervisor -- held, dead, a usage wall, a
start that never said READY, a model switch that failed -- and whatever stops the parent's own slot that serves (or
is to serve) as one, is told to the parent fleet's supervisor with the sub-fleet's id and one line to copy; its
``heal`` decision makes this program act and answer ``[heal result]``.

Notices (parent supervisor, queued at the front, standalone; one per target and cause until it recovers)
  [sub-fleet supervisor held|dead] / [sub-fleet walled] / [sub-fleet held] (a worker of the child) /
  [slot down] / [sub-fleet not up] / [sub-fleet recovered] / [slot recovered] / [heal result]

Decision key (task and service mode), items run in order:
  {"subfleet": <id or name>, "reopen": "stay" | "next" | "head" | <card id>}   the sub-fleet's supervisor
  {"subfleet": <id or name>, "slot": <its worker>, "reopen": ...}             a slot inside that sub-fleet
  {"slot": <own slot>, "reopen": ...}                                         a dead / held / walled own slot
  {"clear_wall": "<cli>:<model>", "in": "self" | <sub-fleet id or name>}      drop a wall record (fx:walls)

Parent Redis
  fx:heal:notices   hash target -> {cause, at}: what was told (dedup; deleted with the recovered line)
  fx:heal:jobs      list of accepted items, run by ``tick`` outside any transaction
  fx:heal:reopen    hash task -> pending sub-fleet supervisor reopen {slot, run, launch, phase, at}
  fx:heal:slots     hash slot -> pending own-slot reopen {generation, at, remote}
  fx:heal:remote    hash id -> items sent to a sub-fleet {task, config, at}
  fx:heal:waits     hash name -> first time an approved / autostart up waited (awake seconds)
Child Redis
  fx:heal:result:<id>  the child's answer to the parent's items (JSON {ok, text})
"""
import json
import time
import uuid
from pathlib import Path

from fleetmux import awake

NOTICES = 'fx:heal:notices'
JOBS = 'fx:heal:jobs'
REOPEN = 'fx:heal:reopen'
SLOTS = 'fx:heal:slots'
REMOTE = 'fx:heal:remote'
WAITS = 'fx:heal:waits'
RESULT = 'fx:heal:result:'
MODES = ('stay', 'next', 'head')
ITEM_KEYS = {'subfleet', 'slot', 'reopen', 'clear_wall', 'in', 'note'}
PASS_KINDS = ('startup', 'wall')  # pauses a heal notice / a heal decision passes (a slot failure, a wall)
HEADS = ('[slot down]', '[slot recovered]', '[sub-fleet supervisor held]', '[sub-fleet supervisor dead]',
         '[sub-fleet walled]', '[sub-fleet held]', '[sub-fleet not up]', '[sub-fleet recovered]', '[heal result]')
NOT_UP_AFTER = 600  # seconds an approved / autostart up may wait before the supervisor is told (named SLOT_WAIT)
REOPEN_LIMIT = 900  # seconds a sub-fleet supervisor reopen may take before it is reported as failed
REOPEN_PROMPT = ('You are fleet agent {label}. Your previous session supervised sub-fleet {task}; the parent '
                 'supervisor reopened it, and this new session takes it over in a moment. Read AGENTS.md in the '
                 'current directory first. In this turn do no task work: your final reply must be exactly READY.')


def _fleet():
    from fleetmux import fleet
    return fleet


def _loads(raw, default=None):
    try:
        return json.loads(raw) if raw else default
    except (TypeError, ValueError):
        return default


def line(items):
    """The copy-paste line a notice offers."""
    return '"heal": ' + json.dumps(items, ensure_ascii=False)


def tail(f):
    return ('\nAnswer with the usual service decision JSON (status RUNNING, dispatch [], skip [])'
            if f.service else '\nContinue the current task using the AGENTS.md decision JSON.')


# ------------------------------------------------------------------ notices (dedup, recovered)
def tell(f, text, send=True, **queue):
    """The one way a notice reaches a supervisor (section 67): any CLI wall wording in ``text`` becomes ``hit a <scope>
    wall[, resets <time>]`` (brake.unquote_walls), so no notice puts a CLI wall line on a supervisor's pane. Queues
    it for this fleet's supervisor when ``send``; returns the text as sent (callers reuse it for user channels)."""
    from fleetmux import brake
    text = brake.unquote_walls(text)
    if send:
        f.queue('supervisor', text, **queue)
    return text


def notify(f, target, cause, text, items=None):
    """Tell this fleet's supervisor once per (target, cause): queued at the front, standalone. A cause seen again
    within held_realert_s is not sent again; a new cause is sent at once. Returns True when it was queued."""
    from fleetmux import runtime_v2
    old = _loads(f.r.hget(NOTICES, target), {}) or {}
    realert = float(runtime_v2.held_watch_knobs()[0]['held_realert_s'])
    if old.get('cause') == cause and awake.since_wall(float(old.get('at') or 0)) < realert:
        return False
    f.r.hset(NOTICES, mapping={target: json.dumps({'cause': cause, 'at': time.time()}, ensure_ascii=False)})
    body = text + ('\nTo fix it, add this to your next decision (copy it as it is): ' + line(items) if items else '')
    body = tell(f, body + tail(f), front=True)
    f.log('heal.notice', target=target, cause=cause, text=body[:300])
    return True


def recovered(f, target, text):
    if not f.r.hdel(NOTICES, target):
        return False
    f.log('heal.recovered', target=target, text=text[:300])
    tell(f, text + tail(f), front=True)
    return True


def result(f, target, ok, why, remote=None):
    """``[heal result]`` to this fleet's supervisor, or (``remote``: the parent's request id) the answer the parent
    reads from this fleet's Redis."""
    f.log('heal.result', target=target, ok=ok, why=str(why)[:300], remote=remote or '')
    if remote:
        f.r.set(RESULT + remote, json.dumps({'ok': ok, 'text': target + ': ' + ('done: ' if ok else 'refused: ')
                                             + str(why)}, ensure_ascii=False), ex=86400)
        return
    tell(f, '[heal result] ' + target + ': ' + ('done: ' if ok else 'refused: ') + str(why) + tail(f), front=True)


def is_notice(text):
    return str(text or '').lstrip().startswith(HEADS)


def pass_through(f):
    """The pause is owned by a slot failure or a wall (heal notices and heal decisions pass it; operator,
    budget and watchdog holds stay closed)."""
    from fleetmux.fleet import SERVICE
    cause = _loads(f.r.hget(SERVICE, 'cause'), {}) or {}
    return cause.get('kind') in PASS_KINDS


def pump_paused(f):
    """Fleet.pump while paused: deliver the supervisor's next message only when it is a heal notice and the pause
    is one a heal may pass."""
    if not getattr(f, 'v2', None) or not pass_through(f):
        return
    info = f.agent('supervisor')
    if info.get('state') != 'idle' or not f.inbox_depth('supervisor'):
        return
    ids = f.v2.bus.pending('supervisor', 1)
    msg = f.v2.bus.message(ids[0]) if ids else {}
    if is_notice(((msg or {}).get('body') or {}).get('text') if isinstance((msg or {}).get('body'), dict)
                 else _loads((msg or {}).get('body'), {}).get('text')):
        f.deliver('supervisor')


# ------------------------------------------------------------------ the decision key
def validate_decision(items):
    if not isinstance(items, list) or not items:
        raise ValueError('heal must be a non-empty list of items')
    for item in items:
        if not isinstance(item, dict) or not set(item) <= ITEM_KEYS:
            raise ValueError('every heal item is an object with keys from ' + ', '.join(sorted(ITEM_KEYS)))
        if 'clear_wall' in item:
            cw = item['clear_wall']
            if not isinstance(cw, str) or ':' not in cw or set(item) - {'clear_wall', 'in', 'note'}:
                raise ValueError('heal clear_wall must be {"clear_wall": "<cli>:<model>", "in": "self" | <sub-fleet>}')
            if not isinstance(item.get('in', 'self'), str):
                raise ValueError('heal clear_wall "in" must be "self" or a sub-fleet id')
            continue
        if not isinstance(item.get('reopen'), str) or not item['reopen'].strip():
            raise ValueError('heal reopen must be "stay", "next", "head" or a card id')
        if 'subfleet' not in item and 'slot' not in item:
            raise ValueError('heal reopen needs "subfleet" (its supervisor) or "slot" (one of your slots)')
        for k in ('subfleet', 'slot'):
            if k in item and (not isinstance(item[k], str) or not item[k]):
                raise ValueError('heal ' + k + ' must be a name')
    return items


def accept(f, items, remote=None):
    """Inside the decision / event handling: the items wait for ``tick`` (side effects run outside transactions)."""
    f.r.rpush(JOBS, json.dumps({'items': items, 'remote': remote or '', 'at': time.time()}, ensure_ascii=False))
    f.log('heal.accepted', items=items, remote=remote or '')


# ------------------------------------------------------------------ tick (parent and child, outside transactions)
def tick(f):
    if not getattr(f, 'v2', None):
        return
    while True:
        raw = f.r.lpop(JOBS)
        if raw is None:
            break
        job = _loads(raw, {}) or {}
        run_items(f, job.get('items') or [], job.get('remote') or None)
    for part in (watch_own_slots, progress_slots, progress_reopens, progress_remote, watch_children):
        try:
            part(f)
        except Exception as exc:  # a heal step never takes the daemon down
            f.log('heal.tick_failed', level='error', part=part.__name__, error=repr(exc)[:300])
    if f.cfg.get('adopt'):
        child_lift(f)


def run_items(f, items, remote=None):
    from fleetmux import subfleet
    groups = {}  # items for a sub-fleet go there together, in order
    for item in items:
        where = item.get('in') if 'clear_wall' in item else (item.get('subfleet') if item.get('slot') else None)
        if where and where not in ('self', f.cfg.get('task_id')):
            d = find_child(f, where)
            if d is None:
                result(f, where, False, 'no running sub-fleet ' + where + ' in this fleet', remote)
                continue
            groups.setdefault(str(d), []).append(dict({k: v for k, v in item.items() if k not in ('subfleet', 'in')},
                                                      **({'in': 'self'} if 'clear_wall' in item else {})))
            continue
        try:
            if 'clear_wall' in item:
                ok, why = clear_wall(f, item['clear_wall'])
                result(f, 'clear_wall ' + item['clear_wall'], ok, why, remote)
            elif item.get('slot'):
                reopen_slot(f, item['slot'], item['reopen'], remote)
            else:
                reopen_subfleet(f, item['subfleet'], item['reopen'], remote)
        except Exception as exc:  # reported, never raised into the daemon
            result(f, item.get('subfleet') or item.get('slot') or item.get('clear_wall') or '?', False,
                   'program error: ' + repr(exc)[:200], remote)
    for d, sub in groups.items():
        send_remote(f, Path(d), sub, remote)


def find_child(f, name):
    from fleetmux import subfleet
    for d in subfleet.active_children(f.run_dir):
        rec = subfleet.record(d)
        if name in (rec.get('task_id'), rec.get('name')):
            return Path(d)
    return None


# ------------------------------------------------------------------ clear a wall record
def clear_wall(f, field):
    """(ok, why). An account wall drops every record of that account (the whole account is usable again)."""
    from fleetmux.fleet import WALLS
    walls = {k: _loads(v, {}) or {} for k, v in f.r.hgetall(WALLS).items()}
    rec = walls.get(field)
    if rec is None:
        cli = field.split(':', 1)[0]
        same = [k for k, v in walls.items() if k.split(':', 1)[0] == cli and v.get('scope') == 'account']
        if not same:
            return False, 'no wall record ' + field + ' in this fleet (records: ' + (', '.join(sorted(walls)) or 'none') + ')'
        rec = walls[same[0]]
    drop = [k for k, v in walls.items() if k == field or (rec.get('scope') == 'account'
                                                          and v.get('account') == rec.get('account'))]
    f.r.hdel(WALLS, *drop)
    f.log('brake.wall_cleared', fields=drop, by='heal')
    return True, 'wall record' + ('s ' if len(drop) > 1 else ' ') + ', '.join(sorted(drop)) + ' cleared; the chain starts from its head again'


# ------------------------------------------------------------------ the model of a reopened slot
def slot_launch(f, slot, mode):
    """(launch dict or None = the slot's default, None) or (False, why refused)."""
    from fleetmux import brake, discover
    from fleetmux.fleet import agent_cli, claude_model
    info = f.agent(slot)
    current = _loads(info.get('runtime_launch'), None)
    if mode == 'stay':
        return current, None
    if mode not in MODES:  # a card
        launch, why = f.split_supervisor_launch(slot, mode, None)
        return (launch, None) if launch else (False, why)
    cfg = f.runtime_cfg(slot)
    cli, model = agent_cli(cfg, slot), f.slot_model(slot, cfg)
    entries, patterns = f.wall_chain(slot)
    at = discover.place(entries, patterns, cli, model or (claude_model(cfg, slot) if cli == 'claude' else ''))
    below = entries if mode == 'head' else (entries[at + 1:] if at is not None else entries)
    action = brake.wall_action(cli, model, below, f.walled(), lambda c, m: f.wall_refused(slot, c))
    if action['action'] != 'switch':
        return False, ('no usable model on the chain (' + ', '.join(action.get('skipped') or []) + ')'
                       if action.get('skipped') else 'the chain has no ' + mode + ' entry here')
    if (action['to_cli'], action['to']) == (cli, model):
        return current, None
    req = dict(action, key=f.wall_key(slot, action['to_cli']), from_key=f.wall_key(slot, cli), source='heal',
               at=uuid.uuid4().hex[:12], kind='heal')
    launch, req = f.wall_target(slot, current or {}, req, cfg)
    f.setf(slot, wall_downgrade='', wall_switch='')
    if not launch:
        return False, 'every model the chain offers is refused here'
    return launch, None


def supervisor_launch(f, slot, mode, current_launch):
    """The model of a sub-fleet supervisor's next session: stay (the delegated session's), next / head on the
    supervisor chain (Claude only, design section 23 gates), or a card. (launch or None, None) / (False, why)."""
    from fleetmux import job_chain, named_subfleet
    from fleetmux.fleet import agent_cli, claude_model
    if mode == 'stay':
        return current_launch, None
    if mode not in MODES:
        launch, why = f.split_supervisor_launch(slot, mode, None)
        return (launch, None) if launch else (False, why)
    try:
        registry = named_subfleet._registry(f)
    except Exception as exc:  # noqa: BLE001
        return False, 'cannot read agent cards: ' + str(exc)

    def refused(c, m):
        card = f.job_model_card(c, m, registry)
        gate = f.card_gate(slot, card, registry) if card else None
        return gate[1] if gate else None
    cfg = f.runtime_cfg(slot)
    cur = (agent_cli(cfg, slot), claude_model(cfg, slot))
    skip, seen, walled = [], mode == 'head', list(f.walled())
    for _ in range(32):
        pick = job_chain.subfleet_supervisor(walled + skip, registry=registry, refused=refused)
        entry = pick.get('entry')
        if entry is None:
            return False, 'no usable supervisor on the chain: ' + str(pick.get('reason') or 'every entry is walled or refused')
        here = job_chain.runs_pick(pick, pick['chain'], *cur)
        if mode == 'next' and here:
            seen = True
            skip.append({'cli': entry['cli'], 'model': entry['model'], 'scope': 'model_pool'})
            continue
        if seen or mode == 'next':
            launch, why = f.split_supervisor_launch(slot, pick.get('profile'), None if pick.get('profile')
                                                    else entry['model'])
            return (launch, None) if launch else (False, why)
        skip.append({'cli': entry['cli'], 'model': entry['model'], 'scope': 'model_pool'})
    return False, 'no usable supervisor on the chain'


# ------------------------------------------------------------------ reopen an own slot
OWN_STATES = ('dead', 'held', 'walled')


def reopen_slot(f, slot, mode, remote=None):
    from fleetmux import profile_launch
    name = f.resolve(slot) if hasattr(f, 'resolve') else slot
    if name not in f.names or name == 'supervisor':
        return result(f, slot, False, ('the supervisor of this fleet is not reopened by heal (a root supervisor is the '
                                       "daemon's; a sub-fleet's is reopened from its parent)") if name == 'supervisor'
                      else 'no slot ' + slot + ' in this fleet', remote)
    info = f.agent(name)
    walled = bool(info.get('wall')) or wall_paused_by(f, name)
    if info.get('state') == 'delegated':
        return result(f, slot, False, name + ' supervises sub-fleet ' + str(info.get('delegated_task')) + ' now: '
                      'reopen that sub-fleet ({"subfleet": "' + str(info.get('delegated_task')) + '", "reopen": ...})', remote)
    if info.get('state') not in OWN_STATES and not walled:
        return result(f, slot, False, name + ' is ' + str(info.get('state')) + ', not dead / held / walled', remote)
    launch, why = slot_launch(f, name, mode)
    if launch is False:
        return result(f, slot, False, why, remote)
    lease = f.v2.bus.slot_state(name)
    msg = None
    if lease.get('current_delivery'):
        try:
            msg = f.v2.bus.message(lease['message_id'])
        except Exception:  # noqa: BLE001
            msg = None
    f.setf(name, wall='', wall_downgrade='', wall_switch='', startup_retries=0, startup_retry_at=0,
           stopfail_at='', stopfail_retry_at='', stopfail_g='', startup_nudged='', startup_timeout='')
    pane = info.get('pane')
    why_rot = 'reopened by the parent supervisor (' + mode + ')'
    if pane and f.mgr.get_pane_status(pane) is not None:
        ok = f.wall_rotate(name, launch, why_rot)
    else:
        if launch is not None:
            f.setf(name, split_launch=json.dumps({'launch': profile_launch.encode_launch(launch)}))
        f.setf(name, state='idle')
        f.v2.respawn([name], True)
        ok = True
    if not ok:
        return result(f, slot, False, 'the new generation could not be started: '
                      + str(f.agent(name).get('profile_reason') or 'see the audit'), remote)
    if msg is not None and f.v2.redeliverable(name, msg):
        f.v2.redeliver(name, msg, time.time())
    gen = int(f.agent(name).get('generation') or 0)
    f.r.hset(SLOTS, mapping={name: json.dumps({'generation': gen, 'at': awake.now(), 'remote': remote or '',
                                              'mode': mode})})
    result(f, slot, True, f.label(name) + ' (generation ' + str(gen) + ') starts on '
           + _model_text(f, name) + '; the pause this slot caused is lifted once it is READY', remote)


def _model_text(f, slot):
    from fleetmux.fleet import agent_cli
    cfg = f.runtime_cfg(slot)
    return agent_cli(cfg, slot) + ':' + (f.slot_model(slot, cfg) or 'default')


def progress_slots(f):
    for slot, raw in f.r.hgetall(SLOTS).items():
        mark = _loads(raw, {}) or {}
        info = f.agent(slot)
        if int(info.get('generation') or 0) < int(mark.get('generation') or 0):
            continue
        if info.get('state') in ('idle', 'busy') and info.get('runtime_ready') == '1':
            f.r.hdel(SLOTS, slot)
            lifted = lift_owned(f, slot)
            f.log('heal.slot_ready', slot=slot, lifted=lifted)
            if not mark.get('remote'):
                recovered(f, 'slot:' + slot, '[slot recovered] ' + f.label(slot) + ': READY on ' + _model_text(f, slot)
                          + ('; the pause it caused is lifted' if lifted else ''))
                f.r.hdel(NOTICES, 'slot:' + slot)
        elif awake.since(float(mark.get('at') or awake.now())) > float(f.cfg.get('profile_ready_timeout') or 300) + 120:
            f.r.hdel(SLOTS, slot)
            result(f, slot, False, 'the new generation did not say READY', mark.get('remote') or None)


# ------------------------------------------------------------------ pauses a heal owns
def wall_paused_by(f, slot):
    from fleetmux.fleet import BRAKE
    owner = _loads(f.r.hget(BRAKE, 'paused_by'), {}) or {}
    return f.paused() and owner.get('kind') == 'wall' and owner.get('agent') == slot


def lift_owned(f, slot):
    """Lift the pause ``slot`` owns: its startup failure (cause kind startup + slot), its wall (brake paused_by /
    cause kind wall + slot). An operator, budget, watchdog or any other pause stays. Never past a budget line."""
    from fleetmux import brake
    from fleetmux.fleet import BRAKE, SERVICE
    if not f.paused():
        return False
    cause = _loads(f.r.hget(SERVICE, 'cause'), {}) or {}
    mine = cause.get('kind') in PASS_KINDS and cause.get('slot') == slot
    if not mine and not wall_paused_by(f, slot):
        return False
    budget = f.brake_budget() if hasattr(f, 'brake_budget') else None
    if budget and brake.trips(f.brake_card(budget), brake.counters(f.r.hgetall(BRAKE), f.quota_reading())):
        return False
    f.r.hset(BRAKE, mapping={'tripped': '', 'paused_by': ''})
    f.resume()
    f.log('heal.pause_lifted', slot=slot, cause=cause.get('kind') or 'wall')
    return True


# ------------------------------------------------------------------ reopen a sub-fleet's supervisor
def reopen_subfleet(f, name, mode, remote=None):
    """Suspend the sub-fleet in place (its daemon stops, its Redis AOF, run directory and task stay), give the slot
    that supervises it its next generation on the chosen model, then hand the same sub-fleet to that generation
    (subfleet.readopt_child: ``daemon --resume``). An adopted supervisor (the parent owns the pane) is no exception."""
    from fleetmux import subfleet
    d = find_child(f, name)
    if d is None:
        return result(f, name, False, 'no running sub-fleet ' + name + ' in this fleet', remote)
    rec = subfleet.record(d)
    if rec.get('state') != 'running':
        return result(f, name, False, 'sub-fleet ' + name + ' is ' + str(rec.get('state')), remote)
    if f.r.hexists(REOPEN, rec['task_id']):
        return result(f, name, False, 'a reopen of ' + name + ' is already running', remote)
    slot = rec['slot']
    launch, why = supervisor_launch(f, slot, mode, _loads(f.agent(slot).get('runtime_launch'), None))
    if launch is False:
        return result(f, name, False, why, remote)
    cfg = subfleet.child_config_or_fail(f, d, rec)
    if cfg is None:
        return result(f, name, False, 'sub-fleet ' + name + ' has no readable config', remote)
    from fleetmux import profile_launch
    f.r.hset(REOPEN, mapping={rec['task_id']: json.dumps({
        'slot': slot, 'run': str(d), 'mode': mode, 'phase': 'suspending', 'at': awake.now(), 'remote': remote or '',
        'launch': profile_launch.encode_launch(launch) if launch else '', 'job': rec.get('job') or '',
        'generation': int(f.agent(slot).get('generation') or 0), 'named': bool(rec.get('named'))})})
    rec.update(state='stopping', outcome='suspended', reason='reopened by the parent supervisor', heal_reopen=1)
    subfleet.save_record(d, rec)
    if rec.get('named'):
        from fleetmux import named_subfleet
        f.r.hset(named_subfleet.HASH + rec['name'], mapping={'state': 'starting', 'started_at': time.time()})
    subfleet.post_stop(cfg, suspend=True)
    f.log('heal.reopen_started', task=rec['task_id'], slot=slot, mode=mode)


def collected(f, rec):
    """subfleet.collect took the pane back from a sub-fleet suspended for a reopen: the slot's next generation
    starts (``split_launch`` set here), the record waits as ``suspended`` for ``progress_reopens``."""
    mark = _loads(f.r.hget(REOPEN, rec['task_id']), {}) or {}
    slot = rec['slot']
    f.setf(slot, split_launch=json.dumps({'launch': mark['launch']}) if mark.get('launch') else '', split_restore='')
    mark['phase'] = 'respawning'
    f.r.hset(REOPEN, mapping={rec['task_id']: json.dumps(mark)})
    return REOPEN_PROMPT.format(label=f.label(slot), task=rec['task_id'])


def progress_reopens(f):
    from fleetmux import subfleet
    for task, raw in f.r.hgetall(REOPEN).items():
        mark = _loads(raw, {}) or {}
        d = Path(mark.get('run') or '.')
        rec = subfleet.record(d)
        slot = mark.get('slot')
        if awake.since(float(mark.get('at') or awake.now())) > REOPEN_LIMIT:
            f.r.hdel(REOPEN, task)
            result(f, task, False, 'the reopen did not finish within ' + str(REOPEN_LIMIT) + ' s (phase '
                   + str(mark.get('phase')) + ')', mark.get('remote') or None)
            continue
        if mark.get('phase') == 'respawning' and rec.get('state') == 'suspended':
            info = f.agent(slot)
            if (info.get('state') != 'idle' or info.get('runtime_ready') != '1'
                    or f.r.hget(f.v2.bus.lease_key(slot), 'current_delivery')):
                continue
            job = {'id': rec.get('job'), 'resume_run': str(d), 'suspended_at': rec.get('finished') or time.time()}
            try:
                subfleet.readopt_child(f, slot, task, job, adopt_extra={'heal_lift': True})
            except subfleet.Refused as exc:
                f.r.hdel(REOPEN, task)
                result(f, task, False, str(exc), mark.get('remote') or None)
                continue
            mark['phase'] = 'readopted'
            f.r.hset(REOPEN, mapping={task: json.dumps(mark)})
            if rec.get('named'):
                from fleetmux import named_subfleet
                f.r.hset(named_subfleet.HASH + rec['name'], mapping={'state': 'starting', 'started_at': time.time(),
                                                                     'slot': slot})
            result(f, task, True, 'its supervisor ' + f.label(slot) + ' runs a new session on ' + _model_text(f, slot)
                   + ' and takes sub-fleet ' + task + ' over where it stood; [sub-fleet recovered] follows once it '
                   'is READY', mark.get('remote') or None)
        elif mark.get('phase') == 'readopted':
            peek = child_peek(d)
            if peek and peek['ready'] and not peek['paused']:
                f.r.hdel(REOPEN, task)
                f.r.hdel(NOTICES, 'sub:' + task)
                f.log('heal.reopened', task=task, slot=slot)
                if not mark.get('remote'):
                    f.queue('supervisor', '[sub-fleet recovered] ' + task + ': its supervisor ' + f.label(slot)
                            + ' is READY in its new session and the sub-fleet goes on' + tail(f), front=True)


# ------------------------------------------------------------------ items sent to a sub-fleet
def send_remote(f, d, items, remote=None):
    from fleetmux import runtime_v2, subfleet
    rec = subfleet.record(d)
    cfg = subfleet.child_config_or_fail(f, d, rec)
    if cfg is None or not subfleet.redis_up(cfg):
        return result(f, rec.get('task_id') or str(d), False, 'its Redis cannot be reached', remote)
    rid = 'heal-' + uuid.uuid4().hex[:10]
    r = _fleet().connect(cfg)
    try:
        runtime_v2.post_event(runtime_v2.bus_for(cfg, r), {'kind': 'heal', 'request_id': rid, 'items': items}, rid)
    finally:
        r.close()
        r.connection_pool.disconnect()
    f.r.hset(REMOTE, mapping={rid: json.dumps({'task': rec.get('task_id'), 'config': str(d / 'config.json'), 'at': awake.now(),
                                               'remote': remote or '', 'n': len(items)})})
    f.log('heal.sent', task=rec.get('task_id'), id=rid, items=items)


def progress_remote(f):
    for rid, raw in f.r.hgetall(REMOTE).items():
        mark = _loads(raw, {}) or {}
        try:
            cfg = _fleet().load(mark['config'])
            r = _fleet().connect(cfg)
        except Exception:  # noqa: BLE001
            r = None
        answers = []
        if r is not None:
            try:
                for k in sorted(r.scan_iter(RESULT + rid + '*')):
                    answers.append((k, _loads(r.get(k), {}) or {}))
                    r.delete(k)
            finally:
                r.close()
                r.connection_pool.disconnect()
        for _, ans in answers:
            text = ans.get('text') or ''
            f.queue('supervisor', '[heal result] ' + str(mark.get('task')) + ' / ' + text + tail(f), front=True)
            f.log('heal.result', target=mark.get('task'), ok=ans.get('ok'), why=text[:300], remote='')
        left = int(mark.get('n') or 1) - len(answers)
        if left <= 0:
            f.r.hdel(REMOTE, rid)
        elif awake.since(float(mark.get('at') or awake.now())) > REOPEN_LIMIT:
            f.r.hdel(REMOTE, rid)
            result(f, str(mark.get('task')), False, 'the sub-fleet did not answer the heal within '
                   + str(REOPEN_LIMIT) + ' s')
        else:
            mark['n'] = left
            f.r.hset(REMOTE, mapping={rid: json.dumps(mark)})


def on_event(f, ev):
    """Child daemon: the parent's ``heal`` event. Each item answers under fx:heal:result:<id>-<n>."""
    items = ev.get('items')
    try:
        validate_decision(items)
    except ValueError as exc:
        f.r.set(RESULT + str(ev.get('request_id')) + '-0', json.dumps({'ok': False, 'text': str(exc)}), ex=86400)
        return {'status': 'rejected', 'reason': str(exc)}
    for i, item in enumerate(items):
        accept(f, [item], remote=str(ev.get('request_id')) + '-' + str(i))
    return {'status': 'accepted'}


# ------------------------------------------------------------------ child side: lift after a readopt, report up
def child_lift(f):
    """Child daemon after a heal readopt: once the new supervisor session is READY, lift the pause its
    predecessor owned (its wall, its startup failure), then clear the mark."""
    adopt = f.cfg.get('adopt') or {}
    if not adopt.get('heal_lift'):
        return
    info = f.agent('supervisor')
    if info.get('runtime_ready') != '1' or info.get('state') not in ('idle', 'busy'):
        return
    lift_owned(f, 'supervisor')
    f.setf('supervisor', wall='')
    fleet = _fleet()
    cfg = fleet.load(f.path)
    cfg['adopt']['heal_lift'] = False
    fleet.save(f.path, cfg)
    f.cfg['adopt']['heal_lift'] = False
    f.log('heal.child_resumed')


def report_up(f, state, slot, text, **extra):
    """A sub-fleet tells its parent (subfleet-notices, ``heal-*.json``) that ``slot`` is held / dead / walled /
    could not switch. Nothing for a root fleet (its supervisor is the daemon's)."""
    parent = f.cfg.get('parent_config')
    if not parent:
        return False
    fleet = _fleet()
    try:
        folder = Path(fleet.load(parent)['run']) / 'subfleet-notices'
        folder.mkdir(parents=True, exist_ok=True)
        fleet.save(folder / ('heal-' + uuid.uuid4().hex[:12] + '.json'), dict(
            extra, state=state, origin_run=str(f.run_dir), task=f.cfg.get('task_id'), slot=slot, label=f.label(slot),
            text=str(text)[:600], walls=walls_of(f, slot)))
        return True
    except Exception as exc:  # a notice never takes the daemon down
        f.log('heal.report_failed', level='error', error=str(exc)[:300])
        return False


def walls_of(f, slot):
    from fleetmux.fleet import WALLS
    return sorted(k for k, v in f.r.hgetall(WALLS).items() if (_loads(v, {}) or {}).get('agent') == slot)


def child_note(f, path):
    """Parent: a ``heal-*.json`` note of one of its sub-fleets -> the notice with the line to copy."""
    fleet = _fleet()
    try:
        note = fleet.load(path)
    except (OSError, ValueError):
        note = None
    path.unlink(missing_ok=True)
    if not note:
        return
    describe(f, note.get('task') or '', note.get('origin_run') or '', note.get('slot') or 'supervisor',
             note.get('label') or note.get('slot') or '', note.get('state') or 'held', note.get('text') or '',
             note.get('walls') or [])


def describe(f, task, run, slot, label, state, text, walls=()):
    sup = slot == 'supervisor'
    if state == 'walled':
        items = ([{'clear_wall': w, 'in': task} for w in walls]
                 + [{'subfleet': task, 'reopen': 'next'} if sup else {'subfleet': task, 'slot': slot, 'reopen': 'head'}])
        if sup and walls:
            items[-1] = {'subfleet': task, 'reopen': 'head'}
        # section 67: ``text`` is the child's brake reason, which names the wall (brake.wall_phrase) and quotes no CLI line
        body = '[sub-fleet walled] sub-fleet ' + task + ' (' + run + '): ' + str(text).rstrip('. ') + '. The sub-fleet is paused.'
    else:
        what = {'dead': 'is dead', 'switch_failed': 'could not switch model', 'held': 'is held'}.get(state, 'is ' + state)
        if sup:
            head = '[sub-fleet supervisor ' + ('held' if state == 'held' else 'dead') + '] sub-fleet ' + task + ' (' + run + ')'
            items = [{'subfleet': task, 'reopen': 'stay' if state == 'held' else 'next'}]
            body = head + ': its supervisor ' + label + ' ' + what + ': ' + text + '. Its daemon does not replace a supervisor.'
        else:
            head = '[sub-fleet held] sub-fleet ' + task + ' (' + run + ')'
            items = [{'subfleet': task, 'slot': slot, 'reopen': 'stay'}]
            body = head + ': its ' + label + ' ' + what + ': ' + text + '.'
    notify(f, 'sub:' + task, state + ':' + slot, body, items)


def held_line(f, note):
    """Fleet.held_notice: a section 24 held escalation of a sub-fleet, told with the line to copy (both modes)."""
    describe(f, str(note.get('task') or ''), str(note.get('origin_run') or ''), note.get('slot') or 'supervisor',
             str(note.get('label') or note.get('slot') or ''), note.get('state') or 'held', str(note.get('text') or ''),
             note.get('walls') or [])


# ------------------------------------------------------------------ parent watch: own slots, ups, recovered
def served(f):
    """{slot: sub-fleet name} of this fleet's slots that serve (or are to serve) as a sub-fleet supervisor."""
    from fleetmux import named_subfleet, subfleet
    out = {}
    try:
        for name, entry in named_subfleet.declared(f).items():
            if entry.get('supervisor_slot'):
                out[entry['supervisor_slot']] = name
    except Exception:  # noqa: BLE001
        pass
    for d in subfleet.active_children(f.run_dir):
        rec = subfleet.record(d)
        if rec.get('slot'):
            out[rec['slot']] = rec.get('name') or rec.get('task_id')
    return out


def watch_own_slots(f):
    pending = f.r.hgetall(SLOTS)
    for slot, name in served(f).items():
        if slot not in f.names or slot in pending:
            continue
        info = f.agent(slot)
        state = info.get('state')
        target = 'slot:' + slot
        if state in ('dead', 'held'):
            why = (info.get('profile_reason') or info.get('regen_reason') or
                   ('its start did not say READY' if state == 'dead' else 'its turn is held'))
            notify(f, target, state, '[slot down] ' + f.label(slot) + ' (supervisor slot of sub-fleet ' + name + ') is '
                   + state + ': ' + why + '.', [{'slot': slot, 'reopen': 'stay'}])
        elif state in ('idle', 'busy', 'delegated') and f.r.hexists(NOTICES, target):
            recovered(f, target, '[slot recovered] ' + f.label(slot) + ': ' + state + ' again')


def not_up(f, name, rid, why):
    """An approved (or autostart) up of named sub-fleet ``name`` has waited NOT_UP_AFTER seconds: tell the
    supervisor why (slot dead / held / busy / quota) with what to copy."""
    from fleetmux import named_subfleet
    entry = named_subfleet.declared(f).get(name) or {}
    slot = entry.get('supervisor_slot') or ''
    info = f.agent(slot) if slot in f.names else {}
    state = info.get('state') or ''
    if state in ('dead', 'held'):
        cause, items = 'slot ' + state, [{'slot': slot, 'reopen': 'stay'}]
    elif why.startswith(named_subfleet.CHAIN_WAIT):
        cause = 'quota: ' + why[len(named_subfleet.CHAIN_WAIT):]
        items = [{'clear_wall': w, 'in': 'self'} for w in sorted(f.r.hkeys(_fleet().WALLS))][:4] or None
    else:
        cause, items = 'slot busy: ' + why, None
    notify(f, 'up:' + name, cause.split(':')[0], '[sub-fleet not up] ' + name + ': '
           + ('approved ' + rid if rid else 'autostart') + ' ' + str(NOT_UP_AFTER // 60) + ' min ago and not up: '
           + cause + '.', items)


def up_waits(f, name, rid, why):
    """named_subfleet: an up that waits; the notice once it waited NOT_UP_AFTER awake seconds."""
    first = f.r.hget(WAITS, name)
    if first is None:
        f.r.hset(WAITS, mapping={name: awake.now()})
        return
    if awake.since(float(first)) >= NOT_UP_AFTER:
        not_up(f, name, rid, why)


def up_done(f, name):
    f.r.hdel(WAITS, name)
    if f.r.hexists(NOTICES, 'up:' + name):
        recovered(f, 'up:' + name, '[sub-fleet recovered] ' + name + ': up')


def child_peek(d):
    """{ready, paused, held} of a sub-fleet's supervisor, read from its Redis; None when it cannot be read."""
    from fleetmux import subfleet
    fleet = _fleet()
    try:
        cfg = fleet.load(Path(d) / 'config.json')
        if not subfleet.redis_up(cfg):
            return None
        r = fleet.connect(cfg)
    except Exception:  # noqa: BLE001
        return None
    try:
        sup = r.hgetall(fleet.key('agent', 'supervisor'))
        svc = r.hgetall('fx:service')
    except Exception:  # noqa: BLE001
        return None
    finally:
        r.close()
        r.connection_pool.disconnect()
    return {'ready': sup.get('runtime_ready') == '1' and sup.get('state') in ('idle', 'busy'),
            'paused': svc.get('paused') == '1', 'held': sup.get('state') in ('held', 'dead')}


def watch_children(f):
    """A sub-fleet told about (``sub:<task>``) that moves again: its supervisor READY, not paused -> recovered."""
    from fleetmux import subfleet
    reopening = f.r.hkeys(REOPEN)
    for target in [t for t in f.r.hkeys(NOTICES) if t.startswith('sub:')]:
        task = target[4:]
        if task in reopening:
            continue
        d = find_child(f, task)
        if d is None:
            f.r.hdel(NOTICES, target)
            continue
        if subfleet.record(d).get('state') != 'running':
            continue
        peek = child_peek(d)
        if peek and peek['ready'] and not peek['paused']:
            recovered(f, target, '[sub-fleet recovered] ' + task + ': its supervisor is READY and it is not paused')
