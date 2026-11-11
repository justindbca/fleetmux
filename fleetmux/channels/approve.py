"""Delete-dialog approval from the phone (P6b; contracts §8 "删除确认框的用户出口", brief v2 §2.1-5 / -13).

  * ``register(r, slot, info, lease, scope, prompt, tail, digest, screen, keys_approve, paths, pause_reason, *,
    ttl_s=None) -> pending id``: ``fx:channel:pending:<p-xxxxxxxx>`` (status pending, EX ttl_s), the slot index
    ``fx:channel:pending_slot:<slot>``, the slot's older record marked stale;
  * ``card(rec)``: the neutral card (``/approve <id>`` only when ``keys_approve`` is set, ``/reject <id>``);
  * ``handle_command(r, env, cmd, *, post, reply, confirm_s=None)``: step one (no nonce) -> confirming + nonce
    (only its sha256 stored) + the confirm card; step two / reject -> ``post`` a ``dialog_user_answer`` event;
  * ``answer_dialog(r, ev, *, audit=None, now=None)``: the daemon side of step two (``RuntimeV2.user_dialog_answer``);
  * ``mark_done(r, slot, reason)``: the human answered at the desk; a late phone step gets ``pending_gone``.

The nonce never reaches Redis (only its sha256), an audit, the supervisor or exec. A record is never written in a
run without ``channels`` (the callers only call in when ``cfg['channels']`` is set). The P8k / P8l names further
down are still A2' stubs.
"""
import hashlib
import json
import re
import secrets
import time

from dataclasses import dataclass

from fleetmux import channels
from fleetmux.channels import envelope

TAIL_MAX = 2000           # contracts §8 key table: tail <= 2000 chars
CARD_TAIL_LINES = 15      # the card shows the screen tail's last 15 lines
LIVE = ('pending', 'confirming')
DECISION_TEXT = {'approve': '批准', 'reject': '拒绝'}
NO_KEY_TEXT = '没有登记的同意键，请在电脑上处理'


def _key(pid):
    return channels.PENDING + pid


def _command(tail):
    """The command shown on the card: the line after a ``Bash command`` header, else the first non-empty line."""
    lines = [x.strip() for x in str(tail or '').splitlines()]
    for n, line in enumerate(lines):
        if line.lower().rstrip(':') in ('bash command', 'command'):
            for nxt in lines[n + 1:]:
                if nxt:
                    return nxt[:300]
    return next((x for x in lines if x), '')[:300]


def _keys_list(rec):
    try:
        keys = json.loads(rec.get('keys_approve') or '[]')
    except ValueError:
        return []
    return [k for k in keys if isinstance(k, str)] if isinstance(keys, list) else []


DIALOG_TITLE = '删除确认'      # the phone card of a delete dialog (unchanged text)
NEUTRAL_TITLE = '操作确认'     # M173: any other dialog class (P8j) when the caller names no title


def register(r, slot, info, lease, scope, prompt, tail, digest, screen, keys_approve, paths, pause_reason, *,
             ttl_s=None, dialog_class=None, title=None):
    ttl = int(ttl_s or channels.APPROVE_TTL_S)
    if ttl <= 0:
        raise ValueError('approve_ttl_s must be positive')
    now = time.time()
    pid = 'p-' + secrets.token_hex(4)
    while r.exists(_key(pid)):
        pid = 'p-' + secrets.token_hex(4)
    tail = str(tail or '')[-TAIL_MAX:]
    rec = {'id': pid, 'slot': slot, 'generation': str((info or {}).get('generation', '')),
           'session_id': str((info or {}).get('thread', '') or ''), 'scope': str(scope or ''),
           'digest': str(digest or ''), 'prompt': str(prompt or ''),
           'command': _command(tail), 'tail': tail, 'screen': str(screen or ''),
           'paths': json.dumps(list(paths or []), ensure_ascii=False),
           'keys_approve': json.dumps(list(keys_approve or [])), 'pause_reason': str(pause_reason or ''),
           'created': repr(now), 'confirm_until': '', 'expires': repr(now + ttl), 'status': 'pending',
           'nonce_hash': '', 'sender': '', 'conversation': '', 'decision': '',
           # M173: a dialog record names its kind and class (P8j: install / send_out / ... ; default delete)
           'kind': 'dialog', 'dialog_class': str(dialog_class or 'delete')}
    if title:
        rec['title'] = str(title)
    old = r.get(channels.PENDING_SLOT + slot)
    if old and old != pid:
        _set_if(r, old, LIVE + ('answered',), {'status': 'stale'})
    pipe = r.pipeline(transaction=True)
    pipe.hset(_key(pid), mapping=rec)
    pipe.expire(_key(pid), ttl)
    pipe.set(channels.PENDING_SLOT + slot, pid, ex=ttl)
    pipe.execute()
    return pid


def card(rec):
    pid = rec['id']
    try:
        paths = json.loads(rec.get('paths') or '[]')
    except ValueError:
        paths = []
    tail = '\n'.join(str(rec.get('tail', '')).splitlines()[-CARD_TAIL_LINES:])
    lines = ['槽 ' + rec.get('slot', '') + '（代数 ' + str(rec.get('generation', '')) + '）',
             '命令：' + rec.get('command', '')]
    if paths:
        lines.append('路径：' + '、'.join(str(x) for x in paths))
    lines += ['', tail]
    buttons = []
    if _keys_list(rec):
        buttons.append({'label': '批准', 'command': '/approve ' + pid})
    else:
        lines += ['', NO_KEY_TEXT]
    buttons.append({'label': '拒绝', 'command': '/reject ' + pid})
    return envelope.card(_dialog_title(rec), '\n'.join(lines), buttons, ref=pid)


def _is_delete(rec):
    return (rec.get('dialog_class') or 'delete') == 'delete'


def _dialog_title(rec):
    """M173: 删除确认 only for a delete dialog; another class -> the record's title, else a neutral title."""
    return DIALOG_TITLE if _is_delete(rec) else str(rec.get('title') or NEUTRAL_TITLE)


