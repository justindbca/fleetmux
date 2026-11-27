"""dev_task delivery through the local rollout (docs/design.md section 70): deliver.via, the registry lookup, the
rebase -> test -> submit order, the origin file and the request id. The submit client is a fake script named by a
temporary registry; no test runs the real rollout or touches a rollout data directory."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import test_fleet  # noqa: F401  (puts the checkout on sys.path)
from fleetmux import fleet_config, lifecycle_deliver, lifecycle_recheck

FAKE = r'''
import json, os, sys
log = os.environ["FAKE_LOG"]
argv = sys.argv[1:]
row = {"argv": argv, "cwd": os.getcwd(), "targets": os.environ.get("ROLLOUT_TARGETS"),
       "nobytecode": os.environ.get("PYTHONDONTWRITEBYTECODE")}
if "--origin-file" in argv:
    row["origin"] = json.load(open(argv[argv.index("--origin-file") + 1]))
if "--commit-message-file" in argv:
    row["commit_message"] = open(argv[argv.index("--commit-message-file") + 1]).read()
with open(log, "a") as fh:
    fh.write(json.dumps(row) + "\n")
if os.environ.get("FAKE_SLEEP"):
    import time
    time.sleep(float(os.environ["FAKE_SLEEP"]))
rc = int(os.environ.get("FAKE_RC", "0"))
if rc:
    print("rollout: rejected: " + os.environ.get("FAKE_ERR", "no"), file=sys.stderr)
    sys.exit(rc)
rid = argv[argv.index("--request-id") + 1]
reply = os.environ.get("FAKE_REPLY") or json.dumps({"status": "enqueued", "request_id": rid, "digest": "d"})
print("noise line")
print(reply)
'''


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A live repository on main and a feature worktree of it (the shape of fleetmux and fleetmux-ship-local)."""
    live = tmp_path / 'live'
    live.mkdir()
    git(live, 'init', '-q', '-b', 'main')
    git(live, 'config', 'user.email', 't@example.org'); git(live, 'config', 'user.name', 't')
    (live / 'a.txt').write_text('base\n')
    (live / 'b.txt').write_text('b\n')
    git(live, 'add', '.'); git(live, 'commit', '-qm', 'base')
    feat = tmp_path / 'feat'
    git(live, 'worktree', 'add', '-q', '-b', 'feat/x', str(feat))
    (feat / 'a.txt').write_text('feature\n')
    git(feat, 'commit', '-qam', 'feat: change a')
    return {'live': live, 'feat': feat}


@pytest.fixture
def site(tmp_path, repo, monkeypatch):
    """A temporary registry with one kind: git target on the live repository and a fake site.client."""
    fake = tmp_path / 'fake_client.py'
    fake.write_text(FAKE)
    log = tmp_path / 'submit.jsonl'
    client_cwd = tmp_path / 'rollout-checkout'
    client_cwd.mkdir()
    reg = tmp_path / 'registry.yaml'
    doc = {'site': {'client': {'cwd': str(client_cwd), 'argv': [sys.executable, str(fake)]}},
           'targets': {'other-files': {'kind': 'files', 'root': str(tmp_path)},
                       'fm-main': {'kind': 'git', 'root': str(repo['live']), 'live_branch': 'main'}}}
    reg.write_text(yaml.safe_dump(doc))
    monkeypatch.setenv('ROLLOUT_TARGETS', str(reg))
    monkeypatch.setenv('FAKE_LOG', str(log))
    monkeypatch.setattr(lifecycle_deliver, '_host', lambda: ('box', 'inventory'))
    return {'registry': reg, 'doc': doc, 'log': log, 'cwd': client_cwd}


def calls(site):
    return [json.loads(x) for x in site['log'].read_text().splitlines()] if site['log'].exists() else []


def write_registry(site, doc):
    site['registry'].write_text(yaml.safe_dump(doc))


def spec(repo, tmp_path, **extra):
    out = {'repo': str(repo['feat']), 'feature_branch': 'feat/x', 'deploy_branch': 'main',
           'test_cmd': [sys.executable, '-c', 'print("3 passed")'], 'via': 'rollout',
           'brief': str(tmp_path / 'brief.md'), 'rollout': {'verify': 'scripts/rollout_check_merge.py'}}
    out.update(extra)
    return out


