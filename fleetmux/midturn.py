"""Daemon mid-turn handoff: bounded file readers, policy and clock arithmetic.

No Redis, CLI hooks or transport here. The probe consumes immutable snapshots supplied by
Fleet's main loop; only that loop may order, settle or rotate a delivery (design r3).
"""
import json
import math
import re
import threading
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path

TAIL_BYTES = 65536
DEFAULTS = {'midturn_ratio': .8, 'midturn_retry': 300, 'midturn_max_splits': 3}
PREFIX = '[FLEET MID-TURN HANDOFF fx-'
MIDTURN_PROMPT = (
    '[FLEET MID-TURN HANDOFF {midturn_id}] Context is {used}/{window}. This is the fleet daemon\'s own '
    'context handoff, not a plan change: obey it. Stop now; start no new step. '
    'Edit only the handoff note {path}: keep its three ## headings '
    '(Done (facts) / In progress / Next steps (in order)), at most 10 lines each; '
    'under In progress write exactly where you stopped inside the current instruction, '
    'under Next steps what remains of it. Do not send your final report or call '
    'fleet_result/fleet_decision. Then end your turn with the single word HANDOFF.'
)
CONTINUATION_PROMPT = (
    '[Continuation] You are {label}; {prev} stopped this same instruction midway (context full). '
    'Handoff note: {note}. Continue from it: do not redo finished steps, re-check the step marked '
    'in progress. The original instruction follows unchanged:\n\n{instruction}'
)


# Long task split (docs/design.md §44): the same probe looks at a long busy turn's tool calls.
SPLIT_DEFAULTS = {'check_minutes': 40, 'window_minutes': 10, 'min_calls': 15, 'min_distinct': 2, 'restore_model': True}
SPLIT_TAIL_BYTES = 1 << 20  # read only when a check is due (once per check_minutes); older rows are not counted
SPLIT_PREFIX = '[FLEET LONG-RUNNING '
SPLIT_PROMPT = (
    '[FLEET LONG-RUNNING {split_id}] You have been on {task} for {minutes} minutes and the work keeps moving '
    '({calls} tool calls on {distinct} different files / commands in the last {window} minutes), so the fleet '
    'wants it split. Finish the step you are in, start nothing new, and end this turn with your report in the '
    'AGENTS.md format: Done = what is finished so far, Not done = what remains; end it with the escalate JSON '
    '{{"escalate": "<one sentence>", "proposal": [{{"id": "{task}.0", "instruction": "<piece, >= 12 chars>", '
    '"output": "<file>", "kind": "<what kind of work: code / research / review / long computation ...>"}}, ...], '
    '"workers": <n>}} that splits what remains into pieces (ids {task}.0, {task}.1, ...). This is not a context '
    'handoff: write no handoff note; the supervisor decides on the split.'
)
# §44: the supervisor approved the split while the worker is in a turn (Fleet.split_hold): same channel, same head.
SPLIT_APPROVED_PROMPT = (
    '[FLEET LONG-RUNNING {split_id}] The supervisor approved the split of {task}: sub-fleet {task} takes over what '
    'remains of it. Finish the step you are in, start nothing new, and end this turn with your report in the '
    'AGENTS.md format: Done = what is finished so far, Not done = what remains. Send no split proposal and write no '
    'handoff note now: the fleet hands over next. A ruling or instruction on {task} that reaches you now does not '
    'change this.'
)
EDIT_TOOLS = {'claude': ('Edit', 'Write', 'MultiEdit', 'NotebookEdit'), 'pi': ('edit', 'write')}
PATCH_FILE = re.compile(r'^\*\*\* (?:Update|Add|Delete) File: (.+)$', re.M)


def resolve_split(table, worker=None):
    """(values, source) of the split settings: worker entry > roster `split:` > built-in, per key."""
    values, where = {}, []
    for key, default in SPLIT_DEFAULTS.items():
        for name, layer in (('worker', worker or {}), ('roster', table or {}), ('default', {key: default})):
            if key in layer:
                values[key] = layer[key]
                where.append(key + ': ' + name)
                break
    return values, '; '.join(where)


def _when(row):
    try:
        return datetime.fromisoformat(str(row.get('timestamp')).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError):
        return None


