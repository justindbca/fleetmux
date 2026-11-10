"""Codex deliveries through `codex queue`: no paste, no Enter, and an exact turn binding.

The running Codex TUI takes queued messages from its own queue, one turn per item in FIFO
order, also while it is busy (a queued item is never merged into a running turn). The rollout
records each such turn with turn_attribution.turn_trigger "queue" and its user message text.

Binding uses a delivery token that the daemon creates and persists *before* the CLI runs: the
queued text ends with a line `(fleet delivery <token>)`, and the record (cq_*) on the agent hash
holds that exact text. A turn settles the delivery only if the rollout shows a queue-triggered
turn whose first user message is exactly that text; any later input must be the registered
mid-turn handoff prompt for this generation. Nothing else is used: not the CLI's queued id
(it never reaches the rollout), not Codex's private queue table (neither read nor written), not
the cumulative notify `input-messages`, not timing. Queueing or starting a turn is never
completion: only the turn end (notify / rollout task_complete) settles. Any failure leaves the
slot to the existing fail-closed paths; nothing falls back to typing and nothing is retried
automatically (a timed-out CLI call is 'uncertain', held for a human).

The channel needs a codex-cli that records turn_attribution (MIN_VERSION). `start` probes the binary once
(capability) and pins the answer in config.json (codex_queue); on an older Codex every slot is typed for the
whole run (audited queue.unsupported at each spawn), never queued into turns that could not be bound.
"""
import datetime
import json
import os
import re
import subprocess
import time
import uuid

FIELDS = ('cq_delivery', 'cq_token', 'cq_text', 'cq_thread', 'cq_generation', 'cq_queued_at', 'cq_state', 'cq_queued_id',
          'cq_turn')
# States: queuing -> queued (CLI confirmed) | failed (CLI said no) | uncertain (no answer) ; -> bound (turn proven);
# abandoned (cleared on resume / rotation / retire / finish: a later turn carrying its token never settles). Abandoned
# is not cancelled: Codex has no official withdraw, so an item still queued in a live TUI runs. Hence deliver refuses
# to queue behind an unsettled item, and --resume kills the old session before it decides (token_turn) on a replay.
QUEUED = re.compile(r'Queued message (\S+) for thread (\S+?)\.?\s*$', re.M)
MARK = '(fleet delivery {})'
# The first codex-cli measured to record `task_started.turn_attribution.turn_trigger` ("queue" for a queued
# turn). codex-cli 0.161.0 has `codex queue` but writes no turn_attribution at all, so bound() can never
# prove a queued turn there: such a fleet is probed at start and its Codex slots stay on typed delivery.
MIN_VERSION = (0, 162, 0)
VERSION = re.compile(r'codex-cli (\d+)\.(\d+)\.(\d+)')


class QueueError(RuntimeError):
    pass


def _fleet():
    from cmux_fleet import fleet
    return fleet


def is_codex(cfg, name):
    """An official Codex slot that takes deliveries through `codex queue`. False on a fleet whose codex
    binary was probed too old at start (cfg codex_queue false): that slot is typed, as before the queue channel."""
    return _fleet().agent_cli(cfg, name) == 'codex' and cfg.get('codex_queue', True) is not False