def job_run(tmp_path, job='job-0123456789', name='ship-local'):
    run = tmp_path / 'run'
    (run / 'workspace').mkdir(parents=True)
    cfg = {'run': str(run), 'job': job, 'root_config': str(tmp_path / 'root' / 'config.json'),
           'roster': str(tmp_path / 'roster.yaml'), 'roles': str(tmp_path / 'roles')}
    (run / 'config.json').write_text(json.dumps(cfg))
    (run / 'subfleet.json').write_text(json.dumps({'name': name}))
    return run


def private_config(tmp_path, monkeypatch, block):
    user = tmp_path / 'private-fleet.yaml'
    user.write_text(yaml.safe_dump({'deliver': block}))
    monkeypatch.setenv(fleet_config.USER_ENV, str(user))


# ------------------------------------------------------------------ the switch
def test_shipped_default_is_rollout_and_spec_via_wins(tmp_path, repo, monkeypatch):
    assert fleet_config.deliver()['via'] == 'rollout'
    base = spec(repo, tmp_path)
    del base['via']
    assert lifecycle_deliver.validate_deliver(base)['via'] == 'rollout'
    queued = dict(base, via='queue', queue=str(tmp_path / 'q.jsonl'))
    assert lifecycle_deliver.validate_deliver(queued)['via'] == 'queue'
    private_config(tmp_path, monkeypatch, {'via': 'queue'})
    assert fleet_config.deliver()['via'] == 'queue'
    assert lifecycle_deliver.validate_deliver(dict(base, queue=str(tmp_path / 'q.jsonl')))['via'] == 'queue'
    assert lifecycle_deliver.validate_deliver(dict(base, via='rollout'))['via'] == 'rollout'


def test_rollout_spec_needs_no_queue_but_brief_and_verify(tmp_path, repo):
    ok = lifecycle_deliver.validate_deliver(spec(repo, tmp_path))
    assert 'queue' not in ok and ok['rollout']['verify'] == 'scripts/rollout_check_merge.py'
    for bad, why in ((dict(spec(repo, tmp_path), brief='rel/brief.md'), 'brief'),
                     ({k: v for k, v in spec(repo, tmp_path).items() if k != 'brief'}, 'brief'),
                     (dict(spec(repo, tmp_path), rollout={}), 'verify'),
                     (dict(spec(repo, tmp_path), rollout={'verify': 'x', 'push': 1}), 'push'),
                     (dict(spec(repo, tmp_path), via='github'), 'via'),
                     (dict(spec(repo, tmp_path), test_cmd=['x'] * 51), 'test_cmd'),
                     (dict(spec(repo, tmp_path), test_cmd=['python', '-c', 'a\nb']), 'test_cmd'),
                     (dict(spec(repo, tmp_path), refix={'root': 'r', 'attempt': 99, 'previous': None,
                                                        'evidence': []}), 'attempt')):
        with pytest.raises(ValueError, match=why):
            lifecycle_deliver.validate_deliver(bad)
    with pytest.raises(ValueError, match='queue'):
        lifecycle_deliver.validate_deliver(dict(spec(repo, tmp_path), via='queue'))


def test_bad_private_deliver_block_is_refused(tmp_path, monkeypatch):
    private_config(tmp_path, monkeypatch, {'via': 'ftp'})
    with pytest.raises(ValueError, match='deliver.via'):
        fleet_config.deliver()
    private_config(tmp_path, monkeypatch, {'via': 'rollout', 'extra': 1})
    with pytest.raises(ValueError, match='deliver'):
        fleet_config.deliver()


