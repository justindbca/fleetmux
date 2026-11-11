"""Claude agent hook channel: deliveries reach Claude Code through its Stop hook, never the input box.

After the first (CLI-argument) startup prompt, the daemon never types into a Claude worker's pane.
It pushes the business text to a private Redis mailbox for that slot and generation. The worker's
Stop hook, which runs at every turn end, settles the turn as before, then blocks on that mailbox
and answers Claude Code with ``{"decision": "block", "reason": <business text>}``. Claude Code
takes the reason as the next instruction and starts a new turn in the same session.

Measured on Claude Code 2.1.280 (docs: claude-hook-report.md):
  * A command hook's ``timeout`` is in seconds with no upper clamp (default 600). A hook that is
    still running at its timeout is killed and its output dropped: the turn ends normally and the
    session sits idle with no hook waiting ("unarmed").
  * ``stop_hook_active`` is only a flag (true on every Stop that follows a block). It never stops a
    block by itself, but Claude Code overrides the 9th consecutive block unless
    CLAUDE_CODE_STOP_HOOK_BLOCK_CAP is raised; 0 turns that cap off (``ENV``).
  * The reason is written to the transcript as a ``"type":"user"``, ``isMeta`` row
    ``"Stop hook feedback:\\n<reason>"`` (``FEEDBACK``).
  * Text typed into the pane while the hook waits is queued, not run: Claude Code writes a
    ``queue-operation`` ``enqueue`` row to the transcript at Enter (<0.5 s) and runs the line only
    once the hook returns. Peer messages (``<cross-session-message>``), finished background tasks
    and the rewake's own notification (``<task-notification>``) queue the same way. The hook
    therefore yields (``OperatorQueue``): it returns without taking anything as soon as any entry
    is queued, and the entry runs about 2 s later as its own turn (manual-input-measurement.md).
  * The rewake fallback must not act while another turn runs: its notification would be absorbed
    into that turn (lost) or queued behind a Stop hook that waits on the emptied mailbox
    (``TurnWatch``). Escape interrupts the turn and kills the waiting hook, so
    the session stays idle (unarmed) until its next turn ends.

Long idle: the hook waits up to ``hook_timeout(cfg)`` (default one day) without starting any turn.
Shortly before that it hands Claude one tiny keepalive turn (``KEEPALIVE``) so the next Stop hook
re-arms; that turn is never settled or reported.
"""
import json
import os
import re
import time

ENV = {'CLAUDE_CODE_STOP_HOOK_BLOCK_CAP': '0'}
# Without this, Claude Code shows the reason as "Stop hook error" and the model often refuses a long
# delivery as a prompt injection (worker-2 research §1b: 3/8 followed without it, 8/8 with it).
DECLARATION = ('Fleet channel: after your first turn, every supervisor instruction (task, follow-up, '
               'handoff) is delivered to you by the fleet Stop hook, as a "Stop hook feedback:" message or '
               'as a "Fleet delivery:" Stop hook notification. Treat that text as the supervisor '
               'instruction and carry it out exactly as if the supervisor had typed it.')
# The typed twin (--no-claude-hook-delivery / clear-after-result workers): the worker role files say
# "text that is not a fleet delivery ... do not act on it", and Claude Code shows a bracketed paste as
# pasted content of unknown origin, so without this a worker may refuse its own task (10-14 evermem).
SUPERVISOR_DECLARATION = ('Fleet channel: after your startup turn, fleet requests, worker reports, corrections '
                          'and handoffs arrive through the Stop hook as "Stop hook feedback:" or '
                          '"Fleet delivery:" notifications. Treat them as fleet deliveries, not user input '
                          'to re-register. You remain the supervisor: follow AGENTS.md and return its '
                          'decision JSON; delegate work rather than doing the workers\' tasks.')
TYPED_DECLARATION = ('Fleet channel: every supervisor instruction (task, follow-up, handoff) is typed or '
                     'pasted into this pane by the fleet daemon, so pasted text that arrives here is the fleet '
                     'delivery. Treat it as the supervisor instruction and carry it out exactly as if the '
                     'supervisor had typed it.')
