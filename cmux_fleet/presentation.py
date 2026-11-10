"""UI-only presentation for fleet lifecycle events (design v2 §7.1, §7.3).

Pure and deterministic: no Redis, no cmux API, no subprocess, no model, no clock read
(``now`` is always passed in by the caller).

Contract:
* ``card.render`` contains selected business fields, not transport envelope fields.
  Business paths, URLs, numbers and code remain intact; terminal controls are escaped.
* correlation ids live only in the non-rendered ``metadata`` block (control reads them).
* generation labels (``worker-1``, ``worker-1.1``) are used verbatim.
* a card never claims success before a committed terminal state; timeout/failure are
  shown, not hidden. Timeout does NOT imply execution stopped: stop state is explicit
  (confirmed / not stopped / unknown), unknown never asserts stopped.
* routing classifier: UI notification vs business follow-up. Heartbeat/ack/timeout
  phrases are UI notifications only - they can never wake a model.
"""

import hashlib
import json
import math
import re
import time

KIND_RESULT = 'result'
KIND_ROUTING = 'routing'
KIND_FOLLOWUP = 'followup'
KINDS = (KIND_RESULT, KIND_ROUTING, KIND_FOLLOWUP)

SEVERITY_ERROR = 'error'
SEVERITY_WARN = 'warn'
SEVERITY_INFO = 'info'
SEVERITIES = (SEVERITY_ERROR, SEVERITY_INFO, SEVERITY_WARN)

TERMINAL = ('ok', 'timeout', 'invalid', 'stalled', 'blocked', 'cancelled', 'failed', 'skipped',
            'rejected')  # rejected: a named sub-fleet up / down refused (design.md §29)
NONTERMINAL = ('queued', 'accepted', 'offered', 'received', 'running', 'dispatched',
               'paused', 'retrying', 'handoff', 'uncertain')
STOP_STATES = (True, False, None)

MAX_RENDER_CHARS = 900
MAX_CELL = 300
MAX_INLINE_BODY = 400

STOP_CONFIRMED = '已确认执行停止。'
STOP_RUNNING = '原执行仍在进行：尚未确认停止，请先确认后再重派。'
STOP_UNKNOWN = '原执行尚未确认停止（未确认，不代表已停）。'

OPERATOR_PREFIX_RE = re.compile(r'^\s*\[Operator result[^\]]*\]\s*', re.I)
_CONTROLS_RE = re.compile(r'[\x00-\x08\x0b-\x1f\x7f-\x9f\ud800-\udfff]')
_ACK_RE = re.compile(r'^(?:ack|acked|acknowledge|收到|已收到|确认|已知晓)[!。.\s]*$', re.I)
_DONE_RE = re.compile(r'^(?:done|completed|已完成|完成)[!。.\s]*$', re.I)
_NUMUNIT_RE = re.compile(r'^\d+(?:\.\d+)?\s*(?:%|个|条|ms|s|秒|分钟|小时|天|次)$', re.I)

#: reply-spec keys / words that only need a UI acknowledgement (never a model turn)
ACK_KEYS = ('ack', 'ok', 'status', 'received', 'done', 'result', 'reply', 'answer',
            'output', 'output_path', 'path', 'paths', 'file', 'files', 'report',
            'reports', 'url', 'link', 'count', 'total', 'completed', 'n', 'num')
JUDGEMENT_WORDS = ('判断', '评估', '总结', '汇总', '建议', '决定', '下一步', '审批',
                   'approve', 'decide', 'judge', 'evaluate', 'review', 'summary',
                   'summarize', 'recommend', 'plan')
FOLLOWUP_KINDS = ('decision', 'business', 'followup', 'judgement', 'review', 'summary')
_INPUT_KINDS = ('operator_input', 'user_input', 'human_input', 'instruction', 'user')
MECHANICAL_KINDS = frozenset(('heartbeat', 'tick', 'ack', 'accepted', 'queued',
    'offered', 'received', 'running', 'settled', 'validated', 'committed', 'result',
    'dispatched', 'notice', 'reconnect', 'handoff', 'ready', 'retry', 'retrying',
    'lease', 'renew', 'dedup', 'routing', 'token', 'tokens', 'token_usage', 'context',
    'timeout', 'invalid', 'failed', 'stalled', 'blocked', 'cancelled', 'skipped', 'rejected'))

