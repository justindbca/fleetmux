"""Transport integration for the existing Fleet dispatcher, not another scheduler.

Daemon effects are first evaluated against a lazy Redis overlay and saved as an
immutable intent. A preflighted Redis script applies the effects, public/UI results,
PEL completion and event ACK together. Prepared intents are replayable. An unexpected
script failure with an 'applying' intent is ambiguous and holds the run for inspection;
Lua does not roll back failed writes. No model/tool side effect is ever replayed.
"""
import copy
import hashlib
import json
import re
import time
import uuid
from pathlib import Path

import redis

from fleetmux import advisor, claude_hook, clear_after_result, cli_pairs, contracts, tabs
from fleetmux import dsh_adapter, fleet_config, opencode_adapter, pi_adapter, session_env
from fleetmux.message_bus import BusError, MessageBus, LUA_REBIND_CHECK, _json, _short, _request_id
from fleetmux.presentation import mask_secrets, result_card, routing_card, sanitize_text
from fleetmux.i18n import t, tr
from fleetmux import applet_lease, codex_queue, lifecycle_close, named_subfleet, subfleet, tui_cards
from fleetmux import awake, midturn, watchdog
from fleetmux import held_requeue, pause_review


def v2_cli_problem(cfg):
    """None when this supervisor/worker CLI pair and mode may run transport v2.

    Decided by the pair table config/fleet.yaml#cli_pairs (see cli_pairs.pair_problem); a pair
    it refuses is never silently replaced by another CLI/model or the old file mailbox."""
    return cli_pairs.pair_problem(cfg)


NO_TYPING = ('fleet never types into a Claude Code input box (user 2026-11-09: '
             '「任何场景都不用 claude 输入框打字」, docs/design.md §48)')


def typed_claude_problem(cfg):
    """Why this config would type text into a Claude Code input box, or None (design §48).

    Every Claude agent takes its deliveries through its Stop-hook mailbox and its first prompt as a CLI
    argument. The only switches that still needed the input box are refused here, at start, resume, daemon
    and actor start alike: the worker typed channel, §28 /clear after each result, a Claude supervisor
    without a hook mailbox (a run started before it was pinned, or v1), and an adopted Claude pane the
    parent did not mark adopt.hook."""
    if cfg.get('clear_after_result') is True:
        return ('--clear-after-result (clear_after_result: true) is retired: it typed /clear into the Claude '
                'input box after every result; ' + NO_TYPING + '. For a clean context per job, dispatch with '
                '"fresh": true (§37)')
    if cfg.get('claude_hook_delivery') is False:
        return ('--no-claude-hook-delivery (claude_hook_delivery: false) is retired: Claude agents take every '
                'delivery through the Stop-hook mailbox; ' + NO_TYPING)
    if cfg.get('supervisor_cli') != 'claude':
        return None
    if cfg.get('adopt'):
        adopt = cfg['adopt'] if isinstance(cfg['adopt'], dict) else {}
        if cfg.get('transport_version') != 2 or adopt.get('hook') is not True:
            return ('the adopted Claude supervisor pane has no hook mailbox (adopt.hook not set by the parent); '
                    + NO_TYPING + ': hand over a pane of a run started with adopted_supervisor_hook_delivery')
        return None
    if cfg.get('transport_version') != 2:
        return 'a Claude supervisor needs transport v2 (its Stop-hook mailbox); ' + NO_TYPING
    # start validates its argv before it pins this marker into the run config; a saved run has 'run'.
    if cfg.get('run') and cfg.get('claude_supervisor_hook_delivery') is not True:
        return ('this run\'s Claude supervisor was started before hook delivery was pinned '
                '(claude_supervisor_hook_delivery); ' + NO_TYPING + ': start a new run')
    return None


def validate_config(cfg):
    problem = typed_claude_problem(cfg)
    if problem:
        raise ValueError(problem)
    clear_after_result.validate(cfg)
    for name in ('approve_confirm_s', 'approve_ttl_s'):  # contracts §8 时限: positive seconds (P6c)
        if name in cfg:
            value = cfg[name]
            if type(value) not in (int, float) or value != value or value in (float('inf'), float('-inf')) \
                    or value <= 0:
                raise ValueError(name + ' must be a positive number of seconds')
    midturn.validate_settings(cfg)
    if 'split_judge' in cfg and cfg['split_judge'] not in ('worker', 'off'):  # P7: who judges a split (contracts §8)
        raise ValueError('split_judge must be worker or off')
    if 'supervisor_split' in cfg and type(cfg['supervisor_split']) is not bool:  # §44 监工直接开子 fleet
        raise ValueError('supervisor_split must be true or false')
    if cfg.get('lease_reclaim') not in (None, 'on', 'off'):  # §24 (contracts §4); absent / start argv None = on
        raise ValueError('lease_reclaim must be on or off')
    version = cfg.get('transport_version', 1)
    if type(version) is not int or version not in (1, 2):
        raise ValueError('transport_version must be 1 or 2')
    if cfg.get('supervisor_cli') == 'opencode':
        raise ValueError('opencode runs only as a worker, never as the supervisor')
    if cfg.get('worker_cli') == 'opencode' and version != 2:
        raise ValueError('opencode workers run only on transport v2 (in-process plugin + agent_bridge.py)')
    if cfg.get('supervisor_cli') == 'dsh':
        raise ValueError('dsh runs only as a worker, never as the supervisor')
    if cfg.get('worker_cli') == 'dsh' and version != 2:
        raise ValueError('dsh workers run only on transport v2 (pane front-end + agent_bridge.py)')
    if version == 2:
        problem = v2_cli_problem(cfg)
        if problem:
            raise ValueError(problem)
        if cfg.get('allow_development_v2') is not True:
            raise ValueError('transport v2 requires --allow-development-v2; real SDK/TUI acceptance is pending')
        for name in watchdog.DEFAULTS:
            watchdog.setting(cfg, name)
        if watchdog.setting(cfg, 'dialog_escalation_timeout') < 60:
            raise ValueError('dialog_escalation_timeout must be at least 60 seconds')
        if cfg.get('prompt_table'):
            try:
                watchdog.parse_table(fleet_config.block('watchdog_prompts', cfg['prompt_table']))
            except OSError as exc:
                raise ValueError('cannot read --prompt-table: ' + str(exc))
    return version


def tui_slot(cfg, slot):
    """Official interactive CLIs (Codex/Claude) have no in-process bridge: the daemon
    receives their delivery and hands the business text to the TUI (typed for Codex and a
    Claude supervisor; through the Stop-hook mailbox for Claude workers, see claude_hook);
    the CLI's own turn-end hook (Codex notify / Claude Stop) settles it. The resident advisor is always
    official Claude (docs/design.md §27)."""
    if slot == advisor.SLOT:
        return cfg.get('transport_version') == 2
    cli = cfg.get('supervisor_cli' if slot == 'supervisor' else 'worker_cli') or 'codex'
    if cli not in (cli_pairs.CLIS if slot == 'supervisor' else cli_pairs.WORKER_CLIS):
        raise ValueError('unknown CLI ' + repr(cli) + ' for ' + str(slot) + '; refusing to treat it as an official TUI')
    # pi, opencode and dsh run fleet's bridge (agent_bridge.py); only Codex / Claude are official TUIs.
    return cfg.get('transport_version') == 2 and cli in ('codex', 'claude')


def bridge_event(cfg, slot, generation, ev):
    """Validate a bridge session binding with the slot's own CLI adapter (pi, opencode or dsh)."""
    cli = cfg.get('supervisor_cli' if slot == 'supervisor' else 'worker_cli') or 'codex'
    if cli == 'opencode':
        return opencode_adapter.event(cfg, slot, generation, dict(ev, kind=opencode_adapter.KIND))
    if cli == 'dsh':
        return dsh_adapter.event(cfg, slot, generation, dict(ev, kind=dsh_adapter.KIND))
    if cli == 'pi':
        return pi_adapter.event(cfg, slot, generation, dict(ev, kind='pi_session'))
    raise ValueError('bridge session from a ' + repr(cli) + ' slot: only pi / opencode / dsh slots run the bridge')


# The reply channel named in business text, worded at queue time: an official CLI answers as AGENTS.md says, a
# bridge (pi / opencode / dsh) through its fleet_decision / fleet_result tool. A wall switch to another CLI rewords
# the slot's queued messages (RuntimeV2.reword_pending).
CHANNEL_WORDING = (('Reply with the service decision JSON from AGENTS.md.', 'Use fleet_decision to route this request.'),
                   ('Reply with the JSON format from AGENTS.md.', 'Use fleet_decision when you have decided.'),
                   ('Continue the current task using the AGENTS.md decision JSON.',
                    'Continue the current task using fleet_decision.'))
REQUEST_TUI = ('\nYour final reply must be exactly one JSON object with the keys: ', '. No text outside the JSON object.')
REQUEST_TOOL = ('\nUse fleet_result with a business JSON object with keys: ', '.')  # Fleet.request_message


def channel_wording(text, bridge):
    """``text`` worded for a bridge slot (``bridge``) or an official-CLI slot."""
    for tui, tool in CHANNEL_WORDING:
        text = text.replace(tui, tool) if bridge else text.replace(tool, tui)
    old, new = (REQUEST_TUI, REQUEST_TOOL) if bridge else (REQUEST_TOOL, REQUEST_TUI)
    at = text.rfind(old[0])
    if at >= 0 and text.endswith(old[1]):
        keys = text[at + len(old[0]):len(text) - len(old[1])]
        if '\n' not in keys:
            text = text[:at] + new[0] + keys + new[1]
    return text


# opencode provider-side refusals (adapter error_class): reported each by name, committed, dispatch paused;
# never a task failure, never re-sent, never another model. model_lock is a failure (opencode_refused).
OPENCODE_REFUSALS = ('free_tier_rejected', 'rate_limited', 'model_unavailable')
OPENCODE_REFUSALS_TEXT = {'free_tier_rejected': '供应方拒绝（FreeTierError，免费档不受理）',
                          'rate_limited': '供应方限流（429）', 'model_unavailable': '模型下线 / 不可用'}  # results
OPENCODE_REFUSALS_CARD = {  # the same on the card, in the interface language (ui.language, docs/design.md §63)
    'free_tier_rejected': ('oc.refusal.free_tier', 'provider refused (FreeTierError, free tier not accepted)'),
    'rate_limited': ('oc.refusal.rate_limited', 'provider rate limit (429)'),
    'model_unavailable': ('oc.refusal.model_unavailable', 'model retired / unavailable'),
    'model_lock': ('oc.refusal.model_lock', 'model lock failed')}


SERVICE_EXHAUSTED = ('stopfail.service_exhausted', '{label} {what} {streak} times in a row (limit {cap}, last: '
                     '{short}); the slot is held and gets no new work, the others go on. Once fixed, end one normal '
                     'turn in that pane to release it.')


def _both(key, en, /, **kw):
    """(card text in the interface language, the zh-Hans text unchanged for notices that leave the terminal)."""
    return t(key, en, **kw), tr('zh-Hans', key, en, **kw)


def startup_problem(ev):
    """One line for an opencode startup that did not settle with READY (failed / aborted / other text)."""
    error = str(ev.get('error') or '').strip()
    if error:
        return (str(ev.get('error_class') or opencode_adapter.classify_error(error)) + ': ' + error)[:500]
    if ev.get('aborted'):
        return 'aborted'
    return 'reply was not READY: ' + repr(str(ev.get('text') or '')[:200])


TUI_REPORT_RETRIES = 2  # corrections for a task worker's malformed report (any CLI) before blocking the task
# Task-mode Claude hook worker whose turn ended with StopFailure (API error, §24): seconds before attempt k+1
# of the same instruction (index k-1; the last value repeats). The attempts themselves are capped by max_attempts.
STOP_FAILURE_BACKOFF = (60, 120, 300, 600)
SERVICE_STOPFAIL_MAX = 3  # service mode: consecutive StopFailures of one slot before it stays held (config service_stopfail_max)
# A service request's messages carry no bus deadline (docs/design.md §43): the request's own deadline (timeout_s /
# --request-timeout, none by default) is enforced by expire() alone, so its expiry holds only the executing slot and
# never races the lease / settlement / renewal checks into a paused fleet. 2100-01-01 UTC in ms: 13 digits, so it
# round-trips through Redis Lua numbers (%.14g) as an integer.
NO_DEADLINE_MS = 4102444800000
STOP_FAILURE_RESUME = ('[Retried after an API error] Your previous turn on this instruction ended with an API error '
                       'before it reported. Files you already wrote stay. Check your directory first and continue '
                       'from where it stopped; do not redo finished steps.\n\n')

STOP_FAILURE_RESUME_SUPERVISOR = ('[Retried after an API error] Your previous turn on this message ended with an API '
                                  'error before your decision was read; nothing from it was applied. Check the run '
                                  'state, then answer this message again with the JSON format from AGENTS.md.\n\n')
# Held watchdog backstop (§24): fleet.yaml#daemon_watch keys and their fallbacks when the file lacks them.
HELD_WATCH = {'held_after_s': 300, 'held_realert_s': 1800,
              'subfleet_stuck_after_s': 600, 'subfleet_check_s': 60}  # R3b: the parent watches its sub-fleets
# Supervisor regeneration (§24): a held supervisor that does not recover gets its next generation from the daemon.
# fleet.yaml#daemon_watch keys, each validated on its own (a bad key keeps its default).
REGEN_WATCH = {'supervisor_regen': True, 'supervisor_regen_max': 3, 'supervisor_regen_window_s': 3600,
               'supervisor_regen_backoff_s': (60, 300, 900), 'supervisor_regen_probe': 'auto'}
REGEN_PROBES = ('auto', 'tcp', 'off')
REGEN_NOTE = ('[Previous generation did not finish] {prev} was handling this message when it stopped ({why}); the '
              'daemon ended that session and started {label}. Nothing from its turn was applied. Check the current '
              'state before acting on it again.\n\n')
REGEN_RESUME_SUPERVISOR = ('[Previous generation did not finish] The previous supervisor generation ended its turn on '
                           'this message with an API error and its retries were used up; nothing from it was applied '
                           'and the daemon started a new generation. Check the run state, then answer this message '
                           'again with the JSON format from AGENTS.md.\n\n')

# Bridge faults that only echo a hold the daemon (or the delivery itself) already decided: not a lost link.
# Any other bridge fault (its own Redis error, child crash, lease not renewed) means the link is gone and
# the slot is restarted (§35); the pane says which of the two it is.
BRIDGE_HELD_REASONS = ('daemon holds execution', 'settlement unconfirmed/held', 'recovered PEL may have executed',
                       'delivery collided with manual input', 'opencode hold', 'dsh hold')
LINK_LOST = 'bridge link lost: '


def link_fault(reason):
    return not str(reason or '').startswith(BRIDGE_HELD_REASONS)


# A startup turn that failed on the network (the machine just woke from sleep, Wi-Fi not back yet): the CLI never
# reached its provider. Retried as the next generation with backoff (Fleet.startup_retry_tick), never a blocked run.
OFFLINE_ERROR = re.compile(r'fetch failed|ECONNRESET|ECONNREFUSED|ENOTFOUND|EAI_AGAIN|ETIMEDOUT|ENETUNREACH|'
                           r'EHOSTUNREACH|ENETDOWN|socket hang up|other side closed|UND_ERR_|getaddrinfo|'
                           r'network (is )?(unreachable|error|down)|connection (error|reset|refused)', re.I)


# A startup turn that failed on the login (user 2026-11-27; 11-26 pi: "OAuth refresh failed for openai: ...
# invalid_grant"): after a wall switch the slot takes the next model on the chain (Fleet.startup_unusable).
AUTH_ERROR = re.compile(r'OAuth refresh failed|invalid_grant|Not logged in|No API key|credentials_not_configured|'
                        r'401 Unauthorized|authentication (failed|error)', re.I)


# A supervisor turn that ended on an API / network error (§24 supervisor regeneration): OFFLINE_ERROR plus pi's own
# give-up line and Claude's connection errors. Separate from OFFLINE_ERROR, which startup_offline also uses.
API_NETWORK_ERROR = re.compile(OFFLINE_ERROR.pattern + r'|Retry failed after \d+ attempts?|Connection lost mid-response'
                               r'|API Error: Connection', re.I)


def startup_offline(ev):
    """A failed startup whose error is a transport error (no model reply at all), not a refusal or a wrong reply."""
    return bool(ev.get('failed')) and not ev.get('aborted') and bool(OFFLINE_ERROR.search(str(ev.get('error') or '')))


def heartbeat_key(bus, slot):
    """Set by the bridge's renew loop (value = its generation, short TTL); gone = the bridge is gone."""
    return bus.prefix + ':hb:' + slot


def clock(t):
    return time.strftime('%H:%M:%S', time.localtime(float(t))) if t else 'never'


# Audit rows bridges and hooks keep writing while no daemon runs: not the daemon's last word (§36).
AGENT_AUDITS = ('renewed', 'received', 'slot_', 'turn.', 'hook.')


def daemon_outage(r, bus, cfg, now=None):
    """§36: what the logs say about a daemon outage, read straight from Redis (no daemon needed): its last beat,
    the last audit record it wrote (at most ~2 s after that beat, agent-side rows skipped), the events it never
    took (its consumer group's PEL plus everything after last-delivered-id) and the deliveries left held."""
    now = time.time() if now is None else now
    beat = float(r.get('fx:daemon:beat') or 0)
    last, last_at = '', 0.0
    for sid, row in r.xrevrange(bus.audit_key, max=str(int(beat * 1000) + 2000) if beat else '+', count=200):
        if not str(row.get('event', '')).startswith(AGENT_AUDITS):
            last, last_at = row.get('event', ''), int(row.get('at') or sid.split('-')[0]) / 1000
            break
    events, pending = bus.prefix + ':events', 0
    for group in r.xinfo_groups(events) if r.exists(events) else []:
        if group['name'] == 'daemon':
            pending = int(group['pending']) + len(r.xrange(events, '(' + group['last-delivered-id'], '+'))
    held = [s for s in ['supervisor'] + list(cfg.get('workers') or [])
            if r.hget(bus.lease_key(s), 'status') in ('uncertain', 'expired')]
    return {'now': now, 'beat': beat, 'down_s': int(now - beat) if beat else None, 'last_event': last,
            'last_event_at': last_at, 'pending_events': pending, 'held': held}


IDENTITY = ('run_id', 'slot', 'generation', 'session_id', 'message_id', 'delivery_id',
            'stream_id', 'fence', 'consumer', 'request_id')


def _norm(text):
    return ' '.join(str(text).split())


def ready_reply(text):
    """Startup settles when the reply's last non-empty line is READY (markdown emphasis and a
    trailing period tolerated): models often put a sentence first. 'NOT READY' is not READY."""
    lines = [x.strip().strip('*_`').rstrip('.').strip() for x in str(text or '').splitlines() if x.strip()]
    return bool(lines) and lines[-1] == 'READY'


STARTUP_NUDGE = ('Your startup reply was not READY. Do no task work in this turn: '
                 'reply exactly READY (one word, nothing else).')

# The launch prompt of a resumed run that never submitted a task (A42-LINUX-RESTORE-FAILURE-04): same wording a
# fresh start uses for its held first workers, so the slot reaches READY and waits for the operator's task.
NO_TASK_PROMPT = ('You are fleet agent {label}. Read AGENTS.md in the current directory first. '
                  'There is no task yet: your final reply must be exactly READY.')


def turn_answers(body_text, inputs):
    """The turn must have been started by the typed delivery text (not a manual turn)."""
    head = _norm(body_text)[:120]
    return bool(head) and any(head in _norm(item) for item in inputs if isinstance(item, str))


# A heading may carry a parenthetical qualifier before its colon: "Evidence (`FLEETMUX_GATE=m3`):" (2026-10-28 ci-1).
WORKER_SECTIONS = re.compile(r'^[\s>*#_-]*(done|evidence|not done|blocked)[\s*_]*(?:\([^)\n]*\)[\s*_]*)?:[\s*_]*(.*)$', re.I)


def tui_candidates(slot, purpose, text, mode='task'):
    """Map an official CLI's final text to the structured candidate the daemon validates.

    The agent-facing reply formats are the original AGENTS.md ones (decision JSON for
    the supervisor, Done/Evidence/Not done/Blocked for task workers, the request's
    business JSON object for service workers). Unparseable text yields no candidate,
    so the daemon's existing invalid/blocked paths apply.
    """
    if purpose == 'handoff':
        return []
    if slot == advisor.SLOT:  # the raw answer: the waiting slot validates it as a configuration sheet (§27)
        return [{'tool': 'advisor_sheet', 'value': {'text': text}}]
    try:
        value = json.loads(text[text.index('{'):text.rindex('}') + 1])
    except ValueError:
        value = None
    if slot == 'supervisor':
        return [{'tool': 'fleet_decision', 'value': value}] if isinstance(value, dict) else []
    if mode == 'service':  # the reply schema of the request is checked by the daemon (parse_reply)
        return [{'tool': 'fleet_result', 'value': value}] if isinstance(value, dict) else []
    if isinstance(value, dict) and 'done' in value and 'evidence' in value:
        return [{'tool': 'fleet_result', 'value': value}]
    # Contract tasks: a trailing JSON object (interface_change) and the Interfaces / Peer notices /
    # Question lines become extra keys, only when present (other reports are unchanged).
    extra, rest = contracts.last_json_object(text)
    extra = {k: extra[k] for k in contracts.REPORT_KEYS if k in extra} if isinstance(extra, dict) else {}
    lines = (rest if extra else text).splitlines()
    sections, current = {}, None
    for line in lines:
        m = contracts.EXTRA_SECTIONS.match(line)
        if m:
            current = None
            extra[{'interfaces': 'interfaces', 'peer notices': 'peer_notices'}.get(m.group(1).lower(), 'question')] \
                = m.group(2).strip()
            continue
        m = WORKER_SECTIONS.match(line)
        if m:
            current = m.group(1).lower()
            sections[current] = [m.group(2).strip()]
        elif current:
            sections[current].append(line.strip())
    items = lambda k: [x.lstrip('-*• ').strip() for x in sections.get(k, []) if x.lstrip('-*• ').strip()]
    value = {'done': items('done'), 'evidence': items('evidence'),
             'not_done': '\n'.join(items('not done')), 'blocked': '\n'.join(items('blocked')), **extra}
    return [{'tool': 'fleet_result', 'value': value}] if value['done'] and value['evidence'] else []


def bus_for(cfg, r):
    return MessageBus(r, cfg.get('run_id') or Path(cfg['run']).name,
                      lease_ms=int(cfg.get('v2_lease_ms', 300000)))


def held_watch_knobs():
    """(knobs, problem): fleet.yaml#daemon_watch held_after_s / held_realert_s / subfleet_stuck_after_s /
    subfleet_check_s (seconds > 0) over ``HELD_WATCH``;
    a bad value -> the defaults and the problem text (audited as held_watch.config_rejected)."""
    try:
        block = fleet_config.block('daemon_watch') or {}
    except (OSError, ValueError, KeyError) as exc:
        return dict(HELD_WATCH), 'fleet.yaml#daemon_watch unreadable: ' + str(exc)[:200]
    knobs, bad = dict(HELD_WATCH), []
    for key in HELD_WATCH:
        if key in block:
            value = block[key]
            if type(value) in (int, float) and value > 0:
                knobs[key] = value
            else:
                bad.append(key)
    if bad:
        return dict(HELD_WATCH), 'fleet.yaml#daemon_watch: ' + ', '.join(bad) + ' must be seconds > 0'
    return knobs, None


def regen_knobs():
    """(knobs, problem): the supervisor regeneration keys of fleet.yaml#daemon_watch over ``REGEN_WATCH``, each
    validated on its own; a bad key keeps its default and is named in the problem text."""
    try:
        block = fleet_config.block('daemon_watch') or {}
    except (OSError, ValueError, KeyError) as exc:
        return dict(REGEN_WATCH), 'fleet.yaml#daemon_watch unreadable: ' + str(exc)[:200]
    number = lambda v: type(v) in (int, float) and v > 0
    checks = {'supervisor_regen': lambda v: type(v) is bool,
              'supervisor_regen_max': lambda v: type(v) is int and v >= 1,
              'supervisor_regen_window_s': number,
              'supervisor_regen_backoff_s': lambda v: number(v) or (
                  isinstance(v, list) and bool(v) and all(number(x) for x in v)),
              'supervisor_regen_probe': lambda v: v in REGEN_PROBES}
    knobs, bad = dict(REGEN_WATCH), []
    for key, ok in checks.items():
        if key in block:
            if ok(block[key]):
                knobs[key] = block[key]
            else:
                bad.append(key)
    backoff = knobs['supervisor_regen_backoff_s']
    knobs['supervisor_regen_backoff_s'] = tuple(backoff) if isinstance(backoff, (list, tuple)) else (backoff,)
    return knobs, ('fleet.yaml#daemon_watch: ' + ', '.join(bad) + ' invalid; their defaults apply') if bad else None


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def handoff_mark(info):
    """The slot's handoff-hold pause mark (RuntimeV2.handoff_pause_arm); {} when absent or unreadable."""
    try:
        mark = json.loads(info.get('handoff_pause') or '{}')
    except ValueError:
        return {}
    ok = isinstance(mark, dict) and mark.get('hid') and mark.get('at') and isinstance(mark.get('generation'), int)
    return mark if ok else {}


# Stable producer dedup is run-local, never expires while retries are possible.
LUA_EVENT = """
local old = redis.call('GET', KEYS[1])
if old then
  if old ~= ARGV[1] then error('event_conflict') end
  return 'exists'
end
local typ = redis.call('TYPE', KEYS[2]).ok
if typ ~= 'none' and typ ~= 'stream' then error('event_wrong_type') end
redis.call('XADD', KEYS[2], '*', 'event_id', ARGV[2], 'data', ARGV[1])
redis.call('SET', KEYS[1], ARGV[1])
return 'added'
"""


def post_event(bus, event, event_id=None):
    if not isinstance(event, dict) or not isinstance(event.get('kind'), str):
        raise ValueError('event requires an object with a kind')
    event_id = _short(event_id or 'e-' + uuid.uuid4().hex, 'event_id')
    raw = dumps(event)
    if len(raw.encode()) > 1000000:
        raise ValueError('event exceeds 1000000 bytes')
    bus.r.eval(LUA_EVENT, 2, bus.prefix + ':producer:' + event_id,
               bus.prefix + ':events', raw, event_id)
    return event_id


def wait_reply(bus, correlation, timeout=30, reader=None):
    _request_id(correlation)
    own = reader is None
    reader = reader if reader is not None else bus.blocking_client(1000)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = reader.xread({bus.prefix + ':reply:' + correlation: '0-0'}, count=1, block=1000)
            if rows:
                return json.loads(rows[0][1][0][1]['data'])
        return {'status': 'pending', 'request_id': correlation}
    finally:
        if own:
            reader.close()
            reader.connection_pool.disconnect()


class Effects:
    """Lazy, single-daemon read-your-writes overlay for Fleet's existing validators.

    Only scheduler-owned string/hash/list keys may be accessed here. The bridge
    writes events, lease renewal and UI cursors, not these scheduler keys. No real
    mutation occurs before the complete effect plan has been persisted.
    """
    def __init__(self, r):
        self.source, self.values, self.commands = r, {}, []

    def value(self, key, kind):
        if key not in self.values:
            actual = self.source.type(key)
            if actual not in ('none', kind):
                raise BusError('wrong_type:' + key)
            self.values[key] = (kind, {'hash': self.source.hgetall,
                'list': lambda k: self.source.lrange(k, 0, -1), 'string': self.source.get}[kind](key))
        if self.values[key][0] != kind:
            raise BusError('overlay wrong_type:' + key)
        return self.values[key][1]

    def command(self, *args):
        self.commands.append([str(a) for a in args])

    def get(self, key):
        return self.value(key, 'string')

    def set(self, key, value, ex=None, nx=False):
        if nx and self.get(key) is not None:
            return False
        self.values[key] = ('string', str(value))
        self.command('SET', key, value, *(['EX', ex] if ex else []))
        return True

    def hgetall(self, key):
        return dict(self.value(key, 'hash'))

    def hget(self, key, field):
        return self.value(key, 'hash').get(field)

    def hset(self, key, mapping):
        self.value(key, 'hash').update({k: str(v) for k, v in mapping.items()})
        self.command('HSET', key, *[x for k, v in mapping.items() for x in (k, v)])

    def hincrby(self, key, field, n):
        value = int(self.hget(key, field) or 0) + n
        self.hset(key, {field: value})
        return value

    def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
            # Subsequent reads of this key must see absence, not the source value.
            actual = self.source.type(key)
            if actual in ('string', 'hash', 'list'):
                self.values[key] = (actual, {'string': None, 'hash': {}, 'list': []}[actual])
        self.command('DEL', *keys)

    def rpush(self, key, value):
        self.value(key, 'list').append(str(value)); self.command('RPUSH', key, value)

    def lpush(self, key, value):
        self.value(key, 'list').insert(0, str(value)); self.command('LPUSH', key, value)

    def lrange(self, key, a, b):
        items = self.value(key, 'list')
        return items[a:] if b == -1 else items[a:b + 1]

    def llen(self, key):
        return len(self.value(key, 'list'))

    def lrem(self, key, count, value):
        if count != 0:
            raise ValueError('effect LREM only supports count=0')
        self.values[key] = ('list', [x for x in self.value(key, 'list') if x != value])
        self.command('LREM', key, count, value)

    def ltrim(self, key, a, b):
        self.values[key] = ('list', self.lrange(key, a, b)); self.command('LTRIM', key, a, b)

    def xadd(self, key, fields):
        self.command('XADD', key, '*', *[x for k, v in fields.items() for x in (k, v)])


# Preflight all expected key types before any effects. Applying marker is written
# separately; unanticipated Redis script failure leaves a visible ambiguous intent.
LUA_APPLY = LUA_REBIND_CHECK + """
if redis.call('HGET', KEYS[1], 'phase') == 'applied' then return 'already' end
if redis.call('HGET', KEYS[1], 'phase') ~= 'applying' then error('intent_not_applying') end
local plan = cjson.decode(ARGV[1])
for _, cmd in ipairs(plan.commands) do
  local want = ({HSET='hash', RPUSH='list', LPUSH='list', LREM='list',
    LTRIM='list', XADD='stream', XACK='stream', SET='string'})[cmd[1]]
  if cmd[1] ~= 'DEL' and not want then error('unsupported_effect') end
  if want then
    local got = redis.call('TYPE', cmd[2]).ok
    if got ~= 'none' and got ~= want then error('effect_wrong_type:' .. cmd[2]) end
  end
  if cmd[1] == 'XACK' then
    local found = false
    for _, group in ipairs(redis.call('XINFO', 'GROUPS', cmd[2])) do
      for i=1,#group,2 do
        if group[i] == 'name' and group[i+1] == cmd[3] then found = true end
      end
    end
    if not found then error('effect_missing_group') end
  end
end
-- The immutable authorization is rechecked at application, including on replay.
if type(plan.authorization) == 'table' then
  for key, fields in pairs(plan.authorization) do
    for field, expected in pairs(fields) do
      if redis.call('HGET', key, field) ~= expected then error('intent_identity:' .. field) end
    end
  end
end
if type(plan.commit_gate) == 'table' then
  local gate = plan.commit_gate
  local t = redis.call('TIME')
  local at = tonumber(t[1])*1000 + math.floor(tonumber(t[2])/1000)
  local deadline = tonumber(redis.call('HGET', gate.message, 'deadline_at'))
  local until_ = tonumber(redis.call('HGET', gate.lease, 'lease_until'))
  if not deadline or not until_ or at >= deadline or at >= until_ then
    error('settlement_expired_before_apply')
  end
  local pending = redis.call('XPENDING', gate.inbox, gate.group, gate.stream_id, gate.stream_id, 1)
  if #pending ~= 1 or pending[1][2] ~= gate.consumer then error('settlement_not_pending') end
end
if type(plan.rebind_gate) == 'table' then
  local g = plan.rebind_gate
  rebindcheck(g.lease, g.inbox, g.group, g.run, g.slot, g.generation, g.old_session, g.fence)
end
if type(plan.stop_gate) == 'table' then
  local g = plan.stop_gate
  local pending = redis.call('XPENDING', g.inbox, g.group, g.stream_id, g.stream_id, 1)
  if #pending ~= 1 or pending[1][2] ~= g.consumer then error('stopped_delivery_not_pending') end
end
for _, cmd in ipairs(plan.commands) do redis.call(unpack(cmd)) end
redis.call('HSET', KEYS[1], 'phase', 'applied')
return 'applied'
"""


