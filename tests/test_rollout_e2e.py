"""Cross-repository end to end (docs/design.md section 70): a dev_task delivery through the real rollout.

The rollout side is an exported rollout checkout (``git archive <sha>`` into a directory) named by
``$FLEETMUX_ROLLOUT_E2E``; without it the test is skipped, so this repository never depends on a path of one machine.
The test uses that export's own test harness (``tests/relinstall_follow_git_world.py``): a temporary live repository
with a squash-batching git target, a release install target that follows it (shell scripts as install commands) and a
github_release target, all under one temporary data root. A temporary registry names them, with ``site.client``
pointing at the export.

The pairing test: the same delivery, then the install's health check fails and the export's own refix pass
(``rollout.refix.run``) sends the refix task through a fake ``task_cmd``; the spec it receives must be one fleetmux
accepts as a job (``fleet.JOB_KEYS``, ``lifecycle_deliver.validate_deliver``).

The chain: lifecycle_deliver submits the feature branch through the real submit client (``--origin-file``; its
``ssh`` is a local stand-in that runs the registry's rpc command on this machine) -> the rollout executors merge it into
main -> the release install target enqueues and finishes ``install-<sha12>`` -> a version raise enqueues the GitHub
release request (``gh`` is never run: the harness answers it). Nothing outside the test's temporary directory and the
export is read or written; no network."""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import test_fleet  # noqa: F401  (puts the checkout on sys.path)
from fleetmux import lifecycle_deliver

ENV = 'FLEETMUX_ROLLOUT_E2E'
EXPORT = os.environ.get(ENV)
pytestmark = pytest.mark.skipif(not EXPORT, reason='set ' + ENV + ' to an exported rollout checkout (git archive)')

REL = {'target': 'rel', 'version_from': {'file': 'rust/Cargo.toml', 'toml_key': 'workspace.package.version'},
       'request_id': 'app-{version}'}
SSH = '''#!/bin/sh
# Local stand-in for ssh: drop the options and the host, run the remote command here (stdin passes through).
while [ $# -gt 1 ]; do case "$1" in -o) shift 2;; -T) shift;; *) break;; esac; done
shift
exec /bin/sh -c "$1"
'''


