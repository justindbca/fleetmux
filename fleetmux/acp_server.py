"""`fleetmux acp`: the running root fleet as an Agent Client Protocol (ACP) agent over stdio (docs/omnigent.md).

An ACP client (Omnigent registers it as ``{name: fleetmux, command: fleetmux acp}``) sends a goal with
``session/prompt``. The goal becomes one job of the running root fleet (docs/design.md section 58), submitted
through the join path (``join.submit``: ``task --config <root> --job -``). Progress goes back as
``session/update`` chunks; the last paragraph of the turn is the program's acceptance result.

Wire format: newline-delimited JSON-RPC 2.0. stdout carries only protocol messages; diagnostics go to stderr.

* ``initialize`` / ``authenticate`` (no authentication) / ``session/new`` / ``session/prompt`` /
  ``session/cancel`` (a notification). ``session/set_model``, ``session/set_config_option``, ``session/load`` and
  any other request get JSON-RPC error -32601 "not supported".
* No running root fleet, a refused job or a bad JSON block: one visible ``agent_message_chunk`` that starts with
  ``ERROR``, then ``stopReason: end_turn``. Nothing is submitted, and no fleet is ever started from here.
* Job keys: an optional fenced ```` ```json ```` block at the very end of the message. Allowed keys are the job
  keys of the README ("Jobs"), except ``goal`` (the message text is the goal). Without declared artifacts the job
  gets DEFAULT_ARTIFACT and the goal gets DEFAULT_NOTE, so every job ends with a program check.
* Omnigent's wrapper (0.22.0, inner/acp_executor.py ``run_turn``): on the first turn of a fresh ACP session it
  prepends its system prompt and, after a respawn, the replayed conversation. Only that first prompt of a session
  is unwrapped (strip_omnigent_wrapper) before the goal and the JSON block are read.
* Progress: the job's state line on every change (``agent_message_chunk``), a heartbeat
  (``agent_thought_chunk``) when nothing was sent for HEARTBEAT_S seconds: Omnigent ends a turn after 300 s
  without any message.
* The root daemon dies under a running turn (Redis still answers, the job row never changes): each status read
  also gives the daemon's heartbeat age. Stale for DAEMON_STALE_S, the run's daemon.lock is probed; no daemon
  holds it, or the beat stays stale for DAEMON_HUNG_S, and the turn ends with a visible ERROR (the job stays
  recorded in the root, nothing is withdrawn).
* ``session/cancel``: ``job_cancel.cancel`` stops only that job's sub-fleet. A job without a sub-fleet yet is
  registered by job id and retried every CANCEL_RETRY_S seconds while this process lives.
* stdin EOF (the client closed the session) ends the process; submitted jobs are never withdrawn.
"""
import argparse
import json
import re
import sys
import threading
import time
import uuid
from pathlib import Path

from fleetmux import join
from fleetmux.join import JoinError

PROTOCOL_VERSION = 1
POLL_S = 20          # seconds between two looks at the job
HEARTBEAT_S = 90     # a heartbeat when nothing was sent this long (Omnigent's idle deadline is 300 s)
CANCEL_RETRY_S = 15  # retry of a cancel registered for a job that has no sub-fleet yet
MAX_VIEW_ERRORS = 10  # consecutive failed status reads before the turn gives up (the job keeps running)
DAEMON_STALE_S = 180  # heartbeat age that makes the turn probe the root's daemon.lock (the daemon beats ~1/s)
DAEMON_HUNG_S = 900   # heartbeat age that ends the turn even while a (hung) daemon still holds the lock
EXCERPT_CHARS = 2000

# README "Jobs" rule 1, without `goal` (the message text). fleet.JOB_KEYS also has overrides / supervisor /
# worker, which the README does not list: refused here.
JOB_KEYS = ('hours', 'artifacts', 'budget', 'lifecycle', 'workers', 'supervisor_profile', 'worker_profiles',
            'name', 'roster', 'roles', 'deliver', 'review_file', 'tree', 'by')
DEFAULT_ARTIFACT = {'path': 'worker-1/result.md', 'min_chars': 300}
DEFAULT_NOTE = ('Write the final answer to worker-1/result.md (at least 300 characters); the program checks '
                'this file.')
