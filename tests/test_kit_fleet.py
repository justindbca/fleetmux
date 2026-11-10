"""start --kit-fleet NAME (design.md §30): the roster template comes from <worker kit>/fleets/NAME/, copied into the run."""
import hashlib
import os
import shutil
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_advisor import fake_start
from test_kit import make_kit
from cmux_fleet import cmux_manager, fleet, kit as kitmod, roster

ROSTER = """workers:
  - name: worker-1
    role: roles/worker-1.md
  - name: memx-1
subfleets:
  - name: mem
    supervisor_slot: memx-1
    supervisor_role: roles/mem-sup.md
    roster: memx_roster.yaml
"""


def kit_with_fleet(root, name='demo', read_only=True):
    kit = make_kit(root)
    d = kit / 'fleets' / name
    (d / 'roles').mkdir(parents=True)
    (d / 'roster.yaml').write_text(ROSTER)
    (d / 'roles' / 'worker-1.md').write_text('role one\n')
    (d / 'roles' / 'mem-sup.md').write_text('sub-fleet supervisor\n')
    (d / 'roles' / 'extract.md').write_text('extract role\n')
    (d / 'memx_roster.yaml').write_text('workers:\n  - name: extract-1\n    role: roles/extract.md\n')
    if read_only:  # like a hashed kit version
        for path in sorted(d.rglob('*'), reverse=True) + [d]:
            os.chmod(path, 0o555 if path.is_dir() else 0o444)
    return kit


def writable(tree):
    for path in sorted(Path(tree).rglob('*'), reverse=True) + [Path(tree)]:
        os.chmod(path, os.stat(path).st_mode | stat.S_IWUSR)


@pytest.fixture
def kit(tmp_path):
    kit = kit_with_fleet(tmp_path / 'kit')
    yield kit
    if kit.exists():
        writable(kit)  # let pytest clean tmp_path


def started(tmp_path):
    latest = fleet.load(tmp_path / 'latest.json')
    return Path(latest['run']), fleet.load(latest['config'])


def test_kit_accepts_a_fleets_dir_without_checking_it(tmp_path):
    kit = make_kit(tmp_path / 'kit')
    (kit / 'fleets' / 'x').mkdir(parents=True)
    (kit / 'fleets' / 'x' / 'roster.yaml').write_text('not: [a roster')  # its content is not the kit's business
    assert set(kitmod.Kit(kit).domains) == {'research_web', 'us_options', 'fleet'}


def test_new_start_copies_the_fleet_and_runs_like_roster(monkeypatch, tmp_path, kit):
    argv = fake_start(monkeypatch, tmp_path)
    fleet.main(argv + ['--worker-kit', str(kit), '--kit-fleet', 'demo'])
    run, cfg = started(tmp_path)
    copy = (run / 'fleet-config').resolve()
    assert sorted(p.relative_to(copy).as_posix() for p in copy.rglob('*')) == [
        'memx_roster.yaml', 'roles', 'roles/extract.md', 'roles/mem-sup.md', 'roles/worker-1.md', 'roster.yaml']
    assert cfg['roster'] == str(copy / 'roster.yaml')
    assert cfg['workers'] == ['worker-1', 'memx-1']
    assert cfg['worker_roles'] == {'worker-1': str(copy / 'roles' / 'worker-1.md')}
    (sub,) = cfg['subfleets']  # the sub-roster's relative paths resolve inside the copy
    assert sub['name'] == 'mem' and sub['roster'] == str(copy / 'memx_roster.yaml')
    assert sub['supervisor_role'] == str(copy / 'roles' / 'mem-sup.md')
    assert roster.load_entries(sub['roster'])[1] == {'extract-1': str(copy / 'roles' / 'extract.md')}
    assert cfg['kit_fleet'] == {'name': 'demo', 'kit': str(kit.resolve()),
                                'roster_md5': hashlib.md5((kit / 'fleets' / 'demo' / 'roster.yaml').read_bytes()).hexdigest()}
    # the copy is the run's own: the advisor switch can write it although the kit is read-only
    assert fleet.advisor_rosters(cfg)[0] == copy / 'roster.yaml'
    fleet.advisor_switch(cfg, True)
    assert roster.advisor((copy / 'roster.yaml').read_bytes()) is True
    assert roster.advisor((kit / 'fleets' / 'demo' / 'roster.yaml').read_bytes()) is False


