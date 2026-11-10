"""Minimal stand-in for the official Claude Code / Codex TUIs (M4 end-to-end test fixture, e2e-1 plan r3 §3.1).

Started by the native shim (fleetmux/crates/probe/examples/fake_cli_shim.rs, copied to ``<root>/bin/claude`` and
``<root>/bin/codex``) as ``python -I fake_cli.py --as claude|codex --log-dir DIR <the CLI's own argv>``, on the
pane's real PTY. Standard library only; it never imports cmux_fleet and never calls a model.

Claude mode (the contract of cmux_fleet/claude_input.py, claude_hook.py and fleet.claude_argv):
- draws the ruled editor ``visible_editor`` accepts: a top rule carrying the session name, ``❯ <draft>``, a bottom
  rule and a whitelisted footer (``? for shortcuts`` idle, ``esc to interrupt`` busy, with a busy status line
  above the top rule); a multi-line or wide paste shows as ``[Pasted text #N +M lines]``;
- writes the transcript ``$HOME/.claude/projects/<cwd slug>/<session>.jsonl`` (user rows: argv prompt without
  origin, typed rows ``origin.kind = human``, block rows ``isMeta`` ``Stop hook feedback:``, rewake rows
  ``origin.kind = task-notification``, ``queue-operation`` enqueue / dequeue; one assistant row per turn);
- runs the ``--settings`` hooks: ``Stop`` (blocking hooks as direct children it waits for, asyncRewake hooks in
  the background; exit 2 of a rewake = next turn), ``SessionStart`` matcher ``clear`` after a raw ``/clear``;
  the settings ``env`` is the hooks' environment, ``CLAUDE_CODE_STOP_HOOK_BLOCK_CAP=0`` lifts the block cap;
- never calls a tool (no PostToolUse), never fails a turn (no StopFailure), never draws a dialog or a wall text.

Codex mode (codex_queue.py, fleet.completed_turn / notify / actor_argv):
- ``codex --version`` -> ``codex-cli 0.162.0``;
- ``codex queue --thread T --message=X`` appends to ``$CODEX_HOME/m4e-queue/T.jsonl`` (private to this fake) and
  prints ``Queued message <id> for thread T.``;
- the TUI writes ``$CODEX_HOME/sessions/Y/M/D/rollout-…-<thread>.jsonl`` like codex-cli 0.162.1
  (tests/fixtures/codex_queue_versions): ``task_started`` with ``turn_attribution`` (``queue`` for a queued
  item), one ``item_completed`` UserMessage, ``task_complete``; then runs the ``-c notify=[…]`` callback with the
  ``agent-turn-complete`` JSON as its last argument.

What it answers (both modes): READY when told to; ``{"<key>": "E2E-OK <rid>"}`` for a worker request; a service
decision for a routing message or an external sub-fleet up / down (approve); a filled three-section note for a
``[SYSTEM HANDOFF]``; an idle service decision on any other supervisor message; READY otherwise.
"""
import codecs
import datetime
import fcntl
import json
import os
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time
import tty
import unicodedata
import uuid
from pathlib import Path

CODEX_VERSION = 'codex-cli 0.162.0'
CLAUDE_VERSION = '2.1.280 (Claude Code)'
TURN_S = 0.3  # a turn "works" this long, so the busy footer is observable
SUPERVISOR_MARK = '## 4. Your role (supervisor)'  # subfleet.start_adopted appends it to an adopted slot's AGENTS.md
IDLE_DECISION = {'status': 'RUNNING', 'dispatch': [], 'skip': []}
THREAD = re.compile(r'[0-9a-f-]{36}\Z')
LOG = {'path': None}