_EXTRA_HINTS = ()


class CardError(ValueError):
    """Raised when a record cannot be turned into a trustworthy card."""


def _jsonable(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    if isinstance(value, bool):
        return value
    return None


def sanitize_text(value, limit=None):
    """Escape terminal controls only. This is not a transport/secret classifier."""
    if value is None:
        return ''
    if not isinstance(value, str):
        fixed = _jsonable(value)
        if fixed is None and not isinstance(value, (bool, int)):
            try:
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                value = str(value)
        else:
            value = str(fixed if fixed is not None else value)
    text = _CONTROLS_RE.sub(lambda m: ('\\x%02x' if ord(m[0]) <= 255 else '\\u%04x')
                            % ord(m[0]), value)
    if limit and len(text) > limit:
        text = text[:limit].rstrip() + ' …'
    return text


def _clamp_lines(text, max_chars=MAX_RENDER_CHARS):
    text = sanitize_text(text)
    if len(text) > max_chars:
        marker = ' … [truncated]'
        text = text[:max_chars - len(marker)].rstrip() + marker
    return text


def _num(value, cast, floor=None, ceiling=None):
    if value is None or isinstance(value, bool):
        return None
    try:
        out = cast(float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    if isinstance(out, float) and not math.isfinite(out):
        return None
    if floor is not None and out < floor:
        return None
    if ceiling is not None and out > ceiling:
        return None
    return out


def _int(value, floor=None, ceiling=None):
    return _num(value, int, floor=floor, ceiling=ceiling)


def _float(value, floor=None, ceiling=None):
    return _num(value, float, floor=floor, ceiling=ceiling)


def _ms_to_s(value):
    """Epoch ms -> seconds (recognised by magnitude only); junk stays None, no 1970."""
    out = _float(value, floor=0)
    if out is None:
        return None
    return out / 1000.0 if out >= 1e11 else out


def _clock(epoch_s):
    """Local HH:MM:SS from epoch s or ms (magnitude decides); junk -> '?' (never 1970)."""
    ts = _ms_to_s(epoch_s)
    if ts is None:
        return None
    try:
        return time.strftime('%H:%M:%S', time.localtime(ts))
    except (ValueError, OverflowError, OSError):
        return None


def _duration_short(seconds):
    secs = _float(seconds, floor=0)
    if secs is None:
        return None
    if secs < 60:
        return '不足 1 分钟'
    minutes = int(secs // 60)
    if minutes < 60:
        return '约 %d 分钟' % minutes
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return ('约 %d 小时 %d 分钟' % (hours, minutes)) if minutes else ('约 %d 小时' % hours)
    days, hours = divmod(hours, 24)
    return ('约 %d 天 %d 小时' % (days, hours)) if hours else ('约 %d 天' % days)


def _stable_id(seed, prefix='ui'):
    raw = json.dumps(seed, ensure_ascii=False, sort_keys=True)
    return prefix + '-' + hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]


def event_id(kind, subject, at=None, slot='', status=''):
    """Stable id: identical inputs -> identical id, so replay/reconnect stay idempotent.

    ``at`` accepts epoch seconds or epoch milliseconds (magnitude decides). Anything
    unparseable is dropped from the seed instead of becoming a fabricated timestamp, so
    an event with no trustworthy clock still gets a stable id.
    """
    seed = {'kind': kind, 'subject': subject}
    ts = _ms_to_s(at)
    if ts is not None:
        seed['at'] = round(ts, 3)
    if slot:
        seed['slot'] = slot
    if status:
        seed['status'] = status
    return _stable_id(seed)


def register_classifier(hints=None):
    """Register extra *business follow-up* hints. Heartbeat/ack/timeout wording cannot
    be registered as a model wake-up (those stay UI-only by design)."""
    global _EXTRA_HINTS
    words = list(_EXTRA_HINTS)
    for hint in (hints or []):
        text = _natural(sanitize_text(hint, 120).strip())
        if text and text not in words:
            words.append(text)
    _EXTRA_HINTS = tuple(words)
    return len(_EXTRA_HINTS)


def _natural(text):
    """Lowercase for keyword matching only; never used for display."""
    return (text or '').lower()


# ---------------------------------------------------------------------------
# record accessors (label/ids kept verbatim; display text is sanitized)
# ---------------------------------------------------------------------------

def _slot_of(record):
    for key in ('worker', 'agent', 'slot', 'to', 'target'):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return 'worker'


def _request_of(record):
    for key in ('request_id', 'request', 'id', 'rid'):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ''


def _stop_state(execution_stopped):
    if execution_stopped is not True and execution_stopped is not False and execution_stopped is not None:
        raise CardError('execution_stopped 只能是 True/False/None')
    if execution_stopped is True:
        return ('confirmed', STOP_CONFIRMED)
    if execution_stopped is False:
        return ('running', STOP_RUNNING)
    return ('unknown', STOP_UNKNOWN)


def _title_of(task_title):
    """Task title shown as-is: only the ``[Operator result ...]`` wrapper is stripped.

    Titles do not supply correlation identity. Unknown stays absent.
    """
    return OPERATOR_PREFIX_RE.sub('', sanitize_text(task_title, MAX_CELL)).strip()


def _meta_base(status, slot, title, rid, now, extra=None):
    meta = {'kind': KIND_RESULT, 'status': status, 'slot': slot}
    if title:
        meta['title'] = title
    if rid:
        # correlation id stays in the non-rendered metadata, never in the window text
        meta['request_id'] = rid
    if now is not None:
        meta['now'] = now
    meta.update(extra or {})
    return meta


def _card(sev, render, meta, now, has_detail=False, wake_model=False):
    identity = {key: meta[key] for key in ('source_event_id', 'request_id', 'message_id',
                'delivery_id', 'stream_id') if key in meta}
    meta['event_id'] = event_id(KIND_RESULT,
        identity or meta.get('title') or meta.get('slot', ''),
        None if identity else now, slot=meta.get('slot', ''), status=meta['status'])
    meta['has_detail'] = bool(has_detail)
    detail_hint = '\n详情见 control。' if has_detail else ''
    render = _clamp_lines(render, MAX_RENDER_CHARS - len(detail_hint)) + detail_hint
    return {'kind': KIND_RESULT, 'severity': sev, 'render': render,
            'metadata': meta, 'wake_model': bool(wake_model)}


def result_card(record, task_title=None, execution_stopped=None, now=None):
    """UI-only result card for one outcome (terminal or intermediate).

    ``task_title``: short original task title (tells apart which item timed out);
    unknown -> the neutral "任务" wording, never invented.
    ``execution_stopped``: True = confirmed stopped; False = known still running;
    None = unknown (must not assert stopped).
    ``now``: epoch s or ms for the stable id; junk is dropped, never guessed.
    """
    if not isinstance(record, dict):
        raise CardError('record 必须是 dict')
    status = None
    for key in ('status', 'state', 'outcome'):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            status = value.strip().lower()
            break
    if status is None:
        raise CardError('record 缺少 status')
    if status not in TERMINAL and status not in NONTERMINAL:
        raise CardError('未知 status: %s' % status[:60])
    if status == 'ok' and record.get('confirmed') not in (True, None):
        raise CardError('ok 结果的 confirmed 标志必须为 True（未确认不能显示成功）')

    slot = _slot_of(record)
    title = _title_of(task_title)          # kept verbatim (only operator noise stripped)
    rid = _request_of(record)
    now = _ms_to_s(now)
    elapsed = _float(record.get('elapsed_s'), floor=0, ceiling=86400 * 365)
    if elapsed is None:
        elapsed = _float(record.get('elapsed'), floor=0, ceiling=86400 * 365)
    dur = _duration_short(elapsed)
    stop_state, stop_line = _stop_state(execution_stopped)
    meta = _meta_base(status, slot, title, rid, now)
    for key in ('message_id', 'delivery_id', 'stream_id'):
        if isinstance(record.get(key), str) and record[key]:
            meta[key] = record[key]
    if isinstance(record.get('event_id'), str) and record['event_id']:
        meta['source_event_id'] = record['event_id']
    if dur is not None:
        meta['duration'] = dur
    meta['stop_state'] = stop_state
    lines = []

    if status == 'ok':
        # Only the outer record is transport. The result is arbitrary business
        # JSON, including nested fields named request_id/attempt/metadata.
        display_fields = ('reply', 'text', 'answer', 'output')
        body = record['result'] if 'result' in record else {
            k: record[k] for k in display_fields if k in record and record[k] is not None}
        detail = False
        if 'result' not in record and not body:
            lines.append('%s 已完成。' % slot)
        elif not isinstance(body, dict) or len(body) <= 1:
            only = (next(iter(body.values())) if isinstance(body, dict)
                    and len(body) == 1 and next(iter(body)) in display_fields else body)
            if isinstance(only, str) and len(only) <= MAX_INLINE_BODY:
                lines.append(only)
            else:
                blob = only if isinstance(only, str) else json.dumps(only, ensure_ascii=False,
                                                                      sort_keys=True)
                if len(blob) > MAX_INLINE_BODY:
                    blob = blob[:MAX_INLINE_BODY] + ' …（完整内容见 control）'
                    detail = True
                lines.append(blob)
        else:
            for key in sorted(body):
                value = body[key]
                blob = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False,
                                                                       sort_keys=True)
                if len(blob) > MAX_CELL:
                    blob = blob[:MAX_CELL] + ' …（完整内容见 control）'
                    detail = True
                lines.append('%s：%s' % (key, blob))
            detail = True
        if dur is not None:
            lines.append('用时 %s。' % dur)
        return _card(SEVERITY_INFO, '\n'.join(lines), meta, now, has_detail=detail)

    if status == 'timeout':
        if title:
            lines.append('任务：%s' % title)
        lines.append('%s 处理超时%s，未取得有效结果。' % (slot, '（%s）' % dur if dur else ''))
        lines.append(stop_line)
        meta['detail_ref'] = 'timeout detail'
        return _card(SEVERITY_ERROR, '\n'.join(lines), meta, now, has_detail=True)

    if status in ('invalid', 'failed'):
        head = '答复无效，未通过验收。' if status == 'invalid' else '执行失败。'
        lines.append('%s %s' % (slot, head))
        meta['detail_ref'] = 'error detail'
        meta['reason'] = 'validation failed' if status == 'invalid' else 'execution failed'
        if execution_stopped is not True:
            lines.append(stop_line)
        return _card(SEVERITY_ERROR, '\n'.join(lines), meta, now,
                     has_detail=True)

    if status == 'stalled':
        idle = _float(record.get('idle_s'), floor=0, ceiling=86400 * 365)
        lines.append('%s 卡住：%s无新动作。' % (slot, _duration_short(idle) or '一段时间'))
        lines.append(stop_line)
        meta['idle'] = _duration_short(idle) or 'unknown'
        meta['detail_ref'] = 'stall detail'
        return _card(SEVERITY_WARN, '\n'.join(lines), meta, now, has_detail=True)

    if status == 'blocked':
        reason = None
        for key in ('reason', 'blocked_on', 'error', 'detail'):
            value = record.get(key)
            if isinstance(value, str) and value.strip():
                reason = value.strip()
                break
        shown = sanitize_text(mask_secrets(reason), MAX_CELL) if reason else '需要人工处理（原因未记录）'
        if len(shown) >= MAX_CELL:
            shown = shown + ' …（完整信息见 control）'
        lines.append('%s 受阻：%s，等待人工处理。' % (slot, shown))
        meta['needs_human'] = True
        meta['reason'] = shown
        if not reason:
            meta['detail_ref'] = 'blocked detail'
        return _card(SEVERITY_WARN, '\n'.join(lines), meta, now,
                     has_detail=not bool(reason))

    if status == 'cancelled':
        lines.append('%s 已取消。' % slot)
        return _card(SEVERITY_INFO, '\n'.join(lines), meta, now)

    if status == 'skipped':
        lines.append('%s 已跳过，本次未执行。' % slot)
        return _card(SEVERITY_INFO, '\n'.join(lines), meta, now)

    if status == 'rejected':
        reason = record.get('reason') if isinstance(record.get('reason'), str) else ''
        lines.append('%s 已拒绝%s。' % (slot, '：' + sanitize_text(mask_secrets(reason), MAX_CELL) if reason else ''))
        return _card(SEVERITY_INFO, '\n'.join(lines), meta, now)

    lines.append('%s %s（尚未完成）。' % (slot, status))
    return _card(SEVERITY_INFO, '\n'.join(lines), meta, now)


# ---------------------------------------------------------------------------
# routing classification: UI notification vs business follow-up
# ---------------------------------------------------------------------------

def classify_routing(text, request_kind='', reply_spec=None, at=None, slot=''):
    """Typed mechanical events stay UI-only; explicit business/input kinds wake.

    Only untyped results use reply-schema/content hints. The caller is responsible
    for supplying a trusted event kind, not a model-authored hide/wake directive.
    """
    if not isinstance(request_kind, str):
        raise CardError('request_kind 必须是 str')
    kind = request_kind.strip().lower()
    body = _natural(sanitize_text(text)).strip()
    def route(wake, reason):
        return _route(KIND_FOLLOWUP if wake else KIND_ROUTING, reason, at, slot, wake)
    if kind in MECHANICAL_KINDS:
        return route(False, '状态通知 %s：仅显示' % kind)
    if not body:
        return route(False, '空文本：仅显示')
    if kind in _INPUT_KINDS:
        return route(True, '用户指令：需要 supervisor 处理')
    if kind in FOLLOWUP_KINDS:
        return route(True, '业务后续：需要 supervisor 处理')
    spec = reply_spec.get('required') if isinstance(reply_spec, dict) else None
    if isinstance(spec, (list, tuple)):
        required = [k.lower() for k in spec if isinstance(k, str)]
        if not required or all(k in ACK_KEYS for k in required):
            return route(False, '简单结果：显示卡即可')
        return route(True, '答复需要业务判断')
    if _ACK_RE.fullmatch(body) or _DONE_RE.fullmatch(body):
        return route(False, '确认/完成状态：显示卡即可')
    if ((body.startswith('/') and not any(c.isspace() for c in body))
            or (not any(c.isspace() for c in body)
                and body.endswith(('.md', '.json', '.csv', '.html', '.txt')))):
        return route(False, '产物路径：显示卡即可')
    if _NUMUNIT_RE.fullmatch(body):
        return route(False, '计数/指标：显示卡即可')
    if any(word.lower() in body for word in JUDGEMENT_WORDS + _EXTRA_HINTS):
        return route(True, '业务内容需要判断')
    if len(body) > 120:
        return route(True, '较长业务正文：需要业务判断')
    return route(False, '简短结果：显示卡即可')


def _route(kind, reason, at, slot, wake, title='', severity=SEVERITY_INFO):
    meta = {'kind': kind, 'reason': sanitize_text(reason, MAX_CELL), 'slot': sanitize_text(slot, 60)}
    if title:
        meta['title'] = sanitize_text(title, MAX_CELL)
    ts = _ms_to_s(at)
    if ts is not None:
        meta['at'] = round(ts, 3)
        meta['clock'] = _clock(ts)
    meta['event_id'] = event_id(kind, title or reason, at, slot=slot)
    head = meta.get('clock') or '?'
    body = meta['title'] if title else reason
    return {'kind': kind, 'severity': severity,
            'render': _clamp_lines('%s %s：%s' % (head, kind, body)),
            'metadata': meta, 'wake_model': bool(wake)}


def routing_card(text, kind='notice', at=None, slot='', title=None, severity=SEVERITY_INFO):
    """UI-only notice card (queued / dispatched / ack / heartbeat). Never wakes a model.

    An unknown ``kind`` is rejected: no silent reclassification into a model wake-up.
    """
    if kind not in MECHANICAL_KINDS:
        raise CardError('未知 routing kind: %s' % kind[:60])
    return _route(kind, text, at, slot, False, title=title or '', severity=severity)


def timeline_line(card):
    """One-line timeline entry: ``HH:MM:SS KIND slot [severity] status text [详情 id=…]``.

    The clock header is resolved once (``metadata['clock']``) so two callers on the same
    record get the same line; the full content stays in the detail store and a truncated
    line keeps a visible ``[... truncated]`` marker instead of a silent gap.
    """
    meta = card.get('metadata') or {}
    head = meta.get('clock') or _clock(meta.get('now') or meta.get('at')) or '?'
    marks = []
    if meta.get('has_detail') or meta.get('detail_ref'):
        marks.append('详情')
    if ' … [truncated]' in card.get('render', ''):
        marks.append('截断')
    if meta.get('event_id'):
        marks.append('id=%s' % meta['event_id'])
    body = card.get('render', '').replace('\n', ' / ')
    parts = [head, str(meta.get('kind', '?')).upper(), sanitize_text(meta.get('slot', '')),
             '[%s]' % card.get('severity', SEVERITY_INFO),
             sanitize_text(meta.get('status', '')), body]
    if marks:
        parts.append('[%s]' % ' '.join(marks))
    line = ' | '.join(p for p in parts if p and p != '[]')
    if len(line) > MAX_RENDER_CHARS:
        line = line[:MAX_RENDER_CHARS - 14].rstrip() + ' … [truncated]'
    return line


def mask_secrets(value, depth=0):
    """Recursively mask known secret field names in dict/list/str.

    Only *known* names are masked: arbitrary secrets inside free text are not claimed
    to be detected. Ordinary model counters (tokens, token_count, max_tokens) stay
    visible so real usage numbers are not swallowed.
    """
    if depth > 12:
        return '<深度截断>'
    if isinstance(value, dict):
        out = {}
        for key, sub in value.items():
            low = _natural(str(key))
            base = low.rsplit('.', 1)[-1].replace('-', '_')
            if base in ('authorization', 'proxy_authorization', 'cookie', 'cookies',
                        'set_cookie', 'api_key', 'x_api_key', 'apikey', 'token',
                        'access_token', 'refresh_token', 'client_secret', 'secret',
                        'password', 'passwd'):
                out[key] = '***'
            else:
                out[key] = mask_secrets(sub, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [mask_secrets(sub, depth + 1) for sub in value]
    if isinstance(value, str):
        # Auth/cookie headers include schemes, spaces and multiple cookie values.
        # Redact the whole value, not just the first word (e.g. "Bearer").
        text = re.sub(r'(?im)(\b(?:authorization|proxy-authorization|cookie|set-cookie)'
                      r'\b[\t ]*["\']?[\t ]*[:=][\t ]*)([^\r\n]+)', r'\1***', value)
        for name in ('api_key', 'api-key', 'x-api-key', 'apikey', 'token',
                     'access_token', 'refresh_token', 'client_secret', 'secret',
                     'password', 'passwd'):
            text = re.sub(r'(?i)(\b%s\b["\']?\s*[:=]\s*)'
                          r'("[^"\r\n]*"|\'[^\'\r\n]*\'|[^\s,;}]+)'
                          % re.escape(name), r'\1***', text)
        return text
    return value