@pytest.fixture
def world(tmp_path, monkeypatch):
    export = Path(EXPORT).resolve()
    monkeypatch.setattr(sys, 'dont_write_bytecode', True)   # nothing is written into the export
    monkeypatch.syspath_prepend(str(export / 'tests'))
    monkeypatch.syspath_prepend(str(export))
    for key, value in (('GIT_CONFIG_GLOBAL', '/dev/null'), ('GIT_CONFIG_NOSYSTEM', '1'), ('GIT_AUTHOR_NAME', 't'),
                       ('GIT_AUTHOR_EMAIL', 't@t'), ('GIT_COMMITTER_NAME', 't'), ('GIT_COMMITTER_EMAIL', 't@t'),
                       ('PYTHONDONTWRITEBYTECODE', '1')):
        monkeypatch.setenv(key, value)
    for key in ('ROLLOUT_CONFIG', 'ROLLOUT_NOTIFY_DIR', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID'):
        monkeypatch.delenv(key, raising=False)   # the rollout's notifier finds no credentials: nothing is sent
    from relinstall_follow_git_world import Pair, ri_spec
    from rollout_world import SITE_DOC
    from test_rollout_batch import full_test
    from test_rollout_git_exec import EXEC_SPEC
    from test_rollout_git_target import g
    from test_rollout_github_release import spec as gh_spec

    from rollout import plugins
    from rollout import targets as tg
    from rollout.plugins.builtin import telegram as btg

    def no_telegram(*a, **k):
        raise AssertionError('the end to end test must not build a real Telegram notifier')
    monkeypatch.setattr(btg, 'telegram_direct', no_telegram)
    monkeypatch.setattr(tg, 'default_disabled_lister', lambda: set(), raising=False)
    plugins.activate(plugins.Site(SITE_DOC))

    p = Pair(tmp_path, release=REL)               # live repo on feature/live, main at the base, data root, scripts
    fake = tmp_path / 'fakebin'
    fake.mkdir()
    (fake / 'ssh').write_text(SSH)
    (fake / 'ssh').chmod(0o755)
    monkeypatch.setenv('PATH', str(fake) + os.pathsep + os.environ['PATH'])
    secrets = tmp_path / 'no-secrets.env'
    secrets.write_text('')
    registry = tmp_path / 'registry.yaml'
    git_spec = dict(EXEC_SPEC, main_branch='main', root=str(p.x.repo),
                    full_test=full_test(tmp_path / 'g', 'tasks/fails', merge_style='squash'))
    registry.write_text(yaml.safe_dump({
        'site': {'data_root': str(p.x.root), 'secrets_env': str(secrets),
                 'client': {'cwd': str(export), 'argv': [sys.executable, '-m', 'rollout']},
                 'remote': {'host': 'local', 'cmd': sys.executable + ' -m rollout rpc', 'interpreter': sys.executable}},
        'targets': {'gitlive': git_spec,
                    'inst': ri_spec(str(p.bin), str(tmp_path), {'git_target': 'gitlive', 'release': REL}),
                    'rel': gh_spec(SimpleNamespace(pub=tmp_path / 'pub.git', src=p.x.repo, tmp=tmp_path),
                                   source={'root': str(p.x.repo), 'branch': 'main'})}}))
    monkeypatch.setenv('ROLLOUT_TARGETS', str(registry))
    monkeypatch.setattr(lifecycle_deliver, '_host', lambda: ('box', 'inventory'))
    feat = tmp_path / 'feat'
    g(p.x.repo, 'worktree', 'add', '-q', '-b', 'feat/x', str(feat), p.base)
    run = tmp_path / 'run'
    (run / 'workspace').mkdir(parents=True)
    (run / 'config.json').write_text(json.dumps({'job': 'job-0123456789', 'root_config': str(tmp_path / 'root.json')}))
    return SimpleNamespace(p=p, g=g, feat=feat, run=run, registry=registry, tmp=tmp_path)


def commit(w, files, message):
    for rel, text in files.items():
        path = w.feat / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    w.g(w.feat, 'add', '-A')
    w.g(w.feat, 'commit', '-q', '-m', message)


def deliver(w, restart='none'):
    block = {'verify': 'checks/v_ok.py'}
    if restart is not None:
        block['restart'] = restart
    spec = lifecycle_deliver.validate_deliver({
        'repo': str(w.feat), 'feature_branch': 'feat/x', 'deploy_branch': 'main',
        'test_cmd': [sys.executable, '-c', 'pass'], 'via': 'rollout', 'brief': str(w.tmp / 'brief.md'),
        'rollout': block})
    return lifecycle_deliver.deliver(spec, w.run / 'workspace', task_id='M56', run=str(w.run))


def merge_and_install(w, rid):
    """The rollout side: approve the member (this git target has no auto approval), batch-merge it into main, then let
    the release install target follow main."""
    p = w.p
    p.x.tick()
    assert p.x.item(rid)['state'] == 'WAIT_USER'
    p.x.approve(rid)
    p.x.tick()
    sha = p.merge([rid])
    assert p.main() == sha
    p.ri_ticks(4)
    return sha


def test_delivery_is_merged_installed_and_a_version_raise_enqueues_the_release(world):
    w, p = world, world.p
    from relinstall_follow_git_world import cargo
    commit(w, {'rust/Cargo.toml': cargo('0.3.0'), 'tasks/x.md': 'x\n'}, 'feat(app): raise the version to 0.3.0')
    rec = deliver(w)
    assert rec['ok'], rec.get('problem')
    rid = 'job-0123456789-' + rec['sha'][:12]
    assert rec['rollout'] == {'target': 'gitlive', 'request_id': rid, 'status': 'enqueued', 'registry': str(w.registry)}
    assert p.main() == p.base                                     # fleetmux never moves main
    item = p.x.item(rid)
    origin = json.loads((w.run / 'workspace' / '.deliver' / ('origin-' + rid + '.json')).read_text())
    assert item['origin'] == origin and origin['job'] == 'job-0123456789' and origin['verify'] == 'checks/v_ok.py'
    assert item['commit_message'] == 'feat(app): raise the version to 0.3.0\n' and item['restart_none'] is True

    sha = merge_and_install(w, rid)
    inst = p.inst('install-' + sha[:12])
    assert inst['state'] == 'DONE' and inst['from_requests'] == [rid] and inst['origins'] == [origin]
    assert p.marks()[:2] == ['install ' + sha, 'activate ' + sha + ' ' + p.base]
    assert inst['release']['bumped'] is True and inst['release']['to'] == '0.3.0'
    rel = p.rel('app-0.3.0')
    assert rel['state'] == 'WAIT_USER' and rel['sha'] == sha and rel['origins'] == [origin]
    assert rel['from_requests'] == [rid] and len(p.rels()) == 1
    assert p.gh_calls == []                                       # gh is never run


def test_no_version_raise_installs_without_a_release(world):
    w, p = world, world.p
    commit(w, {'tasks/x.md': 'x\n'}, 'docs(tasks): add x')
    rec = deliver(w)
    assert rec['ok'], rec.get('problem')
    sha = merge_and_install(w, rec['rollout']['request_id'])
    inst = p.inst('install-' + sha[:12])
    assert inst['state'] == 'DONE' and inst['release']['bumped'] is False
    assert p.rels() == [] and p.gh_calls == []


FAKE_TASK = '''import json, sys
with open(sys.argv[1], "a") as f:
    f.write(json.dumps({"argv": sys.argv[2:], "spec": json.loads(sys.stdin.read())}) + "\\n")
print(json.dumps({"status": "accepted", "job_id": "job-00000000aa", "task_id": "job-00000000aa"}))
'''


@pytest.mark.parametrize('restart', ['none', None])
def test_refix_spec_built_by_the_rollout_from_a_written_origin_is_a_valid_job(world, restart):
    """Decisions 40 / 52: the origin fleetmux writes -> install rolled back -> the rollout's refix spec -> a job spec
    fleetmux accepts, with the chain one attempt further and the delivery's restart carried."""
    from fleetmux import fleet
    from rollout import refix
    from rollout import targets as tg
    w, p = world, world.p
    commit(w, {'tasks/x.md': 'x\n'}, 'docs(tasks): add x')
    rec = deliver(w, restart=restart)
    assert rec['ok'], rec.get('problem')
    rid = rec['rollout']['request_id']
    origin = json.loads((w.run / 'workspace' / '.deliver' / ('origin-' + rid + '.json')).read_text())
    assert ('restart' in origin) is (restart is not None) and origin.get('restart') == restart
    assert p.x.item(rid)['origin'] == origin                     # the rollout took it as written
    p.x.tick()
    p.x.approve(rid)
    p.x.tick()
    sha = p.merge([rid])
    p.health_fails()
    p.ri_ticks(5)
    assert p.inst('install-' + sha[:12])['state'] == 'ROLLED_BACK'
    script = w.tmp / 'fake_task.py'
    script.write_text(FAKE_TASK)
    calls = w.tmp / 'calls.jsonl'
    cfg = tg.check_refix({'enabled': True, 'host': 'box', 'task_cmd': [
        sys.executable, str(script), str(calls), 'task', '--job', '--config', '{root_config}', '-']})
    refix.run(p.x.root, p.peers, cfg)
    [call] = [json.loads(x) for x in calls.read_text().splitlines()]
    assert call['argv'] == ['task', '--job', '--config', origin['root_config'], '-']
    spec = call['spec']
    assert set(spec) <= set(fleet.JOB_KEYS), sorted(set(spec) - set(fleet.JOB_KEYS))
    assert spec['lifecycle'] == 'dev_task'
    d = lifecycle_deliver.validate_deliver(spec['deliver'])
    assert d['via'] == 'rollout' and d['feature_branch'] == origin['branch'] and d['repo'] == origin['repo']
    assert d['refix']['root'] == origin['refix']['root'] == rid
    assert d['refix']['attempt'] == origin['refix']['attempt'] + 1 == 1
    if restart is None:
        assert 'restart' not in d['rollout']
    else:
        assert d['rollout']['restart'] == restart