def log(event, **fields):
    if not LOG['path']:
        return
    rec = dict(event=event, at=time.time(), pid=os.getpid(), **fields)
    try:
        with open(LOG['path'], 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except OSError:
        pass


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def width_of(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in text)


def clip(text, cols):
    out, used = '', 0
    for c in text:
        w = width_of(c)
        if used + w > cols:
            break
        out, used = out + c, used + w
    return out


def plain(text):
    """One harmless echo line: first line, no control characters, no rule-like dash runs."""
    first = (text or '').split('\n', 1)[0]
    first = ''.join(c for c in first if not unicodedata.category(c).startswith('C'))
    return re.sub(r'[─━-]{4,}', '--', first)


# ----------------------------------------------------------------------------------------------- the script
def payload_rid(text):
    """``m4e_rid`` of the request's ``Payload (JSON):`` object (fleet.request_message), or None."""
    at = text.find('Payload (JSON):\n')
    if at < 0:
        return None
    try:
        payload, _ = json.JSONDecoder().raw_decode(text, at + len('Payload (JSON):\n'))
    except ValueError:
        return None
    rid = payload.get('m4e_rid') if isinstance(payload, dict) else None
    return rid if isinstance(rid, str) and re.fullmatch(r'\S+', rid) else None


def answer(text, supervisor):
    """What this fake replies to one turn's input."""
    if '[SYSTEM HANDOFF]' in text:
        m = re.search(r'edit this file: (\S+) \.', text)
        if m:
            note = Path(m.group(1))
            try:
                note.write_text('## Done (facts)\n- e2e fake CLI: no task work was done in this session\n\n'
                                '## In progress\nnone\n\n'
                                '## Next steps (in order)\n- continue as this slot describes in AGENTS.md\n',
                                encoding='utf-8')
                log('handoff.written', path=str(note))
            except OSError as exc:
                log('handoff.failed', path=str(note), error=str(exc))
        return 'handoff written'
    # v2 (RuntimeV2.queue) delivers texts without the leading `[Request <id>]`: the id is transport metadata.
    # The approval names the request id and the sub-fleet in its body, so the header is optional here.
    m = re.search(r'(?:^|\[Request \S+\] )External request (\S+) wants sub-fleet (\S+) ', text, re.M)
    if m:
        return json.dumps(dict(IDLE_DECISION, subfleet={'id': m.group(1), 'decision': 'approve',
                                                        'note': 'e2e fake supervisor approves ' + m.group(2)}))
    m = re.search(r'\[Request (\S+)\]', text)
    keys_at = text.find('one JSON object with the keys: ')
    if keys_at >= 0:
        rid = m.group(1) if m else payload_rid(text)  # the header's id, else the payload's m4e_rid
        if not rid:  # never an E2E-OK object without a known id: the product must see this turn fail
            return 'm4e fake CLI: no request id (no [Request <id>] header and no "m4e_rid" in the payload)'
        rest = text[keys_at + len('one JSON object with the keys: '):].split('\n', 1)[0]
        rest = rest.split(' (allowed keys:', 1)[0].split('. No text', 1)[0].rstrip('.')
        keys = [k.strip() for k in rest.split(',') if k.strip()]
        return json.dumps({k: 'E2E-OK ' + rid for k in keys}, ensure_ascii=False)
    workers = re.search(r'^Workers: (.+)$', text, re.M)
    if m and 'needs routing' in text and workers:
        to = workers.group(1).split(',')[0].strip()
        return json.dumps(dict(IDLE_DECISION, dispatch=[{
            'to': to, 'request_id': m.group(1),
            'instruction': 'Handle request ' + m.group(1) + ' as your role describes.'}]))
    if re.search(r'exactly READY', text):
        return 'READY'
    if supervisor:
        return json.dumps(IDLE_DECISION)
    return 'READY'


# ----------------------------------------------------------------------------------------------- terminal
class Term:
    """Raw-mode PTY input (bracketed paste, Enter, Ctrl+C, Escape) and whole-screen redraws."""

    def __init__(self, alt=True):
        self.fd = 0
        self.saved = None
        self.alt = alt
        self.dec = codecs.getincrementaldecoder('utf-8')('replace')
        self.buf = ''
        self.pasting = None
        if os.isatty(self.fd):
            self.saved = termios.tcgetattr(self.fd)
            tty.setraw(self.fd)
        self.out('\x1b[?1049h' if alt else '', '\x1b[?2004h\x1b[2J')

    def size(self):
        try:
            rows, cols = struct.unpack('hhhh', fcntl.ioctl(1, termios.TIOCGWINSZ, b'\0' * 8))[:2]
            if rows > 0 and cols > 0:
                return rows, cols
        except OSError:
            pass
        return 24, 80

    @staticmethod
    def out(*parts):
        try:
            os.write(1, ''.join(parts).encode('utf-8'))
        except OSError:
            pass

    def close(self):
        self.out('\x1b[?2004l', '\x1b[?1049l' if self.alt else '')
        if self.saved is not None:
            try:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
            except termios.error:
                pass

    def draw(self, lines, cursor):
        rows, cols = self.size()
        parts = ['\x1b[?25l']
        for i in range(rows):
            parts.append('\x1b[%d;1H%s\x1b[K' % (i + 1, clip(lines[i], cols) if i < len(lines) else ''))
        parts.append('\x1b[%d;%dH\x1b[?25h' % cursor)
        self.out(*parts)

    def events(self, data):
        """Decode PTY bytes into ('paste', text) / ('key', name) / ('char', c) events."""
        self.buf += self.dec.decode(data)
        out = []
        while self.buf:
            if self.pasting is not None:
                end = self.buf.find('\x1b[201~')
                if end < 0:
                    self.pasting += self.buf
                    self.buf = ''
                    break
                out.append(('paste', self.pasting + self.buf[:end]))
                self.pasting, self.buf = None, self.buf[end + 6:]
                continue
            c = self.buf[0]
            if c == '\x1b':
                if self.buf.startswith('\x1b[200~'):
                    self.pasting, self.buf = '', self.buf[6:]
                    continue
                m = re.match(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1bO.|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)', self.buf)
                if m:
                    self.buf = self.buf[m.end():]
                    continue
                if len(self.buf) == 1 or self.buf[1] not in '[O]':
                    out.append(('key', 'Escape'))
                    self.buf = self.buf[1:]
                    continue
                break  # an incomplete sequence: wait for the rest
            self.buf = self.buf[1:]
            if c == '\r':
                out.append(('key', 'Enter'))
            elif c == '\x03':
                out.append(('key', 'C-c'))
            elif c == '\x15':
                out.append(('key', 'C-u'))
            elif c in '\x7f\x08':
                out.append(('key', 'BSpace'))
            elif c == '\n' or not unicodedata.category(c).startswith('C'):
                out.append(('char', c))
        return out


class App:
    """The shared loop: PTY input, signals, child hooks, timers."""

    def __init__(self):
        self.quit = False
        self.wake_r, self.wake_w = os.pipe()
        os.set_blocking(self.wake_w, False)
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sig, self.on_signal)
        signal.signal(signal.SIGWINCH, lambda *_: self.poke())
        self.dirty = True
        self.draft, self.collapsed, self.pastes = '', '', 0
        self.last_in, self.last_out = '', ''

    def on_signal(self, sig, _frame):
        log('signal', sig=sig)
        self.quit = True
        self.poke()

    def poke(self):
        self.dirty = True
        try:
            os.write(self.wake_w, b'x')
        except OSError:
            pass

    def loop(self, term):
        while not self.quit:
            if self.dirty:
                self.dirty = False
                self.render(term)
            try:
                ready, _, _ = select.select([0, self.wake_r], [], [], 0.1)
            except InterruptedError:
                ready = []
            if self.wake_r in ready:
                try:
                    os.read(self.wake_r, 4096)
                except OSError:
                    pass
            if 0 in ready:
                try:
                    data = os.read(0, 65536)
                except OSError:
                    data = b''
                if not data:  # the pane's PTY is gone
                    log('pty.eof')
                    self.quit = True
                    break
                for kind, value in term.events(data):
                    self.on_input(kind, value)
                self.dirty = True
            self.tick()

    def on_input(self, kind, value):
        if kind == 'paste':
            self.draft += value
            rows, cols = self.term.size()
            if '\n' in value or any(width_of(line) >= cols - 3 for line in value.split('\n')):
                self.pastes += 1
                self.collapsed = '[Pasted text #%d +%d lines]' % (self.pastes, value.count('\n') + 1)
            log('input.paste', chars=len(value), collapsed=self.collapsed)
        elif kind == 'char':
            self.draft += value
        elif value == 'BSpace':
            self.draft = self.draft[:-1]
            self.collapsed = '' if not self.draft else self.collapsed
        elif value in ('C-c', 'C-u'):
            self.draft, self.collapsed = '', ''
        elif value == 'Escape':
            self.escape()
        elif value == 'Enter' and self.draft:
            text, self.draft, self.collapsed = self.draft, '', ''
            log('input.enter', text=text[:300])
            self.submit(text)

    def editor_body(self):
        if self.collapsed:
            return ['❯ ' + self.collapsed]
        lines = self.draft.split('\n')
        return [('❯ ' + lines[0]) if lines[0] else '❯'] + ['  ' + line for line in lines[1:]]