def _allowed(env):
    """S2: the base class already checked these; re-check the whitelist, private and verified here as well."""
    if env.get('private') is not True or env.get('verified') is not True:
        return 'not a private verified chat'
    try:
        account = channels.load_account(env.get('channel', ''), env.get('account', ''))
    except (OSError, ValueError):
        return 'unknown account'
    allow = getattr(account, 'allow', None)
    if allow is None and isinstance(account, dict):
        allow = account.get('allow')
    if not allow or env.get('sender') not in set(allow):
        return 'sender not allowed'
    return ''


def handle_command(r, env, cmd, *, post, reply, confirm_s=None):
    pid = str(cmd.get('id') or '')
    why = _allowed(env)
    if why:
        reply(envelope.receipt('refused', id=pid, reason=why))
        return
    rec = r.hgetall(_key(pid)) if pid else {}
    now = time.time()
    if not rec or rec.get('status') not in LIVE or float(rec.get('expires') or 0) <= now:
        reply(envelope.receipt('pending_gone', id=pid))
        return
    kind = cmd.get('kind')
    action = rec.get('kind') == 'action'
    if action and kind == 'approve' and not cmd.get('nonce'):
        _action_step_one(r, env, rec, pid, reply, confirm_s, now)
        return
    if kind == 'approve' and not cmd.get('nonce'):
        if not _keys_list(rec):
            reply(envelope.receipt('no_key', id=pid))
            return
        window = float(confirm_s if confirm_s is not None else channels.APPROVE_CONFIRM_S)
        nonce = secrets.token_hex(8)
        if not _set_if(r, pid, LIVE, {'status': 'confirming', 'nonce_hash': hashlib.sha256(nonce.encode()).hexdigest(),
                                      'confirm_until': repr(now + window), 'sender': env.get('sender', ''),
                                      'conversation': env.get('conversation', ''),
                                      'channel': env.get('channel', ''), 'account': env.get('account', '')}):
            reply(envelope.receipt('pending_gone', id=pid))    # answered / expired since the read above
            return
        if _is_delete(rec):
            text = envelope.receipt('confirm_delete', command=rec.get('command', ''), seconds=int(window))
            reply(envelope.card('确认删除', text, [{'label': '确认删除', 'command': '/approve ' + pid + ' ' + nonce},
                                                   {'label': '取消', 'command': '/reject ' + pid}], ref=pid))
        else:  # M173 / U13: a non-delete dialog class confirms with the neutral confirm_action text
            text = envelope.receipt('confirm_action', action=rec.get('command', ''), seconds=int(window))
            reply(envelope.card(_dialog_title(rec), text, [{'label': '确认', 'command': '/approve ' + pid + ' ' + nonce},
                                                          {'label': '取消', 'command': '/reject ' + pid}], ref=pid))
        return
    decision = 'approve' if kind == 'approve' else 'reject'
    ev = {'kind': 'dialog_user_answer', 'pending_id': pid, 'decision': decision,
          'nonce': str(cmd.get('nonce') or '') if decision == 'approve' else '',
          'channel': env.get('channel', ''), 'account': env.get('account', ''), 'sender': env.get('sender', ''),
          'conversation': env.get('conversation', '')}
    if decision == 'approve' and cmd.get('code'):
        ev['code'] = str(cmd['code'])
    if action:   # P8k: oob.check (second_channel) needs the answering chat's own private / verified flags
        ev.update(private=env.get('private') is True, verified=env.get('verified') is True)
    if post is None:                                            # brief §2.1-5: no daemon link -> unconfirmed
        reply(envelope.receipt('unconfirmed', id=pid))
        return
    answer = post(ev) or {}
    if answer.get('status') == 'accepted' and action:
        reply(envelope.receipt('action_taken', id=pid, decision=DECISION_TEXT[decision]))
    elif action and answer.get('reason') == 'oob':
        reply(envelope.receipt('oob_required', id=pid))
    elif answer.get('status') == 'accepted':
        reply(envelope.receipt('answer_taken', decision=DECISION_TEXT[decision]))
    elif answer.get('reason') in ('gone', 'done', 'missing', 'expired'):
        reply(envelope.receipt('pending_gone', id=pid))
    else:
        reply(envelope.receipt('changed', id=pid, reason=answer.get('reason') or 'no answer'))


def _action_step_one(r, env, rec, pid, reply, confirm_s, now):
    """P8k step one: the record's epoch must still be the current one with no /stop pending (else stale / stopped,
    refused, no nonce; not atomic -- ISSUE is the boundary, C3). Then confirming + a fresh nonce and window, a
    plain confirm_action receipt and the card that carries the nonce (the only place it is shown)."""
    try:
        cur = current_epoch(r)
    except EpochError:
        reply(envelope.receipt('refused', id=pid, reason='epoch_malformed'))
        return
    if rec.get('epoch', '') != str(cur) or stop_pending(r):
        _set_if(r, pid, LIVE, {'status': 'stale', 'stale_reason': 'stopped'})
        reply(envelope.receipt('refused', id=pid, reason='stopped'))
        return
    window = float(confirm_s if confirm_s is not None else channels.APPROVE_CONFIRM_S)
    nonce = secrets.token_hex(8)
    if not _set_if(r, pid, LIVE, {'status': 'confirming', 'nonce_hash': hashlib.sha256(nonce.encode()).hexdigest(),
                                  'confirm_until': repr(now + window), 'sender': env.get('sender', ''),
                                  'conversation': env.get('conversation', ''), 'channel': env.get('channel', ''),
                                  'account': env.get('account', '')}):
        reply(envelope.receipt('pending_gone', id=pid))
        return
    reply(envelope.receipt('confirm_action', action=rec.get('action_text', ''), seconds=int(window)))
    reply(envelope.card('确认执行', str(rec.get('action_text', '')),
                        [{'label': '确认执行', 'command': '/approve ' + pid + ' ' + nonce},
                         {'label': '取消', 'command': '/reject ' + pid}], ref=pid))


