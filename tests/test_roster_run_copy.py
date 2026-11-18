"""start --roster <file outside the run>: the run follows its own copy (docs/distributed-development.md, open question 5).

The copy is ``<run>/roster.yaml``. Paths the roster resolves against its own directory are written as absolute
paths against the source's directory, so they resolve the same; the source is never read or written again.
Temporary directories and stubs only; no session starts.
"""
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from test_advisor import fake_start
from fleetmux import cmux_manager, fleet, roster

TEAM = """# my team
workers:
  - name: worker-1
    role: roles/worker-1.md
  - name: alfa-1
subfleets:
  - name: mem
    supervisor_slot: alfa-1
    supervisor_role: roles/mem-sup.md
    roster: alfa_roster.yaml
"""


def source_dir(root):
    src = root / 'src' / 'peer-kit'
    (src / 'roles').mkdir(parents=True)
    (src / 'roles' / 'worker-1.md').write_text('role one\n')
    (src / 'roles' / 'mem-sup.md').write_text('sub-fleet supervisor\n')
    (src / 'alfa_roster.yaml').write_text('workers:\n  - name: extract-1\n')
    (src / 'team.yaml').write_text(TEAM)
    return src


def started(tmp_path):
    latest = fleet.load(tmp_path / 'latest.json')
    return Path(latest['run']), fleet.load(latest['config'])


def test_an_outside_roster_is_copied_into_the_run_and_followed_there(monkeypatch, tmp_path):
    src = source_dir(tmp_path)
    before = (src / 'team.yaml').read_bytes()
    fleet.main(fake_start(monkeypatch, tmp_path) + ['--roster', str(src / 'team.yaml')])
    run, cfg = started(tmp_path)
    copy = run.resolve() / 'roster.yaml'
    assert cfg['roster'] == str(copy) and copy.is_file()
    assert fleet.roster_file(cfg) == copy  # what the daemon follows and hot-reloads
    assert cfg['workers'] == ['worker-1', 'alfa-1']
    assert cfg['worker_roles'] == {'worker-1': str((src / 'roles' / 'worker-1.md').resolve())}
    (sub,) = cfg['subfleets']
    assert sub['roster'] == str((src / 'alfa_roster.yaml').resolve())
    assert sub['supervisor_role'] == str((src / 'roles' / 'mem-sup.md').resolve())
    assert (src / 'team.yaml').read_bytes() == before  # the source is not written


def test_relative_paths_resolve_the_same_in_the_copy(tmp_path):
    src = source_dir(tmp_path)
    (src / 'bin').mkdir()
    (src / 'app').mkdir()
    (src / 'app' / 'params.yaml').write_text('applet: {}\n')
    text = TEAM.replace('    roster: alfa_roster.yaml\n', '    roster: alfa_roster.yaml\n    claude: bin/claude\n') + (
        'tabs:\n  - {name: web, command: "true"}\n  - {name: app, command: "true", cwd: app}\n'
        'applets:\n  - {name: applet-tunnel-1, command: [x], cwd: app, params: {file: app/params.yaml, block: applet}}\n'
        '  - {name: applet-other-1, command: [x]}\n')
    source = src / 'team.yaml'
    source.write_text(text)
    run = tmp_path / 'run'
    run.mkdir()
    copy = roster.copy_into_run(source, run)
    assert copy == run.resolve() / 'roster.yaml'
    for path, base in ((source, src), (copy, run)):
        raw = path.read_bytes()
        got = (roster.parse_entries(raw, base), roster.subfleets(raw, base), roster.tabs(raw, base),
               roster.applets(raw, base))
        if path == source:
            want = got
    assert got == want


def test_a_roster_without_relative_paths_is_copied_byte_for_byte(tmp_path):
    source = tmp_path / 'team.yaml'
    source.write_text('# my team\nworkers:\n  - name: worker-1\n')
    run = tmp_path / 'run'
    run.mkdir()
    assert roster.copy_into_run(source, run).read_text() == '# my team\nworkers:\n  - name: worker-1\n'


def test_a_roster_inside_the_run_is_not_copied_again(tmp_path):
    run = tmp_path / 'run'
    run.mkdir()
    inside = run / 'fleet-config' / 'roster.yaml'
    inside.parent.mkdir()
    inside.write_text('workers:\n  - name: worker-1\n')
    stamp = os.stat(inside).st_mtime_ns
    assert roster.copy_into_run(inside, run) == inside.resolve()
    assert not (run / 'roster.yaml').exists() and os.stat(inside).st_mtime_ns == stamp