# ----------------------------------------------------------------------------------------------- Claude
CLAUDE_VALUE = {'--settings', '--name', '--model', '--append-system-prompt', '--disallowedTools', '--setting-sources',
                '--resume', '--effort', '--system-prompt-file', '--tools', '--session-id'}
CLAUDE_MULTI = {'--add-dir', '--mcp-config'}
CLAUDE_FLAG = {'--dangerously-skip-permissions', '--strict-mcp-config', '--disable-slash-commands'}


def parse_claude(argv):
    opts, prompt, i = {}, None, 0
    while i < len(argv):
        a = argv[i]
        if a == '--':
            prompt = ' '.join(argv[i + 1:])
            break
        if a in CLAUDE_FLAG:
            opts[a] = True
        elif a in CLAUDE_VALUE:
            if i + 1 >= len(argv):
                raise ValueError(a + ' needs a value')
            opts[a] = argv[i + 1]
            i += 1
        elif a in CLAUDE_MULTI:
            vals = []
            while i + 1 < len(argv) and not argv[i + 1].startswith('-'):
                vals.append(argv[i + 1])
                i += 1
            opts.setdefault(a, []).extend(vals)
        elif not a.startswith('-') and prompt is None and i == len(argv) - 1:
            prompt = a
        else:
            raise ValueError('unknown option ' + a)
        i += 1
    return opts, prompt


