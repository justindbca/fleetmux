"""Scripted actor of `fleetmux demo`: stands in for the Claude Code CLI in the demo's panes. It never calls a model.

Started by the demo's launcher (``<demo root>/bin/claude``, a ``#!/bin/sh`` script that only the demo's config names;
it is never put on PATH) as ``python -I demo_actor.py <the argv fleet.claude_argv builds>``, on the pane's PTY. It
keeps the contract of the official CLI that the fleet relies on (fleetmux/claude_input.py, claude_hook.py,
fleet.claude_argv), the same contract tests/m4e/fake_cli.py keeps for the end-to-end tests:

- ``--version`` prints a version at the deps.yaml minimum with the word ``scripted`` in it;
- the ruled editor ``claude_input.visible_editor`` reads (``❯ <draft>`` between two rules, an idle / busy footer);
- the transcript ``$HOME/.claude/projects/<cwd slug>/<session>.jsonl`` (the demo's own HOME, never the user's);
- the ``--settings`` hooks: blocking ``Stop`` hooks it waits for (a ``block`` decision is the next turn) and
  ``asyncRewake`` hooks in the background (exit 2 = next turn).

Only the replies are scripted (``Script``): the supervisor dispatches the task, claims COMPLETED after each
report and passes on the program's rejection; the worker writes the function first without its test, then adds
the test when told. Whether the work is done is decided by the fleet daemon, never here. Every reply and every
screen carries the word ``scripted``. Standard library only: ``-I`` keeps the fleet's packages out.
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
import textwrap
import time
import tty
import unicodedata
import uuid
from pathlib import Path

BANNER = 'scripted agents, no model calls — the acceptance is real fleetmux code'
VERSION = '2.1.280 (scripted fleetmux demo actor, no model calls)'
TURN_S = float(os.environ.get('FLEETMUX_DEMO_TURN_S') or 1.0)  # a turn "works" this long, so it can be watched
GOAL_MARK = '[fleetmux demo task]'
SLUG = 'worker-1/slug.py'      # inside worker-1's own directory of the shared workspace
TEST = 'worker-1/test_slug.py'
SLUG_TEXT = '''import re


def slugify(text):
    """Lower-case words joined by single hyphens."""
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')
'''
TEST_TEXT = '''import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from slug import slugify


def test_slugify():
    assert slugify('Hello, World!') == 'hello-world'
    assert slugify('  fleetmux  demo ') == 'fleetmux-demo'


if __name__ == '__main__':
    test_slugify()
    print('1 passed')
'''


def stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'


def width_of(text):
    return sum(0 if unicodedata.combining(c) else 2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in text)


ESCAPE = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-_]')
RESET = '\x1b[0m'


def clip(text, cols):
    """At most ``cols`` visible columns. An escape sequence takes no column and is kept whole. A line that has one
    ends with a reset, so a cut before the line's own reset leaves no bold or reverse on the rest of the pane."""
    out, used, i, styled = [], 0, 0, False
    while i < len(text):
        m = ESCAPE.match(text, i) if text[i] == '\x1b' else None
        if m:
            out.append(m.group())
            styled, i = True, m.end()
            continue
        w = width_of(text[i])
        if used + w > cols:
            break
        out.append(text[i])
        used, i = used + w, i + 1
    out = ''.join(out)
    return out + RESET if styled and not out.endswith(RESET) else out


def plain(text):
    """Printable text only, no rule-like dash runs (the editor detector must see only the real rules)."""
    text = ''.join(c if c == '\n' or not unicodedata.category(c).startswith('C') else ' ' for c in text or '')
    return re.sub(r'[─━-]{4,}', '--', text)