def answer_dialog(r, ev, *, audit=None, now=None):
    """Step two / reject on the daemon side: accept only a live record whose nonce (approve), first-step sender and
    conversation match, inside ``confirm_until`` (approve) and ``expires``. Writes nothing unless accepted."""
    now = time.time() if now is None else now
    pid = str(ev.get('pending_id') or '')
    rec = r.hgetall(_key(pid)) if pid.startswith('p-') else {}
    decision = ev.get('decision')
    reason = ''
    if not rec:
        reason = 'gone'
    elif rec.get('kind', 'dialog') != 'dialog':
        reason = 'kind'
    elif rec.get('status') in ('done', 'stale', 'expired', 'answered'):
        reason = 'done' if rec.get('status') == 'done' else 'state'
    elif decision not in ('approve', 'reject'):
        reason = 'decision'
    elif float(rec.get('expires') or 0) <= now:
        reason = 'expired'
    elif decision == 'approve':
        if rec.get('status') != 'confirming':
            reason = 'state'
        elif float(rec.get('confirm_until') or 0) <= now:
            reason = 'confirm window over'
        elif not rec.get('nonce_hash') or hashlib.sha256(str(ev.get('nonce') or '').encode()).hexdigest() \
                != rec['nonce_hash']:
            reason = 'nonce'
    if not reason and rec.get('status') == 'confirming' and (
            ev.get('sender') != rec.get('sender') or ev.get('conversation') != rec.get('conversation')):
        reason = 'sender or conversation changed'
    if not reason:
        done = r.eval(_ANSWER_LUA, 1, _key(pid), rec.get('status', ''), rec.get('nonce_hash', ''), decision,
                      repr(now), str(ev.get('sender', '')), str(ev.get('conversation', '')),
                      str(ev.get('channel', '')), str(ev.get('account', '')))
        if done != 1:
            reason = 'state'
    if reason:
        if audit and rec:
            audit('dialog.user_refused', pending_id=pid, slot=rec.get('slot', ''), decision=str(decision or ''),
                  channel=str(ev.get('channel', '')), account=str(ev.get('account', '')),
                  sender=str(ev.get('sender', '')), reason=reason)
        return {'status': 'stale', 'reason': reason}
    return {'status': 'accepted'}


# Compare-and-set: the record is still in the state (and with the nonce hash) the checks above read.
_ANSWER_LUA = """
if redis.call('HGET', KEYS[1], 'status') ~= ARGV[1] then return 0 end
if (redis.call('HGET', KEYS[1], 'nonce_hash') or '') ~= ARGV[2] then return 0 end
redis.call('HSET', KEYS[1], 'status', 'answered', 'decision', ARGV[3], 'answered_at', ARGV[4])
if ARGV[1] == 'pending' then
  -- a reject without step one: this answer is the first (and only) step, record who gave it (the watchdog's audit)
  redis.call('HSET', KEYS[1], 'sender', ARGV[5], 'conversation', ARGV[6], 'channel', ARGV[7], 'account', ARGV[8])
end
return 1
"""


def mark_done(r, slot, reason):
    pid = r.get(channels.PENDING_SLOT + slot)
    if not pid:
        return ''
    _set_if(r, pid, LIVE + ('answered',), {'status': 'done', 'done_reason': str(reason or '')})
    return pid


# Set fields only while the record still exists in one of the given states: a plain HSET on a record that expired
# after it was read would recreate a partial hash without a TTL.
_SET_IF_LUA = """
local status = redis.call('HGET', KEYS[1], 'status')
if not status then return 0 end
local ok = false
for _, s in ipairs(cjson.decode(ARGV[1])) do if s == status then ok = true end end
if not ok then return 0 end
local fields = cjson.decode(ARGV[2])
for k, v in pairs(fields) do redis.call('HSET', KEYS[1], k, v) end
return 1
"""


def _set_if(r, pid, states, mapping):
    return r.eval(_SET_IF_LUA, 1, _key(pid), json.dumps(list(states)),
                  json.dumps({k: str(v) for k, v in mapping.items()}, ensure_ascii=False)) == 1


# ------------------------------------------------------------------------------------------------------------
# P8k action confirmation (contracts §8 "动作确认" / "子 fleet 根确认与祖先链"; approve-1
# P8-child-confirm-routing.md §12 C / G is the single source of these names and signatures, X2 / X8).
# A2' stub: the result / error types only construct; every function raises NotImplementedError. Implemented in P8k.


@dataclass(frozen=True)
class Consume:
    """``consume_ticket`` result. outcome in {consumed, refused, absent}; reason '' unless refused (C8.1)."""
    outcome: str
    p_id: str
    reason: str

    def __bool__(self):
        raise TypeError('check .outcome explicitly')


@dataclass(frozen=True)
class Register:
    """``register_action`` result. outcome in {registered, reused, refused}; reason '' unless refused (C8.2)."""
    outcome: str
    p_id: str
    reason: str

    def __bool__(self):
        raise TypeError('check .outcome explicitly')


@dataclass(frozen=True)
class AgentBinding:
    generation: int
    request: str


@dataclass(frozen=True)
class Route:
    """Passed through from guard.Ancestry unchanged (O8), plus the root-side values route_root adds."""
    hops: tuple
    cfg_paths: tuple
    root_cfg_path: str
    policies: tuple
    taint: object
    r: object
    run_id: str
    task_id: str
    policy: dict
    oob_modes: str
    root_request: str


@dataclass(frozen=True)
class RouteResult:
    """outcome in {local, routed, refused}; reason '' unless refused (C8.3)."""
    outcome: str
    route: object
    reason: str

    def __bool__(self):
        raise TypeError('check .outcome explicitly')


class EpochError(ValueError):
    """fx:service.stop_epoch is malformed (C5 / D3: reported as epoch_malformed)."""


class BindingError(ValueError):
    """fx:agent:<slot> is missing or its generation is missing / malformed (reported as no_agent)."""


EPOCH_RE = re.compile(r'(0|[1-9][0-9]{0,15})')
EPOCH_MAX = 2 ** 53 - 1
HIGH_MODES = ('desk', 'second_channel', 'totp')
CLASS_TEXT = {'delete': '删除', 'send_out': '外发', 'config': '改配置', 'install': '安装', 'secret_read': '读取密钥',
              'real_money': '真实资金操作', 'untrusted': '外来内容引出的操作'}