class Claude(App):
    def __init__(self, argv):
        super().__init__()
        self.opts, prompt = parse_claude(argv)
        self.cwd = os.getcwd()
        self.settings = {}
        if self.opts.get('--settings'):
            self.settings = json.loads(Path(self.opts['--settings']).read_text(encoding='utf-8'))
        self.name = re.sub(r'\s+', '-', self.opts.get('--name') or 'claude')[:40]
        self.state = 'idle'  # idle / working / hooks / clearing
        self.blocking = []  # [(proc, started, timeout, event)]
        self.rewakes = []  # [(proc, err path, message)]
        self.pending = []  # turns waiting for an idle session: (kind, text, content)
        self.blocks = 0
        self.turn = None
        self.reply_at = 0.0
        self.new_session()
        log('claude.start', argv=argv, cwd=self.cwd, session=self.session, name=self.name)
        if prompt:
            self.start('argv', prompt)

    # -- transcript
    def new_session(self):
        self.session = str(uuid.uuid4())
        slug = re.sub(r'[^A-Za-z0-9]', '-', self.cwd)
        self.transcript = Path.home() / '.claude' / 'projects' / slug / (self.session + '.jsonl')
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        self.transcript.touch()

    def row(self, **row):
        base = {'uuid': str(uuid.uuid4()), 'sessionId': self.session, 'cwd': self.cwd, 'timestamp': stamp(),
                'isSidechain': False}
        with self.transcript.open('a', encoding='utf-8') as f:
            f.write(json.dumps(dict(base, **row), ensure_ascii=False) + '\n')

    def user_row(self, content, origin=None, meta=False):
        row = {'type': 'user', 'message': {'role': 'user', 'content': content}}
        if origin:
            row['origin'] = {'kind': origin}
        if meta:
            row['isMeta'] = True
        self.row(**row)

    # -- turns
    def supervisor(self):
        try:
            return SUPERVISOR_MARK in (Path(self.cwd) / 'AGENTS.md').read_text(encoding='utf-8')
        except OSError:
            return False

    def start(self, kind, text, content=None):
        """Begin a turn now (the session is idle): write the row that starts it, then work."""
        if kind == 'argv':
            self.user_row(text)
        elif kind == 'typed':
            self.user_row(text, origin='human')
        elif kind == 'dequeue':
            self.row(type='queue-operation', operation='dequeue')
            self.user_row(text, origin='human')
        elif kind == 'block':
            self.user_row('Stop hook feedback:\n' + text, meta=True)
        elif kind == 'rewake':
            self.user_row(content, origin='task-notification')
        self.blocks = self.blocks + 1 if kind == 'block' else 0
        self.turn = {'kind': kind, 'text': text}
        self.state, self.reply_at = 'working', time.monotonic() + TURN_S
        self.last_in = text
        log('turn.start', kind=kind, text=text[:300])
        self.poke()

    def finish_turn(self):
        reply = answer(self.turn['text'], self.supervisor())
        self.last_out = reply
        self.row(type='assistant', message={'role': 'assistant', 'model': self.opts.get('--model', ''),
                                            'content': [{'type': 'text', 'text': reply}]})
        log('turn.reply', reply=reply[:300])
        payload = {'session_id': self.session, 'transcript_path': str(self.transcript), 'cwd': self.cwd,
                   'permission_mode': 'bypassPermissions', 'hook_event_name': 'Stop',
                   'stop_hook_active': self.turn['kind'] == 'block', 'last_assistant_message': reply}
        self.turn = None
        self.state = 'hooks'
        for entry in (self.settings.get('hooks') or {}).get('Stop') or []:
            for hook in entry.get('hooks') or []:
                if hook.get('type') != 'command':
                    continue
                if hook.get('asyncRewake'):
                    err = Path(LOG['path'] or os.devnull).parent / ('rewake-%d-%s.err' % (os.getpid(), uuid.uuid4().hex[:8]))
                    proc = self.hook(hook['command'], payload, stdout=subprocess.DEVNULL, stderr=err)
                    if proc:
                        self.rewakes.append((proc, err, hook.get('rewakeMessage') or 'Stop hook feedback:'))
                else:
                    proc = self.hook(hook['command'], payload, stdout=subprocess.PIPE)
                    if proc:
                        self.blocking.append((proc, time.monotonic(), float(hook.get('timeout') or 600), 'Stop'))
        if not self.blocking:
            self.state = 'idle'
        self.poke()

    def hook(self, command, payload, stdout, stderr=None):
        env = dict(os.environ, **{k: str(v) for k, v in (self.settings.get('env') or {}).items()})
        env['CLAUDE_PROJECT_DIR'] = self.cwd
        err = open(stderr, 'w') if stderr else open(LOG['path'] + '.hooks', 'a') if LOG['path'] else subprocess.DEVNULL
        try:
            # A direct child in this process group: never detached, so it sees this process as its parent.
            proc = subprocess.Popen(['/bin/sh', '-c', command], stdin=subprocess.PIPE, stdout=stdout, stderr=err,
                                    cwd=self.cwd, env=env)
            proc.stdin.write(json.dumps(payload).encode())
            proc.stdin.close()
        except OSError as exc:
            log('hook.failed', command=command[:200], error=str(exc))
            return None
        finally:
            if err is not subprocess.DEVNULL:
                err.close()
        log('hook.spawn', hook=payload['hook_event_name'], hook_pid=proc.pid, command=command[:300])
        return proc

    def tick(self):
        now = time.monotonic()
        if self.state == 'working' and self.turn and now >= self.reply_at:
            self.finish_turn()
        if self.blocking:
            for proc, started, timeout, event in list(self.blocking):
                if proc.poll() is None and now - started > timeout:
                    proc.terminate()
                    log('hook.timeout', hook_pid=proc.pid)
            done = [b for b in self.blocking if b[0].poll() is not None]
            if done and len(done) == len(self.blocking):
                self.hooks_done(done)
        for item in list(self.rewakes):
            proc, err, message = item
            if proc.poll() is None:
                continue
            self.rewakes.remove(item)
            text = ''
            try:
                text = Path(err).read_text(encoding='utf-8', errors='replace').strip()
            except OSError:
                pass
            log('rewake.exit', hook_pid=proc.pid, code=proc.returncode, text=text[:300])
            if proc.returncode == 2 and text:
                content = ('<task-notification>\n<summary>Stop hook feedback</summary>\n</task-notification>\n'
                           '<system-reminder>\n' + message + ' ' + text + '\n</system-reminder>')
                self.later('rewake', text, content)
        if self.state == 'idle' and self.pending:
            kind, text, content = self.pending.pop(0)
            if kind == 'clear':
                self.clear()
            else:
                self.start(kind, text, content)

    def later(self, kind, text, content=None):
        """A turn for when the session is idle; while a Stop hook waits it is queued like Claude Code does."""
        if self.state == 'idle' and not self.pending:
            if kind == 'clear':
                self.clear()
            else:
                self.start(kind, text, content)
            return
        if kind in ('typed', 'rewake'):
            self.row(type='queue-operation', operation='enqueue', content=content or text)
        self.pending.append(('dequeue' if kind == 'typed' else kind, text, content))
        log('turn.queued', kind=kind, text=text[:200])

    def hooks_done(self, done):
        reasons = []
        for proc, _, _, event in done:
            out = b''
            if proc.stdout:
                try:
                    out = proc.stdout.read() or b''
                except OSError:
                    pass
            log('hook.exit', hook=event, hook_pid=proc.pid, code=proc.returncode, out=out[:300].decode('utf-8', 'replace'))
            try:
                decision = json.loads(out.decode('utf-8', 'replace') or '{}')
            except ValueError:
                decision = {}
            if isinstance(decision, dict) and decision.get('decision') == 'block' and decision.get('reason'):
                reasons.append(decision['reason'])
        self.blocking = []
        cap = int((self.settings.get('env') or {}).get('CLAUDE_CODE_STOP_HOOK_BLOCK_CAP', os.environ.get(
            'CLAUDE_CODE_STOP_HOOK_BLOCK_CAP', '8')) or 0)
        self.state = 'idle'  # drawn idle at once: start_adopted types right after a released hook (plan F21)
        if reasons and (cap <= 0 or self.blocks < cap):
            self.pending.insert(0, ('block', '\n'.join(reasons), None))
        self.poke()

    def submit(self, text):
        if text.strip() == '/clear':
            self.later('clear', text)
        else:
            self.later('typed', text)

    def escape(self):
        if self.state in ('working', 'hooks'):
            for proc, _, _, _ in self.blocking:
                if proc.poll() is None:
                    proc.terminate()
            self.blocking, self.turn = [], None
            self.user_row('[Request interrupted by user]')
            self.state = 'idle'
            log('turn.interrupted')

    def clear(self):
        old = self.session
        self.new_session()
        log('clear', old=old, new=self.session, transcript=str(self.transcript))
        payload = {'session_id': self.session, 'transcript_path': str(self.transcript), 'cwd': self.cwd,
                   'hook_event_name': 'SessionStart', 'source': 'clear'}
        self.state = 'clearing'
        for entry in (self.settings.get('hooks') or {}).get('SessionStart') or []:
            if entry.get('matcher') not in (None, '', 'clear'):
                continue
            for hook in entry.get('hooks') or []:
                if hook.get('type') == 'command':
                    proc = self.hook(hook['command'], payload, stdout=subprocess.PIPE)
                    if proc:
                        self.blocking.append((proc, time.monotonic(), float(hook.get('timeout') or 60), 'SessionStart'))
        if not self.blocking:
            self.state = 'idle'
        self.last_in, self.last_out = '/clear', ''

    def render(self, term):
        rows, cols = term.size()
        lines = [''] * rows
        if rows >= 18:
            lines[0] = 'fake Claude Code (m4e test fixture) ' + self.name
            lines[1] = '> ' + plain(self.last_in)
            lines[2] = '⏺ ' + plain(self.last_out)
        body = self.editor_body()[:max(1, rows - 6)]
        footer = '  ? for shortcuts' if self.state == 'idle' else '  esc to interrupt'
        status = {'idle': '', 'working': '✻ Working… (esc to interrupt)',
                  'hooks': '✻ running stop hooks… (esc to interrupt)',
                  'clearing': '✻ running SessionStart hooks…'}[self.state]
        name = clip(self.name, max(1, cols - 14))
        top = '─' * 10 + ' ' + name + ' ' + '─' * max(1, cols - 12 - width_of(name))
        block = [status, top] + body + ['─' * cols, footer]
        start = max(0, rows - len(block))
        for i, line in enumerate(block):
            if start + i < rows:
                lines[start + i] = line
        cursor_row = start + 2 + len(body) - 1
        term.draw(lines, (cursor_row + 1, min(cols, width_of(body[-1]) + 1)))

    def shutdown(self):
        for proc, _, _, _ in self.blocking:
            if proc.poll() is None:
                proc.terminate()
        deadline = time.monotonic() + 5
        for proc, _, _, _ in self.blocking:
            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                proc.kill()
        # Background rewake hooks are left alone: they see their parent change and exit by themselves (plan F21).
        log('claude.exit', rewakes_running=[p.pid for p, _, _ in self.rewakes if p.poll() is None])