class RuntimeV2:
    last_tick = None  # wall clock of the previous tick: a long gap = the daemon did not run (daemon_gap, §24)
    gap_before = False    # the previous tick also followed a daemon gap (daemon_gap)
    hb_grace_until = 0.0  # after a daemon gap, heartbeat_check waits this long for the bridges' next beat (§35)
    gap_done = False      # Fleet.tick already ran daemon_gap for this loop pass (gap_tick): tick does not again

    def __init__(self, fleet):
        self.f = fleet
        self.bus = bus_for(fleet.cfg, fleet.r)
        self.prefix = self.bus.prefix
        self.events = self.prefix + ':events'
        self.group = 'daemon'
        self.consumer = 'daemon'  # fixed same-run identity, exclusive daemon.lock
        self.reader = self.bus.blocking_client(1000)
        self.effects = None
        self.context = None
        self.queue_index = 0
        self.rotation_pending = set()
        self.startup_nudges = set()  # slots to re-ask for READY once, typed after the transaction
        self.pending_cursor = '0-0'
        self.stopfail_notices = []  # used-up API-error retries: tg / parent notices, sent outside any intent (tick)
        self.held_notices = []  # (slot, text, detail): escalations decided inside an intent, sent by held_watch_tick
        self.held_knobs_at, self.held_knobs_cache, self.pause_alerted = 0.0, None, ''
        self.renewed = {}
        self.cards_dirty, self.cards_refreshed = False, 0.0
        self.watchdog = watchdog.Watchdog(self)
        try:
            fleet.r.xgroup_create(self.events, self.group, '0-0', mkstream=True)
        except redis.ResponseError as exc:
            if 'BUSYGROUP' not in str(exc):
                raise

    def close(self):
        self.reader.close(); self.reader.connection_pool.disconnect()

    def audit(self, event, **fields):
        data = mask_secrets(fields)
        fields = {k: sanitize_text(v) if isinstance(v, str) else dumps(v) for k, v in data.items()}
        fields.update(event=event, run_id=self.bus.run_id, at=str(int(time.time()*1000)))
        if self.effects:
            self.effects.xadd(self.bus.audit_key, fields)
        else:
            self.bus.r.xadd(self.bus.audit_key, fields)

    def log(self, kind, data):
        self.audit(kind, **data)

    def ui(self, slot, card):
        fields = {'event_id': card['metadata']['event_id'], 'data': dumps(card)}
        if self.effects:
            self.effects.xadd(self.prefix + ':ui:' + slot, fields)
        else:
            self.bus.r.xadd(self.prefix + ':ui:' + slot, fields)

    def depth(self, slot):
        r = self.effects or self.bus.r
        return r.llen(self.bus.pending_key(slot))

    def reword_pending(self, slot):
        """In a transaction, after a wall switch set the slot's next launch: its queued messages were worded for the
        old CLI's reply channel (queue(), service request text); word them for the slot's CLI now."""
        bridge = not tui_slot(self.slot_cfg(slot), slot)
        for mid in self.effects.lrange(self.bus.pending_key(slot), 0, -1):
            body = json.loads(self.effects.hget(self.bus.msg_key(mid), 'body') or '{}')
            text = body.get('text') if isinstance(body.get('text'), str) else ''
            new = channel_wording(text, bridge)
            if new != text:
                self.effects.hset(self.bus.msg_key(mid), {'body': _json(dict(body, text=new))})
                self.audit('queued.reworded', slot=slot, message_id=mid, bridge=bridge)

    def pending_text(self, slot):
        r = self.effects or self.bus.r
        mids = r.lrange(self.bus.pending_key(slot), 0, 0)
        return [json.loads(r.hget(self.bus.msg_key(mids[0]), 'body'))['text']] if mids else []

    def queue(self, slot, text, *, request_id=None, kind=None, deadline_at=None, front=False):
        """``front``: the slot's next delivery (a report correction), ahead of what is already queued."""
        from fleetmux.fleet import REQ_HEAD, SUPERVISOR_INSTRUCTION_HEAD, REQ
        if self.effects is None:
            return self.transaction('q-' + uuid.uuid4().hex, lambda: self.queue(
                slot, text, request_id=request_id, kind=kind, deadline_at=deadline_at, front=front))
        # These headers are generated by the original dispatcher, not parsed from
        # arbitrary assistant text to infer execution authorization.
        head = REQ_HEAD.match(text)
        task_head = SUPERVISOR_INSTRUCTION_HEAD.search(text)
        if head:
            request_id = head.group(1)
            text = text[head.end():].lstrip()
            if self.f.r.hgetall(REQ + request_id):
                deadline_at = NO_DEADLINE_MS  # §43: expire() alone enforces the request's own deadline
        elif text.startswith('[New task '):
            end = text.index(']')
            request_id = text[len('[New task '):end]
            text = text[end+1:].lstrip()
        elif slot != 'supervisor' and task_head:
            request_id = task_head.group(1)
            text = text[:task_head.start()] + text[task_head.end():]
        request_id = request_id or (self.context or {}).get('bound_request') or self.f.task().get('id') or 'notice'
        if not self.f.service and self.f.task().get('deadline'):
            # The hours budget is awake time (§35) and the tick ends the task when it runs out (budget_exhausted);
            # the bus deadline is wall time shared with the bridges, so it does not carry the budget (a sleep
            # longer than the time left would hold the slot).
            deadline_at = deadline_at or NO_DEADLINE_MS
        deadline_at = deadline_at or int(time.time()*1000) + 3600000
        if kind is None and slot == 'supervisor' and self.f.service and not head:
            # Service mode: only a [Request <id>] prompt is bound to its request. Every other program message to
            # the supervisor (a sub-fleet report or interface notice, a correction, an escalation note) is a
            # notice: delivered even when the request id it inherited has ended (deliver drops the rest).
            kind = 'followup'
        kind = kind or ('supervisor' if slot == 'supervisor' else 'worker')
        # Prompt instructions remain business content. Transport labels are metadata.
        # Only Pi has the fleet_decision tool; official CLIs keep the AGENTS.md wording.
        if not tui_slot(self.slot_cfg(slot), slot):
            for tui, tool in CHANNEL_WORDING:
                text = text.replace(tui, tool)
        self.queue_index += 1
        mid = 'm-' + digest(self.context['intent_id'] + ':' + str(self.queue_index))[:32]
        body = {'text': text, 'title': t('delivery.title.advisor', 'Advisor consult') if kind == 'advisor'  # display
                else t('delivery.title.task', 'Task') if slot != 'supervisor'  # only (Pi: body.text is the model input)
                else t('delivery.title.business', 'Pending business'),
                'mode': self.f.cfg['mode'], 'purpose': kind}
        if self.f.service and head and slot != 'supervisor':
            body['reply'] = json.loads(self.f.r.hget(REQ + request_id, 'spec'))['reply']
        self.effects.hset(self.bus.msg_key(mid), dict(run_id=self.bus.run_id, slot=slot,
            message_id=mid, body=_json(body), request_id=request_id, kind=kind, status='queued',
            deadline_at=deadline_at, created_at=int(time.time()*1000), attempt=0))
        (self.effects.lpush if front else self.effects.rpush)(self.bus.pending_key(slot), mid)
        self.audit('queued', slot=slot, message_id=mid, request_id=request_id, detail=body)
        return mid

    def transaction(self, identity, action, *, event_stream_id=None, authorization=None):
        intent = self.prefix + ':intent:' + _short(identity, 'intent_id')
        phase = self.bus.r.hget(intent, 'phase')
        if phase == 'applied':
            if event_stream_id:
                # A duplicate Stream entry may exist after ambiguous producer
                # registration. Its durable effect intent proves ACK is now safe.
                self.bus.r.xack(self.events, self.group, event_stream_id)
            return None
        if phase == 'applying':
            raise BusError('ambiguous applying intent; run must remain held: ' + identity)
        if not phase:
            real = self.f.r
            self.effects = Effects(real)
            self.f.r = self.effects
            self.context = {'intent_id': identity}
            self.queue_index = 0
            try:
                answer = action()
                if event_stream_id:
                    self.effects.command('XACK', self.events, self.group, event_stream_id)
                plan = dict(commands=self.effects.commands,
                            authorization=authorization or self.context.get('authorization'),
                            commit_gate=self.context.get('commit_gate'), rebind_gate=self.context.get('rebind_gate'),
                            stop_gate=self.context.get('stop_gate'))
            finally:
                self.f.r = real
                self.effects = None
                self.context = None
            if not plan['commands']:
                return answer
            # No effects have occurred. Failure before this write leaves event PEL
            # replayable; randomness in a discarded plan never authorized work.
            self.bus.r.hset(intent, mapping={'phase': 'prepared', 'plan': dumps(plan)})
        else:
            answer = None
        self.apply(intent)
        return answer

    def apply(self, intent):
        phase = self.bus.r.hget(intent, 'phase')
        if phase == 'applied':
            return
        if phase != 'prepared':
            raise BusError('ambiguous/unrecognized intent: ' + intent)
        raw = self.bus.r.hget(intent, 'plan')
        self.bus.r.hset(intent, 'phase', 'applying')
        # Response loss: inspect phase on restart. An applied intent is complete;
        # anything still applying is held, never blindly rerun RPUSH/XADD.
        self.bus.r.eval(LUA_APPLY, 1, intent, raw)

    def convert_legacy(self):
        """LMOVE LEFT->RIGHT preserves legacy RPUSH FIFO and a reliable processing list."""
        processing = self.prefix + ':legacy-processing'
        raw = self.bus.r.lindex(processing, 0)
        if raw is None:
            raw = self.bus.r.lmove('fx:events', processing, 'LEFT', 'RIGHT')
        if raw is None:
            return False
        identity = 'legacy-' + digest(raw)
        try:
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError('legacy event must be an object')
        except ValueError as exc:
            event = {'kind': 'legacy_invalid', 'error': str(exc), 'raw': raw}
        post_event(self.bus, event, identity)
        self.bus.r.lrem(processing, 1, raw)  # only after deterministic registration
        return True

    def step(self, block_ms=1000):
        self.convert_legacy()
        rows = self.reader.xreadgroup(self.group, self.consumer, {self.events: self.pending_cursor}, count=1)
        if not rows or not rows[0][1]:
            rows = self.reader.xreadgroup(self.group, self.consumer, {self.events: '>'}, count=1, block=block_ms)
        if rows and rows[0][1]:
            sid, fields = rows[0][1][0]
            identity = fields['event_id']
            phase = self.bus.r.hget(self.prefix + ':intent:' + identity, 'phase')
            if phase == 'applying':
                # Preserve this PEL record for operator inspection but allow control
                # events to be handled. A poison/ambiguous event never gets ACKed.
                self.f.pause('ambiguous effect intent held for inspection: ' + identity, kind='integrity')
                self.pending_cursor = sid
                return True
            ev = json.loads(fields['data'])
            def handle():
                try:
                    self.handle(ev)
                except (ValueError, TypeError, KeyError) as exc:
                    # Reject malformed ingress visibly. Planning has no external
                    # side effects; business validators already validate dispatch
                    # before queueing it. Infrastructure errors remain fail closed.
                    self.effects.values.clear()
                    self.effects.commands.clear()
                    self.rotation_pending.clear()
                    self.startup_nudges.clear()
                    self.audit('event.rejected', error=str(exc), level='error', detail=ev)
                    self.reply(ev, {'status': 'rejected', 'reason': str(exc)})
            self.transaction(fields['event_id'], handle, event_stream_id=sid)
            self.pending_cursor = sid
            # Filesystem/session retirement is deliberately outside the Redis intent.
            # Crash at this boundary holds recovery; never repeats runtime execution.
            for slot in list(self.rotation_pending):
                self.rotation_pending.remove(slot)
                self.f.finish_rotation(slot)
            for slot in list(self.startup_nudges):  # pane typing / queueing is not a Redis effect either
                self.startup_nudges.remove(slot)
                pane = self.f.agent(slot).get('pane')
                if codex_queue.is_codex(self.slot_cfg(slot), slot):
                    try:
                        codex_queue.deliver(self.f, slot, 'startup-nudge-g' + str(self.f.agent(slot).get('generation')),
                                            STARTUP_NUDGE)
                    except codex_queue.QueueError as exc:
                        # No typing fallback: the startup deadline then blocks the slot as for no READY.
                        self.audit('startup.nudge_failed', slot=slot, level='error', error=str(exc)[:300])
                elif self.hooked(slot):
                    generation = int(self.f.agent(slot).get('generation') or 0)
                    claude_hook.post(self.bus.r, self.hook_keys(slot, generation),
                                     {'kind': 'nudge', 'generation': generation, 'text': STARTUP_NUDGE})
                elif pane:
                    from fleetmux.fleet import is_claude
                    if is_claude(self.f.runtime_cfg(slot), slot):  # §48: never typed; validate_config refuses first
                        self.transaction('startup-input-' + uuid.uuid4().hex, lambda slot=slot: self.hold(
                            slot, 'startup nudge not sent: Claude slot without a hook mailbox; ' + NO_TYPING))
                    elif not self.f.mgr.send_prompt_to_idle(pane, STARTUP_NUDGE):
                        reason = 'startup input unconfirmed: ' + (getattr(self.f.mgr, 'last_error', '') or 'terminal send failed')
                        self.transaction('startup-input-' + uuid.uuid4().hex,
                                         lambda slot=slot, reason=reason: self.hold(slot, reason))
            return True
        return False

    def handle(self, ev):
        from fleetmux import fleet as legacy
        kind, slot = ev.get('kind'), ev.get('agent')
        delivery = ev.get('delivery') if isinstance(ev.get('delivery'), dict) else {}
        spec = ev.get('spec') if isinstance(ev.get('spec'), dict) else {}
        candidate = ev.get('candidate')
        error_record = kind == 'candidate' and isinstance(candidate, dict) and 'error' in candidate
        self.audit(kind or 'invalid_event', slot=slot or '',
                   request_id=delivery.get('request_id') or spec.get('id') or '',
                   correlation_id=ev.get('request_id', ''), message_id=delivery.get('message_id', ''),
                   delivery_id=delivery.get('delivery_id', ''), generation=ev.get('generation'),
                   level='error' if error_record else 'info', detail=ev)
        if kind in ('cleared', 'stop_failure'):
            if not self.clear_enabled(slot):
                if kind == 'stop_failure':
                    self.api_error_retry(slot, ev)  # task-mode hook worker (§24); anything else: audit only
                return  # switch off: audit only, including StopFailure
            info = self.f.agent(slot)
            if (ev.get('run_id') != self.bus.run_id
                    or ev.get('generation') != int(info.get('generation', -1))):
                self.audit('event.stale', slot=slot)
                return
            if kind == 'cleared':
                self.cleared(slot, ev)
            elif ev.get('thread') != info.get('thread'):
                self.audit('event.stale_session', slot=slot)
            else:
                self.stop_failure(slot, ev)
            return
        if kind in ('bridge_session', 'startup', 'availability', 'candidate', 'started', 'settled', 'bridge_fault',
                    'turn_ended', 'turn_deferred', 'permission_request', 'permission_resolved', 'permission_refused'):
            info = self.f.agent(slot) if slot in self.f.names else {}
            if (not info or ev.get('run_id') != self.bus.run_id
                    or ev.get('generation') != int(info.get('generation', -1))):
                self.audit('event.stale', slot=slot or '')
                return
            if kind == 'bridge_session':
                # Fixed session binding: do not adopt a different same-generation Pi session.
                if info.get('thread') and info['thread'] != ev.get('thread'):
                    raise ValueError('same-generation session replacement is unsupported')
                if tui_slot(self.slot_cfg(slot), slot):
                    self.tui_session(slot, ev)
                else:
                    bridge_event(self.slot_cfg(slot), slot, ev['generation'], ev)
                self.effects.hset(legacy.key('agent', slot), {'thread': ev['thread'],
                    'transcript': ev['transcript'], 'bridge_available': 0})
                # Slot registration is completed by bridge only after this durable reply.
                self.reply(ev, {'status': 'registered'})
            elif ev.get('thread') != info.get('thread'):
                self.audit('event.stale_session', slot=slot)
            elif kind == 'startup':
                late = self.startup_late(info, ev)
                if info.get('state') != 'starting' and not late:
                    self.audit('startup.duplicate', slot=slot)
                    self.reply(ev, {'status': 'rejected', 'reason': 'startup belongs to an already settled or held runtime'})
                    return
                text_ok = ready_reply(ev.get('text', ''))
                walled = None
                if (info.get('launch_ready', '1') == '1' and not text_ok
                        and slot in self.f.workers + ['supervisor']):
                    # E: a wall instead of READY pauses resumable (Pi also reports the model error text).
                    walled = self.f.startup_wall(slot, '\n'.join(str(ev.get(k) or '') for k in ('text', 'error')))
                nudged = info.get('startup_nudged') == info.get('startup_deadline', '')
                if walled:
                    self.f.notify_later(walled)
                    self.reply(ev, {'status': 'rejected', 'reason': 'usage wall at startup: slot walled, fleet paused'})
                elif (not text_ok and info.get('launch_ready', '1') == '1' and not ev.get('failed')
                        and not ev.get('aborted') and not nudged and not legacy.is_opencode(self.slot_cfg(slot), slot)):
                    # opencode: no correction typed (supervisor ruling 2026-10-16); a wrong startup fails below.
                    # One correction per READY ask (keyed by its deadline), then block as before.
                    self.f.setf(slot, startup_nudged=info.get('startup_deadline', ''))
                    self.startup_nudges.add(slot)
                    self.audit('startup.nudged', slot=slot)
                    self.reply(ev, {'status': 'rejected', 'reason': 'startup reply was not READY; asked once more'})
                elif (info.get('launch_ready', '1') != '1'
                        or ev.get('failed') or ev.get('aborted') or not text_ok):
                    self.f.setf(slot, state='dead', runtime_ready=0, bridge_available=0)
                    if slot == advisor.SLOT:  # §27: restarted in place by advisor_tick, never blocks the task
                        self.audit('advisor.startup_failed', slot=slot, level='error')
                    elif legacy.is_opencode(self.slot_cfg(slot), slot):
                        self.f.finish('blocked', slot + ': opencode startup failed before READY ('
                                      + startup_problem(ev) + '); not retried, model unchanged')
                    elif self.f.startup_unusable(slot, '\n'.join(str(ev.get(k) or '') for k in ('text', 'error'))):
                        self.audit('startup.unusable', slot=slot, level='warn', error=str(ev.get('error'))[:300])
                    elif startup_offline(ev) and (retry := self.f.schedule_startup_retry(slot, info)):
                        self.audit('startup.offline', slot=slot, level='warn', error=str(ev.get('error'))[:300],
                                   detail={'attempt': retry[0], 'retry_in_s': retry[1]})
                    else:
                        self.f.finish('blocked', slot + ': startup failed before READY')
                    self.reply(ev, {'status': 'rejected', 'reason': 'startup did not settle with READY'})
                else:
                    self.f.pi_metadata(slot, ev)
                    self.f.setf(slot, state='idle', runtime_ready=1, idle_confirmed=1, bridge_available=0,
                                current_profile=info.get('runtime_profile', ''), startup_retries=0)
                    self.reply(ev, {'status': 'ready'})
                    if late:
                        self.startup_late_lift(slot, late)
                    if slot == 'supervisor':  # section 60: reconcile this generation, hand it the open items
                        from fleetmux import restart_state
                        restart_state.on_supervisor_ready(self.f, int(self.f.agent(slot).get('generation') or 0))
            elif kind == 'availability':
                self.f.setf(slot, bridge_available=int(ev.get('idle') is True))
            elif kind == 'bridge_fault':
                reason = ev.get('reason', 'bridge disconnected')
                if link_fault(reason):
                    self.link_lost(slot, reason)
                elif info.get('state') == 'held':
                    self.audit('bridge.fault_on_hold', slot=slot, error=reason)  # the daemon's hold already stands
                else:
                    self.hold(slot, reason)
            elif kind == 'turn_ended':
                self.release_held(slot, ev)
            elif kind == 'turn_deferred':
                # §24: this delivery's Stop waits on its own background tasks; the watchdog does not count the
                # slot as quiet until the transcript moves again. Not in flight (held, idle): audit only.
                lease = self.bus.r.hgetall(self.bus.lease_key(slot))
                if lease.get('status') == 'received' and lease.get('current_delivery'):
                    self.f.setf(slot, bg_wait=lease['current_delivery'], bg_wait_at=time.time(),
                                bg_wait_act=self.f.last_activity(info), bg_tasks=len(ev.get('tasks') or []))
            elif kind == 'permission_request':
                self.permission_request(slot, ev, info)
            elif kind in ('permission_resolved', 'permission_refused'):
                self.permission_state(slot, kind, ev, info)
            elif kind == 'started':
                try:
                    env = ev['delivery']
                    self.authorization(env)
                    self.f.r.hset(self.bus.msg_key(env['message_id']), mapping={
                        'started_at': int(time.time()*1000), 'execution_phase': 'running'})
                except (ValueError, KeyError, BusError) as exc:
                    self.audit('started.rejected', slot=slot, error=str(exc))
            elif (kind == 'candidate' and error_record and candidate.get('tool') == 'model'
                    and legacy.is_opencode(self.slot_cfg(slot), slot)):
                # opencode: the plugin's classified model error for this delivery (settled then carries only
                # fleet_result candidates); opencode_refused reads it back by delivery id. Never a wall.
                if delivery.get('delivery_id') and candidate.get('error_class') in OPENCODE_REFUSALS + ('model_lock',):
                    pending = json.loads(self.f.agent(slot).get('oc_error') or 'null') or {}
                    if pending.get('delivery_id') != delivery['delivery_id'] or candidate['error_class'] == 'model_lock':
                        self.f.setf(slot, oc_error=dumps({'delivery_id': delivery['delivery_id'],
                                    'error_class': candidate['error_class'], 'error': str(candidate.get('error') or '')[:1000]}))
            elif kind == 'candidate' and error_record and candidate.get('tool') == 'model':
                # Pi model error (stopReason error): remember a wall for this delivery; its settled
                # (failed) event then ends as a wall instead of an unconfirmed hold.
                from fleetmux import brake
                text = str(candidate.get('error') or '')[:brake.TURN_WALL_MAX]
                if delivery.get('delivery_id') and brake.classify_wall(text):
                    self.f.setf(slot, wall_error=dumps({'delivery_id': delivery['delivery_id'], 'text': text}))
                elif (slot == 'supervisor' and delivery.get('delivery_id')
                        and API_NETWORK_ERROR.search(str(candidate.get('error') or ''))):
                    # §24 supervisor regeneration: its settled (failed) event then holds it as an API-error end and
                    # the daemon replaces it without waiting for held_after_s.
                    self.f.setf(slot, api_error=dumps({'delivery_id': delivery['delivery_id'],
                                                       'text': str(candidate.get('error') or '')[:1000]}))
            elif kind == 'settled':
                self.settle(slot, ev)
                env = ev.get('delivery') or {}
                state = self.f.r.hget(self.bus.msg_key(env['message_id']), 'status') if env.get('message_id') else ''
                self.reply(ev, {'status': 'committed' if state == 'committed' else 'held' if state in ('uncertain', 'expired') else 'rejected'})
            # started/candidate are audit only, never ACK delivery or dispatch.
            return
        if kind in ('turn', 'pi_session'):
            self.audit('event.v1_rejected', slot=slot or '')
            return
        if kind == 'peer':  # §21 broker verb: Redis-only effects (queue + lists), plannable unlike chrome
            from fleetmux import peer
            self.reply(ev, peer.broker(self.f, ev))
            return
        if kind == 'unsolicited':  # §21: a worker turn no delivery started; never a settlement
            from fleetmux import peer
            info = self.f.agent(slot) if slot in self.f.workers else {}
            if (not info or ev.get('run_id') != self.bus.run_id or ev.get('thread') != info.get('thread')
                    or ev.get('generation') != int(info.get('generation', -1))):
                self.audit('event.stale', slot=slot or '')
                return
            peer.unsolicited(self.f, slot, str(ev.get('text') or ''))
            return
        if kind == 'dialog_user_answer':  # the phone's step two / reject (contracts §8)
            _request_id(ev.get('request_id'))
            try:
                answer = self.user_dialog_answer(ev)
            except (ValueError, TypeError, KeyError):
                raise
            except Exception as exc:  # fail closed: nothing is approved, the phone is told to look again
                self.audit('dialog.user_answer_failed', level='error', error=type(exc).__name__,
                           pending_id=str(ev.get('pending_id') or ''))
                answer = {'status': 'stale', 'reason': 'answer check failed'}
            self.reply(ev, answer)
            return
        if kind == 'chrome':
            # An external broker call is a side effect and cannot be planned/replayed.
            self.reply(ev, {'status': 'rejected', 'reason': 'browser broker v2 transaction support is not implemented'})
            return
        if kind == 'stop':
            if ev.get('suspend') and self.f.cfg.get('parent_config') and self.f.cfg.get('job') and not self.f.service:
                # section 58 "Restart": the root stops, this job sub-fleet is suspended: its task stays running (no finish)
                # and the root hands it to a fresh slot when it runs again
                self.f.explicit_stop = self.f.stopping = self.f.suspending = True
                self.audit('job.suspending', level='warn', request_id=self.f.task().get('id', ''))
                self.reply(ev, {'status': 'accepted', 'execution_stopped': False, 'suspend': True})
                return
            self.f.explicit_stop = True
            self.f.stopping = True
            self.f.stop_final = bool(ev.get('final'))  # section 60: stop --final, nothing comes back on the next start
            self.f.finish('cancelled', 'explicit fleet stop; runtime retirement required')
            self.reply(ev, {'status': 'accepted', 'execution_stopped': False})
            return
        # Reuse original request/task/operator/pause/resume semantics and validation.
        if kind == 'operator_input':
            ev = dict(ev)
            if ev.get('run_id') != self.bus.run_id:
                self.reply(ev, {'status': 'rejected', 'reason': 'foreign run'})
                return
            # Allocate correlation-derived IDs: stable if the event is replayed.
            # Original Fleet generates request/task IDs only in this effect plan;
            # persistence makes them stable after the plan is saved.
        if kind == 'escalation_answer' and ev.get('run_id') != self.bus.run_id:  # §24 `fleetmux answer`
            self.reply(ev, {'status': 'rejected', 'reason': 'foreign run'})
            return
        if kind not in ('operator_input', 'request', 'task', 'recheck', 'job_cancel', 'reconcile', 'pause', 'resume',
                        'escalation_answer', 'legacy_invalid'):
            self.reply(ev, {'status': 'rejected', 'reason': 'unsupported v2 event'})
            return
        if kind == 'legacy_invalid':
            return
        _request_id(ev.get('request_id'))
        before_values = copy.deepcopy(self.effects.values)
        before_commands = list(self.effects.commands)
        self.f.handle(ev, _v2_internal=True)
        raw = self.f.r.get('fx:reply:' + ev['request_id'])
        answer = json.loads(raw)
        if answer.get('status') == 'rejected':
            # A v2 byte-size rejection may occur when the original handler has
            # already planned request/task fields. Discard them with the queue.
            self.effects.values = before_values
            self.effects.commands = before_commands
            self.f.r.set('fx:reply:' + ev['request_id'], raw, ex=86400)
        self.reply(ev, answer)
        if answer.get('status') == 'accepted':
            self.audit('accepted', request_id=answer.get('id') or answer.get('task_id') or spec.get('id') or '',
                       correlation_id=ev['request_id'], slot=slot or '', generation=ev.get('generation'), detail=answer)

    def tui_session(self, slot, ev):
        """Codex notify / Claude Stop binding: session id, and Claude's transcript only."""
        _short(ev.get('thread'), 'session_id')
        transcript = ev.get('transcript')
        if (self.slot_cfg(slot).get('supervisor_cli' if slot == 'supervisor' else 'worker_cli')) == 'claude':
            if not isinstance(transcript, str) or not transcript.endswith('.jsonl') or not Path(transcript).is_absolute():
                raise ValueError('Claude session binding requires its absolute transcript path')
        elif transcript != '':
            raise ValueError('Codex usage comes from its rollout files; transcript must be empty')

    def reply(self, ev, answer):
        correlation = ev.get('request_id')
        if correlation:
            try:
                _request_id(correlation)
            except ValueError:
                self.audit('reply.invalid_correlation', level='error')
                return
            self.effects.xadd(self.prefix + ':reply:' + correlation, {'data': dumps(answer)})

    def authorization(self, env):
        self.bus._authorization(env['slot'], env)
        msg = self.bus.message(env['message_id'])
        lease = self.bus.r.hgetall(self.bus.lease_key(env['slot']))
        names = ('run_id', 'slot', 'generation', 'session_id', 'message_id', 'delivery_id', 'stream_id', 'fence')
        if any(str(msg.get(k)) != str(env.get(k)) for k in names):
            raise BusError('settlement identity mismatch')
        if msg.get('consumer') != env.get('consumer'):
            raise BusError('settlement consumer mismatch')
        if lease.get('current_delivery') != env['delivery_id']:
            raise BusError('settlement stale lease')
        return msg, lease

    def release(self, env, result, outcome='completed'):
        self.effects.hset(self.bus.msg_key(env['message_id']), {'status': 'committed',
            'outcome': outcome, 'result': _json(result), 'committed_at': int(time.time()*1000)})
        self.effects.hset(self.bus.lease_key(env['slot']), {'status': 'idle', 'current_delivery': '',
            'message_id': '', 'stream_id': '', 'consumer': '', 'lease_until': 0})
        self.effects.command('XACK', self.bus.inbox_key(env['slot']), self.bus.slot_group(env['slot']), env['stream_id'])
        self.f.setf(env['slot'], state='idle', idle_confirmed=1, request='', bridge_available=0)
        if outcome == 'completed' and env['slot'] == 'supervisor' and self.f.agent('supervisor').get('stopfail_streak'):
            self.f.setf('supervisor', stopfail_streak=0)  # §24: a settled decision ends a service StopFailure streak
        self.audit('committed', slot=env['slot'], message_id=env['message_id'],
                   delivery_id=env['delivery_id'], request_id=env['request_id'], detail=result)

    def midturn_card(self, slot, text):
        card = routing_card(text, kind='notice', slot=self.f.label(slot))
        card['metadata']['event_id'] = 'ui-midturn-' + digest(self.context['intent_id'] + ':' + slot)[:32]
        self.ui(slot, card)
        if slot != 'supervisor':
            self.ui('supervisor', card)

    def midturn_no_usage(self, slot, generation):
        from fleetmux import fleet as legacy
        self.f.setf(slot, midturn_no_usage_g=generation)
        if legacy.is_dsh(self.slot_cfg(slot), slot):  # a typed mid-turn prompt would be a manual dsh-TUI input
            self.audit('midturn.no_usage', slot=slot, level='warn', error='dsh: no mid-turn handoff (turn boundaries only)')
            self.midturn_card(slot, t('midturn.dsh', '{label} is a dsh worker: no mid-turn handoff (rotation only at '
                                          'turn boundaries).', label=self.f.label(slot)))
            return
        self.audit('midturn.no_usage', slot=slot, level='error', error='opencode session usage source is unverified')
        self.midturn_card(slot, t('midturn.no_usage', '{label}: mid-turn usage unknown (opencode session source '
                                      'unverified); no mid-turn handoff possible.', label=self.f.label(slot)))

    def midturn_splits(self, msg):
        count = int(msg.get('midturn_splits') or 0)
        if self.f.service:
            count = max(count, int(self.f.r.hget('fx:req:' + msg['request_id'], 'midturn_splits') or 0))
        return count

    def midturn_pause_request(self, rid, now):
        key = 'fx:req:' + rid
        req = self.f.r.hgetall(key) if self.f.service else {}
        if req:
            self.f.r.hset(key, mapping={'clock_paused_at': midturn.pause_clock(req, now)['clock_paused_at']})

    def midturn_resume_request(self, rid):
        key = 'fx:req:' + rid
        req = self.f.r.hgetall(key) if self.f.service else {}
        if req and req.get('clock_paused_at'):
            fields = midturn.resume_clock(req, awake.now())
            self.f.r.hset(key, mapping={k: fields[k] for k in ('clock_paused_at', 'clock_paused_ms')})

    def midturn_clear(self, slot, resume=True):
        info = self.f.agent(slot)
        if resume and info.get('midturn_request'):
            self.midturn_resume_request(info['midturn_request'])
        # Keep the generation stamp: even a successful report permits only one order per generation.
        fields = {k: '' for k in info if k.startswith('midturn_') and k not in
                  ('midturn_typed_g', 'midturn_no_usage_g', 'midturn_ratio', 'midturn_line')}
        if fields:
            self.f.setf(slot, **fields)

    def midturn_blocked(self, slot, msg, impossible=False):
        """No new handoff or kill at the split cap. A still-running delivery remains held."""
        reason = midturn.blocked_text(self.midturn_splits(msg))
        state = self.f.r.hgetall(self.bus.lease_key(slot))
        if state.get('message_id') == msg['message_id'] and state.get('current_delivery'):
            # The bridge reads this to show "split cap" instead of "connection lost" on the pane.
            (self.effects or self.bus.r).hset(self.bus.msg_key(msg['message_id']), mapping={'midturn_limit': '1'})
            self.hold(slot, reason, pause=False)
        elif msg.get('status') == 'queued':
            self.cancel_queued(slot, msg, reason)
        self.midturn_resume_request(msg['request_id'])
        self.midturn_clear(slot)
        self.audit('midturn.impossible' if impossible else 'midturn.limit', slot=slot, level='error',
                   message_id=msg['message_id'], error=reason)
        if slot == 'supervisor':
            if self.f.service:
                self.f.pause(reason, kind='supervisor_fault', slot='supervisor', message_id=msg['message_id'])
            else:
                self.f.finish('blocked', reason)
        elif self.f.service:
            self.f.service_blocked_intake(slot, msg['request_id'], reason)
        else:
            key, note = self.f.blocked_intake(slot, self.f.report_item(slot), reason)
            mid = self.queue('supervisor', '[blocked] ' + self.f.label(slot) + ': ' + reason + note,
                             request_id=msg['request_id'])
            escalations = self.f.escalation_state()
            escalations[key]['message_id'] = mid
            self.f.save_escalations(escalations)
        self.midturn_card(slot, t('card.label_reason', '{label}: {reason}', label=self.f.label(slot), reason=reason))

    def midturn_split(self, slot, env, msg, *, retired=False, note=None, previous=None):
        """One intent: release/ACK the stopped turn, prepend its continuation, retain ownership.

        retired is used only by Watchdog.rotate after kill + confirm_retired; that path
        has already drained the old lease and must not reset the successor's startup state.
        """
        from fleetmux.fleet import slot_base
        info = self.f.agent(slot)
        hid = msg['midturn_id']
        generation = int(msg['generation'])
        prev = previous or self.f.label(slot)
        label = slot_base(slot) + '.' + str(generation + 1)
        note = note or str(self.f.run_dir / 'handoffs' / (slot + '-g' + str(generation) + '-' + hid + '.md'))
        splits = self.midturn_splits(msg) + 1
        if not retired:
            self.release(env, {'midturn': 'split'}, outcome='handoff_split')
        else:
            self.f.r.hset(self.bus.msg_key(msg['message_id']), mapping={'status': 'committed',
                'outcome': 'handoff_split', 'result': _json({'midturn': 'split'}), 'committed_at': int(time.time()*1000)})
        original = msg.get('midturn_instruction') or msg['body']['text']
        body = dict(msg['body'], purpose='continuation',
                    text=midturn.continuation(label, prev, note, original))
        self.queue_index += 1
        mid = 'm-' + digest(self.context['intent_id'] + ':' + str(self.queue_index))[:32]
        self.effects.hset(self.bus.msg_key(mid), dict(run_id=self.bus.run_id, slot=slot, message_id=mid,
            body=_json(body), request_id=msg['request_id'], kind='continuation', status='queued',
            deadline_at=msg['deadline_at'], created_at=int(time.time()*1000), attempt=0,
            continues=msg['message_id'], midturn_splits=splits, replay=msg.get('replay') or 1,
            midturn_instruction=original, original_kind=msg.get('original_kind') or msg.get('kind') or '',
            clock_paused_at=msg.get('clock_paused_at') or time.time(),
            deadline_remaining_ms=msg.get('deadline_remaining_ms') or 1))
        self.effects.lpush(self.bus.pending_key(slot), mid)  # same FIFO head operation as offer_handoff
        if self.f.service and self.f.r.hgetall('fx:req:' + msg['request_id']):
            self.f.r.hset('fx:req:' + msg['request_id'], mapping={'midturn_splits': splits, 'continues': msg['message_id']})
        if not retired:
            self.f.setf(slot, state='handoff', handoff_id=hid, handoff_draft=info['midturn_draft'],
                        handoff_mtime=info['midturn_mtime'], handoff_deadline=time.time() + fleet_config.handoff_timeout(self.f.cfg))
            self.rotation_pending.add(slot)
        else:
            self.midturn_clear(slot, resume=False)
        self.audit('midturn.killed' if retired else 'midturn.split', slot=slot, message_id=msg['message_id'],
                   detail={'continues': mid, 'midturn_splits': splits, 'note': note})
        self.midturn_card(slot, t('midturn.split', '{prev} mid-turn handoff -> {label} continues (#{n})', prev=prev,
                                  label=label, n=splits))
        return mid

    def midturn_result(self, slot, env, msg, error):
        if not msg.get('midturn_id'):
            return False
        if error:
            self.midturn_split(slot, env, msg)
            return True
        self.midturn_clear(slot)
        return False

    def wall_settle(self, slot, ev, env, msg):
        """E: the turn ended on a usage wall (short official-CLI text, or a Pi model error for this delivery).
        The turn is over, so execution has stopped: commit the delivery as `walled` (result not applied, never
        a report), trip the brake, and queue the instruction again ONCE for after the pause / downgrade. A
        replayed settled event finds the message committed (settle.late), so nothing is queued twice."""
        from fleetmux import brake
        text = ev.get('wall_text') if isinstance(ev.get('wall_text'), str) else ''
        if not text:
            pending = json.loads(self.f.agent(slot).get('wall_error') or 'null') or {}
            text = pending.get('text', '') if pending.get('delivery_id') == env['delivery_id'] else ''
        if not text or len(text.strip()) > brake.TURN_WALL_MAX or not brake.classify_wall(text):
            return False
        return self.wall_commit(slot, env, msg, text, 'turn')

    def wall_commit(self, slot, env, msg, text, source, wall=None):
        """The turn ended on a wall (``wall_settle``; a StopFailure safeguards refusal, design section 23): decide and
        trip the brake (``Fleet.on_wall``), commit the delivery as `walled` (result not applied, never a report) and
        queue its instruction again ONCE (worker text behind WALL_RESUME, supervisor text as it was) for the next
        model / after the pause. The commit makes a replayed event a no-op (settle.late / authorization fails)."""
        from fleetmux import brake
        record = self.f.on_wall(slot, text, source, wall=wall)
        self.release(env, {'wall': record['wall']}, 'walled')
        decided = json.loads(self.f.agent(slot).get('wall') or '{}')
        if (wall or {}).get('kind') == brake.SAFEGUARDS and decided.get('action') == 'stop':
            # told upwards at once, as the adopted-refusal branch did (a sub-fleet: its parent's subfleet-notices)
            self.held_notices.append((slot, 'safeguards refusal on ' + str(decided.get('cli')) + ':'
                                      + str(decided.get('from')) + ' and ' + str(decided.get('advice') or 'stop'),
                                      {'reason': 'safeguards', 'paused': True}))
        self.midturn_clear(slot)
        self.f.setf(slot, wall_error='')
        body = msg['body'].get('text') or ''
        if body:  # same request / kind / deadline / reply spec as the walled delivery: only the text gains a note
            self.queue_index += 1
            mid = 'm-' + digest(self.context['intent_id'] + ':' + str(self.queue_index))[:32]
            again = dict(msg['body'], text=body if slot == 'supervisor' else brake.WALL_RESUME + body)
            self.effects.hset(self.bus.msg_key(mid), dict(run_id=self.bus.run_id, slot=slot, message_id=mid,
                body=_json(again), request_id=msg['request_id'], kind=msg.get('kind') or again.get('purpose') or '',
                status='queued', deadline_at=msg['deadline_at'], created_at=int(time.time()*1000), attempt=0,
                wall_of=env['message_id']))
            retained = {k: msg[k] for k in ('midturn_splits', 'continues', 'midturn_instruction', 'original_kind', 'replay')
                        if k in msg}
            if retained:
                self.effects.hset(self.bus.msg_key(mid), retained)
            self.effects.rpush(self.bus.pending_key(slot), mid)
            self.audit('queued', slot=slot, message_id=mid, request_id=msg['request_id'], detail=again)
        self.audit('brake.wall_requeued', slot=slot, message_id=env['message_id'], delivery_id=env['delivery_id'],
                   level='warn', detail={'scope': record['wall']['scope'], 'requeued': bool(body),
                                         'kind': record['wall'].get('kind') or record['wall']['scope'],
                                         'source': source})
        self.f.notify_later(record)
        return True

    def release_held(self, slot, ev):
        """Hold exit: the held delivery's own turn ended in its own session, so its execution has
        stopped. Release the slot (delivery cancelled, result NOT applied); the pause stays as is."""
        env = ev.get('delivery')
        lease = self.bus.r.hgetall(self.bus.lease_key(slot))
        problem = None
        if not isinstance(env, dict) or env.get('slot') != slot:
            problem = 'invalid delivery identity'
        elif self.f.agent(slot).get('clear_conflict') == '1':
            problem = 'session changed during execution; resume must replace this runtime'
        elif self.f.agent(slot).get('state') not in ('held', 'clearing') or lease.get('status') not in ('uncertain', 'expired'):
            problem = 'slot is not held'
        elif lease.get('current_delivery') != env.get('delivery_id') or lease.get('message_id') != env.get('message_id'):
            problem = 'not the held delivery'
        else:
            msg = self.bus.message(env['message_id'])
            if any(str(msg.get(k)) != str(env.get(k)) for k in IDENTITY if k != 'request_id'):
                problem = 'delivery identity mismatch'
        if problem is None:
            try:
                self.bus.resolve_uncertain(slot, env, 'cancelled', execution_stopped=True)  # idempotent on replay
            except (BusError, ValueError, KeyError, redis.ResponseError) as exc:
                problem = 'release failed: ' + str(exc)[:300]
        if problem:
            self.audit('hold.release_rejected', slot=slot, level='warn', error=problem)
            self.reply(ev, {'status': 'rejected', 'reason': problem})
            return
        msg = self.bus.message(env['message_id'])
        if (ev.get('text') or '').strip():
            found = tui_candidates(slot, msg['body'].get('purpose'), ev['text'], msg['body'].get('mode') or 'service')
            self.late_answer(slot, msg, found[0]['value'] if found else {'raw': ev['text'][-4000:]})
        # §24: task mode -- the supervisor learns that this turn was not applied, with its final text (service mode:
        # the request already has its public result; result.late keeps the text).
        notified = (not self.f.service and slot in self.f.workers and slot != advisor.SLOT
                    and self.f.task().get('status') == 'running')
        if notified:
            label, tail = self.f.label(slot), (ev.get('text') or '').strip()[-4000:]
            self.f.queue('supervisor', '[' + label + ' report]\nHELD-RELEASED: the program held this slot ('
                         + str(msg.get('uncertain_reason') or 'execution state unconfirmed')[:300] + ') and did not '
                         'apply the result of its turn; the worker\'s final text follows. Check the workspace, then '
                         'dispatch again or accept it.\n\n' + (tail or '(the turn ended with no text)')
                         + '\n\nReply with the JSON format from AGENTS.md.')
        self.f.setf(slot, state='idle', idle_confirmed=1, bridge_available=0, request='', watch_state='idle',
                    stall_escapes=0, watch_since=0, quiet_since=0, bg_wait='')
        if codex_queue.is_codex(self.slot_cfg(slot), slot):
            # The rollout proved the held item ran to completion; an unsettled record would refuse the next deliver.
            codex_queue.clear(self.f, slot, 'held delivery released')
        if self.clear_enabled(slot):
            self.begin_clear(slot, hook_pid=ev.get('hook_pid'))
        self.audit('hold.released', slot=slot, level='warn', message_id=env['message_id'],
                   delivery_id=env['delivery_id'], detail={'reason': 'held turn ended in its own session; '
                   'execution stopped, result not applied', 'paused': self.f.paused(), 'notified': notified})
        self.reply(ev, {'status': 'released', 'execution_stopped': True})

    def late_answer(self, slot, msg, reply):
        """§43: an answer to a service request that already has its public result (e.g. timeout): audited with its
        payload so it can be recovered, never applied (``fx:result:<id>`` keeps the first result)."""
        rid = msg.get('request_id')
        raw = self.f.r.get(self.prefix + ':public:' + digest(rid)) if self.f.service and rid else None
        if not raw:
            return
        self.audit('result.late', slot=slot, request_id=rid, message_id=msg.get('message_id', ''), level='warn',
                   label=self.f.label(slot), detail={'public_status': json.loads(raw).get('status'), 'reply': reply})

    def remember(self, slot, business_value):
        # Preserve the original recent-reply input to the handoff skeleton. Store
        # candidate business data, before Python injects service correlation IDs.
        self.effects.rpush('fx:replies:' + slot, dumps(business_value)[-4000:])
        self.effects.ltrim('fx:replies:' + slot, -5, -1)

    def settle(self, slot, ev):
        from fleetmux import fleet as legacy
        env = ev.get('delivery')
        if not isinstance(env, dict) or env.get('slot') != slot:
            self.audit('settle.invalid_identity', slot=slot)
            return
        try:
            msg, lease = self.authorization(env)
        except (BusError, ValueError, KeyError) as exc:
            self.audit('settle.rejected', slot=slot, error=str(exc))
            return
        now = int(time.time()*1000)
        if self.clear_enabled(slot):
            self.context['clear_hook_pid'] = ev.get('hook_pid')
        if msg['status'] != 'received' or lease.get('status') != 'received':
            self.audit('settle.late', slot=slot, message_id=env['message_id'], detail=ev)
            found = [c.get('value') for c in ev.get('candidates') or () if isinstance(c, dict)]
            if found:
                self.late_answer(slot, msg, found[0])
            return
        if int(lease['lease_until']) <= now < msg['deadline_at'] and self.reclaim_tui(slot):
            lease = self.bus.r.hgetall(self.bus.lease_key(slot))  # live: the commit gate re-reads it at apply
        if now >= min(msg['deadline_at'], int(lease['lease_until'])):
            if self.clear_enabled(slot) and not any(ev.get(k) for k in ('failed', 'aborted', 'unfinished')):
                # This validated Stop proves execution ended, even if the public timeout
                # won the race. Unlike an ordinary timeout, it is safe to drain the PEL.
                self.stop_failure(slot, dict(ev, thread=env['session_id'],
                                             error='deadline/lease expired before settlement validation'), status='timeout')
                return
            self.hold(slot, 'deadline/lease expired before settlement validation', public_status='timeout')
            return
        self.context['authorization'] = {
            self.bus.msg_key(env['message_id']): dict({k: str(env[k]) for k in
                ('run_id', 'slot', 'generation', 'session_id', 'message_id', 'delivery_id', 'stream_id', 'fence', 'consumer')}, status='received'),
            self.bus.lease_key(slot): {'current_delivery': env['delivery_id'],
                                     'generation': str(env['generation']), 'session_id': env['session_id'], 'status': 'received'}}
        self.context['commit_gate'] = {'message': self.bus.msg_key(env['message_id']),
            'lease': self.bus.lease_key(slot), 'inbox': self.bus.inbox_key(slot),
            'group': self.bus.slot_group(slot), 'stream_id': env['stream_id'], 'consumer': env['consumer']}
        candidates = ev.get('candidates')
        purpose = msg['body'].get('purpose')
        error = None
        if slot == advisor.SLOT and purpose != 'handoff':
            # §27: the advisor's answer to one consultation. Never a report, never task state, never the brake:
            # the waiting slot reads and validates it (Fleet.advisor_poll); a wall restarts the tab.
            if ev.get('aborted') is not False or ev.get('failed') is not False or ev.get('unfinished'):
                self.hold(slot, 'aborted/error/unfinished settlement; execution stop unconfirmed')
                return
            first = candidates[0] if isinstance(candidates, list) and len(candidates) == 1 else {}
            text = str(((first.get('value') if isinstance(first, dict) else None) or {}).get('text') or '')
            self.release(env, {'advisor_reply': text[-4000:]})
            self.f.advisor_settled(env['request_id'], env['message_id'], text)
            return
        if purpose != 'handoff' and self.wall_settle(slot, ev, env, msg):
            return
        if (purpose != 'handoff' and ev.get('failed') is True and ev.get('aborted') is not True and not ev.get('unfinished')
                and legacy.is_opencode(self.slot_cfg(slot), slot) and self.opencode_refused(slot, ev, env, msg)):
            return
        if ev.get('aborted') is not False or ev.get('failed') is not False or ev.get('unfinished'):
            api = self.api_error_of(slot, env['delivery_id']) if slot == 'supervisor' else ''
            if not api and self.requeue_hold(slot, 'aborted/error/unfinished settlement; execution stop unconfirmed',
                                             held_requeue.settle_cause(ev), msg=msg, ev=ev):
                return  # §24 held requeue: only this slot is held; no public result, no pause
            self.hold(slot, ('API error (' + api[:200] + '); execution stop unconfirmed') if api else
                      'aborted/error/unfinished settlement; execution stop unconfirmed', public_status='failed')
            if api:
                self.f.setf(slot, regen_due=awake.now())  # §24: regenerated without waiting for held_after_s
            return
        if purpose == 'handoff':
            self.release(env, {'handoff': 'settled'})
            self.f.setf(slot, state='handoff')
            self.rotation_pending.add(slot)
            return
        if not isinstance(candidates, list) or len(candidates) != 1:
            error = 'exactly one structured candidate is required'
        else:
            candidate = candidates[0]
            tool = 'fleet_decision' if slot == 'supervisor' else 'fleet_result'
            if not isinstance(candidate, dict) or candidate.get('tool') != tool or not isinstance(candidate.get('value'), dict):
                error = 'candidate tool/role/shape mismatch'
        value = {} if error else copy.deepcopy(candidates[0]['value'])
        history_value = copy.deepcopy(value)
        text = dumps(value)
        self.context['bound_request'] = env['request_id']
        self.context['bound_slot'] = slot
        self.context['bound_message'] = env['message_id']
        self.context['bound_delivery'] = env['delivery_id']
        self.f.pi_metadata(slot, ev)
        info = self.f.agent(slot)
        if slot == 'supervisor':
            if self.f.service and not error:
                for field in ('dispatch', 'skip', 'answer'):
                    if not isinstance(value.get(field, []), list):
                        error = field + ' must be a list'
                routed = [] if error else value.get('dispatch', []) + value.get('skip', []) + value.get('answer', [])
                for item in routed:
                    if not isinstance(item, dict) or item.get('request_id', env['request_id']) != env['request_id']:
                        error = 'service decision may route only its bound request'; break
                    item['request_id'] = env['request_id']
                if not error and len(routed) > 1:  # answer is an action too: one dispatch, skip or answer
                    error = 'one dispatch, skip or answer per bound service request'
                if not error and isinstance(value.get('subfleet'), dict):  # §29: only its bound external op
                    if value['subfleet'].setdefault('id', env['request_id']) != env['request_id']:
                        error = 'service decision may answer only its bound sub-fleet request'
                if not error:
                    text = dumps(value)
            if not error:
                try:
                    if self.f.service:
                        legacy.parse_service_decision(text, self.f.dispatch_targets())
                    else:
                        legacy.parse_decision(text, self.f.dispatch_targets(), self.f.others_active(),
                                              self.f.tree_context())
                except (ValueError, TypeError, KeyError) as exc:
                    error = str(exc)
            if self.midturn_result(slot, env, msg, error):
                return
            if error:
                self.audit('candidate.invalid', slot=slot, error=error, detail=candidates)
            self.release(env, value, 'failed' if error else 'completed')
            about = (msg.get('body') or {}).get('text', '')  # §24: the message this reply answers
            if self.f.service:
                self.f.on_service_supervisor(text, invalid=error, about=about, about_id=env['message_id'])
            else:
                self.f.on_supervisor(text, invalid=error, about=about, about_id=env['message_id'])
            invalid = error or int(self.f.r.hget(legacy.SERVICE if self.f.service else legacy.TASK, 'bad_replies') or 0)
            if invalid:
                self.f.r.hset(self.bus.msg_key(env['message_id']), mapping={'outcome': 'failed'})
            else:
                self.remember(slot, history_value)
            if not invalid and not self.f.service and self.f.task().get('status') == 'completed':
                self.ui('supervisor', result_card({'status': 'ok', 'worker': self.f.label(slot),
                    'message_id': env['message_id'], 'result': {'text': self.f.task().get('reason', '产物已通过验收。')}},
                    execution_stopped=True))
            elif not invalid and value.get('reason'):
                summary = routing_card(value['reason'], kind='dispatched', slot=self.f.label(slot))
                summary['metadata']['event_id'] = 'ui-decision-' + env['message_id']
                self.ui('supervisor', summary)
        elif self.f.service:
            req = self.f.r.hgetall(legacy.REQ + env['request_id'])
            if req.get('status') != 'dispatched':
                self.audit('result.late', slot=slot, request_id=env['request_id'])
                self.hold(slot, 'public result already terminal; inspect before reusing slot')
                return
            blocked = '' if error else legacy.service_blocked(value, json.loads(req['spec'])['reply'])
            if blocked:
                self.midturn_clear(slot)
                # Plan F' 8.4 (service): not an answer. The delivery ends, the request stays dispatched, and the
                # supervisor rules; its ruling goes back to this worker as the same request.
                self.release(env, value)
                self.f.service_blocked_intake(slot, env['request_id'], blocked)
                return
            split = '' if error else legacy.service_split(value, json.loads(req['spec'])['reply'])
            if split:  # P7 (brief v2.1 #3): a §44 escalate, not an answer; the request stays dispatched
                self.midturn_clear(slot)
                self.release(env, value)
                self.f.service_split_intake(slot, env['request_id'], value,
                                           instruction=msg.get('midturn_instruction') or msg['body'].get('text'))
                return
            if not error:
                try:
                    legacy.parse_reply(text, json.loads(req['spec'])['reply'])
                except ValueError as exc:
                    error = str(exc)
            if self.midturn_result(slot, env, msg, error):
                return
            # Schema failure is a terminal invalid result; no automatic repeat of tools.
            self.release(env, value, 'failed' if error else 'completed')
            if not error:
                self.remember(slot, value)
                if info.get('stopfail_streak'):
                    self.f.setf(slot, stopfail_streak=0)  # a good answer ends a StopFailure streak
            self.publish(env['request_id'], 'invalid' if error else 'ok',
                         worker=self.f.label(slot), **({'error': error, 'raw': value} if error else {'result': value}))
        else:
            if not error and (not all(isinstance(value.get(k), list) and value[k]
                    and all(isinstance(item, str) for item in value[k]) for k in ('done', 'evidence'))
                    or not all(isinstance(value.get(k), str) for k in ('not_done', 'blocked'))):
                error = 'task result requires done/evidence lists and not_done/blocked text'
            if self.midturn_result(slot, env, msg, error):
                return
            self.release(env, value, 'failed' if error else 'completed')
            retries = int(info.get('report_retries') or 0)
            if error and retries < TUI_REPORT_RETRIES:
                # A malformed report is this worker's correctable mistake, not the task's failure (G1, design
                # §4 回收): like the supervisor's correction path, ask it to resend through the normal FIFO
                # delivery (new delivery/fence, same generation); never invent done/evidence, never hold the
                # other slots. Repeated failure still blocks the task. The correction goes to the head of the
                # queue: "your last report" must be answered before any follow-up already queued, else those
                # replies burn the retries and the correction never runs (2026-10-28 ci-1).
                self.f.setf(slot, report_retries=retries + 1)
                self.audit('candidate.invalid', slot=slot, error=error, level='warn',
                           detail={'retry': retries + 1, 'limit': TUI_REPORT_RETRIES})
                if tui_slot(self.slot_cfg(slot), slot):  # official CLIs report in free text (no fleet_result tool)
                    self.queue(slot, 'Your last report is not in the AGENTS.md reply format (' + error
                               + '). Do not redo the work. Resend only the report, one heading per line: '
                               'Done: / Evidence: / Not done: / Blocked: (each heading followed by a colon).', front=True)
                else:
                    self.queue(slot, 'Your last turn ended without a valid fleet_result (' + error + '). Do not '
                               'redo the work. Call fleet_result exactly once, as your only final tool call, with '
                               'done / evidence / not_done / blocked for the instruction you just finished.', front=True)
            elif error:
                self.f.finish('blocked', slot + ': ' + error)
            else:
                if retries:
                    self.f.setf(slot, report_retries=0)
                self.ui(slot, result_card({'status': 'ok', 'worker': self.f.label(slot),
                    'message_id': env['message_id'], 'result': value}, execution_stopped=True))
                self.remember(slot, value)
                self.f.report(slot, dumps(value), info.get('prev_label'))
                if info.get('prev_label'):
                    self.f.setf(slot, prev_label='')

    def opencode_refused(self, slot, ev, env, msg):
        """opencode: the execution ended (the plugin settles only at its end) on a classified model error.
        Provider refusals (FreeTierError / 429 / model unavailable) commit the delivery as that outcome, pause
        dispatch and say which one; never a task failure, never re-sent, never another model. model_lock
        (paid model or cost != 0) commits as failed and fails the task / request. False: not ours (hold)."""
        from fleetmux import fleet as legacy
        errors = [c for c in ev.get('candidates') or [] if isinstance(c, dict) and c.get('tool') == 'model'
                  and c.get('error_class') in OPENCODE_REFUSALS + ('model_lock',)]
        pending = json.loads(self.f.agent(slot).get('oc_error') or 'null') or {}
        if pending.get('delivery_id') == env['delivery_id']:
            errors.append({'tool': 'model', 'error_class': pending['error_class'], 'error': pending.get('error', '')})
        if not errors:
            return False
        self.f.setf(slot, oc_error='')
        cls = 'model_lock' if any(c['error_class'] == 'model_lock' for c in errors) else errors[0]['error_class']
        text = str(next(c for c in errors if c['error_class'] == cls).get('error') or cls)[:1000]
        label, title = self.f.label(slot), OPENCODE_REFUSALS_TEXT.get(cls, '模型锁失败')
        self.release(env, {'opencode_error': cls, 'error': text}, 'failed' if cls == 'model_lock' else cls)
        self.audit('opencode.' + cls, slot=slot, message_id=env['message_id'], delivery_id=env['delivery_id'],
                   request_id=env['request_id'], level='error', error=text,
                   detail={'requeued': False, 'model_changed': False, 'task_failed': cls == 'model_lock'})
        reason = label + ' opencode ' + cls + ': ' + text
        if self.f.service:
            self.publish(env['request_id'], 'failed' if cls == 'model_lock' else 'blocked', worker=label,
                         error=title + '：' + text, opencode_error=cls)
        elif cls == 'model_lock':
            self.f.finish('blocked', slot + ': opencode model_lock: ' + text)
        if cls != 'model_lock' or self.f.service:
            card = routing_card(t('oc.refusal.card', '{label} {title} ({text}). The result was not used, not re-sent, '
                                  'no other model; dispatch is paused for a person to decide.', label=label,
                                  title=t(*OPENCODE_REFUSALS_CARD.get(cls, OPENCODE_REFUSALS_CARD['model_lock'])),
                                  text=text[:300]), kind='notice', slot=label, severity='warn')
            card['metadata']['event_id'] = 'ui-oc-' + cls + '-' + env['message_id']
            self.ui(slot, card)
            self.ui('supervisor', card)
            self.f.pause(reason, kind='opencode', slot=slot)
        return True

    def permission_request(self, slot, ev, info):
        """opencode stopped on a permission request (bridge event: run / slot / generation / session already
        matched in handle). delete class -> the user only (prompt_wait route=user, screen saved; no ruling is
        ever sent for it). other -> fx:escalations['dialog:<slot>:g<N>'] for the supervisor; its ruling goes
        back as one permission_reply UI card (Fleet.dialog_answer), never as a key."""
        from fleetmux import fleet as legacy
        req = ev.get('request') if isinstance(ev.get('request'), dict) else {}
        rid = req.get('id')
        cli = legacy.agent_cli(self.slot_cfg(slot), slot)  # opencode, or dsh (its pane sends the same shape)
        if cli not in ('opencode', 'dsh') or not isinstance(rid, str) or not rid:
            self.audit('permission.rejected', slot=slot, level='warn', error='not an opencode permission request')
            return
        cls = 'other' if ev.get('class') == 'other' and opencode_adapter.permission_class(req) == 'other' else 'delete'
        generation = int(info.get('generation') or 0)
        lease = self.bus.r.hgetall(self.bus.lease_key(slot))
        delivery = ev.get('delivery') if isinstance(ev.get('delivery'), dict) else {}
        current = lease.get('current_delivery') if lease.get('status') == 'received' else ''
        if delivery and delivery.get('delivery_id') != current:
            cls = 'delete'  # bound to a delivery that is not the in-flight one: only the user can tell
        scope = current or ('startup-g' + str(generation) if info.get('state') == 'starting' else '')
        what = opencode_adapter.permission_text(req)[:2000] or '(no text)'
        label = self.f.label(slot)
        path = Path(self.f.cfg['run']) / 'screens' / (slot + '-g' + str(generation) + '-permission-'
                                                       + re.sub(r'[^A-Za-z0-9_-]', '_', rid)[:64] + '.txt')
        path.parent.mkdir(parents=True, exist_ok=True)
        screen = ''
        try:
            read = getattr(self.f.mgr, 'capture_output', None)
            screen = (read(info['pane'], 200) or '') if read and info.get('pane') else ''
        except Exception as exc:  # the request text below is the record; a pane read is extra
            screen = '(pane capture failed: ' + str(exc)[:200] + ')'
        path.write_text('run ' + self.bus.run_id + ' / ' + label + ' (' + slot + ') / g' + str(generation)
                        + ' / session ' + info.get('thread', '') + ' / request ' + rid + ' / class ' + cls
                        + '\n' + what + '\n\n[screen]\n' + screen + '\n', encoding='utf-8')
        record = {'request_id': rid, 'class': cls, 'generation': generation, 'session_id': info.get('thread', ''),
                  'scope': scope, 'screen': str(path)}
        self.audit(cli + '.permission_request', slot=slot, level='warn', detail=dict(record, text=what))
        if not scope:  # a manual turn: the human at the pane is the one asked; nothing waits on the fleet
            card = routing_card(t('perm.manual', '{label}: a manual turn waits at a permission prompt ({what}); the '
                                  'person at the window answers it. Record: {path}.', label=label, what=what[:200],
                                  path=path), kind='notice', slot=label, severity='warn')
            card['metadata']['event_id'] = 'ui-ocperm-' + digest(slot + ':' + rid)[:32]
            self.ui(slot, card)
            return
        self.f.setf(slot, oc_permission=dumps(record), prompt_channel=cli)
        away = self.watchdog.supervisor_away() if cls == 'other' else ''
        if cls == 'delete' or away:
            reason = (cli + ' permission ' + ('(delete class)' if cls == 'delete' else '(supervisor away: '
                      + away + ')') + ': ' + what[:300] + '; only the user answers, nothing sent')
            self.prompt_wait(slot, scope, reason, str(path), kind=cls)
            card = routing_card(t('perm.ask', 'fleet {run} / {label} wants to {act}: {what}. Only the user can approve '
                                  'or deny it in that {cli} window; the program does not answer.', run=self.bus.run_id,
                                  label=label, act=t('perm.act.delete', 'delete / move') if cls == 'delete'
                                  else t('perm.act.run', 'run'), what=what[:300], cli=cli),
                                kind='notice', slot=label, severity='warn')
            card['metadata']['event_id'] = 'ui-ocperm-' + digest(slot + ':' + rid)[:32]
            self.ui(slot, card)
            self.ui('supervisor', card)
            return
        rec = {'kind': 'dialog', 'slot': slot, 'generation': str(generation), 'session_id': info.get('thread', ''),
               'scope': scope, 'delivery_id': current, 'prompt': cli + ' permission ' + str(req.get('permission')
               or '')[:60], 'screen': str(path), 'digest': hashlib.sha256(what.encode()).hexdigest()[:16],
               'keys_approve': [], 'keys_reject': [], 'channel': 'permission_reply', 'request_id': rid}
        self.prompt_escalate(slot, rec, what)
        self.f.setf(slot, prompt_channel=cli)

    def permission_state(self, slot, kind, ev, info):
        """opencode: a reported permission request ended (permission_resolved) or the plugin did not send a
        ruling (permission_refused); run / slot / generation / session already matched in handle. Only
        decided_by 'tui_user' is a user's decision; 'unattributed' (opencode's own reject: shutdown, cascade)
        and 'none' (cleared by an interrupt / instance end) are never the user's nor the supervisor's.
        permission_refused with held_for_user: the request is still pending in opencode and now only the user
        answers it (prompt_wait route=user); the ruling is never re-sent."""
        from fleetmux import fleet as legacy
        rid = ev.get('request_id')
        cli = legacy.agent_cli(self.slot_cfg(slot), slot)
        if cli not in ('opencode', 'dsh') or not isinstance(rid, str) or not rid:
            self.audit('permission.rejected', slot=slot, level='warn', error='not an opencode permission state')
            return
        reason = str(ev.get('reason') or '')[:300]
        cls = 'other' if ev.get('class') == 'other' else 'delete'
        pending = json.loads(info.get('oc_permission') or 'null') or {}
        label = self.f.label(slot)
        if kind == 'permission_resolved':
            decided = ev.get('decided_by')
            decided = decided if decided in ('fleet', 'tui_user', 'unattributed', 'none') else 'unattributed'
            reply = ev.get('reply') if ev.get('reply') in ('once', 'always', 'reject') else None
            waiting = pending.get('request_id') == rid
            self.audit(cli + '.permission_resolved', slot=slot, level='info', detail={
                'request_id': rid, 'class': cls, 'reply': reply, 'decided_by': decided, 'reason': reason,
                'user_decision': decided == 'tui_user', 'closed_wait': waiting})
            if not waiting:  # e.g. the fleet's own ruling (permission_answer cleared it) or a manual-turn request
                return
            esc = self.f.escalation_state()
            key = legacy.DIALOG_KEY + slot + ':g' + str(info.get('generation') or 0)
            if (esc.get(key) or {}).get('request_id') == rid:
                esc.pop(key)
                self.f.save_escalations(esc)
            self.f.setf(slot, prompt_wait='', prompt_wait_at=0, prompt_route='', prompt_channel='', oc_permission='')
            who = {'tui_user': t('perm.by.user', 'the user answered in the {cli} window ({reply})', cli=cli,
                                 reply=reply),
                   'fleet': t('perm.by.fleet', 'the program answered as the supervisor ruled ({reply})', reply=reply),
                   'unattributed': t('perm.by.cli', '{cli} denied it itself (instance closed or denied with others), '
                                     'not a user decision', cli=cli),
                   'none': t('perm.by.none', 'cleared when the run stopped / the instance ended, nobody answered, '
                             'not a user decision')}[decided]
            card = routing_card(t('perm.ended', "{label}'s {cli} permission request {rid} ended: {who} ({reason}).",
                                  label=label, cli=cli, rid=rid, who=who, reason=reason[:200]),
                                kind='notice', slot=label, severity='info' if decided == 'tui_user' else 'warn')
            card['metadata']['event_id'] = 'ui-ocperm-end-' + digest(slot + ':' + rid)[:32]
            self.ui(slot, card)
            self.ui('supervisor', card)
            return
        held = ev.get('held_for_user') is True
        self.audit(cli + '.permission_refused', slot=slot, level='warn', detail={
            'request_id': rid, 'class': cls, 'reason': reason, 'held_for_user': held})
        card = routing_card(t('perm.refused', "{label}'s {cli} permission request {rid}: the program did not answer: "
                              '{reason}{tail}', label=label, cli=cli, rid=rid, reason=reason[:200],
                              tail=t('perm.refused.held', '; the request still waits. Only the user can approve or deny '
                                     'it in that {cli} window.', cli=cli) if held else t('perm.refused.end', '.')),
                            kind='notice', slot=label, severity='warn')
        card['metadata']['event_id'] = 'ui-ocperm-refused-' + digest(slot + ':' + rid + ':' + reason)[:32]
        self.ui(slot, card)
        self.ui('supervisor', card)
        if not held:
            return
        generation = int(info.get('generation') or 0)
        lease = self.bus.r.hgetall(self.bus.lease_key(slot))
        current = lease.get('current_delivery') if lease.get('status') == 'received' else ''
        scope = current or ('startup-g' + str(generation) if info.get('state') == 'starting' else '')
        if not scope:  # a manual turn: the human at the pane is the one asked
            return
        screen = pending.get('screen', '') if pending.get('request_id') == rid else ''
        self.f.setf(slot, oc_permission=dumps({'request_id': rid, 'class': cls, 'generation': generation,
                    'session_id': info.get('thread', ''), 'scope': scope, 'screen': screen}), prompt_channel=cli)
        self.prompt_wait(slot, scope, cli + ' permission ruling not sent (' + reason + '); only the user answers, '
                         'nothing re-sent', screen or '(see the permission request record)', kind=cls)

    def clear_enabled(self, slot):
        return slot in self.f.workers and clear_after_result.enabled(self.slot_cfg(slot), slot)

    def begin_clear(self, slot, *, hook_pid=None):
        """Redis-only gate; typing happens later in clear_tick, never inside a Stop hook."""
        info = self.f.agent(slot)
        self.f.setf(slot, state='clearing', idle_confirmed=0, bridge_available=0, clear_required=1,
                    clear_phase='waiting', clear_deadline=awake.deadline(self.f.cfg.get('clear_timeout', 20)),
                    clear_hook_pid=hook_pid or '', clear_token=digest(self.context['intent_id'] + ':' + slot),
                    clear_old_thread=info.get('thread', ''), clear_idle_since=0)
        self.audit('clear.waiting', slot=slot, generation=info.get('generation'), hook_pid=hook_pid)

    def cleared(self, slot, ev):
        """Prepare a same-generation rebind and metadata update in ONE replayable intent."""
        info = self.f.agent(slot)
        try:
            self.tui_session(slot, ev)
        except ValueError as exc:
            self.f.setf(slot, clear_required=1, clear_conflict=1)
            self.hold(slot, 'invalid clear session metadata: ' + str(exc))
            return
        if ev.get('thread') == info.get('thread'):
            self.audit('clear.duplicate', slot=slot)
            return
        if ev.get('old_thread') != info.get('thread') or ev.get('clear_token', '') != info.get('clear_token', ''):
            # Multiple clears in the same generation can race: ordering alone cannot
            # prove which new session the TUI now uses. Never deliver to either guess.
            self.f.setf(slot, clear_required=1, clear_conflict=1)
            self.hold(slot, 'conflicting same-generation clear notifications; resume required')
            return
        lease = self.f.r.hgetall(self.bus.lease_key(slot))
        if lease.get('current_delivery'):
            self.f.setf(slot, clear_required=1, clear_conflict=1)
            self.hold(slot, 'SessionStart(clear) during an in-flight delivery; session binding unconfirmed')
            return
        if info.get('state') not in ('idle', 'clearing', 'held'):
            self.audit('clear.rejected', slot=slot, error='slot has another lifecycle owner; resume required')
            return
        if info.get('state') == 'clearing' and awake.passed(info.get('clear_deadline') or 0):
            self.hold(slot, 'clear confirmation deadline expired')
            return
        if (lease.get('status') != 'idle' or lease.get('session_id') != info.get('thread')
                or lease.get('generation') != str(ev['generation'])
                or self.bus.r.xpending(self.bus.inbox_key(slot), self.bus.slot_group(slot))['pending']):
            self.f.setf(slot, clear_required=1, clear_conflict=1)
            self.hold(slot, 'clear rebind requires matching idle lease and drained PEL')
            return
        fence = int(lease['fence'])
        self.context['rebind_gate'] = dict(lease=self.bus.lease_key(slot), inbox=self.bus.inbox_key(slot),
            group=self.bus.slot_group(slot), run=self.bus.run_id, slot=slot, generation=ev['generation'],
            old_session=info['thread'], fence=fence)
        self.context['authorization'] = {'fx:agent:' + slot: {
            'thread': info['thread'], 'generation': str(ev['generation']), 'state': info['state']}}
        self.effects.hset(self.bus.lease_key(slot), {'session_id': ev['thread'], 'fence': fence + 1})
        self.f.setf(slot, thread=ev['thread'], transcript=ev['transcript'], ctx_used=0, ctx_window=0,
                    state='clearing', clear_phase='confirmed', clear_required=1, clear_conflict=0,
                    clear_hook_pid=ev.get('hook_pid') or '', clear_idle_since=0, idle_confirmed=0, bridge_available=0,
                    clear_deadline=info.get('clear_deadline') if info['state'] == 'clearing'
                        else awake.deadline(self.f.cfg.get('clear_timeout', 20)),
                    watch_state='idle', stall_escapes=0, watch_since=0, quiet_since=0, bg_wait='')
        self.audit('slot_rebound', slot=slot, generation=ev['generation'], old_session=info['thread'],
                   session_id=ev['thread'], fence=fence + 1)

    def clear_tick(self, now):
        """No busy TUI input: require hook process exit + two consecutive idle screen observations.

        The second observation is a later daemon tick. Unknown/missing evidence times out
        to hold. Reserve the paste durably before typing; restart never repeats it blindly.
        SessionStart(clear) also has to return before the next business delivery is allowed.
        """
        for slot in self.f.workers:
            info = self.f.agent(slot)
            if not self.clear_enabled(slot) or info.get('state') != 'clearing':
                continue
            if now >= float(info.get('clear_deadline') or 0):  # now: awake time (the caller passes awake.now())
                self.transaction('clear-timeout-' + uuid.uuid4().hex,
                                 lambda slot=slot: self.hold(slot, 'clear confirmation deadline expired'))
                continue
            lease = self.bus.r.hgetall(self.bus.lease_key(slot))
            if lease.get('current_delivery') or info.get('clear_phase') == 'pasted':
                continue
            if not clear_after_result.hook_exited(info.get('clear_hook_pid')):
                self.f.setf(slot, clear_idle_since=0)
                continue
            pane = info.get('pane')
            try:
                idle = bool(pane) and self.f.mgr.get_pane_status(pane) is not None and clear_after_result.idle_prompt(
                    self.f.mgr.capture_output(pane, lines=20))
            except Exception as exc:
                idle = False
                self.audit('clear.capture_failed', slot=slot, error=str(exc)[:300])
            if not idle:
                self.f.setf(slot, clear_idle_since=0)
                continue
            since = float(info.get('clear_idle_since') or 0)
            if not since or now - since < 0.25:
                if not since:
                    self.f.setf(slot, clear_idle_since=now)
                continue
            if info.get('clear_phase') == 'confirmed':
                self.transaction('clear-ready-' + uuid.uuid4().hex, lambda slot=slot: self.finish_clear(slot))
                continue
            # §48: /clear is never typed (validate_config refuses clear_after_result); a slot still here holds.
            self.f.setf(slot, clear_phase='pasted', clear_idle_since=0)
            self.transaction('clear-paste-failed-' + uuid.uuid4().hex, lambda slot=slot: self.hold(
                slot, '/clear not typed: clear_after_result is retired; ' + NO_TYPING))

    def finish_clear(self, slot):
        self.f.setf(slot, state='idle', idle_confirmed=1, bridge_available=0, request='',
                    clear_required=0, clear_phase='', clear_token='', clear_hook_pid='', clear_idle_since=0)
        self.audit('clear.ready', slot=slot)

    def stop_failure(self, slot, ev, *, status='failed'):
        """A CLI failure ends the turn: fail its bound request now, without wall/tool replay."""
        from fleetmux.fleet import REQ
        env = ev.get('delivery')
        if not isinstance(env, dict) or env.get('slot') != slot or env.get('session_id') != ev.get('thread'):
            self.audit('stop_failure.ignored', slot=slot, error='no matching delivery')
            return
        try:
            msg, lease = self.authorization(env)
        except (BusError, ValueError, KeyError) as exc:
            self.audit('stop_failure.rejected', slot=slot, error=str(exc))
            return
        if (lease.get('status') not in ('received', 'uncertain', 'expired')
                or msg['body'].get('purpose') != 'worker' or self.f.agent(slot).get('clear_conflict') == '1'):
            self.audit('stop_failure.ignored', slot=slot, error='not an in-flight service worker request')
            return
        self.context['authorization'] = {self.bus.lease_key(slot): {
            k: str(lease[k]) for k in ('current_delivery', 'generation', 'session_id', 'fence', 'status')}}
        self.context['authorization'][self.bus.msg_key(env['message_id'])] = {
            k: str(msg[k]) for k in IDENTITY if k != 'request_id'}
        self.context['stop_gate'] = dict(inbox=self.bus.inbox_key(slot), group=self.bus.slot_group(slot),
                                         stream_id=env['stream_id'], consumer=env['consumer'])
        self.context.update(bound_slot=slot, bound_request=env['request_id'], bound_message=env['message_id'],
                            bound_delivery=env['delivery_id'], clear_hook_pid=ev.get('hook_pid'))
        from fleetmux.fleet import stop_failure_wall
        wall = stop_failure_wall(ev)
        if wall:  # design section 23: a safeguards refusal is a wall here too (requeued once, request not failed)
            return self.wall_commit(slot, env, msg, wall['line'], 'stop_failure', wall)
        error = str(ev.get('error') or 'Claude StopFailure')[:4000]
        self.release(env, {'error': error}, 'failed')
        if self.f.r.hgetall(REQ + env['request_id']):
            self.publish(env['request_id'], status, worker=self.f.label(slot), error=error)
        else:  # already publicly timed out; keep its terminal result, still clear after this turn ends
            self.begin_clear(slot, hook_pid=ev.get('hook_pid'))
        self.audit('stop_failure.failed', slot=slot, request_id=env['request_id'], error=error)

    def api_error_retry(self, slot, ev):
        """Task-mode Claude hook-channel worker or supervisor (§24): StopFailure ended the turn of its in-flight
        delivery, so its execution has stopped. Commit it as not adopted and queue the same message again at the
        head of this slot's queue (``replay`` attempt, capped by max_attempts). Only this slot is held until the
        backoff ends (``tick``); the fleet goes on. Used up: held; a worker's supervisor is told, a supervisor is
        escalated upwards (``held_escalate``: parent fleet / root channels). The advisor takes §27's fault branch
        (held, its consultation falls back, restarted in place): a consultation is never re-run."""
        from fleetmux.fleet import REQ
        supervisor, is_advisor = slot == 'supervisor', slot == advisor.SLOT
        info = self.f.agent(slot) if slot in self.f.names and (slot in self.f.workers or supervisor or is_advisor) else {}
        env = ev.get('delivery')
        if (not info or not (self.hooked(slot) or is_advisor)
                or ev.get('run_id') != self.bus.run_id or ev.get('generation') != int(info.get('generation', -1))
                or ev.get('thread') != info.get('thread')):
            self.audit('stop_failure.ignored', slot=slot or '', error='not this session of a hook-channel agent')
            return
        if not isinstance(env, dict) or env.get('slot') != slot or env.get('session_id') != ev.get('thread'):
            self.audit('stop_failure.ignored', slot=slot, error='no matching delivery')
            return
        try:
            msg, lease = self.authorization(env)
        except (BusError, ValueError, KeyError) as exc:
            self.audit('stop_failure.rejected', slot=slot, error=str(exc))
            return
        error = ' '.join(' '.join(str(ev.get(k) or '') for k in ('error', 'text')).split()) or 'Claude StopFailure'
        if is_advisor:
            if lease.get('status') in ('received', 'uncertain', 'expired'):
                self.hold(slot, 'API error: ' + error[:200], pause=False)  # §27: advisor_fault, never a pause
                self.audit('stop_failure.advisor', slot=slot, message_id=env['message_id'], level='warn', error=error[:300])
            return
        kinds = ('supervisor', 'followup') if supervisor else ('worker', 'continuation')
        if (lease.get('status') not in ('received', 'uncertain', 'expired') or msg.get('kind') not in kinds
                or msg['body'].get('purpose') == 'handoff' or info.get('state') not in ('busy', 'held')):
            self.audit('stop_failure.ignored', slot=slot, error='not an in-flight ' + ('supervisor message' if supervisor
                       else 'worker instruction'))
            return
        self.context['authorization'] = {self.bus.lease_key(slot): {
            k: str(lease[k]) for k in ('current_delivery', 'generation', 'session_id', 'fence', 'status')}}
        self.context['authorization'][self.bus.msg_key(env['message_id'])] = {
            k: str(msg[k]) for k in IDENTITY if k != 'request_id'}
        self.context['stop_gate'] = dict(inbox=self.bus.inbox_key(slot), group=self.bus.slot_group(slot),
                                         stream_id=env['stream_id'], consumer=env['consumer'])
        from fleetmux.fleet import stop_failure_wall
        wall = stop_failure_wall(ev)
        if wall:
            # design section 23: a safeguards refusal is a wall, decided BEFORE the same-model retry, the refusal /
            # service branches and the regeneration counter: the slot is never held, nothing below runs.
            return self.wall_commit(slot, env, msg, wall['line'], 'stop_failure', wall)
        self.release(env, {'stop_failure': error[:4000]}, 'failed')  # not adopted, never a report
        self.midturn_clear(slot)
        refusal = bool(ev.get('refusal'))
        if self.f.service and not (supervisor and not self.f.r.hgetall(REQ + env['request_id'])):
            return self.service_stop_failure(slot, env, info, error, refusal)
        # task mode, or a service supervisor's message with no live request behind it (a note): re-sent, not lost
        cap, attempt, now = int(self.f.cfg['max_attempts']), int(msg.get('replay') or 1), time.time()
        lost = session_env.route_closed(self.f, slot, info)  # applet route port closed: no attempt spent, held
        label, short = self.f.label(slot), error[:200]
        self.f.setf(slot, state='held', idle_confirmed=0, bridge_available=0, stopfail_at=now,
                    stopfail_g=info.get('generation', '0'), stopfail_retry_at=0)
        adopted = supervisor and subfleet.adopted(self.f.cfg, slot, info.get('generation'))
        if supervisor and refusal and adopted:
            # The session refused: it takes no more work. The parent owns this pane, so no new session here: held,
            # not re-sent, and the parent fleet's supervisor decides (it can take the slot back / rotate it).
            text = t('stopfail.sub_refusal', '{label} model refusal ({short}); the sub-fleet supervisor slot is held, '
                     'not re-sent to the same session, reported to the parent fleet.', label=label, short=short)
            self.audit('execution.held', slot=slot, message_id=env['message_id'], level='error',
                       error='model refusal: ' + short, detail={'refusal': True, 'paused': False})
            self.audit('stop_failure.refused', slot=slot, message_id=env['message_id'], level='error')
            self.held_notices.append((slot, 'model refusal (' + short + '); the refused message was not re-sent to '
                                      'that session, and the sub-fleet cannot replace a pane its parent owns',
                                      {'reason': 'refusal', 'refusal': True}))
        elif attempt < cap or lost:
            wait = STOP_FAILURE_BACKOFF[min(attempt, len(STOP_FAILURE_BACKOFF)) - 1]
            mid = self.requeue_after_stop_failure(slot, env, msg, attempt + (not lost), now, supervisor)
            self.f.setf(slot, stopfail_retry_at=awake.deadline(wait))
            if supervisor and refusal:
                self.f.setf(slot, fresh_pending=1, stopfail_fresh=1)  # §37: the retry starts a fresh session
            text = t('stopfail.retry', '{label} API error ({short}); automatic retry #{n} in {wait} s. Only this slot '
                     'is held; the others go on.', label=label, short=short, wait=wait, n=attempt)
            self.audit('execution.held', slot=slot, message_id=env['message_id'], level='warn', error='API error: ' + short,
                       detail={'retry_in_s': wait, 'attempt': attempt + 1, 'max_attempts': cap, 'paused': False})
            self.audit('stop_failure.retry', slot=slot, message_id=env['message_id'], delivery_id=env['delivery_id'],
                       level='warn', detail={'requeued': mid, 'attempt': attempt + 1, 'retry_in_s': wait,
                                             'refusal': refusal})
        else:
            text, note = _both('stopfail.exhausted', '{label} API error ({short}), still failing after {n} retries '
                               '(max-attempts {cap}); the slot is held and gets no new work, the others go on. Once it '
                               'recovers, end one normal turn in that pane to release it.', label=label, short=short,
                               n=attempt - 1, cap=cap)
            self.audit('execution.held', slot=slot, message_id=env['message_id'], level='error', error='API error: ' + short,
                       detail={'attempt': attempt, 'max_attempts': cap, 'paused': False})
            self.audit('stop_failure.exhausted', slot=slot, message_id=env['message_id'], delivery_id=env['delivery_id'],
                       level='error', detail={'attempt': attempt, 'max_attempts': cap})
            if supervisor and self.regen_on(slot, info):
                # §24 supervisor regeneration owns it from here: the message stays queued once at the head (no bus
                # deadline, so a long network wait cannot expire it), no timed release into the same session, and
                # held_watch_tick replaces the session with the next generation. No escalation now: only a used-up
                # regeneration budget is reported outward.
                self.requeue_after_stop_failure(slot, env, msg, attempt + 1, now, supervisor,
                                                lead=REGEN_RESUME_SUPERVISOR, deadline_at=NO_DEADLINE_MS)
                self.f.setf(slot, stopfail_retry_at=0, regen_due=awake.now())
                more = _both('stopfail.regen', ' The supervisor message is kept; the daemon restarts the supervisor in '
                             'a new generation.')
                text, note = text + more[0], note + more[1]
                self.audit('supervisor_regen.due', slot=slot, level='warn', message_id=env['message_id'],
                           detail={'reason': 'stop_failure_exhausted', 'attempt': attempt})
            elif supervisor:
                # A supervisor cannot be told about itself: escalated upwards outside this intent (held_watch_tick).
                # Its message is not dropped: its turns are decisions nobody else can make, so it keeps being retried
                # at the longest backoff and the run goes on as soon as the API answers again.
                wait = STOP_FAILURE_BACKOFF[-1]
                self.requeue_after_stop_failure(slot, env, msg, attempt + 1, now, supervisor)
                self.f.setf(slot, stopfail_retry_at=awake.deadline(wait))
                more = _both('stopfail.sup_retry', ' The supervisor message is kept; retry in {wait} s, reported.',
                             wait=wait)
                text, note = text + more[0], note + more[1]
                self.held_notices.append((slot, 'API error retries used up (' + str(attempt) + ' of max-attempts '
                                          + str(cap) + '): ' + short + '; its message is retried every ' + str(wait)
                                          + ' s until the API answers', {'reason': 'stop_failure_exhausted'}))
            else:
                if self.f.task().get('status') == 'running':
                    self.f.queue('supervisor', '[' + label + ' report]\nAPI ERROR: ' + label + ' failed ' + str(attempt)
                                 + ' attempt(s) (max-attempts ' + str(cap) + ') on an API error: ' + short + '. Its '
                                 'instruction was NOT re-run again; it may be partly done. The slot is held (no new work) '
                                 'until its session ends a normal turn; the other workers go on. Dispatch the work '
                                 'elsewhere, or reply BLOCKED if a human is needed.\n\nReply with the JSON format from AGENTS.md.')
                self.stopfail_notices.append(note)
        card = routing_card(text, kind='notice', slot=label, severity='warn')
        card['metadata']['event_id'] = 'ui-stopfail-' + digest(slot + ':' + env['message_id'])[:32]
        self.ui(slot, card)
        if not supervisor:
            self.ui('supervisor', card)

    def requeue_after_stop_failure(self, slot, env, msg, replay, now, supervisor=False, lead=None, deadline_at=None):
        """The same message again, at the head of the slot's queue, with the API-error note in front (§24)."""
        lead = lead or (STOP_FAILURE_RESUME_SUPERVISOR if supervisor else STOP_FAILURE_RESUME)
        body = msg['body'].get('text') or ''
        for old in (STOP_FAILURE_RESUME_SUPERVISOR, REGEN_RESUME_SUPERVISOR):
            body = body[len(old):] if body.startswith(old) and old != lead else body
        self.queue_index += 1
        mid = 'm-' + digest(self.context['intent_id'] + ':' + str(self.queue_index))[:32]
        again = dict(msg['body'], text=body if body.startswith(lead) else lead + body)
        self.effects.hset(self.bus.msg_key(mid), dict(run_id=self.bus.run_id, slot=slot, message_id=mid,
            body=_json(again), request_id=msg['request_id'], kind=msg['kind'], status='queued',
            deadline_at=deadline_at or msg['deadline_at'], created_at=int(now*1000), attempt=0, replay=replay,
            stopfail_of=env['message_id']))
        retained = {k: msg[k] for k in ('midturn_splits', 'continues', 'midturn_instruction', 'original_kind') if k in msg}
        if retained:
            self.effects.hset(self.bus.msg_key(mid), retained)
        self.effects.lpush(self.bus.pending_key(slot), mid)  # ahead of anything queued meanwhile
        self.audit('queued', slot=slot, message_id=mid, request_id=msg['request_id'], detail=again)
        return mid

    def service_stop_failure(self, slot, env, info, error, refusal):
        """Service mode (§24): the StopFailure ended the turn, so its request fails now (never re-sent here; the
        requester owns retries) instead of timing out and pausing the fleet. Only this slot is held for a
        backoff (``stopfail_tick``); a model refusal also gives it a fresh session for the next request (§37).
        ``service_stopfail_max`` consecutive failures: the slot stays held, the supervisor and parent are told."""
        from fleetmux import fleet as legacy
        label, short, now = self.f.label(slot), error[:200], time.time()
        cap = int(self.f.cfg.get('service_stopfail_max') or SERVICE_STOPFAIL_MAX)
        streak = int(info.get('stopfail_streak') or 0) + (not session_env.route_closed(self.f, slot, info))
        what = '模型拒答' if refusal else 'API 错误'  # audit / notices; the card says it in the interface language
        what_card = t('stopfail.refusal', 'model refusal') if refusal else t('stopfail.api_error', 'API error')
        if self.f.r.hgetall(legacy.REQ + env['request_id']):
            self.publish(env['request_id'], 'failed', worker=label,
                         error=('model refusal' if refusal else 'API error') + ': ' + short)
        self.f.setf(slot, state='held', idle_confirmed=0, bridge_available=0, stopfail_at=now,
                    stopfail_g=info.get('generation', '0'), stopfail_retry_at=0, stopfail_streak=streak,
                    **({'fresh_pending': 1, 'stopfail_fresh': 1} if refusal else {}))
        detail = {'streak': streak, 'max': cap, 'refusal': refusal, 'paused': False}
        if streak < cap:
            wait = STOP_FAILURE_BACKOFF[min(streak, len(STOP_FAILURE_BACKOFF)) - 1]
            self.f.setf(slot, stopfail_retry_at=awake.deadline(wait))
            text = t('stopfail.service', '{label} {what} ({short}); the request is failed, not re-sent; the slot is '
                     'released in {wait} s{fresh}. Only this slot is held; the others go on ({streak} in a row, '
                     'limit {cap}).', label=label, what=what_card, short=short, wait=wait, streak=streak, cap=cap,
                     fresh=t('stopfail.service.fresh', ', next one in a new session') if refusal else '')
            self.audit('execution.held', slot=slot, message_id=env['message_id'], level='warn',
                       error=what + ': ' + short, detail=dict(detail, retry_in_s=wait))
        else:
            text = t(*SERVICE_EXHAUSTED, label=label, what=what_card, streak=streak, cap=cap, short=short)
            note = tr('zh-Hans', *SERVICE_EXHAUSTED, label=label, what=what, streak=streak, cap=cap,
                      short=short)  # tg / parent notice: unchanged
            self.audit('stop_failure.exhausted', slot=slot, message_id=env['message_id'], level='error',
                       error=what + ': ' + short, detail=detail)
            self.stopfail_notices.append(note)
        card = routing_card(text, kind='notice', slot=label, severity='warn')
        card['metadata']['event_id'] = 'ui-stopfail-' + digest(slot + ':' + env['message_id'])[:32]
        self.ui(slot, card)
        self.ui('supervisor', card)

    def stopfail_tick(self, slot, info, now):
        """Inside ``expire``: end a StopFailure backoff hold (lease idle) when it is due, or earlier when a Stop
        hook of the same session waits again (a normal turn ended there: the API answers again)."""
        if info.get('stopfail_g') != info.get('generation') or info.get('link_lost') == '1':
            return
        if session_env.route_closed(self.f, slot, info):
            return  # applet route port closed: stays held, nothing re-delivered until it answers
        due = float(info.get('stopfail_retry_at') or 0)
        armed = None
        if now >= float(info.get('stopfail_at') or 0) + claude_hook.ARMED_TTL:  # a stale flag expires by then
            try:
                armed = json.loads(self.bus.r.get(self.hook_keys(slot, int(info['generation']))['armed']) or 'null')
            except (ValueError, TypeError):
                armed = None
        turn = (isinstance(armed, dict) and str(armed.get('generation')) == info.get('generation')
                and armed.get('session_id') == info.get('thread'))
        if not turn and not (due and awake.passed(due)):
            return
        self.f.setf(slot, state='idle', idle_confirmed=1, stopfail_at='', stopfail_retry_at='', stopfail_g='')
        self.audit('stop_failure.released', slot=slot, level='warn',
                   detail={'reason': 'a normal turn ended in the same session' if turn else 'backoff over',
                           'retry': bool(self.bus.r.llen(self.bus.pending_key(slot)))})

    def publish(self, rid, status, replace=False, **fields):
        from fleetmux import fleet as legacy
        if self.effects is None:
            return self.transaction('pub-' + digest(rid)[:32] + ('-replace' if replace else ''),
                                    lambda: self.publish(rid, status, replace=replace, **fields))
        # Tombstones are retained past public RESULT_TTL, so expiry never authorizes a rerun. ``replace`` (a delivery
        # gate recheck of a blocked job) is the one deliberate second result.
        tombstone = self.prefix + ':public:' + digest(rid)
        if self.f.r.get(tombstone) and not replace:
            return
        req = self.f.r.hgetall(legacy.REQ + rid)
        if self.f.service:  # plan F': a [blocked] question about a request that is now terminal is moot
            esc = self.f.escalation_state()
            moot = [k for k, v in esc.items() if v.get('kind') == 'blocked' and v.get('request') == rid]
            if moot:
                self.f.save_escalations({k: v for k, v in esc.items() if k not in moot})
                self.audit('blocked.moot', request_id=rid, detail={'tasks': moot, 'status': status})
        now, awake_now = time.time(), awake.now()  # the request's clock is awake seconds (fleetmux/awake.py)
        req = midturn.resume_clock(req, awake_now)
        rec = dict(id=rid, status=status, at=now,
                   elapsed_s=round(midturn.effective_age(req, awake_now), 1),
                   clock_paused_ms=int(req.get('clock_paused_ms') or 0), **fields)
        raw = dumps(rec)
        self.f.r.set(tombstone, raw)
        self.f.r.set(legacy.RESULT + rid, raw, ex=legacy.RESULT_TTL)
        self.f.r.rpush(legacy.RESULTS, raw)
        self.f.r.ltrim(legacy.RESULTS, -1000, -1)
        self.f.r.delete(legacy.REQ + rid)
        self.f.r.lrem(legacy.REQQ, 0, rid)
        stopped = status in ('ok', 'invalid', 'skipped')
        spec = json.loads(req.get('spec') or '{}')
        payload = spec.get('payload')
        title = payload.get('message') if isinstance(payload, dict) else None
        title = title if isinstance(title, str) else spec.get('instruction')
        title = title.splitlines()[0][:120] if isinstance(title, str) and title else None
        card = result_card(rec, task_title=title, execution_stopped=True if stopped else None, now=now)
        if status == 'ok' and isinstance(fields.get('result'), dict) and isinstance(fields['result'].get('reply'), str):
            card['render'] = sanitize_text(t('card.worker_prefix', '{worker}: ', worker=fields.get('worker', 'worker'))) \
                + card['render']
        self.ui('supervisor', card)
        for slot in self.f.workers:
            if self.f.label(slot) == fields.get('worker'):
                self.ui(slot, card)
                # Only deliveries which reached this worker need clearing. Queued-only
                # timeouts must not disturb an unrelated active request on that slot.
                info = self.f.agent(slot)
                delivered = (info.get('request') == rid
                             or self.context.get('bound_slot') == slot and self.context.get('bound_request') == rid)
                delivered = delivered and not applet_lease.skip_fresh(self.f, slot)  # leased: one session per lease
                if self.clear_enabled(slot) and delivered:
                    self.begin_clear(slot, hook_pid=self.context.get('clear_hook_pid'))
                elif delivered and self.slot_cfg(slot).get('fresh_after_result') is True:
                    self.f.setf(slot, fresh_pending=1, result_fresh=1)  # §51: rotated once idle (Fleet.idle_rotate)
        self.audit('result', request_id=rid, slot=self.context.get('bound_slot') or self.f.resolve(fields.get('worker', 'supervisor')),
                   message_id=self.context.get('bound_message', ''), delivery_id=self.context.get('bound_delivery', ''),
                   level='info' if status=='ok' else 'error', detail={'public_result': rec, 'request': req})

    def heartbeat_check(self, slot, info):
        """§35: a bridge slot whose current-generation bridge has beaten once and then stopped lost its link.
        A bridge that never beat (older code) is never judged by this."""
        if (info.get('state') not in ('idle', 'busy') or info.get('runtime_ready') != '1'
                or tui_slot(self.slot_cfg(slot), slot) or time.time() < self.hb_grace_until):
            return
        beat = self.bus.r.get(heartbeat_key(self.bus, slot))  # bridge-owned key: read it live, not via effects
        generation = str(info.get('generation', '0'))
        if beat == generation:
            if info.get('bridge_hb_g') != generation:
                self.f.setf(slot, bridge_hb_g=generation)
        elif info.get('bridge_hb_g') == generation:
            self.link_lost(slot, 'bridge heartbeat stopped')

    def link_lost(self, slot, why):
        """§35: the bridge link is gone. Hold only this slot; the watchdog restarts its session
        (kill + next generation from a handoff note, capped). The work is never re-run automatically."""
        self.hold(slot, LINK_LOST + str(why)[:300], pause=False)
        if slot != advisor.SLOT:  # §27: the advisor's hold already restarts it in place (advisor_tick)
            self.f.setf(slot, link_lost=1)

    def requeue_hold(self, slot, reason, cause, msg=None, ev=None):
        """§24 held requeue: when the request interrupted on ``slot`` may be re-run (held_requeue.eligible), hold only
        this slot (no public result, no pause) and mark it for held_requeue.step. False: the caller holds as before."""
        if slot == 'supervisor' or slot == advisor.SLOT:
            return False
        if msg is None:
            mid = (self.effects or self.bus.r).hget(self.bus.lease_key(slot), 'message_id')
            try:
                msg = self.bus.message(mid) if mid else {}
            except (BusError, ValueError, KeyError):
                return False
        if not held_requeue.eligible(self.f, slot, msg, cause, self.held_knobs(), ev=ev)[0]:
            return False
        self.hold(slot, reason, pause=False)
        held_requeue.mark(self.f, slot, msg, cause)
        self.audit('held_requeue.held', slot=slot, level='warn', message_id=msg.get('message_id', ''),
                   request_id=msg.get('request_id', ''), detail={'cause': cause})
        return True

    def hold(self, slot, reason, public_status=None, pause=True):
        """``pause=False``: only this slot is held (no new work for it); the fleet keeps working."""
        from fleetmux import fleet as legacy
        r = self.effects or self.bus.r
        state = r.hgetall(self.bus.lease_key(slot))
        mid = state.get('message_id')
        self.midturn_clear(slot)
        # hold_pauses: does this hold pause the fleet on its own (pause review, ruling 30: it blocks another lift)
        self.f.setf(slot, state='held', idle_confirmed=0, bridge_available=0, hold_pauses=1 if pause else 0)
        if mid:
            r.hset(self.bus.lease_key(slot), mapping={'status': 'uncertain'})
            r.hset(self.bus.msg_key(mid), mapping={'status': 'uncertain', 'uncertain_reason': reason})
            rid = r.hget(self.bus.msg_key(mid), 'request_id')
            if public_status and r.hgetall(legacy.REQ + rid):
                self.publish(rid, public_status, worker=self.f.label(slot), error=reason)
        self.audit('execution.held', slot=slot, message_id=mid or '', level='error', error=reason)
        if slot == advisor.SLOT:
            # §27: an advisor fault never pauses the fleet nor holds another slot. Its consultation falls back
            # (the waiting slot sees the uncertain message) and advisor_tick restarts the tab in place.
            self.f.setf(slot, advisor_fault=reason[:300])
            return
        if public_status is None:
            text = (t('held.unconfirmed.paused', 'Execution state unconfirmed; dispatch paused; the original run is '
                      'not confirmed stopped.') if pause else
                    t('held.unconfirmed.slot', 'Execution state unconfirmed; the slot is held and gets no new work, the '
                      'others go on; the original run is not confirmed stopped.'))
            card = routing_card(self.f.label(slot) + ' ' + text + t('held.see_control', ' Details in control.'),
                                kind='notice', slot=self.f.label(slot), severity='warn')
            card['metadata']['event_id'] = 'ui-held-' + digest(slot + ':' + (mid or 'startup'))[:32]
            self.ui(slot, card)
            if slot != 'supervisor':
                self.ui('supervisor', card)
        if pause:
            prior = self.f.r.hgetall(legacy.SERVICE)
            self.f.pause(slot + ': ' + reason, kind='slot_hold', slot=slot, message_id=mid or '',
                         generation=int(self.f.agent(slot).get('generation') or 0))
            self.handoff_pause_arm(slot, mid, prior)
            # §24 held watchdog: this hold owns the pause only when the run was not paused before; held_watch_tick
            # may lift it later (same fx:service.at, never past a brake), never anyone else's pause.
            self.f.setf(slot, hold_pause=dumps({'at': self.f.r.hget(legacy.SERVICE, 'at'), 'message_id': mid or '',
                        'generation': int(self.f.agent(slot).get('generation') or 0)})
                        if prior.get('paused') != '1' else '')

    def handoff_pause_arm(self, slot, mid, prior):
        """Mark a paused hold of this slot's own handoff delivery as the owner of the pause (handoff_resume_tick).
        Ours only when the fleet was not paused before (or this is the same handoff's second hold on the same
        pause) and the old pane is no tmux-backed tab (killing that tab does not prove its CLI gone)."""
        from fleetmux.fleet import SERVICE
        info = self.f.agent(slot)
        hid = info.get('handoff_id') or ''
        mark = handoff_mark(info)
        ours = (hid and mid == 'm-handoff-' + hid and info.get('pane')
                and (prior.get('paused') != '1' or mark.get('hid') == hid and mark.get('at') == prior.get('at'))
                and getattr(self.f.mgr, 'tmux_view_info', lambda pane: None)(info['pane']) is None)
        if ours:
            self.f.setf(slot, handoff_pause=dumps({'hid': hid, 'at': self.f.r.hget(SERVICE, 'at'),
                        'generation': int(info['generation']), 'label': self.f.label(slot)}))
        elif info.get('handoff_pause'):
            self.f.setf(slot, handoff_pause='')

    def handoff_resume_tick(self):
        for slot in self.f.names:
            if self.f.agent(slot).get('handoff_pause'):
                self.transaction('handoff-resume-' + uuid.uuid4().hex, lambda slot=slot: self.handoff_pause_lift(slot))
            if self.f.agent(slot).get('prompt_pause'):
                self.transaction('prompt-resume-' + uuid.uuid4().hex, lambda slot=slot: self.prompt_pause_lift(slot))

    def handoff_pause_lift(self, slot):
        """Resume the pause a handoff hold set (handoff_pause_arm) once it is safe: still that very pause
        (every pause() rewrites fx:service.at), the run is live, this rotation retired the old generation
        (pane killed and gone, the handoff delivery confirmed stopped), the successor is READY on its own lease,
        and no brake. Not yet: the mark stays for the next tick. Stale or someone else's pause: mark dropped."""
        from fleetmux.fleet import SERVICE
        info = self.f.agent(slot)
        mark = handoff_mark(info)
        service = self.f.r.hgetall(SERVICE)
        generation = int(info.get('generation') or 0)
        if (not mark or service.get('paused') != '1' or service.get('at') != mark['at'] or not self.f.running()
                or generation > mark['generation'] + 1):
            self.f.setf(slot, handoff_pause='')
            return
        mid = 'm-handoff-' + mark['hid']
        msg = self.bus.r.hgetall(self.bus.msg_key(mid))
        lease = self.bus.r.hgetall(self.bus.lease_key(slot))
        if not (generation == mark['generation'] + 1 and not info.get('handoff_id')
                and info.get('launch_ready') == '1' and not info.get('retired_pane')
                and msg.get('stop_confirmed') == '1' and msg.get('outcome') == 'cancelled'
                and info.get('state') == 'idle' and info.get('runtime_ready') == '1' and not info.get('prompt_wait')
                and lease.get('generation') == str(generation) and lease.get('session_id') == info.get('thread')
                and lease.get('status') == 'idle' and not lease.get('current_delivery')):
            return
        self.f.setf(slot, handoff_pause='')
        label = self.f.label(slot)
        self.pause_owner_resume(slot, mid, {'from_generation': mark['generation'], 'to_generation': generation,
                                            'prev_label': mark.get('label') or slot, 'label': label,
                                            'pause_reason': service.get('reason', ''), 'paused_at': mark['at']},
                                t('resume.handoff', '{prev}: the handoff-timeout pause is lifted: the old generation '
                                  'is closed and confirmed stopped, the new generation {label} is READY, dispatch '
                                  'resumes.', prev=mark.get('label') or slot, label=label), mark['hid'])

    def startup_late(self, info, ev):
        """The mark of a READY that came after this generation's startup deadline set the slot ``dead`` (a sleep
        the §35 deadline shift did not cover), else None. Only a clean READY counts."""
        try:
            mark = json.loads(info.get('startup_timeout') or '{}')
        except ValueError:
            return None
        if (info.get('state') != 'dead' or not isinstance(mark, dict)
                or mark.get('generation') != str(info.get('generation', '0')) or info.get('launch_ready', '1') != '1'
                or ev.get('failed') or ev.get('aborted') or not ready_reply(ev.get('text', ''))):
            return None
        return mark

    def startup_late_lift(self, slot, mark):
        """The slot is READY after all: drop the mark, and lift the pause its startup timeout set when that pause is
        still the only one (fx:service.at unchanged since, the run live); a later pause stays with its owner."""
        from fleetmux.fleet import SERVICE
        self.f.setf(slot, startup_timeout='')
        service = self.f.r.hgetall(SERVICE)
        mine = bool(mark.get('at')) and service.get('paused') == '1' and service.get('at') == mark['at']
        self.audit('startup.late_ready', slot=slot, level='warn',
                   detail={'generation': mark['generation'], 'paused_at': mark.get('at') or '', 'lifts_pause': mine})
        if mine and self.f.running():
            label = self.f.label(slot)
            self.pause_owner_resume(slot, '', {'source': 'startup_timeout', 'label': label,
                                               'pause_reason': service.get('reason', ''), 'paused_at': mark['at']},
                                    label + ' became READY after its startup deadline (the machine probably slept); '
                                    'the pause its startup timeout set is lifted and dispatch resumes.',
                                    'startup:' + mark['at'])

    def pause_owner_resume(self, slot, mid, detail, text, card_key):
        """Shared tail of the owner-lifted pauses (handoff hold, dialog wait): never past a brake / budget line;
        else resume, audit execution.auto_resumed and tell the slot and the supervisor."""
        from fleetmux import brake
        from fleetmux.fleet import BRAKE
        state = self.f.r.hgetall(BRAKE)
        budget = self.f.brake_budget()
        over = budget and brake.trips(self.f.brake_card(budget), brake.counters(
            state, self.f.quota_reading()))
        if state.get('tripped') or state.get('paused_by') or over:
            self.audit('execution.auto_resume_refused', slot=slot, message_id=mid, level='warn',
                       error='brake tripped or budget over the line; resume stays with the operator',
                       detail=dict(detail, tripped=state.get('tripped') or '', paused_by=state.get('paused_by') or '',
                                   over=brake.describe(over) if over else ''))
            return
        label = self.f.label(slot)
        self.f.resume()
        self.audit('execution.auto_resumed', slot=slot, message_id=mid, level='warn', detail=detail)
        card = routing_card(text, kind='notice', slot=label, severity='info')
        card['metadata']['event_id'] = 'ui-autoresume-' + digest(slot + ':' + card_key)[:32]
        self.ui(slot, card)
        if slot != 'supervisor':
            self.ui('supervisor', card)

    def prompt_pause_arm(self, slot, scope, prior):
        """Mark a dialog wait's pause as this dialog's own (prompt_pause_lift). Ours only when the fleet was not
        paused before, or the pause before was this slot's own dialog pause (same mark, same SERVICE.at)."""
        from fleetmux.fleet import SERVICE
        info = self.f.agent(slot)
        try:
            mark = json.loads(info.get('prompt_pause') or '{}')
        except ValueError:
            mark = {}
        if prior.get('paused') != '1' or (isinstance(mark, dict) and mark.get('at') == prior.get('at')):
            self.f.setf(slot, prompt_pause=dumps({'scope': scope, 'at': self.f.r.hget(SERVICE, 'at'),
                        'generation': int(info.get('generation') or 0), 'label': self.f.label(slot)}))
        elif info.get('prompt_pause'):
            self.f.setf(slot, prompt_pause='')

    def prompt_pause_lift(self, slot):
        """Resume the pause a dialog wait set (prompt_wait) once the dialog is gone (its prompt_wait cleared:
        answered in the pane, refused by the CLI itself, the turn moved on) and that pause is still the only one:
        SERVICE.at unchanged since (every pause() rewrites it: BLOCKED, holds, link-loss, operator, another
        slot's dialog), the run live, no other slot waiting on the user, no brake / budget line. The program
        never answers a dialog here; it only stops waiting for one that is no longer there (2026-11-09)."""
        from fleetmux.fleet import SERVICE
        info = self.f.agent(slot)
        try:
            mark = json.loads(info.get('prompt_pause') or '{}')
        except ValueError:
            mark = {}
        service = self.f.r.hgetall(SERVICE)
        if (not isinstance(mark, dict) or not mark.get('at') or service.get('paused') != '1'
                or service.get('at') != mark['at'] or not self.f.running()
                or str(info.get('generation') or 0) != str(mark.get('generation'))):
            self.f.setf(slot, prompt_pause='')
            return
        if info.get('prompt_wait') or any(self.f.agent(other).get('prompt_wait')
                                          and self.f.agent(other).get('prompt_route') != 'supervisor'
                                          for other in self.f.names if other != slot):
            return  # the dialog (or another one waiting on the user) is still there: next tick
        self.f.setf(slot, prompt_pause='')
        label = self.f.label(slot)
        self.pause_owner_resume(slot, '', {'source': 'prompt_wait', 'scope': mark.get('scope') or '',
                                           'label': label, 'pause_reason': service.get('reason', ''),
                                           'paused_at': mark['at']},
                                t('resume.dialog_gone', "{label}'s dialog is gone (answered, or denied by the CLI "
                                  'itself); the pause was only for that dialog and is lifted, dispatch resumes.',
                                  label=label),
                                'prompt:' + str(mark.get('scope')) + ':' + mark['at'])

    def prompt_wait(self, slot, scope, reason, screen, kind=None):
        """A dialog waits for the user (plan F B-1 / F' 8.2): mark the slot, pause dispatch, tell the user.

        Not a hold: the lease, the message and the slot state stay as they are (busy / received, or
        starting), so once a human answers in the pane the turn ends and settles normally."""
        if self.f.agent(slot).get('midturn_id') and not self.f.agent(slot).get('midturn_wait_at'):
            self.f.setf(slot, midturn_wait_at=time.time())
        self.f.setf(slot, prompt_wait=scope, prompt_wait_at=time.time(), prompt_route='user', watch_state='prompting',
                    prompt_class=kind or 'unread')  # §24 held watchdog: a delete dialog is only ever escalated
        self.audit('execution.prompt_wait', slot=slot, level='error', error=reason,
                   detail={'scope': scope, 'screen': screen, 'route': 'user', 'dialog_class': kind or 'unread'})
        card = routing_card(t('prompt.wait', '{label} waits at a dialog for a person; dispatch paused. Screen: {screen}.',
                              label=self.f.label(slot), screen=screen),
                            kind='notice', slot=self.f.label(slot), severity='warn')
        card['metadata']['event_id'] = 'ui-prompt-' + digest(slot + ':' + scope)[:32]
        self.ui(slot, card)
        if slot != 'supervisor':
            self.ui('supervisor', card)
        from fleetmux.fleet import SERVICE
        prior = self.f.r.hgetall(SERVICE)
        self.f.pause(slot + ': ' + reason, kind='dialog', slot=slot)
        self.prompt_pause_arm(slot, scope, prior)

    def push_user_dialog(self, slot, rec):
        """A delete dialog in a run with ``channels`` (P6b; contracts §8 删除确认框的用户出口): register the pending
        record and queue one user-only card. ``rec`` carries ``register``'s arguments (info, lease, scope, prompt,
        tail, digest, screen, keys_approve, paths); the pause reason is the one ``prompt_wait`` just set. Nothing
        else of ``prompt_wait`` changes (pause, notice card, zero keys)."""
        from fleetmux import channels
        from fleetmux.channels import approve, envelope
        if not self.f.cfg.get('channels'):
            return ''
        r = self.f.r
        pid = approve.register(r, slot, rec.get('info') or self.f.agent(slot), rec.get('lease') or {},
                               rec.get('scope', ''), rec.get('prompt', ''), rec.get('tail', ''), rec.get('digest', ''),
                               rec.get('screen', ''), rec.get('keys_approve') or [], rec.get('paths') or [],
                               r.hget('fx:service', 'reason') or '', ttl_s=self.f.cfg.get('approve_ttl_s'),
                               dialog_class=rec.get('dialog_class'), title=rec.get('title'))
        item = envelope.outbound('prompt_wait', target='last', card=approve.card(r.hgetall(channels.PENDING + pid)),
                                 user_only=True)
        r.lpush(channels.OUTQ, json.dumps(item, ensure_ascii=False))
        return pid

    def user_dialog_answer(self, ev):
        """Step two / reject from the phone (the ``dialog_user_answer`` event, P6b): ``{status: accepted}`` only for
        a live record whose nonce (approve), first-step sender / conversation and time limits match; otherwise
        ``{status: stale, reason}`` and the record is unchanged. Never touches ``fx:escalations`` or a key."""
        from fleetmux import channels
        from fleetmux.channels import approve, security
        # The record is settled by one compare-and-set script, which an effect plan cannot hold: inside a
        # transaction use the real client (the audits still go through the plan).
        r = self.effects.source if self.effects else self.f.r
        pid = str(ev.get('pending_id') or '')
        if pid.startswith('p-') and r.hget(channels.PENDING + pid, 'kind') == 'action':
            # P8k: an action confirmation; the ticket is written only here (contracts §8 动作确认)
            policy = security.policy(self.f.cfg)
            if policy is None:
                return {'status': 'stale', 'reason': 'no_policy'}
            return approve.answer_action(r, r.hgetall(channels.PENDING + pid), ev, policy=policy,
                                         audit=self.audit)
        return approve.answer_dialog(r, ev, audit=self.audit)

    def prompt_escalate(self, slot, rec, tail):
        """A non-delete dialog goes to the supervisor to rule on (plan F' 8.3): mark the slot, record the
        escalation, queue the [dialog] question. No pause, no key; the lease stays received."""
        from fleetmux.fleet import DIALOG_KEY
        if self.f.agent(slot).get('midturn_id') and not self.f.agent(slot).get('midturn_wait_at'):
            self.f.setf(slot, midturn_wait_at=time.time())
        key = DIALOG_KEY + slot + ':g' + str(rec['generation'])
        label = self.f.label(slot)
        self.f.setf(slot, prompt_wait=rec['scope'], prompt_wait_at=time.time(), prompt_route='supervisor',
                    prompt_digest=rec['digest'], watch_state='prompting')
        keys = ', '.join(rec['keys_approve']) if rec['keys_approve'] else 'no key (it will go to the user)'
        if rec.get('channel') == 'permission_reply':  # opencode: the ruling is a permission reply, not a key
            text = ('[dialog] ' + label + ' (' + str(rec['prompt']).split(' ')[0] + ') asks permission (' + rec['prompt'] + '); the program '
                    'classified it as not a delete / move. Request:\n' + tail + '\n[end of request]\nAnswer in your '
                    'next JSON: "escalations": [{"task": "' + key + '", "decision": "approve" | "reject", "note": '
                    '"<optional>"}]. approve = the program allows this one request once; reject = the program '
                    'rejects it. Do not dispatch to ' + label + ' in this reply. If you think it deletes or moves '
                    'anything, or only the user can decide, answer reject and say why in reason.\n\nReply with the '
                    'JSON format from AGENTS.md.')
        else:
            text = ('[dialog] ' + label + ' is stopped at a confirmation dialog (' + rec['prompt'] + '); the program '
                    'classified it as not a delete / move. Last ' + str(len(tail.splitlines())) + ' screen lines:\n'
                    + tail + '\n[end of screen]\nAnswer in your next JSON: "escalations": [{"task": "' + key + '", '
                    '"decision": "approve" | "reject", "note": "<optional>"}]. approve = the program presses ' + keys
                    + ' for it; reject = the program presses Escape. Do not dispatch to ' + label + ' in this reply. If '
                    'you think it deletes or moves anything, or only the user can decide, answer reject and say why in '
                    'reason.\n\nReply with the JSON format from AGENTS.md.')
        if self.f.service:
            # Bound to the worker's request (kind followup: delivered even if that request ends meanwhile).
            mid = self.queue('supervisor', text, request_id=self.f.agent(slot).get('request') or 'notice', kind='followup')
        else:
            mid = self.f.queue('supervisor', text)
        rec = dict(rec, message_id=mid, delivered_at=0, status='escalated', asked_at=time.time())
        esc = self.f.escalation_state()
        esc[key] = rec
        self.f.save_escalations(esc)
        self.audit('watch.prompt_escalated', slot=slot, level='warn', task=key, prompt=rec['prompt'],
                   screen=rec['screen'], digest=rec['digest'], message_id=rec['message_id'])
        card = routing_card(t('prompt.escalated', '{label} waits at a dialog (not a delete); the supervisor decides. '
                              'Screen: {screen}.', label=label, screen=rec['screen']),
                            kind='notice', slot=label, severity='warn')
        card['metadata']['event_id'] = 'ui-dialog-' + digest(key + ':' + rec['scope'])[:32]
        self.ui(slot, card)
        self.ui('supervisor', card)
        return key

    def replay_intents(self):
        # Replay prepared effects only; applied plans never run validators again.
        clean = True
        for intent in self.bus.r.scan_iter(match=self.prefix + ':intent:*'):
            if self.bus.r.hget(intent, 'phase') == 'prepared':
                try:
                    self.apply(intent)
                except redis.ResponseError as exc:
                    self.f.pause('prepared settlement invalid at recovery; held: ' + str(exc), kind='integrity')
                    clean = False
            elif self.bus.r.hget(intent, 'phase') != 'applied':
                self.f.pause('ambiguous intent requires inspection: ' + intent, kind='integrity')
                clean = False
        return clean

    def recover(self):
        self.replay_intents()
        for slot in self.f.names:
            info = self.f.agent(slot)
            state = self.bus.r.hgetall(self.bus.lease_key(slot))
            if state.get('current_delivery'):
                env = self.bus.message(state['message_id'])
                self.bus.mark_uncertain(slot, env, 'daemon restart; automatic execution replay forbidden')
                self.f.setf(slot, state='held', bridge_available=0, idle_confirmed=0)
            elif info.get('state') in ('starting', 'handoff', 'busy', 'clearing'):
                self.f.setf(slot, state='held', bridge_available=0, idle_confirmed=0)
        self.f.pause('same-run daemon restart recovered held; inspect sessions before resuming', kind='restart_recovery')
        self.audit('daemon.recovered', level='warn', execution_allowed=False)

    # -- explicit start --resume (docs/design.md §17). A plain same-run restart still uses recover().
    def classify_resume(self):
        """A42-LINUX-RESTORE-FAILURE-04 (second layer): what this ``--resume`` is resuming, decided **before** any
        retire, respawn, result deletion or replay side effect. ``'none'``: the run never submitted a task (no task
        record at all, or the strictly confirmed pre-READY startup-failure placeholder) — it takes the common
        resume path below and then waits for a task. ``'task'``: a real task (non-empty id and a usable numeric
        deadline). Anything else is damaged: ``SystemExit`` with a clear message, nothing respawned, no fake task."""
        from fleetmux import lifecycle_hold
        from fleetmux.fleet import result_state
        present, value = result_state(self.f.run_dir)
        try:
            return lifecycle_hold.classify(self.f.task(), value if isinstance(value, dict) else {}, present,
                                           value is not None)
        except ValueError as exc:
            raise SystemExit('cannot resume ' + str(self.f.run_dir) + ': ' + str(exc)) from exc

    def resume_run(self):
        """Reattach live official-CLI workers, respawn everything else as the next generation, and
        re-run the work that was in flight or queued once more (capped by max_attempts). The operator's
        explicit --resume is the authorization to replay; a plain daemon restart never replays.

        A run that never submitted a task (A42-LINUX-RESTORE-FAILURE-04) goes through this same path — the intent
        safety gate, the retire/reattach/generation/fencing steps and ``Fleet.resume`` (unpause) all still run —
        and only the task brief is skipped: its placeholder is cleared at the end, so it waits for a task again.
        """
        from fleetmux import lifecycle_hold, restart_state
        from fleetmux.fleet import SERVICE, run_result
        restart_state.on_resume_entry(self.f)  # section 60: Redis as the previous daemon left it (never raises)
        kind = None if self.f.service else self.classify_resume()  # before any side effect; damaged refuses here
        if not self.replay_intents():
            self.audit('resume.refused', level='error', error='ambiguous effect intent; held like a plain restart')
            return self.recover()
        previous = self.bus.r.hget(SERVICE, 'reason') if self.f.paused() else ''
        self.f.job_resume_reconcile()  # section 58 "Restart": carried / lost rows, cancels, suspend lost job sub-fleets
        readopt = bool((self.f.cfg.get('adopt') or {}).get('readopt'))
        if readopt:  # section 58 "Restart": this job sub-fleet was handed to a fresh slot of its root
            subfleet.readopt_prepare(self.f)
        outage = daemon_outage(self.bus.r, self.bus, self.f.cfg)  # before clear_slot and before the loop beats again
        unsettled = self.codex_unsettled()  # read before clear_slot abandons them and releases their leases
        lost = self.lost_dispatches()
        sup = self.f.agent('supervisor').get('pane')
        page = bool(sup) and self.f.mgr.get_pane_status(sup) is not None
        # A sub-fleet's adopted supervisor pane belongs to its parent fleet: reattach it like a worker.
        slots = self.f.workers + (['supervisor'] if subfleet.adopted(self.f.cfg, 'supervisor', self.f.agent(
            'supervisor').get('generation')) else [])
        reattach = [s for s in slots if page and self.reattachable(s)]
        # Section 17: an adopted supervisor whose view died with the page while its tmux session runs on is kept
        # (reattached through a new view of that session below), never replaced by a second generation.
        kept = None if page else subfleet.kept_adopted_view(self.f)
        kept_inflight = False
        if kept and 'supervisor' in slots:
            reattach.append('supervisor')
            kept_inflight = bool(self.bus.r.hget(self.bus.lease_key('supervisor'), 'current_delivery'))
        self.kept_view = (kept, kept_inflight) if kept else None
        for slot in self.f.names:
            if not self.clear_slot(slot, slot in reattach) and slot in reattach:
                reattach.remove(slot)  # its lease is still held: never leave it silently undeliverable
        for slot in reattach:
            self.f.setf(slot, state='idle', bridge_available=0, busy_since=time.time(), report_retries=0)
            self.audit('resume.reattached', slot=slot, generation=self.f.agent(slot).get('generation'))
        # A delegated slot's pane is run by its sub-fleet (resumed below): never replace it here.
        delegated = [s for s in self.f.workers if self.f.agent(s).get('state') == 'delegated']
        # P7: an approved / rotating split keeps its request and its model across the restart (before respawn).
        pinned = self.f.split_pin_on_resume() if self.f.service else set()
        # The advisor tab's next generation goes through its own fault branch (§27): never blocks the resume.
        self.respawn([s for s in self.f.names if s not in reattach and s not in delegated and s != advisor.SLOT
                      and not (readopt and s == 'supervisor')], page, prompt=NO_TASK_PROMPT if kind == 'none' else None)
        if readopt:
            subfleet.readopt_supervisor(self.f)
        if advisor.SLOT in self.f.names:
            self.f.advisor_restart('start --resume', source='resume')
        ran = self.codex_ran(unsettled)  # only now: respawn closed those sessions (never reattached)
        lost = [m for m in lost if m['message_id'] not in ran]
        self.f.security_keeps_pause() or self.f.resume()
        if self.f.service:
            self.f.split_settle_accepted()  # P7 (S11): an accepted split's request is answered, never routed again
            self.transaction('resume-requests-' + uuid.uuid4().hex, lambda: self.requeue_requests(ran, pinned))
        elif kind == 'none':
            # A run that never submitted a task: the common resume path above has passed the intent gate, retired
            # and respawned the slots and unpaused the run. Now the strict placeholder is cleared by the normal
            # writer so the run waits for a task again; its closed result stays as result.closed.json evidence.
            result = run_result(self.f.run_dir)
            if lifecycle_hold.never_submitted(self.f.task(), result):
                kept = lifecycle_hold.clear_no_task(self.f.r, self.f.run_dir, self.f.task(), result)
                self.audit('resume.no_task', level='warn', reason=str(result.get('reason') or '')[:300],
                           closed_via=str(result.get('closed_via') or ''),
                           evidence=str(kept or ''), detail='the run never submitted a task; it waits for one now')
            else:
                self.audit('resume.no_task', level='warn', detail='the run has no task record; it waits for one')
        elif self.f.task().get('status') == 'running':
            self.transaction('resume-task-' + uuid.uuid4().hex, lambda: self.brief(lost, ran, outage))
        self.audit('daemon.resumed', level='warn', reattached=reattach, lost=len(lost), previous_pause=previous or '',
                   **{k: outage[k] for k in ('down_s', 'last_event', 'pending_events', 'held')})
        subfleet.resume_children(self.f)  # children whose daemon is gone run again (--resume)
        subfleet.reopen_parent_views(self.f)  # section 17: a delegated tmux slot's parent tab, once its child's Redis is up

    def codex_unsettled(self):
        """Codex slots with a queued delivery never settled: [(slot, record, pane, lease message id or '')].
        Abandoning does not withdraw such an item (Codex has no official way to), so a live TUI would still
        run it next to the replay: clear_slot never reattaches these slots and respawn closes their session."""
        out = []
        for slot in self.f.names:
            info = self.f.agent(slot)
            if codex_queue.is_codex(self.slot_cfg(slot), slot) and codex_queue.unsettled(info):
                rec = codex_queue.record(info)
                lease = self.bus.r.hgetall(self.bus.lease_key(slot))
                current = lease.get('message_id') if lease.get('current_delivery') == rec['cq_delivery'] else ''
                out.append((slot, rec, info.get('pane') or '', current or ''))
        return out

    def codex_ran(self, unsettled):
        """After the old sessions are closed, the old rollout decides by the delivery's token: no turn -> it
        never ran (replayed); started -> interrupted by the close (replayed, like an aborted typed turn);
        completed -> it ran while the daemon was down: never replayed. A session still open (respawn could
        not close it; the task is blocked) is never replayed either. Returns {message_id: (slot, record, turn)}."""
        ran = {}
        for slot, rec, pane, current in unsettled:
            open_ = bool(pane) and self.f.mgr.get_pane_status(pane) is not None
            hit = codex_queue.token_turn(rec['cq_thread'], rec['cq_token'], self.f.sessions)
            if current and (open_ or (hit and hit['complete'])):
                ran[current] = (slot, rec, (hit or {}).get('turn', ''))
            event = 'resume.codex_open' if open_ else 'resume.codex_ran' if hit and hit['complete'] else (
                'resume.codex_interrupted' if hit else 'resume.codex_not_started')
            self.audit(event, slot=slot, level='warn', delivery_id=rec['cq_delivery'],
                       turn=(hit or {}).get('turn', ''), message_id=current,
                       replay='no' if current in ran else 'yes')
        return ran

    def lost_dispatches(self):
        """Task-mode worker instructions of the running task that were queued or in flight."""
        task = self.f.task()
        if self.f.service or task.get('status') != 'running':
            return []
        lost = []
        for slot in self.f.workers:
            state = self.bus.r.hgetall(self.bus.lease_key(slot))
            mids = ([state['message_id']] if state.get('current_delivery') else []) + self.bus.pending(slot)
            for mid in mids:
                msg = self.bus.message(mid)
                if msg.get('kind') in ('worker', 'continuation') and msg.get('request_id') == task['id']:
                    lost.append(msg)
        return lost

    def reattachable(self, slot):
        """Only official-CLI panes still running their CLI: a Pi bridge never survives a Redis outage."""
        from fleetmux.fleet import RESUME_CLIS
        info = self.f.agent(slot)
        pane = info.get('pane')
        if (self.hooked(slot) and self.bus.r.hget(self.bus.lease_key(slot), 'current_delivery')
                and not subfleet.adopted(self.f.cfg, slot, info.get('generation'))):
            # The Escape that aborts its in-flight turn would also kill the waiting Stop hook and
            # leave the session with no delivery channel: start the next generation instead. Never for
            # an adopted supervisor: its pane is the parent's (no next generation here); after the
            # Escape its last Stop's rewake fallback hands the replay over.
            return False
        lease = self.bus.r.hgetall(self.bus.lease_key(slot)) if self.clear_enabled(slot) else None
        return (tui_slot(self.slot_cfg(slot), slot) and info.get('runtime_ready') == '1' and not info.get('handoff_id')
                and info.get('clear_required') != '1' and info.get('clear_conflict') != '1'
                and (lease is None or (lease.get('session_id') == info.get('thread')
                                      and lease.get('generation') == info.get('generation')))
                and info.get('state') in ('idle', 'busy', 'held') and bool(pane)
                and self.f.mgr.get_pane_status(pane) is not None and self.f.mgr.has_cli_process(pane, RESUME_CLIS))

    def clear_slot(self, slot, reattached):
        """Release the slot's lease and cancel its queue (re-queued afterwards). False: lease still held."""
        state = self.bus.r.hgetall(self.bus.lease_key(slot))
        released = True
        # A Codex delivery that was queued but never proven to run may still sit in this live session's
        # queue (Codex's queue is not ours to edit) and would run next to the replay: do not reattach
        # that session, respawn it (new thread; the old item can only run if that session is resumed).
        keep_out = (reattached and codex_queue.is_codex(self.slot_cfg(slot), slot)
                    and codex_queue.unsettled(self.f.agent(slot)))
        codex_queue.clear(self.f, slot, 'start --resume')  # its token can never settle anything now
        if keep_out:
            self.audit('resume.codex_unsettled', slot=slot, level='warn',
                       reason='queued delivery not proven to have run; session respawned, not reattached')
        if state.get('current_delivery'):
            pane = self.f.agent(slot).get('pane')
            if reattached and pane:
                self.f.mgr.send_key(pane, 'Escape')  # abort the old turn before its instruction is typed again
            try:
                env = self.bus.message(state['message_id'])
                if state.get('status') not in ('uncertain', 'expired'):
                    self.bus.mark_uncertain(slot, env, 'start --resume: replay authorized by the operator')
                self.bus.resolve_uncertain(slot, env, 'cancelled', execution_stopped=True)
            except (BusError, ValueError, KeyError) as exc:
                self.audit('resume.release_failed', slot=slot, level='error', error=str(exc))
                released = False
            if reattached:
                self.f.setf(slot, idle_confirmed=0)  # Escape is not proof the old turn stopped
        for mid in self.bus.pending(slot):
            self.cancel_queued(slot, {'message_id': mid}, 'start --resume: re-queued after reconcile')
        return released and not keep_out

    def respawn(self, slots, page, prompt=None, handoff=''):
        """Next generation of each slot in place; without a supervisor pane, rebuild the whole page.
        ``prompt`` / ``handoff``: another launch prompt ({label}) and its note (a §29 sub-fleet return)."""
        from fleetmux import history
        from fleetmux.fleet import SERVICE, SUPERVISOR_LABEL, NAMES
        old = {}
        for slot in slots:
            info = self.f.agent(slot)
            pane = info.get('pane')
            if not info.get('generation'):  # never spawned (crash during provisioning)
                if slot == 'supervisor':
                    self.f.setf(slot, label=SUPERVISOR_LABEL)
                else:
                    self.f.init_worker(slot)
                continue
            generation, prev = int(info['generation']), self.f.label(slot)
            alive = bool(pane) and self.f.mgr.get_pane_status(pane) is not None
            snapshot = ''
            if alive:
                snapshot = self.f.run_dir / 'handoffs' / (slot + '-g' + str(generation) + '-resume.terminal.txt')
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                self.f.mgr.capture_to(pane, str(snapshot))
            history.record(self.f.run_dir, slot=slot, label=prev, generation=generation, handoff=handoff,
                           snapshot=str(snapshot), source='resume', timed_out=False)
            label = (SUPERVISOR_LABEL if slot == 'supervisor' else slot) + '.' + str(generation + 1)
            self.f.setf(slot, generation=generation + 1, label=label, handoff_id='', retired_pane='',
                        clear_required=0, clear_conflict=0, clear_phase='', clear_token='',
                        prev_label='' if slot == 'supervisor' else prev)
            if info.get('rescue_launch') or info.get('rescue_relay'):
                # An advisor respawn cut off mid-handoff: the successor still starts with the sheet's
                # runtime (its domains are in advice_domains); the relay note / hint never leak into a later rotation.
                fields = dict(rescue_launch='', rescue_hint='', rescue_relay='')
                if info.get('rescue_launch'):
                    fields['runtime_launch'] = info['rescue_launch']
                self.f.setf(slot, **fields)
                self.audit('resume.rescue_launch', slot=slot, generation=generation + 1)
            if info.get('split_launch'):  # §44: the split's model, or the slot's own after its sub-fleet returned
                self.f.setf(slot, runtime_launch=json.loads(info['split_launch'])['launch'], split_launch='')
            if slot != 'supervisor':
                self.f.r.hset(NAMES, mapping={label: slot})
            old[slot] = pane if alive else None
        prompt = prompt or ('You are fleet agent {label}. Read AGENTS.md in the current directory first. This run was '
                            'resumed after an interruption; the daemon sends the current state next. In this turn do no '
                            'task work: your final reply must be exactly READY.')
        if not page:  # whole page lost (e.g. cmux restarted): close leftovers, then lay it out again
            for slot, pane in old.items():
                if pane and (not self.f.mgr.kill_pane(pane) or self.f.mgr.get_pane_status(pane) is not None):
                    self.f.setf(slot, state='dead', launch_ready=0)
                    return self.f.finish('blocked', 'start --resume could not close the old session of ' + slot)
                self.confirm_retired(slot)
            for slot in slots:
                self.f.spawn(slot, prompt.format(label=self.f.label(slot)))
            history.write_page(self.f.run_dir, self.f.history_slots())
            kept, self.kept_view = getattr(self, 'kept_view', None), None
            if kept:  # the kept adopted supervisor: a new view of its session, before the page is laid out
                subfleet.reopen_child_view(self.f, *kept)
            self.f.arrange_page()  # live workers only: a delegated slot's stale pane never fails the layout
            return
        for slot in slots:
            new = self.f.spawn(slot, prompt.format(label=self.f.label(slot)), launch_held=True)
            pane = old.get(slot)
            if pane:  # the successor takes the old session's place, as in a handoff
                placed = self.f.mgr.tab_into(new, pane)
                if not placed or not self.f.mgr.kill_pane(pane) or self.f.mgr.get_pane_status(pane) is not None:
                    self.f.setf(slot, state='dead', launch_ready=0, retired_pane=pane,
                                retired_launch=self.f.agent(slot).get('runtime_launch') or '')
                    return self.f.finish('blocked', 'start --resume could not close the old session of ' + slot)
                self.confirm_retired(slot)
            elif slot != 'supervisor':
                peer = next((p for p in (self.f.agent(w).get('pane') for w in self.f.workers if w != slot)
                             if p and p != new and self.f.mgr.get_pane_status(p) is not None), None)
                if peer:
                    self.f.mgr.tab_into(new, peer, before=False)
                else:
                    self.f.mgr.place_right_of(new, self.f.agent('supervisor')['pane'])
            self.f.setf(slot, launch_ready=1)
        self.f.refresh_history()
        if self.f.history_lost():  # the page stayed but its history pane is empty or gone: lay it out again
            self.f.arrange_page()
        history_pane = self.f.r.hget(SERVICE, 'history_pane')
        if history_pane:
            self.f.mgr.dock_control(history_pane)  # the new control tab joins history again

    def progress(self, task):
        """Where a task stands on disk and in Redis: verified / missing artifacts, accepted dispatches.
        Shared by the resume brief and the night-switch brief (docs/design.md §17, §25)."""
        from fleetmux.fleet import check_artifacts, extended_checks
        head = lambda text: ' '.join(text.split())[:200]
        done = []
        for key in self.bus.r.scan_iter(match=self.prefix + ':message:*'):
            msg = self.bus.message(key.rsplit(':', 1)[1])
            if (msg.get('kind') in ('worker', 'continuation') and msg.get('slot') in self.f.workers
                    and msg.get('request_id') == task['id']
                    and msg.get('status') == 'committed' and msg.get('outcome') == 'completed'):
                done.append((msg['committed_at'], '- ' + msg['slot'] + ': ' + head(msg['body']['text'])
                             + '\n  report: ' + dumps(msg.get('result') or {})[:600]))
        artifacts = json.loads(task.get('artifacts') or '[]')
        checked = [(a['path'], check_artifacts([a], self.f.workspace(), extended_checks(task)))
                   for a in artifacts]
        return ('\nDeclared artifacts verified on disk: ' + json.dumps([p for p, bad in checked if not bad])
                + '\nDeclared artifacts missing or failing: '
                + json.dumps([p + ' (' + '; '.join(bad) + ')' for p, bad in checked if bad], ensure_ascii=False)
                + '\nCompleted dispatches (their reports were accepted; do not redo them):\n'
                + ('\n'.join(x for _, x in sorted(done)) or '- none'))

    def brief(self, lost, ran=None, outage=None):
        """Re-queue lost dispatches (attempt-capped) and brief the new supervisor generation. `ran`: worker
        instructions that ran to completion before the interruption (never re-queued, reported instead).
        `outage`: daemon_outage() read before the resume (§36)."""
        task = self.f.task()
        if not task.get('id'):  # A42-LINUX-RESTORE-FAILURE-04: never a bare KeyError on a damaged record
            raise ValueError('cannot brief a resumed run: its task record has no id; refusing to brief a fake task')
        head = lambda text: ' '.join(text.split())[:200]
        requeued = self.requeue_lost(lost)
        if requeued is None:
            return
        done = ['- ' + self.f.label(slot) + ': ' + head(self.bus.message(mid).get('body', {}).get('text', ''))
                for mid, (slot, _rec, _turn) in (ran or {}).items() if slot != 'supervisor']
        progress = self.progress(task)
        state = self.f.contract_state()
        self.f.queue('supervisor', self.fit_brief(task['id'], progress, lambda progress: '[Resumed task ' + task['id'] + '] This run was interrupted and resumed with '
                     'start --resume. You are a new supervisor generation: earlier decisions are not in your '
                     'context, so rely on this summary and the files on disk.'
                     + '\nGoal: ' + task.get('goal', '')
                     + '\nWorkers you can dispatch to: ' + ', '.join(self.f.labels())
                     + '\nShared output directory: ' + str(self.f.workspace())
                     + (('\nDaemon outage (from the logs): last heartbeat ' + clock(outage['beat']) + ', down '
                         + str(outage['down_s']) + ' s until this resume; last daemon audit event '
                         + (outage['last_event'] or 'none') + ' at ' + clock(outage['last_event_at']) + '; '
                         + str(outage['pending_events']) + ' events were waiting and are processed now; '
                         'held/expired deliveries found: ' + (', '.join(outage['held']) or 'none') + '.')
                        if outage and outage['beat'] else '')
                     + progress
                     + '\nLost dispatches the daemon already re-queued (do NOT dispatch them again):\n'
                     + ('\n'.join(requeued) or '- none')
                     + (('\nInstructions that already ran to completion before the interruption; their reports were '
                         'not received (check their files; do NOT dispatch them again):\n' + '\n'.join(done))
                        if done else '')
                     + (('\nInterface contracts are on: read CONTRACTS.md in your directory and '
                         + str(self.f.workspace() / 'contracts.json') + '. ' + contracts.table_line(state))
                        if state else '')
                     + '\nDispatch only work that is still missing. Reply with the JSON format from AGENTS.md.'))

    BRIEF_LIMIT = 120000  # UTF-8 bytes of brief text: well under message_bus.MAX_BODY with the envelope

    def fit_brief(self, task_id, progress, build):
        """The resume brief must fit one bus message however long the run has been (2026-11-09: a few
        hundred accepted dispatches made it exceed MAX_BODY and the resumed daemon died on every start).
        Over BRIEF_LIMIT the full text goes to <run>/handoffs/resume-brief-<task>-<ms>.md and the queued
        brief keeps everything except the oldest part of `progress`, with a pointer to the file."""
        full = build(progress)
        if len(full.encode()) <= self.BRIEF_LIMIT:
            return full
        path = self.f.run_dir / 'handoffs' / ('resume-brief-' + task_id + '-' + str(int(time.time() * 1000)) + '.md')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(full)
        note = ('\n[The full resume summary is ' + str(len(full.encode())) + ' bytes, over the message limit; it is in '
                + str(path) + '. Read it (cat) before dispatching anything. Below: only its most recent part.]\n')
        room = self.BRIEF_LIMIT - len(build(note).encode())
        tail = progress.encode()[-max(room, 0):].decode('utf-8', 'ignore')
        tail = tail[tail.find('\n') + 1:] if '\n' in tail else tail  # start on a whole line
        brief = build(note + tail)
        if len(brief.encode()) > self.BRIEF_LIMIT:  # the non-progress parts alone are too long: keep the pointer
            brief = ('[Resumed task ' + task_id + '] This run was interrupted and resumed with start --resume. '
                     'The resume summary is too long for one message; it is in ' + str(path)
                     + '. Read it (cat) before dispatching anything, then reply with the JSON format from AGENTS.md.')
        self.audit('resume.brief_spilled', level='warn', path=str(path), bytes=len(full.encode()))
        return brief

    def requeue_lost(self, lost, when='start --resume'):
        """Re-queue lost worker dispatches of the running task once more (``replay`` = attempt). Returns the
        brief's lines, or None when one is over max-attempts: the task is blocked (§17)."""
        task = self.f.task()
        cap = int(self.f.cfg['max_attempts'])
        head = lambda text: ' '.join(text.split())[:200]
        over = next((m for m in lost if int(m.get('replay') or 1) >= cap), None)
        if over:
            self.f.finish('blocked', when + ': the dispatch to ' + self.f.label(over['slot'])
                          + ' was lost again after ' + str(int(over.get('replay') or 1)) + ' attempt(s) '
                          '(max-attempts ' + str(cap) + '): ' + head(over['body']['text']))
            return None
        requeued = []
        for msg in lost:
            attempt = int(msg.get('replay') or 1) + 1
            mid = self.f.queue(msg['slot'], '[Supervisor instruction ' + task['id'] + '] ' + msg['body']['text'])
            retained = {k: msg[k] for k in ('midturn_splits', 'continues', 'midturn_instruction', 'original_kind') if k in msg}
            if msg.get('kind') == 'continuation':
                retained['kind'] = 'continuation'
                body = json.loads(self.effects.hget(self.bus.msg_key(mid), 'body'))
                retained['body'] = _json(dict(body, purpose='continuation'))
            self.effects.hset(self.bus.msg_key(mid), dict(retained, replay=attempt))
            requeued.append('- ' + self.f.label(msg['slot']) + ' (attempt ' + str(attempt) + ' of ' + str(cap)
                            + '): ' + head(msg['body']['text']))
        return requeued

    def requeue_requests(self, ran=None, pinned=()):
        """Service: accepted requests without a public result run once more (unless replay is false, or a
        Codex item of theirs already ran to completion before the interruption). ``pinned``: requests held by an
        approved / rotating split (Fleet.split_pin_on_resume) stay with their slot, not replayed."""
        from fleetmux.fleet import REQQ
        done = {self.bus.message(mid).get('request_id') for mid in (ran or {})}
        for rid in self.f.r.lrange(REQQ, 0, -1):
            if rid in pinned:
                self.audit('request.pinned_to_split', request_id=rid)
                continue
            self.replay_request(rid, done)
        named_subfleet.requeue_awaiting(self.f)  # §29 external ops wait in their own queue, not REQQ

    def replay_request(self, rid, done=(), when='on resume'):
        """One accepted request without a public result is routed once more (§17: ``replay`` = attempt; replay
        false, over max-attempts, or already run to completion -> published timeout instead)."""
        from fleetmux.fleet import REQ
        cap = int(self.f.cfg['max_attempts'])
        req = self.f.r.hgetall(REQ + rid)
        if not req:
            return
        spec = json.loads(req['spec'])
        attempt = int(req.get('replay') or 1) + 1
        worker = req.get('worker') or 'supervisor'
        if rid in done:
            self.publish(rid, 'timeout', worker=worker, error='not replayed ' + when + ': it already ran to '
                         'completion before the interruption and its result was not received')
        elif spec.get('replay') is False:
            self.publish(rid, 'timeout', worker=worker, error='not replayed ' + when + ' (request has replay: false)')
        elif attempt > cap:
            self.publish(rid, 'timeout', worker=worker, error='not replayed ' + when + ': ' + str(cap)
                         + ' attempt(s) used (max-attempts ' + str(cap) + ')')
        else:
            # A fresh timeout window: the downtime does not count against the replay.
            resumed = midturn.resume_clock(req, awake.now())
            paused_ms = int(resumed.get('clock_paused_ms') or 0)
            self.f.r.hset(REQ + rid, mapping={'replay': attempt, 'status': 'routing', 'worker': '',
                'created': awake.now() - paused_ms / 1000, 'clock_paused_at': '', 'clock_paused_ms': paused_ms})
            self.f.route(rid, spec)
            self.audit('request.replayed', request_id=rid, attempt=attempt)

    def deliver(self, slot):
        from fleetmux.fleet import REQ
        info = self.f.agent(slot)
        if info.get('state') != 'idle' or info.get('clear_required') == '1':
            return
        state = self.bus.r.hgetall(self.bus.lease_key(slot))
        if state.get('status') != 'idle':
            return
        if self.clear_enabled(slot) and (state.get('session_id') != info.get('thread')
                or state.get('generation') != info.get('generation')):
            self.transaction('clear-binding-' + uuid.uuid4().hex,
                             lambda: self.hold(slot, 'worker session differs from its bus binding'))
            return
        mids = self.bus.pending(slot, 1)
        if not mids:
            return
        msg = self.bus.message(mids[0])
        # A consultation (kind advisor) is bound to its ticket, never to a request: its own deadline ends it (§27).
        # A program notice to the supervisor is kind followup (queue): only request-bound messages are dropped.
        if (self.f.service and msg['kind'] not in ('followup', 'advisor') and not self.f.r.hgetall(REQ + msg['request_id'])
                and not self.f.r.hgetall(named_subfleet.OP + msg['request_id'])  # §29: an open sub-fleet op
                and not self.f.r.hgetall(applet_lease.OP + msg['request_id'])):  # an open applet op (applets.md 3)
            self.transaction('drop-' + msg['message_id'], lambda: self.cancel_queued(slot, msg, 'already terminal'))
            return
        if msg.get('kind') == 'continuation':
            def resume():
                fields = midturn.resume_message(msg, int(time.time() * 1000))
                self.f.r.hset(self.bus.msg_key(msg['message_id']), mapping={k: fields[k] for k in
                    ('deadline_at', 'clock_paused_at', 'clock_paused_ms') if k in fields})
                self.midturn_resume_request(msg['request_id'])
            self.transaction('midturn-offer-' + msg['message_id'], resume)
        env = self.bus.offer(slot, mids[0])
        if env:
            # Offer + lease is durable before updating legacy display state. Crash
            # between these boundaries is detected and held on same-run recovery.
            self.f.setf(slot, state='busy', msg_id=env['message_id'], busy_since=time.time(),
                        request=env['request_id'], delivered=int(self.f.agent(slot).get('delivered') or 0)+1,
                        bridge_available=0)
            self.f.brake_count('calls')
            self.audit('deliver', slot=slot, request_id=env['request_id'], message_id=env['message_id'],
                       delivery_id=env['delivery_id'], generation=env['generation'], detail=env['body'])
            if tui_slot(self.slot_cfg(slot), slot):
                self.type_into(slot, env)

    def hooked(self, slot):
        """A hook-channel Claude agent: deliveries go through its mailbox, never its input box."""
        return tui_slot(self.slot_cfg(slot), slot) and claude_hook.enabled(self.slot_cfg(slot), slot)

    def hook_keys(self, slot, generation):
        return claude_hook.keys(self.bus.prefix, slot, generation)

    def handoff_taken(self, slot):
        """The slot's Stop hook (or its rewake fallback) handed the current handoff delivery to Claude."""
        info = self.f.agent(slot)
        try:
            bind = json.loads(self.bus.r.get(self.hook_keys(slot, int(info.get('generation') or 0))['bind']) or '{}')
        except (ValueError, TypeError):
            return False
        return isinstance(bind, dict) and bind.get('message_id') == 'm-handoff-' + (info.get('handoff_id') or '-')

    def type_into(self, slot, env):
        """Receive an offered delivery for an official CLI and hand over only its business text.

        No message file and no transport IDs reach the model (a Codex delivery carries only its
        opaque delivery-token line). An official Codex agent gets the text through `codex queue`
        (codex_queue); hook-channel Claude agents (including direct v2 supervisors) from their
        Stop hook; legacy/opt-out Claude workers and adopted supervisors keep typed delivery. The
        CLI's own turn-end hook settles against this delivery in Redis. Any failure holds the slot
        (fail closed).
        """
        consumer = 'tui-' + slot + '-g' + str(env['generation'])
        reason = 'TUI input not confirmed; prompt state unknown'
        try:
            got = self.bus.receive(slot, consumer, 1, generation=env['generation'],
                                   session_id=env['session_id'], reader=self.reader)
            if not got or got['delivery_id'] != env['delivery_id'] or not got['execution_allowed']:
                raise BusError('offered delivery was not received by this generation')
            self.renewed[slot] = time.monotonic()
            if codex_queue.is_codex(self.slot_cfg(slot), slot):
                # Codex: the TUI takes the message from its own queue; nothing is typed (codex_queue).
                got_q = codex_queue.deliver(self.f, slot, env['delivery_id'], got['body']['text'])
                self.audit('tui.queued', slot=slot, message_id=env['message_id'], delivery_id=env['delivery_id'],
                           queued_id=got_q['queued_id'])
                return True
            pane = self.f.agent(slot).get('pane')
            if self.hooked(slot):
                k = self.hook_keys(slot, env['generation'])
                waiting = claude_hook.post(self.bus.r, k, {
                    'kind': 'delivery', 'generation': env['generation'], 'message_id': env['message_id'],
                    'delivery_id': env['delivery_id'], 'text': got['body']['text']})
                # armed=False is normal right after a settle (the hook is still reporting the turn). If
                # Escape / a hook timeout left the session idle, the next turn end of this session (any
                # typed line) picks it up; otherwise the watchdog's stall path applies.
                self.audit('tui.hooked', slot=slot, message_id=env['message_id'], delivery_id=env['delivery_id'],
                           generation=env['generation'], armed=waiting)
                return True
            claude = self.slot_cfg(slot).get('supervisor_cli' if slot == 'supervisor' else 'worker_cli') == 'claude'
            if claude:  # §48: no typed Claude delivery; validate_config refuses every config that needed one
                reason += ': Claude slot without a hook mailbox; ' + NO_TYPING
            elif pane and self.f.mgr.send_prompt_to_idle(pane, got['body']['text']):
                self.audit('tui.typed', slot=slot, message_id=env['message_id'], delivery_id=env['delivery_id'])
                return True
            else:
                reason += ': ' + (getattr(self.f.mgr, 'last_error', '') or 'terminal send failed')
        except (BusError, ValueError, KeyError, OSError, codex_queue.QueueError) as exc:
            reason += ': ' + str(exc)[:500]
        self.transaction('tui-input-' + env['delivery_id'], lambda: self.hold(slot, reason, public_status='failed'))
        return False

    def supervisor_yield_timeout(self):
        """Unknown slash/queued-input completion cannot silently strand the hook mailbox."""
        slot = 'supervisor'
        if not self.hooked(slot):
            return
        info = self.f.agent(slot)
        if info.get('state') in ('held', 'dead', 'delegated'):
            return
        k = self.hook_keys(slot, int(info.get('generation') or 0))
        raw = self.bus.r.get(k['yield_wait'])
        if not raw:
            return
        mark = json.loads(raw)
        if (mark.get('run_id') != self.bus.run_id or mark.get('slot') != slot
                or str(mark.get('generation')) != info.get('generation')
                or mark.get('session_id') != info.get('thread')
                or not awake.passed(mark['deadline'])):  # awake time: written by the hook (awake.of)
            return
        def hold_unconfirmed():
            # A concurrent Stop/block may already have resolved the boundary.
            if self.f.r.get(k['yield_wait']) == raw:
                self.hold(slot, 'Claude slash/queued input completion unconfirmed after '
                          + str(claude_hook.HUMAN_HOLD) + ' s; inspect control and the supervisor session')
        self.transaction('hook-yield-' + digest(raw), hold_unconfirmed)

    def renew_tui(self):
        """The daemon is the receiving consumer for official CLIs, so it renews their
        received leases while the runtime tab exists (stall/liveness checks still hold)."""
        self.supervisor_yield_timeout()
        period = min(10.0, self.bus.lease_ms / 3000.0)
        for slot in self.f.names:
            if not tui_slot(self.slot_cfg(slot), slot) or time.monotonic() - self.renewed.get(slot, 0) < period:
                continue
            state = self.bus.r.hgetall(self.bus.lease_key(slot))
            if state.get('status') != 'received' or not state.get('consumer', '').startswith('tui-'):
                continue
            pane = self.f.agent(slot).get('pane')
            if not pane or self.f.mgr.get_pane_status(pane) is None:
                continue  # liveness tick holds a missing tab
            self.renewed[slot] = time.monotonic()
            if int(state.get('lease_until') or 0) <= int(time.time()*1000) and self.reclaim_tui(slot):
                continue  # LUA_RENEW would hold a lease that already ran out: take it back first (§24)
            try:
                renewed = self.bus.renew(slot, self.bus.message(state['message_id']))
            except BusError as exc:
                renewed, why = False, str(exc)
            else:
                why = 'lease expired'
            if not renewed:
                self.transaction('tui-renew-' + uuid.uuid4().hex,
                                 lambda slot=slot, why=why: self.requeue_hold(
                                     slot, why + '; execution stop unconfirmed', 'renew_failed')
                                 or self.hold(slot, why + '; execution stop unconfirmed'))

    def reclaim_tui(self, slot):
        """§24: an official CLI's lease is renewed by the daemon itself (renew_tui), so one that ran out while its
        work deadline did not only proves the daemon did not run (lid-closed sleep, a stuck daemon). Take it back
        instead of holding: a received lease of this generation's tui consumer (the slot's only consumer: no PEL
        transfer, no second execution), its tab alive. Written live, never as an intent effect (it corrects the
        present; a replayed intent must not carry it). False = hold as before (``lease_reclaim: off`` too)."""
        if self.f.cfg.get('lease_reclaim', 'on') == 'off' or slot == advisor.SLOT or not tui_slot(self.slot_cfg(slot), slot):
            return False
        info = self.f.agent(slot)
        lease = self.bus.r.hgetall(self.bus.lease_key(slot))
        generation = info.get('generation', '0')
        if (lease.get('status') != 'received' or not lease.get('message_id') or lease.get('generation') != generation
                or lease.get('consumer') != 'tui-' + slot + '-g' + generation):
            return False
        pane = info.get('pane')
        if not pane or self.f.mgr.get_pane_status(pane) is None:
            return False  # the liveness tick owns a missing tab
        try:
            if not self.bus.reclaim(slot, self.bus.message(lease['message_id'])):
                return False  # the work deadline passed too
        except (BusError, ValueError) as exc:
            self.audit('lease.reclaim_refused', slot=slot, level='warn', error=str(exc)[:300])
            return False
        self.audit('lease.reclaimed', slot=slot, message_id=lease['message_id'], delivery_id=lease['current_delivery'],
                   level='warn', detail={'gap_s': round((time.time()*1000 - int(lease['lease_until'])) / 1000, 1)})
        return True

    def daemon_gap(self, now):
        """§24: the daemon itself did not run for a while (lid-closed sleep, a stuck daemon). Every stillness the
        watchdog would measure across the gap is the daemon's absence, not the agent's: restart the in-flight slots'
        stall-window baseline and floor their activity at now (``act_floor``, read by ``Fleet.last_activity``)."""
        last, self.last_tick = self.last_tick, now
        beat = min(10, self.bus.lease_ms / 3000)  # the bridge's renew period (AgentBridge.renew_loop)
        gap = last is not None and now - last > 2 * beat  # shorter gaps cannot outlive the key's 3-beat TTL
        if gap and (now - last > 3 * beat or not self.gap_before):
            # The bridges slept too: their heartbeat keys expired during the gap. Give them three beats to write
            # it again before heartbeat_check calls the link lost (a woken laptop is not a dead bridge, §35).
            # A busy daemon ticks every two to three beats (§47): only the first of such consecutive gaps
            # starts the wait, or it would be renewed on every tick and a dead bridge never judged.
            self.hb_grace_until = now + 3 * beat
        self.gap_before = gap
        window = float(watchdog.setting(self.f.cfg, 'stall_window')) or self.bus.lease_ms / 1000.0
        if last is None or now - last < max(30.0, window / 2):
            return
        slots = []
        for slot in self.f.names:
            if not self.bus.r.hget(self.bus.lease_key(slot), 'current_delivery'):
                continue
            # A slot waiting on a dialog reads "activity after the dialog" as the answer: no floor for it.
            floor = {} if self.f.agent(slot).get('prompt_wait') else {'act_floor': now}
            self.f.setf(slot, quiet_since=0, **floor)
            slots.append(slot)
        self.audit('daemon.gap', level='warn', detail={'gap_s': round(now - last, 1), 'slots': slots})

    def gap_tick(self):
        """daemon_gap at the start of Fleet.tick, before its parts (the applets part judges heartbeats), once per
        loop pass: a second call in the same pass would read the first as the previous tick (gap_before)."""
        self.daemon_gap(time.time())
        self.gap_done = True

    def cancel_queued(self, slot, msg, reason):
        r = self.effects or self.bus.r
        raw = r.hgetall(self.bus.msg_key(msg['message_id']))
        if raw.get('kind') == 'continuation':
            self.midturn_resume_request(raw['request_id'])
        r.hset(self.bus.msg_key(msg['message_id']), mapping={'status': 'committed',
            'outcome': 'cancelled', 'result': '{}', 'committed_at': int(time.time()*1000), 'cancel_reason': reason})
        r.lrem(self.bus.pending_key(slot), 0, msg['message_id'])

    def finish(self, status, reason, kind=None, **ref):
        """``kind`` / ``ref``: the pause cause of a service run (Fleet.pause; section 24 pause review)."""
        from fleetmux.fleet import TASK
        if self.effects is None:
            return self.transaction('finish-' + uuid.uuid4().hex, lambda: self.finish(status, reason, kind, **ref))
        if self.f.service:
            self.f.pause(status + ': ' + reason, kind=kind, **ref)
            return
        self.f.r.hset(TASK, mapping={'status': status, 'reason': reason})
        for slot in self.f.names:
            codex_queue.clear(self.f, slot, 'task ' + status)  # its token can never settle (Codex's queue is not edited)
            for mid in self.f.r.lrange(self.bus.pending_key(slot), 0, -1):
                msg = dict(message_id=mid)
                self.cancel_queued(slot, msg, reason)
            if self.f.r.hget(self.bus.lease_key(slot), 'current_delivery') and slot != advisor.SLOT:
                self.hold(slot, status + ': ' + reason)  # the advisor's answer just comes late and is ignored (§27)
        self.audit('task.' + status, request_id=self.f.task().get('id', ''), detail={'reason': reason})

    def offer_handoff(self, slot, prompt):
        # Handoff runs at the existing idle boundary ahead of held business FIFO.
        mid = 'm-handoff-' + self.f.agent(slot)['handoff_id']
        deadline = int(float(self.f.agent(slot)['handoff_deadline'])*1000)
        deadline = max(deadline, int(time.time()*1000)+100)  # zero-timeout test may still retire immediately
        body = {'text': prompt, 'title': t('delivery.title.handoff', 'Write the handoff note'),  # display only
                'mode': self.f.cfg['mode'], 'purpose': 'handoff'}
        # Register with the reviewed bus, then move only this known handoff reference
        # to the FIFO head before authorization. No second consumer/scheduler.
        self.bus.enqueue(slot, body, request_id=self.f.task().get('id') or 'handoff',
                         kind='followup', deadline_at=deadline, message_id=mid)
        self.bus.r.eval("local n=redis.call('LREM', KEYS[1], 1, ARGV[1]); "
                        "if n==1 then redis.call('LPUSH', KEYS[1], ARGV[1]) end", 1,
                        self.bus.pending_key(slot), mid)
        env = self.bus.offer(slot, mid)
        if env:
            self.f.setf(slot, msg_id=mid, bridge_available=0)
            if tui_slot(self.slot_cfg(slot), slot):
                self.type_into(slot, env)

    def slot_cfg(self, slot):
        """This slot's own CLI view: a roster/profile launch may run another worker CLI than the run default."""
        return self.f.runtime_cfg(slot)

    def confirm_retired(self, slot):
        """Call only after cmux confirmed the old runtime process-tree/tab retirement."""
        state = self.bus.r.hgetall(self.bus.lease_key(slot))
        if state.get('current_delivery'):
            env = self.bus.message(state['message_id'])
            if state.get('status') not in ('uncertain', 'expired'):
                self.bus.mark_uncertain(slot, env, 'runtime retired by daemon')
            self.bus.resolve_uncertain(slot, env, 'cancelled', execution_stopped=True)
        self.audit('runtime.retired', slot=slot, execution_stopped=True)

    def retire(self, slot, reason):
        codex_queue.clear(self.f, slot, 'retire: ' + reason)
        info = self.f.agent(slot)
        pane = info.get('pane')
        if info.get('state') == 'reclaimed' and not pane:  # already retired by the idle reclaim
            self.confirm_retired(slot)
            return True
        if subfleet.adopted(self.f.cfg, slot, info.get('generation')) and pane:
            if not subfleet.hand_back(self.f):  # the pane goes back to the parent fleet, never killed
                return False
            self.confirm_retired(slot)
            self.f.setf(slot, state='dead', runtime_ready=0, bridge_available=0, launch_ready=0)
            return True
        if not pane or not self.f.mgr.kill_pane(pane) or self.f.mgr.get_pane_status(pane) is not None:
            self.transaction('retire-failed-' + uuid.uuid4().hex, lambda: self.hold(slot, reason + '; retirement unconfirmed'))
            return False
        self.confirm_retired(slot)
        self.f.setf(slot, state='dead', runtime_ready=0, bridge_available=0, launch_ready=0)
        return True

    # -- liveness (tick, every 30 s): a slot's tab is gone. Outside any Redis intent (tabs / spawn).
    def tab_missing(self, slot, info):
        """Only proof that the session stopped (its tmux session ended) starts the slot's next generation; a
        tab closed over a live tmux session is reattached. Without proof a worker is held alone; only the
        supervisor still pauses the fleet (nobody else would dispatch). A delegated slot is never held."""
        pane = info['pane']
        adopted = subfleet.adopted(self.f.cfg, slot, info.get('generation'))
        probe = getattr(self.f.mgr, 'tmux_session_alive', None)
        alive = probe(pane) if probe else None  # only False is evidence that execution stopped
        reason = 'runtime tab missing; stop evidence unavailable'
        if alive:
            peer = self.peer_tab(slot)
            new = self.f.mgr.reattach_client(pane, peer) if peer else None
            if new:
                self.f.setf(slot, pane=new)
                if info.get('delegated_pane') == pane:
                    self.f.setf(slot, delegated_pane=new)
                    subfleet.repoint(self.f, slot, pane, new)
                self.audit('liveness.reattached', slot=slot, level='warn', detail={'old_pane': pane, 'pane': new})
                return
            reason = 'runtime tab missing; its tmux session runs on but no tab could be reattached'
        elif alive is False:
            reason = 'runtime tab missing; its tmux session ended'
        if info.get('state') == 'delegated':
            # A hold would end the hook route into its sub-fleet (delegated_route): never held here.
            down = alive is False and subfleet.delegated_lost(self.f, slot, reason)
            self.audit('liveness.delegated_tab_missing', slot=slot, level='error', error=reason,
                       detail={'subfleet_down': down})
            return
        if alive is False and not adopted and slot != advisor.SLOT and self.restart_lost(slot, info, reason):
            return
        # An adopted supervisor proven gone: the parent takes this sub-fleet down (its own liveness check).
        pause = slot == 'supervisor' and not (adopted and alive is False)
        self.transaction('closed-' + uuid.uuid4().hex, lambda: self.hold(slot, reason, pause=pause))

    def peer_tab(self, slot):
        """A live tab of another slot to open ``slot``'s tab next to (workers first, then the supervisor)."""
        names = [n for n in self.f.workers + ['supervisor'] if n != slot]
        return next((p for p in (self.f.agent(n).get('pane') for n in names)
                     if p and self.f.mgr.get_pane_status(p) is not None), None)

    def restart_lost(self, slot, info, why):
        """A worker's session is proven gone: what ``start --resume`` does for a dead tab, for this slot alone
        (§17): release its lease and queue (clear_slot), next generation in place (next_generation), its lost
        work replayed once more under the same rules (requeue_lost / replay_request: attempt count,
        max-attempts, replay false). A resumed generation lost before its READY ran nothing: only replaced.
        False (the caller holds the slot instead): the supervisor (never in tmux, never proven), run paused
        (§7: no rotation while paused; this check comes back after a resume), a fresh generation that never
        said READY (no restart loop), mid handoff / clear."""
        from fleetmux.fleet import REQ, REQQ
        resumed = info.get('resume_generation') == info.get('generation') and info.get('runtime_ready') != '1'
        if (slot == 'supervisor' or self.f.paused() or info.get('handoff_id') or not resumed and (
                info.get('runtime_ready') != '1' or info.get('state') not in ('idle', 'busy', 'held'))):
            return False
        lost = [m for m in self.lost_dispatches() if m.get('slot') == slot]  # read before clear_slot cancels them
        rids = [rid for rid in self.f.r.lrange(REQQ, 0, -1) if self.f.service
                and self.f.r.hmget(REQ + rid, 'status', 'worker') == ['dispatched', slot]]
        if not resumed and not self.clear_slot(slot, False):
            self.transaction('lost-' + uuid.uuid4().hex, lambda: self.hold(slot, why + '; lease not released', pause=False))
            return True
        self.f.mgr.kill_pane(info['pane'])  # forgets the closed tab (after the lease: a later check still has proof)
        prev = self.f.label(slot)
        try:
            resume = self.next_generation(slot, info, why)
        except RuntimeError as exc:
            self.transaction('lost-' + uuid.uuid4().hex, lambda: self.hold(
                slot, why + '; the next generation did not start: ' + str(exc)[:300], pause=False))
            return True
        if not resumed:
            when = 'after the session of ' + prev + ' ended'
            self.transaction('lost-replay-' + uuid.uuid4().hex, lambda: self.requeue_lost(lost, when) if lost else [
                self.replay_request(rid, when=when) for rid in rids])
        self.audit('liveness.restarted', slot=slot, level='error', prev=prev, label=self.f.label(slot), reason=why,
                   detail={'resume_session': resume, 'replayed_dispatches': len(lost), 'replayed_requests': rids})
        return True

    def next_generation(self, slot, info, why):
        """``respawn`` of a lost slot. A Claude worker resumes its own session first (``claude --resume``, the
        transcript must still be there); a resumed generation lost again (or one that never said READY) gets a
        fresh generation from a skeleton handoff note, pointed at PROGRESS.md when the worker keeps one.
        Returns the resumed session id ('' for a fresh generation)."""
        from fleetmux import handoff
        from fleetmux.fleet import agent_cwd
        transcript = info.get('transcript') or ''
        generation, prev = int(info.get('generation') or 0), self.f.label(slot)
        esc = lambda text: text.replace('{', '{{').replace('}', '}}')
        progress = agent_cwd(self.f.cfg, slot) / 'PROGRESS.md'
        thread = self.resumable(slot, info)
        if thread:
            self.f.setf(slot, resume_session=thread, resume_generation=generation + 1)
            self.respawn([slot], True, prompt=self.resume_prompt(slot, why))
            return thread
        note = self.f.run_dir / 'handoffs' / (slot + '-g' + str(generation) + '-lost.md')
        note.parent.mkdir(parents=True, exist_ok=True)
        text = self.f.skeleton(slot, dict(info, handoff_id='lost-' + uuid.uuid4().hex[:8]))
        if progress.is_file():
            text = handoff.add_next_step(text, 'continue from ' + str(progress) + ': the milestones already reached')
        note.write_text(text)
        prompt = ('You are fleet agent {label}. The session of ' + esc(prev) + ' ended unexpectedly (' + esc(why)
                  + '). Read the handoff note ' + esc(str(note)) + ' and AGENTS.md; the previous transcript is '
                  + esc(transcript or 'unknown') + '. Its unfinished work is delivered to you again after READY: '
                  'check what is already on disk before redoing anything. In this turn do no task work: your final '
                  'reply must be exactly READY.')
        self.respawn([slot], True, prompt=prompt, handoff=str(note))
        return ''

    def resumable(self, slot, info):
        """The Claude session the next generation of ``slot`` can continue (``claude --resume``), '' when none: an
        official Claude slot whose transcript is still there and that is not already a resumed generation."""
        from fleetmux.fleet import is_claude
        thread, transcript = info.get('thread') or '', info.get('transcript') or ''
        if (is_claude(self.slot_cfg(slot), slot) and thread
                and info.get('resume_generation') != str(int(info.get('generation') or 0))
                and transcript and Path(transcript).is_file()):
            return thread
        return ''

    def resume_prompt(self, slot, why):
        """Launch prompt ({label}) of a resumed session; its unfinished work is delivered again after READY."""
        from fleetmux.fleet import agent_cwd
        esc = lambda text: text.replace('{', '{{').replace('}', '}}')
        progress = agent_cwd(self.f.cfg, slot) / 'PROGRESS.md'
        return ('Your session was interrupted (' + esc(why) + ') and the daemon resumed it: you are now fleet '
                'agent {label}. Your unfinished work is delivered to you again after READY: check what is '
                'already on disk (' + esc(str(progress)) + ' if it exists) and continue from there instead of '
                'redoing it. In this turn do no task work: your final reply must be exactly READY.')

    def redeliverable(self, slot, msg):
        """§43: may the work cancelled with ``msg`` (in flight on ``slot``) go once more to its resumed session? The
        lost-slot rules (§17): a running task's dispatch or a service request dispatched to this slot, within
        max_attempts; a request with replay false never."""
        from fleetmux.fleet import REQ
        cap = int(self.f.cfg['max_attempts'])
        if not msg.get('message_id') or msg.get('kind') not in ('worker', 'continuation'):
            return False
        if self.f.service:
            req = self.f.r.hgetall(REQ + (msg.get('request_id') or ''))
            return (bool(req) and req.get('status') == 'dispatched' and req.get('worker') == slot
                    and json.loads(req['spec']).get('replay') is not False and int(req.get('replay') or 1) < cap)
        task = self.f.task()
        return (task.get('status') == 'running' and msg.get('request_id') == task.get('id')
                and int(msg.get('replay') or 1) < cap)

    def redeliver(self, slot, msg, when):
        """Queue the cancelled work of a resumed slot once more (requeue_lost / replay_request, replay + 1)."""
        if self.f.service:
            self.transaction('resume-replay-' + uuid.uuid4().hex, lambda: self.replay_request(msg['request_id'], when=when))
        else:
            self.transaction('resume-replay-' + uuid.uuid4().hex, lambda: self.requeue_lost([msg], when))

    def adopted_held_tick(self):
        """§19 (follow-up 2, real run fleet-20261109-053936-beb7e74c): a sub-fleet's adopted supervisor held by
        this daemon (lease ran out while the machine slept, its turn ended with an API error) has no hold exit
        of its own: its StopFailure posts no event and the pane belongs to the parent (no next generation here).
        Once the operator resumed the run (``fleet.sh resume --config <child config>``, i.e. not paused any more):

        * its session still runs (tab there, Claude in it): taken back in that same session, as ``resume_run``
          does for an adopted pane -- Escape (an old turn may still run), the held delivery cancelled, the slot
          idle, and that delivery's text queued again at the front. The pane's waiting rewake fallback (it
          follows the route into this sub-fleet) or its next Stop hook hands it over.
        * session gone: the task ends ``failed``; the parent collects this sub-fleet and starts the slot's next
          generation (§19 collect). A service sub-fleet (no task) stays held for the parent's liveness check."""
        from fleetmux.fleet import RESUME_CLIS
        slot = 'supervisor'
        info = self.f.agent(slot)
        if (info.get('state') != 'held' or self.f.paused() or not self.hooked(slot) or info.get('stopfail_at')
                or not subfleet.adopted(self.f.cfg, slot, info.get('generation'))):
            return  # a StopFailure hold (§24) is stopfail_tick's: its message is already queued again
        pane = info.get('pane')
        alive = (bool(pane) and self.f.mgr.get_pane_status(pane) is not None
                 and self.f.mgr.has_cli_process(pane, RESUME_CLIS))
        if not alive:
            if not self.f.service and self.f.task().get('status') == 'running':
                self.audit('adopted.lost', slot=slot, level='error', pane=pane or '')
                self.finish('failed', 'the adopted supervisor was held and its session is gone after the resume; '
                                      'the parent fleet takes this sub-fleet back')
            return
        state = self.bus.r.hgetall(self.bus.lease_key(slot))
        msg = None
        if state.get('current_delivery'):
            self.f.mgr.send_key(pane, 'Escape')  # abort a turn that may still run before its message comes again
            try:
                msg = self.bus.message(state['message_id'])
                if state.get('status') not in ('uncertain', 'expired'):
                    self.bus.mark_uncertain(slot, msg, 'operator resume of a held adopted supervisor')
                self.bus.resolve_uncertain(slot, msg, 'cancelled', execution_stopped=True)
            except (BusError, ValueError, KeyError) as exc:
                self.audit('adopted.release_failed', slot=slot, level='error', error=str(exc)[:300])
                self.f.pause('held adopted supervisor not released after the resume: ' + str(exc)[:200], kind='adopted',
                             slot='supervisor')
                return  # paused again: no Escape every tick; the next resume tries once more
        self.f.setf(slot, state='idle', idle_confirmed=0, bridge_available=0, busy_since=time.time(),
                    report_retries=0)
        if msg is not None:
            self.queue(slot, msg['body']['text'], request_id=msg.get('request_id'), front=True)
        self.audit('adopted.reattached', slot=slot, level='warn', pane=pane,
                   message_id=(msg or {}).get('message_id', ''))

    # ------------------------------------------------------------------ §24 held watchdog backstop
    def held_knobs(self):
        """fleet.yaml#daemon_watch held_after_s / held_realert_s, re-read at most every 30 s (hot, like the file)."""
        if self.held_knobs_cache is None or time.monotonic() - self.held_knobs_at >= 30:
            knobs, problem = held_watch_knobs()
            regen, regen_problem = regen_knobs()
            requeue, requeue_problem = held_requeue.knobs()
            review, review_problem = pause_review.knobs()
            knobs = dict(knobs, **regen, **requeue, **review)
            problem = '; '.join(p for p in (problem, regen_problem, requeue_problem, review_problem) if p) or None
            if problem and (self.held_knobs_cache is None or self.held_knobs_cache[1] != problem):
                self.audit('held_watch.config_rejected', level='error', error=problem)
            self.held_knobs_cache, self.held_knobs_at = (knobs, problem), time.monotonic()
        return self.held_knobs_cache[0]

    def held_watch_tick(self):
        """§24 held watchdog: the daemon, not an outside session, acts on a slot held (or waiting on a user dialog)
        for ``held_after_s`` awake seconds with no session progress, and on a run paused that long.

        Per held episode, once: recover when the turn is proven stopped (``stop_proof``: settled like the
        StopFailure event that never arrived) or, for a sub-fleet's adopted supervisor, by lifting the pause its own
        hold set (``adopted_held_tick`` then takes it back in its session). A pause is lifted only when this hold
        owns it (``hold_pause``: same fx:service.at) and never past a brake (``pause_owner_resume``). A user dialog
        (above all a delete dialog), a delete dialog on the held pane's screen, or anything not recovered:
        escalated (``held_escalate``), again after ``held_realert_s``. Runs outside any intent (screen, files)."""
        from fleetmux.fleet import SERVICE
        notices, self.held_notices = self.held_notices, []
        for slot, text, detail in notices:
            self.f.setf(slot, held_alert_at=awake.now(), held_tried=1)
            self.held_escalate(slot, text, detail)
        knobs = self.held_knobs()
        after, realert = float(knobs['held_after_s']), float(knobs['held_realert_s'])
        busy = False
        for slot in self.f.names:
            if slot == advisor.SLOT:
                continue  # §27: advisor_tick restarts a held advisor in place; its holds never pause
            info = self.f.agent(slot)
            if slot != 'supervisor' and info.get('requeue_due'):
                # §24 held requeue: an interrupted pure request; gave_up is today's path (held, paused, escalated)
                busy = True
                if held_requeue.step(self, slot, knobs) in ('started', 'waiting', 'gave_up'):
                    continue
                info = self.f.agent(slot)
            dialog = bool(info.get('prompt_wait')) and info.get('prompt_route') == 'user'
            held = info.get('state') == 'held'
            if not held and not dialog:
                if info.get('held_since') or info.get('held_alert_at'):
                    self.f.setf(slot, held_since='', held_at='', held_alert_at='', held_tried='')
                continue
            busy = True
            # §24 supervisor regeneration: an API-error end (regen_due) does not wait for held_after_s
            due = held and not dialog and bool(info.get('regen_due')) and self.regen_on(slot, info, knobs)
            if not info.get('held_since'):
                self.f.setf(slot, held_since=awake.now(), held_at=time.time())
                if not due:
                    continue
                info = self.f.agent(slot)
            since = (awake.since_wall(float(info.get('prompt_wait_at') or time.time())) if dialog
                     else awake.since(float(info['held_since'])))
            if since < after and not due:
                continue
            if (held and info.get('stopfail_at') and info.get('stopfail_retry_at')
                    and not awake.passed(float(info['stopfail_retry_at']))):
                continue  # a StopFailure backoff: stopfail_tick releases it on schedule
            if not dialog and self.f.last_activity(info) > float(info.get('held_at') or 0) + 1:
                # the session moved: a new episode (an API-error end that then moves is not regenerated at once)
                self.f.setf(slot, held_since=awake.now(), held_at=time.time(), regen_due='')
                continue
            # Regeneration keeps its own clock (regen_next_at); the re-alert gate only paces the escalations.
            regen = held and not dialog and self.regen_on(slot, info, knobs)
            gated = bool(info.get('held_alert_at')) and awake.since(float(info['held_alert_at'])) < realert
            if gated and not regen:
                continue
            detail = {'held_s': int(since), 'paused': self.f.paused(), 'state': info.get('state', '')}
            if dialog:
                kind = info.get('prompt_class') or 'unread'
                why = ('a delete confirmation dialog waits for the user; deleting is the user\'s alone, nothing is '
                       'pressed' if kind == 'delete' else 'a confirmation dialog waits for the user; nothing is pressed')
                self.f.setf(slot, held_alert_at=awake.now(), held_tried=1)
                self.held_escalate(slot, why, dict(detail, reason='dialog', dialog_class=kind))
                continue
            # a regenerable supervisor's screen is read once per tick (input box first): the delete class blocks the
            # recovery below as for any slot, and the verdict is passed on to supervisor_regen
            screen = self.regen_screen(slot, info) if regen else None
            kind = ('delete' if screen == 'delete' else None) if regen else self.screen_dialog_class(slot, info)
            if kind == 'delete':
                if gated:
                    continue
                self.f.setf(slot, held_alert_at=awake.now(), held_tried=1)
                self.held_escalate(slot, 'held with a delete confirmation dialog on its screen; deleting is the '
                                   'user\'s alone, nothing is pressed', dict(detail, reason='dialog', dialog_class='delete'))
                continue
            recovered = None
            if info.get('held_tried') != '1':
                self.f.setf(slot, held_tried=1)
                recovered = self.held_recover(slot, info)
            if recovered and not self.f.paused():
                continue
            outcome = None
            if regen and not recovered and self.f.agent(slot).get('state') == 'held':
                outcome = self.supervisor_regen(slot, self.f.agent(slot), knobs, screen=screen)
                if outcome == 'started':
                    continue
                if outcome == 'waiting':
                    # a backoff waits quietly; a network wait past held_after_s is escalated (gated, no count spent)
                    if since < after or self.f.agent(slot).get('regen_state') != 'waiting_network':
                        continue
                    outcome = 'not regenerated (waiting for the network to come back; no count spent)'
            if gated:
                continue
            self.f.setf(slot, held_alert_at=awake.now())
            if outcome == 'exhausted':
                reason = ('held and its regeneration budget is used up (' + str(knobs['supervisor_regen_max'])
                          + ' new generations within ' + str(int(knobs['supervisor_regen_window_s'])) + ' s; last: '
                          + str(self.f.agent(slot).get('regen_reason') or 'unknown')[:200] + '). Check the network '
                          'and the CLI login, then fleetmux resume, or start --resume ' + str(self.f.cfg.get('run')))
            else:
                reason = (recovered + '; the run stays paused (not this hold\'s pause, or a brake)') if recovered else (
                    'held (' + str(self.bus.r.hget(self.bus.msg_key(self.bus.slot_state(slot).get('message_id') or '-'),
                                                   'uncertain_reason') or info.get('stopfail_at') and 'API error '
                                                   'retries used up' or 'execution state unconfirmed')[:300]
                    + ') with no progress, and the program could not prove its turn stopped'
                    + ('; ' + (outcome if outcome.startswith('not regenerated') else 'not regenerated: ' + outcome)
                       if outcome else ''))
            self.held_escalate(slot, reason, dict(detail, reason='not_recovered' if not recovered else 'paused',
                                                  **({'regen': outcome} if outcome else {})))
        service = self.f.r.hgetall(SERVICE)
        if (not busy and service.get('paused') == '1' and service.get('at') and service['at'] != self.pause_alerted
                and awake.since_wall(float(service['at'])) >= after):
            self.pause_alerted = service['at']
            self.held_escalate('supervisor', 'the run has been paused for ' + str(int(awake.since_wall(float(
                service['at'])))) + ' s (' + str(service.get('reason') or '')[:200] + ') with no held slot; the '
                'program does not lift a pause it did not set', {'reason': 'paused', 'paused': True}, pause_only=True)

    def screen_dialog_class(self, slot, info):
        """R4: the bottom of a held pane's screen; 'delete' means no key may be pressed there (§24 delete class)."""
        read = getattr(self.f.mgr, 'capture_output', None)
        if not read or not info.get('pane'):
            return None
        try:
            screen = read(info['pane'], 200) or ''
            return watchdog.dialog_class(self.watchdog.cache.get(self.audit), None, screen) if screen.strip() else None
        except Exception as exc:  # a failed look never takes the daemon down; it only means no recovery by keys
            self.audit('held_watch.capture_failed', slot=slot, level='warn', error=str(exc)[:300])
            return None

    def stop_proof(self, slot, info, lease):
        """What proves the held delivery's turn ended: the StopFailure hook's ``turnend`` mark, or an API-error end
        in the session's transcript, after that delivery was received. None: unproven (never re-sent then)."""
        mid = lease.get('message_id')
        if not mid or not lease.get('current_delivery') or not self.hooked(slot):
            return None
        received = float(self.bus.r.hget(self.bus.msg_key(mid), 'received_at') or 0)
        received = received / 1000.0 if received > 1e11 else received
        try:
            mark = float(self.bus.r.get(self.hook_keys(slot, int(info.get('generation') or 0))['turnend']) or 0)
        except (TypeError, ValueError):
            mark = 0.0
        if mark and mark >= received:
            return 'turnend'
        try:
            ended = claude_hook.failure_end(claude_hook.tail_rows(info.get('transcript') or ''))
        except (OSError, ValueError):
            ended = None
        if ended and float(ended) >= received:
            return 'transcript'
        return None

    def held_recover(self, slot, info):
        """One recovery attempt for a stuck held slot. Returns what was done, or None."""
        lease = self.bus.slot_state(slot)
        proof = self.stop_proof(slot, info, lease)
        done = None
        if proof:
            msg = self.bus.message(lease['message_id'])
            ev = dict(kind='stop_failure', agent=slot, generation=int(info.get('generation') or 0), run_id=self.bus.run_id,
                      thread=info.get('thread'), error='held watchdog: the turn ended with an API error (' + proof
                      + ') and no StopFailure event arrived', text='', refusal=False,
                      delivery={x: msg[x] for x in IDENTITY if x in msg})
            self.transaction('held-proof-' + uuid.uuid4().hex, lambda: self.api_error_retry(slot, ev))
            if self.f.agent(slot).get('stopfail_at'):
                done = 'settled as an API error (' + proof + ') and its message queued again'
                self.audit('held_watch.recovered', slot=slot, level='warn', message_id=lease['message_id'],
                           detail={'proof': proof})
        adopted = slot == 'supervisor' and subfleet.adopted(self.f.cfg, slot, info.get('generation'))
        if (done or adopted) and self.f.paused() and self.hold_pause_lift(slot):
            if not done:
                done = 'its own pause lifted; the adopted supervisor is taken back in its session'
                self.audit('held_watch.resumed_owned_pause', slot=slot, level='warn')
        return done

    def hold_pause_lift(self, slot):
        """Lift the pause this slot's hold set (``hold_pause``), when nobody paused again since; never past a brake."""
        from fleetmux.fleet import SERVICE
        info = self.f.agent(slot)
        try:
            mark = json.loads(info.get('hold_pause') or '{}')
        except ValueError:
            mark = {}
        service = self.f.r.hgetall(SERVICE)
        if not mark or service.get('paused') != '1' or str(service.get('at')) != str(mark.get('at')):
            return False
        label = self.f.label(slot)
        self.transaction('held-resume-' + uuid.uuid4().hex, lambda: self.pause_owner_resume(
            slot, mark.get('message_id') or '', {'reason': 'held watchdog', 'paused_at': mark.get('at')},
            t('resume.held', '{label} was held with no progress; the daemon lifted the pause it had set itself (held '
              'watchdog).', label=label), 'held-' + str(mark.get('at'))))
        if self.f.paused():
            return False
        self.f.setf(slot, hold_pause='')
        return True

    # ------------------------------------------------------------------ §24 supervisor regeneration
    def regen_on(self, slot, info, knobs=None):
        """Does supervisor regeneration apply to this slot: the supervisor of this fleet (root, or a sub-fleet whose
        supervisor is not adopted), switch on, fleet not stopping. Never the adopted supervisor (§19), the advisor
        (§27) or a worker."""
        knobs = knobs or self.held_knobs()
        return (slot == 'supervisor' and bool(knobs.get('supervisor_regen')) and not self.f.stopping
                and not subfleet.adopted(self.f.cfg, slot, info.get('generation')))

    def api_error_of(self, slot, delivery_id):
        """The API / network error text a model-error candidate recorded for this delivery ('' when none)."""
        try:
            rec = json.loads(self.f.agent(slot).get('api_error') or 'null') or {}
        except ValueError:
            rec = {}
        return str(rec.get('text') or '') if rec.get('delivery_id') == delivery_id else ''

    def regen_guard(self, slot, info, screen=True):
        """Why this held supervisor must not be regenerated now ('' when it may): another mechanism owns it, a dialog
        waits for a human, or there is no pane this fleet owns to end (no kill, no proof: escalate only)."""
        if info.get('state') != 'held':
            return 'not held'
        if info.get('handoff_id'):
            return 'handoff in progress'
        if info.get('link_lost') in ('1', 'restarting'):
            return 'link restart (section 35) owns it'
        if info.get('fresh_pending') == '1' or info.get('stopfail_fresh') == '1':
            return 'model refusal: a fresh session (section 37) owns it'
        if (info.get('stopfail_at') and info.get('stopfail_retry_at')
                and not awake.passed(float(info['stopfail_retry_at']))):
            return 'API error backoff still running'
        if info.get('prompt_wait'):
            return 'a dialog waits for the user'
        pane = info.get('pane')
        if not pane or self.f.mgr.get_pane_status(pane) is None:
            return 'its tab is gone (no proof its session stopped)'
        owns = getattr(self.f.mgr, 'owns', None)
        if owns is not None and not owns(pane):
            return 'its pane is not in this fleet\'s registry'
        if screen and self.regen_screen(slot, info):
            return 'a dialog is on its screen'
        return ''

    def regen_screen(self, slot, info):
        """The bottom of a held supervisor's screen, read once: '' (no dialog: blank, or the CLI's own idle input box,
        whatever words are above it), 'delete' (the delete class: no key may be pressed, no recovery either),
        'dialog' (an allowlisted or unknown prompt marker) or 'unreadable'. Anything but '' blocks regeneration."""
        read = getattr(self.f.mgr, 'capture_output', None)
        if not read or not info.get('pane'):
            return ''
        try:
            screen = read(info['pane'], 200) or ''
            if not screen.strip():
                return ''
            table, cli = self.watchdog.cache.get(self.audit), self.watchdog.cli(slot)
            if cli in watchdog.INPUT_BOX_CLIS and watchdog.input_box(
                    watchdog.screen_tail(screen, table['tail_lines']).lower().splitlines(), cli):
                return ''  # the CLI's own idle input box: a delete word above it is history, not a dialog
            if watchdog.dialog_class(table, None, screen) == 'delete':
                return 'delete'
            entry, marker = watchdog.match_prompt(table, screen, cli)
            return 'dialog' if entry or marker else ''
        except Exception as exc:  # a failed look never takes the daemon down; it only means no regeneration
            self.audit('held_watch.capture_failed', slot=slot, level='warn', error=str(exc)[:300])
            return 'unreadable'

    def regen_error_text(self, slot, info):
        lease = self.bus.slot_state(slot)
        reason = self.bus.r.hget(self.bus.msg_key(lease.get('message_id') or '-'), 'uncertain_reason') or ''
        try:
            api = (json.loads(info.get('api_error') or 'null') or {}).get('text') or ''
        except ValueError:
            api = ''
        return ' '.join(x for x in (api, reason, info.get('regen_reason') or '') if x)

    def regen_online(self, slot, info, mode):
        """The connectivity probe before a regeneration (no new dependency): the CLI's declared egress route
        (session_env), then, when the last error is a network error or mode is tcp, the existing TCP probes."""
        if mode == 'off':
            return True
        if session_env.route_closed(self.f, slot, info):
            return False
        if mode == 'tcp' or API_NETWORK_ERROR.search(self.regen_error_text(slot, info)):
            return bool(self.f.network_up())
        return True

    def supervisor_regen(self, slot, info, knobs, screen=None):
        """One step of §24 supervisor regeneration for a held supervisor the existing recovery did not release.
        Returns 'started' | 'waiting' (backoff or offline: no count spent) | 'exhausted' | 'failed' | a guard text.
        Order: guards -> budget -> backoff -> probe -> lift only its own pause -> Watchdog.rotate (successor
        launch-held, kill and verify the old session, confirm_retired, the message once more, gate opened)."""
        now = awake.now()
        def guarded(guard):
            if info.get('regen_state') != 'guarded: ' + guard:
                self.f.setf(slot, regen_state='guarded: ' + guard)
                self.audit('supervisor_regen.guarded', slot=slot, level='warn', error=guard)
            return 'not regenerated (' + guard + ')'
        guard = self.regen_guard(slot, info, screen=False)  # the screen verdict comes from held_watch_tick
        if guard:
            return guarded(guard)
        window, cap = float(knobs['supervisor_regen_window_s']), int(knobs['supervisor_regen_max'])
        backoff = tuple(knobs['supervisor_regen_backoff_s'])
        try:
            log = [float(t) for t in json.loads(info.get('regen_log') or '[]') if now - float(t) < window]
        except (ValueError, TypeError):
            log = []
        if len(log) >= cap:
            if info.get('regen_state') != 'exhausted':
                self.f.setf(slot, regen_state='exhausted')
                self.audit('supervisor_regen.exhausted', slot=slot, level='error',
                           detail={'max': cap, 'window_s': window, 'last': info.get('regen_reason', '')})
            return 'exhausted'
        if info.get('regen_next_at') and now < float(info['regen_next_at']):
            return 'waiting'
        if (self.regen_screen(slot, info) if screen is None else screen):
            return guarded('a dialog is on its screen')
        if not self.regen_online(slot, info, knobs['supervisor_regen_probe']):
            if info.get('regen_state') != 'waiting_network':
                self.audit('supervisor_regen.waits_network', slot=slot, level='warn',
                           detail={'probe': knobs['supervisor_regen_probe']})
            self.f.setf(slot, regen_state='waiting_network', regen_next_at=now + backoff[0])
            return 'waiting'
        if self.f.paused() and not self.hold_pause_lift(slot):
            if info.get('regen_state') != 'guarded: paused':
                self.f.setf(slot, regen_state='guarded: paused')
                self.audit('supervisor_regen.guarded', slot=slot, level='warn',
                           error='the run is paused by someone else (or a brake)')
            return 'not regenerated (the run is paused by someone else, or a brake)'
        why = (self.regen_error_text(slot, info) or 'held, execution state unconfirmed')[:300]
        log.append(now)
        self.f.setf(slot, regen_log=dumps(log), regen_next_at=now + backoff[min(len(log), len(backoff)) - 1],
                    regen_state='rotating', regen_reason=why)
        self.audit('supervisor_regen.started', slot=slot, level='error', reason=why,
                   detail={'attempt': len(log), 'max': cap, 'window_s': window})
        read = getattr(self.f.mgr, 'capture_output', None)
        try:
            screen = (read(info['pane'], 2000) if read else '') or ''
        except Exception:  # noqa: BLE001  (the screen is evidence only)
            screen = ''
        ok = self.watchdog.rotate(slot, 'held: ' + why, screen, reason='regen')
        if not ok:
            self.f.setf(slot, regen_state='failed')
            self.audit('supervisor_regen.failed', slot=slot, level='error', reason=why)
            return 'failed'
        return 'started'

    def resume_held(self):
        """``fleetmux resume`` (after the pause / brake part): what it can do for held slots. A proven-stopped turn is
        released (the StopFailure settlement / held_recover; backoffs unchanged for workers); an unproven held
        supervisor is regenerated at once (guards, budget, probe and backoff still apply); every other held slot is
        listed with the reason and the next step. Adopted supervisors: adopted_held_tick takes them back (§19)."""
        out = {'released': [], 'regenerating': [], 'held': []}
        knobs = self.held_knobs()
        for slot in self.f.names:
            info = self.f.agent(slot)
            if slot == advisor.SLOT or info.get('state') != 'held':
                continue
            if slot == 'supervisor' and subfleet.adopted(self.f.cfg, slot, info.get('generation')):
                continue
            if slot != 'supervisor' and info.get('requeue_due'):
                # §24 held requeue: its next generation now (guards and budget still apply)
                mark = held_requeue.due(info) or {}
                mark['at'] = awake.now() - float(knobs['held_requeue_after_s']) - 1
                self.f.setf(slot, requeue_due=dumps(mark))
                out['regenerating'].append({'slot': slot, 'label': self.f.label(slot), 'in_s': 0, 'requeue': True})
                continue
            label, lease = self.f.label(slot), self.bus.slot_state(slot)
            if info.get('stopfail_at') and not lease.get('current_delivery') and not info.get('regen_due'):
                if slot == 'supervisor' and float(info.get('stopfail_retry_at') or 0) > 0:
                    # its StopFailure backoff is running: stopfail_tick releases it this tick instead of later
                    self.f.setf(slot, stopfail_retry_at=awake.now() - 1)
                    out['released'].append({'slot': slot, 'label': label, 'proof': 'stop_failure'})
                    continue
                if slot != 'supervisor':
                    # R1: a worker's StopFailure backoff / exhausted hold is left exactly as it is; only reported.
                    retry = float(info.get('stopfail_retry_at') or 0)
                    out['held'].append({'slot': slot, 'label': label, 'reason': 'held after an API error ('
                                        + ('retry scheduled' if retry > 0 else 'retries used up') + ')',
                                        'next': ('the program retries it on its own in '
                                                 + str(max(0, int(retry - awake.now()))) + ' s' if retry > 0 else
                                            'it is released when its session ends a normal turn; dispatch its '
                                            'work elsewhere, or fleetmux start --resume ' + str(self.f.cfg.get('run')))})
                    continue
                # a supervisor at the retry cap (no backoff running) falls through to regeneration below
            proof = self.stop_proof(slot, info, lease) if lease.get('current_delivery') else None
            if proof:
                # Runs inside the control event's intent: no nested transaction here. The held watch settles it on
                # its next tick through the existing held_recover (api_error_retry; backoff semantics unchanged).
                self.f.setf(slot, held_tried='', held_alert_at='', held_at=time.time(),
                            held_since=awake.now() - float(knobs['held_after_s']) - 1)
                out['released'].append({'slot': slot, 'label': label, 'proof': proof, 'when': 'next tick'})
                continue
            info = self.f.agent(slot)
            reason = str(self.bus.r.hget(self.bus.msg_key(lease.get('message_id') or '-'), 'uncertain_reason')
                         or self.regen_error_text(slot, info) or 'execution state unconfirmed')[:300]
            if slot == 'supervisor' and self.regen_on(slot, info, knobs):
                guard = self.regen_guard(slot, info)
                window, cap = float(knobs['supervisor_regen_window_s']), int(knobs['supervisor_regen_max'])
                now = awake.now()
                try:
                    used = [t for t in json.loads(info.get('regen_log') or '[]') if now - float(t) < window]
                except (ValueError, TypeError):
                    used = []
                if not guard and len(used) < cap:
                    self.f.setf(slot, regen_due=now)
                    due = max(now, float(info.get('regen_next_at') or 0))
                    out['regenerating'].append({'slot': slot, 'label': label,
                                                'in_s': max(0, int(due - now))})
                    continue
                nxt = ('answer the dialog in its pane, then fleetmux resume' if 'dialog' in guard else
                       'the regeneration budget is used up (' + str(len(used)) + ' within ' + str(int(window))
                       + ' s); check the network and the CLI login, then fleetmux resume again later, or '
                       'fleetmux start --resume ' + str(self.f.cfg.get('run')) if not guard else
                       'not regenerated now (' + guard + '); the program retries on its own, or fleetmux start '
                       '--resume ' + str(self.f.cfg.get('run')))
            elif slot == 'supervisor':
                nxt = ('stop that session by hand (end the CLI in its tab), then fleetmux resume; or set '
                       'daemon_watch.supervisor_regen: true')
            else:
                nxt = ('its turn is not proven stopped; when its session ends a turn it is released (the result is '
                       'not applied). Dispatch its work elsewhere, or fleetmux start --resume '
                       + str(self.f.cfg.get('run')))
            out['held'].append({'slot': slot, 'label': label, 'reason': 'held (' + reason + '); its turn is not '
                                'proven stopped', 'next': nxt})
        if out['released'] or out['regenerating'] or out['held']:
            self.audit('resume.held', level='warn', detail=out)
        return out

    def channel_alarm(self, text):
        """§24 / §53: a root supervisor that cannot run tells the user through the run's channels with the existing
        default-on daemon_down push (the fleet's own control loop is down). Only entries whose push list carries it;
        an entry a managed run already reaches through run_closed (lifecycle_close.notify) is not sent twice.
        Returns the status written to notifications.jsonl."""
        entries = [e for e in self.f.cfg.get('channels') or [] if isinstance(e, dict)]
        if not entries:
            return 'no_channel'
        from fleetmux import channels as channel_pkg
        managed = lifecycle_close.managed(self.f)
        push = lambda e: e.get('push') if e.get('push') is not None else channel_pkg.DEFAULT_PUSH
        if not any('daemon_down' in push(e) and not (managed and 'run_closed' in (e.get('push') or ()))
                   for e in entries):
            return 'no_channel'
        from fleetmux.channels import actor, envelope
        try:
            res = actor.send_direct(self.f.cfg, envelope.outbound('daemon_down', target='last', text=text))
        except Exception as exc:  # noqa: BLE001  (a notice never takes the daemon down)
            self.audit('held_watch.notify_failed', slot='supervisor', level='error', error=type(exc).__name__)
            return 'failed'
        status = res.get('status') if isinstance(res, dict) else None
        return 'sent' if status == 'sent' else (status or 'failed')

    def held_escalate(self, slot, text, detail, pause_only=False):
        """Report a stuck slot / run upwards through the existing paths: a sub-fleet -> its parent fleet's
        supervisor (``subfleet-notices``, kind ``held``); a root fleet -> its supervisor (task mode, not about
        itself) and, for a managed run, the tg / parent channels (``lifecycle_close.notify``)."""
        from fleetmux import fleet as legacy
        label = self.f.label(slot)
        self.audit('held_watch.escalated', slot=slot, level='error', error=text[:300], detail=detail)
        whole = ('the run' if pause_only else label) + ': ' + text
        parent = self.f.cfg.get('parent_config')
        if parent:
            try:
                folder = Path(legacy.load(parent)['run']) / 'subfleet-notices'
                folder.mkdir(parents=True, exist_ok=True)
                legacy.save(folder / ('held-' + uuid.uuid4().hex[:12] + '.json'), dict(
                    detail, kind='held', origin_run=str(self.f.run_dir), task=self.f.cfg.get('task_id'), slot=slot,
                    label=label, text=text))
            except Exception as exc:  # a notice never takes the daemon down
                self.audit('held_watch.notify_failed', slot=slot, level='error', error=str(exc)[:300])
        elif (not pause_only and slot != 'supervisor' and not self.f.service
              and self.f.task().get('status') == 'running'):
            self.f.queue('supervisor', '[' + label + ' report]\nHELD: ' + text + '. Held for ' + str(detail.get('held_s', 0))
                         + ' s. Check that slot, then dispatch its work elsewhere, or reply BLOCKED if a human is '
                         'needed.\n\nReply with the JSON format from AGENTS.md.')
        if not parent and lifecycle_close.managed(self.f):
            from fleetmux import lifecycle
            try:
                policy = lifecycle.get_mode(self.f.cfg['lifecycle'])
            except ValueError:
                policy = {}
            try:
                lifecycle_close.notify(self.f, 'slot_held', '[fleet ' + lifecycle_close.fleet_name(self.f) + '] ' + whole
                                       + ' run: ' + self.f.cfg['run'], policy)
            except Exception as exc:
                self.audit('held_watch.notify_failed', slot=slot, level='error', error=str(exc)[:300])
        if not parent and slot == 'supervisor' and not pause_only:
            # §24 supervisor regeneration: about the supervisor itself, never queued to it. A root also tells the user
            # through its channels (daemon_down); without channels nothing leaves the machine and the record says so.
            line = '[fleet ' + lifecycle_close.fleet_name(self.f) + '] ' + whole + ' run: ' + str(self.f.cfg['run'])
            status = self.channel_alarm(line)
            if status != 'no_channel' or not lifecycle_close.managed(self.f):
                try:
                    with open(Path(self.f.cfg['run']) / 'notifications.jsonl', 'a', encoding='utf-8') as fh:
                        fh.write(dumps({'event': 'supervisor_held', 'channel': 'channels', 'text': line,
                                        'status': status, 'run': self.f.cfg.get('run'), 'at': time.time()}) + '\n')
                except OSError as exc:
                    self.audit('held_watch.notify_failed', slot=slot, level='error', error=str(exc)[:300])
            if status == 'no_channel' and not lifecycle_close.managed(self.f):
                # §24 "No route for human notices": nothing left the machine (a managed run counts in notify)
                from fleetmux import notify_route
                try:
                    notify_route.record_undelivered(self.f.cfg, 'supervisor_held', line)
                except OSError as exc:
                    self.audit('notify.count_failed', slot=slot, level='error', error=str(exc)[:300])
            self.audit('held_watch.notified', slot=slot, level='warn', detail={'channels': status})
        card = routing_card(whole, kind='notice', slot=label, severity='warn')
        card['metadata']['event_id'] = 'ui-heldwatch-' + digest(slot + ':' + str(time.time()))[:32]
        self.ui('supervisor', card)

    def resume_fallback(self):
        """A resumed session that did not say READY in time (``expire`` marks it): a fresh generation instead."""
        for slot in self.f.workers:
            info = self.f.agent(slot)
            if info.get('resume_failed') == '1' and info.get('state') == 'starting':
                self.f.setf(slot, resume_failed=0)
                try:
                    self.next_generation(slot, info, 'the resumed session did not say READY in time')
                except RuntimeError as exc:
                    self.transaction('lost-' + uuid.uuid4().hex, lambda: self.hold(
                        slot, 'resume fallback: the next generation did not start: ' + str(exc)[:300], pause=False))
                self.audit('liveness.resume_failed', slot=slot, level='error', label=self.f.label(slot))

    def render_cards(self, refresh_every=5.0):
        """Official-CLI slots have no UI-only entry: render their cards off-model (tui_cards)."""
        slots = [slot for slot in self.f.names if tui_slot(self.f.cfg, slot)]
        if slots and tui_cards.render(self, slots, self.f.run_dir):
            self.cards_dirty = True
        if self.cards_dirty and time.monotonic() - self.cards_refreshed >= refresh_every:
            self.cards_dirty, self.cards_refreshed = False, time.monotonic()
            self.f.refresh_history()  # history page "Cards" view; reload only, no focus change

    def tick(self):
        from fleetmux.fleet import REQ, REQQ, SERVICE, task_deadline as legacy_deadline
        now = time.time()  # the bus (lease_until / deadline_at, written by the bridges)
        awake_now = awake.now()  # request timeouts, startup deadlines and the task budget: awake seconds (§35)
        if not self.gap_done:
            self.daemon_gap(now)
        self.gap_done = False
        # Timeouts and cancellation effects use the same intent machinery as results.
        def expire():
            if self.f.service:
                from fleetmux.fleet import request_limit
                for rid in self.f.r.lrange(REQQ, 0, -1):
                    req = self.f.r.hgetall(REQ + rid)
                    if not req:
                        continue
                    limit = request_limit(json.loads(req['spec']), self.f.cfg)  # None: no wall-clock deadline
                    if limit is not None and midturn.effective_age(req, awake_now) >= limit:
                        # §43: a stale answer, not a fault. Only the worker executing it is held (its late
                        # answer is never applied); the fleet keeps dispatching. The supervisor is never held
                        # for it: its routing decision on a finished request is skipped (dispatch.unknown_request).
                        for slot in self.f.names:
                            state = self.f.r.hgetall(self.bus.lease_key(slot))
                            if (slot != 'supervisor' and state.get('message_id') and self.f.r.hget(
                                    self.bus.msg_key(state['message_id']), 'request_id') == rid):
                                self.hold(slot, 'request timeout; execution stop unconfirmed', pause=False)
                        self.publish(rid, 'timeout', worker=req.get('worker') or 'supervisor',
                                     error='no valid answer within ' + str(int(limit)) + ' s')
            task = self.f.task()
            if task.get('status') == 'running' and awake_now >= legacy_deadline(task, awake_now):
                self.finish('budget_exhausted', 'time budget exhausted', kind='budget')
            for slot in self.f.names:
                info = self.f.agent(slot)
                state = self.f.r.hgetall(self.bus.lease_key(slot))
                if (info.get('state') == 'starting' and info.get('advice_hold') != '1'  # held for its first advisor sheet
                        and not session_env.held(self.f, slot, info)  # held by its launch wait for the route port
                        # a dialog before READY waits for a human (plan F B-2b): not a dead start
                        and info.get('prompt_wait') != 'startup-g' + str(info.get('generation', '0'))
                        and awake_now >= float(info.get('startup_deadline') or 0)):
                    if info.get('resume_generation') == info.get('generation') and slot in self.f.workers:
                        self.f.setf(slot, resume_failed=1)  # a resumed session: a fresh generation next (tick)
                        continue
                    if (slot == 'supervisor' and info.get('regen_gen') == info.get('generation')
                            and self.regen_on(slot, info)):
                        # §24: a regenerated supervisor that did not say READY is held again and regenerated within
                        # the budget (its attempt is already counted), never a blocked task / paused service.
                        self.hold(slot, 'regenerated supervisor did not say READY in time', pause=False)
                        self.f.setf(slot, regen_due=awake.now(), regen_reason='the new generation did not say READY')
                        self.audit('supervisor_regen.not_ready', slot=slot, level='error')
                        continue
                    self.f.setf(slot, state='dead', bridge_available=0, runtime_ready=0)
                    if slot == advisor.SLOT:  # §27: restarted in place by advisor_tick, never blocks the task
                        self.audit('advisor.startup_timeout', slot=slot, level='error')
                    else:
                        prior = self.f.r.hgetall(SERVICE)
                        self.finish('blocked', slot + ' startup timed out before READY', kind='startup', slot=slot)
                        # A late READY of this generation revives the slot; it lifts the pause only when this
                        # timeout set it (not paused before) and nothing paused again since (startup_late).
                        if self.f.service:  # a task run ended blocked: nothing for a late READY to undo
                            ours = prior.get('paused') != '1'
                            self.f.setf(slot, startup_timeout=dumps({'generation': str(info.get('generation', '0')),
                                        'at': self.f.r.hget(SERVICE, 'at') if ours else ''}))
                elif state.get('current_delivery') and state.get('status') not in ('uncertain', 'expired'):
                    mid = state['message_id']
                    deadline = int(self.f.r.hget(self.bus.msg_key(mid), 'deadline_at'))
                    if info.get('midturn_id') and info.get('prompt_wait'):
                        deadline = max(deadline, int(now * 1000) + 1000)
                    if (int(state['lease_until']) <= int(now*1000) < deadline
                            and not tui_slot(self.slot_cfg(slot), slot)):
                        if now < self.hb_grace_until:
                            continue  # just woken: the bridge slept too and renews within the grace (§35)
                        self.link_lost(slot, 'lease not renewed by the bridge')  # the work's own deadline still runs
                        continue
                    elif int(state['lease_until']) <= int(now*1000) < deadline and self.reclaim_tui(slot):
                        continue  # the daemon did not run: its own lease, taken back (§24)
                    elif (int(state['lease_until']) <= int(now*1000) < deadline and not self.f.service
                            and slot in self.f.workers and slot != advisor.SLOT):
                        # The daemon renews an official CLI worker's lease itself (renew_tui): it ran out only
                        # because the daemon did not run (e.g. a lid-closed sleep). Hold this slot only (§24).
                        self.hold(slot, 'lease expired without renewal (daemon not running, e.g. system sleep); '
                                  'execution stop unconfirmed', pause=False)  # task mode: nothing to publish; the card tells
                        continue
                    elif int(now*1000) >= min(deadline, int(state['lease_until'])):
                        if not self.requeue_hold(slot, 'lease/delivery deadline expired; execution stop unconfirmed',
                                                 'lease_expired'):
                            self.hold(slot, 'lease/delivery deadline expired; execution stop unconfirmed',
                                      public_status='timeout')
                        continue
                    # Stalls are the watchdog's (docs/design.md §24): Escape ladder, then kill + next generation.
                elif info.get('state') == 'held' and info.get('stopfail_at') and not state.get('current_delivery'):
                    self.stopfail_tick(slot, info, now)
                self.heartbeat_check(slot, info)
        self.transaction('tick-' + uuid.uuid4().hex, expire)
        self.stopfail_notify()
        self.resume_fallback()
        self.held_watch_tick()  # before adopted_held_tick: it may lift the pause that held an adopted supervisor
        self.adopted_held_tick()
        try:  # §24 pause review: here, not in job_tick (job_tick returns while the run is paused)
            pause_review.tick(self, self.held_knobs())
        except Exception as exc:  # a review never takes the daemon down
            self.audit('pause_review.failed', level='error', error=str(exc)[:300])
        self.clear_tick(awake_now)  # clear_deadline is awake time
        self.renew_tui()
        self.render_cards()
        self.watchdog.tick(now)  # screen / keys / kill / spawn: side effects, outside any Redis intent
        for slot in self.f.names:
            info = self.f.agent(slot)
            if info.get('handoff_id') and info.get('state') in ('handoff', 'held') and now >= float(info.get('handoff_deadline') or 0):
                self.f.finish_rotation(slot, timed_out=True)
        if now >= self.f.next_liveness:
            self.f.next_liveness = now+30
            for slot in self.f.names:
                info = self.f.agent(slot)
                if info.get('pane') and self.f.mgr.get_pane_status(info['pane']) is None:
                    self.tab_missing(slot, info)
                elif slot == 'supervisor' and info.get('pane'):
                    try:  # outside the per-part guards: a failing probe must never take the daemon down
                        self.cli_check(info)
                    except Exception as exc:
                        self.audit('cli_check.failed', level='error', error=str(exc)[:300])
        self.handoff_resume_tick()  # last: every pause this tick may set has landed (docs/design.md §4)

    def stopfail_notify(self):
        """tg / parent notices of used-up API-error retries (subprocesses: never inside an intent)."""
        notices, self.stopfail_notices = self.stopfail_notices, []
        if not notices or not lifecycle_close.managed(self.f):
            return
        from fleetmux import lifecycle
        try:
            policy = lifecycle.get_mode(self.f.cfg['lifecycle'])
        except ValueError:
            policy = {}
        for text in notices:
            try:
                lifecycle_close.notify(self.f, 'slot_held', '[fleet ' + lifecycle_close.fleet_name(self.f) + '] ' + text
                                       + ' run: ' + self.f.cfg['run'], policy)
            except Exception as exc:  # a notice never takes the daemon down
                self.audit('stop_failure.notify_failed', level='error', error=str(exc)[:300])

    def cli_check(self, info):
        """§36: an official-CLI supervisor whose CLI exited leaves a shell in its tab (auto_close=False) and, unlike a
        pi bridge, no heartbeat. Two liveness checks in a row (30 s apart) without the CLI process = gone: restarted
        as a lost link (watchdog.link_restart -> rotate)."""
        from fleetmux.fleet import RESUME_CLIS
        probe = getattr(self.f.mgr, 'has_cli_process', None)
        if (probe is None or info.get('state') not in ('idle', 'busy') or info.get('runtime_ready') != '1'
                or not tui_slot(self.slot_cfg('supervisor'), 'supervisor')
                or subfleet.adopted(self.f.cfg, 'supervisor', info.get('generation'))):
            return
        if probe(info['pane'], RESUME_CLIS):
            if info.get('cli_missing', '0') != '0':
                self.f.setf('supervisor', cli_missing=0)
            return
        misses = int(info.get('cli_missing') or 0) + 1
        self.f.setf('supervisor', cli_missing=0 if misses >= 2 else misses)
        if misses >= 2:
            self.transaction('cli-gone-' + uuid.uuid4().hex, lambda: self.link_lost(
                'supervisor', 'supervisor CLI exited; only a shell is left in its tab'))

    def run(self):
        marker = self.prefix + ':boot'
        first = self.bus.r.set(marker, 'provisioning', nx=True)
        control = None
        try:
            try:
                from fleetmux.control_view import start_control
                control = start_control(self.f.cfg)
            except Exception as exc:
                self.audit('control.unavailable', level='error', error=str(exc))
            if first:
                self.f.start_all()
                self.f.job_carry_in()  # section 58 "Restart": open jobs of a run this start could not continue
                self.bus.r.set(marker, 'running')
            elif self.f.resume_requested:
                self.resume_run()  # explicit start --resume: reconcile, respawn, replay once
            else:
                self.recover()  # no start_all, no Redis shutdown
            self.f.midturn_probe.start()
            from fleetmux import applet_host
            while not self.f.stopping or applet_host.stop_wait(self.f):  # root host: applet leases end first
                self.bus.r.set('fx:daemon:beat', str(time.time()))
                self.step()
                self.f.midturn_apply()
                self.f.pump()
                self.f.tick()  # Fleet.tick: roster reload, result.json, sub-fleets, then this tick
            if self.f.explicit_stop:
                from fleetmux import applet_host, restart_state
                restart_state.on_stop(self.f)  # section 60: the state before anything is taken down (never raises)
                applets_stopped = applet_host.stop_all(self.f)  # host.draining, SIGTERM, grace, SIGKILL (applets.md 2)
                children_stopped = subfleet.stop_children(self.f) and applets_stopped  # descendants first, panes taken back
                all_stopped = all([self.retire(slot, 'explicit fleet stop') for slot in self.f.names]) and children_stopped
                all_stopped = tabs.stop_all(self.f) and all_stopped  # companion tabs close with the fleet (§31)
                if all_stopped:
                    self.bus.r.set(marker, 'stopped')
                    # An explicit stop finishes the run: start --resume refuses it, auto starts anew.
                    from fleetmux.fleet import save_result
                    self.f.write_result()
                    if getattr(self.f, 'suspending', False):  # section 58 "Restart": the task stays running, resumable
                        from fleetmux.fleet import JOB_SUSPEND_REASON
                        save_result(self.f.run_dir, self.f.task(), 'suspended', JOB_SUSPEND_REASON)
                    elif not (self.f.run_dir / 'result.json').exists():
                        save_result(self.f.run_dir, {}, 'stopped',
                                    (self.f.lifecycle_close or {}).get('reason') or 'explicit fleet stop')
                    self.f.job_stopped()  # section 58: the last jobs.json write before Redis goes down
                else:
                    self.f.explicit_stop = False  # preserve Redis/evidence for unconfirmed stop
                    self.audit('stop.unconfirmed', level='error')
                # channels (P1e, brief v2 §2.1-8): run_closed to last, then the bounded actor drain; both no-ops
                # without cfg channels, both before finalize and before Redis closes
                from fleetmux.channels import actor as channel_actor
                channel_actor.enqueue_run_closed(self.f)
                self.f.channel_stop_all(drain_s=5)
                lifecycle_close.finalize(self.f, all_stopped)  # closed_by/closed_at, notifications
        finally:
            self.f.midturn_probe.close()
            if control:
                try:
                    control.stop(timeout=1.0)
                except Exception:
                    pass
            self.close()
