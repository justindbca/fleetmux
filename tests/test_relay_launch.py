"""Starting a segment from in-repo files: render job.spec.yaml into this machine's start spec and hand it to a
replaceable executor (R4, R9, R24). No test here starts a fleet: the real executor only ever gets a fake runner."""
import json
import subprocess

import pytest

from fleetmux import fleet, lifecycle_deliver
from fleetmux.relay import jobfile, launch
from relay_flow_support import write_job


def _render(tmp_path, **kw):
    if not (tmp_path / '.relay').exists():
        write_job(tmp_path)
    spec = jobfile.load_spec(tmp_path, 'demo', 'M3')
    args = dict(job='demo', seg='M3', epoch=2, worktree=tmp_path, task_branch='relay/demo/M3/e2-desktop',
                deploy_branch=launch.deploy_branch_of('demo', 'M3'), queue=tmp_path / 'q' / 'queue.jsonl',
                relay_python='/opt/py/bin/python')
    args.update(kw)
    return launch.render(spec, **args)


def test_render_fills_local_fields_and_passes_fleetmux_checks(tmp_path):
    out = _render(tmp_path)
    assert set(out) <= set(fleet.JOB_KEYS)
    assert out['lifecycle'] == 'dev_task'
    deliver = lifecycle_deliver.validate_deliver(out['deliver'])
    assert deliver['repo'] == str(tmp_path)
    assert deliver['feature_branch'] == 'relay/demo/M3/e2-desktop'
    assert deliver['deploy_branch'] == 'relay/demo/M3/delivered'
    assert deliver['test_cmd'] == ['/usr/bin/env', 'RELAY_PYTHON=/opt/py/bin/python',
                                   '/bin/bash', '.relay/demo/M3/gate.sh']
    assert 'review_file' not in deliver  # an older job.spec.yaml's key: accepted, not carried
    assert deliver['test_timeout_s'] == 1800
    assert out['roster'] == str(tmp_path / '.relay/demo/M3/roster.yaml')
    assert out['roles'] == str(tmp_path / '.relay/demo/M3/roles')
    assert out['hours'] == 8 and out['artifacts'][0]['path'] == 'notes/integ-1/integration.md'
    assert 'Read .relay/demo/M3/brief.md and do it.' in out['goal']
    assert out['name'] == 'relay-demo-M3-e2'
    assert launch.parse_name(out['name']) == ('demo', 'M3', 2)


def test_render_from_repo_only_needs_no_machine_paths(tmp_path):
    """R4/R24: the job files carry no absolute path; only the fields the program fills do."""
    out = _render(tmp_path)
    filled = {out['roster'], out['roles'], out['deliver']['repo'], out['deliver']['queue'],
              out['deliver']['test_cmd'][1]}
    text = json.dumps(out)
    for value in filled:
        text = text.replace(json.dumps(value)[1:-1], '')
    assert str(tmp_path) not in text


def test_takeover_spec_reads_takeover_md_first(tmp_path):
    out = _render(tmp_path, takeover=True)
    assert out['goal'].startswith('First read .relay/demo/M3/TAKEOVER.md')
    plain = _render(tmp_path)
    assert 'TAKEOVER.md' not in plain['goal']


def test_merge_in_named_in_goal(tmp_path):
    out = _render(tmp_path, merge_in=['aaa111', 'bbb222'])
    assert 'git merge aaa111 bbb222' in out['goal']


def test_roster_and_roles_left_out_when_absent(tmp_path):
    root = write_job(tmp_path)
    (root / 'M3' / 'roster.yaml').unlink()
    for p in (root / 'M3' / 'roles').iterdir():
        p.unlink()
    (root / 'M3' / 'roles').rmdir()
    out = _render(tmp_path)
    assert 'roster' not in out and 'roles' not in out


def test_deploy_branch_of_merge_segment():
    assert launch.deploy_branch_of('demo', 'merge') == 'relay/demo/merged'
    assert launch.deploy_branch_of('demo', 'M1') == 'relay/demo/M1/delivered'


