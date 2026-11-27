"""The replies of the scripted actor of `fleetmux scenario upgrade` (fleetmux/scenario_actor.py).

The prompts are the ones a real run on the fleet sends (service root, job sub-fleet with the decomposition tree on,
job worker). Standard library + git only; no fleet, no model.
"""
import json
import subprocess

from fleetmux import scenario_actor as sa

GOAL = ('Goal: Sub-fleet M0 (delegated by worker-1.1). Original instruction:\n'
        '[fleetmux scenario job] a: commit one step per turn in repo={repo} until {repo}/SCENARIO-RELEASE exists.\n'
        'Workers you can dispatch to: worker-1\n'
        'Decomposition is on for this task: your first reply hands in the tree only ("tree": [...], "dispatch": []); '
        'see "Decomposition tree" in AGENTS.md.\nReply with the JSON format from AGENTS.md.')
ACCEPTED = ('[job job-0123456789] accepted: the program runs it in its own task-mode sub-fleet on a free worker '
            '(1 sub-fleet workers, 2 h). Goal: [fleetmux scenario job] a: commit one step per turn in repo={repo} '
            'until {repo}/SCENARIO-RELEASE exists.')


def repo(tmp_path):
    r = tmp_path / 'job-a'
    r.mkdir()
    git = ['git', '-C', str(r)]
    subprocess.run(git + ['init', '-q', '-b', 'main'], check=True)
    (r / 'README').write_text('x\n')
    subprocess.run(git + ['add', 'README'], check=True)
    subprocess.run(git + sa.GIT_ID + ['commit', '-q', '-m', 'start'], check=True)
    return r


def count(r):
    out = subprocess.run(['git', '-C', str(r), 'rev-list', '--count', 'HEAD'], check=True, capture_output=True,
                         text=True)
    return int(out.stdout) - 1


def test_the_root_supervisor_stays_a_service_supervisor_on_a_job_notice(tmp_path):
    r = repo(tmp_path)
    s = sa.ScenarioScript('supervisor', tmp_path / 'agents' / 'supervisor')
    reply = json.loads(s.answer(ACCEPTED.format(repo=r)))
    assert reply == {'status': 'RUNNING', 'dispatch': [], 'skip': []}


def test_the_job_supervisor_hands_in_a_child_block_then_dispatches_it(tmp_path):
    r = repo(tmp_path)
    s = sa.ScenarioScript('worker-1', tmp_path / 'worker-1')
    tree = json.loads(s.answer(GOAL.format(repo=r)))
    assert tree['dispatch'] == [] and [b['id'] for b in tree['tree']] == ['M0.1']
    first = json.loads(s.answer('[tree] rev 1 accepted'))
    assert first['status'] == 'RUNNING'
    assert [(d['id'], d['to']) for d in first['dispatch']] == [('M0.1', 'worker-1')]
    assert sa.STEP_MARK in first['dispatch'][0]['instruction'] and 'repo=' + str(r) in first['dispatch'][0]['instruction']
    # while the step is out, no second dispatch; after the report, the next one
    assert json.loads(s.answer('a notice'))['dispatch'] == []
    report = '[worker-1 report] Done: [scripted] ' + sa.REPORT_MARK + ' committed step 1 in repo=' + str(r)
    assert [d['id'] for d in json.loads(s.answer(report))['dispatch']] == ['M0.1']


def test_a_rejected_tree_is_handed_in_again(tmp_path):
    r = repo(tmp_path)
    s = sa.ScenarioScript('worker-1', tmp_path / 'worker-1')
    s.answer(GOAL.format(repo=r))
    again = json.loads(s.answer('Your reply is not a valid decision JSON (decompose is on: your first reply hands in '
                                'the tree only ("tree": [...], "dispatch": []); dispatch its blocks in the next reply).'))
    assert again['dispatch'] == [] and again['tree'][0]['id'] == 'M0.1'


def test_the_worker_commits_one_step_per_turn_and_a_new_session_continues(tmp_path):
    r = repo(tmp_path)
    text = sa.STEP_MARK + ' Commit the next step in repo=' + str(r) + ' then report.'
    one = sa.ScenarioScript('worker-1', tmp_path / 'w').answer(text)
    assert 'committed step 1' in one and sa.REPORT_MARK in one
    two = sa.ScenarioScript('worker-1', tmp_path / 'w2').answer(text)  # another session (or release)
    assert 'committed step 2' in two
    assert count(r) == 2
    assert (r / 'step-1.txt').exists() and (r / 'step-2.txt').exists()
    assert (r / sa.PROGRESS).read_text().strip() == '2'


def test_a_released_job_stops_committing_and_completes(tmp_path):
    r = repo(tmp_path)
    (r / sa.RELEASE).write_text('')
    text = sa.STEP_MARK + ' Commit the next step in repo=' + str(r) + ' then report.'
    assert 'released' in sa.ScenarioScript('worker-1', tmp_path / 'w').answer(text)
    assert count(r) == 0
    s = sa.ScenarioScript('worker-1', tmp_path / 'worker-1')
    s.answer(GOAL.format(repo=r))
    # released before any step: one last delivery lets the worker write its report, then COMPLETED declares it
    last = json.loads(s.answer('[tree] rev 1 accepted'))
    assert last['status'] == 'RUNNING' and [d['id'] for d in last['dispatch']] == ['M0.1']
    done = json.loads(s.answer('[worker-1 report] Done: [scripted] ' + sa.REPORT_MARK + ' released, no step '
                               'committed in repo=' + str(r)))
    assert done['status'] == 'COMPLETED' and done['artifacts'] == [{'path': 'worker-1/' + sa.ARTIFACT}]