UNTRUSTED_CLASS = 'untrusted'   # M127 (b): an action forced to confirmation only by an untrusted request; tier normal
MODE_TEXT = {'desk': '只能在电脑上用 fleetmux channel approve 确认', 'second_channel': '第二步要从另一个已绑定通道确认',
             'totp': '第二步要带 6 位动态码：/approve <待办id> <nonce> <码>'}
TIER_ORDER = {'off': 0, 'normal': 1, 'high': 2}
BINDING = ('kind', 'scope', 'run_id', 'slot', 'generation', 'request_id', 'action_hash', 'epoch', 'route_epoch',
           'root_request', 'tier', 'oob_modes')   # C2: the full binding, in the order the p-id is derived from
EXPECT_KEYS = ('scope', 'run_id', 'slot', 'generation', 'request_id', 'action_hash')
ISSUE_VERSION = ('nonce_hash', 'confirm_until', 'sender', 'conversation', 'channel', 'account', 'tier', 'oob_modes',
                 'kind', 'action_hash', 'epoch')  # C3


def _int_rule(value, what):
    """ARGV integers (call_epoch, route_epoch, generation): a real int (not bool), 0 <= x < 2**53."""
    if type(value) is not int or value < 0 or value > EPOCH_MAX:
        raise ValueError(what + ' must be an integer 0..2**53-1')
    return str(value)


def _finite(value, what):
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError(what + ' must be a finite number') from None
    if value != value or value in (float('inf'), float('-inf')):
        raise ValueError(what + ' must be a finite number')
    return value


def current_epoch(r):
    """fx:service.stop_epoch (C5): missing -> 0; else ^(0|[1-9][0-9]{0,15})$ and < 2**53; anything else (or
    fx:service not a hash) -> EpochError, reported by every caller as epoch_malformed (D3)."""
    import redis
    try:
        value = r.hget('fx:service', 'stop_epoch')
    except redis.ResponseError as exc:   # WRONGTYPE
        raise EpochError('fx:service is not a hash') from exc
    if value is None:
        return 0
    if not isinstance(value, str) or not EPOCH_RE.fullmatch(value) or int(value) > EPOCH_MAX:
        raise EpochError('fx:service.stop_epoch is malformed')
    return int(value)


def agent_binding(r, slot):
    """fx:agent:<slot> -> AgentBinding; missing hash or a missing / malformed generation -> BindingError (never 0)."""
    rec = r.hgetall('fx:agent:' + str(slot))
    if not rec:
        raise BindingError('no agent ' + str(slot))
    generation = rec.get('generation')
    if not isinstance(generation, str) or not EPOCH_RE.fullmatch(generation) or int(generation) > EPOCH_MAX:
        raise BindingError('agent ' + str(slot) + ' has no valid generation')
    return AgentBinding(int(generation), rec.get('request') or '')


def stop_pending(r):
    return bool(r.exists(channels.STOP))


def route_hash(run_id, action_hash):
    return hashlib.sha256(b'route\0' + str(run_id).encode() + b'\0' + str(action_hash).encode()).hexdigest()[:32]


def key_hash(rec):
    if rec.get('scope') == 'routed':
        return route_hash(rec.get('run_id', ''), rec.get('action_hash', ''))
    return rec.get('action_hash', '')


def _run_id(cfg):
    from pathlib import Path
    return str(cfg.get('run_id') or Path(str(cfg.get('run', ''))).name)


