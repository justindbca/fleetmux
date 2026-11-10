"""The shapes that cross the channel boundary (design draft §4.1, contracts §8). Pure functions, no I/O.

* inbound envelope: what every adapter yields for one message (``validate`` / ``make``);
* origin: where a request came from, kept under ``fx:channel:origin:<id>`` (``origin_from``);
* outbound item: one entry of the ``fx:channel:outq`` queue and the JSON an ``exec`` channel gets on stdin
  (``outbound``);
* card: a channel-neutral card with command buttons (``card`` / ``card_text``);
* commands: the deterministic grammar of what a user types (``parse_command``; no LLM guesses intent);
* receipts: the fixed reply texts, the same wording as the Claude supervisor's input hook (fleet.claude_prompt).
"""
import json
import re
import time
import uuid

from cmux_fleet import channels

TEXT_MAX = 32000  # same bound as Fleet.handle's operator_input
ENVELOPE_KEYS = {
    'channel', 'account', 'conversation', 'message_id', 'sender', 'sender_name', 'text', 'files',
    'rejected_files', 'reply_ref', 'action', 'private', 'verified', 'at',
    'provenance',  # P8a: outside content (forwarded / quoted / mail body ...); ``text`` is only what the user typed
}
# P8a (brief v2.2 §8.2 ①): where an outside segment came from. ``text`` holds only what the user typed himself;
# forwarded / quoted messages, a mail body, a contact card, a location, a caption go to ``provenance`` as
# [{kind, text?, path?, name?}] (each item needs text or path). P8b decides how they reach the supervisor.
PROVENANCE_KINDS = ('forwarded', 'quoted', 'email_body', 'attachment', 'contact_card', 'location', 'caption')
REQUIRED = ('channel', 'account', 'conversation', 'message_id', 'sender', 'text', 'private', 'verified', 'at')


def _scalar_map(value):
    return isinstance(value, dict) and all(isinstance(k, str) and (v is None or isinstance(v, (str, int, bool)))
                                           for k, v in value.items())


def problems(env):
    """Every problem with ``env`` as an inbound envelope ([] when valid)."""
    if not isinstance(env, dict):
        return ['envelope must be an object']
    out = ['missing ' + k for k in REQUIRED if k not in env]
    out += ['unknown key ' + k for k in sorted(set(env) - ENVELOPE_KEYS)]
    if out:
        return out
    for k in ('channel', 'account', 'conversation', 'message_id', 'sender'):
        if not isinstance(env[k], str) or not env[k].strip():
            out.append(k + ' must be a non-empty string')
    if not out:
        if env['channel'] not in channels.known_types():
            out.append('unknown channel ' + env['channel'])
        elif not env['message_id'].startswith(channels.prefix(env['channel']) + ':'):
            out.append('message_id must start with ' + channels.prefix(env['channel']) + ':')
    if not isinstance(env['text'], str) or len(env['text']) > TEXT_MAX:
        out.append('text must be a string of at most ' + str(TEXT_MAX) + ' chars')
    for k in ('private', 'verified'):
        if not isinstance(env[k], bool):
            out.append(k + ' must be true or false')
    if isinstance(env['at'], bool) or not isinstance(env['at'], int) or env['at'] <= 0:
        out.append('at must be epoch seconds (int)')
    if 'sender_name' in env and not isinstance(env['sender_name'], str):
        out.append('sender_name must be a string')
    if 'reply_ref' in env and not _scalar_map(env['reply_ref']):
        out.append('reply_ref must be an object of strings / numbers (the adapter alone reads it)')
    files = env.get('files', [])
    if not isinstance(files, list) or not all(isinstance(f, dict) and isinstance(f.get('name'), str)
                                              and isinstance(f.get('path'), str) for f in files):
        out.append('files must be a list of {name, path, size?}')
    rejected = env.get('rejected_files', [])
    if not isinstance(rejected, list) or not all(isinstance(f, dict) and isinstance(f.get('name'), str)
                                                 and isinstance(f.get('reason'), str) for f in rejected):
        out.append('rejected_files must be a list of {name, reason}')
    action = env.get('action')
    if action is not None and not (_scalar_map(action) and isinstance(action.get('command'), str)):
        out.append('action must be {command, card_message_id?} (a button press)')
    provenance = env.get('provenance', [])
    if not isinstance(provenance, list) or not all(
            isinstance(p, dict) and not set(p) - {'kind', 'text', 'path', 'name'} and p.get('kind') in PROVENANCE_KINDS
            and all(isinstance(p[k], str) for k in ('text', 'path', 'name') if k in p)
            and len(p.get('text', '')) <= TEXT_MAX
            and (p.get('text') or p.get('path')) for p in provenance):
        out.append('provenance must be a list of {kind, text?, path?, name?} with kind one of '
                   + ', '.join(PROVENANCE_KINDS) + ' and a text (at most ' + str(TEXT_MAX) + ' chars) or a path')
    if (isinstance(env.get('text'), str) and not env['text'].strip() and not files and not rejected and not action
            and not provenance):
        out.append('empty message: no text, file, button or outside content')
    return out