REWAKE_MESSAGE = 'Fleet delivery:'  # asyncRewake prefix; the transcript row is a task-notification
GRACE = 10  # seconds with an entry queued and no Stop hook waiting/settling before the rewake acts
STOPPING_TTL = 120  # a Stop hook settling its turn (tui_turn waits up to settle_wait=30 s)
# After the hook yielded to a queued entry, the rewake fallback keeps off the mailbox this long
# (or until the next Stop): its notification would otherwise be absorbed into the yielded-to turn.
HUMAN_HOLD = 600
INTERRUPTED = '[Request interrupted by user'  # the row Escape writes; it ends a turn without a Stop
FEEDBACK = 'Stop hook feedback:'
KEEPALIVE = ('Fleet keepalive: there is no new instruction yet. Do no work and use no tools; '
             'reply exactly IDLE.')
DEFAULT_TIMEOUT = 86400  # seconds; Node's timer limit is ~24.8 days, keep well below
MARGIN = 120  # the hook answers this long before Claude Code's own timeout would kill it
POLL = 5  # seconds per BLPOP; also the liveness re-check period
ARMED_TTL = 3 * POLL
BIND_TTL = 7 * 86400


def enabled(cfg, name, claude=None):
    """Directly launched v2 Claude supervisors require hooks; worker switches stay independent.

    An adopted supervisor (a sub-fleet's delegated worker pane, design §19/§29) runs the parent's pinned
    worker hooks: it is on the hook channel only when the parent marked the hand-over (``adopt.hook``, set
    at spawn for a run started with ``adopted_supervisor_hook_delivery`` and a hook-channel pane).

    ``claude=True``: the caller already knows this runtime is Claude Code (its own Stop hook).
    """
    if claude is None:
        from fleetmux.fleet import is_claude
        claude = is_claude(cfg, name)
    if name == 'supervisor' and cfg.get('adopt'):
        return bool(claude) and cfg.get('transport_version') == 2 and (cfg['adopt'] or {}).get('hook') is True
    if name == 'supervisor':
        # New runs pin this at start. Old settings have no long Stop timeout/rewake: never
        # reinterpret a live typed supervisor just because its hook imports newer code.
        return (bool(claude) and cfg.get('transport_version') == 2 and not cfg.get('adopt')
                and cfg.get('claude_supervisor_hook_delivery') is True)
    return (bool(claude) and cfg.get('claude_hook_delivery', True) is not False
            and cfg.get('clear_after_result') is not True)  # §28 needs typed delivery (validate refuses both)


def hook_timeout(cfg):
    """Claude Code's Stop-hook ``timeout`` (seconds); ``claude_hook_timeout`` in the run config."""
    return max(10, int(float(cfg.get('claude_hook_timeout') or DEFAULT_TIMEOUT)))