def effective_policy(policies):
    """§12 F (O2 + D1): policies child -> root. The root's confirm.high decides the mode; every hop's must exist and be
    one of desk / second_channel / totp, else ('' mode, 'policy_refused'). Other keys: the strictest along the chain.
    Root None -> (None, 'no_policy'); a None child / middle hop is skipped."""
    import copy
    policies = tuple(policies or ())
    if not policies or not isinstance(policies[-1], dict):
        return None, 'no_policy'
    hops = [p for p in policies if isinstance(p, dict)]
    eff = copy.deepcopy(policies[-1])
    conf = eff.setdefault('confirm', {})
    why = ''
    for p in hops:
        if (p.get('confirm') or {}).get('high') not in HIGH_MODES:
            why = 'policy_refused'
    for p in hops[:-1]:
        c = p.get('confirm') or {}
        classes = conf.setdefault('classes', {})
        for name, tier in (c.get('classes') or {}).items():
            if TIER_ORDER.get(tier, 2) > TIER_ORDER.get(classes.get(name, 'off'), 0):
                classes[name] = tier
        patterns = conf.setdefault('patterns', {})
        for name, items in (c.get('patterns') or {}).items():
            merged = list(patterns.get(name) or [])
            merged += [x for x in (items or []) if x not in merged]
            patterns[name] = merged
        for key in ('wait_s', 'ticket_ttl_s', 'approve_ttl_s'):
            if isinstance(c.get(key), (int, float)) and not isinstance(c.get(key), bool):
                conf[key] = min(conf[key], c[key]) if isinstance(conf.get(key), (int, float)) else c[key]
        if p.get('untrusted_actions') == 'deny':
            eff['untrusted_actions'] = 'deny'
        limits = eff.setdefault('limits', {})
        for key, value in (p.get('limits') or {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool) \
                    and isinstance(limits.get(key), (int, float)) and not isinstance(limits.get(key), bool):
                limits[key] = min(limits[key], value)
    if why:
        conf['high'] = ''
    return eff, why


def route_root(cfg, cfg_path, *, connect=None, load=None, verify=None):
    """C6: the ancestry comes only from sec-3's ``guard.verify_ancestry`` (passed through unchanged); approve adds the
    root-side checks. Needs sec-3's guard module unless ``verify`` is injected."""
    from fleetmux.channels import security
    if verify is None:
        from fleetmux import guard   # sec-3 P8i: the gate module is fleetmux.guard (M181)
        verify = guard.verify_ancestry
    anc = verify(cfg, cfg_path=cfg_path)
    if anc.outcome == 'refused':
        return RouteResult('refused', None, anc.reason)
    if anc.outcome == 'root':
        pol = security.policy(cfg)
        high = ((pol or {}).get('confirm') or {}).get('high', '')
        route = Route(hops=(), cfg_paths=tuple(getattr(anc, 'cfg_paths', ()) or ()), root_cfg_path=str(cfg_path),
                      policies=(pol,), taint=anc.taint, r=None, run_id=_run_id(cfg), task_id='', policy=pol,
                      oob_modes=high if high in HIGH_MODES else '', root_request='')
        return RouteResult('local', route, '')
    if anc.outcome != 'verified':
        return RouteResult('refused', None, 'root_unreachable')
    if load is None or connect is None:
        from fleetmux import fleet
        load = load or fleet.load
        connect = connect or fleet.connect
    try:
        root_cfg = load(anc.root_cfg_path)
    except Exception:
        return RouteResult('refused', None, 'root_unreachable')
    if security.policy(root_cfg) is None:
        return RouteResult('refused', None, 'no_policy')
    if not root_cfg.get('channels'):
        return RouteResult('refused', None, 'no_channels')
    try:
        rr = connect(root_cfg)
        current_epoch(rr)
    except EpochError:
        return RouteResult('refused', None, 'epoch_malformed')
    except Exception:
        return RouteResult('refused', None, 'root_unreachable')
    eff, why = effective_policy(anc.policies)
    if eff is None:
        return RouteResult('refused', None, 'no_policy')
    taint = anc.taint
    root_request = (str(taint.get('root_request') or '?') if isinstance(taint, dict) else '?') if taint is not None \
        else ''
    hops = tuple(anc.hops)
    route = Route(hops=anc.hops, cfg_paths=anc.cfg_paths, root_cfg_path=anc.root_cfg_path, policies=anc.policies,
                  taint=taint, r=rr, run_id=_run_id(cfg), task_id=str(hops[0][3]) if hops and len(hops[0]) > 3 else '',
                  policy=eff, oob_modes='' if why else (eff.get('confirm') or {}).get('high', ''),
                  root_request=root_request)
    return RouteResult('routed', route, '')


# Lua helpers shared by the scripts below (C5 epoch rule; strings compared, never tostring() of a number).
_LUA_EPOCH = """
local function epoch_ok(v)
  if type(v) ~= 'string' or #v == 0 or #v > 16 then return false end
  if not (string.match(v, '^0$') or string.match(v, '^[1-9]%d*$')) then return false end
  return tonumber(v) <= 9007199254740991
end
local function epoch_of(key)
  local t = redis.call('TYPE', key)['ok']
  if t == 'none' then return '0' end
  if t ~= 'hash' then return nil end
  local v = redis.call('HGET', key, 'stop_epoch')
  if not v then return '0' end
  if epoch_ok(v) then return v end
  return nil
end
"""

_REGISTER_LUA = _LUA_EPOCH + """
-- KEYS: fx:service, fx:channel:stop, fx:agent:<slot>
-- ARGV: call_epoch scope run_id slot generation request_id action_hash route_epoch root_request tier oob_modes
--       now expires ttl prefix class action_text route_task untrusted
local call, scope = ARGV[1], ARGV[2]
if not epoch_ok(call) then return {'refused', '', 'epoch_malformed'} end
if scope == 'routed' and not epoch_ok(ARGV[8]) then return {'refused', '', 'epoch_malformed'} end
local cur = epoch_of(KEYS[1])
if not cur then return {'refused', '', 'epoch_malformed'} end
if not tonumber(ARGV[12]) or not tonumber(ARGV[13]) or not tonumber(ARGV[14]) then
  return redis.error_reply('register_action: bad now / expires / ttl') end
if ARGV[10] == 'high' and ARGV[11] ~= 'desk' and ARGV[11] ~= 'second_channel' and ARGV[11] ~= 'totp' then
  return redis.error_reply('register_action: tier high needs oob_modes desk / second_channel / totp') end
if cur ~= call then return {'refused', '', 'stale_call'} end
if redis.call('EXISTS', KEYS[2]) == 1 then return {'refused', '', 'stopped'} end
if scope == 'local' then
  if redis.call('TYPE', KEYS[3])['ok'] ~= 'hash' then return {'refused', '', 'no_agent'} end
  local g = redis.call('HGET', KEYS[3], 'generation')
  if not g or not epoch_ok(g) then return {'refused', '', 'no_agent'} end
  if g ~= ARGV[5] then return {'refused', '', 'old_generation'} end
  if (redis.call('HGET', KEYS[3], 'request') or '') ~= ARGV[6] then return {'refused', '', 'other_request'} end
end
local route_epoch, root_request = '', ''
if scope == 'routed' then route_epoch, root_request = ARGV[8], ARGV[9] end
local b = 'action' .. '\\0' .. scope .. '\\0' .. ARGV[3] .. '\\0' .. ARGV[4] .. '\\0' .. ARGV[5] .. '\\0' .. ARGV[6]
  .. '\\0' .. ARGV[7] .. '\\0' .. cur .. '\\0' .. route_epoch .. '\\0' .. root_request .. '\\0' .. ARGV[10]
  .. '\\0' .. ARGV[11]
local want = {kind = 'action', scope = scope, run_id = ARGV[3], slot = ARGV[4], generation = ARGV[5],
              request_id = ARGV[6], action_hash = ARGV[7], epoch = cur, route_epoch = route_epoch,
              root_request = root_request, tier = ARGV[10], oob_modes = ARGV[11]}
local names = {'kind', 'scope', 'run_id', 'slot', 'generation', 'request_id', 'action_hash', 'epoch', 'route_epoch',
               'root_request', 'tier', 'oob_modes'}
for seq = 0, 7 do
  local pid = 'p-' .. string.sub(redis.sha1hex(b .. '\\0' .. seq), 1, 8)
  local key = ARGV[15] .. pid
  if redis.call('EXISTS', key) == 0 then
    redis.call('HSET', key, 'id', pid, 'kind', 'action', 'scope', scope, 'run_id', ARGV[3], 'slot', ARGV[4],
      'generation', ARGV[5], 'request_id', ARGV[6], 'action_hash', ARGV[7], 'action_text', ARGV[17],
      'class', ARGV[16], 'tier', ARGV[10], 'oob_modes', ARGV[11], 'epoch', cur, 'status', 'pending',
      'created', ARGV[12], 'expires', ARGV[13], 'confirm_until', '', 'nonce_hash', '', 'sender', '',
      'conversation', '', 'channel', '', 'account', '', 'decision', '')
    if scope == 'routed' then
      redis.call('HSET', key, 'route_epoch', route_epoch, 'root_request', root_request, 'route_task', ARGV[18])
    end
    if ARGV[19] == '1' then redis.call('HSET', key, 'untrusted', '1') end
    redis.call('EXPIRE', key, ARGV[14])
    return {'registered', pid, ''}
  end
  if redis.call('TYPE', key)['ok'] == 'hash' then
    local same = true
    for _, n in ipairs(names) do
      if (redis.call('HGET', key, n) or '') ~= want[n] then same = false end
    end
    local st = redis.call('HGET', key, 'status')
    if same and (st == 'pending' or st == 'confirming')
        and (tonumber(redis.call('HGET', key, 'expires') or '0') or 0) > tonumber(ARGV[12]) then
      return {'reused', pid, ''}
    end
  end
end
return {'refused', '', 'exhausted'}
"""


def register_action(r, *, slot, generation, request_id, run_id, cls, tier, action_hash, action_text, call_epoch,
                    oob_modes, route=None, route_epoch=None, ttl_s=None, now=None):
    """C2: one Lua script; refused outcomes write nothing. A new record queues one user-only action card itself
    (M127 a). ``cls``: one of security.CLASSES, or 'untrusted' (tier normal; M127 b)."""
    now = time.time() if now is None else _finite(now, 'now')
    ttl = int(ttl_s or channels.APPROVE_TTL_S)
    if ttl <= 0:
        raise ValueError('approve_ttl_s must be positive')
    if tier not in ('normal', 'high'):
        raise ValueError('tier must be normal or high')
    from fleetmux.channels import security
    if cls not in security.CLASSES and cls != UNTRUSTED_CLASS:
        raise ValueError('cls must be one of ' + ', '.join(security.CLASSES + (UNTRUSTED_CLASS,)))
    if cls == UNTRUSTED_CLASS and tier != 'normal':
        raise ValueError("cls 'untrusted' is tier normal only")
    scope = 'routed' if route is not None else 'local'
    for name, value in (('call_epoch', call_epoch), ('generation', generation)):
        if type(value) is not int:
            raise TypeError(name + ' must be an int')
    if scope == 'routed' and type(route_epoch) is not int:
        raise TypeError('route_epoch must be an int for a routed action')
    call = str(call_epoch) if 0 <= call_epoch <= EPOCH_MAX else 'x'          # out of shape -> epoch_malformed
    rep = (str(route_epoch) if 0 <= route_epoch <= EPOCH_MAX else 'x') if scope == 'routed' else ''
    gen = _int_rule(generation, 'generation')
    untrusted = '1' if cls == UNTRUSTED_CLASS or (
        request_id and r.exists(channels.UNTRUSTED + envelope.origin_id(request_id))) else ''
    out = r.eval(_REGISTER_LUA, 3, 'fx:service', channels.STOP, 'fx:agent:' + str(slot),
                 call, scope, str(run_id), str(slot), gen, str(request_id or ''), str(action_hash), rep,
                 str(route.root_request or '') if route is not None else '', tier, str(oob_modes or ''),
                 repr(now), repr(now + ttl), str(ttl), channels.PENDING, str(cls), str(action_text or ''),
                 str(route.task_id or '') if route is not None else '', untrusted)
    res = Register(*[x.decode() if isinstance(x, bytes) else x for x in out])
    # M127 (a): the card is queued here, by the register itself -- the hook (sec-3) never reads pending records.
    # Once per record: a reused record already has its card on the phone.
    if res.outcome == 'registered':
        item = envelope.outbound('prompt_wait', target='last', card=action_card(r.hgetall(_key(res.p_id))),
                                 user_only=True)
        r.lpush(channels.OUTQ, json.dumps(item, ensure_ascii=False))
    return res


def action_card(rec):
    pid = rec['id']
    cls = rec.get('class') or rec.get('cls') or ''
    lines = [str(rec.get('action_text', '')), '', '槽 ' + str(rec.get('slot', '')) + ' · 请求 '
             + str(rec.get('request_id', '') or '—')]
    if rec.get('scope') == 'routed':
        lines.append('来自子 fleet ' + str(rec.get('route_task', '')) + '，根请求 ' + str(rec.get('root_request', '')))
    if rec.get('tier') == 'high':
        lines.append('高危：' + MODE_TEXT.get(rec.get('oob_modes', ''), '没有可用的确认方式，不能批准'))
    if rec.get('untrusted'):
        lines += ['', envelope.RECEIPTS['untrusted_notice']]
    buttons = [{'label': '批准', 'command': '/approve ' + pid}, {'label': '拒绝', 'command': '/reject ' + pid}]
    return envelope.card('确认' + CLASS_TEXT.get(cls, cls), '\n'.join(lines), buttons, ref=pid)


_ISSUE_LUA = _LUA_EPOCH + """
-- KEYS: fx:service, fx:channel:stop, record, ticket, wait
-- ARGV: 1-11 the C3 version fields of the snapshot, 12 p_id, 13 now, 14 ticket_ttl, 15 decision
if not epoch_of(KEYS[1]) then return 'epoch_malformed' end
if not tonumber(ARGV[13]) or not tonumber(ARGV[14]) then return redis.error_reply('issue: bad now / ttl') end
local rt = redis.call('TYPE', KEYS[3])['ok']
if rt ~= 'hash' then return 'gone' end
local cur = epoch_of(KEYS[1])
if (redis.call('HGET', KEYS[3], 'epoch') or '') ~= cur or redis.call('EXISTS', KEYS[2]) == 1 then
  redis.call('HSET', KEYS[3], 'status', 'stale', 'stale_reason', 'stopped')
  return 'stopped'
end
local now = tonumber(ARGV[13])
local st = redis.call('HGET', KEYS[3], 'status')
local expires = tonumber(redis.call('HGET', KEYS[3], 'expires') or '0') or 0
if ARGV[15] == 'reject' then
  if (st ~= 'pending' and st ~= 'confirming') or expires <= now then return 'stale' end
  if (redis.call('HGET', KEYS[3], 'nonce_hash') or '') ~= ARGV[1] then return 'stale' end
  redis.call('HSET', KEYS[3], 'status', 'answered', 'decision', 'reject', 'answered_at', ARGV[13])
  redis.call('LPUSH', KEYS[5], 'reject')
  redis.call('EXPIRE', KEYS[5], 60)
  return 'ok'
end
local names = {'nonce_hash', 'confirm_until', 'sender', 'conversation', 'channel', 'account', 'tier', 'oob_modes',
               'kind', 'action_hash', 'epoch'}
for i, n in ipairs(names) do
  if (redis.call('HGET', KEYS[3], n) or '') ~= ARGV[i] then return 'stale' end
end
if st ~= 'confirming' or expires <= now
    or (tonumber(redis.call('HGET', KEYS[3], 'confirm_until') or '0') or 0) <= now then return 'stale' end
redis.call('HSET', KEYS[3], 'status', 'answered', 'decision', 'approve', 'answered_at', ARGV[13])
redis.call('SET', KEYS[4], ARGV[12], 'EX', ARGV[14])
redis.call('LPUSH', KEYS[5], 'approve')
redis.call('EXPIRE', KEYS[5], 60)
return 'ok'
"""


def answer_action(r, rec, ev, *, policy, audit, now=None):
    """The second step / reject of an action record (contracts §8 动作确认; C3). Python checks the snapshot ``rec``
    (nonce, identity by the record's tier / oob_modes, oob.check last); one script then re-checks the record's
    version fields, epoch and STOP before it writes the ticket and wakes the waiter. Refusals before the script
    write nothing. Returns {status: accepted} or {status: stale, reason}."""
    from fleetmux.channels import oob
    now = time.time() if now is None else _finite(now, 'now')
    rec = dict(rec or {})
    pid = str(rec.get('id') or ev.get('pending_id') or '')
    decision = ev.get('decision')
    fields = dict(p_id=pid, action_hash=rec.get('action_hash', ''), **{'class': rec.get('class', '')},
                  sender=str(ev.get('sender', '')), channel=str(ev.get('channel', '')))

    def refuse(reason, **extra):
        audit('security.action_denied', reason=reason, **fields, **extra)
        return {'status': 'stale', 'reason': reason}

    if not pid.startswith('p-') or rec.get('kind') != 'action':
        return refuse('kind')
    if decision not in ('approve', 'reject'):
        return refuse('decision')
    try:
        expires, confirm_until = float(rec.get('expires') or 0), float(rec.get('confirm_until') or 0)
    except ValueError:
        return refuse('binding')
    if expires <= now:
        return refuse('expired')
    if rec.get('status') == 'confirming' and (ev.get('sender') != rec.get('sender')
                                              or ev.get('conversation') != rec.get('conversation')) \
            and not (decision == 'approve' and rec.get('tier') == 'high' and rec.get('oob_modes') != 'totp'):
        return refuse('identity')
    if decision == 'approve':
        if rec.get('status') != 'confirming':
            return refuse('state')
        if not rec.get('nonce_hash') or hashlib.sha256(str(ev.get('nonce') or '').encode()).hexdigest() \
                != rec['nonce_hash']:
            return refuse('nonce')
        if confirm_until <= now:
            return refuse('expired')
        if rec.get('tier') == 'high':
            mode = rec.get('oob_modes', '')
            if mode not in HIGH_MODES:
                return refuse('policy_refused')
            if ev.get('channel') == 'desk' and not ev.get('desk_proof'):
                return refuse('desk_proof')
            try:
                ok, why = oob.check(rec, ev, policy, r=r)
            except Exception:   # fail closed (P8l not landed, a broken desk key, a Redis error ...)
                ok, why = False, 'oob check unavailable'
            if not ok:
                audit('security.oob_refused', mode=mode, why=str(why or '')[:200], **fields)
                return {'status': 'stale', 'reason': 'oob'}
        elif ev.get('channel') == 'desk' or ev.get('channel') != rec.get('channel') \
                or ev.get('account') != rec.get('account'):
            return refuse('identity')
    k = key_hash(rec)
    ttl = int(((policy or {}).get('confirm') or {}).get('ticket_ttl_s') or channels.APPROVE_TTL_S)
    version = [str(rec.get(n, '') or '') for n in ISSUE_VERSION]
    out = r.eval(_ISSUE_LUA, 5, 'fx:service', channels.STOP, _key(pid), channels.GUARD_TICKET + k,
                 channels.GUARD_WAIT + k, *version, pid, repr(now), str(ttl), decision)
    out = out.decode() if isinstance(out, bytes) else out
    if out != 'ok':
        return refuse({'gone': 'gone'}.get(out, out))
    if decision == 'approve':
        audit('security.action_approved', stage='answered', **fields)
    else:
        audit('security.action_denied', reason='rejected', **fields)
    return {'status': 'accepted'}


_CONSUME_LUA = _LUA_EPOCH + """
-- KEYS: ticket, fx:service, fx:channel:stop, fx:agent:<slot>
-- ARGV: call_epoch scope run_id slot generation request_id action_hash route_epoch root_request now prefix
if not epoch_ok(ARGV[1]) then return {'refused', '', 'epoch_malformed'} end
if ARGV[2] == 'routed' and not epoch_ok(ARGV[8]) then return {'refused', '', 'epoch_malformed'} end
local cur = epoch_of(KEYS[2])
if not cur then return {'refused', '', 'epoch_malformed'} end
if not tonumber(ARGV[10]) then return redis.error_reply('consume: bad now') end
local tt = redis.call('TYPE', KEYS[1])['ok']
if tt ~= 'string' and tt ~= 'none' then return redis.error_reply('consume: ticket is not a string') end
local function mark(p, reason)
  local key = ARGV[11] .. p
  if redis.call('TYPE', key)['ok'] == 'hash' then
    redis.call('HSET', key, 'status', 'stale', 'stale_reason', reason)
  end
end
local function burn(reason, stale)
  local p = redis.call('GETDEL', KEYS[1])
  if p then mark(p, stale) end
  return {'refused', p or '', reason}
end
if cur ~= ARGV[1] then return burn('stale_call', 'stopped') end
if redis.call('EXISTS', KEYS[3]) == 1 then return burn('stopped', 'stopped') end
if ARGV[2] == 'local' then
  if redis.call('TYPE', KEYS[4])['ok'] ~= 'hash' then return burn('no_agent', 'no_agent') end
  local g = redis.call('HGET', KEYS[4], 'generation')
  if not g or not epoch_ok(g) then return burn('no_agent', 'no_agent') end
  if g ~= ARGV[5] then return burn('old_generation', 'old_generation') end
  if (redis.call('HGET', KEYS[4], 'request') or '') ~= ARGV[6] then return burn('other_request', 'other_request') end
end
local p = redis.call('GETDEL', KEYS[1])
if not p then return {'absent', '', ''} end
local key = ARGV[11] .. p
if redis.call('TYPE', key)['ok'] ~= 'hash' then return {'refused', p, 'binding'} end
local function f(n) return redis.call('HGET', key, n) or '' end
if f('epoch') ~= cur then mark(p, 'stopped') ; return {'refused', p, 'stopped'} end
local same = f('kind') == 'action' and f('scope') == ARGV[2] and f('run_id') == ARGV[3] and f('slot') == ARGV[4]
  and f('generation') == ARGV[5] and f('request_id') == ARGV[6] and f('action_hash') == ARGV[7]
if ARGV[2] == 'routed' then same = same and f('route_epoch') == ARGV[8] and f('root_request') == ARGV[9] end
if not same then mark(p, 'binding') ; return {'refused', p, 'binding'} end
if f('status') ~= 'answered' or f('decision') ~= 'approve' then mark(p, 'not_approved') ; return {'refused', p, 'not_approved'} end
if (tonumber(f('expires')) or 0) <= tonumber(ARGV[10]) then mark(p, 'expired') ; return {'refused', p, 'expired'} end
redis.call('HSET', key, 'status', 'done', 'consumed_at', ARGV[10])
return {'consumed', p, ''}
"""


def consume_ticket(r, key_hash, *, expect, call_epoch, audit, now=None):
    """§11 T2: the only way a ticket is read; one script, GETDEL inside. -> Consume (C8.1)."""
    missing = [k for k in EXPECT_KEYS if k not in (expect or {})]
    if (expect or {}).get('scope') == 'routed':
        missing += [k for k in ('route_epoch', 'root_request') if k not in expect]
    if missing:
        raise TypeError('expect is missing ' + ', '.join(missing))
    if type(call_epoch) is not int or type(expect['generation']) is not int:
        raise TypeError('call_epoch and expect.generation must be ints')
    now = time.time() if now is None else _finite(now, 'now')
    routed = expect['scope'] == 'routed'
    call = str(call_epoch) if 0 <= call_epoch <= EPOCH_MAX else 'x'
    rep = ''
    if routed:
        if type(expect['route_epoch']) is not int:
            raise TypeError('expect.route_epoch must be an int')
        rep = str(expect['route_epoch']) if 0 <= expect['route_epoch'] <= EPOCH_MAX else 'x'
    out = r.eval(_CONSUME_LUA, 4, channels.GUARD_TICKET + str(key_hash), 'fx:service', channels.STOP,
                 'fx:agent:' + str(expect['slot']), call, expect['scope'], str(expect['run_id']), str(expect['slot']),
                 _int_rule(expect['generation'], 'generation'), str(expect['request_id'] or ''),
                 str(expect['action_hash']), rep, str(expect.get('root_request', '') or '') if routed else '',
                 repr(now), channels.PENDING)
    res = Consume(*[x.decode() if isinstance(x, bytes) else x for x in out])
    fields = dict(p_id=res.p_id, action_hash=str(expect['action_hash']), key_hash=str(key_hash),
                  slot=str(expect['slot']))
    if res.outcome == 'consumed':
        audit('security.action_approved', stage='consumed', **fields)
    elif res.outcome == 'refused':
        audit('security.action_denied', reason=res.reason, **fields)
    return res


_CLAIM_LUA = _LUA_EPOCH + """
-- KEYS: record, fx:service, fx:channel:stop
if redis.call('TYPE', KEYS[1])['ok'] ~= 'hash' then return 0 end
local cur = epoch_of(KEYS[2])
if not cur then return 0 end
local e = redis.call('HGET', KEYS[1], 'epoch')
if (e and e ~= cur) or redis.call('EXISTS', KEYS[3]) == 1 then
  redis.call('HSET', KEYS[1], 'status', 'stale', 'stale_reason', 'stopped')
  return 0
end
if redis.call('HGET', KEYS[1], 'status') ~= 'answered' then return 0 end
redis.call('HSET', KEYS[1], 'status', 'done', 'done_reason', 'pressed')
return 1
"""


def claim_press(r, p_id):
    """answered -> done just before a press, unless the epoch moved since the record was made (a record without an
    epoch field -- a P6 dialog -- skips that check) or a /stop is unhandled (-> stale / stopped). True = press."""
    return r.eval(_CLAIM_LUA, 3, _key(str(p_id)), 'fx:service', channels.STOP) == 1


def desk_key():
    """The local desk key (``config_dir()/desk.key``, 0600, config dir 0700): created once with 32 random bytes,
    then read. Only the local ``fleetmux channel approve`` uses it to sign ``desk_proof`` (oob.desk_proof); it
    never leaves this machine."""
    import os
    folder = channels.config_dir()
    folder.mkdir(parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    path = folder / 'desk.key'
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        os.chmod(path, 0o600)
        return path.read_text(encoding='utf-8').strip()
    value = secrets.token_hex(32)
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        fh.write(value + '\n')
    return value