# ------------------------------------------------------------------ rebase -> test -> submit
def test_rebases_onto_main_tests_the_rebased_sha_and_submits_it(tmp_path, repo, site):
    live, feat = repo['live'], repo['feat']
    (live / 'b.txt').write_text('main moved\n')
    git(live, 'commit', '-qam', 'main: change b')
    main_before = git(live, 'rev-parse', 'main')
    tested = tmp_path / 'tested-sha'
    run = job_run(tmp_path)
    s = spec(repo, tmp_path, test_cmd=[sys.executable, '-c',
                                       'import subprocess; open(%r, "w").write(subprocess.run(["git", "rev-parse", '
                                       '"HEAD"], capture_output=True, text=True).stdout.strip())' % str(tested)])
    rec = lifecycle_deliver.deliver(s, run / 'workspace', task_id='M56', run=str(run))
    assert rec['ok'], rec.get('problem')
    tip = git(feat, 'rev-parse', 'feat/x')
    assert git(feat, 'merge-base', '--is-ancestor', main_before, tip) == ''  # rebased onto the newest main
    assert tested.read_text() == tip == rec['sha']                           # the tested sha is the submitted sha
    assert git(live, 'rev-parse', 'main') == main_before                     # main is not moved by fleetmux
    assert rec['steps'] == ['rebased', 'base_check', 'tests', 'submitted']
    [call] = calls(site)
    argv = call['argv']
    assert argv[0] == 'submit'
    assert argv[argv.index('--sha') + 1] == tip
    assert argv[argv.index('--target') + 1] == 'fm-main'
    assert argv[argv.index('--repo') + 1] == str(feat)
    assert argv[argv.index('--verify') + 1] == 'scripts/rollout_check_merge.py'
    rid = 'job-0123456789-' + tip[:12]
    assert argv[argv.index('--request-id') + 1] == rid
    assert call['cwd'] == os.path.realpath(site['cwd'])
    assert call['targets'] == str(site['registry']) and call['nobytecode'] == '1'
    assert call['commit_message'].startswith('feat: change a')
    assert argv[argv.index('--session') + 1] == 'job-0123456789'
    assert rec['rollout'] == {'target': 'fm-main', 'request_id': rid, 'status': 'enqueued',
                              'registry': str(site['registry'])}
    assert not list(tmp_path.rglob('*.jsonl*')) or [p.name for p in tmp_path.rglob('*.jsonl')] == ['submit.jsonl']


def test_origin_carries_job_brief_branch_host_test_cmd_verify_and_a_fresh_refix_chain(tmp_path, repo, site):
    run = job_run(tmp_path)
    s = spec(repo, tmp_path)
    rec = lifecycle_deliver.deliver(s, run / 'workspace', task_id='M56', run=str(run))
    assert rec['ok'], rec.get('problem')
    origin = calls(site)[0]['origin']
    rid = rec['rollout']['request_id']
    assert origin == {'v': 1, 'job': 'job-0123456789', 'task': 'M56', 'brief': s['brief'], 'branch': 'feat/x',
                      'repo': str(repo['feat']), 'host': 'box', 'host_source': 'inventory',
                      'root_config': str(tmp_path / 'root' / 'config.json'), 'test_cmd': s['test_cmd'],
                      'verify': 'scripts/rollout_check_merge.py', 'name': 'ship-local',
                      'roster': str(tmp_path / 'roster.yaml'), 'roles': str(tmp_path / 'roles'),
                      'refix': {'root': rid, 'attempt': 0, 'previous': None, 'evidence': []}}
    files = sorted(p.name for p in (run / 'workspace' / '.deliver').iterdir())
    assert files == ['commit-' + rid + '.txt', 'origin-' + rid + '.json']


def test_refix_chain_from_the_spec_is_carried_as_written(tmp_path, repo, site):
    run = job_run(tmp_path, job='job-abcdefabcd')
    chain = {'root': 'job-0123456789-aaaaaaaaaaaa', 'attempt': 1, 'previous': 'job-0123456789-aaaaaaaaaaaa',
             'evidence': [str(tmp_path / 'ev.md')]}
    rec = lifecycle_deliver.deliver(spec(repo, tmp_path, refix=chain), run / 'workspace', task_id='M60', run=str(run))
    assert rec['ok'], rec.get('problem')
    origin = calls(site)[0]['origin']
    assert origin['refix'] == chain and origin['job'] == 'job-abcdefabcd' and origin['task'] == 'M60'


def test_request_id_without_a_job_is_stable_per_run_and_distinct_across_runs(tmp_path, repo, site):
    sha = 'f' * 40
    a = lifecycle_deliver.request_id(None, 'M56', '/runs/a', sha)
    assert a == 'M56-' + hashlib.sha256(b'/runs/a').hexdigest()[:10] + '-' + 'f' * 12
    assert a == lifecycle_deliver.request_id(None, 'M56', '/runs/a', sha)
    assert a != lifecycle_deliver.request_id(None, 'M56', '/runs/b', sha)
    assert lifecycle_deliver.request_id('job-0123456789', 'M56', '/runs/a', sha) == 'job-0123456789-' + 'f' * 12
    long = lifecycle_deliver.request_id(None, 'T/' + 'x' * 200 + ' y', '/runs/a', sha)
    assert len(long) <= 80 and lifecycle_deliver.RID_RE.fullmatch(long) and long.endswith('-' + 'f' * 12)
    # a root fleet's own dev_task (no config.json job): the origin has job null
    run = tmp_path / 'rootrun'
    (run / 'workspace').mkdir(parents=True)
    rec = lifecycle_deliver.deliver(spec(repo, tmp_path), run / 'workspace', task_id='M7', run=str(run))
    assert rec['ok'], rec.get('problem')
    origin = calls(site)[0]['origin']
    assert origin['job'] is None and origin['root_config'] == str(run / 'config.json')
    assert rec['rollout']['request_id'] == lifecycle_deliver.request_id(None, 'M7', str(run), rec['sha'])