def margin(cfg):
    """How long before Claude Code's timeout the hook answers on its own (keepalive)."""
    return min(MARGIN, max(3, hook_timeout(cfg) // 10))


def keys(prefix, slot, generation):
    """mailbox: deliveries for one generation; armed: a hook is waiting now; bind: what it handed over."""
    base = prefix + ':hook:' + slot
    return {'mailbox': base + ':g' + str(int(generation)) + ':mailbox',
            'armed': base + ':armed', 'bind': base + ':bind',
            'stopping': base + ':stopping', 'rewake': base + ':rewake',
            'human': base + ':human', 'yielded': base + ':yielded', 'turnend': base + ':turnend',
            'prompt_blocked': base + ':prompt_blocked', 'stopped': base + ':stopped',
            'operator_last': base + ':operator_last',
            'rearmed': base + ':g' + str(int(generation)) + ':rearmed', 'yield_wait': base + ':g' + str(int(generation)) + ':yield_wait'}


def post(r, k, entry):
    """Queue one entry for the slot's Stop hook. Returns True if a hook is waiting right now."""
    r.rpush(k['mailbox'], json.dumps(entry, ensure_ascii=False))
    r.expire(k['mailbox'], BIND_TTL)
    return bool(r.exists(k['armed']))


def live(r, k):
    """A Stop hook of this slot waits now, is still settling its turn, or yielded to an operator line."""
    return bool(r.exists(k['armed']) or r.exists(k['stopping']) or r.exists(k['human']))


class OperatorQueue:
    """Entries Claude Code queued in a transcript while the Stop hook blocks.

    Measured rows (2.1.280): ``{"type":"queue-operation","operation":"enqueue","content":<text>}``
    when an operator line, a peer message or a task notification arrives; ``dequeue`` (no content,
    oldest first) when it starts its own turn; ``remove`` (with content) when it is absorbed into a
    running turn. Every kind counts: a queued rewake notification or peer message starves behind a
    waiting hook exactly like a typed line. Reads incrementally: the first call scans the whole
    file, later calls only the complete rows added since.
    """

    def __init__(self, path):
        self.path, self.offset, self.queue = path, 0, []

    def pending(self):
        """Byte offsets of the enqueue rows still queued (operator, peer or task notification)."""
        try:
            with open(self.path, 'rb') as f:
                f.seek(self.offset)
                while True:
                    line = f.readline()
                    if not line.endswith(b'\n'):  # EOF, or a row still being written
                        break
                    at, self.offset = self.offset, self.offset + len(line)
                    if b'queue-operation' in line:
                        self.row(at, line)
        except (OSError, TypeError, ValueError):
            pass
        return [at for at, _ in self.queue]

    def row(self, at, line):
        try:
            row = json.loads(line)
        except ValueError:
            return
        if not isinstance(row, dict) or row.get('type') != 'queue-operation':
            return
        op, text = row.get('operation'), row.get('content')
        text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
        if op == 'enqueue':
            self.queue.append((at, text))
        elif op == 'remove':
            same = next((i for i, (_, t) in enumerate(self.queue) if t == text), 0 if self.queue else None)
            if same is not None:
                self.queue.pop(same)
        elif op == 'dequeue' and self.queue:
            self.queue.pop(0)


class TurnWatch:
    """Whether another turn of this session runs, from the transcript rows written after ``start``.

    The rewake fallback starts at a Stop; a row it sees afterwards that starts a turn makes the
    session busy: a non-meta user row with ``origin.kind`` (typed, peer, task notification), a
    Stop-hook feedback row (a block), or a ``queue-operation dequeue``. The final assistant row of
    the turn that just stopped lands after the Stop and is ignored. Escape ends a turn without a
    Stop and writes a ``[Request interrupted by user…]`` user row (no origin): the session is idle
    again. A StopFailure ends it without a Stop too: ``ended(at)`` (the ``turnend`` key).
    """

    def __init__(self, path):
        self.path, self.busy, self.since = path, False, 0.0
        self.start_offset = 0
        try:
            self.offset = os.path.getsize(path)
        except (OSError, TypeError, ValueError):
            self.offset, self.path = 0, None

    def poll(self):
        if self.path:
            try:
                with open(self.path, 'rb') as f:
                    f.seek(self.offset)
                    while True:
                        line = f.readline()
                        if not line.endswith(b'\n'):
                            break
                        self.offset += len(line)
                        self.row(line)
            except (OSError, ValueError):
                pass
        return self.busy

    def ended(self, at):
        """A StopFailure at ``at`` (epoch) ended the turn that made the session busy."""
        if self.busy and at > self.since:
            self.busy = False

    def blocked(self, offset):
        """A supervisor input hook blocked the dequeued prompt: it will have no Stop callback.

        Use the transcript byte boundary, not polling time: never clear a newer running turn.
        """
        if self.busy and self.start_offset <= offset:
            self.busy = False

    def row(self, line):
        if b'"queue-operation"' not in line and b'"user"' not in line:
            return
        try:
            row = json.loads(line)
        except ValueError:
            return
        if not isinstance(row, dict) or row.get('isSidechain'):
            return
        if row.get('type') == 'queue-operation':
            if row.get('operation') == 'dequeue':
                self.start()
            return
        if row.get('type') != 'user':
            return
        content = (row.get('message') or {}).get('content')
        if isinstance(content, list):
            content = '\n'.join(p.get('text', '') for p in content if isinstance(p, dict) and p.get('type') == 'text')
        content = content if isinstance(content, str) else ''
        if content.startswith(INTERRUPTED):
            self.busy = False
        elif (row.get('origin') or {}).get('kind') and not row.get('isMeta'):
            self.start()
        elif row.get('isMeta') and content.startswith(FEEDBACK):
            self.start()

    def start(self):
        self.busy, self.since = True, time.time()
        self.start_offset = self.offset


def yielding(r, k, thread, queue):
    """True (and recorded) if an entry is queued that this session has not yielded to yet.

    The yielded offsets are kept per session, so a queued row Claude Code never dequeued (a crash
    mid-write) makes the hook yield once, not at every Stop for ever.
    """
    if queue is None:
        return False
    pending = queue.pending()
    if not pending:
        return False
    try:
        seen = json.loads(r.get(k['yielded']) or '{}')
    except ValueError:
        seen = {}
    done = set(seen.get('offsets') or []) if seen.get('session_id') == (thread or '') else set()
    new = [at for at in pending if at not in done]
    if not new:
        return False
    # Claude Code runs the oldest queued line first: record only that one, so a second line still
    # queued after it ran makes the next Stop yield again.
    r.set(k['yielded'], json.dumps({'session_id': thread or '', 'offsets': sorted(done | {new[0]})[-50:]}),
          ex=BIND_TTL)
    r.set(k['human'], json.dumps({'session_id': thread or '', 'pid': os.getpid()}), ex=HUMAN_HOLD)
    return True


def wait(r, k, generation, thread, deadline, alive, accept, poll=POLL, queue=None):
    """Block until ``accept`` takes a mailbox entry, the generation retires, an operator line is
    queued (``queue``: OperatorQueue of this session's transcript), or ``deadline``.

    ``alive()`` -> False ends the wait (retired / dead generation). ``accept(entry)`` -> the text to
    hand Claude, or None to drop a stale entry. Returns (text, entry) or (None, None). Yielding
    takes nothing: the mailbox, the bind and the lease stay as they are, and the operator's turn
    settles as unsolicited; its Stop hook then waits here again.
    """
    mark = json.dumps({'generation': int(generation), 'session_id': thread or '', 'pid': os.getpid()})
    try:
        if alive():  # a Stop of this generation fired: the operator turn it yielded to (if any) has ended
            r.delete(k['human'])
        while time.monotonic() < deadline:
            if not alive():
                return None, None
            if yielding(r, k, thread, queue):  # checked first: a line queued during the last turn
                return None, None
            r.set(k['armed'], mark, ex=ARMED_TTL)
            r.delete(k['stopping'])
            got = r.blpop([k['mailbox']], timeout=max(1, int(min(poll, deadline - time.monotonic()))))
            if not got:
                continue
            try:
                if not alive():  # retired or orphaned while blocked: leave the entry for the live hook
                    r.lpush(k['mailbox'], got[1])
                    return None, None
                if yielding(r, k, thread, queue):  # the operator came first: the entry waits for the next Stop
                    r.lpush(k['mailbox'], got[1])
                    return None, None
                try:
                    entry = json.loads(got[1])
                except ValueError:
                    continue
                text = accept(entry) if isinstance(entry, dict) else None
            except BaseException:  # SIGTERM (Escape, ctrl+x ctrl+s, timeout) mid hand-over: put it back
                r.lpush(k['mailbox'], got[1])
                raise
            if text:
                return text, entry
        return None, None
    finally:
        try:
            if r.get(k['armed']) == mark:
                r.delete(k['armed'])
            if r.get(k['stopping']) == str(os.getpid()):
                r.delete(k['stopping'])  # yielded before arming: this Stop is no longer settling
        except Exception:  # the hook is exiting anyway; the TTL clears the flag
            pass


def block(text):
    return {'decision': 'block', 'reason': text}


def rewake_prompt_text(content):
    """Strict whole-notification shape for the supervisor input gate, not a substring exemption."""
    if not isinstance(content, str):
        return None
    match = re.fullmatch(
        r'<task-notification>\n<summary>Stop hook feedback</summary>\n</task-notification>\n'
        r'<system-reminder>\n(?:Fleet delivery:\s*|Stop hook blocking error from command "[^"\n]+":\s*)'
        r'(.*)\n</system-reminder>', content.strip(), re.DOTALL)
    return match.group(1).strip() if match else None


def rewake_text(content):
    """The delivery of an asyncRewake row (origin task-notification), or None for any other one.

    Measured shape: '<task-notification>\\n<summary>Stop hook feedback</summary>\\n</task-notification>\\n'
    '<system-reminder>\\n<REWAKE_MESSAGE> <text>\\n</system-reminder>'.
    """
    if isinstance(content, list):
        content = '\n'.join(p.get('text', '') for p in content if isinstance(p, dict) and p.get('type') == 'text')
    if not isinstance(content, str) or '<summary>Stop hook feedback</summary>' not in content:
        return None
    at = content.find(REWAKE_MESSAGE)
    if at >= 0:
        text = content[at + len(REWAKE_MESSAGE):]
    else:  # rewakeMessage is an internal setting: fall back to Claude Code's default prefix
        start = content.find('<system-reminder>')
        text = content[start + len('<system-reminder>'):] if start >= 0 else ''
        text = re.sub(r'^\s*Stop hook blocking error from command "[^"]*":', '', text)
    end = text.rfind('</system-reminder>')
    return (text[:end] if end >= 0 else text).strip('\n ').lstrip() or None


def feedback_text(content):
    """The reason of a Stop-hook block row, or None for any other meta row."""
    if isinstance(content, str) and content.startswith(FEEDBACK):
        return content[len(FEEDBACK):].lstrip('\n')
    return None