def validate(env):
    """Return ``env`` when it is a valid inbound envelope, else ValueError listing every problem."""
    found = problems(env)
    if found:
        raise ValueError('invalid envelope: ' + '; '.join(found))
    return env


def make(channel, account, *, conversation, message_id, sender, text='', private=True, verified=True, at=None,
         sender_name=None, files=None, rejected_files=None, reply_ref=None, action=None, provenance=None):
    """Build and validate an envelope. ``message_id`` is the platform id without the prefix (this adds it)."""
    env = {'channel': channel, 'account': account, 'conversation': str(conversation),
           'message_id': channels.prefix(channel) + ':' + str(message_id), 'sender': str(sender), 'text': text,
           'private': private, 'verified': verified, 'at': int(at if at is not None else time.time())}
    for k, v in (('sender_name', sender_name), ('files', files), ('rejected_files', rejected_files),
                 ('reply_ref', reply_ref), ('action', action), ('provenance', provenance)):
        if v is not None:
            env[k] = v
    return validate(env)


# ------------------------------------------------------------------ origin
ORIGIN_KEYS = ('channel', 'account', 'conversation', 'reply_ref', 'message_id', 'sender', 'at')


def origin_from(env):
    """The origin of a request an envelope started: enough to answer in the same conversation, nothing else
    (no text). Stored by the actor as JSON under ``fx:channel:origin:<request id | task id>``."""
    return {'channel': env['channel'], 'account': env['account'], 'conversation': env['conversation'],
            'reply_ref': dict(env.get('reply_ref') or {}), 'message_id': env['message_id'],
            'sender': env['sender'], 'at': env['at']}


def origin_problem(origin):
    """'' when ``origin`` is a valid origin (what the actor writes under fx:channel:origin:<id> once the fleet
    answered accepted, and what the outbound scan reads back)."""
    if not isinstance(origin, dict) or set(origin) != set(ORIGIN_KEYS):
        return 'origin must be {' + ', '.join(ORIGIN_KEYS) + '}'
    if not all(isinstance(origin[k], str) and origin[k] for k in ('channel', 'account', 'conversation', 'message_id',
                                                                   'sender')):
        return 'origin channel / account / conversation / message_id / sender must be non-empty strings'
    if not _scalar_map(origin['reply_ref']) or isinstance(origin['at'], bool) or not isinstance(origin['at'], int):
        return 'origin reply_ref must be a flat object and at an int'
    return ''


FOLLOWUP_SUFFIX = '.done'  # P7's naming convention for a delegated request's final result: '<id>.done'


def origin_id(result_id):
    """The request id whose origin a result goes to (brief v2, ruling C7): '<id>.done' (P7's follow-up of a
    delegated request) -> '<id>'; any other id -> itself. The outbound scan looks up fx:channel:origin:<this>."""
    if isinstance(result_id, str) and result_id.endswith(FOLLOWUP_SUFFIX) and len(result_id) > len(FOLLOWUP_SUFFIX):
        return result_id[:-len(FOLLOWUP_SUFFIX)]
    return result_id