def test_exists_counts_as_taken(tmp_path, repo, site, monkeypatch):
    run = job_run(tmp_path)
    first = lifecycle_deliver.deliver(spec(repo, tmp_path), run / 'workspace', task_id='M56', run=str(run))
    rid = first['rollout']['request_id']
    monkeypatch.setenv('FAKE_REPLY', json.dumps({'status': 'exists', 'request_id': rid, 'state': 'QUEUED'}))
    again = lifecycle_deliver.deliver(spec(repo, tmp_path), run / 'workspace', task_id='M56', run=str(run))
    assert again['ok'] and again['rollout']['status'] == 'exists' and again['rollout']['request_id'] == rid


# ------------------------------------------------------------------ blocked, never a queue file
@pytest.mark.parametrize('case', ['no_registry', 'no_client', 'no_target', 'other_repo', 'rejected', 'bad_reply',
                                  'wrong_id', 'named_target_missing', 'two_targets'])
def test_blocked_with_a_reason_and_no_fallback(tmp_path, repo, site, monkeypatch, case):
    doc = json.loads(json.dumps(site['doc']))
    want = {'no_registry': 'registry', 'no_client': 'site.client', 'no_target': 'kind: git',
            'other_repo': 'kind: git', 'rejected': 'exit 1', 'bad_reply': 'status',
            'wrong_id': 'request id', 'named_target_missing': 'nope', 'two_targets': 'more than one'}[case]
    extra = {}
    if case == 'no_registry':
        monkeypatch.setenv('ROLLOUT_TARGETS', str(tmp_path / 'missing.yaml'))
    elif case == 'no_client':
        del doc['site']['client']
    elif case == 'no_target':
        del doc['targets']['fm-main']
    elif case == 'other_repo':
        other = tmp_path / 'other'
        other.mkdir()
        git(other, 'init', '-q', '-b', 'main')
        doc['targets']['fm-main']['root'] = str(other)
    elif case == 'rejected':
        monkeypatch.setenv('FAKE_RC', '1')
        monkeypatch.setenv('FAKE_ERR', 'unknown argument --origin-file')
    elif case == 'bad_reply':
        monkeypatch.setenv('FAKE_REPLY', json.dumps({'status': 'refused', 'msg': 'x'}))
    elif case == 'wrong_id':
        monkeypatch.setenv('FAKE_REPLY', json.dumps({'status': 'enqueued', 'request_id': 'someone-else'}))
    elif case == 'named_target_missing':
        extra = {'rollout': {'verify': 'v.py', 'target': 'nope'}}
    elif case == 'two_targets':
        doc['targets']['fm-main-2'] = dict(doc['targets']['fm-main'])
    write_registry(site, doc)
    run = job_run(tmp_path)
    main_before = git(repo['live'], 'rev-parse', 'main')
    rec = lifecycle_deliver.deliver(spec(repo, tmp_path, **extra), run / 'workspace', task_id='M56', run=str(run))
    assert not rec['ok'] and want in rec['problem'], rec.get('problem')
    assert 'queue_entry' not in rec
    assert git(repo['live'], 'rev-parse', 'main') == main_before
    assert not [p for p in tmp_path.rglob('*.jsonl') if p.name != 'submit.jsonl']
    if case == 'rejected':
        assert 'unknown argument --origin-file' in rec['problem']


def test_registry_comes_from_the_config_list_when_the_env_is_unset(tmp_path, repo, site, monkeypatch):
    monkeypatch.delenv('ROLLOUT_TARGETS')
    private_config(tmp_path, monkeypatch, {'via': 'rollout',
                                           'registries': [str(tmp_path / 'absent.yaml'), str(site['registry'])]})
    assert lifecycle_deliver.registry_path() == site['registry']
    run = job_run(tmp_path)
    rec = lifecycle_deliver.deliver(spec(repo, tmp_path), run / 'workspace', task_id='M56', run=str(run))
    assert rec['ok'], rec.get('problem')
    assert calls(site)[0]['targets'] == str(site['registry'])
    private_config(tmp_path, monkeypatch, {'via': 'rollout', 'registries': [str(tmp_path / 'absent.yaml')]})
    assert lifecycle_deliver.registry_path() is None