START_COMMAND = 'fleetmux start --lifecycle service --allow-development-v2'
NO_ROOT = ('ERROR: no fleetmux root fleet is running on this machine, so the goal was not submitted. Start one '
           'in a terminal, then send the goal again:\n  ' + START_COMMAND + '\n(fleetmux acp never starts a fleet '
           'itself. A job needs a worker on the official Claude Code CLI.)\n')

# Omnigent 0.22.0 prompt wrapper: runtime/prompt.py WORKSPACE_IMAGE_INSTRUCTION is the last part of every
# system prompt (_framework_instructions_for); inner/acp_executor.py _history_prefix frames a replayed history.
OMNIGENT_LAST_SYSTEM_PART = (
    "When showing an existing screenshot or raster image saved inside the session "
    "workspace, embed it in your reply as `![description](/absolute/workspace/path/image.png)` "
    "unless the user's instructions say otherwise. Use the actual workspace file path; "
    "remote image URLs and files outside the workspace cannot be displayed."
)
HISTORY_HEAD = 'Conversation so far:\n'
HISTORY_TAIL = '\n\nRespond to the latest user message, using the conversation above as context.'
LATEST_USER = '\n\nuser: '
OPEN_FENCE = re.compile(r'```[ \t]*json[ \t]*\n', re.I)


class PromptError(ValueError):
    """The message cannot become a job; the text is shown to the person."""


def allowed_keys():
    return 'Allowed keys in the ```json block at the end of the message: ' + ', '.join(JOB_KEYS) + '.'


def strip_omnigent_wrapper(text):
    """The person's own text of a session's first prompt: Omnigent's system prompt and replayed history removed.
    Text without a recognised wrapper is returned whole."""
    head = text.find(HISTORY_HEAD)
    if head != -1 and (head == 0 or text[head - 2:head] == '\n\n'):
        tail = text.find(HISTORY_TAIL, head)
        if tail != -1:
            rest = text[tail + len(HISTORY_TAIL):]
            if rest.startswith(LATEST_USER):
                return rest[len(LATEST_USER):]
            if not rest.strip():
                return ''
    mark = text.find(OMNIGENT_LAST_SYSTEM_PART)
    if mark != -1:
        rest = text[mark + len(OMNIGENT_LAST_SYSTEM_PART):]
        if rest.startswith('\n\n'):
            return rest[2:]
        if not rest.strip():
            return ''
    return text


def split_declaration(text):
    """(text before the block, the block's raw JSON) when the message ends with a ```json block, else
    (text, None). A JSON block anywhere else is part of the goal."""
    stripped = text.rstrip()
    if not stripped.endswith('```'):
        return text, None
    body_end = len(stripped) - 3
    opens = list(OPEN_FENCE.finditer(stripped, 0, body_end))
    if not opens:
        return text, None
    raw = stripped[opens[-1].end():body_end]
    if '```' in raw:  # the closing fence belongs to another code block
        return text, None
    return stripped[:opens[-1].start()], raw


def job_spec(text):
    """The job spec for the person's message ``text``. PromptError when it cannot be one."""
    goal, raw = split_declaration(text)
    spec = {}
    if raw is not None:
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise PromptError('the ```json block at the end of the message is not valid JSON (' + str(exc) + '). '
                              + allowed_keys())
        if not isinstance(data, dict):
            raise PromptError('the ```json block at the end of the message must be a JSON object. ' + allowed_keys())
        if 'goal' in data:
            raise PromptError('`goal` is the message text above the ```json block, not a key of it. '
                              + allowed_keys())
        bad = sorted(set(data) - set(JOB_KEYS))
        if bad:
            raise PromptError('unknown job key(s): ' + ', '.join(bad) + '. ' + allowed_keys())
        spec = dict(data)
    goal = goal.strip()
    if not goal:
        raise PromptError('the message has no goal text. Write the goal as the message; the optional ```json '
                          'block at its end only sets job keys. ' + allowed_keys())
    if spec.get('artifacts') in (None, []):  # not declared: the default program check
        spec.pop('artifacts', None)
        if 'roster' in spec:
            raise PromptError('a job with `roster` must declare `artifacts`: its worker names are not known here, '
                              'so the default check (' + DEFAULT_ARTIFACT['path'] + ') cannot be used. '
                              + allowed_keys())
        spec['artifacts'] = [dict(DEFAULT_ARTIFACT)]
        goal += '\n\n' + DEFAULT_NOTE
    spec['goal'] = goal
    return spec