# ------------------------------------------------------------------ card
def card(title, text='', buttons=(), ref=''):
    """A channel-neutral card: title, body text, command buttons. A button is ``{label, command}``; pressing it
    is the same as typing ``command`` (the adapter yields an envelope with ``action.command`` and that text)."""
    c = {'title': title, 'text': text, 'buttons': [dict(b) for b in buttons], 'ref': ref}
    problem = card_problem(c)
    if problem:
        raise ValueError(problem)
    return c


def card_problem(c):
    if not isinstance(c, dict) or set(c) - {'title', 'text', 'buttons', 'ref'}:
        return 'card must be {title, text, buttons, ref}'
    if not isinstance(c.get('title'), str) or not c['title'].strip():
        return 'card title must be a non-empty string'
    if not isinstance(c.get('text', ''), str) or not isinstance(c.get('ref', ''), str):
        return 'card text / ref must be strings'
    buttons = c.get('buttons', [])
    if not isinstance(buttons, list) or len(buttons) > 6 or not all(
            isinstance(b, dict) and set(b) == {'label', 'command'} and isinstance(b['label'], str) and b['label']
            and isinstance(b['command'], str) and b['command'].startswith('/') for b in buttons):
        return 'card buttons must be up to 6 {label, command} with command starting with /'
    return ''  # no length rule here (M4b): COMMAND_MAX_BYTES is Telegram's own limit, its adapter falls back


def redact_card(c):
    """The card without anything a user could act on: no buttons, no ref. What a channel that does not reach
    only the whitelisted user's private chat (CARRIES_TOKENS False: exec) gets, so pending ids / nonces never
    leave through it. The body text must not carry nonces either (approve.card keeps them in buttons only)."""
    return {'title': c['title'], 'text': c.get('text', ''), 'buttons': [], 'ref': ''}


def card_text(c):
    """The plain-text form of a card for a channel without cards / buttons: every button's command is spelled
    out so the user can type it (email, or a channel whose card call failed)."""
    lines = [c['title']]
    if c.get('text'):
        lines += ['', c['text']]
    if c.get('buttons'):
        lines += [''] + ['回复 ' + b['command'] + ' = ' + b['label'] for b in c['buttons']]
    return '\n'.join(lines)


# ------------------------------------------------------------------ outbound queue item
TARGETS = ('origin', 'last', 'first', 'explicit')
OUT_KEYS = {'id', 'event', 'target', 'channel', 'account', 'conversation', 'reply_ref', 'request_id', 'text',
            'card', 'file', 'caption', 'update', 'user_only', 'at', 'attempts'}


def outbound(event, *, target='last', text=None, card=None, file=None, caption='', update=None, request_id='',
             channel='', account='', conversation='', reply_ref=None, user_only=False):
    """One item of ``fx:channel:outq`` (and the stdin JSON of an ``exec`` channel). Exactly one of text / card /
    file. ``target``: origin (``request_id``'s origin), last (last interaction, else first), first (first
    configured channel), explicit (channel + account + conversation given). ``update``: the message id of a card
    to replace (update_card; falls back to a new card). ``user_only``: only a conversation where the whitelisted
    user talked privately may get it (delete approvals, design draft §5.2)."""
    item = {'id': 'out-' + uuid.uuid4().hex, 'event': event, 'target': target, 'channel': channel,
            'account': account, 'conversation': conversation, 'reply_ref': dict(reply_ref or {}),
            'request_id': request_id, 'text': text, 'card': card, 'file': file, 'caption': caption,
            'update': update, 'user_only': bool(user_only), 'at': time.time(), 'attempts': 0}
    problem = outbound_problem(item)
    if problem:
        raise ValueError(problem)
    return item