class Runner:
    """Fake subprocess.run: answers by sub-command and records every call."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, argv, input=None, capture_output=True, text=True, timeout=None):
        self.calls.append((list(argv), input))
        code, out = self.answers[argv[1]]
        return subprocess.CompletedProcess(argv, code, out, '')


ATTACHED = ('attached to the running service fleet /r/config.json (no new fleet started); submit the work with: '
            'fleetmux task --config /h/joins/relay-demo-M3-e2.json -\n')


def test_fleetmux_executor_start_attaches_then_submits(tmp_path):
    run = Runner({'status': (0, '{}'), 'start': (0, ATTACHED),
                  'task': (0, json.dumps({'status': 'accepted', 'job_id': 'job-1'}))})
    ex = launch.FleetmuxExecutor(['fleetmux'], root_config='/r/config.json', run=run, timeout_s=5)
    spec = _render(tmp_path)
    handle = ex.start('demo', 'M3', spec)
    assert handle == {'stub': '/h/joins/relay-demo-M3-e2.json', 'job_id': 'job-1', 'name': 'relay-demo-M3-e2',
                      'epoch': 2}
    argvs = [c[0] for c in run.calls]
    assert argvs[0] == ['fleetmux', 'status', '--config', '/r/config.json']
    assert argvs[1] == ['fleetmux', 'start', '--name', 'relay-demo-M3-e2', '--no-view']
    assert argvs[2] == ['fleetmux', 'task', '--config', '/h/joins/relay-demo-M3-e2.json', '-']
    assert json.loads(run.calls[2][1]) == spec


def test_fleetmux_executor_refuses_without_live_root(tmp_path):
    run = Runner({'status': (2, '')})
    ex = launch.FleetmuxExecutor(['fleetmux'], root_config='/r/config.json', run=run, timeout_s=5)
    with pytest.raises(launch.ExecutorError, match='root fleet'):
        ex.start('demo', 'M3', _render(tmp_path))
    assert [c[0][1] for c in run.calls] == ['status']        # never reached `start`


def test_fleetmux_executor_reports_start_that_did_not_attach(tmp_path):
    run = Runner({'status': (0, '{}'), 'start': (0, 'started run x\n')})
    ex = launch.FleetmuxExecutor(['fleetmux'], root_config='/r/config.json', run=run, timeout_s=5)
    with pytest.raises(launch.ExecutorError, match='did not attach'):
        ex.start('demo', 'M3', _render(tmp_path))
    assert [c[0][1] for c in run.calls] == ['status', 'start']


def test_fleetmux_executor_refused_job(tmp_path):
    run = Runner({'status': (0, '{}'), 'start': (0, ATTACHED),
                  'task': (0, json.dumps({'status': 'refused', 'reason': 'no slot'}))})
    ex = launch.FleetmuxExecutor(['fleetmux'], root_config='/r/config.json', run=run, timeout_s=5)
    with pytest.raises(launch.ExecutorError, match='no slot'):
        ex.start('demo', 'M3', _render(tmp_path))


def test_fleetmux_executor_stop_uses_stub():
    run = Runner({'stop': (0, '')})
    ex = launch.FleetmuxExecutor(['fleetmux'], root_config='/r/config.json', run=run, timeout_s=5)
    ex.stop({'stub': '/h/joins/relay-demo-M3-e2.json', 'job_id': 'job-1', 'name': 'relay-demo-M3-e2'})
    assert run.calls[0][0] == ['fleetmux', 'stop', '--config', '/h/joins/relay-demo-M3-e2.json']


def test_fleetmux_executor_lists_running_segments_from_root_status():
    """After a restart the relay finds its sub-fleets in the root's status (jobs + sub-fleet tree) and stops
    one through the child's own config, as `fleetmux stop` on a join stub does."""
    status = {'jobs': [{'id': 'job-1', 'name': 'relay-demo-M3-e2', 'subfleet': 'sf-3', 'state': 'running'},
                       {'id': 'job-2', 'name': 'relay-other-M1-e1', 'subfleet': 'sf-4', 'state': 'running'},
                       {'id': 'job-3', 'name': 'something else', 'subfleet': '', 'state': 'queued'}],
              'subfleets': {'sf-3 (relay-demo-M3-e2)': {'run': '/runs/sf-3', 'state': 'running'}}}
    run = Runner({'status': (0, json.dumps(status, indent=2)), 'stop': (0, '')})
    ex = launch.FleetmuxExecutor(['fleetmux'], root_config='/r/config.json', run=run, timeout_s=5)
    found = ex.running('demo')
    assert found == {'M3': {'stub': None, 'job_id': 'job-1', 'name': 'relay-demo-M3-e2', 'epoch': 2,
                            'child_config': '/runs/sf-3/config.json'}}
    ex.stop(found['M3'])
    assert run.calls[-1][0] == ['fleetmux', 'stop', '--config', '/runs/sf-3/config.json']


def test_fleetmux_executor_has_no_default_runner():
    """The only way to spawn `fleetmux` is a runner passed in: a test cannot start a fleet by omission."""
    with pytest.raises(TypeError):
        launch.FleetmuxExecutor(['fleetmux'], root_config='/r/config.json', timeout_s=5)


def test_fake_executor_records_start_and_stop():
    ex = launch.FakeExecutor()
    h = ex.start('demo', 'M3', {'name': 'relay-demo-M3-e1'})
    assert ex.running('demo') == {'M3': h} and h['epoch'] == 1
    ex.stop(h)
    assert ex.started == [('demo', 'M3', {'name': 'relay-demo-M3-e1'})]
    assert ex.stopped == [h] and ex.running('demo') == {}


# ---------------------------------------------------------------- the dev_task gate's needs (deploy branch, queue)

