"""Scripted agent of tests/test_parent_heals_child.py: the demo actor (fleetmux/demo_actor.py: the Claude Code CLI
stand-in with its Stop hook and transcript) with a scenario script instead of the demo's fixed one.

Test-only. Launched by the harness's ``bin/claude`` (tests/parent_heals_kit.py) as ``python -I <this file> ...``.
Every turn is logged to ``$FLEETMUX_DEMO_ACTOR_LOG`` with the pane's cwd, so a test can read what each agent got.

Cues (``$PHK_CUES``, a JSON list, re-read at every turn): ``{"id", "match": <substring of the cwd>, "when":
"startup" | "work", "needle": <optional substring of the turn's text>, "do": "refuse_ready" | "wall" | "stop_failure", "times": <n>}``. A cue fires at most ``times``
times over all generations of that pane (counted in ``<cues file>.<id>.n``):

* ``refuse_ready``: a startup turn answers a non-READY line (the slot's start fails, as a model that never came up).
* ``wall``: a work turn prints a usage-wall line on the screen and never ends (the CLI sits on the wall).
* ``stop_failure``: a work turn ends with the StopFailure hook (an API error), not the Stop hook.
* ``hold``: a work turn stays in flight for ``seconds`` (default 12) before it answers; the screen it draws then is
  logged once as ``held_screen`` (``cols`` and the feed rows), so a test can check what the daemon could read.

Signals (``$PHK_CUES.signals``, a JSON list ``{"id", "match", "do": "rewall"}``, read every 0.5 s, each applied once
by the panes it matches): ``rewall`` prints newer output and then the same wall line again in the turn on screen.

Roles: a pane whose cwd is a fleet's ``workspace/supervisor`` is a supervisor; a worker pane becomes a sub-fleet
supervisor when the program's role-change prompt ("Your role changed. You are now supervisor") reaches it.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleetmux import demo_actor  # noqa: E402

WALL_LINE = "You've hit your weekly limit · resets 3pm (Europe/Paris)"
HEAL_LINE = re.compile(r'"heal":\s*(\[.*?\])\s*(?:$|\n)', re.S)


def cues():
    path = os.environ.get('PHK_CUES')
    if not path:
        return []
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return []


def fire(cwd, when, text):
    """The cue that fires now for this pane, or None (and counts it)."""
    path = os.environ.get('PHK_CUES')
    for cue in cues():
        if cue.get('when') != when or cue.get('match', '\0') not in cwd or cue.get('needle', '') not in text:
            continue
        count = Path(path + '.' + str(cue['id']) + '.n')
        try:
            used = int(count.read_text())
        except (OSError, ValueError):
            used = 0
        if used >= int(cue.get('times', 1)):
            continue
        count.write_text(str(used + 1))
        return cue
    return None


class Script(demo_actor.Script):
    """Replies by role. The supervisor of any fleet answers with a valid decision JSON; it approves every sub-fleet
    op and copies any ``"heal": [...]`` line a notice offers into its next decision."""

    def __init__(self, role, cwd):
        super().__init__(role, cwd)
        self.sub_supervisor = False
        self.blocks = {}
        self.reported = set()

    @property
    def is_supervisor(self):
        return self.role == 'supervisor' or self.sub_supervisor

    def answer(self, text):
        if 'Your role changed. You are now supervisor' in text:
            self.sub_supervisor = True
        if re.search(r'exactly READY', text):
            return 'READY'
        if '[SYSTEM HANDOFF]' in text or 'handoff note' in text and 'Write' in text:
            return '[scripted] handoff written'
        if self.is_supervisor:
            return self.supervise(text)
        return self.work_reply(text)

    @staticmethod
    def service_mode(text):
        return ('[Request ' in text or 'service decision' in text or 'External request' in text
                or 'service fleet' in text or '"skip"' in text)

    def supervise(self, text):
        heal = None
        m = HEAL_LINE.search(text)
        if m:
            try:
                heal = json.loads(m.group(1))
            except ValueError:
                heal = None
        extra = {'heal': heal} if heal else {}
        op = re.search(r'wants sub-fleet .*?"subfleet": \{"id": "([^"]+)"', text, re.S)  # the op id (v2 strips the header)
        if op:
            extra['subfleet'] = {'id': op.group(1), 'decision': 'approve', 'note': '[scripted] approved'}
        ask = re.search(r'quote ([A-Z]+)', text)
        if ask and self.is_supervisor and not self.role == 'supervisor':
            # a sub-fleet's service supervisor answers a quote request itself (the daemon binds the request id)
            return json.dumps(dict({'status': 'RUNNING', 'dispatch': [], 'skip': [], 'reason': '[scripted] answered',
                                    'answer': [{'result': {'quote': ask.group(1) + ' 100.0'}}]}, **extra))
        if self.service_mode(text) or self.role == 'supervisor' and self.cwd_service():
            return json.dumps(dict({'status': 'RUNNING', 'dispatch': [], 'skip': [], 'answer': [],
                                    'reason': '[scripted] service supervisor'}, **extra), ensure_ascii=False)
        # task mode. A root task: one block M0 for worker-1. A sub-fleet's task (its id from the goal): two blocks,
        # <id>.1 for worker-2 and <id>.2 for worker-1, dispatched together; COMPLETED once every owner reported.
        task = re.search(r'Sub-fleet (M\d+(?:\.\d+)*)', text)
        if task and not self.blocks:
            self.blocks = {task.group(1) + '.1': 'worker-2', task.group(1) + '.2': 'worker-1'}
        blocks = self.blocks or {'M0': 'worker-1'}
        if 'Your reply is not a valid decision JSON' in text and not self.reported:
            self.dispatched = False  # the dispatch was refused (e.g. the tree first): send it again later
        for who in re.findall(r'\[(worker-\d+)(?:\.\d+)? report[\] ]', text):
            self.reported.add(who)
        if self.dispatched and set(blocks.values()) <= self.reported:
            return self.decision('COMPLETED', 'every block reported', completion=100, **extra)
        if 'your first reply hands in the tree only' in text and not self.tree_sent:  # §62: the tree comes first
            self.tree_sent = True
            return self.decision('RUNNING', 'handing in the tree: ' + ', '.join(blocks), completion=5, tree=[
                {'id': b, 'goal': 'one file by ' + w, 'est': {'minutes': 5}, 'owns': [w + '/step.txt'],
                 'accept': w + ' reports the file'} for b, w in blocks.items()], **extra)
        if not self.dispatched and (not self.tree_sent or '[tree] rev' in text):
            self.dispatched = True
            return self.decision('RUNNING', 'dispatching ' + ', '.join(blocks), [
                {'id': b, 'to': w, 'instruction': '[scripted] Write ' + w + '/step.txt with one line and report '
                 'the path.'} for b, w in blocks.items()], 10, **extra)
        return self.decision('RUNNING', 'waiting', **extra)

    def cwd_service(self):
        try:
            cfg = json.loads((self.cwd.parents[1] / 'config.json').read_text())
        except (OSError, ValueError, IndexError):
            return False
        return cfg.get('mode') == 'service'

    def work_reply(self, text):
        own = self.cwd
        own.mkdir(parents=True, exist_ok=True)
        (own / 'step.txt').write_text('[scripted] ' + self.role + ' ' + str(time.time()) + '\n')
        return ('Done: [scripted ' + self.role + '] wrote ' + self.role + '/step.txt\nEvidence: ' + self.role
                + '/step.txt\nNot done: none\nBlocked: none')


class Actor(demo_actor.Actor):
    def __init__(self, argv, log_path=None):
        super().__init__(argv, log_path)
        self.script = Script(self.role, self.cwd)

    def log(self, event, **fields):
        fields.setdefault('cwd', getattr(self, 'cwd', ''))
        super().log(event, **fields)

    def start(self, kind, text, content=None):
        super().start(kind, text, content)
        when = 'startup' if re.search(r'exactly READY', text) else 'work'
        cue = fire(self.cwd, when, text)
        if cue is None:
            return
        self.log('cue', id=cue['id'], do=cue['do'])
        self.turn['cue'] = cue['do']
        if cue['do'] == 'wall':
            self.feed.append(('out', WALL_LINE))
            self.reply_at = float('inf')  # the CLI sits on the wall: the turn never ends
            self.poke()
        elif cue['do'] == 'hold':  # the daemon's lease stays received: its watchdog reads this screen meanwhile
            self.reply_at = time.monotonic() + float(cue.get('seconds') or 12)
            self.hold_logged = False
            self.poke()

    def render(self, term):
        super().render(term)
        if (self.turn or {}).get('cue') == 'hold' and not getattr(self, 'hold_logged', True):
            self.hold_logged = True
            cols = term.size()[1]
            self.log('held_screen', cols=cols, rows=self.feed_lines(cols)[-40:])

    def tick(self):
        super().tick()
        now = time.monotonic()
        if now < getattr(self, 'signal_at', 0):
            return
        self.signal_at = now + 0.5
        path = os.environ.get('PHK_CUES')
        try:
            signals = json.loads(Path(path + '.signals').read_text()) if path else []
        except (OSError, ValueError):
            signals = []
        for sig in signals:
            done = Path(path + '.sig.' + str(sig['id']))
            if sig.get('match', '\0') not in self.cwd or done.exists():
                continue
            done.write_text('1')
            self.log('signal', id=sig['id'], do=sig['do'])
            if sig['do'] == 'rewall':  # newer output, then the same wall line again: a NEW wall (ruling G7)
                self.feed.append(('out', '[scripted] retrying the request after the reset'))
                self.feed.append(('out', WALL_LINE))
                self.poke()

    def finish_turn(self):
        cue = (self.turn or {}).get('cue')
        if cue == 'refuse_ready':
            self.turn['text'] = '[scripted] model unavailable'
            reply = '[scripted] the model did not come up'
            self.feed.append(('out', reply))
            self.row(type='assistant', message={'role': 'assistant', 'model': 'scripted',
                                                'content': [{'type': 'text', 'text': reply}]})
            self.log('turn.reply', reply=reply)
            self.run_hooks('Stop', reply)
            return
        if cue == 'stop_failure':
            self.log('turn.stop_failure')
            self.feed.append(('out', 'API Error: 500 {"type":"error","error":{"type":"api_error"}}'))
            self.run_hooks('StopFailure', '', error='server_error',
                           error_details='API Error: 500 {"type":"error","error":{"type":"api_error"}}')
            return
        super().finish_turn()

    def run_hooks(self, event, reply, **extra):
        payload = dict({'session_id': self.session, 'transcript_path': str(self.transcript), 'cwd': self.cwd,
                        'permission_mode': 'bypassPermissions', 'hook_event_name': event,
                        'stop_hook_active': False, 'last_assistant_message': reply}, **extra)
        self.turn = None
        self.state = 'hooks'
        for entry in (self.settings.get('hooks') or {}).get(event) or []:
            for hook in entry.get('hooks') or []:
                if hook.get('type') != 'command':
                    continue
                if hook.get('asyncRewake'):
                    err = self.transcript.with_name('rewake-%d.err' % os.getpid())
                    proc = self.hook(hook['command'], payload, stdout=demo_actor.subprocess.DEVNULL, stderr=err)
                    if proc:
                        self.rewakes.append((proc, err, hook.get('rewakeMessage') or 'Stop hook feedback:'))
                else:
                    proc = self.hook(hook['command'], payload, stdout=demo_actor.subprocess.PIPE)
                    if proc and event == 'Stop':
                        self.blocking.append((proc, time.monotonic(), float(hook.get('timeout') or 600)))
        if not self.blocking:
            self.state = 'idle'
        self.poke()


def main(argv):
    demo_actor.Actor = Actor  # demo_actor.main builds the actor by this name
    return demo_actor.main(argv)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