def _calls(cli, row):
    """[(distinct keys, edited files)] for each tool call in one transcript row."""
    out = []
    if cli == 'claude':
        if row.get('type') != 'assistant' or row.get('isSidechain') or row.get('agentId'):
            return out
        content = (row.get('message') or {}).get('content')
        for block in content if isinstance(content, list) else []:
            if isinstance(block, dict) and block.get('type') == 'tool_use':
                inp = block.get('input') if isinstance(block.get('input'), dict) else {}
                target = next((inp[k] for k in ('file_path', 'notebook_path', 'path', 'command', 'pattern', 'url')
                               if isinstance(inp.get(k), str)), None)
                out.append(([str(target or block.get('name'))],
                            [target] if target and block.get('name') in EDIT_TOOLS['claude'] else []))
    elif cli == 'pi':
        message = row.get('message') if row.get('type') == 'message' else None
        if not isinstance(message, dict) or message.get('role') != 'assistant':
            return out
        for block in message.get('content') or []:
            if isinstance(block, dict) and block.get('type') == 'toolCall':
                args = block.get('arguments') if isinstance(block.get('arguments'), dict) else {}
                target = next((args[k] for k in ('path', 'command') if isinstance(args.get(k), str)), None)
                out.append(([str(target or block.get('name'))],
                            [target] if target and block.get('name') in EDIT_TOOLS['pi'] else []))
    elif cli == 'codex':
        payload = row.get('payload') if row.get('type') == 'response_item' else None
        if isinstance(payload, dict) and payload.get('type') in ('function_call', 'custom_tool_call', 'local_shell_call'):
            text = payload.get('arguments') or payload.get('input') or json.dumps(payload.get('action'))
            text = text if isinstance(text, str) else json.dumps(text)
            files = [f.strip() for f in PATCH_FILE.findall(text)]  # apply_patch: the files it edits
            out.append((files or [' '.join(text.split())[:300]], files))
    return out


def activity(cli, path, since):
    """{calls, distinct, files} of the tool calls after ``since`` (epoch) in a bounded transcript tail.

    None when this CLI keeps no tool-call record the fleet can read (opencode, dsh) or the file is
    unreadable: never guessed. Rows older than the tail are not counted (a lower bound)."""
    if cli not in ('claude', 'codex', 'pi'):
        return None
    try:
        _, tail = _read_tail(path, SPLIT_TAIL_BYTES)
    except (OSError, TypeError, ValueError):
        return None
    calls, distinct, files = 0, set(), set()
    for line in reversed(tail.splitlines()):
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(row, dict):
            continue
        when = _when(row)
        if when is None:
            continue
        if when < since:
            break
        for targets, edited in _calls(cli, row):
            calls += 1
            distinct.update(targets)
            files.update(edited)
    return {'calls': calls, 'distinct': len(distinct), 'files': len(files)}


def _number(value, name, minimum=0):
    if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
        raise ValueError(name + ' must be a finite number >= ' + str(minimum))
    return value


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(name + ' must be an integer >= ' + str(minimum))
    return value


def _ratio(value, name):
    _number(value, name)
    if not 0 < value < 1:
        raise ValueError(name + ' must be between 0 and 1')
    return value


def validate_settings(settings):
    """Validate the new keys in a config/card/roster entry; leave existing keys to §22.

    Zero splits disables splitting; retry must be positive.
    """
    if not isinstance(settings, dict):
        raise ValueError('midturn settings must be a mapping')
    for name in DEFAULTS.keys() & settings.keys():
        if name == 'midturn_ratio':
            _ratio(settings[name], name)
        elif name == 'midturn_max_splits':
            _integer(settings[name], name)
        else:
            _number(settings[name], name, .001)


def resolve_settings(cli, model, cfg, table=None, worker=None, card=None):
    """(values, sources), first hit per key: worker > roster > config > card > flag.

    Tables use exact ``cli:model`` then ``cli`` keys, as in §22. ``card`` is
    (profile id, mapping of new keys). This resolves no window or handoff_ratio.
    """
    layers = [('worker', worker or {})]
    for where, tab in (('roster', table or {}), ('config', cfg.get('handoff') or {})):
        layers += [(where + ' ' + key, tab[key]) for key in
                   ((cli + ':' + model) if model else None, cli) if key and key in tab]
    if card:
        layers.append(('card ' + card[0], card[1]))
    values, sources = {}, {}
    for where, entry in layers + [('flag', cfg)]:
        validate_settings(entry)
        for key in DEFAULTS.keys() & entry.keys():
            if key not in values:
                values[key] = entry[key]
                sources[key] = '--' + key.replace('_', '-') if where == 'flag' else where
    for key, default in DEFAULTS.items():
        if key not in values:
            values[key], sources[key] = default, 'default'
    return values, sources