def test_the_daemon_reloads_on_copy_edits_and_ignores_source_edits(monkeypatch, tmp_path):
    src = source_dir(tmp_path)
    fleet.main(fake_start(monkeypatch, tmp_path) + ['--roster', str(src / 'team.yaml')])
    _run, cfg = started(tmp_path)
    reloads = []
    f = SimpleNamespace(cfg=cfg, roster_followed=None, roster_stamp=None, roster_digest=None,
                        night_tick=lambda path: None, reload_roster=reloads.append, workers=[], desired=[],
                        profile_swap_tick=lambda: None)
    fleet.Fleet.roster_tick(f)
    assert reloads == [Path(cfg['roster'])]
    (src / 'team.yaml').write_text(TEAM + '  - name: worker-2\n')
    fleet.Fleet.roster_tick(f)
    assert len(reloads) == 1, 'a source edit never reaches the fleet'
    copy = Path(cfg['roster'])
    copy.write_text(copy.read_text().replace('alfa-1\n', 'alfa-1\n  - name: worker-2\n', 1))
    st = copy.stat()
    os.utime(copy, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    fleet.Fleet.roster_tick(f)
    assert reloads == [copy, copy]


def resume_stubs(monkeypatch, tmp_path):
    monkeypatch.setattr(fleet, 'daemon_running', lambda path: False)
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: True)
    monkeypatch.setattr(fleet, 'connect', lambda cfg: object())
    monkeypatch.setattr(fleet, 'LATEST', tmp_path / 'latest.json')
    mgr = SimpleNamespace(spawn_window=lambda *a, **kw: SimpleNamespace(success=True))
    monkeypatch.setattr(fleet, 'manager_for', lambda cfg: mgr)


def test_resume_keeps_the_run_copy_and_copies_a_new_outside_roster(monkeypatch, tmp_path):
    run = tmp_path / 'run'
    run.mkdir()
    copy = run / 'roster.yaml'
    copy.write_text('workers:\n  - name: worker-1\n')
    cfg = dict(run=str(run), python=sys.executable, workers=['worker-1'], lifecycle='service', mode='service',
               supervisor_cli='codex', worker_cli='claude', transport_version=2, allow_development_v2=True,
               roster=str(copy))
    fleet.save(run / 'config.json', cfg)
    resume_stubs(monkeypatch, tmp_path)
    assert fleet.resume(run, SimpleNamespace(max_attempts=None, roster=None, advisor=False))
    assert fleet.load(run / 'config.json')['roster'] == str(copy)
    outside = tmp_path / 'team.yaml'
    outside.write_text('workers:\n  - name: worker-1\n  - name: worker-2\n')
    assert fleet.resume(run, SimpleNamespace(max_attempts=None, roster=str(outside), advisor=False))
    assert fleet.load(run / 'config.json')['roster'] == str(copy.resolve())
    assert 'worker-2' in copy.read_text()


def test_kit_fleet_still_follows_its_fleet_config_copy(monkeypatch, tmp_path):
    from test_kit_fleet import kit_with_fleet, writable
    kit = kit_with_fleet(tmp_path / 'kit')
    try:
        fleet.main(fake_start(monkeypatch, tmp_path) + ['--worker-kit', str(kit), '--kit-fleet', 'demo'])
        run, cfg = started(tmp_path)
        assert cfg['roster'] == str((run / 'fleet-config').resolve() / 'roster.yaml')
        assert not (run / 'roster.yaml').exists()
    finally:
        writable(kit)


def test_the_copy_path_is_named_in_the_docs():
    text = (Path(__file__).resolve().parents[1] / 'docs' / 'design.md').read_text()
    assert 'copies it to `<run>/roster.yaml`' in text and 'Edit the copy, not the source' in text
    text = (Path(__file__).resolve().parents[1] / 'docs' / 'distributed-development.md').read_text()
    assert '**Implemented as recommended:** the copy is `<run>/roster.yaml`' in text


def test_start_names_the_copy_to_edit(monkeypatch, tmp_path, capsys):
    src = source_dir(tmp_path)
    fleet.main(fake_start(monkeypatch, tmp_path) + ['--roster', str(src / 'team.yaml')])
    _run, cfg = started(tmp_path)
    out = capsys.readouterr().out
    assert 'roster copied into the run; edit this copy while the run goes on: ' + cfg['roster'] in out
