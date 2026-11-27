"""Scripted actor of `fleetmux scenario upgrade`: the demo actor (fleetmux/demo_actor.py) with the scenario's replies.

Started by the scenario's launcher (``<world>/bin/claude``) as ``python -I scenario_actor.py <argv>`` on the pane's PTY,
exactly like the demo actor: the terminal, transcript, hook and editor contract are demo_actor's, unchanged. Only
the replies differ (``ScenarioScript``). No model is ever called.

- Service supervisors (the root, the named sub-fleet ``ops``) do nothing: every turn is the service decision
  ``{"status": "RUNNING", "dispatch": [], "skip": []}``.
- A job's sub-fleet supervisor (its goal starts with ``[fleetmux scenario job]`` and names ``repo=<dir>``) dispatches
  one step at a time to ``worker-1`` and never claims COMPLETED: the job keeps running until the scenario ends it.
- A job worker commits one step per turn in the job's repository: ``step-<n>.txt``, where ``n`` follows the step
  number recorded in the repository's ``SCENARIO-PROGRESS`` file (so a new session, or a new release, continues
  with the next step instead of redoing one).
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # -I leaves the script's directory off sys.path
import demo_actor  # noqa: E402

JOB_MARK = '[fleetmux scenario job]'
STEP_MARK = '[fleetmux scenario step]'
REPORT_MARK = '[fleetmux scenario report]'
PROGRESS = 'SCENARIO-PROGRESS'
RELEASE = 'SCENARIO-RELEASE'
GIT_ID = ['-c', 'user.name=fleetmux scenario', '-c', 'user.email=scenario@fleetmux.invalid']
ARTIFACT = 'scenario-report.md'  # the job worker's report in its own directory: the job's declared artifact
RECORD = '.scenario-job.json'  # in the job supervisor's directory: the job it runs, for its next session
REPO = re.compile(r'repo=(/\S+?)(?=[\s.,;:"\']|$)')
HEAL = re.compile(r'"heal": (\[[^\]]*\])')  # the fleet's own fix for a held sub-fleet supervisor, copied as it is


def commit_step(repo):
    """Write and commit the next step in ``repo``; returns its number."""
    repo = Path(repo)
    try:
        done = int((repo / PROGRESS).read_text(encoding='utf-8').strip() or 0)
    except (OSError, ValueError):
        done = 0
    n = done + 1
    (repo / ('step-%d.txt' % n)).write_text('step %d by %s (pid %d, python %s)\n'
                                            % (n, os.environ.get('FLEETMUX_RELEASE_SHA') or 'scripted actor',
                                               os.getpid(), sys.executable), encoding='utf-8')
    (repo / PROGRESS).write_text(str(n) + '\n', encoding='utf-8')
    for args in (['add', 'step-%d.txt' % n, PROGRESS], GIT_ID + ['commit', '-q', '-m', 'scenario: step %d' % n]):
        subprocess.run(['git', '-C', str(repo)] + args, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=60)
    return n


class ScenarioScript(demo_actor.Script):
    def __init__(self, role, cwd):
        super().__init__(role, cwd)
        self.repo = None
        self.task = None        # the job sub-fleet's task id (``M<n>``): its tree block is ``<task>.1``
        self.outstanding = False  # a step is dispatched and its report has not come back
        if role != 'supervisor':  # a new session of a job's supervisor (after a restart or an upgrade)
            try:
                rec = json.loads((Path(cwd) / RECORD).read_text(encoding='utf-8'))
                self.repo, self.task = rec.get('repo'), rec.get('task')
                self.tree_sent = True  # the tree of this job was handed in by an earlier session
            except (OSError, ValueError, AttributeError):
                pass

    def remember(self):
        try:
            (Path(self.cwd) / RECORD).write_text(json.dumps({'repo': self.repo, 'task': self.task}),
                                                 encoding='utf-8')
        except OSError:
            pass

    @staticmethod
    def service(text=''):
        """The service decision; a ``"heal": [...]`` that the prompt asks for is copied into it, as a supervisor
        would (a sub-fleet supervisor held after a restart needs it)."""
        out = {'status': 'RUNNING', 'dispatch': [], 'skip': []}
        m = HEAL.search(text)
        if m:
            try:
                out['heal'] = json.loads(m.group(1))
            except ValueError:
                pass
        return json.dumps(out)

    def block(self):
        return (self.task or 'M0') + '.1'

    def worker_step(self, text):
        m = REPO.search(text)
        if not m:
            return '[scripted] no repo= in the instruction'
        repo = m.group(1)
        if (Path(repo) / RELEASE).exists():
            reply = ('Done: [scripted] ' + REPORT_MARK + ' released, no step committed in repo=' + repo
                     + '\nEvidence: ' + repo + '/' + RELEASE + '\nNot done: none\nBlocked: none')
        else:
            n = commit_step(repo)
            reply = ('Done: [scripted] ' + REPORT_MARK + ' committed step ' + str(n) + ' in repo=' + repo
                     + '\nEvidence: ' + repo + '/step-' + str(n) + '.txt\nNot done: the next step\nBlocked: none')
        try:  # the artifact the job supervisor declares when it completes the released job
            Path(self.cwd).mkdir(parents=True, exist_ok=True)
            (Path(self.cwd) / ARTIFACT).write_text('# fleetmux scenario job\n\n' + reply + '\n', encoding='utf-8')
        except OSError:
            pass
        return reply

    def job_supervisor(self, text):
        invalid = 'not a valid decision JSON' in text
        if invalid or REPORT_MARK in text:
            self.outstanding = False
        if 'hands in the tree only' in text and (invalid or not self.tree_sent):
            self.tree_sent = True
            return self.decision('RUNNING', 'handing in the tree: one block, ' + self.block(), completion=5, tree=[{
                'id': self.block(), 'goal': 'commit one step per turn in repo=' + self.repo,
                'est': {'minutes': 60}, 'owns': [], 'accept': 'the scenario releases the job'}])
        if (Path(self.repo) / RELEASE).exists():
            # complete only once the worker reported the release (no delivery is left out, and its report is
            # written): the program verifies the declared artifact
            if REPORT_MARK in text and 'released, no step committed' in text:
                return self.decision('COMPLETED', 'released by the scenario; worker-1 reported it', completion=100,
                                     artifacts=[{'path': 'worker-1/' + ARTIFACT}])
            if self.outstanding:
                return self.decision('RUNNING', 'released; waiting for the report of worker-1', completion=90)
        if self.outstanding and 'must dispatch' not in text:
            return self.decision('RUNNING', 'waiting for the report of worker-1', completion=50)
        self.outstanding = True
        return self.decision('RUNNING', 'dispatching the next step to worker-1', [{
            'id': self.block(), 'to': 'worker-1',
            'instruction': STEP_MARK + ' Commit the next step in repo=' + self.repo + ' then report.'}], 50)

    def answer(self, text):
        if re.search(r'exactly READY', text):
            return 'READY'
        if '[SYSTEM HANDOFF]' in text:
            return '[scripted] handoff not used in the scenario'
        if self.role == 'supervisor':  # the root's own supervisor (a service fleet): never runs a job itself
            return self.service(text)
        # a job worker: one step per turn. A report delivery to the job supervisor also names the step (as the
        # worker's last task), so a text carrying a report is never a step.
        if STEP_MARK in text and REPORT_MARK not in text:
            return self.worker_step(text)
        if JOB_MARK in text or REPORT_MARK in text:
            m = REPO.search(text)
            if m:
                self.repo = m.group(1)
            t = re.search(r'Sub-fleet (M[0-9.]+)', text)
            if t:
                self.task = t.group(1)
            if t:  # a (new) job handed to this session: its tree comes first
                self.tree_sent, self.outstanding = False, False
            if m or t:
                self.remember()
        if self.repo is not None:  # a job's sub-fleet supervisor (task mode)
            return self.job_supervisor(text)
        return self.service(text)


def main(argv):
    demo_actor.Script = ScenarioScript
    demo_actor.GOAL_MARK = JOB_MARK
    return demo_actor.main(argv)


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