def test_rebase_conflict_is_blocked_and_aborted(tmp_path, repo, site):
    live, feat = repo['live'], repo['feat']
    tip = git(feat, 'rev-parse', 'feat/x')
    (live / 'a.txt').write_text('main conflicts\n')
    git(live, 'commit', '-qam', 'main: change a')
    run = job_run(tmp_path)
    rec = lifecycle_deliver.deliver(spec(repo, tmp_path), run / 'workspace', task_id='M56', run=str(run))
    assert not rec['ok'] and 'rebase' in rec['problem'] and 'aborted' in rec['problem']
    assert git(feat, 'rev-parse', 'feat/x') == tip
    assert git(feat, 'status', '--porcelain') == ''
    assert not (Path(git(feat, 'rev-parse', '--git-dir')) / 'rebase-merge').exists()
    assert calls(site) == []


def test_failed_tests_submit_nothing(tmp_path, repo, site):
    run = job_run(tmp_path)
    rec = lifecycle_deliver.deliver(spec(repo, tmp_path, test_cmd=[sys.executable, '-c', 'raise SystemExit(3)']),
                                    run / 'workspace', task_id='M56', run=str(run))
    assert not rec['ok'] and 'tests failed (exit 3)' in rec['problem']
    assert calls(site) == []


# ------------------------------------------------------------------ done_when, recheck
def test_done_when_for_rollout_is_the_request_id(tmp_path, repo, site):
    run = job_run(tmp_path)
    s = spec(repo, tmp_path)
    rec = lifecycle_deliver.deliver(s, run / 'workspace', task_id='M56', run=str(run))
    assert lifecycle_deliver.done_when_problem(s, rec['sha'], rec) is None
    assert lifecycle_deliver.done_when_problem(s, rec['sha'], dict(rec, rollout={})) is not None
    assert lifecycle_recheck.confirmed(rec, s) is rec
    (repo['feat'] / 'a.txt').write_text('more\n')
    git(repo['feat'], 'commit', '-qam', 'more')
    assert not lifecycle_recheck.confirmed(rec, s)['ok']   # the tip moved past the submitted sha


def test_closing_text_names_the_request_id_and_says_not_merged(monkeypatch):
    from types import SimpleNamespace
    from fleetmux import lifecycle_close
    monkeypatch.setenv('FLEETMUX_UI_LANGUAGE', 'en')
    f = SimpleNamespace(cfg={'lifecycle': 'dev_task', 'name': 'ship', 'run': '/runs/r'})
    res = {'status': 'completed', 'task_id': 'M56', 'delivery': {
        'ok': True, 'sha': 'a' * 40, 'rollout': {'target': 'fm-main', 'request_id': 'job-0123456789-' + 'a' * 12,
                                                 'status': 'enqueued', 'registry': '/r.yaml'}}}
    text = lifecycle_close.closing_text(f, {'closed_by': 'auto'}, res, True)
    assert 'request job-0123456789-' + 'a' * 12 in text and 'fm-main' in text and 'not merged yet' in text


def test_a_stored_spec_without_via_stays_a_queue_delivery(tmp_path, repo, site):
    """validate_deliver writes via into every spec; one without it was stored by an older release (a run continued
    across a release switch, a recheck of an old gate record) and keeps the queue delivery under the rollout default."""
    assert fleet_config.deliver()['via'] == 'rollout'
    feat, live = repo['feat'], repo['live']
    old = {'repo': str(feat), 'feature_branch': 'feat/x', 'deploy_branch': 'dep',
           'test_cmd': [sys.executable, '-c', 'pass'], 'queue': str(tmp_path / 'q' / 'queue.jsonl')}
    git(live, 'branch', 'dep', 'main')
    rec = lifecycle_deliver.deliver(old, tmp_path / 'ws', task_id='M1', run=str(tmp_path / 'run'))
    assert rec['ok'], rec.get('problem')
    sha = git(feat, 'rev-parse', 'feat/x')
    assert rec['steps'] == ['fast_forward_check', 'tests', 'merged', 'enqueued'] and 'rollout' not in rec
    assert git(live, 'rev-parse', 'dep') == sha and calls(site) == []
    assert lifecycle_recheck.confirmed(rec, old) is rec