def test_a_new_session_of_the_job_supervisor_remembers_its_job(tmp_path):
    """After the upgrade the job's sub-fleet is re-adopted by a new session (a new actor process) whose first
    prompts need not repeat the goal: it continues the same job from its own directory's record."""
    r = repo(tmp_path)
    cwd = tmp_path / 'worker-1'
    cwd.mkdir()
    sa.ScenarioScript('worker-1', cwd).answer(GOAL.format(repo=r))
    again = sa.ScenarioScript('worker-1', cwd)
    out = json.loads(again.answer('The run was continued after a restart: carry on with the task.'))
    assert out['status'] == 'RUNNING'
    assert [(d['id'], d['to']) for d in out['dispatch']] == [('M0.1', 'worker-1')]
    assert 'repo=' + str(r) in out['dispatch'][0]['instruction']
    # a root worker or a service supervisor in another directory is not affected
    other = tmp_path / 'ops-1'
    other.mkdir()
    assert json.loads(sa.ScenarioScript('ops-1', other).answer('a service prompt')) == \
        {'status': 'RUNNING', 'dispatch': [], 'skip': []}


def test_a_report_whose_status_line_names_the_step_is_answered_as_the_job_supervisor(tmp_path):
    # seen in a real run: the report delivery lists the worker's last task, which carries the step mark; the job
    # supervisor must answer it with a decision, not commit a step itself
    r = repo(tmp_path)
    s = sa.ScenarioScript('worker-1', tmp_path / 'worker-1')
    s.answer(GOAL.format(repo=r))
    s.answer('[tree] rev 1 accepted')
    report = ('[worker-1 report]\n{"done":["[scripted] ' + sa.REPORT_MARK + ' committed step 1 in repo=' + str(r)
              + '"],"evidence":[],"not_done":"the next step","blocked":"none"}\n\n'
              'Worker status now (state, queue, context, last task):\n'
              '- worker-1: idle, last task: [M0.1] ' + sa.STEP_MARK + ' Commit the next step in repo=' + str(r)
              + '\n\nReply with the JSON format from AGENTS.md.')
    reply = json.loads(s.answer(report))
    assert reply['status'] == 'RUNNING' and [d['id'] for d in reply['dispatch']] == ['M0.1']
    assert count(r) == 0


def test_the_root_supervisor_copies_the_heal_it_is_asked_for(tmp_path):
    s = sa.ScenarioScript('supervisor', tmp_path / 'agents' / 'supervisor')
    held = ('[sub-fleet supervisor held] sub-fleet M0 (/w/runs/x/sub/M0): its supervisor supervisor-1 is held: held '
            '(stale_identity:generation; execution stop unconfirmed) with no progress.\n'
            'To fix it, add this to your next decision (copy it as it is): "heal": [{"subfleet": "M0", "reopen": '
            '"stay"}]\nAnswer with the usual service decision JSON (status RUNNING, dispatch [], skip [])')
    reply = json.loads(s.answer(held))
    assert reply == {'status': 'RUNNING', 'dispatch': [], 'skip': [],
                     'heal': [{'subfleet': 'M0', 'reopen': 'stay'}]}
    # an ordinary turn afterwards carries no heal
    assert json.loads(s.answer('a notice')) == {'status': 'RUNNING', 'dispatch': [], 'skip': []}


def test_a_released_job_waits_for_the_step_out_and_completes_with_the_workers_report(tmp_path):
    # s2run7: the job supervisor claimed COMPLETED while a step was out and without artifacts; the program
    # refused it three times and the job ended blocked
    r = repo(tmp_path)
    ws = tmp_path / 'M0-workspace'
    s = sa.ScenarioScript('worker-1', tmp_path / 'worker-1')
    s.answer(GOAL.format(repo=r))
    s.answer('[tree] rev 1 accepted')  # step 1 dispatched
    worker = sa.ScenarioScript('worker-1', ws / 'worker-1')
    step = sa.STEP_MARK + ' Commit the next step in repo=' + str(r) + ' then report.'
    assert 'committed step 1' in worker.answer(step)
    assert str(r) in (ws / 'worker-1' / sa.ARTIFACT).read_text()
    (r / sa.RELEASE).write_text('')
    wait = json.loads(s.answer('a notice'))
    assert wait['status'] == 'RUNNING' and wait['dispatch'] == []
    refused = json.loads(s.answer('COMPLETED refused: worker-1 (working on a delivery) still have unsettled '
                                  'deliveries. Wait for their reports (RUNNING with an empty dispatch)'))
    assert refused['status'] == 'RUNNING' and refused['dispatch'] == []
    # the step's report: one last delivery for the released report, then COMPLETED with the artifact
    last = json.loads(s.answer('[worker-1 report] Done: [scripted] ' + sa.REPORT_MARK + ' committed step 1 in repo='
                               + str(r)))
    assert last['status'] == 'RUNNING' and [d['id'] for d in last['dispatch']] == ['M0.1']
    released = worker.answer(step)
    assert 'released' in released and count(r) == 1
    assert 'released' in (ws / 'worker-1' / sa.ARTIFACT).read_text()
    done = json.loads(s.answer('[worker-1 report] ' + released))
    assert done['status'] == 'COMPLETED' and done['artifacts'] == [{'path': 'worker-1/' + sa.ARTIFACT}]