# ----------------------------------------------------------------------------------------------- Codex
def codex_queue_cmd(argv):
    thread, message = None, None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == '--thread' and i + 1 < len(argv):
            thread, i = argv[i + 1], i + 1
        elif a.startswith('--message='):
            message = a[len('--message='):]
        else:
            print('codex queue: unknown argument ' + a, file=sys.stderr)
            return 2
        i += 1
    home = os.environ.get('CODEX_HOME')
    if not home or not thread or not THREAD.match(thread) or message is None:
        print('codex queue: need CODEX_HOME, --thread <uuid> and --message=<text>', file=sys.stderr)
        return 2
    qid = str(uuid.uuid4())
    path = Path(home) / 'm4e-queue' / (thread + '.jsonl')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps({'id': qid, 'text': message, 'at': time.time()}, ensure_ascii=False) + '\n')
    log('codex.queue', thread=thread, queued=qid, text=message[:300])
    print('Queued message %s for thread %s.' % (qid, thread))
    return 0


def parse_codex(argv):
    opts, configs, prompt, i = {}, [], None, 0
    while i < len(argv):
        a = argv[i]
        if a in ('--profile', '--sandbox', '--model', '-c'):
            if i + 1 >= len(argv):
                raise ValueError(a + ' needs a value')
            if a == '-c':
                configs.append(argv[i + 1])
            else:
                opts[a] = argv[i + 1]
            i += 1
        elif a == '--no-alt-screen':
            opts[a] = True
        elif not a.startswith('-') and i == len(argv) - 1:
            prompt = a
        else:
            raise ValueError('unknown option ' + a)
        i += 1
    return opts, configs, prompt


