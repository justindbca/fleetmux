"""Harness of tests/test_actor_hook_contract.py: one world per case, the subject CLI on a real PTY.

A ``World`` lives in the case's ``tmp_path``: its own HOME, Claude config dir, run directory and recorder. The
subject is started with exactly the argv ``fleet.claude_argv`` builds for a slot (the config names the CLI: the
demo's ``#!/bin/sh`` launcher of ``fleetmux/demo_actor.py`` for ``fake``, ``$FLEETMUX_CONTRACT_REAL_CLAUDE`` for
``real``), in the slot's agent directory, on a PTY read through ``pyte``. In the ``--settings`` file it wrote, only
each hook's ``command`` is replaced: by ``recorder.py <label> <dir>``, which appends what it got (payload, cwd, env)
to ``<label>.jsonl`` and then does the next step of ``<label>.plan.json`` (print, exit code, wait for a file, sleep).
Teardown ends the subject's own process group and any recorder still running; nothing outside the world is touched.
"""
import fcntl
import json
import os
import re
import shlex
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path

import pyte

from fleetmux import claude_input, fleet

REAL_ENV = 'FLEETMUX_CONTRACT_REAL_CLAUDE'
LAUNCH_PROMPT = 'There is no task yet: reply exactly READY'
TYPED_LINE = '[contract] operator line: reply exactly READY'
DRAFT = 'contract draft text'
ROWS, COLS = 40, 120
LABELS = {'claude-stop': 'stop', 'claude-rewake': 'rewake', 'claude-stop-failure': 'stop-failure',
          'claude-posttool': 'posttool', 'claude-prompt': 'prompt'}
ENV_KEYS = ('CLAUDE_CODE_STOP_HOOK_BLOCK_CAP', 'DISABLE_AUTOUPDATER', 'CLAUDE_PROJECT_DIR', 'HOME')
# Only these reach the real CLI from the test process (its login token, if the operator chose to give one); the
# fake subject never gets them.
PASS_THROUGH = ('ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN')

RECORDER = r'''import fcntl, json, os, signal, sys, time
label, rec = sys.argv[1], sys.argv[2]
KEYS = %r


def note(**kw):
    with open(os.path.join(rec, label + '.jsonl'), 'a') as f:
        f.write(json.dumps(dict(kw, pid=os.getpid(), at=time.time())) + '\n')


def term(*_):
    note(event='terminated')
    os._exit(143)


signal.signal(signal.SIGTERM, term)
raw = sys.stdin.read()
try:
    payload = json.loads(raw or '{}')
except ValueError:
    payload = {'_raw': raw}
plan = os.path.join(rec, label + '.plan.json')
with open(plan + '.lock', 'a+') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        with open(plan) as f:
            steps = json.load(f)
    except (OSError, ValueError):
        steps = []
    step = steps.pop(0) if steps else {}
    with open(plan, 'w') as f:
        json.dump(steps, f)
note(event='call', payload=payload, cwd=os.getcwd(), env={k: os.environ.get(k) for k in KEYS}, step=step)
if step.get('wait_file'):
    while not os.path.exists(step['wait_file']):
        time.sleep(0.1)
if step.get('sleep'):
    time.sleep(step['sleep'])
for stream, key in ((sys.stdout, 'stdout'), (sys.stderr, 'stderr')):
    if step.get(key) is not None:
        stream.write(step[key] if isinstance(step[key], str) else json.dumps(step[key]))
        stream.flush()
note(event='exit', code=step.get('exit', 0))
sys.exit(step.get('exit', 0))
''' % (ENV_KEYS,)

NOT_SEEN = (None, None)


def text(tag):
    return '[contract] %s: reply exactly READY' % tag


def rows(path):
    out = []
    try:
        lines = Path(path).read_text(encoding='utf-8', errors='replace').split('\n')
    except (OSError, TypeError):
        return out
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def content_text(row):
    content = (row.get('message') or {}).get('content')
    if isinstance(content, list):
        content = '\n'.join(p.get('text', '') for p in content if isinstance(p, dict) and p.get('type') == 'text')
    return content