def outbound_problem(item):
    if not isinstance(item, dict) or set(item) - OUT_KEYS:
        return 'outbound item has unknown keys'
    if item.get('event') not in channels.OUT_EVENTS:
        return 'outbound event must be one of ' + ', '.join(channels.OUT_EVENTS)
    if item.get('target') not in TARGETS:
        return 'outbound target must be one of ' + ', '.join(TARGETS)
    bodies = [k for k in ('text', 'card', 'file') if item.get(k) is not None]
    if len(bodies) != 1:
        return 'outbound item needs exactly one of text / card / file'
    if item.get('text') is not None and (not isinstance(item['text'], str) or not item['text'].strip()):
        return 'outbound text must be a non-empty string'
    if item.get('card') is not None and card_problem(item['card']):
        return card_problem(item['card'])
    if item.get('file') is not None and (not isinstance(item['file'], str) or not item['file']):
        return 'outbound file must be a path'
    if item['target'] == 'origin' and not item.get('request_id'):
        return 'target origin needs request_id'
    if item['target'] == 'explicit' and not (item.get('channel') and item.get('account') and item.get('conversation')):
        return 'target explicit needs channel, account and conversation'
    return ''


def dumps(item):
    return json.dumps(item, ensure_ascii=False, sort_keys=True)


# ------------------------------------------------------------------ commands (design draft §4.2)
COMMANDS = ('status', 'approve', 'reject', 'help', 'stop')  # stop: P8a (brief v2.2 E2), handled by the actor (P8m)
# A pending delete dialog (P6b, brief v2 §2.1-4/5): id 'p-' + 8 hex, the confirmation nonce 16 hex.
# `/approve p-xxxxxxxx <16 hex>` is 36 bytes, inside Telegram's 64-byte callback_data. The nonce stops misclicks,
# mix-ups and replays; it is not a security boundary against a process of the same user (contracts §8).
PENDING_ID_RE = r'p-[0-9a-f]{8}'
NONCE_RE = r'[0-9a-f]{16}'
CODE_RE = r'[0-9]{6}'  # P8a: optional third /approve argument (RFC 6238 code, P8l)
COMMAND_MAX_BYTES = 64  # Telegram callback_data; not a card rule: the Telegram adapter sends an over-limit card as card_text


def parse_command(text):
    """What a message asks for. Deterministic; anything that is not a known command is operator text.

    Returns a dict with ``kind``:
      * ``text``: plain operator text -> ``operator_input`` (``text`` stripped);
      * ``status``: ``/status`` (read-only summary);
      * ``help``: ``/help``;
      * ``approve``: ``/approve <pending id> [<nonce>]`` (no nonce = step one, answered by
        ``approve.handle_command`` with a confirmation card; with the nonce = step two, the confirmation);
      * ``reject``: ``/reject <pending id> [reason…]`` (one step);
      * ``stop``: ``/stop [reason…]`` (P8a; the actor stops dispatch without the supervisor, P8m);
      * ``approve`` with a third argument: ``/approve <pending id> <nonce> <6 digits>`` adds ``code`` (P8a, the
        TOTP code of P8l; any other shape is ``invalid``);
      * ``invalid``: a known command with bad arguments (``reason`` says how to write it);
      * ``unknown``: another ``/word`` (slash commands are never queued as operator input, design §5).
    """
    stripped = (text or '').strip()
    if not stripped.startswith('/'):
        return {'kind': 'text', 'text': stripped}
    head, _, rest = stripped.partition(' ')
    name = head[1:].split('@', 1)[0].lower()  # Telegram appends @botname in groups
    args = rest.split()
    if name in ('status', 'help'):
        return {'kind': name}
    if name == 'stop':
        return {'kind': 'stop', 'reason': ' '.join(args)}
    if name in ('approve', 'reject'):
        if not args or not re.fullmatch(PENDING_ID_RE, args[0]):
            return {'kind': 'invalid', 'command': name,
                    'reason': '用法：/' + name + ' <待办id>' + (' [理由]' if name == 'reject' else '') + '（按卡片按钮即可）'}
        if name == 'approve':
            if (len(args) > 3 or (len(args) >= 2 and not re.fullmatch(NONCE_RE, args[1]))
                    or (len(args) == 3 and not re.fullmatch(CODE_RE, args[2]))):
                return {'kind': 'invalid', 'command': name, 'reason': '用法：/approve <待办id>（再按确认）'}
            cmd = {'kind': 'approve', 'id': args[0], 'nonce': args[1] if len(args) >= 2 else ''}
            if len(args) == 3:
                cmd['code'] = args[2]  # P8a: the TOTP code of a high-tier action (P8l); 36 + 7 = 43 bytes
            return cmd
        return {'kind': 'reject', 'id': args[0], 'reason': ' '.join(args[1:])}
    return {'kind': 'unknown', 'command': name}