class Codex(App):
    def __init__(self, argv):
        super().__init__()
        self.opts, configs, prompt = parse_codex(argv)
        self.notify = None
        for item in configs:
            if item.startswith('notify='):
                self.notify = json.loads(item[len('notify='):])
        home = os.environ.get('CODEX_HOME')
        if not home:
            raise ValueError('CODEX_HOME is not set')
        self.home = Path(home)
        self.cwd = os.path.realpath(os.getcwd())
        self.thread = str(uuid.uuid4())
        day = datetime.datetime.now()
        self.rollout = (self.home / 'sessions' / day.strftime('%Y') / day.strftime('%m') / day.strftime('%d')
                        / ('rollout-' + day.strftime('%Y-%m-%dT%H-%M-%S') + '-' + self.thread + '.jsonl'))
        self.rollout.parent.mkdir(parents=True, exist_ok=True)
        self.write({'type': 'session_meta', 'payload': {
            'id': self.thread, 'session_id': self.thread, 'timestamp': stamp(), 'cwd': self.cwd,
            'cli_version': CODEX_VERSION.split()[-1], 'originator': 'codex-tui', 'source': 'cli'}})
        self.queue = self.home / 'm4e-queue' / (self.thread + '.jsonl')
        self.offset = 0
        self.inputs, self.pending, self.turn, self.reply_at = [], [], None, 0.0
        self.callbacks = []
        log('codex.start', argv=argv, cwd=self.cwd, thread=self.thread, rollout=str(self.rollout))
        if prompt:
            self.pending.append(('user', prompt))

    def write(self, row):
        row = dict({'timestamp': stamp()}, **row)
        with self.rollout.open('a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')

    def tick(self):
        now = time.monotonic()
        try:
            with self.queue.open('r', encoding='utf-8') as f:
                f.seek(self.offset)
                while True:
                    line = f.readline()
                    if not line.endswith('\n'):
                        break
                    self.offset += len(line.encode('utf-8'))
                    self.pending.append(('queue', json.loads(line)['text']))
        except (OSError, ValueError):
            pass
        if self.turn and now >= self.reply_at:
            self.finish_turn()
        if not self.turn and self.pending:
            trigger, text = self.pending.pop(0)
            self.start(trigger, text)
        self.callbacks = [p for p in self.callbacks if p.poll() is None]

    def start(self, trigger, text):
        turn = str(uuid.uuid4())
        self.write({'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_attribution': {
            'turn_id': turn, 'turn_trigger': trigger, 'parent_turn_id': None, 'initiating_agent_path': None,
            'root_turn_id': turn}, 'turn_id': turn, 'root_turn_id': turn}})
        self.write({'type': 'event_msg', 'payload': {'type': 'item_completed', 'thread_id': self.thread, 'turn_id': turn,
                                                     'item': {'type': 'UserMessage', 'id': str(uuid.uuid4()),
                                                              'content': [{'type': 'text', 'text': text}]}}})
        self.inputs.append(text)
        self.turn, self.reply_at = {'id': turn, 'text': text, 'trigger': trigger}, time.monotonic() + TURN_S
        self.last_in = text
        log('turn.start', trigger=trigger, turn=turn, text=text[:300])
        self.poke()

    def finish_turn(self):
        turn = self.turn
        reply = answer(turn['text'], True)
        self.last_out = reply
        self.write({'type': 'event_msg', 'payload': {'type': 'item_completed', 'thread_id': self.thread,
                                                     'turn_id': turn['id'], 'item': {
                                                         'type': 'AgentMessage', 'id': 'msg_' + uuid.uuid4().hex,
                                                         'content': [{'type': 'Text', 'text': reply}],
                                                         'phase': 'final_answer'}}})
        self.write({'type': 'event_msg', 'payload': {'type': 'task_complete', 'root_turn_id': turn['id'],
                                                     'turn_id': turn['id'], 'last_agent_message': reply}})
        self.turn = None
        log('turn.reply', turn=turn['id'], reply=reply[:300])
        if self.notify:
            data = {'type': 'agent-turn-complete', 'thread-id': self.thread, 'turn-id': turn['id'], 'cwd': self.cwd,
                    'client': 'codex-tui', 'input-messages': list(self.inputs), 'last-assistant-message': reply}
            sink = open(LOG['path'] + '.notify', 'a') if LOG['path'] else subprocess.DEVNULL
            try:
                self.callbacks.append(subprocess.Popen(list(self.notify) + [json.dumps(data, ensure_ascii=False)],
                                                       stdin=subprocess.DEVNULL, stdout=sink, stderr=sink, cwd=self.cwd))
            except OSError as exc:
                log('notify.failed', error=str(exc))
            finally:
                if sink is not subprocess.DEVNULL:
                    sink.close()
        self.poke()

    def submit(self, text):
        self.pending.append(('user', text))

    def escape(self):
        pass

    def render(self, term):
        rows, cols = term.size()
        lines = [''] * rows
        lines[0] = 'fake Codex (m4e test fixture) ' + CODEX_VERSION
        if rows >= 8:
            lines[1] = '› ' + plain(self.last_in)
            lines[2] = '• ' + plain(self.last_out)
        body = self.editor_body()
        status = '• Working (esc to interrupt)' if self.turn else ''
        block = [status] + ['› ' + b[2:] if b.startswith('❯') else b for b in body] + ['', '  ⏎ send']
        start = max(0, rows - len(block))
        for i, line in enumerate(block):
            if start + i < rows:
                lines[start + i] = line
        term.draw(lines, (start + 1 + len(body), 3))

    def shutdown(self):
        log('codex.exit', callbacks_running=[p.pid for p in self.callbacks if p.poll() is None])