# ----------------------------------------------------------------------------------------------- the script
class Script:
    """The replies. ``role`` is the slot: ``supervisor`` or a worker name."""

    def __init__(self, role, cwd):
        self.role, self.cwd = role, Path(cwd)
        self.dispatched = False
        self.tree_sent = False
        self.dispatched_tree = False

    @staticmethod
    def decision(status, reason, dispatch=(), completion=0, **extra):
        return json.dumps(dict({'status': status, 'completion': completion, 'reason': '[scripted] ' + reason,
                                'dispatch': list(dispatch)}, **extra), ensure_ascii=False)

    def work(self, reason, instruction, completion):
        """The one block of the demo, M0 (with the decomposition tree on, the id must be a block of the tree)."""
        return self.decision('RUNNING', reason, [{'id': 'M0', 'to': 'worker-1', 'instruction': instruction}],
                             completion)

    def answer(self, text):
        if re.search(r'exactly READY', text):
            return 'READY'
        if '[SYSTEM HANDOFF]' in text:
            return '[scripted] handoff not used in the demo'
        if self.role == 'supervisor':
            return self.supervisor(text)
        return self.worker(text)

    def supervisor(self, text):
        failed = re.search(r'Independent verification failed, the task cannot end: (.+)', text)
        if failed:  # the program's own rejection: pass its reason on to the worker
            why = failed.group(1).strip()
            return self.work('the program rejected the claim; sending the fix to worker-1',
                             '[scripted] The acceptance check failed: ' + why + '. Add ' + TEST
                             + ' with a test for slugify(), then report.', 60)
        if 'your first reply hands in the tree only' in text and not self.tree_sent:  # §62: the tree comes first
            self.tree_sent = True
            return self.decision('RUNNING', 'handing in the tree: one block, M0', completion=5, tree=[{
                'id': 'M0', 'goal': 'slugify(text) and its test', 'est': {'minutes': 5}, 'owns': [SLUG, TEST],
                'accept': 'the program accepts the declared artifacts'}])
        if (GOAL_MARK in text and not self.dispatched) or (self.tree_sent and '[tree] rev' in text
                                                            and not self.dispatched_tree):
            self.dispatched = True
            self.dispatched_tree = self.tree_sent
            return self.work('dispatching the task to worker-1', '[scripted] Write ' + SLUG
                             + ' with slugify(text) and ' + TEST + ' with its test, then report.', 10)
        if self.dispatched and '[worker-1 report]' in text and '[scripted worker-1]' in text:
            return self.decision('COMPLETED', 'worker-1 reports done; claiming COMPLETED', completion=100)
        return self.decision('RUNNING', 'nothing to do')

    def worker(self, text):
        root = self.cwd.parent  # the shared workspace (fleet.agent_cwd: <run>/workspace/<worker>)
        tag = '[scripted ' + self.role + '] '
        if TEST in text and 'acceptance check failed' in text:
            (root / TEST).write_text(TEST_TEXT, encoding='utf-8')
            return 'Done: ' + tag + 'wrote ' + TEST + '.\nEvidence: ' + TEST + '\nNot done: none\nBlocked: none'
        if SLUG in text:
            (root / SLUG).parent.mkdir(exist_ok=True)
            (root / SLUG).write_text(SLUG_TEXT, encoding='utf-8')
            # the scripted mistake: the test is declared but not written, and the report says done anyway
            return ('Done: ' + tag + 'wrote ' + SLUG + ' and its test.\nEvidence: ' + SLUG
                    + '\nNot done: none\nBlocked: none')
        return tag + 'READY'


# ----------------------------------------------------------------------------------------------- terminal
class Term:
    """Raw-mode PTY input (bracketed paste, Enter, Ctrl+C, Escape) and whole-screen redraws."""

    def __init__(self):
        self.fd = 0
        self.saved = None
        self.dec = codecs.getincrementaldecoder('utf-8')('replace')
        self.buf = ''
        self.pasting = None
        if os.isatty(self.fd):
            self.saved = termios.tcgetattr(self.fd)
            tty.setraw(self.fd)
        self.out('\x1b[?1049h\x1b[?2004h\x1b[2J')

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
        self.out('\x1b[?2004l\x1b[?1049l')
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
                break
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


# ----------------------------------------------------------------------------------------------- the CLI
CLAUDE_VALUE = {'--settings', '--name', '--model', '--append-system-prompt', '--disallowedTools', '--setting-sources',
                '--resume', '--effort', '--system-prompt-file', '--tools', '--session-id', '--permission-mode'}
CLAUDE_MULTI = {'--add-dir', '--mcp-config', '--allowedTools'}
CLAUDE_FLAG = {'--dangerously-skip-permissions', '--strict-mcp-config', '--disable-slash-commands'}


def parse_argv(argv):
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