def progress_line(jid, row, item):
    state = row.get('state') or 'queued'
    line = '[fleetmux] ' + jid + ': ' + state
    if row.get('subfleet'):
        line += ' in sub-fleet ' + row['subfleet'] + (' on ' + row['slot'] if row.get('slot') else '')
    if item and item.get('state') and item['state'] != state:
        line += ' (sub-fleet ' + str(item['state']) + ')'
    if row.get('waiting'):
        line += ' - waiting: ' + str(row['waiting'])
    return line


def _one_paragraph(value):
    return re.sub(r'\n\s*\n+', '\n', str(value)).strip()


def verdict(jid, res):
    """The final text: an excerpt of worker-1/result.md (when written), then the program check as the last
    paragraph (no blank line inside it)."""
    res = res if isinstance(res, dict) else {}
    r = res.get('result') if isinstance(res.get('result'), dict) else {}
    status = str(res.get('status') or 'unknown')
    parts = []
    workspace = r.get('workspace')
    if workspace:
        path = Path(workspace) / DEFAULT_ARTIFACT['path']
        try:
            text = path.read_text(errors='replace') if path.is_file() else ''
        except OSError:
            text = ''
        if text.strip():
            cut = text[:EXCERPT_CHARS].strip() + ('\n[... ' + str(len(text) - EXCERPT_CHARS) + ' more characters]'
                                                   if len(text) > EXCERPT_CHARS else '')
            parts.append(DEFAULT_ARTIFACT['path'] + ':\n' + cut)
    lines = ['fleetmux program check: ' + status.upper() + ' (job ' + jid + ')']
    for label, value in (('outcome', r.get('outcome')), ('reason', r.get('reason') or r.get('reply')),
                         ('error', res.get('error')),
                         ('delivery gate', (r.get('delivery') or {}).get('problem')
                          if isinstance(r.get('delivery'), dict) else None),
                         ('artifacts', ', '.join(a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)
                                                 for a in r.get('artifacts') or [])),
                         ('run', r.get('run')), ('workspace', workspace)):
        if value:
            lines.append(label + ': ' + _one_paragraph(value))
    parts.append('\n'.join(lines))
    return '\n' + '\n\n'.join(parts) + '\n'


def run_dir(root):
    """The root's run directory, read as join._root reads it: the config's ``run``, else the config's folder."""
    try:
        data = json.loads(Path(root).read_text())
    except (OSError, ValueError):
        data = {}
    run = data.get('run') if isinstance(data, dict) else None
    return Path(run) if isinstance(run, str) and run else Path(root).parent


class Ops:
    """The running root fleet, through join's calls (each runs ``fleet.py <command>`` in a child process)."""

    def find_root(self):
        from fleetmux import fleet
        return join.find_root(fleet.LATEST.parent)

    def submit(self, root, spec):
        return join.submit(root, spec)

    def view(self, root, jid):
        """(job row or None, its sub-fleet tree entry or None, the root daemon's heartbeat age in s or None) from
        one ``status`` of the root (the row / entry as join.job_view reads them)."""
        code, out, err = join.fleet_cli(['status', '--config', str(root)])
        status = join.parse_json(out)
        if code != 0 or not status:
            raise JoinError('status of the root failed: ' + (err.strip() or out.strip() or 'exit code ' + str(code)))
        row = next((r for r in status.get('jobs') or [] if isinstance(r, dict) and r.get('id') == jid), None)
        item = None
        if row and row.get('subfleet'):
            from fleetmux import subfleet
            tree = status.get('subfleets') or {}
            found = [v for k, v in tree.items() if k == row['subfleet'] or k.startswith(row['subfleet'] + ' (')]
            live = [v for v in found if isinstance(v, dict) and v.get('state') in subfleet.ACTIVE]
            item = (live or found or [None])[-1]
        age = (status.get('daemon') or {}).get('heartbeat_age_s')
        return row, item, float(age) if isinstance(age, (int, float)) and not isinstance(age, bool) else None

    def daemon_running(self, root):
        """Whether a daemon holds the root run's daemon.lock (asked only once its heartbeat is stale)."""
        from fleetmux import fleet
        run = run_dir(root)
        if not (run / 'daemon.lock').is_file():  # fleet.daemon_running opens with a+: never create a stray lock
            return False
        return fleet.daemon_running(run)

    def result(self, root, jid):
        return join.root_result({'root_config': root}, jid)

    def cancel(self, root, jid):
        from fleetmux import job_cancel
        return job_cancel.cancel(root, jid)