def reply_text(payload):
    """The turn's reply as fleet.claude_stop takes it: ``last_assistant_message``, else the transcript's last
    non-sidechain assistant text."""
    if payload.get('last_assistant_message') is not None:
        return payload['last_assistant_message']
    for row in reversed(rows(payload.get('transcript_path', ''))):
        if row.get('type') == 'assistant' and not row.get('isSidechain'):
            got = content_text(row)
            if got:
                return got
    return ''


def alive(pid):
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:  # a zombie the subject has not reaped yet is gone for the protocol
        state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout
    except OSError:
        return True
    return bool(state.strip()) and not state.strip().startswith('Z')


class Session:
    """The subject on a PTY; ``screen`` is what a terminal would show."""

    def __init__(self, argv, cwd, env):
        self.cwd = Path(cwd)
        self.screen = pyte.Screen(COLS, ROWS)
        self.stream = pyte.ByteStream(self.screen)
        self.lock = threading.Lock()
        self.master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', ROWS, COLS, 0, 0))
        self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, cwd=str(cwd), env=env,
                                     start_new_session=True, close_fds=True)
        os.close(slave)
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.reader.start()

    def read(self):
        while True:
            try:
                data = os.read(self.master, 65536)
            except OSError:
                return
            if not data:
                return
            with self.lock:
                self.stream.feed(data)

    def text(self):
        with self.lock:
            return '\n'.join(self.screen.display)

    def editor(self):
        return claude_input.visible_editor(self.text())

    def state(self):
        view = self.editor()
        return (view.text, view.idle) if view is not None else NOT_SEEN

    def type(self, value):
        for ch in value:  # key by key, as a person types (a burst could read as a paste)
            os.write(self.master, ch.encode('utf-8'))
            time.sleep(0.02)

    def key(self, name):
        os.write(self.master, {'Enter': b'\r', 'Escape': b'\x1b', 'C-u': b'\x15'}[name])

    def close(self):
        if self.proc.poll() is None:
            for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
                try:
                    os.killpg(self.proc.pid, sig)  # the subject's own group (start_new_session): its hooks too
                except ProcessLookupError:
                    break
                try:
                    self.proc.wait(timeout=wait)
                    break
                except subprocess.TimeoutExpired:
                    continue
        try:
            os.close(self.master)
        except OSError:
            pass