# ----------------------------------------------------------------------------------------------- main
def main(argv):
    if len(argv) < 4 or argv[0] != '--as' or argv[2] != '--log-dir':
        print('usage: fake_cli.py --as claude|codex --log-dir DIR [argv...]', file=sys.stderr)
        return 64
    kind, log_dir, rest = argv[1], argv[3], argv[4:]
    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        LOG['path'] = str(Path(log_dir) / ('%s-%d.jsonl' % (kind, os.getpid())))
    except OSError:
        LOG['path'] = None
    log('exec', argv=rest, cwd=os.getcwd(), home=os.environ.get('HOME'), ppid=os.getppid(),
        pgid=os.getpgrp(), tty=os.ttyname(0) if os.isatty(0) else '')
    if rest == ['--version']:
        print(CODEX_VERSION if kind == 'codex' else CLAUDE_VERSION)
        return 0
    if kind == 'codex' and rest[:1] == ['queue']:
        return codex_queue_cmd(rest[1:])
    try:
        app = Codex(rest) if kind == 'codex' else Claude(rest) if kind == 'claude' else None
    except (ValueError, OSError) as exc:
        log('usage', error=str(exc))
        print('fake %s: %s' % (kind, exc), file=sys.stderr)
        return 64
    if app is None:
        print('fake CLI: unknown kind ' + kind, file=sys.stderr)
        return 64
    term = Term(alt=kind == 'claude')
    app.term = term
    try:
        app.loop(term)
    except Exception:
        import traceback
        log('crash', traceback=traceback.format_exc())
        raise
    finally:
        app.shutdown()
        term.close()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