def capability(codex, *, run=subprocess.run, timeout=15):
    """(supported, version, why) of the codex binary for the queue channel, from `codex --version`.

    Supported only from MIN_VERSION: the binding needs the rollout's turn_attribution. Anything
    unreadable (no binary, error, timeout, unknown output) is unsupported: typed delivery, never a
    queue whose turns can never be bound."""
    if not codex:
        return False, '', 'no codex binary'
    try:
        p = run([codex, '--version'], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, '', 'codex --version failed: ' + str(exc)[:200]
    out = (p.stdout or '').strip()
    m = VERSION.search(out)
    if p.returncode != 0 or not m:
        return False, out[:80], 'unrecognised codex --version output (rc ' + str(p.returncode) + '): ' + out[:80]
    version = '.'.join(m.groups())
    if tuple(int(x) for x in m.groups()) < MIN_VERSION:
        return False, version, ('codex-cli ' + version + ' records no turn_attribution in its rollout, so a queued '
                                'turn cannot be bound (needs >= ' + '.'.join(map(str, MIN_VERSION)) + ')')
    return True, version, ''


def new_token():
    return 'fx-' + uuid.uuid4().hex[:16]


def marked(text, token):
    """The exact text that is queued: the business text plus the delivery token as its last line."""
    return text.rstrip('\n') + '\n\n' + MARK.format(token)


def queue_argv(cfg, thread, text):
    # One `--message=<text>` token: a body starting with `-` must never be parsed as a flag.
    return [cfg.get('codex') or 'codex', 'queue', '--thread', thread, '--message=' + text]


def enqueue(cfg, name, thread, text, *, run=subprocess.run, timeout=30):
    """Queue `text` (already marked) for the session `thread`; {'queued_id', 'thread'}.

    QueueError('failed: ...') when the CLI answered no (rc != 0), QueueError('uncertain: ...') when there
    was no usable answer (timeout, OSError, rc 0 without the queued line, or another thread): the item may
    or may not be queued. The caller never retries either automatically."""
    fleet = _fleet()
    if not thread or not re.fullmatch(fleet.THREAD_RE, thread):
        raise QueueError('failed: no bound Codex session to queue into')
    env = dict(os.environ, CODEX_HOME=str(fleet.codex_home(cfg)))
    try:
        p = run(queue_argv(cfg, thread, text), env=env, cwd=str(fleet.agent_cwd(cfg, name)),
                capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise QueueError('uncertain: codex queue gave no answer: ' + str(exc)[:300])
    m = QUEUED.search(p.stdout or '')
    if p.returncode != 0:
        raise QueueError('failed: codex queue rc=' + str(p.returncode) + ': '
                         + ((p.stderr or '') + (p.stdout or '')).strip()[-300:])
    if not m:  # rc 0 but no confirmation: the item may be queued, so its outcome is unknown, not refused
        raise QueueError('uncertain: codex queue rc=0 without a queued-message line: '
                         + ((p.stderr or '') + (p.stdout or '')).strip()[-300:])
    if m.group(2) != thread:
        raise QueueError('uncertain: codex queue answered for another thread: ' + m.group(2))
    return {'queued_id': m.group(1), 'thread': thread}


def _ms(stamp):
    try:
        return int(datetime.datetime.fromisoformat(str(stamp).replace('Z', '+00:00')).timestamp() * 1000)
    except ValueError:
        return 0


def turn_record(thread, turn, sessions):
    """What the rollout says about one turn: {'trigger', 'started_ms', 'attributed', 'users': [{'text'}]}."""
    fleet = _fleet()
    for path in fleet.rollouts(thread, sessions):
        try:
            if fleet._is_subagent(fleet._meta(path)):
                continue
            lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
        except (OSError, ValueError):
            continue
        found, users = None, []
        for line in lines:
            if turn not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            payload = row.get('payload') or {}
            if row.get('type') != 'event_msg':
                continue
            if payload.get('type') == 'task_started':
                attribution = payload.get('turn_attribution') or {}
                if (attribution.get('turn_id') or payload.get('turn_id')) == turn:
                    found = {'trigger': attribution.get('turn_trigger') or '', 'started_ms': _ms(row.get('timestamp')),
                             'attributed': 'turn_attribution' in payload}
            elif payload.get('type') == 'item_completed' and payload.get('turn_id') == turn:
                item = payload.get('item') or {}
                if item.get('type') == 'UserMessage':
                    users.append({'text': '\n'.join(
                        c.get('text', '') for c in item.get('content') or []
                        if isinstance(c, dict) and c.get('type') == 'text')})
        if found:
            return dict(found, users=users)
    return None


def token_turn(thread, token, sessions):
    """The turn whose one user message carries `token` in the rollout of `thread`: {'turn', 'complete'}
    (complete: task_complete seen; an aborted or unfinished turn is not complete), or None if no turn
    carrying it has started."""
    fleet = _fleet()
    mark = MARK.format(token)
    for path in fleet.rollouts(thread, sessions):
        try:
            if fleet._is_subagent(fleet._meta(path)):
                continue
            lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
        except (OSError, ValueError):
            continue
        turn = None
        for line in lines:
            if turn is None and mark not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            payload = row.get('payload') or {}
            if row.get('type') != 'event_msg':
                continue
            if turn is None:
                item = payload.get('item') or {}
                if payload.get('type') == 'item_completed' and item.get('type') == 'UserMessage' and any(
                        isinstance(c, dict) and mark in c.get('text', '') for c in item.get('content') or []):
                    turn = payload.get('turn_id') or ''
            elif payload.get('type') == 'task_complete' and payload.get('turn_id') == turn:
                return {'turn': turn, 'complete': True}
        if turn is not None:
            return {'turn': turn, 'complete': False}
    return None


def record(info):
    return {k: info.get(k, '') for k in FIELDS + ('midturn_id', 'midturn_prompt', 'midturn_typed_g')}


def bound(rec, thread, turn, sessions, generation=None):
    """(True, '') only if the rollout proves `turn` was started by the delivery in `rec`: same thread,
    same generation (when known), a queue-triggered turn starting with this token's text;
    later messages may only repeat this generation's registered mid-turn handoff prompt."""
    if not rec.get('cq_delivery') or not rec.get('cq_token') or rec.get('cq_state') in ('', 'abandoned'):
        return False, 'no live queued delivery (state ' + (rec.get('cq_state') or 'none') + ')'
    if thread != rec.get('cq_thread'):
        return False, 'turn belongs to another thread'
    if generation is not None and str(generation) != rec.get('cq_generation'):
        return False, ('delivery was queued for generation ' + (rec.get('cq_generation') or 'none')
                       + ', not ' + str(generation))
    if rec.get('cq_turn') and rec['cq_turn'] != turn:
        return False, 'delivery already bound to turn ' + rec['cq_turn']
    if not turn:
        return False, 'turn end carries no turn id'
    facts = turn_record(thread, turn, sessions)
    if not facts:
        return False, 'turn not found in the rollout'
    if not facts.get('attributed', True):  # codex-cli < MIN_VERSION: nothing proves the queue started it (start probes this)
        return False, 'rollout records no turn_attribution (codex-cli too old for the queue channel)'
    if facts['trigger'] != 'queue':
        return False, 'turn was not started by the queue (trigger ' + (facts['trigger'] or 'none') + ')'
    from cmux_fleet.midturn import registered_prompt
    prompt = registered_prompt(rec, rec.get('cq_generation'))
    if not facts['users'] or any(not prompt or item['text'] != prompt for item in facts['users'][1:]):
        return False, 'turn has unregistered extra user messages'
    if facts['users'][0]['text'] != rec.get('cq_text') or MARK.format(rec['cq_token']) not in facts['users'][0]['text']:
        return False, 'turn input is not this delivery (token ' + rec['cq_token'] + ')'
    return True, ''


_CAS = ("local s = redis.call('HGET', KEYS[1], 'cq_state') "
        "if s == ARGV[1] then redis.call('HSET', KEYS[1], unpack(ARGV, 2)) return 1 end return 0")
_BIND = ("local s = redis.call('HGET', KEYS[1], 'cq_state') "
         "if s and s ~= '' and s ~= 'abandoned' and redis.call('HGET', KEYS[1], 'cq_token') == ARGV[1] then "
         "redis.call('HSET', KEYS[1], 'cq_state', 'bound', 'cq_turn', ARGV[2]) return 1 end return 0")


def _set_if(r, key, expect, mapping):
    """Atomic compare-and-set on cq_state (a Lua script on Redis; the v1 test double is single-threaded)."""
    if hasattr(r, 'eval'):
        return bool(r.eval(_CAS, 1, key, expect, *[x for kv in mapping.items() for x in kv]))
    if r.hget(key, 'cq_state') == expect:
        r.hset(key, mapping=mapping)
        return True
    return False


def _real(r):
    """The real Redis client behind runtime_v2's lazy Effects overlay: the record must be durable
    before the CLI runs, not deferred to the end of a transaction."""
    return getattr(r, 'source', r)


def deliver(f, slot, delivery, text, thread=None):
    """Queue `text` for the slot's bound session (or `thread`, e.g. an adopted pane's session not yet
    bound to the new slot). The record and token are durable before the CLI runs. Raises QueueError
    ('failed' / 'uncertain' recorded) on any failure; the caller holds, never retries or types."""
    fleet = _fleet()
    r = _real(f.r)
    info = r.hgetall(fleet.key('agent', slot))
    thread = thread or info.get('thread') or ''
    pane = info.get('pane') or ''
    if not pane or f.mgr.get_pane_status(pane) is None:
        # A queued item outlives its TUI and runs whenever the session is resumed later; never queue blind.
        f.log('queue.failed', agent=slot, delivery=delivery, error='runtime pane is gone')
        raise QueueError('failed: runtime pane is gone; not queued')
    prev = record(info)
    if prev['cq_state'] in ('queuing', 'queued', 'uncertain'):
        # Abandoning only stops settlement: the older item may still sit in this live TUI's queue and run, and
        # Codex offers no official way to withdraw it. Never queue a second item behind it.
        f.log('queue.refused', agent=slot, delivery=delivery, unsettled=prev['cq_delivery'], token=prev['cq_token'])
        raise QueueError('failed: delivery ' + prev['cq_delivery'] + ' is still unsettled (' + prev['cq_state']
                         + ') in this session; not queued behind it')
    token = new_token()
    queued = marked(text, token)
    r.hset(fleet.key('agent', slot), mapping={'cq_delivery': delivery, 'cq_token': token, 'cq_text': queued,
                                              'cq_thread': thread, 'cq_generation': str(info.get('generation', '')),
                                              'cq_queued_at': str(int(time.time() * 1000)),
                                              'cq_state': 'queuing', 'cq_queued_id': '', 'cq_turn': ''})
    try:
        got = enqueue(f.runtime_cfg(slot), slot, thread, queued)
    except QueueError as exc:
        state = 'uncertain' if str(exc).startswith('uncertain') else 'failed'
        # Only from 'queuing': a fast turn end may have bound (and settled) it, or clear abandoned it, meanwhile.
        if not _set_if(r, fleet.key('agent', slot), 'queuing', {'cq_state': state}) \
                and r.hget(fleet.key('agent', slot), 'cq_state') == 'bound':
            # The rollout already proved the item ran: a late CLI error must not hold a settled delivery.
            f.log('queue.late_cli_error', agent=slot, delivery=delivery, token=token, error=str(exc)[:300])
            return {'queued_id': '', 'thread': thread, 'token': token, 'text': queued}
        f.log('queue.' + state, agent=slot, delivery=delivery, token=token, error=str(exc)[:300])
        raise
    # Compare-and-set: a fast turn end may already have bound it; queued is not completion either way.
    _set_if(r, fleet.key('agent', slot), 'queuing', {'cq_state': 'queued', 'cq_queued_id': got['queued_id']})
    f.log('queue.delivered', agent=slot, delivery=delivery, token=token, queued_id=got['queued_id'], thread=thread)
    return dict(got, token=token, text=queued)


def clear(f, slot, reason):
    """Abandon the slot's unsettled delivery: a turn that later carries its token never settles.

    Codex's private queue is not modified: an item that has not run yet stays queued in its own
    session and may still run there (only if that session is resumed). Returns the abandoned record."""
    fleet = _fleet()
    r = _real(f.r)
    rec = record(r.hgetall(fleet.key('agent', slot)))
    if rec['cq_state'] not in ('queuing', 'queued', 'uncertain', 'failed'):
        return {}
    r.hset(fleet.key('agent', slot), mapping={'cq_state': 'abandoned'})
    f.log('queue.abandoned', agent=slot, delivery=rec['cq_delivery'], token=rec['cq_token'],
          was=rec['cq_state'], reason=reason)
    return rec


def unsettled(info):
    """True when the slot has a delivery that may still be queued in Codex or running (not bound)."""
    return record(info)['cq_state'] in ('queuing', 'queued', 'uncertain')


def mark_bound(r, slot, turn, token):
    """Bind atomically, only while the record is still the live delivery with `token` (never revives an
    abandoned or superseded record). False: nothing was bound."""
    r, key = _real(r), _fleet().key('agent', slot)
    if hasattr(r, 'eval'):
        return bool(r.eval(_BIND, 1, key, token, turn))
    if r.hget(key, 'cq_state') not in (None, '', 'abandoned') and r.hget(key, 'cq_token') == token:
        r.hset(key, mapping={'cq_state': 'bound', 'cq_turn': turn})
        return True
    return False


def new_delivery_id(prefix):
    return prefix + '-' + uuid.uuid4().hex[:12]