class World:
    def __init__(self, tmp_path, subject, real_path=''):
        self.root = Path(tmp_path).resolve()
        self.subject = subject
        self.home, self.config, self.rec = self.root / 'h', self.root / 'claude-config', self.root / 'rec'
        self.run = self.root / 'run'
        for d in (self.home, self.config, self.rec, self.run / 'workspace' / 'worker-1', self.run / 'agents' / 'supervisor',
                  self.root / 'bin'):
            d.mkdir(parents=True, exist_ok=True)
        (self.root / 'recorder.py').write_text(RECORDER)
        if subject == 'fake':
            # the demo's launcher (fleetmux/demo.py World): the scripted actor, never on PATH
            self.cli = str(self.root / 'bin' / 'claude')
            Path(self.cli).write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -I '
                                      + shlex.quote(str(Path(fleet.__file__).with_name('demo_actor.py'))) + ' "$@"\n')
            os.chmod(self.cli, 0o700)
        else:
            assert real_path and os.access(real_path, os.X_OK), '%s=%r is not an executable file' % (REAL_ENV, real_path)
            self.cli = real_path
        self.turn_wait = 20 if subject == 'fake' else 300
        self.quiet = 3 if subject == 'fake' else 30
        self.cfg_path = self.run / 'config.json'
        self.cfg = {'run': str(self.run), 'python': sys.executable, 'claude': self.cli, 'transport_version': 2,
                    'worker_cli': 'claude', 'supervisor_cli': 'claude', 'claude_supervisor_hook_delivery': True}
        self.cfg_path.write_text(json.dumps(self.cfg))
        # Pre-answered first-run state for the real CLI's own config dir (the fake ignores it): onboarding done,
        # the agent directories trusted, external CLAUDE.md imports declined (as fleet.trust_claude_dir does).
        projects = {str(self.run / 'workspace' / 'worker-1'): {}, str(self.run / 'agents' / 'supervisor'): {}}
        for entry in projects.values():
            entry.update(hasTrustDialogAccepted=True, hasClaudeMdExternalIncludesApproved=False,
                         hasClaudeMdExternalIncludesWarningShown=True)
        (self.config / '.claude.json').write_text(json.dumps({'hasCompletedOnboarding': True, 'projects': projects}))
        self.env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(self.home), 'TERM': 'xterm-256color',
                    'LANG': 'en_US.UTF-8', 'TMPDIR': str(self.root), 'CLAUDE_CONFIG_DIR': str(self.config),
                    'FLEETMUX_DEMO_TURN_S': '0.2'}
        if subject == 'real':
            self.env.update({k: os.environ[k] for k in PASS_THROUGH if os.environ.get(k)})
        self.sessions = []

    # -- settings: fleet.claude_argv's own, commands swapped for the recorder
    def argv(self, slot, generation=1, stop_timeout=None):
        argv = fleet.claude_argv(self.cfg_path, self.cfg, slot, generation, LAUNCH_PROMPT)
        settings = Path(argv[argv.index('--settings') + 1])
        original = json.loads(settings.read_text())
        rewritten = json.loads(json.dumps(original))
        for entries in rewritten['hooks'].values():
            for entry in entries:
                for hook in entry['hooks']:
                    verb = re.search(r'fleet\.py (claude-[a-z-]+) ', hook['command']).group(1)
                    label = LABELS[verb]
                    hook['command'] = shlex.join([sys.executable, '-I', str(self.root / 'recorder.py'), label,
                                                  str(self.rec)])
                    if stop_timeout and verb == 'claude-stop':
                        hook['timeout'] = stop_timeout
        settings.write_text(json.dumps(rewritten))
        return argv, original, rewritten

    def settings(self, slot):
        _, original, rewritten = self.argv(slot)
        return original, rewritten

    def start(self, slot, stop_timeout=None):
        argv, _, _ = self.argv(slot, stop_timeout=stop_timeout)
        s = Session(argv, fleet.agent_cwd(self.cfg, slot), self.env)
        self.sessions.append(s)
        return s

    def plan(self, label, steps):
        (self.rec / (label + '.plan.json')).write_text(json.dumps(steps))

    # -- what the recorder saw
    def calls(self, label):
        return [r for r in rows(self.rec / (label + '.jsonl')) if r.get('event') == 'call']

    def wait(self, what, pred, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if pred():
                return
            time.sleep(0.1)
        raise AssertionError('timed out after %ss waiting for: %s\n%s' % (timeout, what, self.dump()))

    def wait_calls(self, label, n):
        self.wait('%d %s hook call(s)' % (n, label), lambda: len(self.calls(label)) >= n, self.turn_wait * n)
        return self.calls(label)

    def stays(self, what, pred):
        """``pred`` holds for the whole quiet window (a bounded poll, not a sleep-and-hope)."""
        end = time.monotonic() + self.quiet
        while time.monotonic() < end:
            assert pred(), 'broken: ' + what + '\n' + self.dump()
            time.sleep(0.1)

    def prompts(self, payload, count=10):
        return fleet.claude_user_prompts(payload['transcript_path'], count=count)

    def inside(self, path):
        try:
            return bool(path) and Path(path).resolve().is_relative_to(self.root) and Path(path).is_file()
        except OSError:
            return False

    @staticmethod
    def same_dir(a, b):
        return bool(a) and Path(a).resolve() == Path(b).resolve()

    def dump(self):
        out = ['subject: ' + self.subject]
        for label in sorted(set(LABELS.values())):
            got = rows(self.rec / (label + '.jsonl'))
            if got:
                out.append('%s: %s' % (label, [(r.get('event'), r.get('step')) for r in got]))
        for s in self.sessions:
            out.append('--- screen\n' + s.text().rstrip())
        return '\n'.join(out)

    def close(self):
        for s in self.sessions:
            s.close()
        for label in set(LABELS.values()):  # a recorder that outlived its CLI (it was started by this case)
            got = rows(self.rec / (label + '.jsonl'))
            ended = {r['pid'] for r in got if r.get('event') in ('exit', 'terminated')}
            for r in got:
                if r.get('event') == 'call' and r['pid'] not in ended and alive(r['pid']):
                    try:
                        os.kill(r['pid'], signal.SIGKILL)
                    except OSError:
                        pass