def _git(repo, *args):
    return subprocess.run(['git', '-C', str(repo)] + list(args), capture_output=True, text=True, check=True)


def test_ensure_deploy_branch_created_at_base_once(tmp_path):
    """The gate fails with `branch missing` unless the deploy branch exists; the program makes it at `base`."""
    repo = tmp_path / 'wt'
    repo.mkdir()
    _git(repo, 'init', '-q', '-b', 'relay/demo/M3/e1-laptop')
    _git(repo, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-q', '--allow-empty', '-m', 'a')
    base = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    _git(repo, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-q', '--allow-empty', '-m', 'b')
    assert launch.ensure_deploy_branch(repo, 'relay/demo/M3/delivered', base, run=subprocess.run) is True
    assert _git(repo, 'rev-parse', 'relay/demo/M3/delivered').stdout.strip() == base
    head = _git(repo, 'rev-parse', 'HEAD').stdout.strip()
    assert launch.ensure_deploy_branch(repo, 'relay/demo/M3/delivered', head, run=subprocess.run) is False
    assert _git(repo, 'rev-parse', 'relay/demo/M3/delivered').stdout.strip() == base      # never moved


def test_queued_delivery_reads_the_gate_queue(tmp_path):
    q = tmp_path / 'queue.jsonl'
    assert launch.queued_delivery(q, 'relay/demo/M3/delivered') is None
    q.write_text('not json\n' + json.dumps({'status': 'pending_approval', 'sha': 'aaa',
                                            'deploy_branch': 'relay/demo/M0/delivered'}) + '\n'
                 + json.dumps({'status': 'pending_approval', 'sha': 'bbb', 'feature_branch': 'f',
                               'deploy_branch': 'relay/demo/M3/delivered'}) + '\n')
    assert launch.queued_delivery(q, 'relay/demo/M3/delivered')['sha'] == 'bbb'


def test_goal_tells_every_slot_where_to_keep_progress(tmp_path):
    """The relay reads the newest <segment dir>/notes/<slot>/PROGRESS.md for checkpoints and TAKEOVER.md, so the
    goal tells the sub-fleet's slots to keep and commit it there."""
    from fleetmux.relay import progress
    goal = _render(tmp_path)['goal']
    assert '.relay/demo/M3/notes/<slot>/PROGRESS.md' in goal
    wt = tmp_path / 'wt'
    note = wt / '.relay/demo/M3/notes/dev-1/PROGRESS.md'
    note.parent.mkdir(parents=True)
    note.write_text('## Done\n')
    assert progress.find(wt, '.relay/demo/M3') == '.relay/demo/M3/notes/dev-1/PROGRESS.md'


def test_render_pins_the_queue_delivery_whatever_the_default(tmp_path, monkeypatch):
    """The relay judges 'delivered' from the queue file, so its spec says via: queue; with the fleet default at
    rollout, the gate on that spec still moves the deploy branch and appends one queue row, and never submits."""
    import sys
    import yaml
    from fleetmux import fleet_config
    user = tmp_path / 'private-fleet.yaml'
    user.write_text(yaml.safe_dump({'deliver': {'via': 'rollout'}}))
    monkeypatch.setenv(fleet_config.USER_ENV, str(user))
    monkeypatch.setenv('ROLLOUT_TARGETS', str(tmp_path / 'no-registry.yaml'))
    (tmp_path / 'job').mkdir()
    out = _render(tmp_path / 'job')
    assert out['deliver']['via'] == 'queue'
    assert lifecycle_deliver.validate_deliver(out['deliver'])['via'] == 'queue'
    repo = tmp_path / 'repo'
    repo.mkdir()

    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True,
                              text=True).stdout.strip()
    git('init', '-q', '-b', 'main')
    git('config', 'user.email', 't@example.org'); git('config', 'user.name', 't')
    (repo / 'a').write_text('a\n'); git('add', '.'); git('commit', '-qm', 'base')
    git('branch', out['deliver']['deploy_branch'])
    git('checkout', '-qb', out['deliver']['feature_branch'])
    (repo / 'a').write_text('b\n'); git('commit', '-qam', 'work')
    spec = dict(out['deliver'], repo=str(repo), test_cmd=[sys.executable, '-c', 'pass'])
    rec = lifecycle_deliver.deliver(lifecycle_deliver.validate_deliver(spec), tmp_path / 'ws', task_id='M3',
                                    run=str(tmp_path / 'run'))
    assert rec['ok'], rec.get('problem')
    sha = git('rev-parse', 'HEAD')
    assert git('rev-parse', out['deliver']['deploy_branch']) == sha
    rows = [json.loads(x) for x in open(out['deliver']['queue'], encoding='utf-8').read().splitlines()]
    assert [r['sha'] for r in rows] == [sha] and rec['queue_entry']['sha'] == sha
    assert 'rollout' not in rec