def decide(used, window, *, midturn_ratio=.8, busy=True, ready=False, guarded=False,
           midturn_splits=0, midturn_max_splits=3, ordered=False, elapsed=0,
           tries=1, midturn_retry=300):
    """Return none / steer / retry / kill / blocked; never clamp the user's ratio."""
    if guarded or not (busy or ready):
        return 'none'
    if ordered:
        return ('retry' if tries < 2 else 'kill') if elapsed >= midturn_retry else 'none'
    if used is None or not window or used < midturn_ratio * window:
        return 'none'
    if ready or midturn_splits >= midturn_max_splits:
        return 'blocked'
    return 'steer'


def continuation(label, prev, note, instruction):
    return CONTINUATION_PROMPT.format(label=label, prev=prev, note=note, instruction=instruction)


# Pane status for a slot held at the split cap (agent_bridge fault frame -> pi status bar).
LIMIT_STATUS = '已达续做上限，等监工拆小。'


def blocked_text(splits):
    return '这条派工一个会话装不下（已续做 ' + str(splits) + ' 次），请拆小'


def registered_prompt(info, generation):
    """Only the exact prompt registered for this generation may bypass an input gate."""
    hid = info.get('midturn_id') or ''
    prompt = info.get('midturn_prompt') or ''
    if (info.get('midturn_typed_g') == str(generation) and hid
            and prompt.startswith('[FLEET MID-TURN HANDOFF ' + hid + '] ')):
        return prompt
    return ''


def _read_tail(path, size):
    with open(path, 'rb') as stream:
        stream.seek(0, 2)
        start = max(0, stream.tell() - size)
        stream.seek(start)
        tail = stream.read(size)
    if start:
        first, sep, rest = tail.partition(b'\n')
        start += len(first) + len(sep)
        tail = rest
    return start, tail


def _tail_rows(path, size):
    """Read a bounded suffix, dropping a cut first line and malformed/unfinished JSON."""
    try:
        _, tail = _read_tail(path, size)
        for line in reversed(tail.splitlines()):
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(row, dict):
                yield row
    except (OSError, TypeError, ValueError):
        return


def claude_transcript_usage(path, window):
    """Latest main-thread usage in at most 64KB; missing usage is (None, window).

    A missing measurement must not masquerade as zero or a file-size estimate. No glob,
    transcript discovery, Redis access or writes: caller supplies this generation's file.
    """
    fields = ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens', 'output_tokens')
    for row in _tail_rows(path, TAIL_BYTES):
        if row.get('type') != 'assistant' or row.get('isSidechain') or row.get('agentId'):
            continue
        message = row.get('message')
        usage = message.get('usage') if isinstance(message, dict) else None
        if not isinstance(usage, dict) or 'input_tokens' not in usage:
            continue
        values = [usage.get(key, 0) for key in fields]
        if all(type(value) is int and value >= 0 for value in values):
            return sum(values), window
    return None, window


def codex_rollout_usage(path, window):
    """Latest token_count.last_token_usage in at most 64KB; never cumulative totals."""
    for row in _tail_rows(path, TAIL_BYTES):
        payload = row.get('payload')
        if (row.get('type') != 'event_msg' or not isinstance(payload, dict)
                or payload.get('type') != 'token_count'):
            continue
        info = payload.get('info')
        if not isinstance(info, dict):
            continue
        usage = info.get('last_token_usage')
        if not isinstance(usage, dict):
            continue
        used = usage.get('total_tokens')
        measured = info.get('model_context_window')
        if type(used) is not int or used < 0:
            continue
        if measured is not None and (type(measured) is not int or measured <= 0):
            continue
        return used, window if measured is None else measured
    return None, window


def js_length(text):
    return len(str(text).encode('utf-16-le', errors='surrogatepass')) // 2