# ------------------------------------------------------------------ receipts (fixed texts, design draft §4.2)
RECEIPTS = {
    # Fleet.handle / claude_prompt answers, same wording as the supervisor's input hook
    'accepted': 'Fleet：指令已排队（{id}），监工会按派活流程处理。',
    'rejected': 'Fleet 输入被拒绝：{reason}',
    'unconfirmed': 'Fleet 输入未确认（{id}）；请先查看 control 再决定是否重试。',
    # channel-only answers
    'no_operator_input': 'Fleet：这个 fleet 的监工不收中途输入。',  # a native Codex supervisor (design §5 input boundary)
    'file_refused': 'Fleet：文件 {name} 未转交（{reason}）。',
    'unknown_command': 'Fleet：不认识的命令 /{command}；可用 /status、/help；批准 / 拒绝请按待办卡片上的按钮。',
    'invalid_command': 'Fleet：{reason}',
    'pending_gone': 'Fleet：待办 {id} 已不存在 / 已被别处答掉。',
    'confirm_delete': '确认删除？\n{command}\n再按一次「确认删除」才生效（{seconds} 秒内）。',
    'answer_taken': 'Fleet：已收到（{decision}），按键前会再核对屏幕。',
    'answered': 'Fleet：待办 {id} 已答（{decision}），已按键；fleet 仍暂停（{reason}），恢复请在 control 里 resume 或运行 {resume}。',
    'changed': 'Fleet：待办 {id} 已变化（{reason}），没有按键，请重看。',
    'no_key': 'Fleet：待办 {id} 没有登记的同意键，不能在手机上批准，请在电脑上处理（拒绝仍可用）。',
    'expired': 'Fleet：待办 {id} 已过期，没有按键；确认框留给电脑前的人。',
    'refused': 'Fleet：待办 {id} 未受理（{reason}），没有按键。',
    'help': ('Fleet：直接发文字 = 在监工窗格打字；开头写 @dev-1（worker 名）= 不经监工直接派给它；在 worker 回复的帖子里'
             '回复 = 接着找那个 worker；/status 看状态；删除待办按卡片上的「批准」「拒绝」（两步确认）。'),
    # §53「直连 worker」 (user 2026-11-10)
    'direct_accepted': 'Fleet：已直接交给 {worker}（{id}）{busy}。',
    'direct_unavailable': 'Fleet：这个 fleet 不收直连 worker 的消息（{worker}），请去掉开头的名字发给监工。',
    # P8a (brief v2.2 §8.2 ⑤): security answers
    # {resume}: a whole sentence by level - pause / interrupt: `cmux-fleet resume --config <run>/config.json`;
    # kill: the run is explicitly finished, a new start is needed (never "start --resume recovers it")
    'stopped': 'Fleet：已停止（{level}）；{resume}',
    'stop_failed': 'Fleet：停止没有生效（{reason}）；已写停止标记，daemon 下一拍处理。',
    'secret_blocked': 'Fleet：一条出站消息含疑似密钥（规则 {rule}），已整条拦下未发（{id}）。',
    'rate_limited': 'Fleet：太频繁，已暂缓（{reason}）。',
    'confirm_action': '确认执行？\n{action}\n再按一次才生效（{seconds} 秒内）',
    'action_taken': 'Fleet：动作 {id} 已收到（{decision}），worker 原样重试时生效。',
    'oob_required': 'Fleet：此操作要在电脑上确认（{id}）。',
    # only the Claude PreToolUse gate (P8i) stops an action beforehand; other CLIs (P8j): neither a gate nor an audit
    # row per action is promised -- coverage depends on what each CLI supports
    'untrusted_notice': 'Fleet：此请求含外来内容，已标记。由它引出的写入 / 执行 / 外发类操作：Claude worker 会被拦下等你确认'
                        '（或按配置直接拒绝）；其它 CLI 的拦截和审计覆盖取决于其支持能力。',
}


def receipt(which, /, **fields):
    return RECEIPTS[which].format(**{k: ('' if v is None else v) for k, v in fields.items()})