class AcpServer:
    def __init__(self, ops=None, poll_s=POLL_S, heartbeat_s=HEARTBEAT_S, cancel_retry_s=CANCEL_RETRY_S,
                 daemon_stale_s=DAEMON_STALE_S, daemon_hung_s=DAEMON_HUNG_S):
        self.ops = ops or Ops()
        self.poll_s, self.heartbeat_s, self.cancel_retry_s = poll_s, heartbeat_s, cancel_retry_s
        self.daemon_stale_s, self.daemon_hung_s = daemon_stale_s, daemon_hung_s
        self.sessions = {}         # session id -> {'prompted': bool, 'turn': dict or None}
        self.pending_cancels = {}  # job id -> root config: cancels that wait for the job's sub-fleet
        self.closed = threading.Event()
        self._lock = threading.Lock()
        self._write = threading.Lock()
        self._retry = None
        self.out = None

    # -- transport
    def serve(self, inp, out):
        """Read requests until EOF. Returns then; jobs already submitted keep running on the root."""
        self.out = out
        try:
            for line in iter(inp.readline, ''):
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    self._send({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}})
                    continue
                if not isinstance(msg, dict):
                    self._send({'jsonrpc': '2.0', 'id': None,
                                'error': {'code': -32600, 'message': 'invalid request'}})
                    continue
                try:
                    self.dispatch(msg)
                except Exception as exc:  # one bad message never ends the session
                    if msg.get('method') and 'id' in msg:
                        self._error(msg['id'], -32603, 'internal error: ' + str(exc))
                    sys.stderr.write('fleetmux acp: ' + repr(exc) + '\n')
        finally:
            self.closed.set()

    def _send(self, msg):
        if self.closed.is_set() and 'method' in msg:
            return
        data = json.dumps(msg, ensure_ascii=False) + '\n'
        with self._write:
            try:
                self.out.write(data)
                self.out.flush()
            except (OSError, ValueError):  # the client is gone; the job is not affected
                pass

    def _result(self, rid, result):
        self._send({'jsonrpc': '2.0', 'id': rid, 'result': result})

    def _error(self, rid, code, message):
        self._send({'jsonrpc': '2.0', 'id': rid, 'error': {'code': code, 'message': message}})

    def _update(self, sid, kind, text):
        self._send({'jsonrpc': '2.0', 'method': 'session/update', 'params': {
            'sessionId': sid, 'update': {'sessionUpdate': kind, 'content': {'type': 'text', 'text': text}}}})

    def say(self, sid, text):
        self._update(sid, 'agent_message_chunk', text)

    def think(self, sid, text):
        self._update(sid, 'agent_thought_chunk', text)

    # -- methods
    def dispatch(self, msg):
        method, rid, params = msg.get('method'), msg.get('id'), msg.get('params') or {}
        if not method:
            return  # a response: this agent sends no requests
        request = 'id' in msg
        if method == 'initialize':
            self._result(rid, {'protocolVersion': PROTOCOL_VERSION,
                               'agentCapabilities': {'loadSession': False, 'promptCapabilities': {
                                   'image': False, 'audio': False, 'embeddedContext': False}},
                               'authMethods': [], 'agentInfo': {'name': 'fleetmux', 'title': 'fleetmux'}})
        elif method == 'authenticate':
            self._result(rid, {})
        elif method == 'session/new':
            sid = params.get('sessionId') if isinstance(params.get('sessionId'), str) and params['sessionId'] \
                else 'fleetmux-' + uuid.uuid4().hex[:16]
            with self._lock:
                self.sessions[sid] = {'prompted': False, 'turn': None}
            self._result(rid, {'sessionId': sid})
        elif method == 'session/prompt':
            self.prompt(rid, params)
        elif method == 'session/cancel':
            with self._lock:
                turn = (self.sessions.get(params.get('sessionId')) or {}).get('turn')
            if turn:
                turn['cancel'].set()
        elif request:
            self._error(rid, -32601, method + ' is not supported by fleetmux acp')

    def prompt(self, rid, params):
        sid = params.get('sessionId')
        with self._lock:
            sess = self.sessions.get(sid)
            if sess is None:
                return self._error(rid, -32602, 'unknown session ' + repr(sid) + '; call session/new first')
            if sess['turn'] is not None:
                return self._error(rid, -32602, 'a prompt is already running in session ' + sid)
            first, sess['prompted'] = not sess['prompted'], True
            turn = sess['turn'] = {'cancel': threading.Event(), 'job': None, 'root': None}
        blocks = params.get('prompt') or []
        text = '\n'.join(b.get('text') or '' for b in blocks if isinstance(b, dict) and b.get('type') == 'text')
        threading.Thread(target=self._run_turn, args=(sid, rid, sess, turn, text, first), daemon=True,
                         name='acp-turn-' + str(sid)).start()

    def _run_turn(self, sid, rid, sess, turn, text, first):
        try:
            reason = self.turn(sid, turn, strip_omnigent_wrapper(text) if first else text)
        except Exception as exc:  # the turn ends visibly; a submitted job keeps running
            self.say(sid, 'ERROR: fleetmux acp failed: ' + str(exc) + '\n')
            reason = 'end_turn'
        with self._lock:
            sess['turn'] = None
        if reason:
            self._result(rid, {'stopReason': reason})

    def turn(self, sid, turn, text):
        """One prompt: the stopReason (None when the client went away)."""
        try:
            spec = job_spec(text)
        except PromptError as exc:
            self.say(sid, 'ERROR: ' + str(exc) + '\n')
            return 'end_turn'
        root = self.ops.find_root()
        if not root:
            self.say(sid, NO_ROOT)
            return 'end_turn'
        if turn['cancel'].is_set():
            self.say(sid, '[fleetmux] cancelled before the job was submitted; nothing was started.\n')
            return 'cancelled'
        try:
            jid = self.ops.submit(root['config'], spec)
        except JoinError as exc:
            self.say(sid, 'ERROR: ' + str(exc) + '\n')
            return 'end_turn'
        turn.update(job=jid, root=root['config'])
        self.say(sid, '[fleetmux] job ' + jid + ' accepted by the running fleet ' + root['config'] + '. Progress '
                 'follows; the last paragraph is the program check. Closing this session does not stop the job '
                 '(result later: fleetmux result ' + jid + ').\n')
        return self.watch(sid, turn)

    def watch(self, sid, turn):
        jid, root = turn['job'], turn['root']
        started = last_sent = time.monotonic()
        last_line, state, errors = None, 'queued', 0
        while not self.closed.is_set():
            if turn['cancel'].is_set():
                return self.cancel(sid, turn)
            code, res = self.ops.result(root, jid)
            if code == 0:
                self.say(sid, verdict(jid, res))
                return 'end_turn'
            if code == 2:
                self.say(sid, 'ERROR: the root fleet no longer knows job ' + jid + ' and has no result for it. '
                         'Check `fleetmux status` and `fleetmux result ' + jid + '`.\n')
                return 'end_turn'
            line = None
            try:
                row, item, beat_age = self.ops.view(root, jid)
                errors = 0
                if row:
                    state = row.get('state') or state
                    line = progress_line(jid, row, item)
                down = self.daemon_down(root, beat_age)
                if down:
                    self.say(sid, 'ERROR: the root fleet daemon ' + down + ' (no heartbeat for '
                             + str(int(beat_age // 60)) + ' min), so job ' + jid + ' (last state: ' + state
                             + ') cannot make progress and this turn ends here. The job stays recorded in the root '
                             'fleet; nothing was withdrawn. Bring the root back with: fleetmux start '
                             '--allow-development-v2 --resume ' + str(run_dir(root)) + ' ; its result later: '
                             'fleetmux result ' + jid + '\n')
                    return 'end_turn'
            except JoinError as exc:
                errors += 1
                if errors >= MAX_VIEW_ERRORS:
                    self.say(sid, 'ERROR: cannot read the root fleet any more (' + str(exc) + '). The job may still '
                             'run: check `fleetmux result ' + jid + '`.\n')
                    return 'end_turn'
            now = time.monotonic()
            if line and line != last_line:
                self.say(sid, line + '\n')
                last_line, last_sent = line, now
            elif now - last_sent >= self.heartbeat_s:
                self.think(sid, '[fleetmux] ' + jid + ' still ' + state + ' (' + str(int((now - started) // 60))
                           + ' min since it was submitted)\n')
                last_sent = now
            turn['cancel'].wait(self.poll_s)
        return None

    def daemon_down(self, root, beat_age):
        """Why the root daemon cannot move the job any more, or None. The lock is probed only once the heartbeat
        is stale: a probe takes it for an instant, and a daemon starting right then would fail."""
        if beat_age is None or beat_age < self.daemon_stale_s:
            return None
        try:
            alive = self.ops.daemon_running(root)
        except (OSError, ValueError) as exc:  # cannot tell: only the hung limit decides
            sys.stderr.write('fleetmux acp: daemon.lock probe failed: ' + repr(exc) + '\n')
            alive = True
        if not alive:
            return 'has stopped (no process holds its daemon.lock)'
        if beat_age >= self.daemon_hung_s:
            return 'holds its lock but has stopped its heartbeat'
        return None

    # -- cancel
    def cancel(self, sid, turn):
        jid, root = turn['job'], turn['root']
        try:
            out = self.ops.cancel(root, jid)
        except JoinError as exc:
            self.say(sid, 'ERROR: the cancel of job ' + jid + ' failed: ' + str(exc) + '. Stop it by hand: '
                     'fleetmux cancel ' + jid + '\n')
            return 'cancelled'
        if out.get('pending'):
            self.register_cancel(jid, root)
            self.say(sid, '[fleetmux] cancel registered: job ' + jid + ' is still ' + str(out.get('state') or 'queued')
                     + ' and has not been stopped yet. This session retries every ' + str(int(self.cancel_retry_s))
                     + ' s and stops it once its sub-fleet starts; if the session closes first, the registration is '
                     'lost and the job runs. Stop it by hand with: fleetmux cancel ' + jid + '\n')
        elif out.get('state') == 'ended':
            self.say(sid, '[fleetmux] job ' + jid + ' had already ended; nothing was stopped. Its result: '
                     'fleetmux result ' + jid + '\n')
        elif out.get('stopped'):
            self.say(sid, '[fleetmux] job ' + jid + ': its sub-fleet is stopping (only this job; the fleet and its '
                     'other jobs keep running). Its result: fleetmux result ' + jid + '\n')
        else:
            self.say(sid, 'ERROR: job ' + jid + ' was not stopped: ' + str(out.get('reason') or out) + '. Retry by '
                     'hand: fleetmux cancel ' + jid + '\n')
        return 'cancelled'

    def register_cancel(self, jid, root):
        with self._lock:
            self.pending_cancels[jid] = root
            if self._retry is None:
                self._retry = threading.Thread(target=self._retry_loop, daemon=True, name='acp-cancel-retry')
                self._retry.start()

    def _retry_loop(self):
        while not self.closed.wait(self.cancel_retry_s):
            with self._lock:
                items = list(self.pending_cancels.items())
                if not items:
                    self._retry = None
                    return
            for jid, root in items:
                try:
                    out = self.ops.cancel(root, jid)
                except JoinError as exc:
                    if 'no open job' not in str(exc):
                        continue  # the root did not answer: try again next round
                    out = {}
                if not out.get('pending'):
                    with self._lock:
                        self.pending_cancels.pop(jid, None)
        with self._lock:
            self._retry = None


def main(argv):
    argparse.ArgumentParser(
        prog='fleetmux acp', description='Serve the running root fleet as an ACP agent on stdin / stdout (for '
        'Omnigent and other ACP clients; docs/omnigent.md). Each prompt becomes one job.').parse_args(argv)
    out = sys.stdout
    for stream in (sys.stdin, out):
        try:
            stream.reconfigure(encoding='utf-8')
        except (AttributeError, ValueError):
            pass
    sys.stdout = sys.stderr  # a stray print never corrupts the JSON-RPC channel
    AcpServer().serve(sys.stdin, out)
    return 0