def js_json(value):
    """Compact JSON.stringify spelling, including JS's fixed/exponent number boundary."""
    if value is None or isinstance(value, (bool, str)):
        text = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        # Well-formed JSON.stringify escapes lone surrogates; astral characters stay intact.
        return ''.join('\\u%04x' % ord(c) if 0xD800 <= ord(c) <= 0xDFFF else c for c in text)
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            return 'null'
        if value == 0:
            return '0'
        if isinstance(value, int) and abs(value) < 10**21:
            return str(value)
        text = repr(float(value)).lower()
        if 1e-6 <= abs(value) < 1e21:
            fixed = format(Decimal(text), 'f')
            return fixed.rstrip('0').rstrip('.') if '.' in fixed else fixed
        mantissa, _, exponent = text.partition('e')
        mantissa = mantissa.removesuffix('.0')
        return mantissa + ('e' + ('+' if int(exponent) >= 0 else '-') + str(abs(int(exponent))) if exponent else '')
    if isinstance(value, list):
        return '[' + ','.join(js_json(item) for item in value) + ']'
    return '{' + ','.join(js_json(str(k)) + ':' + js_json(v) for k, v in value.items()) + '}'


def pi_estimate(message):
    """Pi 1.3 estimateTokens: round each message separately, using JS string lengths."""
    content, chars = message.get('content') or [], 0
    if isinstance(content, str):
        chars = js_length(content)
    else:
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get('type')
            if kind == 'text':
                chars += js_length(block.get('text', ''))
            elif message.get('role') == 'assistant':
                if kind == 'thinking':
                    chars += js_length(block.get('thinking', ''))
                elif kind == 'toolCall':
                    chars += js_length(block.get('name', '')) + js_length(js_json(block.get('arguments')))
    return math.ceil(chars / 4)


def pi_session_usage(path, window, state=None):
    """Pi's last valid usage + later messages; a compaction resets usage to unknown.

    The optional file-thread cache retains the anchor when it leaves the 64KB suffix.
    Only a contiguous suffix may extend it: gaps, truncation, or a new file require a
    fresh valid assistant. An incomplete last JSON line is retried on the next scan.
    """
    state = {} if state is None else state
    try:
        start, tail = _read_tail(path, TAIL_BYTES)
    except (OSError, TypeError, ValueError):
        return None, window
    offset = state.get('offset', -1)
    used = None
    if state.get('path') == str(path) and start <= offset <= start + len(tail):
        used = state.get('used')
        tail, start = tail[offset - start:], offset
    for line in tail.splitlines(keepends=True):
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            if not line.endswith(b'\n'):
                break
            start += len(line)
            continue
        start += len(line)
        if not isinstance(row, dict):
            continue
        if row.get('type') == 'compaction':
            used = None
            continue
        message = row.get('message')
        if row.get('type') != 'message' or not isinstance(message, dict):
            continue
        total = 0
        if message.get('role') == 'assistant' and message.get('stopReason') not in ('aborted', 'error'):
            usage = message.get('usage') or {}
            if isinstance(usage, dict):
                values = [usage.get(k, 0) for k in ('input', 'output', 'cacheRead', 'cacheWrite')]
                total = usage.get('totalTokens') or (sum(values) if all(type(v) is int and v >= 0 for v in values) else 0)
        if type(total) is int and total > 0:
            used = total
        elif used is not None:
            used += pi_estimate(message)
    state.update(path=str(path), used=used, offset=start)
    return used, window