def test_new_start_equals_roster_on_the_copy(monkeypatch, tmp_path, kit):
    argv = fake_start(monkeypatch, tmp_path)
    fleet.main(argv + ['--worker-kit', str(kit), '--kit-fleet', 'demo', '--advisor'])
    run, by_kit = started(tmp_path)
    copy = run / 'fleet-config' / 'roster.yaml'
    assert roster.advisor(copy.read_bytes()) is True and not (run / 'roster.yaml').exists()
    fleet.main(argv + ['--worker-kit', str(kit), '--roster', str(copy)])
    _, by_roster = started(tmp_path)
    for key in ('roster', 'workers', 'worker_roles', 'worker_profiles', 'subfleets'):
        assert by_kit[key] == by_roster[key], key
    assert 'kit_fleet' not in by_roster


@pytest.mark.parametrize('extra,match', [
    ([], 'needs --worker-kit'),
    (['--worker-kit', 'KIT', '--kit-fleet', 'other'], 'has no .*fleets/other/roster.yaml'),
    (['--worker-kit', 'KIT', '--kit-fleet', 'Bad_Name'], 'name must match'),
    (['--worker-kit', 'KIT', '--kit-fleet', '1x'], 'name must match'),
    (['--worker-kit', 'KIT', '--roster', 'ROSTER'], 'replaces --roster'),
    (['--worker-kit', 'KIT', '--workers', '2'], 'replaces --roster'),
    (['--worker-kit', 'KIT', '--worker-names', 'a,b'], 'replaces --roster'),
])
def test_refused_before_provisioning(monkeypatch, tmp_path, kit, extra, match):
    argv = fake_start(monkeypatch, tmp_path)
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: pytest.fail('refused before provisioning'))
    extra = [{'KIT': str(kit), 'ROSTER': str(kit / 'fleets' / 'demo' / 'roster.yaml')}.get(a, a) for a in extra]
    if '--kit-fleet' not in extra:
        extra += ['--kit-fleet', 'demo']
    with pytest.raises(SystemExit, match=match):
        fleet.main(argv + extra)
    assert not (tmp_path / 'runs').exists()


def test_resume_keeps_the_hot_edited_copy_without_the_kit(monkeypatch, tmp_path, kit):
    argv = fake_start(monkeypatch, tmp_path)
    fleet.main(argv + ['--worker-kit', str(kit), '--kit-fleet', 'demo'])
    run, cfg = started(tmp_path)
    copy = run / 'fleet-config' / 'roster.yaml'
    edited = copy.read_text().replace('  - name: memx-1\n', '  - name: memx-1\n  - name: worker-3\n') + 'advisor: on\n'
    copy.write_text(edited)  # hot edits while running: one worker added, the advisor turned on
    writable(kit)
    shutil.rmtree(kit)  # the kit version is gone
    monkeypatch.setattr(fleet, 'daemon_running', lambda path: False)
    monkeypatch.setattr(fleet, 'connect', lambda cfg: SimpleNamespace(hgetall=lambda key: {}))
    commands = []
    monkeypatch.setattr(fleet, 'manager_for', lambda cfg: SimpleNamespace(
        spawn_window=lambda *a, **kw: commands.append(a) or SimpleNamespace(success=True)))
    fleet.main(['start', '--resume', str(run), '--worker-kit', str(kit), '--kit-fleet', 'demo'])
    assert '--resume' in commands[0][1]
    assert copy.read_text() == edited  # not copied again
    saved = fleet.load(run / 'config.json')
    assert saved['roster'] == cfg['roster'] and saved['kit_fleet'] == cfg['kit_fleet']
    with pytest.raises(SystemExit, match='replaces --roster'):  # the flags still exclude each other on resume
        fleet.main(['start', '--resume', str(run), '--kit-fleet', 'demo', '--roster', str(copy)])