# ------------------------------------------------------------------ supervisor read (M56.7)
def test_squash_message_first_line_is_the_first_subject_as_written(tmp_path, repo, site):
    feat = repo['feat']
    (feat / 'b.txt').write_text('second\n')
    git(feat, 'commit', '-qam', 'fix(x): second change')
    run = job_run(tmp_path)
    rec = lifecycle_deliver.deliver(spec(repo, tmp_path), run / 'workspace', task_id='M56', run=str(run))
    assert rec['ok'], rec.get('problem')
    call = calls(site)[0]
    lines = call['commit_message'].splitlines()
    assert lines[0] == 'feat: change a'                       # no "(+1 more)" in the commit subject
    assert '- fix(x): second change' in lines
    argv = call['argv']
    assert argv[argv.index('--title') + 1] == 'feat: change a (+1 more)'


def test_commit_message_shape_is_checked_before_submit(tmp_path, repo, site):
    for bad in ('\nbody without a title', 'title\x00', 'x' * 20001):
        with pytest.raises(ValueError, match='deliver.rollout.commit_message'):
            lifecycle_deliver.validate_deliver(spec(repo, tmp_path, rollout={'verify': 'v.py', 'commit_message': bad}))
    feat = repo['feat']
    git(feat, 'reset', '-q', '--hard', 'main')
    git(feat, 'commit', '-q', '--allow-empty', '--allow-empty-message', '-m', '')
    run = job_run(tmp_path)
    rec = lifecycle_deliver.deliver(spec(repo, tmp_path), run / 'workspace', task_id='M56', run=str(run))
    assert not rec['ok'] and 'deliver.rollout.commit_message' in rec['problem'], rec.get('problem')
    assert calls(site) == []


def test_submit_timeout_names_the_request_id_and_the_status_check(tmp_path, repo, site, monkeypatch):
    private_config(tmp_path, monkeypatch, {'via': 'rollout', 'submit_timeout_s': 0.5})
    monkeypatch.setenv('FAKE_SLEEP', '5')
    run = job_run(tmp_path)
    rec = lifecycle_deliver.deliver(spec(repo, tmp_path), run / 'workspace', task_id='M56', run=str(run))
    rid = 'job-0123456789-' + rec['sha'][:12]
    assert not rec['ok'] and rid in rec['problem'] and '--status ' + rid in rec['problem'], rec.get('problem')
    assert 'do not recheck' in rec['problem']


def test_a_rerun_after_main_moved_names_the_earlier_request_id(tmp_path, repo, site, monkeypatch):
    run = job_run(tmp_path)
    first = lifecycle_deliver.deliver(spec(repo, tmp_path), run / 'workspace', task_id='M56', run=str(run))
    old = first['rollout']['request_id']
    live = repo['live']
    (live / 'b.txt').write_text('main moved\n')
    git(live, 'commit', '-qam', 'main: change b')
    monkeypatch.setenv('FAKE_RC', '1')
    rec = lifecycle_deliver.deliver(spec(repo, tmp_path), run / 'workspace', task_id='M56', run=str(run))
    assert rec['sha'] != first['sha'] and not rec['ok']
    assert rec['rollout']['earlier_request_ids'] == [old]
    assert old in rec['problem'], rec.get('problem')


def test_restart_labels_or_none_are_passed_to_the_submit(tmp_path, repo, site):
    run = job_run(tmp_path)
    s = spec(repo, tmp_path, rollout={'verify': 'v.py', 'restart': 'none'})
    assert lifecycle_deliver.deliver(lifecycle_deliver.validate_deliver(s), run / 'workspace', task_id='M56',
                                     run=str(run))['ok']
    s = spec(repo, tmp_path, rollout={'verify': 'v.py', 'restart': ['app-daemon', 'app-web']})
    assert lifecycle_deliver.deliver(lifecycle_deliver.validate_deliver(s), run / 'workspace', task_id='M56',
                                     run=str(run))['ok']
    first, second = (c['argv'] for c in calls(site))
    assert first[first.index('--restart'):first.index('--restart') + 2] == ['--restart', 'none']
    assert [second[i + 1] for i, a in enumerate(second) if a == '--restart'] == ['app-daemon', 'app-web']
    assert '--restart' not in calls(site)[0]['argv'][:first.index('--restart')]
    # decisions 40: the origin carries the same value, so a refix task delivers with it again
    assert [c['origin']['restart'] for c in calls(site)] == ['none', ['app-daemon', 'app-web']]
    for bad in ('all', [], ['-x'], ['a b'], [1]):
        with pytest.raises(ValueError, match='deliver.rollout.restart'):
            lifecycle_deliver.validate_deliver(spec(repo, tmp_path, rollout={'verify': 'v.py', 'restart': bad}))