class Actor:
    def __init__(self, argv, log_path=None):
        self.quit = False
        self.wake_r, self.wake_w = os.pipe()
        os.set_blocking(self.wake_w, False)
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sig, self.on_signal)
        signal.signal(signal.SIGWINCH, lambda *_: self.poke())
        self.log_path = log_path
        self.opts, prompt = parse_argv(argv)
        self.cwd = os.getcwd()
        self.role = Path(self.cwd).name
        self.script = Script(self.role, self.cwd)
        self.settings = {}
        if self.opts.get('--settings'):
            self.settings = json.loads(Path(self.opts['--settings']).read_text(encoding='utf-8'))
        self.name = re.sub(r'\s+', '-', self.opts.get('--name') or self.role)[:40]
        self.state = 'idle'  # idle / working / hooks / prompting
        self.blocking, self.rewakes, self.pending = [], [], []
        # UserPromptSubmit gates in flight: [(procs, kind, text, content, was_state)]; blocked_note: the footer line
        # the real CLI shows after a hook blocked a prompt (claude_input.visible_editor accepts it)
        self.gates, self.blocked_note = [], ''
        self.blocks, self.turn, self.reply_at = 0, None, 0.0
        self.draft, self.collapsed, self.pastes = '', '', 0
        self.feed = []  # the conversation shown on screen: (kind, text)
        self.dirty = True
        self.session = str(uuid.uuid4())
        slug = re.sub(r'[^A-Za-z0-9]', '-', self.cwd)
        self.transcript = Path.home() / '.claude' / 'projects' / slug / (self.session + '.jsonl')
        self.transcript.parent.mkdir(parents=True, exist_ok=True)
        self.transcript.touch()
        self.live = self.mark_alive()
        self.log('start', argv=argv, cwd=self.cwd, home=os.environ.get('HOME'))
        if prompt:
            self.start('argv', prompt)

    @staticmethod
    def mark_alive():
        """Run the demo's liveness marker (``$FLEETMUX_DEMO_LIVE``, a ``sleep`` linked under the name ``claude`` in
        the demo's own directory) while this actor runs. It stays on this pane's terminal and in this process group:
        the daemon's check for an official CLI on the pane (a process named ``claude``) sees this actor as alive,
        and closing the pane ends the marker with it. None outside the demo."""
        marker = os.environ.get('FLEETMUX_DEMO_LIVE')
        if not marker:
            return None
        try:
            return subprocess.Popen([marker, '3600'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL)
        except OSError:
            return None

    def log(self, event, **fields):
        if not self.log_path:
            return
        try:
            with open(self.log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(dict(event=event, at=time.time(), role=self.role if hasattr(self, 'role') else '',
                                        **fields), ensure_ascii=False) + '\n')
        except OSError:
            pass

    def on_signal(self, sig, _frame):
        self.quit = True
        self.poke()

    def poke(self):
        self.dirty = True
        try:
            os.write(self.wake_w, b'x')
        except OSError:
            pass

    # -- transcript
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
    def start(self, kind, text, content=None):
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
        self.feed.append(('in', text))
        self.log('turn.start', kind=kind, text=text[:2000])
        self.poke()

    def finish_turn(self):
        reply = self.script.answer(self.turn['text'])
        self.feed.append(('out', reply))
        self.row(type='assistant', message={'role': 'assistant', 'model': 'scripted',
                                            'content': [{'type': 'text', 'text': reply}]})
        self.log('turn.reply', reply=reply[:2000])
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
                    err = self.transcript.with_name('rewake-%d-%s.err' % (os.getpid(), uuid.uuid4().hex[:8]))
                    proc = self.hook(hook['command'], payload, stdout=subprocess.DEVNULL, stderr=err)
                    if proc:
                        self.rewakes.append((proc, err, hook.get('rewakeMessage') or 'Stop hook feedback:'))
                else:
                    proc = self.hook(hook['command'], payload, stdout=subprocess.PIPE)
                    if proc:
                        self.blocking.append((proc, time.monotonic(), float(hook.get('timeout') or 600)))
        if not self.blocking:
            self.state = 'idle'
        self.poke()

    def hook(self, command, payload, stdout, stderr=None):
        env = dict(os.environ, **{k: str(v) for k, v in (self.settings.get('env') or {}).items()})
        env['CLAUDE_PROJECT_DIR'] = self.cwd
        err = open(stderr, 'w') if stderr else subprocess.DEVNULL
        try:
            proc = subprocess.Popen(['/bin/sh', '-c', command], stdin=subprocess.PIPE, stdout=stdout, stderr=err,
                                    cwd=self.cwd, env=env)
            proc.stdin.write(json.dumps(payload).encode())
            proc.stdin.close()
        except OSError as exc:
            self.log('hook.failed', error=str(exc))
            return None
        finally:
            if err is not subprocess.DEVNULL:
                err.close()
        return proc

    def tick(self):
        now = time.monotonic()
        if self.state == 'working' and self.turn and now >= self.reply_at:
            self.finish_turn()
        if self.blocking:
            for proc, started, timeout in list(self.blocking):
                if proc.poll() is None and now - started > timeout:
                    proc.terminate()
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
                Path(err).unlink()
            except OSError:
                pass
            if proc.returncode == 2 and text:
                content = ('<task-notification>\n<summary>Stop hook feedback</summary>\n</task-notification>\n'
                           '<system-reminder>\n' + message + ' ' + text + '\n</system-reminder>')
                self.submit('rewake', text, content)
        if self.gates:
            self.gates_tick(now)
        if self.state == 'idle' and self.pending:
            kind, text, content = self.pending.pop(0)
            self.start(kind, text, content)

    def submit(self, kind, text, content=None):
        """A prompt about to become a turn (a typed line, an asyncRewake notification): the settings'
        UserPromptSubmit hooks run first, as in Claude Code (measured on 2.1.307, fleet.supervisor_rewake_prompt); a
        block decision or exit 2 keeps it from running and from the queue. No such hooks: straight to ``later``."""
        hooks = [h for entry in (self.settings.get('hooks') or {}).get('UserPromptSubmit') or []
                 for h in entry.get('hooks') or [] if h.get('type') == 'command']
        if not hooks:
            self.later(kind, text, content)
            return
        payload = {'session_id': self.session, 'transcript_path': str(self.transcript), 'cwd': self.cwd,
                   'permission_mode': 'bypassPermissions', 'hook_event_name': 'UserPromptSubmit',
                   'prompt': content if kind == 'rewake' else text}
        procs = []
        for hook in hooks:
            proc = self.hook(hook['command'], payload, stdout=subprocess.PIPE)
            if proc:
                procs.append((proc, time.monotonic(), float(hook.get('timeout') or 600)))
        self.log('prompt_hook.start', kind=kind, hooks=len(procs))
        self.gates.append((procs, kind, text, content, self.state))
        if self.state == 'idle':
            self.state = 'prompting'  # busy on screen while the hook runs (esc to interrupt)
        self.poke()

    def gates_tick(self, now):
        for gate in list(self.gates):
            procs, kind, text, content, was = gate
            for proc, started, timeout in procs:
                if proc.poll() is None and now - started > timeout:
                    proc.terminate()
            if any(proc.poll() is None for proc, _, _ in procs):
                continue
            self.gates.remove(gate)
            reasons = []
            for proc, _, _ in procs:
                out = b''
                try:
                    out = (proc.stdout.read() or b'') if proc.stdout else b''
                except OSError:
                    pass
                try:
                    decision = json.loads(out.decode('utf-8', 'replace') or '{}')
                except ValueError:
                    decision = {}
                decision = decision if isinstance(decision, dict) else {}
                if proc.returncode == 2 or decision.get('decision') == 'block':
                    reasons.append(str(decision.get('reason') or 'blocked'))
            if self.state == 'prompting' and not self.gates:
                self.state = 'idle'
            if reasons:
                self.blocked_note = 'UserPromptSubmit operation blocked by hook: ' + plain('; '.join(reasons))
                self.log('prompt_hook.blocked', kind=kind, reason='; '.join(reasons)[:500])
            else:
                self.log('prompt_hook.passed', kind=kind)
                self.later(kind, text, content)
            self.poke()

    def later(self, kind, text, content=None):
        if self.state == 'idle' and not self.pending:
            self.start(kind, text, content)
            return
        if kind in ('typed', 'rewake'):
            self.row(type='queue-operation', operation='enqueue', content=content or text)
        self.pending.append(('dequeue' if kind == 'typed' else kind, text, content))

    def hooks_done(self, done):
        reasons = []
        for proc, _, _ in done:
            out = b''
            if proc.stdout:
                try:
                    out = proc.stdout.read() or b''
                except OSError:
                    pass
            try:
                decision = json.loads(out.decode('utf-8', 'replace') or '{}')
            except ValueError:
                decision = {}
            if isinstance(decision, dict) and decision.get('decision') == 'block' and decision.get('reason'):
                reasons.append(decision['reason'])
        self.blocking = []
        cap = int((self.settings.get('env') or {}).get('CLAUDE_CODE_STOP_HOOK_BLOCK_CAP', os.environ.get(
            'CLAUDE_CODE_STOP_HOOK_BLOCK_CAP', '8')) or 0)
        self.state = 'idle'
        if reasons and (cap <= 0 or self.blocks < cap):
            self.pending.insert(0, ('block', '\n'.join(reasons), None))
        self.poke()

    # -- input
    def on_input(self, kind, value, cols):
        if kind == 'paste':
            self.draft += value
            if '\n' in value or any(width_of(line) >= cols - 3 for line in value.split('\n')):
                self.pastes += 1
                self.collapsed = '[Pasted text #%d +%d lines]' % (self.pastes, value.count('\n') + 1)
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
            self.blocked_note = ''
            self.submit('typed', text)

    def escape(self):
        if self.state == 'prompting':  # the prompt hooks are cut off: the prompt does not run
            for procs, _, _, _, _ in self.gates:
                for proc, _, _ in procs:
                    if proc.poll() is None:
                        proc.terminate()
            self.gates, self.state = [], 'idle'
            return
        if self.state in ('working', 'hooks'):
            for proc, _, _ in self.blocking:
                if proc.poll() is None:
                    proc.terminate()
            self.blocking, self.turn = [], None
            self.user_row('[Request interrupted by user]')
            self.state = 'idle'

    # -- screen
    def feed_lines(self, cols):
        out = []
        for kind, text in self.feed:
            head = '◀ ' if kind == 'in' else '▶ '
            body = plain(text)
            if kind == 'out' and body.lstrip().startswith('{'):
                try:  # a decision: show what it does, not the JSON
                    d = json.loads(body)
                    body = d.get('status', '') + '  ' + d.get('reason', '') + ''.join(
                        '\n  -> ' + x.get('to', '') + ': ' + x.get('instruction', '') for x in d.get('dispatch') or [])
                except ValueError:
                    pass
            if kind == 'in' and len(body) > 600:
                body = body[:600] + ' ...'
            for line in body.split('\n'):
                for part in textwrap.wrap(line, max(10, cols - 2)) or ['']:
                    out.append(head + part)
                    head = '  '
            out.append('')
        return out

    def render(self, term):
        rows, cols = term.size()
        body = (['❯ ' + self.collapsed] if self.collapsed else
                [('❯ ' + line if i == 0 else '  ' + line) if line or i else '❯'
                 for i, line in enumerate(self.draft.split('\n'))])[:max(1, rows - 8)]
        footer = '  ? for shortcuts' if self.state == 'idle' else '  esc to interrupt'
        status = {'idle': '', 'working': '✻ Working… (esc to interrupt)',
                  'hooks': '✻ running stop hooks… (esc to interrupt)',
                  'prompting': '✻ running prompt hooks… (esc to interrupt)'}[self.state]
        name = clip(self.name, max(1, cols - 14))
        top = '─' * 10 + ' ' + name + ' ' + '─' * max(1, cols - 12 - width_of(name))
        notes = ['  ' + clip(self.blocked_note, max(1, cols - 3))] if self.blocked_note else []
        block = [status, top] + body + ['─' * cols] + notes + [footer]
        head = ['\x1b[1;7m ' + clip(BANNER, max(1, cols - 2)) + ' \x1b[0m',
                '[scripted] ' + self.role + ' (demo actor, no model)', '']
        room = max(0, rows - len(head) - len(block))
        # the newest output sits right above the input box, as a CLI draws it (a wall row stays in the bottom rows
        # the brake's screen gate reads, section 67)
        feed = self.feed_lines(cols)[-room:] if room else []
        lines = head + [''] * (room - len(feed)) + feed if room else head[:rows]
        lines = (lines + [''] * rows)[:max(0, rows - len(block))] + block
        cursor_row = len(lines) - len(block) + 2 + len(body) - 1
        term.draw(lines, (cursor_row + 1, min(cols, width_of(body[-1]) + 1)))

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
                if not data:
                    break
                cols = term.size()[1]
                for kind, value in term.events(data):
                    self.on_input(kind, value, cols)
                self.dirty = True
            self.tick()

    def shutdown(self):
        if self.live is not None and self.live.poll() is None:
            self.live.terminate()
            try:
                self.live.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.live.kill()
        for proc, _, _ in self.blocking:
            if proc.poll() is None:
                proc.terminate()
        for proc, _, _ in self.blocking:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()


def main(argv):
    if argv == ['--version']:
        print(VERSION)
        return 0
    log_path = os.environ.get('FLEETMUX_DEMO_ACTOR_LOG') or None
    try:
        actor = Actor(argv, log_path)
    except (ValueError, OSError) as exc:
        print('fleetmux demo actor (scripted): ' + str(exc), file=sys.stderr)
        return 64
    term = Term()
    try:
        actor.loop(term)
    finally:
        actor.shutdown()
        term.close()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