class Probe:
    """One sequential file-only thread, with a locked snapshot/result mailbox.

    Main-loop snapshots contain no live Fleet/Redis references. A slow scan delays only
    this thread. Every changed file costs at most TAIL_BYTES; unchanged stats cost no read.
    """
    def __init__(self):
        self.lock = threading.Lock()
        self.targets, self.results, self.stamps, self.pi_states = {}, {}, {}, {}
        self.stop = threading.Event()
        self.thread = None

    def update(self, targets):
        with self.lock:
            self.targets = {slot: dict(target) for slot, target in targets.items()}

    def take(self):
        with self.lock:
            results, self.results = self.results, {}
        return results

    def poll(self):
        with self.lock:
            targets = dict(self.targets)
        for slot, target in targets.items():
            result = dict(target, used=None, path='', stamp=None)
            identity = (slot, target['generation'], target['message_id'])
            try:
                cli = target['cli']
                path = Path(target['path']) if target.get('path') else None
                if cli == 'pi':
                    paths = list(Path(target['directory']).glob('*.jsonl'))
                    path = max(paths, key=lambda p: p.stat().st_mtime_ns) if paths else None
                elif cli == 'codex' and not path and target.get('thread'):
                    paths = list(Path(target['directory']).rglob('*' + target['thread'] + '*.jsonl'))
                    path = max(paths, key=lambda p: p.stat().st_mtime_ns) if paths else None
                if target.get('split_since') is not None:  # §44: due once per check, file changed or not
                    result['activity'] = activity(cli, path, target['split_since']) if path else None
                if path:
                    stat = path.stat()
                    stamp = (str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino)
                    if self.stamps.get(identity) == stamp:
                        if 'activity' in result:
                            with self.lock:
                                self.results[slot] = result
                        continue
                    previous = self.stamps.get(identity)
                    if previous and (previous[0] != stamp[0] or previous[3] != stamp[3] or previous[2] > stamp[2]):
                        self.pi_states.pop(identity, None)
                    self.stamps[identity] = stamp
                    reader = {'pi': pi_session_usage, 'claude': claude_transcript_usage,
                              'codex': codex_rollout_usage}.get(cli)
                    if reader:
                        used, window = (reader(path, target['window'], self.pi_states.setdefault(identity, {}))
                                        if cli == 'pi' else reader(path, target['window']))
                        result.update(used=used, window=target.get('override') or window,
                                      path=str(path), stamp=stamp)
            except (OSError, ValueError, TypeError) as exc:
                result['error'] = str(exc)
            with self.lock:
                self.results[slot] = result
        live = {(s, t['generation'], t['message_id']) for s, t in targets.items()}
        self.stamps = {k: v for k, v in self.stamps.items() if k in live}
        self.pi_states = {k: v for k, v in self.pi_states.items() if k in live}

    def run(self):
        while not self.stop.is_set():
            started = time.monotonic()
            self.poll()
            self.stop.wait(max(0, 1 - (time.monotonic() - started)))

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self.run, name='fleet-midturn', daemon=True)
            self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=1)


def _stored_number(record, key, default=0):
    """Redis hashes contain strings; reject nonfinite/negative clock values."""
    value = record.get(key, default)
    if value in (None, ''):
        value = default
    if isinstance(value, bool):
        raise ValueError('invalid ' + key)
    return _number(float(value), key)


def _paused_at(record):
    value = record.get('clock_paused_at')
    return None if value is None or value == '' else _stored_number(record, 'clock_paused_at')


def effective_age(req, now):
    """Service request age in seconds, excluding completed and current pauses (r3 §2.5).

    This is not the task hours budget: that hard budget continues to use wall time.
    """
    _number(now, 'now')
    created = _stored_number(req, 'created')
    paused = _paused_at(req)
    end = min(now, paused) if paused is not None else now
    return max(0, end - created - _stored_number(req, 'clock_paused_ms') / 1000)


def pause_clock(record, now):
    """Copy with an idempotent pause marker; callers must check expiry before ordering."""
    _number(now, 'now')
    result = dict(record)
    if _paused_at(record) is None:
        result['clock_paused_at'] = now
    return result


def resume_clock(record, now):
    """Copy with actual paused milliseconds accumulated and marker cleared, on every exit."""
    _number(now, 'now')
    result = dict(record)
    paused = _paused_at(record)
    if paused is not None:
        result['clock_paused_ms'] = round(_stored_number(record, 'clock_paused_ms')
                                         + max(0, now - paused) * 1000)
        result['clock_paused_at'] = ''
    return result


def pause_message(message, now_ms, *, midturn_retry=300, profile_ready_timeout=180):
    """Snapshot remaining bus deadline and protect the old delivery during handoff.

    Only the daemon renews the lease. The returned fields belong in the ordering transaction.
    """
    _integer(now_ms, 'now_ms')
    _number(midturn_retry, 'midturn_retry')
    _number(profile_ready_timeout, 'profile_ready_timeout')
    result = dict(message)
    if _paused_at(message) is not None:
        return result
    deadline = _stored_number(message, 'deadline_at')
    if deadline <= now_ms:
        raise ValueError('message deadline already expired; cannot order midturn handoff')
    result = pause_clock(message, now_ms / 1000)
    result['deadline_remaining_ms'] = round(deadline - now_ms)
    result['deadline_at'] = round(deadline + (2 * midturn_retry + profile_ready_timeout) * 1000)
    return result


def resume_message(message, now_ms):
    """Continuation offer: restore exactly the original remaining duration, once."""
    _integer(now_ms, 'now_ms')
    result = resume_clock(message, now_ms / 1000)
    if _paused_at(message) is not None:
        if 'deadline_remaining_ms' not in message:
            raise ValueError('paused message lacks deadline_remaining_ms')
        result['deadline_at'] = now_ms + round(_stored_number(message, 'deadline_remaining_ms'))
    return result
